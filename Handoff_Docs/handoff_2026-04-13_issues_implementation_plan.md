# 📄 HANDOFF DOC: FreeHive — Issues Analysis & Implementation Plan

**Date:** 2026-04-13 | **Agent/Dev:** Claude Sonnet
**Current Scope/Goal:** Full analysis of 10 reported issues across startup, auth, UI, and backend — root causes identified, implementation plans defined per issue.

---

## ✅ COMPLETED

- [x] Full repo analysis — all frontend/backend/Tauri files reviewed

---

## 🎯 ISSUE-BY-ISSUE BREAKDOWN

---

### 🐛 ISSUE 1 — App freezes on initial startup

**Why it happens:**
- `freehive-backend.exe` is a PyInstaller `--onefile` bundle. On first run it extracts its entire payload (~30MB) to a temp folder before uvicorn starts. This extraction blocks for 5–15s.
- Tauri's `spawn_backend_if_available()` blocks the main thread (1.5s `sleep` we added), then the WebView loads immediately and starts the 15-retry loop. The app appears frozen because the WebView has nothing to render while both the extraction AND the sleep are happening.

**Root files:**
- `src-tauri/src/lib.rs` — spawns backend synchronously, sleeps 1.5s on main thread
- `src/lib/SetupScreen.svelte:79` — retry loop

**Fix plan:**
1. **Switch sidecar from `--onefile` to `--onedir`** → `scripts/build_backend_sidecar.py`
   - Remove `--onefile` flag, add `--distpath src-tauri/sidecar/freehive-backend-dir`
   - Update `src-tauri/tauri.conf.json` resources to `"sidecar/freehive-backend-dir/**/*"`
   - Update `src-tauri/src/lib.rs` binary path to `freehive-backend-dir/freehive-backend.exe`
   - **Result:** No extraction on startup, instant launch
2. **Move backend spawn off main thread** → `src-tauri/src/lib.rs`
   - Wrap `kill_stale_backend()` + `Command::new(target).spawn()` in `std::thread::spawn()`
   - Remove the 1.5s sleep (no longer needed if spawn is async)
3. **Show loading spinner in WebView** → `src/lib/SetupScreen.svelte`
   - During retry attempts show "Starting FreeHive backend…" with a spinner instead of blank screen

---

### 🐛 ISSUE 2 — Setup page missing ChatGPT option

**Why it happens:**
- `SetupScreen.svelte` `TOOL_META` object defines only 3 tools: `openclaude`, `claude_code`, `gemini_cli`. ChatGPT (`chatgpt_cli` / Codex) is never rendered.
- `setup_router.py` has `chatgpt_cli` auth logic implemented (`/api/setup/auth/chatgpt_cli`) but the frontend never shows it.

**Root files:**
- `src/lib/SetupScreen.svelte:16–56` — `TOOL_META` object, missing `chatgpt_cli`
- `backend/setup_router.py` — has ChatGPT auth endpoints already

**Fix plan:**
1. Add `chatgpt_cli` to `TOOL_META` in `SetupScreen.svelte`:
```js
chatgpt_cli: {
    name: 'ChatGPT (Codex)',
    tag: 'Free access',
    tagColor: 'gray',
    headline: 'Official OpenAI Codex CLI',
    bullets: [
        'Works with a free OpenAI account',
        'Uses Codex CLI for authentication',
        'Auth handled silently — FreeHive controls everything',
    ],
    warn: null,
},
```
2. Add it to the tool selection grid in the template (alongside the 3 existing cards)
3. Verify `backend/setup_router.py` `/api/setup/auth/chatgpt_cli` and `/api/setup/install/chatgpt_cli` are implemented — they are, just need frontend exposure

---

### 🐛 ISSUE 3 — Gemini/ChatGPT "Link" in Accounts page goes to Settings overlay instead of auth

