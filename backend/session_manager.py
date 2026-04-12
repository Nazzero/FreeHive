"""
session_manager.py — FreeHive v0.5.1
Routes model names to the correct adapter.
"""

import logging
from typing import Optional

from backend.feature_flags import is_arena_enabled

logger = logging.getLogger(__name__)


class SessionManager:
    """
    Manages per-session adapter instances and routes messages to the right backend.

    Model routing:
      "claude"           → ClaudeDirectAdapter
      "chatgpt"          → ChatGPTDirectAdapter (with codex subprocess fallback)
      "gemini"           → GeminiDirectAdapter
      "arena/<model>"    → ArenaPlaywrightAdapter  (shared instance via ArenaManager, when enabled)
    """

    def __init__(self, arena_manager=None, *, arena_enabled: bool | None = None):
        # arena_manager is injected by main.py after it creates ArenaManager
        self._arena_manager = arena_manager
        self._arena_enabled = is_arena_enabled() if arena_enabled is None else bool(arena_enabled)
        self._sessions: dict[str, dict] = {}

    def set_arena_manager(self, arena_manager):
        """Inject ArenaManager after initialization (avoids circular imports)."""
        self._arena_manager = arena_manager

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create_session(self, session_id: str, model: str) -> dict:
        """Create a new session and instantiate the appropriate adapter."""
        adapter = self._make_adapter(model)
        self._sessions[session_id] = {
            "id": session_id,
            "model": model,
            "adapter": adapter,
        }
        logger.info(f"[SessionManager] Created session {session_id} for model '{model}'")
        return self._sessions[session_id]

    def get_session(self, session_id: str) -> Optional[dict]:
        return self._sessions.get(session_id)

    def delete_session(self, session_id: str):
        if session_id in self._sessions:
            del self._sessions[session_id]
            logger.info(f"[SessionManager] Deleted session {session_id}")

    # ------------------------------------------------------------------
    # Message routing
    # ------------------------------------------------------------------

    async def send_message(
        self,
        session_id: str,
        message: str,
        conversation_manager=None,
    ) -> str:
        """
        Route a message to the correct adapter and return the response string.
        Persists both user message and response to DB via conversation_manager.
        """
        session = self.get_session(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        model = session["model"]
        adapter = session["adapter"]

        # Snapshot history before storing the current user turn so adapters that
        # build prompts from DB history do not duplicate the latest message.
        history_before_user_turn = (
            conversation_manager.get_messages(session_id) if conversation_manager else []
        )

        # Persist user message
        if conversation_manager:
            conversation_manager.add_message(session_id, "user", message)
            db_session = conversation_manager.get_session(session_id)
            if db_session and not db_session.get("title"):
                words = message.strip().split()
                title = " ".join(words[:8]) if words else "New chat"
                if len(words) > 8:
                    title += "..."
                conversation_manager.update_session_title(session_id, title)

        # Dispatch to adapter
        m = model.lower()
        if m.startswith("arena/"):
            if not self._arena_enabled:
                raise RuntimeError("Arena is disabled in this build.")
            arena_model = model[len("arena/"):]
            response = await adapter.send_message(message, arena_model, session_id=session_id)
        elif m == "claude" or m.startswith("claude-") or m == "claude_code":
            response = await adapter.send_message(message)
        elif m == "chatgpt" or any(m.startswith(p) for p in ("gpt-", "o1-", "o3-", "o4-", "codex-")) or m in ("gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.2-codex"):
            # ChatGPT adapter needs full history injected as context
            response = await adapter.send_message(
                message,
                conversation_history=history_before_user_turn,
            )
        elif m == "gemini" or m.startswith("gemini-"):
            response = await adapter.send_message(message)
        else:
            raise ValueError(f"Unknown model: {model}")

        # Persist assistant response
        if conversation_manager:
            conversation_manager.add_message(session_id, "assistant", response)

        return response

    async def load_history(self, session_id: str, messages: list[dict]):
        """
        Called on session restore — rebuild adapter's in-memory history from DB.
        """
        session = self.get_session(session_id)
        if not session:
            return
        adapter = session["adapter"]
        if hasattr(adapter, "load_history"):
            import inspect
            res = adapter.load_history(messages)
            if inspect.isawaitable(res):
                await res

    def clear_history(self, session_id: str):
        session = self.get_session(session_id)
        if session and hasattr(session["adapter"], "clear_history"):
            adapter = session["adapter"]
            model = str(session.get("model", ""))
            if model.startswith("arena/"):
                adapter.clear_history(session_id=session_id)
            else:
                adapter.clear_history()

    def clear_model_sessions(self, model: str) -> int:
        """Clear and remove all in-memory sessions for a specific model."""
        target = str(model or "").strip()
        if not target:
            return 0
        to_clear = [
            sid
            for sid, data in self._sessions.items()
            if str(data.get("model", "")).strip() == target
        ]
        for sid in to_clear:
            self.clear_history(sid)
            self.delete_session(sid)
        return len(to_clear)

    def clear_all_sessions(self) -> int:
        """Clear and remove all in-memory sessions."""
        all_ids = list(self._sessions.keys())
        for sid in all_ids:
            self.clear_history(sid)
            self.delete_session(sid)
        return len(all_ids)

    # ------------------------------------------------------------------
    # Internal: adapter factory
    # ------------------------------------------------------------------

    def _make_adapter(self, model: str):
        """Instantiate the right adapter for the given model string.

        Accepts both short names ('claude', 'chatgpt', 'gemini') and full
        model strings ('claude-haiku-4-5', 'gpt-5.2', 'gemini-3-flash-preview').
        Full model strings are passed through to the adapter as a model override.
        """
        m = model.lower()

        if m == "claude" or m.startswith("claude-"):
            from backend.adapters.claude_direct_adapter import ClaudeDirectAdapter
            return ClaudeDirectAdapter(model=None if m == "claude" else model)

        elif m == "chatgpt" or any(m.startswith(p) for p in ("gpt-", "o1-", "o3-", "o4-", "codex-")) or m in ("gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.2-codex"):
            from backend.adapters.chatgpt_direct_adapter import ChatGPTDirectAdapter
            return ChatGPTDirectAdapter(model=None if m == "chatgpt" else model)

        elif m == "gemini" or m.startswith("gemini-"):
            from backend.adapters.gemini_direct_adapter import GeminiDirectAdapter
            return GeminiDirectAdapter(model=None if m == "gemini" else model)

        elif model.startswith("arena/"):
            if not self._arena_enabled:
                raise RuntimeError("Arena is disabled in this build.")
            # All arena/* sessions share the single Playwright Chromium context.
            # The adapter is retrieved from ArenaManager, not instantiated fresh.
            if self._arena_manager is None:
                raise RuntimeError(
                    "ArenaManager not set on SessionManager. "
                    "Call set_arena_manager() before creating arena sessions."
                )
            adapter = self._arena_manager.get_adapter()
            if adapter is None:
                raise RuntimeError(
                    "Arena Bridge is not active. "
                    "Ensure Chrome is open with the FreeHive extension and an Arena tab active."
                )
            return adapter

        else:
            raise ValueError(f"Unknown model: '{model}'")
