"""
ChatGPTDirectAdapter — FreeHive v0.5.3

Calls ChatGPT via the same WebSocket endpoint the Codex CLI uses:
  wss://chatgpt.com/backend-api/codex/responses

Uses the OAuth token from ~/.codex/auth.json — no API key, no subprocess.
Protocol discovered by reading the open-source Codex CLI Rust source (openai/codex).

Connection strategy: persistent — one TLS handshake per session, not per message.
The WebSocket stays open across turns and is only reopened when:
  - It was never opened yet
  - The server closed it (60-min limit → websocket_connection_limit_reached)
  - The connection dropped unexpectedly
  - clear_history() is called (new session)

Multi-turn memory: since free accounts require store=False, the server holds no
history server-side. We send the full conversation in the input array each time.
This is correct and fast — the payload grows with the conversation, but there is
no per-message connection overhead.

Tool use: raw_request() accepts Chat Completions format tools/tool_choice and
converts them to the Responses API format before sending. Function call events
are collected from the WebSocket stream and returned in Chat Completions format.

Falls back to ChatGPTAdapter (codex exec subprocess) only on persistent failures.
"""

import json
import logging
import time
import uuid
from pathlib import Path

import websockets
import websockets.exceptions

from backend.adapters.chatgpt_adapter import ChatGPTAdapter

AUTH_FILE = Path.home() / ".codex" / "auth.json"
WS_URL = "wss://chatgpt.com/backend-api/codex/responses"
DEFAULT_MODEL = "gpt-5.2"

# Reopen before the server's 60-minute connection limit
_WS_MAX_AGE_SECONDS = 55 * 60

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Format converters: Chat Completions ↔ Responses API
# ---------------------------------------------------------------------------

def _convert_messages_to_input(messages: list[dict]) -> tuple[str, list]:
    """
    Convert Chat Completions messages array to (instructions, input_items) for
    the Responses API WebSocket payload.

    System messages become instructions (last one wins).
    Tool result messages (role=tool) become top-level function_call_output items.
    Assistant messages with tool_calls become function_call content items.
    """
    instructions = "You are a helpful assistant."
    input_items = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content")

        if role == "system":
            if content:
                instructions = content if isinstance(content, str) else str(content)
            continue

        if role == "user":
            if isinstance(content, list):
                text = "\n".join(
                    b.get("text", "") for b in content if b.get("type") == "text"
                )
            else:
                text = content or ""
            input_items.append({
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            })

        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                # Text part (if any) → assistant content item
                if content:
                    input_items.append({
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    })
                # Function calls → top-level items, NOT nested in content
                for tc in tool_calls:
                    input_items.append({
                        "type": "function_call",
                        "id": tc["id"],
                        "call_id": tc["id"],
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    })
            else:
                input_items.append({
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content or ""}],
                })

        elif role == "tool":
            # Tool results are top-level items, not wrapped in role/content
            input_items.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id", ""),
                "output": content if isinstance(content, str) else json.dumps(content),
            })

    return instructions, input_items


def _convert_tools_to_ws(tools: list[dict]) -> list[dict]:
    """
    Convert Chat Completions tool definitions to Responses API format.
    Chat Completions wraps function defs in {"type": "function", "function": {...}}.
    Responses API wants them flat: {"type": "function", "name": ..., ...}.
    """
    result = []
    for t in tools:
        if t.get("type") == "function":
            f = t["function"]
            result.append({
                "type": "function",
                "name": f["name"],
                "description": f.get("description", ""),
                "parameters": f.get("parameters", {}),
                "strict": f.get("strict", False),
            })
    return result


def _convert_tool_choice(tool_choice, has_tools: bool):
    """Convert Chat Completions tool_choice to Responses API format."""
    if tool_choice is None:
        return "auto" if has_tools else "none"
    if isinstance(tool_choice, str):
        if tool_choice in ("none", "required"):
            return tool_choice
        return "auto"  # "auto" or anything unrecognised
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        return {"type": "function", "name": tool_choice["function"]["name"]}
    return "auto"