**Why it happens:**
- `AccountPanel.svelte` has an `authenticate()` function that calls `api.authenticateTool(tool)`. But that function streams from `/api/setup/auth/{tool}`.
- The "link" button in the accounts page likely navigates to the settings page (`view = 'settings'`) instead of calling `authenticate()`.
- Looking at `+page.svelte`, the accounts page button likely dispatches `showSettings` or changes `view` instead of triggering the auth SSE flow.

**Root files:**
- `src/lib/AccountPanel.svelte` — `authenticate()` function exists but button wiring may be wrong
- `src/routes/+page.svelte` — view routing logic
- `src/lib/api.js:authenticateTool()` — SSE auth streaming, correct

**Fix plan:**
1. In `AccountPanel.svelte`, locate the "Link" button for Gemini and ChatGPT — confirm it calls `authenticate(tool)` not a navigation action
2. If it navigates: change the handler to call `authenticate(tool)` directly, which streams `/api/setup/auth/{tool}`
3. The auth flow for Gemini opens a browser (`setup_router.py` uses `webbrowser.open`) — confirm this still works on Windows via `shell=True`
4. For ChatGPT/Codex auth: `setup_router.py` runs `npx @openai/codex auth` — confirm Codex CLI is installable and this works
5. Add visible progress/log output in AccountPanel during auth (same SSE log pattern as SetupScreen)

---

### 🐛 ISSUE 4 — Model list stuck on fallback after linking, no auto-reload

**Why it happens:**
- `model_discovery.py` runs `run_discovery_background()` after successful auth in `setup_router.py`
- But the **frontend never re-fetches models after setup completes**. `+page.svelte` loads `availableModels` once at mount and never refreshes.
- The backend log confirms models ARE discovered (`[ModelDiscovery] Claude: found 9 models, tier=pro`) but the frontend doesn't know to reload.

**Root files:**
- `src/routes/+page.svelte` — `onMount` fetches models once
- `src/lib/api.js:getAvailableModels()` — works, just not called again
- `backend/model_discovery.py` — correctly discovers and caches to `~/.freehive/config.json`

**Fix plan:**
1. In `+page.svelte` after setup completes (on `SetupScreen` `ready` event), call `getAvailableModels(refresh=true)` and update the store
2. Add a **"Refresh Models" button** in the sidebar or settings — calls `getAvailableModels(refresh=true)` → `POST /api/setup/models/refresh`
3. Alternatively: poll `/api/setup/models` every 30s and update if changed (less preferred)
4. Update `availableModels` store in `store.js` reactively when refresh returns

```js
// In +page.svelte, inside the 'ready' event handler:
async function onSetupReady(e) {
    setupDone = true;
    const freshModels = await getAvailableModels(true); // refresh=true
    availableModels.set(freshModels);
}
```

---

### 🐛 ISSUE 5 — Claude Sonnet returns 429 (rate limited), Haiku works

**Why it happens:**
- `429 Too Many Requests` from `api.anthropic.com/v1/messages` means the account hit rate limits on Sonnet. Sonnet has much tighter RPM/TPM limits than Haiku, especially on free/low-tier Claude accounts.
- `claude_direct_adapter.py` makes raw API calls — if the token is a free-tier OAuth token, Sonnet may not be available or severely rate-limited.
- The adapter doesn't implement retry-with-backoff or surface the 429 gracefully.

**Root files:**
- `backend/adapters/claude_direct_adapter.py` — no retry logic on 429
- `backend/router.py` — returns 503 to frontend on any adapter error

**Fix plan:**
1. **Add exponential backoff retry** in `claude_direct_adapter.py` for 429 responses:
```python
import asyncio
for attempt in range(3):
    resp = await client.post(...)
    if resp.status_code == 429:
        await asyncio.sleep(2 ** attempt)
        continue
    break
```
2. **Surface 429 specifically to frontend** — return `{"error": "rate_limited", "retry_after": N}` instead of generic 503, so the UI can show "Rate limited — try again in Xs" instead of a generic error
3. **Add model availability check** — if Sonnet is 429ing consistently, auto-suggest Haiku in the UI
4. **Note:** This is an account-level issue — the user's Claude account may need Pro tier for Sonnet access at normal rates. Not fully fixable in code.

