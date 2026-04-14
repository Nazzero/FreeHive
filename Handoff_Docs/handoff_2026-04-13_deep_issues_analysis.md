# 📋 FreeHive — Deep Issues Analysis & Fix Plan
**Date:** 2026-04-13 | **Analyst:** Claude Sonnet 4.6  
**Scope:** Full code audit of all 10 reported issues + hidden bugs discovered during analysis.  
**Audience:** The next AI agent or developer taking over this codebase. All root causes are verified against actual file contents — not guesses.

---

## 🗺️ HOW THE APP WORKS (Critical Orientation)

```
User launches FreeHive.exe (Tauri shell)
  → Rust lib.rs kills port 7200, spawns freehive-backend.exe (PyInstaller sidecar)
  → FastAPI / uvicorn binds 127.0.0.1:7200
  → SvelteKit WebView loads src/routes/+page.svelte
  → SetupScreen.svelte polls /api/setup/status every 1s (up to 15 tries)
  → Once status.ready == true, dispatches 'ready' event → main app unlocks
  → AccountPanel shows providers / handles login/logout
  → Chat goes through api.js → /api/sessions + /api/chat → session_manager → adapters
```

**Key config files:**
- `src/lib/config.js` — `API_BASE_URL = http://127.0.0.1:7200/api`
- `src/lib/store.js` — Svelte stores: `selectedModel`, `availableModels`, `messages`
- `backend/setup_router.py` — All setup, auth, model-discovery endpoints
- `backend/model_discovery.py` — Live API calls to Claude/ChatGPT/Gemini to get models
- `backend/router.py` — Chat, sessions CRUD
- `src/routes/+page.svelte` — App shell (1025 lines); manages activeView, models, session list

---

## ✅ USER-REPORTED ISSUES (10 items)

---

### ISSUE 1 — Console/terminal window flashes on launch (Windows)

**Reported:** A black console window appears briefly when the app starts.

**Root Cause:** `src-tauri/src/lib.rs` line 81-86  
```rust
Command::new(target)
    .env("FREEHIVE_BACKEND_HOST", "127.0.0.1")
    .env("FREEHIVE_BACKEND_PORT", "7200")
    .env("FREEHIVE_BACKEND_RELOAD", "0")
    .spawn()
    .ok()
```
`Command::new().spawn()` on Windows defaults to inheriting the parent's console. Since Tauri apps on Windows are `#![windows_subsystem = "windows"]` (no console), spawning a subprocess this way still creates a new console window for the child process momentarily.

Additionally, `kill_stale_backend()` (lines 44-56) runs PowerShell and `taskkill` — both of which flash a brief console window for each invocation because they're not created with `CREATE_NO_WINDOW`.

**Fix — `src-tauri/src/lib.rs`:**
```rust
use std::os::windows::process::CommandExt;
const CREATE_NO_WINDOW: u32 = 0x08000000;

// In spawn_backend_if_available():
let mut cmd = Command::new(target);
cmd.env("FREEHIVE_BACKEND_HOST", "127.0.0.1")
   .env("FREEHIVE_BACKEND_PORT", "7200")
   .env("FREEHIVE_BACKEND_RELOAD", "0");
#[cfg(target_os = "windows")]
cmd.creation_flags(CREATE_NO_WINDOW);
cmd.spawn().ok()

// In kill_stale_backend() for both powershell and taskkill calls:
let _ = Command::new("powershell")
    .args([...])
    .creation_flags(CREATE_NO_WINDOW)
    .output();
let _ = Command::new("taskkill")
    .args(["/F", "/IM", "freehive-backend.exe"])
    .creation_flags(CREATE_NO_WINDOW)
    .output();
```

---

### ISSUE 2 — ChatGPT has no setup/install option in the Setup Wizard

**Reported:** ChatGPT doesn't appear as a selectable option during initial setup.

**Root Cause:** `src/lib/SetupScreen.svelte` lines 16-56  
`TOOL_META` defines only 3 entries:
```javascript
const TOOL_META = {
    openclaude:  { ... },
    claude_code: { ... },
    gemini_cli:  { ... },
    // chatgpt_cli is MISSING
};
```
And `toolState` also only has 3 entries — no `chatgpt_cli`.  
The setup wizard loops over `Object.entries(TOOL_META)` to render tool cards, so ChatGPT never appears.

