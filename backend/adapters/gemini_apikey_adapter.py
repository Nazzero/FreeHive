"""
gemini_apikey_adapter.py — FreeHive Resilience Fallback

Last-resort Gemini adapter using user-provided Google AI API key.
Uses the public generativelanguage.googleapis.com endpoint — no OAuth, no Code Assist.
"""

import json
import logging
import time
import uuid
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

API_KEY_FILE = Path.home() / ".freehive" / "api_keys.json"
PUBLIC_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-2.0-flash"


class GeminiApiKeyAdapter:
    """Gemini via public Google AI API with user API key."""

    def __init__(self, model: str | None = None):
        self.conversation_history: list[dict] = []
        self._model = model or DEFAULT_MODEL

    def _get_api_key(self) -> str:
        if not API_KEY_FILE.exists():
            raise RuntimeError("No Google AI API key configured. Add one in Settings → API Keys.")
        keys = json.loads(API_KEY_FILE.read_text())
        key = keys.get("google", "").strip()
        if not key:
            raise RuntimeError("Google AI API key is empty. Update in Settings → API Keys.")
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
        """Takes Chat Completions format, returns Chat Completions format."""
        api_key = self._get_api_key()

        # Reuse Gemini direct adapter's format converters
        from backend.adapters.gemini_direct_adapter import (
            _convert_messages_to_gemini,
            _convert_tools_to_gemini,
            _convert_tool_choice_to_gemini,
            _result_to_chat_completions,
        )

        system_text, contents = _convert_messages_to_gemini(messages)
        gemini_tools = _convert_tools_to_gemini(tools) if tools else []
        tool_config = _convert_tool_choice_to_gemini(tool_choice, has_tools=bool(gemini_tools))

        body: dict = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": 8192},
        }
        if gemini_tools:
            body["tools"] = gemini_tools
        if tool_config:
            body["tool_config"] = tool_config
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text}]}

        url = f"{PUBLIC_API_BASE}/models/{self._model}:generateContent?key={api_key}"

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                url,
                headers={"Content-Type": "application/json"},
                json=body,
            )

        if resp.status_code != 200:
            raise RuntimeError(f"Gemini API error {resp.status_code}: {resp.text[:200]}")

        data = resp.json()

        # Parse public API response (slightly different from Code Assist SSE)
        text_parts = []
        function_calls = []
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                if "text" in part:
                    text_parts.append(part["text"])
                elif "functionCall" in part:
                    fc = part["functionCall"]
                    function_calls.append({
                        "id": f"call_{uuid.uuid4().hex[:16]}",
                        "name": fc.get("name", ""),
                        "arguments": json.dumps(fc.get("args", {})),
                    })

        result = {"text": "".join(text_parts), "function_calls": function_calls}
        return _result_to_chat_completions(result, self._model)

    async def send_message(self, message: str, history: list[dict] = None) -> str:
        if not self.conversation_history and history:
            self.load_history(history)
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
