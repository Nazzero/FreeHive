# FreeHive — Handoff Document V0.6.3 (ChatGPT Direct Adapter)

**Session date:** 2026-04-09  
**Version:** v0.6.3  
**Scope:** Make ChatGPT integration use **direct API first**, with **Codex CLI fallback** only when direct fails.

---

## 1. Objective Completed

Implemented a new ChatGPT adapter path that:

1. Reads Codex OAuth token from `~/.codex/auth.json`.
2. Attempts `POST https://api.openai.com/v1/chat/completions` first.
3. Falls back to `codex exec` only on direct-path failures.
4. Surfaces which path was used (`direct_api` vs `codex_cli_fallback`) back to frontend.

---

## 2. What Was Implemented

### A) New Direct-First Adapter
- Added: `backend/adapters/chatgpt_direct_adapter.py`
- Behavior:
  - Primary transport: OpenAI Chat Completions (`gpt-4o` default).
  - Fallback transport: existing `ChatGPTAdapter` (`codex exec`).
  - Cooldown windows after direct failures (quota/auth/rate/model errors) to avoid repeated slow-fail loops.
  - Backward-compatible params: accepts both `conversation_history` and `history`.
  - Tracks last transport metadata (`provider`, `path`, `model`, `reason`, `timestamp`).

### B) Session Routing Fix
- Updated: `backend/session_manager.py`
- ChatGPT route now calls:
  - `adapter.send_message(message, conversation_history=history)`
- This fixed a real mismatch bug (`history` keyword was previously used against a different signature).

### C) API Response Includes Transport Metadata
- Updated: `backend/router.py` (`POST /api/chat`)
- Response now includes:
```json
{
  "response": "...",
  "transport": {
    "provider": "chatgpt",
    "path": "direct_api | codex_cli_fallback",
    "model": "...",
    "reason": "...",
    "timestamp": 1775751991
  }
}
```

### D) Frontend Displays Used Transport
- Updated: `src/lib/api.js` to return full `/api/chat` payload (not only `response` string).
- Updated: `src/lib/store.js` to store optional `transport` per assistant message.
- Updated: `src/routes/+page.svelte` to render assistant meta as:
  - `chatgpt · direct_api · <time>`
  - or `chatgpt · codex_cli_fallback · <time>`

### E) Direct Model Override for Fast Testing
- Updated: `backend/adapters/chatgpt_direct_adapter.py`
- New env var:
  - `CHATGPT_DIRECT_MODEL` (default `gpt-4o`)
  - Example: `CHATGPT_DIRECT_MODEL=gpt-4o-mini`

---

## 3. Live Validation Results

### Direct API probe with current Codex token
Ran live tests against:
- `gpt-4o-mini`
- `gpt-4o`

Both returned:
- HTTP `429`
- `error.code = insufficient_quota`

Interpretation:
- Token format/auth is accepted by endpoint.
- Account/token currently lacks usable API quota for direct completions.
- Therefore runtime falls back to CLI path as designed.

---

## 4. Files Changed

- `backend/adapters/chatgpt_direct_adapter.py` (new)
- `backend/session_manager.py`
- `backend/router.py`
- `src/lib/api.js`
- `src/lib/store.js`
- `src/routes/+page.svelte`

---

## 5. Known Remaining Constraints

1. Direct path success depends on OpenAI API quota/billing for the account tied to token.
2. CLI fallback remains slower due process startup (`codex exec` cold start).
3. `npm run check` still reports many pre-existing TypeScript strict-mode issues in repo (not introduced by this feature).  
   Build is still passing with `npm run build`.

---

## 6. Quick Re-Test Commands

```bash
# Backend
cd ~/Ilee_AI
source venv/bin/activate
uvicorn backend.main:app --host 127.0.0.1 --port 7200 --reload
```

```bash
# Optional: test smaller direct model
export CHATGPT_DIRECT_MODEL=gpt-4o-mini
```

```bash
# Direct probe (manual)
ACCESS_TOKEN=$(python3 - <<'PY'
import json, pathlib
print(json.loads(pathlib.Path('/home/nazmoney/.codex/auth.json').read_text())['tokens']['access_token'])
PY
)
curl -sS -X POST https://api.openai.com/v1/chat/completions \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","max_tokens":50,"messages":[{"role":"user","content":"hello"}]}'
```

Expected right now on current account: `insufficient_quota` (429), then app uses `codex_cli_fallback`.

---

## 7. Next Recommended Step

Use a Codex/OpenAI account with active API quota and run one message in ChatGPT mode.  
Confirm frontend meta shows `direct_api` on assistant response.

