use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::Mutex;

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;
#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x08000000;

use tauri::Manager;

struct BackendProcess(Mutex<Option<Child>>);

fn backend_binary_name() -> &'static str {
    #[cfg(target_os = "windows")]
    {
        "freehive-backend.exe"
    }
    #[cfg(not(target_os = "windows"))]
    {
        "freehive-backend"
    }
}

fn sidecar_candidates(app: &tauri::App) -> Vec<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    let bin_name = backend_binary_name();
    let app_name = "freehive-backend";

    if let Ok(resource_dir) = app.path().resource_dir() {
        // onedir layout: sidecar/freehive-backend/freehive-backend(.exe)
        candidates.push(resource_dir.join("sidecar").join(app_name).join(bin_name));
        // onefile layout (legacy): sidecar/freehive-backend(.exe)
        candidates.push(resource_dir.join("sidecar").join(bin_name));
        candidates.push(resource_dir.join(bin_name));
    }

    if let Ok(exe_path) = std::env::current_exe() {
        if let Some(parent) = exe_path.parent() {
            candidates.push(parent.join("sidecar").join(app_name).join(bin_name));
            candidates.push(parent.join("sidecar").join(bin_name));
            candidates.push(parent.join(bin_name));
        }
    }

    candidates
}

/// Kill whatever process is holding port 7200 so we can bind cleanly.
fn kill_stale_backend() {
    #[cfg(target_os = "windows")]
    {
        // 1. Kill by port using PowerShell — catches any process name, no console window
        let _ = Command::new("powershell")
            .args([
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-NetTCPConnection -LocalPort 7200 -ErrorAction SilentlyContinue \
                 | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }",
            ])
            .creation_flags(CREATE_NO_WINDOW)
            .output();
        // 2. Kill by name + process tree (/T) as a fallback, no console window
        let _ = Command::new("taskkill")
            .args(["/F", "/T", "/IM", "freehive-backend.exe"])
            .creation_flags(CREATE_NO_WINDOW)
            .output();
    }
    #[cfg(not(target_os = "windows"))]
    {
        // Kill by port (lsof) and by name
        let _ = Command::new("sh")
            .args(["-c", "lsof -ti tcp:7200 | xargs kill -9 2>/dev/null; pkill -f freehive-backend 2>/dev/null"])
            .output();
    }
}

fn spawn_backend_if_available(app: &tauri::App) -> Option<Child> {
    if cfg!(debug_assertions) {
        return None;
    }

    let target = sidecar_candidates(app)
        .into_iter()
        .find(|candidate| Path::new(candidate).exists())?;

    kill_stale_backend();

    // Wait for the OS to fully release the port
    std::thread::sleep(std::time::Duration::from_millis(1500));

    let mut cmd = Command::new(target);
    cmd.env("FREEHIVE_BACKEND_HOST", "127.0.0.1")
       .env("FREEHIVE_BACKEND_PORT", "7200")
       .env("FREEHIVE_BACKEND_RELOAD", "0");
    #[cfg(target_os = "windows")]
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd.spawn().ok()
}

fn stop_backend(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<BackendProcess>() {
        if let Ok(mut guard) = state.0.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}

/// Return the path to the bundled Arena Chrome extension so the user can load it unpacked.
#[tauri::command]
fn get_extension_path(app_handle: tauri::AppHandle) -> Result<String, String> {
    if let Ok(resource_dir) = app_handle.path().resource_dir() {
        let ext_dir = resource_dir.join("extensions").join("arena");
        if ext_dir.exists() {
            return Ok(ext_dir.to_string_lossy().to_string());
        }
    }
    Err("Arena extension not found in bundled resources".to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let child = spawn_backend_if_available(app);
            if !cfg!(debug_assertions) && child.is_none() {
                return Err("Missing backend sidecar executable in bundled resources".into());
            }
            app.manage(BackendProcess(Mutex::new(child)));
            Ok(())
        })
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![greet, get_extension_path])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| match event {
            tauri::RunEvent::ExitRequested { .. } => stop_backend(app_handle),
            tauri::RunEvent::Exit => stop_backend(app_handle),
            _ => {}
        });
}
