# FreeHive — Handoff Document V0.0.53 (Arena CDP -> Injected Direct Fetch)

**Session date:** 2026-04-08  
**Objective:** Make Arena reliable in CDP mode first, then migrate message send path from UI automation to injected direct Arena fetch calls.

---

## 1. What Was Completed This Session

### A. CDP Arena lifecycle is integrated and usable

Implemented and wired:
- `backend/adapters/arena_playwright_adapter.py` (CDP-backed adapter, v0.5.7)
- `backend/arena_manager.py` (CDP-aware start/stop/status flow)
- `backend/router.py` (better error handling around session creation and Arena endpoints)
- `src/lib/api.js` (`startArena(forceLogin)`)
- `src/routes/+page.svelte` (Arena UI copy + flow updated for CDP reality)

Working now:
- `POST /api/arena/start` connects to `http://localhost:9222`
- `GET /api/arena/status` returns CDP state + `logged_in`
- `GET /api/arena/models` returns a real model list from Arena page HTML
- Arena session creation via `POST /api/sessions` for `arena/...` models

### B. Arena chat path improved with error hardening

Added:
- Stream capture via in-page `fetch` hook (SSE line parsing + fallback)
- DOM-based fallback response extraction for battle-style output blocks
- Retry-once behavior when Arena stream returns `{"error":"prompt failed"}`
- Dialog dismissal + forced click fallback to avoid some UI interception crashes

### C. Confirmed smoke tests (live)

Verified in this session:
- Arena status/start/models endpoints return success
- End-to-end chat success observed multiple times for:
  - `arena/gpt-5.2-chat-latest` -> response `"OK"`

---

## 2. What Was Attempted But Did NOT Work Reliably

These attempts should **not** be repeated in the same form.

### Attempt 1: Pure DOM text block diff extraction

Approach:
- Compare pre/post page text blocks and infer assistant response.

Why it failed:
- Arena battle mode layout is dynamic (`Assistant A/B` + side content).
- Set-based diffs missed or reordered output.
- Very brittle under UI changes.

### Attempt 2: Generic model picker click strategy

Approach:
- Click common selectors (`combobox`, `dialog`, listbox, etc.) and match text.

Why it failed:
- Hidden elements matched first.
- Mode and model controls overlap visually/structurally.
- Some requested models (`gemini-3.1-pro`) were not reliably selectable through current dialog/query path.

### Attempt 3: Wait-for-DOM fallback after `prompt failed`

Approach:
- If stream capture empty, wait for DOM response fallback.

Why it failed:
- Arena can return `{"error":"prompt failed"}` quickly, but DOM never updates.
- Caused long unnecessary waits/timeouts.

### Attempt 4: New Chat retry without overlay defense

Approach:
- Reset with New Chat and retry once.

Why it failed:
- reCAPTCHA/dialog overlays can intercept pointer events.
- Produced backend `500` (click timeout) before guard code was added.

---

## 3. Current State: What Is Flawless vs What Is Not

### Working reliably (in tested path)

- CDP connection lifecycle (`/api/arena/start`, `/api/arena/status`, `/api/arena/models`)
- Arena model list retrieval for UI display
- Arena end-to-end message path for `arena/gpt-5.2-chat-latest` in repeated tests
- Prompt-failed retry path no longer guaranteed to crash backend

### Current issues still facing

1. **Per-model switching is not reliable across all models**
- `arena/gemini-3.1-pro` frequently fails with stream preview `{"error":"prompt failed"}`
- Adapter often logs: `Could not confirm model switch to '...'`
- Additional observed bug: chat requests attempt model switching, but the send path does not reliably verify the model actually used by Arena before sending.

2. **Model-used verification is missing/incomplete**
- Current flow attempts to click/select a model but does not validate effective model selection from Arena state before prompt submission.
- This causes prompts to run on the previously active model in some sessions.

3. **Conversation continuity is broken in some runs**
- Additional observed bug: follow-up user messages can trigger an unintended New Chat/reset behavior, so messages do not stay in the same Arena conversation thread.
- This must be fixed so a second/third message continues the same conversation unless user explicitly starts a new chat.

