# FreeHive — AI Handoff Document (v0.2.0)

## Project Identity

- **Internal name:** FreeHive
- **Package name:** `ilee_ai` (in `package.json` and `src-tauri/Cargo.toml`)
- **Repo location:** `/home/nazmoney/Ilee_AI`
- **Current version:** v0.2.0
- **Git branch:** `master`

---

## Project Goal

An open-source desktop app and local REST API that gives free programmatic access to frontier AI models (Claude, ChatGPT, Gemini) by automating their web UIs and CLI tools. Users plug in their own accounts. All data stays local. It doubles as the free inference layer for a personal multi-agent AI system called JARVIS.

**v1 target:** Localhost web app — SvelteKit on port 1420 (Tauri dev) / 5173 (Vite dev), FastAPI on port 8000. No Tauri packaging yet.

**v2 target:** Packaged Tauri desktop executable with Python backend as a sidecar binary.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Desktop wrapper | Tauri v2 (Rust) — scaffolded, not yet used for packaging |
| Frontend | SvelteKit 2 + Svelte 5, adapter-static, Vite on port 1420 |
| Backend | Python FastAPI + uvicorn on port 8000 |
| HTTP client (backend) | httpx (async) |
| Credential encryption | Fernet via `cryptography` library |
| Credential storage | `~/.claude/.credentials.json` (written by openclaude or Claude Code CLI) |

---

## Architecture

```
User
  └── Browser / Tauri webview (http://localhost:1420 or 5173)
        └── SvelteKit frontend
              └── axios → FastAPI backend (:8000)
                    └── SessionManager
                          └── ClaudeDirectAdapter
                                └── httpx POST → api.anthropic.com/v1/messages
                                      (Bearer token from ~/.claude/.credentials.json)
```

---

## How Claude Authentication Works

This is the most important thing to understand.

Both **OpenClaude** (`@gitlawb/openclaude`) and **official Claude Code** (`@anthropic-ai/claude-code`) are Node.js CLI tools that authenticate with claude.ai via OAuth. After auth, both write the same credential format to `~/.claude/.credentials.json`:

```json
{
  "claudeAiOauth": {
    "accessToken": "sk-ant-oat01-...",
    "refreshToken": "sk-ant-ort01-...",
    "expiresAt": 1775358881353,
    "scopes": ["user:inference", "user:profile", "..."],
    "subscriptionType": "pro"
  }
}
```

The `accessToken` with `user:inference` scope can be used as a `Bearer` token directly against `api.anthropic.com/v1/messages` — no subscription API key needed. This is the core mechanism that makes FreeHive work for free.

**OpenClaude** is a fork of Claude Code's source code (leaked via npm source maps on March 31, 2026). It is functionally identical for auth purposes. It is the free-access path. Official **Claude Code** is Anthropic-maintained, requires Pro subscription, and is the stable/reliable path.

Both tools are supported in FreeHive's setup screen. Both write to the same credential file. FreeHive reads that file regardless of which tool created it.

---

## File Structure (relevant files only)

```
/home/nazmoney/Ilee_AI/
│
├── start.sh                          ← Run this to start both backend and frontend
├── requirements.txt                  ← pip install -r requirements.txt
├── package.json
├── vite.config.ts                    ← Port 1420, strictPort
├── svelte.config.js                  ← adapter-static, SPA mode
│
├── backend/
│   ├── main.py                       ← FastAPI app, CORS, registers routers
│   ├── router.py                     ← /chat, /models, /compare, /accounts CRUD, /chat/clear
│   ├── setup_router.py               ← /setup/status, /setup/install, /setup/auth/{tool}
│   ├── session_manager.py            ← Lazy-loads adapters, routes model name → adapter
│   ├── account_store.py              ← Fernet-encrypted credential store (~/.freehive/)
│   └── adapters/
│       ├── claude_direct_adapter.py  ← PRIMARY: OAuth token → api.anthropic.com directly
│       └── claude_adapter.py         ← OLD/UNUSED: subprocess openclaude --print <msg>
│
├── src/
│   ├── routes/
│   │   └── +page.svelte              ← Main app: setup check on mount, chat UI, sidebar
│   ├── lib/
│   │   ├── SetupScreen.svelte        ← First-run setup: prereq checks, install, auth flows
│   │   ├── AccountPanel.svelte       ← Account manager UI (future: browser automation)
│   │   ├── api.js                    ← axios wrappers: sendChat, getSetupStatus, clearHistory
│   │   └── store.js                  ← Svelte stores: messages, isLoading, selectedModel
│   └── app.html
│
└── src-tauri/
    ├── tauri.conf.json               ← Tauri config (frontendDist: ../build)
    └── src/lib.rs                    ← Boilerplate only — no custom Rust yet
```

---

## What Was Built

### v0.1.0 — Existed before this session

- Basic chat UI (SvelteKit)
- Old `ClaudeAdapter` that spawned `openclaude --print <msg>` as a subprocess (now unused)
- `account_store.py` with Fernet encryption (infrastructure for future browser automation)
- `AccountPanel.svelte` for managing stored credentials
- Basic FastAPI router with `/chat`, `/models`, `/compare`, `/accounts`

### v0.2.0 — Built in this session

**`backend/setup_router.py`** (new)
- `GET /setup/status` — detects Node.js, npm, ripgrep, openclaude, claude binary presence, and auth state from credential file. Uses `bash -l -c` (login shell) for all binary detection so nvm/pyenv-managed tools are found.
- `POST /setup/install` — runs `npm install -g <package>` via login shell, streams install output via SSE
- `GET /setup/auth/{tool}` — spawns the CLI tool inside a real PTY (`pty.openpty()`) so React Ink TUI renders correctly. Streams status via SSE. Polls credential file for changes. Opens browser via `webbrowser.open()` when auth URL detected in output. Strips ANSI escape codes from all output before processing.

