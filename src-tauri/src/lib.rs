use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::Mutex;

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

    if let Ok(resource_dir) = app.path().resource_dir() {
        candidates.push(resource_dir.join("sidecar").join(bin_name));
        candidates.push(resource_dir.join(bin_name));
    }

    if let Ok(exe_path) = std::env::current_exe() {
        if let Some(parent) = exe_path.parent() {
            candidates.push(parent.join("sidecar").join(bin_name));
            candidates.push(parent.join(bin_name));
        }
    }

    candidates
}

fn spawn_backend_if_available(app: &tauri::App) -> Option<Child> {
    if cfg!(debug_assertions) {
        return None;
    }

    let target = sidecar_candidates(app)
        .into_iter()
        .find(|candidate| Path::new(candidate).exists())?;

    Command::new(target)
        .env("FREEHIVE_BACKEND_HOST", "127.0.0.1")
        .env("FREEHIVE_BACKEND_PORT", "7200")
        .env("FREEHIVE_BACKEND_RELOAD", "0")
        .spawn()
        .ok()
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
        .invoke_handler(tauri::generate_handler![greet])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| match event {
            tauri::RunEvent::ExitRequested { .. } => stop_backend(app_handle),
            tauri::RunEvent::Exit => stop_backend(app_handle),
            _ => {}
        });
}
