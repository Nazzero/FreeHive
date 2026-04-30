# Qwen Provider Integration — Handoff Document

**Date**: 2026-04-30
**Branch**: `feat/qwen-provider` (based off `main`)
**Status**: Backend + frontend built, blocked on API access method

---

## What Was Built

All code is on `feat/qwen-provider` branch. Main branch is untouched.

### Files Created
- `backend/adapters/qwen_direct_adapter.py` — Full adapter with JWT auth, streaming, tool calling, model discovery

### Files Modified
- `backend/compat_router.py` — `freehive-qwen` key routing, model matching, real SSE streaming path
- `backend/model_discovery.py` — `discover_qwen_models()` using public `/api/models` endpoint
- `backend/resilience/cascade_factory.py` — `build_qwen_cascade()`
- `backend/session_manager.py` — Qwen routing (`qwen*` model names)
- `backend/setup_router.py` — Auth status, token save/logout endpoints (`/setup/qwen/token`, `/setup/qwen/status`, `/setup/qwen/logout`)
- `src/lib/api.js` — `getQwenStatus()`, `saveQwenToken()`, `logoutQwen()`
- `src/lib/AccountPanel.svelte` — Qwen login card with browser popup + manual JWT paste
- `src/lib/SettingsPage.svelte` — Qwen provider label (color: `#6366f1`, logo: `/logos/qwen.png`)

### What Works
- Model discovery: `GET https://chat.qwen.ai/api/models` returns 3 models (no auth needed)
- JWT token save/load/expiry check
- Frontend shows Qwen card in Accounts panel
- Qwen models appear in sidebar after token save
- Session creation and routing

### What Doesn't Work
- **`POST https://chat.qwen.ai/api/chat/completions` returns 504 Gateway Timeout from server-side requests**

---

## Research Findings

### 1. Qwen OAuth (Dead)
- OAuth free tier **discontinued April 15, 2026**
- Token endpoint: `https://chat.qwen.ai/api/v1/oauth2/token`
- Client ID: `f0304373b74a44d2b584a3fb70ca9e56`
- Refresh returns `{"error":"invalid_client","error_description":"Invalid client credentials"}`
- Qwen CLI (`~/.qwen/oauth_creds.json`) has expired/invalid tokens
- `qwen auth status` confirms: "Free tier (discontinued 2026-04-15)"