**Backend is already ready:** `backend/setup_router.py` line 56-57 already has:
```python
AUTH_BINARIES = {
    **CLI_BINARIES,
    "chatgpt_cli": "codex",
}
```
And `/api/setup/status` already returns `chatgpt_cli` status. The backend is fully wired — only the frontend is missing it.

**Fix — `src/lib/SetupScreen.svelte`:**  
Add `chatgpt_cli` to `TOOL_META` (around line 16):
```javascript
chatgpt_cli: {
    label: 'ChatGPT (Codex CLI)',
    description: 'OpenAI\'s Codex CLI — GPT-5 and all ChatGPT models',
    icon: '🤖',
    installCmd: 'npm install -g @openai/codex',
    docsUrl: 'https://github.com/openai/codex',
    loginTool: 'chatgpt_cli',
},
```
And add to `toolState`:
```javascript
chatgpt_cli: { installing: false, installLog: [], authing: false, authLog: [], authPhase: '' },
```
**Note:** `INSTALL_COMMANDS` in `backend/setup_router.py` line 42-46 does NOT include `chatgpt_cli`. Add it:
```python
INSTALL_COMMANDS = {
    "openclaude":  "npm install -g @gitlawb/openclaude",
    "claude_code": "npm install -g @anthropic-ai/claude-code",
    "gemini_cli":  "npm install -g @google/gemini-cli",
    "chatgpt_cli": "npm install -g @openai/codex",   # ← ADD THIS
}
```
Also add `chatgpt_cli` to `CLI_BINARIES` in the same file (line 48):
```python
CLI_BINARIES = {
    "openclaude":  "openclaude",
    "claude_code": "claude",
    "gemini_cli":  "gemini",
    "chatgpt_cli": "codex",   # ← ADD THIS
}
```
This is required so `/setup/select-tool` accepts `chatgpt_cli` (line 372: `if request.tool not in CLI_BINARIES`).

---

### ISSUE 3 — Clicking "Login" for Gemini/ChatGPT opens Settings instead of auth flow

**Reported:** The Login button for Gemini and ChatGPT goes to the Settings page rather than starting authentication.

**Root Cause:** `src/lib/AccountPanel.svelte` lines 267-269 (inside `handleLogin()`):
```javascript
} else if (!provider.installed) {
    dispatch('openSettings', { provider: provider.id });
    return;
}
```
When a non-Claude provider's CLI is **not installed**, `handleLogin` immediately dispatches `openSettings` and returns — it never reaches `authenticateTool()`. The `dispatch('openSettings', ...)` event bubbles up to `+page.svelte` which sets `activeView = 'settings'`.

This is wrong behavior for two reasons:
1. If the user wants to authenticate, they should get auth instructions, not settings.
2. Even if the intent was "go install it first," the Settings page has no install flow — it only shows API keys.

**Fix — `src/lib/AccountPanel.svelte`:**  
Replace the premature redirect with a prompt to install the CLI from the Setup tab:
```javascript
} else if (!provider.installed) {
    // Show inline message rather than navigating away
    providerError[provider.id] = `${provider.name} CLI is not installed. Go to Setup to install it first.`;
    return;
}
```
Or, better: dispatch a different event that takes the user to the Setup screen step for that provider, not generic Settings.

**Additional root cause:** `loginButtonLabel()` returns `'Open Setup'` when `!provider.installed`, suggesting the intent was to open the setup wizard — but the event dispatched is `openSettings` not `openSetup`. The receiver (`+page.svelte`) has no `on:openSetup` handler at all.

---

### ISSUE 4 — Model list doesn't update after completing setup / logging in

**Reported:** The model dropdown shows stale/hardcoded models after setup completes.

**Root Cause — Part A (setup):** `src/routes/+page.svelte` lines 77-89, `onSetupReady()`:
```javascript
function onSetupReady(event) {
    setupDone = true;
    chosenTool = event.detail?.tool;
    // ... sets up session source, loads sessions ...
    // ← MISSING: getAvailableModels() is NOT called here
}
```
After setup, the app never re-fetches models. `getAvailableModels()` is only called in `onMount` (line 45-75), which ran before auth was complete, so it got empty/fallback data.

