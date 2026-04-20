"""
claude_apikey_adapter.py — FreeHive Resilience Fallback

Last-resort Claude adapter using a user-provided Anthropic API key.
No OAuth, no billing marker, no identity block, no content scrubbing needed.
Standard Anthropic API — the simplest, most stable path.
"""

import json
import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

API_KEY_FILE = Path.home() / ".freehive" / "api_keys.json"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5"


class ClaudeApiKeyAdapter:
    """Claude adapter using user-provided API key. No OAuth tricks."""

    def __init__(self, model: str | None = None):
        self.conversation_history: list[dict] = []
        self._model = model or DEFAULT_MODEL

    def _get_api_key(self) -> str:
        if not API_KEY_FILE.exists():
            raise RuntimeError(
                "No Anthropic API key configured. Add one in Settings → API Keys."
            )
        try:
            keys = json.loads(API_KEY_FILE.read_text())
            key = keys.get("anthropic", "").strip()
            if not key:
                raise RuntimeError("Anthropic API key is empty. Update in Settings → API Keys.")
            return key
        except json.JSONDecodeError:
            raise RuntimeError("API keys file corrupted. Re-add key in Settings.")

    def load_history(self, history: list[dict]):
        self.conversation_history = [
            {"role": m["role"], "content": m["content"]} for m in history
        ]

    async def raw_request(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 8096,
        system: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
        thinking_effort: str = "off",
    ) -> dict:
        api_key = self._get_api_key()

        body: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                MESSAGES_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            )

        if resp.status_code != 200:
            raise RuntimeError(f"Claude API error {resp.status_code}: {resp.text[:200]}")

        return resp.json()

    async def send_message(self, message: str, history: list[dict] = None) -> str:
        if not self.conversation_history and history:
            self.load_history(history)
        self.conversation_history.append({"role": "user", "content": message})

        result = await self.raw_request(self.conversation_history)

        content = result.get("content", [])
        text = next((b["text"] for b in content if b.get("type") == "text"), "")
        self.conversation_history.append({"role": "assistant", "content": text})
        return text

    def clear_history(self):
        self.conversation_history = []

    def is_authenticated(self) -> bool:
        try:
            self._get_api_key()
            return True
        except RuntimeError:
            return False