4. **UI automation remains brittle by definition**
- Overlay interference can still happen depending on Arena page state.
- Any Arena UI change can break selectors.

5. **Fallback logic remains heuristic**
- DOM extraction is still a backup, not a deterministic protocol parser.

---

## 4. Why Injected Direct Arena Request Is The Correct Next Step

Move message sending to:
- `page.evaluate(() => fetch('/nextjs-api/stream/...', payload))`
- Parse SSE lines directly (`a0:`, etc.)

Benefits over UI-click sending:
- Deterministic model and payload fields (no dropdown clicking)
- No textbox/button selector fragility
- Less sensitive to overlays and layout shifts
- Cleaner protocol-level retries and error handling

Keep UI automation only for:
- User login in real Chrome session
- Basic page readiness checks

### 4.1 Suggested improvement beyond current Playwright injection

If reliability is the priority, move from backend-driven `page.evaluate(...)` calls to a
**browser-native transport bridge**:

- Browser side (userscript or extension) runs continuously in `arena.ai` tab.
- Backend sends jobs to browser bridge via localhost WebSocket/HTTP.
- Browser bridge performs in-page `fetch('/nextjs-api/stream/...')` and streams SSE back.

Why this is better than backend Playwright injection:
- Avoids many CDP race/state issues.
- Better persistence across tab navigation/reloads.
- Cleaner model-used verification from in-page state before send.
- Better handling when UI overlays appear.

### 4.2 Is there an even better way?

Yes, for an Arena-first product:

1. **Best Arena-native architecture**  
   Use a **Chrome extension + native messaging host** (instead of ad-hoc userscript).  
   This is stronger than Playwright injection and temporary userscript bridges because it is
   persistent, versioned, and easier to make deterministic across sessions and browser restarts.

2. **Fallback Arena transport**  
   Keep backend Playwright injection as a fallback transport only when extension/native bridge
   is unavailable, so Arena remains functional without changing product direction.

> **Execution directive update (2026-04-08):**
> Fallback implementation is intentionally deferred.
> Ship extension + native host path to "flawless" first, then add Playwright injection fallback.

---

## 5. Recommended Implementation (Do This Next)

### Phase 1: Chrome extension transport (primary path)

Build a persistent extension that runs in Arena tab context and performs direct protocol calls:
- Intercept backend "send job" requests from localhost bridge
- Execute in-page fetch calls:
  - New conversation: `/nextjs-api/stream/create-evaluation`
  - Follow-up: `/nextjs-api/stream/post-to-evaluation/{id}`
- Parse SSE frames (`a0:` etc.) in extension context and stream normalized events back to host
- Capture and return:
  - effective model id/name at send time
  - conversation/evaluation id
  - final assembled assistant text
  - fatal error payload preview

### Phase 2: Native messaging host

Build host process for deterministic transport between backend and extension:
- Request/response + stream event schema (JSON lines)
- Job lifecycle state machine (`queued -> running -> complete|failed`)
- Heartbeat and reconnect behavior for browser/host restart
- Explicit typed errors (no opaque `500`)

### Phase 3: Backend bridge + API integration

Backend should call native host bridge as the only active transport:
- Add bridge client in backend service layer
- Route `/api/chat` Arena requests through extension/native path only
- Maintain per-session conversation mapping server-side
- Return explicit `503` for bridge unavailable, login missing, prompt failed, model mismatch

### Phase 4: Observability + hard validation gates

Add structured logs and enforce release criteria:
- requested model vs effective model used
- endpoint selected (create vs post)
- stream start/end timestamps + duration
- first fatal payload preview + retry count
- no backend `500` for known Arena failure classes

---

## 6. Two-Agent Split (Extension-First, No Conflict Plan)

Use two AI agents with **disjoint write ownership**.

### Agent 1 (Harder Task - Core Transport Owner, Me)

**Owns files/directories:**
- `arena_extension/*` (manifest, service worker, content script)
- `native_host/*` (native messaging host process + install script)
- `shared/arena_bridge_protocol.py` (or equivalent shared schema module)
- `backend/services/arena_bridge_client.py`

