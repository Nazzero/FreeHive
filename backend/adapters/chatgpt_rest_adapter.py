"""
chatgpt_rest_adapter.py — FreeHive Resilience Fallback

HTTP REST fallback for ChatGPT when WebSocket endpoint is blocked/changed.
Uses same OAuth token from ~/.codex/auth.json but via standard HTTP POST.
"""

import json
import logging
import time
import uuid
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

AUTH_FILE = Path.home() / ".codex" / "auth.json"
# Try REST Responses API (same auth, no WebSocket)
REST_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_MODEL = "gpt-5.2"


class ChatGPTRestAdapter:
    """ChatGPT via HTTP REST instead of WebSocket. Same token, different transport."""

    def __init__(self, model: str | None = None):
        self.conversation_history: list[dict] = []
        self._model = model or DEFAULT_MODEL

    def _read_auth(self) -> tuple[str, str | None]:
        if not AUTH_FILE.exists():
            raise RuntimeError("Not authenticated with Codex. Run: codex login")
        data = json.loads(AUTH_FILE.read_text())
        tokens = data.get("tokens") or {}
        token = str(tokens.get("access_token") or "").strip()
        if not token:
            raise RuntimeError("No access token in ~/.codex/auth.json")
        account_id = str(tokens.get("account_id") or "").strip() or None
        return token, account_id

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
        token, account_id = self._read_auth()

        # Build Responses API payload (same as WebSocket, but via HTTP)
        from backend.adapters.chatgpt_direct_adapter import (
            _convert_messages_to_input,
            _convert_tools_to_ws,
            _convert_tool_choice,
            _result_to_chat_completions,
        )

        instructions, input_items = _convert_messages_to_input(messages)
        ws_tools = _convert_tools_to_ws(tools) if tools else []
        ws_tool_choice = _convert_tool_choice(tool_choice, has_tools=bool(ws_tools))

        payload = {
            "model": self._model,
            "instructions": instructions,
            "input": input_items,
            "tools": ws_tools,
            "tool_choice": ws_tool_choice,
            "store": False,
            "stream": False,
        }

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "originator": "codex_cli_rs",
        }
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(REST_URL, headers=headers, json=payload)

        if resp.status_code != 200:
            raise RuntimeError(f"ChatGPT REST error {resp.status_code}: {resp.text[:200]}")

        data = resp.json()

        # Parse response into standard format
        text = ""
        function_calls = []
        for item in data.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        text += content.get("text", "")
            elif item.get("type") == "function_call":
                function_calls.append({
                    "id": item.get("id", f"call_{uuid.uuid4().hex[:16]}"),
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "{}"),
                })

        result = {"text": text, "function_calls": function_calls}
        return _result_to_chat_completions(result, self._model)

    async def send_message(self, message: str, conversation_history: list[dict] | None = None) -> str:
        if not self.conversation_history and conversation_history:
            self.load_history(conversation_history)
        self.conversation_history.append({"role": "user", "content": message})

        messages_for_api = list(self.conversation_history)
        result = await self.raw_request(messages_for_api)

        text = result["choices"][0]["message"]["content"] or ""
        self.conversation_history.append({"role": "assistant", "content": text})
        return text

    def clear_history(self):
        self.conversation_history = []

    def is_authenticated(self) -> bool:
        try:
            self._read_auth()
            return True
        except RuntimeError:
            return False