def _result_to_chat_completions(result: dict, model: str) -> dict:
    """
    Convert the structured WebSocket result to a Chat Completions response dict.
    result = {"text": str, "function_calls": [{"id", "name", "arguments"}, ...]}
    """
    text = result.get("text", "")
    function_calls = result.get("function_calls", [])

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    if function_calls:
        tool_calls = [
            {
                "id": fc["id"],
                "type": "function",
                "function": {
                    "name": fc["name"],
                    "arguments": fc["arguments"],
                },
            }
            for fc in function_calls
        ]
        message = {"role": "assistant", "content": text or None, "tool_calls": tool_calls}
        finish_reason = "tool_calls"
    else:
        message = {"role": "assistant", "content": text}
        finish_reason = "stop"

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class ChatGPTDirectAdapter:
    """
    Sends messages to ChatGPT via the Codex CLI WebSocket endpoint.
    One connection per session — persistent across all turns.
    """

    def __init__(self, model: str | None = None):
        self._session_id = str(uuid.uuid4())
        self._fallback = ChatGPTAdapter()
        self.conversation_history: list[dict] = []
        self._ws = None          # open WebSocket connection
        self._ws_opened_at = 0.0 # epoch seconds when connection was opened
        self._model = model or DEFAULT_MODEL

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def load_history(self, messages: list[dict]):
        """Rebuild in-memory history from DB messages."""
        self.conversation_history = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
        ]

    async def send_message(
        self,
        message: str,
        conversation_history: list[dict] | None = None,
    ) -> str:
        if not self.conversation_history and conversation_history:
            self.load_history(conversation_history)

        self.conversation_history.append({"role": "user", "content": message})

        try:
            token, account_id = self._read_auth()
            result = await self._send_over_ws(token, account_id)
            response_text = result["text"]
            if not response_text:
                raise RuntimeError("ChatGPT returned empty response via WebSocket.")
            self.conversation_history.append({"role": "assistant", "content": response_text})
            return response_text
        except Exception as exc:
            logger.warning("[ChatGPTDirect] Failed, falling back to subprocess: %s", exc)
            self.conversation_history.pop()
            await self._close_ws()
            return await self._fallback.send_message(message, conversation_history)

    async def raw_request(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice=None,
        thinking_effort: str = "off",
    ) -> dict:
        """
        Pass-through for the compat layer.
        Takes Chat Completions format messages/tools, returns a Chat Completions response dict.
        Does not touch self.conversation_history (the client owns state).
        """
        from backend.thinking import chatgpt_reasoning_param
        instructions, input_items = _convert_messages_to_input(messages)
        ws_tools = _convert_tools_to_ws(tools) if tools else []
        ws_tool_choice = _convert_tool_choice(tool_choice, has_tools=bool(ws_tools))

        try:
            token, account_id = self._read_auth()
            result = await self._send_over_ws(
                token, account_id,
                input_items=input_items,
                instructions=instructions,
                tools=ws_tools,
                tool_choice=ws_tool_choice,
                reasoning=chatgpt_reasoning_param(thinking_effort),
            )
            return _result_to_chat_completions(result, self._model)
        except Exception as exc:
            raise RuntimeError(f"ChatGPT request failed: {exc}") from exc

    async def close(self):
        """Call when the session is being torn down."""
        await self._close_ws()

    def clear_history(self):
        self.conversation_history = []
        self._session_id = str(uuid.uuid4())
        self._fallback.clear_history()
        self._ws_opened_at = 0.0  # force reconnect on next message

    def is_authenticated(self) -> bool:
        if not AUTH_FILE.exists():
            return False
        try:
            data = json.loads(AUTH_FILE.read_text())
            return bool((data.get("tokens") or {}).get("access_token"))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _read_auth(self) -> tuple[str, str | None]:
        if not AUTH_FILE.exists():
            raise RuntimeError("Not authenticated with Codex. Run: codex login")
        try:
            data = json.loads(AUTH_FILE.read_text())
        except Exception as exc:
            raise RuntimeError(f"Failed to read ~/.codex/auth.json: {exc}") from exc
        tokens = data.get("tokens") or {}
        token = str(tokens.get("access_token") or "").strip()
        if not token:
            raise RuntimeError("No access token in ~/.codex/auth.json. Run: codex login")
        account_id = str(tokens.get("account_id") or "").strip() or None
        return token, account_id

    # ------------------------------------------------------------------
    # Persistent WebSocket connection
    # ------------------------------------------------------------------

    def _ws_is_alive(self) -> bool:
        if self._ws is None:
            return False
        age = time.monotonic() - self._ws_opened_at
        if age >= _WS_MAX_AGE_SECONDS:
            return False
        try:
            return self._ws.close_code is None
        except Exception:
            return False

    async def _ensure_ws(self, token: str, account_id: str | None):
        if self._ws_is_alive():
            return
        await self._close_ws()
        headers = {
            "Authorization": f"Bearer {token}",
            "originator": "codex_cli_rs",
            "OpenAI-Beta": "responses_websockets=2026-02-06",
            "x-client-request-id": str(uuid.uuid4()),
            "session_id": self._session_id,
        }
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
        self._ws = await websockets.connect(
            WS_URL,
            additional_headers=headers,
            open_timeout=20,
        )
        self._ws_opened_at = time.monotonic()
        logger.debug("[ChatGPTDirect] WebSocket connected (session=%s)", self._session_id[:8])

    async def _close_ws(self):
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Core send/receive
    # ------------------------------------------------------------------

    def _build_input_items(self) -> list:
        """Build Responses API input array from internal conversation_history."""
        items = []
        for turn in self.conversation_history:
            if turn["role"] == "user":
                items.append({
                    "role": "user",
                    "content": [{"type": "input_text", "text": turn["content"]}],
                })
            else:
                items.append({
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": turn["content"]}],
                })
        return items

    async def _send_over_ws(
        self,
        token: str,
        account_id: str | None,
        *,
        input_items: list | None = None,
        instructions: str = "You are a helpful assistant.",
        tools: list | None = None,
        tool_choice=None,
        reasoning: dict | None = None,
    ) -> dict:
        """
        Try up to twice (existing connection, then reconnect).
        Returns {"text": str, "function_calls": [...]} dict.
        """
        if input_items is None:
            input_items = self._build_input_items()
        if tools is None:
            tools = []
        if tool_choice is None:
            tool_choice = "auto" if tools else "none"

        for attempt in range(2):
            try:
                await self._ensure_ws(token, account_id)
                return await self._do_request(input_items, instructions, tools, tool_choice, reasoning=reasoning)
            except websockets.exceptions.ConnectionClosed as exc:
                logger.warning("[ChatGPTDirect] Connection closed mid-flight: %s", exc)
                await self._close_ws()
                if attempt == 1:
                    raise RuntimeError(f"WebSocket connection lost: {exc}") from exc

    async def _do_request(
        self,
        input_items: list,
        instructions: str,
        tools: list,
        tool_choice,
        *,
        reasoning: dict | None = None,
    ) -> dict:
        """
        Send one response.create and collect the full response.
        Returns {"text": str, "function_calls": [{"id", "name", "arguments"}, ...]}
        """
        payload = {
            "type": "response.create",
            "model": self._model,
            "instructions": instructions,
            "input": input_items,
            "tools": tools,
            "tool_choice": tool_choice,
            "parallel_tool_calls": False,
            "store": False,
            "stream": True,
            "include": [],
        }
        if reasoning:
            payload["reasoning"] = reasoning

        await self._ws.send(json.dumps(payload))

        text_parts: list[str] = []
        # Keyed by BOTH item.id and item.call_id (they may differ).
        # Entries are shared dicts so either key updates the same object.
        function_calls: dict[str, dict] = {}

        async for raw in self._ws:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type", "")

            if event_type == "response.output_text.delta":
                delta = event.get("delta")
                if delta:
                    text_parts.append(delta)

            elif event_type == "response.output_item.added":
                item = event.get("item", {})
                if item.get("type") == "function_call":
                    item_id  = item.get("id") or str(uuid.uuid4())
                    call_id  = item.get("call_id") or item_id
                    entry = {
                        "id": item_id,
                        "name": item.get("name", ""),
                        "arguments_parts": [],
                    }
                    # Index by both so delta lookups hit regardless of which key they use
                    function_calls[item_id]  = entry
                    function_calls[call_id]  = entry

            elif event_type == "response.function_call_arguments.delta":
                # Try call_id first, then item_id
                key   = event.get("call_id") or event.get("item_id", "")
                delta = event.get("delta", "")
                if key in function_calls and delta:
                    function_calls[key]["arguments_parts"].append(delta)

            elif event_type == "response.output_item.done":
                # Authoritative: use completed arguments from this event, overriding
                # any partial deltas collected so far (handles empty-delta edge cases)
                item = event.get("item", {})
                if item.get("type") == "function_call":
                    item_id = item.get("id") or ""
                    call_id = item.get("call_id") or item_id
                    final_args = item.get("arguments", "")
                    for key in (item_id, call_id):
                        if key in function_calls and final_args:
                            function_calls[key]["arguments_parts"] = [final_args]

            elif event_type == "response.completed":
                break

            elif event_type == "response.failed":
                error = event.get("error") or event.get("response", {}).get("error", {})
                msg = error.get("message") if isinstance(error, dict) else str(error)
                raise RuntimeError(f"ChatGPT response failed: {msg}")

            elif event_type == "error":
                error = event.get("error") or {}
                msg = error.get("message") if isinstance(error, dict) else str(error)
                code = error.get("code") if isinstance(error, dict) else ""
                status = event.get("status_code") or event.get("status")
                if code == "websocket_connection_limit_reached":
                    await self._close_ws()
                    raise websockets.exceptions.ConnectionClosed(None, None)
                if status == 429:
                    raise RuntimeError(f"ChatGPT rate limited: {msg}")
                raise RuntimeError(f"ChatGPT WebSocket error ({status}): {msg}")

        # Deduplicate — both item_id and call_id keys point to the same entry object
        seen: set[int] = set()
        calls = []
        for fc in function_calls.values():
            if id(fc) not in seen:
                seen.add(id(fc))
                calls.append({
                    "id": fc["id"],
                    "name": fc["name"],
                    "arguments": "".join(fc["arguments_parts"]),
                })

        return {"text": "".join(text_parts), "function_calls": calls}
