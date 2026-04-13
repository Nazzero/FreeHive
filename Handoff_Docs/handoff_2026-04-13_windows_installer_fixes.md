# 📄 HANDOFF DOC: FreeHive Windows Installer & Backend Fixes

**Date:** 2026-04-13 | **Agent/Dev:** Claude Sonnet 4.6
**Current Scope/Goal:** Build working MSI and NSIS (.exe) installers for FreeHive (Tauri v2 + Python FastAPI sidecar) and fix all runtime errors preventing the app from reaching its backend after installation.

---

## ✅ COMPLETED

- [x] **PyInstaller sidecar build** → `scripts/build_backend_sidecar.py` | Status: Stable
  - Installed `pyinstaller` into `venv/` (was missing)
  - Installed all `requirements.txt` deps into `venv/` (uvicorn, fastapi, etc. were missing — first builds were hollow)
- [x] **MSI + NSIS installer output** → `src-tauri/target/release/bundle/msi/FreeHive_0.1.0_x64_en-US.msi` and `src-tauri/target/release/bundle/nsis/FreeHive_0.1.0_x64-setup.exe` | Status: Stable
- [x] **Frozen-env uvicorn fix** → `backend/main.py:72` | Status: Stable
  - PyInstaller EXE crashed with `Error loading ASGI app. Could not import module "backend.main"` because uvicorn was invoked with a module string inside a frozen bundle
  - Fixed: detect `sys.frozen` and pass `app` object directly instead of string
- [x] **CORS open for WebView2** → `backend/main.py:22` | Status: Stable
  - WebView2 origin (`https://tauri.localhost`) was blocked by FastAPI's CORS middleware despite being listed — changed to `allow_origins=["*"]`, `allow_credentials=False`
- [x] **Frontend retry logic** → `src/lib/SetupScreen.svelte:79` | Status: Stable
  - Single-shot fetch showed error before backend could start — replaced with 15-attempt retry loop (1s delay each)
- [x] **Port 7200 cleanup on launch** → `src-tauri/src/lib.rs:39` | Status: Stable
  - Stale processes from previous sessions caused `[WinError 10048] only one usage of each socket address` — fixed with PowerShell `Get-NetTCPConnection` kill + `taskkill /F /IM freehive-backend.exe` fallback
- [x] **Git init + push to GitHub** → `https://github.com/NazzWay/FreeHive` | Status: Stable
  - Configured identity: `NazzWay / nasseromar81666@gmail.com`
  - Merged with existing remote (conflicts resolved keeping local fixes)
  - Sidecar binaries excluded via `.gitignore`

---

## ⚠️ INCOMPLETE / FAILED

- [ ] **App fully working end-to-end** → `src/lib/SetupScreen.svelte` | Issue: CORS fix and retry logic were confirmed at build time but not verified in a full installed-app run by the user. Last screenshot showed 16x `/api/setup/status` 200 OK responses with error still displayed — root cause confirmed as CORS, fix applied, not yet re-tested by user.
- [ ] **`ripgrep` prerequisite** → `backend/setup_router.py` | Issue: `/api/setup/status` returns `"ripgrep": false` — not installed on target machine, may block some features
- [ ] **Sidecar startup time** → PyInstaller onefile EXEs extract to a temp dir on first run, which adds 5–15s cold-start delay on slow machines. The 15-retry × 1s loop may not be enough on very slow machines.

---

## 🎯 NEXT STEPS & FUTURE PIPELINE

**Immediate Priorities (To Unblock/Finish Current Scope):**

1. **Verify CORS fix works in installed app** → Run `FreeHive_0.1.0_x64-setup.exe`, open app, confirm setup screen loads without error | Expected: No "Cannot reach FreeHive backend" message
2. **If still failing, add origin logging** → `backend/main.py` — temporarily log `request.headers.get("origin")` in a middleware to see the exact origin WebView2 sends | Expected: Identify exact origin string to target
3. **Install ripgrep** → `winget install BurntSushi.ripgrep.MSVC` on end-user machine | Expected: `"ripgrep": true` in `/api/setup/status`

**Subsequent Tasks (To Execute Once Unblocked):**

- [ ] **Increase retry timeout or add loading indicator** → `src/lib/SetupScreen.svelte` — show "Starting backend… (attempt X/15)" instead of blank loading state during retries
- [ ] **Switch sidecar from onefile to onedir** → `scripts/build_backend_sidecar.py` — remove `--onefile`, bundle as directory — eliminates extraction delay, faster startup. Tauri `resources` config would need updating to `sidecar/**/*`
- [ ] **Sign the installer** → `src-tauri/tauri.conf.json` — add Windows code signing cert to avoid SmartScreen warnings on install
- [ ] **Version bump workflow** → `package.json` + `src-tauri/tauri.conf.json` + `src-tauri/Cargo.toml` — all three must match for Tauri builds

- 🧪 Verification Command: `curl -s http://127.0.0.1:7200/api/setup/status` — should return JSON with `"ready": false/true` and no port errors

---

## 🔍 HOW IT WORKS (Critical Context)

