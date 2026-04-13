# FreeHive — Handoff Document (v0.5.0 Final)

## Quick Status

| Adapter | Status | Notes |
|---|---|---|
| Claude | ✅ Working | OAuth direct API |
| ChatGPT | ✅ Working | codex exec subprocess |
| Gemini | ✅ Working | Free API key |
| Arena | ❌ Broken | Needs Playwright rewrite |

---

## What's Complete

### Core Infrastructure
- FastAPI backend on port **7200**
- SvelteKit frontend on port **5173**
- SQLite conversation DB at `~/.freehive/conversations.db`
- Full session system — every chat requires `session_id`
- `POST /sessions` → create session
- `POST /chat` → send message with session_id
- `GET /sessions/{id}/messages` → full history
- History persists across restarts, rebuilds from DB on resume
- Markdown rendering in frontend
- `+ New Chat` clears session and history

### Claude Adapter
- OAuth Bearer token from `~/.claude/.credentials.json`
- Hits `api.anthropic.com/v1/messages` with `anthropic-beta: oauth-2025-04-20`
- Auto token refresh via `platform.claude.com/v1/oauth/token`
- Full conversation history, DB-backed persistence

### ChatGPT Adapter
- `codex exec --skip-git-repo-check -m gpt-5.4` subprocess
- History injected as plain text context per call
- Output parser handles 3 response modes (full banner, partial, bare)

### Gemini Adapter
- Free API key, model: `gemini-2.0-flash-latest`
- Full conversation history, DB-backed persistence
- Role mapping: `assistant` → `model` for Gemini format

### Frontend
- Sidebar with Claude, ChatGPT, Gemini model buttons
- Arena section — collapsible, shows models, privacy warning modal
- Session auto-created on first message per model
- Privacy warning shown when Arena model selected

### LMArenaBridge Fixes Applied (even though broken)
- Domain migration: `lmarena.ai` → `arena.ai` in constants.py, main.py, transport.py
- Cloudscraper NameError fix
- These fixes are committed but Arena still doesn't work

---

## What's Not Complete

### 1. Arena Adapter (Priority)
**Current state:** `arena_adapter.py` calls LMArenaBridge at localhost:8000 which is broken.

**Root cause:** Arena.ai's bot detection rejects all programmatic requests — empty SSE responses despite 200 OK. Camoufox headless browser gets flagged. Not a config issue, a fundamental bot detection issue.

**Solution:** Replace LMArenaBridge entirely with **Playwright persistent Chromium adapter**.

**Build plan:**
- `backend/adapters/arena_playwright_adapter.py` — new file
- Persistent Chromium profile at `~/.freehive/arena_profile/`
- Headless after first login, completely invisible
- Login flow: headless=False once for user to log in, then headless=True forever
- Send messages via `page.evaluate()` injecting fetch() into arena.ai page
- Capture SSE stream from injected fetch
- Parse `a0:` chunks for text content
- Same session/history interface as other adapters

```python
from playwright.async_api import async_playwright

context = await playwright.chromium.launch_persistent_context(
    user_data_dir="~/.freehive/arena_profile",
    headless=True,
    args=["--disable-blink-features=AutomationControlled"]
)
```

**Install:**
```bash
source ~/Ilee_AI/venv/bin/activate
pip install playwright
playwright install chromium
```

### 2. Anthropic-Compatible API Endpoint
**What it is:** Claw Code (open source Claude Code reimplementation) and other tools use `ANTHROPIC_BASE_URL` to point at a custom endpoint. FreeHive needs a `/v1/messages` endpoint that speaks the Anthropic API format so these tools can use FreeHive as a free drop-in.

**Build plan:**
```python
# Add to router.py
@router.post("/v1/messages")
async def anthropic_messages(request: AnthropicMessagesRequest):
    # Extract model, messages, max_tokens from Anthropic format
    # Create/reuse session
    # Route through session_manager
    # Return Anthropic-format response
```

