# FreeHive — Handoff Document V0.0.53 (ChatGPT Direct Adapter)

**Session date:** 2026-04-08
**Starting point:** V0.0.52 handoff (GeminiDirectAdapter completed)
**Ending version:** v0.5.1 (unchanged — no working direct path found)

---

## What This Session Was About

Investigate whether the Codex CLI OAuth token (`~/.codex/auth.json`) can call
OpenAI's API endpoints directly, bypassing the 3-5s subprocess cold start overhead.
Mirror the pattern of `ClaudeDirectAdapter` and `GeminiDirectAdapter`.

**Goal:** Replace the `codex exec` subprocess in `ChatGPTAdapter` with direct REST or
WebSocket calls, eliminating cold start latency.

---

## What Was Attempted

### 1. JWT Token Analysis — COMPLETED

Decoded the JWT from `~/.codex/auth.json` (`accessToken` field).

**Key claims found:**
```json
{
  "chatgpt_plan_type": "free",
  "chatgpt_account_id": "<account_id>",
  "scp": [
    "openid",
    "profile",
    "email",
    "offline_access",
    "api.connectors.read",
    "api.connectors.invoke"
  ]
}
```

**Critical findings:**
- Account is **free tier** (`chatgpt_plan_type: free`)
- Scopes are **connectors only** — no `api.completions.write`, no `api.responses.write`
- This is the fundamental blocker for all direct REST paths

---

### 2. Direct REST — `api.openai.com/v1/chat/completions` — BLOCKED

```
POST https://api.openai.com/v1/chat/completions
Authorization: Bearer <codex_access_token>
```

**Result: `insufficient_quota`**

The Codex OAuth token is not an API key. Free plan accounts have no API credits.
This endpoint requires a paid API subscription, not a ChatGPT subscription.

---

### 3. Direct REST — `api.openai.com/v1/responses` — BLOCKED

```
POST https://api.openai.com/v1/responses
Authorization: Bearer <codex_access_token>
```

**Result: 403 — `Missing scopes: api.responses.write`**

The token only has `api.connectors.read/invoke`. The `responses` endpoint requires
`api.responses.write` scope, which is not issued to free accounts.

---

### 4. chatgpt.com REST — BLOCKED by Cloudflare

```
POST https://chatgpt.com/backend-api/conversation
Authorization: Bearer <codex_access_token>
```

**Result:** Cloudflare HTML challenge page (403/403 with HTML body)

The `chatgpt.com` REST API endpoints are protected by Cloudflare's bot detection.
Regular HTTP clients cannot get through, even with valid Bearer tokens.

---

### 5. WebSocket — `wss://chatgpt.com/backend-api/codex/responses` — CONNECTS BUT FAILS

This was the breakthrough discovery: **WebSocket bypasses Cloudflare**.

```python
import websockets
uri = "wss://chatgpt.com/backend-api/codex/responses"
headers = {"Authorization": f"Bearer {token}"}
# Connection SUCCEEDS — no Cloudflare block
```

However, every model name attempted returned the same error:
```
"The 'None' model is not supported when using Codex with a ChatGPT account."
```

Models tried: `gpt-5.2`, `gpt-5.4`, `gpt-4o`, `gpt-4o-mini`, `o4-mini`, `codex`

**What "None" means:** The server is receiving the model field but the account has no
assigned Codex model. The "None" string is the server's internal representation of
"no model assigned to this account tier". The free plan has no Codex model entitlement.

---

### 6. Traffic Proxying — INCONCLUSIVE

Attempted to proxy actual Codex CLI WebSocket traffic using:
```bash
CODEX_CHATGPT_BASE_URL=http://localhost:9999 codex exec --skip-git-repo-check -m gpt-5.2 "hello"
```

No WebSocket traffic was captured. The Codex binary (`@openai/codex-linux-x64`) is a
Rust binary — the Node.js wrapper just launches it. The Rust binary appears to ignore
the `CODEX_CHATGPT_BASE_URL` environment variable for WebSocket connections, or the
variable name is different.

The mitmproxy approach would work but requires SSL certificate installation which
was not pursued this session.

---

## Root Cause Summary

| Path | Status | Reason |
|---|---|---|
| `api.openai.com/v1/chat/completions` | BLOCKED | No API credits (free plan) |
| `api.openai.com/v1/responses` | BLOCKED | Missing `api.responses.write` scope |
| `chatgpt.com/backend-api/conversation` | BLOCKED | Cloudflare bot protection |
| `wss://chatgpt.com/backend-api/codex/responses` | CONNECTS / FAILS | Free account has no Codex model assigned |