**Root Cause — Part B (store defaults):** `src/lib/store.js` lines 44-65 initializes `availableModels` with hardcoded model IDs that may no longer be current:
```javascript
export const availableModels = writable({
    claude:  { tier: 'unknown', models: [{ id: 'claude-haiku-4-5', ... }] },
    chatgpt: { tier: 'unknown', models: [{ id: 'gpt-5.2', ... }] },
    gemini:  { tier: 'unknown', models: [{ id: 'gemini-3-flash-preview', ... }] },
});
```
If model discovery fails or setup hasn't completed, these stale defaults remain displayed.

**Root Cause — Part C (after auth):** `backend/setup_router.py` line 525: `asyncio.create_task(_run_model_discovery())` IS called after auth, but the frontend is never notified. There's no WebSocket/SSE push — the frontend must poll or re-fetch manually after auth.

**Fix:**  
In `+page.svelte`, call `getAvailableModels(true)` (with refresh=true) at the end of `onSetupReady()`:
```javascript
async function onSetupReady(event) {
    setupDone = true;
    chosenTool = event.detail?.tool;
    // ... existing code ...
    // ADD:
    try {
        const fresh = await getAvailableModels(true);
        availableModels.set(fresh);
        // Auto-select first model for the chosen provider
        if (chosenTool && fresh[providerForTool(chosenTool)]?.models?.length) {
            selectedModel.set(fresh[providerForTool(chosenTool)].models[0].id);
        }
    } catch (e) { /* non-fatal */ }
}
```
Also call `getAvailableModels(true)` in `AccountPanel.svelte` after a successful `authenticateTool()` response.

---

### ISSUE 5 — Sidecar startup delay (5–15 seconds on first launch)

**Reported:** App takes too long to load after installation.

**Root Cause:** `scripts/build_backend_sidecar.py` uses `--onefile` flag for PyInstaller.  
OneFILE mode compresses all Python files into a single EXE that extracts itself to `%TEMP%/_MEIxxxxxx/` on every launch. This extraction takes 5–15 seconds on slow machines or cold boots.

**Fix — `scripts/build_backend_sidecar.py`:**  
Remove `--onefile`, switch to `--onedir`:
```python
# Change this:
"--onefile",
# To: (just remove it — onedir is the default)
```
Then update `src-tauri/tauri.conf.json` resources to bundle the whole directory:
```json
"bundle": {
    "resources": ["sidecar/**/*"]
}
```
And update `sidecar_candidates()` in `lib.rs` to look for the binary inside the onedir folder:
```rust
// sidecar/freehive-backend/freehive-backend.exe (onedir layout)
candidates.push(resource_dir.join("sidecar").join("freehive-backend").join(bin_name));
// Existing paths as fallbacks...
candidates.push(resource_dir.join("sidecar").join(bin_name));
```
**Trade-off:** The sidecar dir will be ~80-150MB instead of one ~60MB exe, but startup is instant on every subsequent launch (no extraction).

---

### ISSUE 6 — No "Back" button / can't return from Accounts or Settings to Chat

**Reported:** Once users navigate to Accounts or Settings, there's no way to get back to chat.

**Root Cause:** `src/routes/+page.svelte` — `<AccountPanel>` and `<SettingsPage>` are mounted without close handlers:
```svelte
<!-- Line ~620 -->
<AccountPanel on:openSettings={() => activeView = 'settings'} />
<!-- No on:close handler -->

<!-- Line ~630 -->
<SettingsPage />
<!-- No on:close handler -->
```
Neither component dispatches a `close` event, and neither has a visible back/close button in their own UI.

**Fix:**  
1. Add a `close` event dispatch to both components.
2. In `AccountPanel.svelte`, add a close button at the top:
```svelte
<button class="back-btn" on:click={() => dispatch('close')}>← Back to Chat</button>
```
3. In `SettingsPage.svelte`, same pattern.
4. In `+page.svelte`, add the handler:
```svelte
<AccountPanel 
    on:openSettings={() => activeView = 'settings'}
    on:close={() => activeView = 'chat'} />
<SettingsPage on:close={() => activeView = 'chat'} />
```
Alternatively, add a persistent top-level nav bar with tabs (Chat / Accounts / Settings) so switching between views is always available.

---

### ISSUE 7 — Session list shows no provider/model info

**Reported:** The sessions sidebar shows only conversation titles with no indicator of which AI was used.

