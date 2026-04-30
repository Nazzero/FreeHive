"""
Qwen Direct Adapter — calls chat.qwen.ai/api/chat/completions directly.

Uses JWT from browser login (Google/GitHub OAuth on chat.qwen.ai).
Token stored in ~/.freehive/qwen_session.json.
OpenAI-compatible request/response format — no conversion needed.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import AsyncGenerator

import httpx

logger = logging.getLogger(__name__)

QWEN_BASE_URL = "https://chat.qwen.ai"
QWEN_CHAT_URL = f"{QWEN_BASE_URL}/api/chat/completions"
QWEN_MODELS_URL = f"{QWEN_BASE_URL}/api/models"
SESSION_FILE = Path.home() / ".freehive" / "qwen_session.json"
DEFAULT_MODEL = "qwen3.6-plus"
MAX_RETRIES = 2
_REQUEST_SEM = asyncio.Semaphore(3)


class QwenDirectAdapter:
    """
    Calls chat.qwen.ai OpenAI-compatible API using JWT from browser login.
    Supports streaming, tool calling, and thinking mode.
    """

    def __init__(self, model: str | None = None):
        self.conversation_history: list[dict] = []
        self._model = model or DEFAULT_MODEL

    def load_history(self, history: list[dict]):
        self.conversation_history = [
            {"role": m["role"], "content": m["content"]}
            for m in history
        ]

    def clear_history(self):
        self.conversation_history = []

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    @staticmethod
    def _read_session() -> dict:
        if not SESSION_FILE.exists():
            raise RuntimeError(
                "Not authenticated with Qwen. "
                "Go to Settings > Accounts and log in to chat.qwen.ai."
            )
        try:
            data = json.loads(SESSION_FILE.read_text())
        except Exception:
            raise RuntimeError("Qwen session file is corrupted. Please re-login.")
        token = data.get("token", "")
        if not token:
            raise RuntimeError("No Qwen token found. Please re-login via Settings.")
        return data

    @staticmethod
    def _is_expired(session: dict) -> bool:
        expires_at = session.get("expires_at", 0)
        if not expires_at:
            return False
        return time.time() >= expires_at

    def _get_token(self) -> str:
        session = self._read_session()
        if self._is_expired(session):
            raise RuntimeError(
                "Qwen session expired. Please re-login via Settings > Accounts."
            )
        return session["token"]

    @staticmethod
    def is_authenticated() -> bool:
        try:
            if not SESSION_FILE.exists():
                return False
            data = json.loads(SESSION_FILE.read_text())
            token = data.get("token", "")
            if not token:
                return False
            expires_at = data.get("expires_at", 0)
            if expires_at and time.time() >= expires_at:
                return False
            return True
        except Exception:
            return False

    @staticmethod
    def save_session(token: str, expires_at: int = 0, **extra):
        """Save JWT token from browser login."""
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "token": token,
            "token_type": "Bearer",
            "expires_at": expires_at,
            "saved_at": int(time.time()),
            **extra,
        }
        SESSION_FILE.write_text(json.dumps(data, indent=2))
        SESSION_FILE.chmod(0o600)
        logger.info("[QwenAdapter] Session saved (expires_at=%s)", expires_at)

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    async def _call_api(
        self,
        messages: list[dict],
        *,
        stream: bool = False,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """Non-streaming API call."""
        token = self._get_token()
        body: dict = {
            "model": self._model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Origin": QWEN_BASE_URL,
            "Referer": f"{QWEN_BASE_URL}/",
        }

        async with _REQUEST_SEM:
            for attempt in range(MAX_RETRIES + 1):
                try:
                    async with httpx.AsyncClient(timeout=120.0) as client:
                        resp = await client.post(QWEN_CHAT_URL, headers=headers, json=body)

                    if resp.status_code == 401:
                        raise RuntimeError(
                            "Qwen session expired or invalid. Please re-login via Settings."
                        )
                    if resp.status_code == 429:
                        wait = min(2 ** attempt * 2, 30)
                        logger.warning("[QwenAdapter] Rate limited, waiting %ds", wait)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status_code != 200:
                        raise RuntimeError(
                            f"Qwen API error {resp.status_code}: {resp.text[:500]}"
                        )
                    return resp.json()
                except httpx.TimeoutException:
                    if attempt < MAX_RETRIES:
                        continue
                    raise RuntimeError("Qwen API timed out after retries.")

        raise RuntimeError("Qwen API failed after all retries.")

    async def stream_chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Streaming API call — yields SSE chunks as dicts."""
        token = self._get_token()
        body: dict = {
            "model": self._model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Origin": QWEN_BASE_URL,
            "Referer": f"{QWEN_BASE_URL}/",
        }

        async with _REQUEST_SEM:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("POST", QWEN_CHAT_URL, headers=headers, json=body) as resp:
                    if resp.status_code == 401:
                        raise RuntimeError(
                            "Qwen session expired or invalid. Please re-login."
                        )
                    if resp.status_code != 200:
                        body_text = await resp.aread()
                        raise RuntimeError(
                            f"Qwen API error {resp.status_code}: {body_text.decode()[:500]}"
                        )

                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        if line == "data: [DONE]":
                            return
                        if line.startswith("data: "):
                            try:
                                chunk = json.loads(line[6:])
                                yield chunk
                            except json.JSONDecodeError:
                                continue

    # ------------------------------------------------------------------
    # High-level send (for UI chat)
    # ------------------------------------------------------------------

    async def send_message(self, message: str, history: list[dict] = None) -> str:
        if not self.conversation_history and history:
            self.load_history(history)

        self.conversation_history.append({"role": "user", "content": message})

        result = await self._call_api(self.conversation_history)

        choices = result.get("choices", [])
        if not choices:
            raise RuntimeError("Qwen returned empty response.")

        assistant_msg = choices[0].get("message", {})
        assistant_text = assistant_msg.get("content", "")

        self.conversation_history.append(
            {"role": "assistant", "content": assistant_text}
        )
        return assistant_text

    # ------------------------------------------------------------------
    # Model discovery (static, no auth needed)
    # ------------------------------------------------------------------

    @staticmethod
    async def fetch_models() -> list[dict]:
        """Fetch available models from chat.qwen.ai (no auth required)."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    QWEN_MODELS_URL,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
            if resp.status_code != 200:
                return []
            data = resp.json()
            models = []
            for m in data.get("data", []):
                mid = m.get("id", "")
                info = m.get("info", {})
                meta = info.get("meta", {})
                caps = meta.get("capabilities", {})
                models.append({
                    "id": mid,
                    "display_name": info.get("name", mid),
                    "description": meta.get("short_description", ""),
                    "capabilities": caps,
                    "max_context": meta.get("max_context_length", 0),
                    "thinking": caps.get("thinking", False),
                    "vision": caps.get("vision", False),
                    "search": caps.get("search", False),
                })
            return models
        except Exception as exc:
            logger.warning("[QwenAdapter] Failed to fetch models: %s", exc)
            return []
