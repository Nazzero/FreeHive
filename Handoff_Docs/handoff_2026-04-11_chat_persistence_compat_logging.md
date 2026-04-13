# 📄 HANDOFF DOC: Chat Persistence + Compat API Conversation Logging

**Date:** 2026-04-11 | **Agent/Dev:** Codex (GPT-5)
**Current Scope/Goal:** Persist all chat conversations in local SQLite for both UI and compat API traffic, and allow loading past chats in UI with visible model metadata.

## ✅ COMPLETED

- [x]  DB schema/migration extension for session/message metadata → `/home/nazmoney/Ilee_AI/backend/conversation_manager.py` | Status: Stable/Tested
- [x]  Session listing filters + robust message fetch guard → `/home/nazmoney/Ilee_AI/backend/router.py` | Status: Stable/Tested
- [x]  In-memory adapter rehydration for old DB sessions before `/api/chat` send → `/home/nazmoney/Ilee_AI/backend/router.py` | Status: Stable/Tested
- [x]  Auto-title first user turn for UI sessions → `/home/nazmoney/Ilee_AI/backend/session_manager.py` | Status: Stable/Tested
- [x]  Compat API persistence for both `/v1/messages` and `/v1/chat/completions` (including tool-use/tool-call preview storage) → `/home/nazmoney/Ilee_AI/backend/compat_router.py` | Status: Stable/Tested
- [x]  Frontend API layer updated for active-session semantics, session list/load support → `/home/nazmoney/Ilee_AI/src/lib/api.js` | Status: Stable/Tested
- [x]  UI saved-chats panel added, click-to-load session, model shown in list, startup auto-restore of most recent chat → `/home/nazmoney/Ilee_AI/src/routes/+page.svelte` | Status: Stable/Tested
- [x]  Build/compile verification passed → `python3 -m compileall backend scripts` and `npm run build` | Status: Passed

## ⚠️ INCOMPLETE / FAILED

- [ ]  Deterministic compat thread grouping when client sends no stable conversation header → `/home/nazmoney/Ilee_AI/backend/compat_router.py` | Issue: Fallback grouping uses hash heuristic (`provider|model|api_key|first_user_message`), which can split/merge threads if request context changes.
- [ ]  UI source segmentation (`ui` vs `compat`) and filtering controls → `/home/nazmoney/Ilee_AI/src/routes/+page.svelte` + `/home/nazmoney/Ilee_AI/src/lib/api.js` | Issue: Saved list currently mixes all sources; no source tabs/filter exposed.

## 🎯 NEXT STEPS & FUTURE PIPELINE

**Immediate Priorities (To Unblock/Finish Current Scope):**

1. Add source filter in UI (`All / UI / API`) and wire `source` query to `/api/sessions` → `/home/nazmoney/Ilee_AI/src/routes/+page.svelte` + `/home/nazmoney/Ilee_AI/src/lib/api.js` | Expected: User can isolate API conversations from UI chats.
2. Add stable session-id guidance and optional enforcement for compat clients (`x-freehive-session-id`) → `/home/nazmoney/Ilee_AI/backend/compat_router.py` + docs/hints in settings UI | Expected: API conversations consistently grouped into correct DB threads.

**Subsequent Tasks (To Execute Once Unblocked):**

- [ ]  Rich replay rendering from `meta_json` → Render tool calls/results structured in UI instead of preview text-only fallback.
- [ ]  Session management UX → Add delete/archive action per saved chat and pagination for large history lists.

- 🧪 Verification Command: `cd /home/nazmoney/Ilee_AI && python3 -m compileall backend scripts && npm run build`

## 🔍 HOW IT WORKS (Critical Context)

- Flow: Incoming message (UI `/api/chat` or compat `/v1/*`) → adapter response → normalized message rows persisted in `~/.freehive/conversations.db` → UI loads sessions/messages via `/api/sessions` + `/api/sessions/{id}/messages`.
- Key Configs/Env: Optional compat conversation headers (`x-freehive-session-id`, `x-conversation-id`, `openai-conversation-id`, `anthropic-conversation-id`, `x-session-id`); no new env vars required for this scope.
- State/Storage: SQLite DB at `/home/nazmoney/.freehive/conversations.db`; tables `sessions` and `messages` now include metadata columns (`source`, `provider`, `external_key`, `metadata_json`, `content_type`, `meta_json`).

## 🚫 DO NOT TOUCH

- `/home/nazmoney/Ilee_AI/backend/router.py` rehydration block in `POST /chat` (`if sm.get_session(body.session_id) is None: ...`) → Reason: Required for continuing loaded historical sessions after restart; removing breaks “open past chat then continue”.
- `/home/nazmoney/Ilee_AI/backend/compat_router.py` `_persist_compat_conversation(...)` call sites in both compat endpoints → Reason: This is the only persistence hook for API traffic; removing breaks API conversation history.
- `/home/nazmoney/Ilee_AI/backend/conversation_manager.py` migration order in `init_db()` (add columns first, then indexes) → Reason: Reordering reintroduces migration crash on existing DBs.

## 🔄 ATTEMPTED & FAILED (Avoid Repeating)

- `Create new indexes on migrated columns before ALTER migration` → Failed: `sqlite3.OperationalError: no such column: source` (full trace during `cm.init_db()` call) → Use `ALTER TABLE` migration first, then create `idx_sessions_source_updated`/`idx_sessions_external`.

## 💡 SUGGESTED FIXES / METHODS

- For `API thread grouping drift when no client session id`: Try requiring/strongly encouraging `x-freehive-session-id` in client configs and keep heuristic only as fallback → Ref: Existing `_compat_external_key(...)` pattern in `/home/nazmoney/Ilee_AI/backend/compat_router.py`.

## 📁 KEY FILES & REFERENCES

- `/home/nazmoney/Ilee_AI/backend/conversation_manager.py` → DB schema, migrations, session/message CRUD, compat-session upsert.
- `/home/nazmoney/Ilee_AI/backend/router.py` → UI session endpoints, chat send rehydration logic.
- `/home/nazmoney/Ilee_AI/backend/session_manager.py` → Internal message persistence routing and auto-title logic.
- `/home/nazmoney/Ilee_AI/backend/compat_router.py` → Compat API routing + persistence bridge for external API traffic.
- `/home/nazmoney/Ilee_AI/src/lib/api.js` → Frontend session APIs and active-session lifecycle.
- `/home/nazmoney/Ilee_AI/src/routes/+page.svelte` → Saved Chats UI, load/open flow, model visibility per chat.