**Root Cause:** `src/routes/+page.svelte` lines 467-484, the session list renders:
```svelte
{#each filteredSessions as session}
    <button class="session-item ...">
        <span class="session-title">{session.title || 'Untitled'}</span>
        <!-- No model label, no provider dot, no timestamp -->
    </button>
{/each}
```
The `session` object from `/api/sessions` DOES include `model` and `created_at` fields (from `conversation_manager.py`), but they're not rendered.

**Fix:** Add a model badge to each session row:
```svelte
{#each filteredSessions as session}
    <button class="session-item ...">
        <span class="session-title">{session.title || 'Untitled'}</span>
        {#if session.model}
            <span class="session-model-badge" 
                  style="color: {providerColor(session.model)}">
                {shortModelName(session.model)}
            </span>
        {/if}
    </button>
{/each}
```
Where `providerColor()` and `shortModelName()` are small helper functions using the same logic as `selectedProvider` derived store.

---

### ISSUE 8 — Retry counter not shown during backend startup

**Reported:** The loading screen is blank for 15 seconds with no feedback.

**Root Cause:** `src/lib/SetupScreen.svelte`, `fetchStatus()` loop. Current retry loop (lines 79-102):
```javascript
for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
        const res = await fetch(`${BASE_URL}/setup/status`);
        // ...
    } catch {
        if (attempt < maxAttempts) {
            await new Promise((r) => setTimeout(r, delayMs));
        }
    }
}
```
There's no reactive variable updated per attempt — the UI just shows the same `loading` spinner with no text.

**Fix:** Add a reactive `startupAttempt` variable:
```javascript
let startupAttempt = 0;
// In fetchStatus():
for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    startupAttempt = attempt;  // ← triggers reactive update
    try { ... }
}
```
And in the template:
```svelte
{#if loading && startupAttempt > 1}
    <p class="startup-hint">Starting backend… ({startupAttempt}/{maxAttempts})</p>
{/if}
```

---

### ISSUE 9 — Deprecated `navigator.platform` usage

**Reported:** Browser deprecation warning in console; may break on future Chromium/WebView2 updates.

**Root Cause:** `src/lib/SetupScreen.svelte` line 214:
```javascript
const isWindows = navigator.platform?.startsWith('Win');
```
`navigator.platform` is deprecated in favor of `navigator.userAgentData.platform` (high entropy hint) or simply checking the user agent string.

**Fix:**
```javascript
const isWindows = 
    navigator.userAgentData?.platform?.toLowerCase().includes('windows') ??
    navigator.userAgent.toLowerCase().includes('win');
```
This uses the modern API with a fallback.

---

### ISSUE 10 — No loading state or error feedback during CLI auth flow

**Reported:** When clicking Login, the UI shows nothing for potentially 3 minutes (auth timeout).

**Root Cause:** `src/lib/AccountPanel.svelte` `handleLogin()` calls `authenticateTool(tool, onEvent)`. The `onEvent` callback receives SSE events from the backend (`status: 'starting'`, `status: 'waiting'`, `status: 'browser_opened'`, etc.) — but what the UI does with them depends on implementation.

From the code, `authLog[provider.id]` is populated with messages, but there's no prominent "status banner" in the AccountPanel UI showing "Waiting for browser login…" — the log entries may be in a collapsed or non-obvious section.

**Fix:** Ensure the most recent `status: 'waiting'` and `status: 'browser_opened'` messages are shown prominently in the UI — not just appended to a log list. Example:
```svelte
{#if toolState[provider.loginTool]?.authing}
    <div class="auth-status-banner">
        {toolState[provider.loginTool].authPhase || 'Starting auth...'}
    </div>
{/if}
```

---

## 🐛 HIDDEN BUGS (Found During Code Audit — Not Reported by User)

---

### HIDDEN BUG A — `chatgpt_cli` is NEVER persisted as `selected_tool`

**File:** `backend/setup_router.py` line 498-499
```python
def _persist_selected_tool() -> bool:
    return tool in ("openclaude", "claude_code", "gemini_cli")
```
`chatgpt_cli` is explicitly excluded. After a successful ChatGPT auth, `_set_selected_tool(tool)` is never called. The config file never gets `"selected_tool": "chatgpt_cli"`.

