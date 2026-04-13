# 📄 HANDOFF DOC: Arena Disabled + Bundled Build Flow

**Date:** 2026-04-12  
**Agent/Dev:** GPT-5 Codex  
**Scope:** Keep Arena fully offline/hidden for now, ensure non-Arena app flow works, and enforce bundled sidecar build for release artifacts.

## ✅ COMPLETED

- [x] Arena default feature flag added (OFF by default)  
  - `backend/feature_flags.py`
  - `FREEHIVE_ENABLE_ARENA=1` required to enable Arena explicitly.

- [x] Backend startup no longer initializes Arena unless enabled  
  - `backend/main.py`
  - `SessionManager` now receives `arena_enabled` at startup.

- [x] Arena blocked from session/chat API when disabled  
  - `backend/router.py`
  - `POST /sessions`, `POST /chat`, and session listing/lookup paths now reject/hide Arena models when disabled.
  - `/arena/*` endpoints return `404 Not found` when disabled.

- [x] Session routing hard-blocks Arena model handling when disabled  
  - `backend/session_manager.py`
  - Any `arena/*` session creation/message routing raises `"Arena is disabled in this build."`

- [x] Frontend model normalization no longer auto-routes unknown models to `arena/*`  
  - `src/lib/api.js`

- [x] Frontend provider derivation no longer treats `arena/*` as an active provider  
  - `src/lib/store.js`

- [x] Bundled build flow tightened for easier installer creation  
  - `scripts/build_sidecar.cjs` (cross-platform Python launcher for sidecar build)
  - `package.json` adds `build:sidecar`
  - `src-tauri/tauri.conf.json` now runs `npm run build && npm run build:sidecar` before Tauri release build.

- [x] Documentation updated to reflect Arena-off status and new build flow  
  - `README.md`

## 🧪 VALIDATION RUN

- `python3 -m py_compile backend/feature_flags.py backend/main.py backend/router.py backend/session_manager.py` → pass
- `npm run check` → pass (0 errors / 0 warnings)
- `npm run build` → pass
- `npm run build:sidecar` → pass; generated `src-tauri/sidecar/freehive-backend`
- `npm run tauri build -- --bundles deb,rpm` → pass; generated:
  - `src-tauri/target/release/bundle/deb/FreeHive_0.1.0_amd64.deb`
  - `src-tauri/target/release/bundle/rpm/FreeHive-0.1.0-1.x86_64.rpm`
- Runtime guard sanity check:
  - `SessionManager(...).create_session(..., "arena/test")` raises `RuntimeError: Arena is disabled in this build.`

## ⚠️ NOTES

- PyInstaller was installed in project venv and `build:sidecar` now succeeds.
- On fresh machines, install first:
  - `python3 -m pip install pyinstaller` (or venv equivalent)

## 🎯 RELEASE COMMANDS

1. Install build dependency:
   - `python3 -m pip install pyinstaller`
2. Build release (sidecar bundled automatically):
   - `npm run tauri build`
3. Linux-only no-AppImage variant:
   - `npm run tauri build -- --bundles deb,rpm`