- **Flow:** Tauri (`ilee_ai.exe`) launches → Rust `lib.rs` kills port 7200 → spawns `freehive-backend.exe` (PyInstaller bundle of `backend/main.py`) → uvicorn binds `127.0.0.1:7200` → SvelteKit WebView fetches `/api/setup/status` every 1s until ready
- **Key Configs/Env:**
  - `FREEHIVE_BACKEND_HOST=127.0.0.1`, `FREEHIVE_BACKEND_PORT=7200`, `FREEHIVE_BACKEND_RELOAD=0` — set by Rust before spawning sidecar
  - `src-tauri/tauri.conf.json` → `bundle.resources: ["sidecar/*"]` — bundles sidecar dir into installer
  - `src-tauri/tauri.conf.json` → `build.beforeBuildCommand: "npm run build && npm run build:sidecar"` — auto-runs PyInstaller before Rust compile
- **State/Storage:** SQLite DB for conversations managed by `backend/conversation_manager.py` — location is OS app data dir at runtime

---

## 🚫 DO NOT TOUCH

- `backend/main.py:22–28` → CORS is intentionally `allow_origins=["*"]` — reverting to specific origins will break WebView2 fetch (confirmed)
- `backend/main.py:58–73` → `sys.frozen` check before `uvicorn.run()` — removing this breaks the PyInstaller EXE with "Could not import module backend.main"
- `src-tauri/src/lib.rs:39–65` → `kill_stale_backend()` — removing causes `[WinError 10048]` port conflict on every launch after first use
- `src-tauri/sidecar/` → Listed in `.gitignore` — do not commit `.exe` or Linux ELF binaries here, they are build artifacts

---

## 🔄 ATTEMPTED & FAILED (Avoid Repeating)

- **`taskkill /F /IM freehive-backend.exe` alone** → Failed: Did not kill processes holding the port when process name differed or process was in a zombie state → Use PowerShell `Get-NetTCPConnection -LocalPort 7200` kill first, then taskkill as fallback
- **`netstat -ano | findstr ":7200 "` in Rust** → Failed: Regex pattern with trailing space missed some entries, Rust Command args didn't pipe correctly → Use PowerShell cmdlet instead
- **Specific CORS origins list** → Failed: `["tauri://localhost", "https://tauri.localhost", ...]` did not match actual WebView2 origin at runtime despite both variants being listed → Use `["*"]` with `allow_credentials=False`
- **Building sidecar before installing requirements** → Failed: PyInstaller packed a hollow EXE with no fastapi/uvicorn → Always run `venv/Scripts/pip install -r requirements.txt` before `npm run build:sidecar`
- **`uvicorn.run("backend.main:app", ...)` in frozen EXE** → Failed: Module string import doesn't work in PyInstaller frozen env → Use `uvicorn.run(app, ...)` with object reference when `sys.frozen` is True

---

## 💡 SUGGESTED FIXES / METHODS

- **If backend still won't start:** Run `dist/freehive-backend.exe` directly from terminal — check for missing DLL errors or import errors before blaming CORS/port
- **If CORS still fails:** Add `print(request.headers)` middleware in `backend/main.py` to log the actual `Origin` header WebView2 sends, then add it explicitly
- **If port conflict persists:** Run `powershell -Command "Get-NetTCPConnection -LocalPort 7200"` to identify PID, then `Stop-Process -Id <PID> -Force`
- **For faster iteration without full rebuild:** Only rebuild sidecar + Rust (skip frontend) with `npm run build:sidecar && cargo tauri build` from `src-tauri/`
- **For onedir sidecar:** Change `--onefile` to `--onedir` in `scripts/build_backend_sidecar.py`, update `tauri.conf.json` resources to `"sidecar/**/*"`

---

## 📁 KEY FILES & REFERENCES

- `backend/main.py` → FastAPI app entrypoint; CORS config and frozen-env uvicorn fix live here
- `src-tauri/src/lib.rs` → Rust: port cleanup, sidecar discovery, spawn/kill logic
- `src/lib/SetupScreen.svelte` → Frontend: backend health check retry loop and error display
- `src/lib/config.js` → `API_BASE_URL` = `http://127.0.0.1:7200/api` — change here to move backend port
- `scripts/build_backend_sidecar.py` → PyInstaller build script; hidden imports listed here
- `scripts/build_sidecar.cjs` → Node wrapper that finds Python and calls `build_backend_sidecar.py`
- `src-tauri/tauri.conf.json` → Bundle targets (`"all"` = MSI + NSIS), resource paths, beforeBuildCommand
- `src-tauri/sidecar/README.md` → Notes on sidecar binary placement
- `requirements.txt` → Python deps: fastapi, uvicorn, httpx, cryptography, pydantic, websockets
- `src-tauri/target/release/bundle/msi/FreeHive_0.1.0_x64_en-US.msi` → Windows MSI installer (build artifact)
- `src-tauri/target/release/bundle/nsis/FreeHive_0.1.0_x64-setup.exe` → Windows NSIS installer (build artifact)
- `https://github.com/NazzWay/FreeHive` → Main repo; branch `main` is current
