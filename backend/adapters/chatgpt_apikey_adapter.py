"""
chatgpt_apikey_adapter.py — FreeHive Resilience Fallback

Last-resort ChatGPT adapter using user-provided OpenAI API key.
Standard OpenAI Chat Completions API — no WebSocket, no Codex tricks.
"""

import json
import logging
import time
import uuid
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

API_KEY_FILE = Path.home() / ".freehive" / "api_keys.json"
COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o"


class ChatGPTApiKeyAdapter:
    """ChatGPT via standard OpenAI API with user API key."""

    def __init__(self, model: str | None = None):
        self.conversation_history: list[dict] = []
        self._model = model or DEFAULT_MODEL

    def _get_api_key(self) -> str:
        if not API_KEY_FILE.exists():
            raise RuntimeError("No OpenAI API key configured. Add one in Settings → API Keys.")
        keys = json.loads(API_KEY_FILE.read_text())
        key = keys.get("openai", "").strip()
        if not key:
            raise RuntimeError("OpenAI API key is empty. Update in Settings → API Keys.")
        return key

    def load_history(self, messages: list[dict]):
        self.conversation_history = [
            {"role": m["role"], "content": m["content"]} for m in messages
        ]

    async def raw_request(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice=None,
        thinking_effort: str = "off",
    ) -> dict:
        api_key = self._get_api_key()

        body: dict = {
            "model": self._model,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                COMPLETIONS_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )

        if resp.status_code != 200:
            raise RuntimeError(f"OpenAI API error {resp.status_code}: {resp.text[:200]}")

        return resp.json()

    async def send_message(self, message: str, conversation_history: list[dict] | None = None) -> str:
        if not self.conversation_history and conversation_history:
            self.load_history(conversation_history)
        self.conversation_history.append({"role": "user", "content": message})

        result = await self.raw_request(self.conversation_history)

        text = result["choices"][0]["message"].get("content", "")
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
