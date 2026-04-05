from backend.adapters.claude_direct_adapter import ClaudeDirectAdapter
from backend.adapters.chatgpt_adapter import ChatGPTAdapter


class SessionManager:
    """
    Routes messages to the correct adapter.
    Adapters are lazy-loaded on first use so startup never fails
    if the user hasn't authenticated yet.
    """

    def __init__(self):
        self._adapters: dict = {}
        self._history: dict = {}

    def _get_adapter(self, model: str):
        if model not in self._adapters:
            if model == "claude":
                self._adapters[model] = ClaudeDirectAdapter()
            elif model == "chatgpt":
                self._adapters[model] = ChatGPTAdapter()
            else:
                raise ValueError(
                    f"Model '{model}' is not available. Available: {self._available()}"
                )
        return self._adapters[model]

    def _available(self) -> list[str]:
        return ["claude", "chatgpt"]

    async def send_message(self, model: str, message: str) -> str:
        adapter = self._get_adapter(model)

        if model == "chatgpt":
            history = self._history.get(model, [])
            response = await adapter.send_message(message, conversation_history=history)
            history.append({"role": "user", "content": message})
            history.append({"role": "assistant", "content": response})
            self._history[model] = history
        else:
            response = await adapter.send_message(message)

        return response

    def clear_history(self, model: str):
        self._history.pop(model, None)
        if model in self._adapters:
            self._adapters[model].clear_history()
    
    def clear_all_history(self):
        self._history.clear()
        for adapter in self._adapters.values():
            adapter.clear_history()