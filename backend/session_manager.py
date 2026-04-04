from backend.adapters.claude_adapter import ClaudeAdapter

class SessionManager:

    def __init__(self):
        self.adapters = {
            "claude": ClaudeAdapter()
        }

    async def send_message(self, model: str, message: str) -> str:
        if model not in self.adapters:
            raise ValueError(
                f"Model '{model}' not available. "
                f"Available: {list(self.adapters.keys())}"
            )
        return await self.adapters[model].send_message(message)