**Responsibilities:**
- Define and implement extension <-> native host protocol
- Implement in-extension direct Arena fetch + strict SSE parsing
- Implement model-used verification before/at send
- Implement conversation/evaluation ID continuity across turns
- Implement host reconnect/heartbeat and deterministic failure signaling

**Must not edit:**
- `backend/router.py`
- `backend/arena_manager.py`
- frontend UI files

### Agent 2 (Difficult Task - Integration, UX Surface, and Validation Owner)

**Owns files/directories:**
- `backend/router.py`
- `backend/arena_manager.py`
- `src/lib/api.js`
- `src/routes/+page.svelte`
- `scripts/smoke_arena_extension.sh` (or equivalent smoke runner)
- `Handoff_Docs/*` (execution notes + validation logs)

**Responsibilities:**
- Integrate backend endpoints to call bridge client and surface typed errors
- Add UI state/actions for:
  - extension not connected
  - native host offline
  - login required in Arena tab
  - retry in progress / retry exhausted
- Build smoke coverage for start/status/models/chat multi-turn
- Validate "requested model == effective model" in at least 3 models
- Verify no backend `500` leaks for known Arena failures

**Must not edit:**
- extension protocol internals
- native host transport internals
- shared protocol schema owned by Agent 1

### Integration order

1. Agent 1 delivers protocol schema + extension/native host MVP with local self-test.
2. Agent 2 integrates backend/UI against schema and publishes smoke results.
3. Joint fix pass for any contract mismatch.
4. Gate release on validation checklist in Section 8.

### Fallback policy (explicit)

- Do **not** implement Playwright fallback in this cycle.
- If extension path fails a scenario, return explicit `503` and log precise reason.
- Start fallback implementation only after Section 8 is fully green on extension path.

---

## 7. “Do Not Repeat” Checklist

Do NOT:
- Rely on pure DOM text diff as primary response extraction.
- Rely on dropdown clicking as primary model routing mechanism.
- Wait full DOM timeout after explicit `prompt failed` stream error.
- Assume one selector strategy will survive Arena UI updates.
- Assume model selection succeeded without verifying actual model used.
- Reset to New Chat for normal follow-up messages in the same session.

Do:
- Prefer injected protocol calls with explicit payload fields.
- Treat UI as login/session shell only.
- Parse stream protocol directly and fail fast on known fatal errors.
- Persist and reuse conversation/session identifiers so multi-turn chat stays in-thread.

---

## 8. Validation Checklist For Next Session

Before marking Arena “flawless”:

1. `POST /api/arena/start` works when Chrome CDP is up.
2. `GET /api/arena/models` returns current model list.
3. Chat success for at least 3 distinct models (not only one).
4. Confirm the model requested is the model actually used at send time.
5. Confirm follow-up messages stay in same conversation (no implicit new-chat reset).
6. No backend `500` during prompt-failed, captcha, or overlay scenarios.
7. Error responses are explicit `503` with actionable detail.
8. UI shows accurate status/action hints (CDP missing, login needed, retrying, failed).

---

## 9. Quick Repro Commands (used in this session)

```bash
# start backend
source venv/bin/activate
uvicorn backend.main:app --host 127.0.0.1 --port 7200

# status/start/models
curl -sS http://127.0.0.1:7200/api/arena/status
curl -sS -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:7200/api/arena/start
curl -sS http://127.0.0.1:7200/api/arena/models

# create session and send message
curl -sS -X POST -H 'Content-Type: application/json' \
  -d '{"model":"arena/gpt-5.2-chat-latest"}' \
  http://127.0.0.1:7200/api/sessions

curl -sS -X POST -H 'Content-Type: application/json' \
  -d '{"model":"arena/gpt-5.2-chat-latest","message":"Reply with exactly: OK","session_id":"<SESSION_ID>"}' \
  http://127.0.0.1:7200/api/chat
```

---

## 10. Summary

CDP Arena integration is now materially better and usable for at least one validated model path.  
The remaining reliability gap is model switching + protocol determinism.  
Next milestone should be full migration to injected direct Arena stream requests, with UI automation retained only for login/session presence.