**Consequence:** On next app launch, `/api/setup/status` returns `"selected_tool": null`. The `ready` condition:
```python
"ready": (authed or gemini_ready or chatgpt_ready) and selected_tool_ready
selected_tool_ready = selected_tool is not None or chatgpt_ready
```
This means `ready=True` if chatgpt is authenticated (because `chatgpt_ready=True` makes `selected_tool_ready=True`). But `status.selected_tool` is `null`. In `SetupScreen.svelte`, if `status.selected_tool` is null, `chosenTool` is never set, and then when `onSetupReady` fires in `+page.svelte`, `event.detail.tool` is null — potentially causing null-reference errors downstream.

**Fix:** Add `chatgpt_cli` to `_persist_selected_tool()`:
```python
def _persist_selected_tool() -> bool:
    return tool in ("openclaude", "claude_code", "gemini_cli", "chatgpt_cli")
```

---

### HIDDEN BUG B — `_get_codex_client_version()` uses `bash` — broken on Windows

**File:** `backend/model_discovery.py` lines 173-182
```python
def _get_codex_client_version() -> str:
    try:
        result = subprocess.run(
            ["bash", "-l", "-c", "cat $(npm root -g)/@openai/codex/package.json"],
            ...
        )
```
`bash` is not available on Windows. This will always fail and return the hardcoded fallback `"0.118.0"`. If the actual installed Codex version differs significantly, the ChatGPT model list API may reject requests.

**Fix:** Use platform-appropriate approach:
```python
def _get_codex_client_version() -> str:
    import os
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/c", "npm root -g"],
                capture_output=True, text=True, timeout=5,
            )
        else:
            result = subprocess.run(
                ["bash", "-l", "-c", "npm root -g"],
                capture_output=True, text=True, timeout=5,
            )
        if result.returncode == 0:
            npm_root = result.stdout.strip()
            pkg_path = Path(npm_root) / "@openai" / "codex" / "package.json"
            if pkg_path.exists():
                data = json.loads(pkg_path.read_text())
                return data.get("version", "0.118.0")
    except Exception:
        pass
    return "0.118.0"
```

---

### HIDDEN BUG C — `getAvailableModels()` in `api.js` deletes `arena` key but stores still expect it to be absent

**File:** `src/lib/api.js` lines 274-276
```javascript
const data = (res.data && typeof res.data === 'object') ? { ...res.data } : {};
delete data.arena;
return data;
```
This is intentional (arena hidden until v2). But if the backend ever starts returning `arena` data that the discovery cache already has, and the frontend tries to render `availableModels` by iterating `Object.entries()`, the arena key will be absent in the response but might still be in the store from a prior stale state. Low risk now, but could cause confusion if arena is ever partially re-enabled.

---

### HIDDEN BUG D — `normalizeModelId()` doesn't handle `chatgpt_cli` or `gemini_cli` keys

**File:** `src/lib/api.js` lines 23-36
```javascript
function normalizeModelId(model) {
    if (id === 'claude' || id === 'chatgpt' || id === 'gemini') return id;
    if (id.startsWith('claude-') || id.startsWith('gpt-') || ...) return id;
```
If the user somehow passes `'chatgpt_cli'` or `'gemini_cli'` as a model ID (e.g., from a bug in chosenTool being used as a model), it will pass through as-is to the backend, which will fail with an unknown model error. The error message from the backend won't be helpful.

**Fix:** Add a guard that strips `_cli` suffixes or maps tool names to provider names:
```javascript
const TOOL_TO_PROVIDER = { chatgpt_cli: 'chatgpt', gemini_cli: 'gemini', openclaude: 'claude', claude_code: 'claude' };
if (TOOL_TO_PROVIDER[id]) return TOOL_TO_PROVIDER[id];
```

---

### HIDDEN BUG E — Backend process not hidden on Windows (visible in Task Manager with no window grouping)

**File:** `src-tauri/src/lib.rs` line 81  
Covered in Issue 1 above, but an additional concern: because `FREEHIVE_BACKEND_RELOAD=0` is passed but the backend still creates a uvicorn worker subprocess internally, on Windows there will be **two** `freehive-backend.exe` processes (the PyInstaller wrapper + the uvicorn worker). The `kill_stale_backend()` taskkill only kills by name, which should catch both — but the PowerShell port kill only kills the process owning the port socket. If the PyInstaller parent doesn't die when the child is killed, port will be freed but parent will remain.