---

### 🐛 ISSUE 6 — Backend terminal window shows on launch; need hidden backend + in-app log viewer

**Why it happens:**
- On Windows, `Command::new(target).spawn()` in `lib.rs` opens a console window because the process inherits the parent's console subsystem. The PyInstaller EXE is built as a console app.
- No log streaming from backend to frontend exists currently.

**Root files:**
- `src-tauri/src/lib.rs:72` — `Command::new(target).spawn()`
- `backend/main.py` — logging goes to stdout/stderr only
- `src/lib/SettingsPage.svelte` — no backend logs tab

**Fix plan:**

**Part A — Hide the terminal window:**
1. In `src-tauri/src/lib.rs`, add Windows-specific creation flags to hide the window:
```rust
#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

Command::new(target)
    .env(...)
    .creation_flags(0x08000000) // CREATE_NO_WINDOW
    .spawn()
```
The constant `0x08000000` is `CREATE_NO_WINDOW` — the process runs silently.

**Part B — In-app backend log viewer:**
1. **Backend**: Add a `/api/logs` SSE endpoint in `backend/router.py` that streams from a `logging.Handler` that buffers to a `asyncio.Queue`
2. **Frontend**: Add "Backend Logs" tab to `SettingsPage.svelte` — `EventSource('/api/logs')` that appends lines to a scrolling `<pre>` element
3. **Log handler** (`backend/main.py`):
```python
import asyncio, logging
log_queue = asyncio.Queue(maxsize=500)

class QueueHandler(logging.Handler):
    def emit(self, record):
        try:
            log_queue.put_nowait(self.format(record))
        except asyncio.QueueFull:
            pass

logging.getLogger().addHandler(QueueHandler())
```
4. **SSE endpoint** (`backend/router.py`):
```python
@router.get("/logs")
async def stream_logs():
    async def generate():
        while True:
            line = await log_queue.get()
            yield f"data: {line}\n\n"
    return StreamingResponse(generate(), media_type="text/event-stream")
```

---

### 🐛 ISSUE 7 — Provider color coding missing in recent chats

**Why it happens:**
- The sessions list in `+page.svelte` renders chat history but applies no provider-specific color. The `model` field is stored per session so color can be derived from it.

**Root files:**
- `src/routes/+page.svelte` — sessions sidebar rendering
- `src/lib/store.js` — provider color could be defined here

**Fix plan:**
1. Add a provider color map in `store.js`:
```js
export const PROVIDER_COLORS = {
    claude: '#C17A3A',    // Anthropic brownish-gold
    chatgpt: '#FFFFFF',   // OpenAI white
    gemini: '#4285F4',    // Google blue
    openclaude: '#C17A3A',
};
```
2. In `+page.svelte` sessions list, derive provider from session `model` field:
```js
function providerFromModel(model) {
    if (!model) return 'claude';
    if (model.startsWith('claude') || model === 'openclaude') return 'claude';
    if (model.startsWith('gemini')) return 'gemini';
    if (model.startsWith('gpt') || model.startsWith('o1') || model.startsWith('o3') || model.startsWith('chatgpt')) return 'chatgpt';
    return 'claude';
}
```
3. Apply as a colored left-border or dot on each session list item:
```svelte
<div class="session-item" style="border-left: 3px solid {PROVIDER_COLORS[providerFromModel(s.model)]}">
```

---

### 🐛 ISSUE 8 — Copy button on API keys doesn't work

