# FreeHive — Handoff Document V0.6.2 (Arena Stabilization Status)

**Session date:** 2026-04-09  
**Scope:** Arena Extension Bridge stabilization after v0.6.0 migration  
**Status:** Partially stable, still has model-quality and policy/rate-limit issues

---

## 1. What Was Achieved

### A. Core bridge now works end-to-end
- Multi-turn chat continuity works for valid models.
- Session continuity is preserved (same `session_id` in backend, stable Arena `conversation_id` per session).
- `POST /api/chat/clear` now exists and works.
- Arena model switch in frontend now auto-starts a new chat (no manual `+ New Chat` needed).

### B. Reliability hardening completed
- Recaptcha token minting improved:
  - broader sitekey/action detection,
  - v3 mint retry,
  - v2 fallback attempt path.
- Error diagnostics expanded (bridge version + recaptcha environment + retry flags).
- Transient retry logic added:
  - extension-level retry for `429 prompt failed` and `429 Too Many Requests`,
  - backend-level retry with adaptive delay (up to 3 attempts for transient errors).

### C. Model-failure handling improved
- `404 Model not found` now returns user-facing actionable error.
- Models that fail with `404 Model not found` are marked invalid and filtered from future model refreshes (during current backend runtime).
- `422 not permitted for this type of question` now treated similarly (marked invalid for conversational chat).
- Additional chat-model filtering heuristics added on both extension and backend sides.

---

## 2. Key Files Changed During This Stabilization

- `arena_extension/page_bridge.js`
  - model extraction/filtering
  - recaptcha handling + retries
  - 429 handling + retry-after support
  - diagnostics enrichment
  - conversation-id fallback ordering fix
- `backend/adapters/arena_bridge_adapter.py`
  - transient retry policy
  - invalid model tracking/filtering (`404`, `422`)
  - clearer user error messages
- `backend/services/arena_bridge_client.py`
  - larger diagnostics payload preview for logs
- `backend/router.py`
  - added `/api/chat/clear`
- `backend/session_manager.py`
  - model-aware clear behavior
  - clear-all and clear-by-model helpers
- `src/routes/+page.svelte`
  - auto-clear/new-chat on Arena model switch
  - improved Arena error display

---

## 3. Current Behavior (from latest logs)

### Working patterns
- Valid models (example: `claude-sonnet-4-6`, `gemini-3-flash`, `gpt-5-high`, some `grok-*`) can complete requests.
- Initial `429` often recovers after delay/retry.

### Failing patterns still observed
- `429 Too Many Requests` on first send after model switch/clear.
- `404 Model not found` for many listed models (model appears in list but unusable for direct chat in current Arena context).
- `422 This model is not permitted to handle this type of question` for some models (task/policy mismatch).
- Occasional timeout before eventual fail/recover.
- `400 Cannot select private models in non-battle mode` for private/battle-only models (not valid for Direct mode).
- Very large `retry_after_header` values (`~1161–1242s`) for some models, implying model-specific cooldown windows.

### Concrete evidence from latest test run (2026-04-09)
- **Succeeded**:
  - `claude-haiku-4-5-20251001`
  - `deepseek-v3.2-thinking`
- **Failed (private model in direct mode)**:
  - `clawl` -> `400 Cannot select private models in non-battle mode`
- **Failed (model unavailable)**:
  - `gemini-3.1-pro-preview` -> `404 Model not found`
- **Failed (rate-limited / cooldown)**:
  - `gemma-3-27b-it` -> sequence `429 prompt failed` + `403 recaptcha` + final `429 prompt failed`
  - `glm-5v-turbo` -> repeated `429 Too Many Requests` with `retry_after_header` around `1220`, `1190`, `1161`

---

## 4. Why Issues Continue

1. **Arena model catalog is not a strict "chat-available-now" list**
- Returned/parsed model metadata includes entries that are:
  - not chat-capable,
  - restricted by account/region/policy,
  - temporarily unavailable.

2. **Arena enforcement is dynamic**
- Same model family may be allowed one day and denied later.
- Permission can depend on question type, account state, or upstream load.