**Fix:** No code change needed — the existing `taskkill /F /IM freehive-backend.exe` fallback handles this. But add `/T` flag to kill the process tree:
```rust
let _ = Command::new("taskkill")
    .args(["/F", "/T", "/IM", "freehive-backend.exe"])
    .creation_flags(CREATE_NO_WINDOW)
    .output();
```

---

### HIDDEN BUG F — `Gemini token expiry check is per-request, not on startup`

**File:** `backend/model_discovery.py` lines 318-323
```python
expiry_ms = creds.get("expiry_date", 0)
if (time.time() * 1000) >= expiry_ms:
    result["error"] = "Token expired — re-authenticate with: gemini auth login"
    result["models"] = _gemini_fallback_models("free")
    return result
```
The expiry is also checked in `_read_gemini_auth_status()` in `setup_router.py` line 183: `expired = (time.time() * 1000) >= expiry_ms`. So `gemini_cli.authenticated` in `/api/setup/status` will correctly show `false` when token is expired — the app will show "Re-authenticate." This is actually working correctly, but there's **no automatic token refresh**. Gemini OAuth tokens typically expire every 1 hour. The user will need to re-auth every hour.

**Improvement (not a bug fix):** Implement OAuth refresh token flow using the `refresh_token` field in `~/.gemini/oauth_creds.json` before expiry.

---

### HIDDEN BUG G — Session title never updated after first message

**File:** `backend/conversation_manager.py` (not read, inferred from router.py)  
In `router.py` lines 39-56, `create_session()` creates a DB record with `model`. There's no code visible that updates the session `title` after the first message — session titles in the sidebar likely remain as the session UUID or "Untitled" forever.

**Verification needed:** Read `backend/conversation_manager.py` to confirm whether `update_session_title()` is called anywhere after the first assistant response.

---

### HIDDEN BUG H — `getAvailableModels(refresh=false)` in `api.js` uses GET but backend `refresh` endpoint is POST

**File:** `src/lib/api.js` lines 266-272
```javascript
export async function getAvailableModels(refresh = false) {
    const url = refresh
        ? `${BASE_URL}/setup/models/refresh`
        : `${BASE_URL}/setup/models`;
    const res = refresh
        ? await axios.post(url)
        : await axios.get(url);
```
When `refresh=true`, it POSTs to `/setup/models/refresh`. In `setup_router.py` lines 709-714, this endpoint is `@setup_router.post("/setup/models/refresh")`. ✅ This matches.

When `refresh=false`, it GETs `/setup/models`. In `setup_router.py` lines 691-706, this is `@setup_router.get("/setup/models")` — but it has a `refresh: bool = False` query param. If someone calls `getAvailableModels()` with no arg, it GETs without the param, which is fine. ✅ This is correct.

**No bug here** — just confirmation the routing is correct.

---

### HIDDEN BUG I — `selected_tool_ready` logic in `/api/setup/status` has a logical hole

**File:** `backend/setup_router.py` lines 295-296
```python
selected_tool_ready = selected_tool is not None or chatgpt_ready
"ready": (authed or gemini_ready or chatgpt_ready) and selected_tool_ready,
```
Scenario: User authenticates with Claude (`authed=True`) but never selected a tool (e.g., first run, skipped tool selection). `selected_tool=None`, `chatgpt_ready=False`. So `selected_tool_ready=False`. Result: `ready=False`. This is correct behavior — forces tool selection.

But scenario 2: User authenticates ChatGPT (`chatgpt_ready=True`), `selected_tool=None`. `selected_tool_ready = None is not None or True = True`. `ready = (False or False or True) and True = True`. App marks ready, dispatches event. But `status.selected_tool = None`. The frontend in `SetupScreen.svelte` does:
```javascript
if (status.selected_tool) { chosenTool = status.selected_tool; step = 'setup'; }
if (status.ready) { dispatch('ready', { tool: status.selected_tool }); }
```
`dispatch('ready', { tool: null })` fires. In `+page.svelte`, `onSetupReady({ detail: { tool: null } })` runs. `chosenTool = null`. Any downstream code that does `chosenTool.startsWith(...)` will throw. 

**Fix:** Either always set `selected_tool` when chatgpt auth succeeds (see Hidden Bug A), or in `onSetupReady`, guard against null tool:
```javascript
chosenTool = event.detail?.tool ?? 'chatgpt_cli'; // fallback
```

---