### 2. chat.qwen.ai Web Interface
- Based on **Open WebUI** (forked/modified by Alibaba)
- Auth: Google, GitHub, Apple OAuth
- `enable_anonymous: true` in config (but "pending" role can't chat)
- Account signup works: `POST /api/v1/auths/signup` returns JWT + user ID
- But pending accounts get 504 on chat endpoint

### 3. API Endpoint Testing

| Endpoint | Method | Auth | Result |
|----------|--------|------|--------|
| `/api/config` | GET | None | 200 — full config with features/limits |
| `/api/models` | GET | None | 200 — returns 3 models with full metadata |
| `/api/v1/auths/signup` | POST | None | 200 — creates account, returns JWT |
| `/api/chat/completions` | POST | Bearer JWT | **504 Gateway Timeout** |
| `/api/chat` | POST | Bearer JWT | 500 Internal Server Error |
| `/api/chat/new` | POST | Bearer JWT | 500 |
| `/api/v1/chat/completions` | POST | Bearer JWT | 404 |

### 4. The 504 Problem — Root Cause Analysis

Tested with:
- Valid JWT (30 days remaining, decoded and verified)
- Full browser-like headers (User-Agent, Sec-Fetch-*, sec-ch-ua, Origin, Referer)
- Session cookies from fresh page load
- Cookie + Bearer combined
- Different request body formats (stream true/false, with/without features)

**All return 504 from `alibaba-ga` (Alibaba Gateway/SLB)**

Conclusion: Alibaba's infrastructure blocks non-browser requests. Likely using:
- **TLS fingerprinting** (curl/httpx TLS handshake differs from Chrome)
- **JavaScript challenge cookies** (set by client-side JS, not present in curl)
- **WebSocket upgrade detection** (browser may use different transport)

### 5. Available Models on chat.qwen.ai

| Model | Capabilities | Context |
|-------|-------------|---------|
| `qwen3.6-plus` | thinking, vision, search, MCP, audio, video | 1M tokens |
| `qwen3.6-max-preview` | thinking | text only |
| `qwen3.6-27b` | thinking, vision | open-source |

All have `auto_thinking: true`, `auto_search: true`.

### 6. Qwen vs Arena Comparison

| Aspect | Arena.ai | chat.qwen.ai |
|--------|----------|--------------|
| CAPTCHA | reCAPTCHA (major pain) | **None detected** |
| Anti-bot | Minimal | TLS fingerprint / JS cookies |
| API format | Custom | **OpenAI-compatible** |
| Models | 150+ (many broken) | 3 (all functional) |
| Auth | Google account | Google/GitHub/Apple |
| Rate limit | 5s gap, aggressive | Unknown |
| Tool calling | Serialized XML hack | **Native OpenAI format** |
| Streaming | Custom SSE | **Standard SSE** |

---

## Possible Solutions (Ranked)

### A. Browser Bridge Extension (Arena-style) — Most Proven
**How**: Extend FreeHive's Chrome extension to intercept chat.qwen.ai requests
- Add `https://chat.qwen.ai/*` to extension's `host_permissions`
- Content script on chat.qwen.ai page captures auth cookies
- Route messages through extension → native host → FreeHive backend
- Same architecture as Arena bridge

**Pros**: Proven pattern, already built for Arena, no CAPTCHA unlike Arena
**Cons**: Needs Chrome open, adds latency (~1-2s), user must have chat.qwen.ai tab open

### B. Playwright/Puppeteer Browser Automation — Most Reliable
**How**: Use Playwright (already in project) to automate Chrome with real browser context
- Launch headless Chrome via Playwright
- Login once (save browser state/cookies)
- Make API calls from browser context (bypasses TLS fingerprinting)
- Extract responses back to FreeHive

**Pros**: Bypasses all anti-bot, real browser environment, no extension needed
**Cons**: Heavy (headless Chrome), slower startup, memory usage

**Note**: FreeHive already has Playwright integration for Arena (`arena_playwright_adapter.py`, `arena_steel_adapter.py`). Could reuse same infrastructure.

### C. TLS Fingerprint Spoofing — Fastest if it works
**How**: Use `curl_cffi` or `tls-client` Python library that mimics Chrome's TLS fingerprint
- These libraries use Chrome's boringssl and JA3/JA4 fingerprint
- May bypass Alibaba's TLS-based detection

```python
from curl_cffi import requests
resp = requests.post(
    "https://chat.qwen.ai/api/chat/completions",
    headers={"Authorization": f"Bearer {token}"},
    json={"model": "qwen3.6-plus", "messages": [...]},
    impersonate="chrome"
)
```

**Pros**: Direct API speed (no browser), simple implementation
**Cons**: May not work if Alibaba uses JS challenge cookies too, fragile

### D. Cloudflare Workers / Proxy — Cleanest
**How**: Deploy a Cloudflare Worker that proxies requests to chat.qwen.ai
- Worker runs in Cloudflare's edge, may have different fingerprint
- Or use a residential proxy service

**Pros**: Clean separation, may bypass geo/TLS restrictions
**Cons**: Requires Cloudflare account, adds latency, may still get blocked

### E. Chrome DevTools Protocol (CDP) — Precise
**How**: Use Chrome DevTools Protocol to execute fetch() inside user's browser
- Connect to Chrome's remote debugging port
- Execute `fetch('/api/chat/completions', ...)` in page context
- Response comes back through CDP

**Pros**: Uses real browser context, bypasses all detection
**Cons**: Requires Chrome with `--remote-debugging-port`, complex setup

---

## Tools Available for Next Session

### Chrome DevTools MCP
- **Installed**: Yes, on user scope (`npx chrome-devtools-mcp@latest`)
- **Config location**: `/home/nazmoney/.claude.json` under `projects./home/nazmoney/Ilee_AI.mcpServers.chrome-devtools`
- **Tools**: `list_pages`, `navigate_page`, `take_snapshot`, `evaluate_script`, `click`, `fill`, `network` tools
- **Use case**: Navigate to chat.qwen.ai, login, execute fetch() in browser context, capture real network requests

### Playwright
- **Installed**: Yes, in project venv (`pip install playwright`)
- **Existing code**: `backend/adapters/arena_playwright_adapter.py` — can be repurposed
- **Use case**: Automate browser login and API calls from Python

### Key Commands
```bash
# Switch to feature branch
cd ~/Ilee_AI && git checkout feat/qwen-provider

# Check current state
git log --oneline -5

# Start dev
npm run tauri dev

# Test model discovery (works without auth)
source venv/bin/activate && python -c "
from backend.adapters.qwen_direct_adapter import QwenDirectAdapter
import asyncio
models = asyncio.run(QwenDirectAdapter.fetch_models())
print(f'{len(models)} models found')
"

# Test TLS fingerprint approach
pip install curl_cffi
python -c "
from curl_cffi import requests
r = requests.post('https://chat.qwen.ai/api/chat/completions',
    headers={'Authorization': 'Bearer TOKEN_HERE', 'Content-Type': 'application/json'},
    json={'model':'qwen3.6-plus','messages':[{'role':'user','content':'hi'}],'stream':False},
    impersonate='chrome')
print(r.status_code, r.text[:200])
"
```

---

## Recommended Next Steps

1. **Try `curl_cffi` with Chrome impersonation** — quickest test, 5 minutes
2. **If that fails → Use Chrome DevTools MCP** to execute fetch() in real browser context and capture exact request format
3. **If format is confirmed → Build Playwright adapter** (reuse Arena Playwright code)
4. **If all server-side fails → Extend Arena bridge extension** to support chat.qwen.ai

The adapter code is ready. Only the transport layer (how we talk to chat.qwen.ai) needs solving. Everything else — routing, model discovery, frontend, session management — is complete.

---

## Git State
```
Branch: feat/qwen-provider
Last commit: 64554a5 — fix: wrap Qwen card in {#if status} for valid @const placement
Commits ahead of main: 3
  - feat: add Qwen as direct provider via chat.qwen.ai API
  - feat: complete Qwen provider frontend + streaming integration
  - fix: wrap Qwen card in {#if status} for valid @const placement
```

Main branch has all the arena bridge fixes from earlier in this session (pushed to origin/main).
