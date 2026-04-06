from backend.adapters.claude_direct_adapter import ClaudeDirectAdapter
from backend.adapters.chatgpt_adapter import ChatGPTAdapter
from backend.adapters.gemini_adapter import GeminiAdapter
from backend import conversation_manager as cm


class SessionManager:
    """
    Routes messages to the correct adapter.
    All history is persisted in conversations.db and injected per-call.
    Adapters are lazy-loaded on first use.
    """

    def __init__(self):
        self._adapters: dict = {}
        cm.init_db()

    def _get_adapter(self, model: str):
        if model not in self._adapters:
            if model == "claude":
                self._adapters[model] = ClaudeDirectAdapter()
            elif model == "chatgpt":
                self._adapters[model] = ChatGPTAdapter()
            elif model == "gemini":
                self._adapters[model] = GeminiAdapter()
            else:
                raise ValueError(
                    f"Model '{model}' is not available. Available: {self._available()}"
                )
        return self._adapters[model]

    def _available(self) -> list[str]:
        return ["claude", "chatgpt", "gemini"]

    async def send_message(self, model: str, message: str, session_id: str) -> str:
        # Validate session
        session = cm.get_session(session_id)
        if not session:
            raise ValueError(f"Session '{session_id}' not found. Create one via POST /sessions.")
        if session["model"] != model:
            raise ValueError(
                f"Session '{session_id}' is for model '{session['model']}', not '{model}'."
            )

        adapter = self._get_adapter(model)

        # Load full history from DB
        messages = cm.get_messages(session_id)
        history = [{"role": m["role"], "content": m["content"]} for m in messages]

        # All three adapters now accept history for context rebuild on restart
        if model == "chatgpt":
            response = await adapter.send_message(message, conversation_history=history)
        else:
            # Claude and Gemini: pass history so they can rebuild if their
            # internal history is empty (app restart or first message)
            response = await adapter.send_message(message, history=history)

        # Persist both turns to DB
        cm.add_message(session_id, "user", message)
        cm.add_message(session_id, "assistant", response)

        # Auto-title from first message
        if not session.get("title"):
            words = message.strip().split()
            title = " ".join(words[:6])
            if len(words) > 6:
                title += "..."
            cm.update_session_title(session_id, title)

        return response

    def clear_history(self, model: str):
        if model in self._adapters:
            self._adapters[model].clear_history()

    def clear_all_history(self):
        for adapter in self._adapters.values():
            adapter.clear_history()