## 📦 IMPLEMENTATION PRIORITY

| Priority | Issue | File(s) | Effort |
|----------|-------|---------|--------|
| 🔴 Critical | Hidden Bug A — chatgpt never set as selected_tool | `setup_router.py` line 499 | 1 line |
| 🔴 Critical | Hidden Bug I — null selected_tool crash | `+page.svelte` onSetupReady | 2 lines |
| 🟠 High | Issue 2 — ChatGPT missing from setup wizard | `SetupScreen.svelte`, `setup_router.py` | ~20 lines |
| 🟠 High | Issue 3 — Login button goes to Settings | `AccountPanel.svelte` handleLogin | ~5 lines |
| 🟠 High | Issue 4 — Models don't refresh after setup | `+page.svelte` onSetupReady | ~8 lines |
| 🟠 High | Issue 6 — No back button | `AccountPanel.svelte`, `SettingsPage.svelte`, `+page.svelte` | ~15 lines |
| 🟡 Medium | Issue 1 — Console flash | `lib.rs` | ~10 lines |
| 🟡 Medium | Hidden Bug B — bash on Windows | `model_discovery.py` | ~15 lines |
| 🟡 Medium | Hidden Bug E — taskkill /T | `lib.rs` kill_stale_backend | 1 char |
| 🟢 Low | Issue 7 — No model in session list | `+page.svelte` | ~10 lines |
| 🟢 Low | Issue 8 — No retry counter | `SetupScreen.svelte` | ~5 lines |
| 🟢 Low | Issue 5 — Onefile startup delay | `build_backend_sidecar.py`, `tauri.conf.json`, `lib.rs` | Medium refactor |
| 🟢 Low | Issue 9 — navigator.platform deprecated | `SetupScreen.svelte` line 214 | 2 lines |
| 🟢 Low | Issue 10 — Auth UI feedback | `AccountPanel.svelte` | ~10 lines |

---

## 📁 KEY FILE QUICK-REFERENCE

| File | Lines | Purpose |
|------|-------|---------|
| `src/routes/+page.svelte` | 1025 | App shell. `onSetupReady()` at line 77. `getAvailableModels()` at line 45. `activeView` controls routing. |
| `src/lib/SetupScreen.svelte` | ~817 | Setup wizard. `TOOL_META` at line 16. `fetchStatus()` retry at line 79. `navigator.platform` at line 214. |
| `src/lib/AccountPanel.svelte` | 565 | Provider auth UI. `handleLogin()` bad redirect at line 267. `toProviders()` maps backend status. |
| `src/lib/SettingsPage.svelte` | ~200 | API keys display only. No close button. No install flow. |
| `src/lib/api.js` | 278 | All API calls. `getAvailableModels()` at line 266. `authenticateTool()` SSE reader at line 183. |
| `src/lib/store.js` | 65 | Svelte stores. `availableModels` hardcoded defaults at line 44. |
| `src/lib/config.js` | ~5 | `API_BASE_URL = http://127.0.0.1:7200/api` |
| `backend/main.py` | 73 | FastAPI entrypoint. CORS at line 22. frozen-env uvicorn at line 62. |
| `backend/setup_router.py` | 737 | Setup/auth/models API. `chatgpt_cli` missing from `CLI_BINARIES`/`INSTALL_COMMANDS`. `_persist_selected_tool()` bug at line 498. |
| `backend/model_discovery.py` | 487 | Live model fetching + cache. `bash` hardcode bug at line 173. |
| `backend/router.py` | ~200 | Chat + sessions CRUD. |
| `src-tauri/src/lib.rs` | 125 | Rust: kill port, spawn sidecar. Missing `CREATE_NO_WINDOW`. Add `/T` to taskkill. |
| `scripts/build_backend_sidecar.py` | ~50 | PyInstaller build. `--onefile` causes startup delay. |

---

## 🚫 DO NOT TOUCH (Confirmed Working)

- `backend/main.py:22-28` — `allow_origins=["*"]` is intentional; reverting breaks WebView2
- `backend/main.py:58-73` — `sys.frozen` check; removing breaks PyInstaller EXE
- `src-tauri/src/lib.rs:39-65` — `kill_stale_backend()`; removing causes WinError 10048
- `src/lib/SetupScreen.svelte:79-102` — 15-retry loop; reduce and app shows error before backend starts