**Why it happens:**
- `SettingsPage.svelte` has copy buttons but `navigator.clipboard.writeText()` requires a secure context (HTTPS or localhost). The Tauri WebView2 origin is `https://tauri.localhost` which should be secure, but if the button handler has a JS error or the clipboard API is blocked, it silently fails.
- Also common: the button calls `writeText()` on an undefined value if the ref to the input is wrong.

**Root files:**
- `src/lib/SettingsPage.svelte` — copy button handler

**Fix plan:**
1. Replace clipboard call with a fallback that works in all contexts:
```js
async function copyToClipboard(text) {
    try {
        await navigator.clipboard.writeText(text);
    } catch {
        // Fallback for non-secure contexts
        const el = document.createElement('textarea');
        el.value = text;
        el.style.position = 'fixed';
        el.style.opacity = '0';
        document.body.appendChild(el);
        el.select();
        document.execCommand('copy');
        document.body.removeChild(el);
    }
    // Show visual confirmation
    copyConfirmed = true;
    setTimeout(() => copyConfirmed = false, 1500);
}
```
2. Confirm the value passed to copy is the actual key string, not an element reference

---

### 🐛 ISSUE 9 — No way to return to home/chat from Accounts or Settings page

**Why it happens:**
- `+page.svelte` controls `view` state (`'chat'`, `'accounts'`, `'settings'`). There is no back/home button rendered inside `AccountPanel.svelte` or `SettingsPage.svelte`.

**Root files:**
- `src/routes/+page.svelte` — view state controller
- `src/lib/AccountPanel.svelte`, `src/lib/SettingsPage.svelte` — missing home button

**Fix plan:**
1. Both components dispatch a `close` event:
```svelte
<!-- In AccountPanel.svelte and SettingsPage.svelte header -->
<button class="close-btn" on:click={() => dispatch('close')}>✕</button>
```
2. In `+page.svelte`, handle the event:
```svelte
<AccountPanel on:close={() => view = 'chat'} />
<SettingsPage on:close={() => view = 'chat'} />
```
3. Style: position the `✕` button top-right of the panel, or add a `← Back` button top-left
4. Also wire the sidebar "New Chat" icon to set `view = 'chat'` if currently in accounts/settings

---

### 🐛 ISSUE 10 — Backend keeps running after frontend window is closed

**Why it happens:**
- Tauri's `ExitRequested` and `Exit` events call `stop_backend()` in `lib.rs`, which kills the child process. BUT if the user closes the window without the app fully exiting (e.g., system tray, or window close ≠ process exit in some Tauri configs), the backend stays alive.
- The backend has no independent check that the frontend is still alive.

**Root files:**
- `src-tauri/src/lib.rs` — `stop_backend()` on `Exit`/`ExitRequested`
- `backend/router.py` or `backend/main.py` — no heartbeat endpoint

**Fix plan:**

**Part A — Tauri side (ensure exit kills backend):**
In `lib.rs`, also handle `tauri::RunEvent::WindowEvent` with `WindowEvent::CloseRequested`:
```rust
tauri::RunEvent::WindowEvent { event: tauri::WindowEvent::CloseRequested { .. }, .. } => {
    stop_backend(app_handle);
}
```

**Part B — Backend heartbeat (belt-and-suspenders):**
1. Add `GET /api/ping` endpoint that returns `{"ok": true}`
2. In `backend/main.py`, start a background task on startup that polls for a "parent alive" signal:
```python
import asyncio, os, sys

async def watchdog():
    """Shut down if parent Tauri process dies."""
    parent_pid = int(os.getenv("FREEHIVE_PARENT_PID", "0"))
    if not parent_pid:
        return
    while True:
        await asyncio.sleep(5)
        try:
            os.kill(parent_pid, 0)  # check if PID exists
        except (ProcessLookupError, PermissionError):
            sys.exit(0)  # parent gone, shut down

@app.on_event("startup")
async def startup():
    asyncio.create_task(watchdog())
```
3. In `lib.rs`, pass the Tauri PID to the backend:
```rust
Command::new(target)
    .env("FREEHIVE_PARENT_PID", std::process::id().to_string())
    ...
```