**Usage after building:**
```bash
export ANTHROPIC_BASE_URL="http://localhost:7200"
export ANTHROPIC_AUTH_TOKEN="freehive"
# Now Claw Code, JARVIS, any Anthropic SDK tool uses FreeHive for free
```

**Also add OpenAI-compatible endpoint:**
```python
@router.post("/v1/chat/completions")  # OpenAI format
```
This lets tools like Claw Code's OpenAI mode, Continue.dev, and others point at FreeHive.

### 3. CDP Integration (Deferred)
Connect to user's already-running Chrome via Chrome DevTools Protocol. Zero extra memory, perfect fingerprint, fastest option. Requires Chrome launched with `--remote-debugging-port=9222`. Skip until after Playwright version is working.

### 4. Gemini API Key → Account Store
Currently hardcoded in `gemini_adapter.py`. Should be moved to encrypted account store so users can set their own key through the UI.

### 5. Version Number
Still shows `v0.2.0` in frontend sidebar. Update to `v0.5.0` in `+page.svelte`.

### 6. Tauri Packaging (v2)
Python backend not bundled. Needs PyInstaller + Tauri sidecar config. Deferred to v2.

---

## Current Issues

### Arena broken
**Issue:** Empty SSE responses from arena.ai despite 200 OK.
**Cause:** Bot detection — arena.ai silently returns empty streams for non-trusted browser sessions.
**Fix:** Playwright persistent Chromium (see above).

### LMArenaBridge port conflict
**Issue:** LMArenaBridge runs on port 8000, same as many default services.
**Status:** FreeHive moved to 7200, LMArenaBridge stays on 8000 — no conflict.
**Note:** LMArenaBridge is being replaced by Playwright adapter, so this becomes irrelevant.

### ChatGPT codex ToS gray area
**Issue:** Using `codex exec` as a subprocess to extract responses is technically automating a CLI tool not meant for programmatic use.
**Risk:** Low for personal use, higher for high-frequency JARVIS calls.
**Future fix:** When OpenAI releases an affordable API option or when a better free path is found.

---

## File Locations

```
/home/nazmoney/Ilee_AI/
├── start.sh                          ← ./start.sh to run everything
├── backend/
│   ├── main.py                       ← FastAPI app
│   ├── router.py                     ← all endpoints
│   ├── session_manager.py            ← routes model → adapter
│   ├── conversation_manager.py       ← SQLite DB layer
│   ├── arena_manager.py              ← LMArenaBridge manager (legacy)
│   └── adapters/
│       ├── claude_direct_adapter.py  ← WORKING
│       ├── chatgpt_adapter.py        ← WORKING
│       ├── gemini_adapter.py         ← WORKING (needs key)
│       └── arena_adapter.py         ← BROKEN, replace with playwright version
├── src/
│   ├── routes/+page.svelte           ← main UI
│   └── lib/
│       ├── api.js                    ← axios + session management
│       └── store.js
└── LMArenaBridge/                    ← cloned, legacy, not actively used
```

---

## Next Steps (in order)

1. `pip install playwright && playwright install chromium`
2. Build `backend/adapters/arena_playwright_adapter.py`
3. Update `session_manager.py` to use new Arena adapter
4. Update `router.py` arena endpoints for Playwright lifecycle
5. Test Arena end to end
6. Add `/v1/messages` Anthropic-compatible endpoint
7. Add `/v1/chat/completions` OpenAI-compatible endpoint
8. Test Claw Code pointing at FreeHive
9. Update version to v0.5.0 in frontend
10. Commit and push

---

## How to Run

```bash
cd ~/Ilee_AI
./start.sh
# Open http://localhost:5173
```

Manual:
```bash
source venv/bin/activate
uvicorn backend.main:app --host 127.0.0.1 --port 7200 --reload
# separate terminal:
npm run dev
```

LMArenaBridge (if needed for testing):
```bash
cd ~/Ilee_AI/LMArenaBridge
PYTHONPATH=~/.local/lib/python3.12/site-packages:/usr/lib/python3/dist-packages /usr/bin/python3 -m src.main
```