**`backend/adapters/claude_direct_adapter.py`** (new)
- Reads `~/.claude/.credentials.json`
- Calls `api.anthropic.com/v1/messages` directly using `Authorization: Bearer <token>`
- Maintains conversation history per session instance (stateful, not stateless like the old adapter)
- Default model: `claude-sonnet-4-6`
- Raises clear errors on expired token, 401, 429

**`backend/session_manager.py`** (rewritten)
- Lazy-loads adapters on first use so startup never fails if not yet authenticated
- Added `clear_history(model)` and `clear_all_history()`

**`backend/main.py`** (updated)
- Registers `setup_router` under `/api` prefix
- Version bumped to 0.2.0

**`backend/router.py`** (updated)
- Added `POST /chat/clear` endpoint

**`src/lib/SetupScreen.svelte`** (new)
- Shows prerequisite status (Node.js, npm, ripgrep) with ✓/✗ and copy-paste install commands
- Per-tool cards for OpenClaude and Claude Code with install and auth buttons
- SSE stream log showing real-time output during install and auth
- Install buttons disabled if npm is missing
- Spinner shown during install/auth in progress
- "Continue" button enabled only when at least one tool is authenticated
- "↻ Refresh status" button

**`src/routes/+page.svelte`** (updated)
- Calls `getSetupStatus()` on `onMount`, shows `SetupScreen` if not ready, shows app if ready
- Added "New Chat" button that clears history on both frontend store and backend adapter
- Version badge updated to v0.2.0

**`src/lib/api.js`** (updated)
- Added `getSetupStatus()`
- Added `clearHistory(model)`

**`start.sh`** (new)
- Starts Python backend (activates venv, runs uvicorn) and SvelteKit dev server in parallel
- Checks for venv and node_modules before starting
- Handles Ctrl+C cleanup for both processes
- Prints the URL to open

**`requirements.txt`** (new)
- `fastapi`, `uvicorn`, `httpx`, `cryptography`, `pydantic` with pinned versions

---

## How to Run (Development)

```bash
cd /home/nazmoney/Ilee_AI
./start.sh
```

Then open `http://localhost:5173` in a browser.

Or manually:

```bash
# Terminal 1 — backend
source venv/bin/activate
uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload

# Terminal 2 — frontend
npm run dev
```

The development machine already has OpenClaude installed globally and authenticated (Pro account). The setup screen will show green on first load and the "Continue" button will be immediately available.

---

## Known Gaps / Not Yet Done

| Item | Notes |
|---|---|
| **Tauri sidecar** | Python backend is not bundled in a Tauri build. `tauri build` produces a working window but the backend is missing. Needs PyInstaller to compile backend → single binary, configure as sidecar in `tauri.conf.json`, add Rust spawn/kill code in `lib.rs`. Also needs `tauri://localhost` and `https://tauri.localhost` added to CORS origins in `backend/main.py`. |
| **Token refresh** | `ClaudeDirectAdapter._get_token()` raises an error when the token is expired instead of refreshing it. The refresh endpoint and flow are not implemented. User must re-authenticate manually via the setup screen. |
| **ChatGPT adapter** | Stubbed as "coming soon" in the sidebar. `account_store.py` and `AccountPanel.svelte` infrastructure exists for storing credentials but no Playwright browser automation is built yet. |
| **Gemini adapter** | Same situation as ChatGPT. |
| **Markdown rendering** | Chat bubbles use `white-space: pre-wrap` plain text only. No markdown parser. Code blocks, lists, bold, etc. show as raw syntax. |
| **Compare UI** | `POST /api/compare` backend endpoint exists and works. No frontend UI for it. |
| **Windows compatibility** | `pty.openpty()` in `setup_router.py` is Unix-only. The auth flow will crash on Windows. Needs `winpty` or a platform guard with a fallback. |
| **`claude_adapter.py`** | The old subprocess adapter is now dead code. Can be safely deleted. |
| **AccountPanel form bug** | Default `form.model` is `'chatgpt'` but the dropdown only has `gemini` and `claude`. Minor UI inconsistency. |
| **Hardcoded "connected" status** | Chat header always shows green dot + "connected" regardless of actual backend reachability. |
| **GUI app PATH** | When launched from a desktop icon rather than a terminal, the Python process may have a minimal PATH. `bash -l -c` mitigates this for most Linux/macOS cases but may fail depending on how the app is launched. |

---

## Suggested Next Steps (in order)

1. **Token refresh** — implement the OAuth refresh flow in `ClaudeDirectAdapter` using the `refreshToken` from the credentials file so sessions stay alive without manual re-auth
2. **Markdown rendering** — add `marked` to the frontend and render assistant message content as HTML
3. **Live "connected" status** — replace the hardcoded green dot with a real health check against the backend
4. **ChatGPT adapter via Playwright** — use stored credentials from `account_store.py` to automate `chat.openai.com`
5. **Gemini adapter via Playwright** — same pattern for `gemini.google.com`
6. **Tauri sidecar (v2)** — PyInstaller bundle + sidecar config + Rust lifecycle management

# Terminal 1 — Chrome with CDP
pkill -9 -f chrome
sleep 2
/usr/bin/google-chrome --remote-debugging-port=9222 --user-data-dir=/home/nazmoney/.config/google-chrome --no-first-run &
sleep 3
curl http://localhost:9222/json  # verify it's up

# Terminal 2 — Backend
cd ~/Ilee_AI
source venv/bin/activate
uvicorn backend.main:app --host 127.0.0.1 --port 7200 --reload

# Terminal 3 — Frontend
cd ~/Ilee_AI
npm run dev