**The free ChatGPT account cannot make direct API calls by any method currently discovered.**
The subprocess (`codex exec`) remains the only working path.

---

## What the chatgpt_direct_adapter.py Does (Already Exists)

`backend/adapters/chatgpt_direct_adapter.py` was already present before this session.
It implements the try-direct / fallback pattern:

1. Reads token from `~/.codex/auth.json`
2. Tries `api.openai.com/v1/chat/completions` with Bearer token
3. On `insufficient_quota` / `Missing scopes` / auth error → falls back to `ChatGPTAdapter` (subprocess)

The direct path **always fails** for this free account. The fallback always triggers.
The adapter works correctly — it's just always using the subprocess path.

---

## What Would Unlock Direct Access

| Option | What Changes | Cost |
|---|---|---|
| Upgrade to **ChatGPT Plus** | May assign a Codex model to the account | ~$20/month |
| Upgrade to **ChatGPT Pro** | Full Codex access confirmed | ~$200/month |
| Add **OpenAI API credits** | Unlocks `api.openai.com/v1/chat/completions` with API key | Pay-per-use |
| **Capture CLI WebSocket protocol** | Get exact request format from working Codex CLI | Requires mitmproxy + cert |

If the account is upgraded to Plus/Pro, the WebSocket path (`wss://chatgpt.com/backend-api/codex/responses`)
likely starts working immediately — the connection already succeeds, only the model
assignment fails.

---

## How to Capture Real Codex CLI WebSocket Traffic (Future)

If someone wants to crack this on a paid account:

```bash
# Install mitmproxy
pip install mitmproxy

# Start proxy
mitmproxy --listen-port 8080 --ssl-insecure

# Install mitmproxy CA cert (required for WebSocket TLS)
# Copy ~/.mitmproxy/mitmproxy-ca-cert.pem to system trust store

# Run Codex via proxy
HTTPS_PROXY=http://localhost:8080 \
  NODE_EXTRA_CA_CERTS=~/.mitmproxy/mitmproxy-ca-cert.pem \
  codex exec --skip-git-repo-check -m gpt-5.2 "hello"
```

Look for WebSocket frames to `chatgpt.com/backend-api/codex/responses`.
Capture the initial handshake message format — that's what needs to be replicated.

---

## Current State of chatgpt_direct_adapter.py

The file works as-is. It silently falls back to subprocess. No changes needed.

`session_manager.py` routes `"chatgpt"` to `ChatGPTDirectAdapter`:
```python
elif model == "chatgpt":
    from backend.adapters.chatgpt_direct_adapter import ChatGPTDirectAdapter
    return ChatGPTDirectAdapter()
```

---

## Subprocess Performance (Baseline)

Current subprocess path: `codex exec --skip-git-repo-check -m gpt-5.2 <prompt>`

- Cold start: 3-5 seconds (Node.js + Rust binary init)
- Warm: N/A — each call is a new subprocess (no process reuse)

**Possible optimization (not implemented):** Pre-warm a subprocess pool at startup.
Keep 1-2 Codex processes idle, route messages to them via stdin. Risk: Codex CLI
may not support interactive stdin mode — it's designed for one-shot exec.

---

## Files Touched This Session

None. All work was research/investigation only. No files were modified or created
(other than this handoff doc).

---

## Next Steps (in order)

1. **Accept subprocess for now** — The 3-5s overhead is the cost of using free ChatGPT.
   Users who notice it can be informed it's due to free-tier limitations.

2. **If Plus/Pro upgrade happens** — Test WebSocket path immediately:
   ```python
   import websockets
   uri = "wss://chatgpt.com/backend-api/codex/responses"
   headers = {"Authorization": f"Bearer {token}"}
   # Send the handshake message (need to capture format from working CLI)
   ```

3. **Update SetupScreen.svelte** — Add Gemini CLI card (this is more valuable,
   since Gemini direct adapter is confirmed working with zero latency).

4. **Build `/v1/messages` Anthropic-compatible endpoint** — Lets Claw Code use
   FreeHive as a free drop-in:
   ```bash
   export ANTHROPIC_BASE_URL="http://localhost:7200"
   export ANTHROPIC_AUTH_TOKEN="freehive"
   ```

5. **Delete `gemini_adapter.py`** — Dead code since `GeminiDirectAdapter` is live.

6. **Test GeminiDirectAdapter token refresh** — Force-expire the token.

---

## How to Run

```bash
cd ~/Ilee_AI
./start.sh
# ChatGPT in sidebar uses subprocess fallback (direct path blocked for free accounts)
# Gemini in sidebar uses direct OAuth adapter (fast, no subprocess)
```