3. **Rate limiting is external**
- `429` behavior is Arena-side throttling, not local backend failure.

4. **Reasoning/thinking leakage**
- Some models return internal reasoning/thinking text in stream output and UI currently displays it inline with final answer.

5. **Direct-mode policy mismatch**
- Arena list can include private/battle-only entries that cannot be selected in Direct mode (`400` class).

6. **Long cooldown models**
- Some models return very long Retry-After windows; short retries cannot recover these in-session.

---

## 5. What Is Incomplete

1. **Model list quality**
- Still not guaranteed that "Refresh Models" only shows truly chat-usable models.
- Current filtering is heuristic + runtime invalidation, not authoritative capability validation.

2. **Persistent model quality memory**
- Invalid/working knowledge is in-memory for current runtime only.
- Backend restart loses learned invalid list.

3. **Reasoning-content suppression**
- No production-grade output sanitation yet for "thinking" traces.

4. **Capability-aware routing**
- No explicit per-model capability map (`chat`, `code`, `image`, `reasoning-only`, restricted).

5. **Policy-aware suppression**
- `400 private/non-battle` models are not yet auto-blacklisted as incompatible for direct mode.

6. **Cooldown-aware suppression**
- Models with very large `Retry-After` are retried but remain effectively unusable for current user flow.

---

## 6. Recommended Next Solutions (Priority Order)

### P1 — Passive "Verified Chat Models" system (no mass probing)
Goal: isolate what works **without testing every model aggressively**.

- Add persistent store (e.g., `~/.freehive/arena_model_health.json`) with:
  - `model_name`
  - `status` (`verified`, `invalid_404`, `invalid_422`, `rate_limited`, `unknown`)
  - `last_success_at`, `last_error_at`, `error_count`
- Promote model to `verified` only after real user success.
- Demote on repeated `404`/`422`.
- Frontend default view: show `verified` first, optionally hide `unknown` behind "Show unverified".
- This avoids bulk active probing and reduces suspicious request patterns.

### P2 — Stronger model metadata filtering
- Parse explicit capability flags from `initialModels` when present.
- Require `text/chat`-compatible modalities if available.
- Exclude known non-chat task families by metadata first, not just name heuristics.

### P3 — Reasoning/thinking suppression pipeline
- Add post-processing guard before emitting assistant text:
  - drop known reasoning channels/fields if present in stream frame structure,
  - strip common wrappers (`<think>...</think>`, explicit "Reasoning:" blocks) when confidence is high,
  - keep final answer content.
- Add backend toggle: `arena_strip_reasoning_output=true` for safe rollout.

### P4 — Rate-limit UX
- Surface "Rate limited, retrying..." in UI while retries are in progress.
- Add per-model cooldown hints (e.g., "Try again in ~30s").

### P5 — Policy/Cooldown-aware model suppression
- On `400 Cannot select private models in non-battle mode`:
  - mark model status `invalid_private_direct` and hide from normal list.
- On `429` with `retry_after_header` above threshold (e.g., `>=120s`):
  - mark model `cooldown_until=<timestamp>`,
  - hide by default until cooldown expires (or show in "cooldown" section).
- Persist both states in model health store so backend restarts do not reintroduce known-bad entries.

---

## 7. Specific User-Reported UX Issue to Track

**Issue:** Some reasoning-capable models render internal thinking + final answer together in frontend.  
**Impact:** Noisy responses and privacy/quality concerns.  
**Needed fix:** Implement reasoning suppression pipeline (P3 above).

---

## 8. Operational Notes

- `has_auth_cookie` in diagnostics may show `false` even when session is effectively authenticated (cookie visibility limitations and browser policy behavior); do not use this as sole login truth.
- The extension bridge version in recent logs should read:
  - `2026-04-09.v6-rate-limit-retry`

---

## 9. Short Conclusion

The extension bridge is now functionally stable for many models, with significantly better retry and failure handling.  
Remaining instability is mainly **model selection quality** (availability/capability mismatch) and **reasoning output cleanup**.  
The safest path forward is a **passive verified-model registry** plus **reasoning suppression** rather than active probing of the full model list.