---

## 📋 IMPLEMENTATION ORDER (Priority)

| # | Issue | Effort | Impact | Do First? |
|---|-------|--------|--------|-----------|
| 6A | Hide terminal window | XS (2 lines Rust) | High | ✅ YES |
| 9 | Add close/back button | XS | High | ✅ YES |
| 8 | Fix copy button | XS | Medium | ✅ YES |
| 4 | Auto-reload models | S | High | ✅ YES |
| 7 | Provider color coding | S | Medium | YES |
| 2 | Add ChatGPT to setup | S | High | YES |
| 5 | 429 retry + better error | S | Medium | YES |
| 3 | Fix Gemini/ChatGPT auth link | M | High | YES |
| 10 | Backend watchdog | M | Medium | After above |
| 6B | In-app log viewer | M | Medium | After above |
| 1 | Fix startup freeze (onedir) | L | High | Last (rebuild) |

---

## 🔍 HOW IT WORKS (Critical Context)

- **View routing:** `+page.svelte` holds `let view = 'chat'` — switches between `<SetupScreen>`, `<AccountPanel>`, `<SettingsPage>`, and main chat. All navigation is controlled here.
- **Auth flow:** `api.authenticateTool(tool)` → SSE stream from `/api/setup/auth/{tool}` → backend runs CLI auth → emits `{status: 'success'}` → frontend marks done
- **Model discovery:** On auth success, backend calls `run_discovery_background()` → writes to `~/.freehive/config.json` → frontend must explicitly call `/api/setup/models` to pick up new data
- **Sessions:** In-memory dict in `session_manager.py` + SQLite via `conversation_manager.py` in OS app data dir
- **Adapters:** `claude_direct_adapter` → `api.anthropic.com`, `chatgpt_direct_adapter` → WebSocket `wss://chatgpt.com`, `gemini_direct_adapter` → Google Code Assist HTTPS

---

## 🚫 DO NOT TOUCH

- `backend/main.py:22–28` → `allow_origins=["*"]` — DO NOT revert, WebView2 CORS fix
- `backend/main.py:58–73` → `sys.frozen` uvicorn check — DO NOT remove
- `src-tauri/src/lib.rs:39–65` → `kill_stale_backend()` — DO NOT remove
- `backend/adapters/claude_direct_adapter.py:token refresh logic` → handles 5-min buffer, works correctly

---

## 🔄 ATTEMPTED & FAILED (Avoid Repeating)

- Specific CORS origins → Failed, use `["*"]`
- `--onefile` PyInstaller → Causes startup freeze, switch to `--onedir`
- `taskkill` only for port cleanup → Failed, PowerShell `Get-NetTCPConnection` required first

---

## 📁 KEY FILES FOR EACH FIX

| Fix | File(s) |
|-----|---------|
| Hide terminal | `src-tauri/src/lib.rs` |
| Close button | `src/lib/AccountPanel.svelte`, `src/lib/SettingsPage.svelte`, `src/routes/+page.svelte` |
| Copy button | `src/lib/SettingsPage.svelte` |
| Model reload | `src/routes/+page.svelte`, `src/lib/api.js` |
| Provider colors | `src/routes/+page.svelte`, `src/lib/store.js` |
| ChatGPT setup card | `src/lib/SetupScreen.svelte` |
| 429 retry | `backend/adapters/claude_direct_adapter.py`, `backend/router.py` |
| Auth link fix | `src/lib/AccountPanel.svelte` |
| Watchdog | `backend/main.py`, `src-tauri/src/lib.rs` |
| Log viewer | `backend/router.py`, `backend/main.py`, `src/lib/SettingsPage.svelte` |
| Startup freeze | `scripts/build_backend_sidecar.py`, `src-tauri/tauri.conf.json`, `src-tauri/src/lib.rs` |
