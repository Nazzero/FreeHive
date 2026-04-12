"""
compat_router.py — FreeHive v0.5.3

Drop-in API compatibility layer. Lets any tool that speaks Anthropic or OpenAI
point at FreeHive instead of the real API servers.

Endpoints:
  POST /v1/messages          — Anthropic Messages API (Claude Code, Claw Code, SDK)
  POST /v1/chat/completions  — OpenAI Chat Completions API (Continue.dev, Cursor, etc.)

Model routing (by model name prefix):
  claude-* / claude   → ClaudeDirectAdapter
  gpt-* / o1-* / o3-* / o4-* / codex-* / chatgpt → ChatGPTDirectAdapter
  gemini-*  / gemini  → GeminiDirectAdapter
  (unrecognised)      → falls back to selected_tool in ~/.freehive/config.json

Auth: any non-empty x-api-key or Authorization header is accepted.
FreeHive uses its own OAuth tokens — the key value is not checked.

Streaming: both endpoints support stream=true/false.
Adapters return full text; we chunk it into SSE events so streaming clients
(Claude Code uses streaming by default) get valid responses.
"""

import json
import time
import uuid
import logging
import hashlib
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

compat_router = APIRouter()

# ---------------------------------------------------------------------------
# API key → provider mapping
#
# Three fixed keys, one per provider. Use these in Cursor / Continue.dev / SDK:
#
#   freehive-claude    →  ClaudeDirectAdapter   (model prefix: claude-*)
#   freehive-chatgpt   →  ChatGPTDirectAdapter  (model prefix: gpt-*, o4-*, etc.)
#   freehive-gemini    →  GeminiDirectAdapter   (model prefix: gemini-*)
#
# The model name in the request body overrides the provider default.
# If no key matches a provider, routing falls back to the model name prefix.
# ---------------------------------------------------------------------------

# Legacy 3-key shortcuts (still supported for backward compat)
_KEY_PROVIDER = {
    "freehive-claude":  "claude",
    "freehive-chatgpt": "chatgpt",
    "freehive-gemini":  "gemini",
}


def _get_api_key(request: Request) -> str:
    return (
        request.headers.get("x-api-key")
        or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    )


def _adapter_for_model_id(model_id: str):
    """
    Instantiate the right adapter for an exact model ID string.
    Returns (adapter_instance, provider_name).
    """
    m = model_id.lower()
    if m == "claude" or m.startswith("claude-"):
        from backend.adapters.claude_direct_adapter import ClaudeDirectAdapter
        return ClaudeDirectAdapter(model=None if m == "claude" else model_id), "claude"
    if m == "chatgpt" or any(m.startswith(p) for p in ("gpt-", "o1-", "o3-", "o4-", "codex-", "chatgpt")):
        from backend.adapters.chatgpt_direct_adapter import ChatGPTDirectAdapter
        return ChatGPTDirectAdapter(model=None if m == "chatgpt" else model_id), "chatgpt"
    if m == "gemini" or m.startswith("gemini-"):
        from backend.adapters.gemini_direct_adapter import GeminiDirectAdapter
        return GeminiDirectAdapter(model=None if m == "gemini" else model_id), "gemini"
    # Unknown model — default to claude
    from backend.adapters.claude_direct_adapter import ClaudeDirectAdapter
    return ClaudeDirectAdapter(), "claude"


def _adapter_for_request(api_key: str, model: str):
    """
    Resolve provider and model from API key first, then fall back to model name prefix.

    Key formats (in priority order):
      freehive-<model-id>   — exact model routing, e.g. freehive-claude-haiku-4-5
      freehive-claude        — legacy: routes to claude with default model
      freehive-chatgpt       — legacy: routes to chatgpt with default model
      freehive-gemini        — legacy: routes to gemini with default model
      (any other key)        — fall back to model name prefix in request body

    Returns (adapter_instance, provider_name).
    """
    key = api_key.strip()

    # freehive-[model-id] format: strip prefix and use remainder as exact model ID
    if key.lower().startswith("freehive-"):
        key_model = key[len("freehive-"):]  # e.g. "claude-haiku-4-5" or "gpt-5.4"
        if key_model and key_model not in ("claude", "chatgpt", "gemini"):
            # Specific model key — ignore the model field in the request body
            return _adapter_for_model_id(key_model)
        # Legacy 3-key shortcuts
        provider = _KEY_PROVIDER.get(key.lower())
        if provider:
            return _adapter_for_model_id(provider)  # uses provider default model

    # No matching key — infer from model name in request body
    return _adapter_for_model_id(model if model else _get_selected_tool() or "claude")


def _get_selected_tool() -> str | None:
    config_file = Path.home() / ".freehive" / "config.json"
    try:
        return json.loads(config_file.read_text()).get("selected_tool")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _check_auth(request: Request) -> str:
    """Return the API key, or raise 401 if missing."""
    api_key = _get_api_key(request)
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Use x-api-key or Authorization: Bearer <key>. "
                   "Provider keys: freehive-claude, freehive-chatgpt, freehive-gemini",
        )
    return api_key


def _extract_messages(messages: list[dict]) -> tuple[list[dict], str]:
    """
    Extract history (all messages except last user turn) and the last user message.
    Returns (history, last_user_text).
    Handles both string content and Anthropic content-block arrays.
    """
    if not messages:
        raise HTTPException(status_code=400, detail="messages array is empty")

    def _text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(block.get("text") or block.get("content") or "")
            return "\n".join(p for p in parts if p)
        return str(content)

    # Find last user message
    last_user_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break

    if last_user_idx is None:
        raise HTTPException(status_code=400, detail="No user message found in messages array")

    last_user_text = _text(messages[last_user_idx]["content"])

    # Everything before the last user message becomes history
    history = []
    for msg in messages[:last_user_idx]:
        role = msg.get("role", "user")
        if role == "system":
            continue  # skip system messages (adapters use their own instructions)
        history.append({"role": role, "content": _text(msg["content"])})

    return history, last_user_text


def _first_user_text(messages: list[dict]) -> str:
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text") or block.get("content")
                        if isinstance(text, str) and text.strip():
                            parts.append(text)
                if parts:
                    return "\n".join(parts)
    return ""


def _title_from_text(text: str) -> str:
    words = str(text or "").strip().split()
    if not words:
        return "New chat"
    title = " ".join(words[:8])
    if len(words) > 8:
        title += "..."
    return title


def _message_preview_text(msg: dict) -> str:
    role = str(msg.get("role") or "")
    content = msg.get("content")

    # OpenAI tool calls
    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        chunks = []
        if isinstance(content, str) and content.strip():
            chunks.append(content.strip())
        for tc in tool_calls:
            fn = (tc.get("function") or {}).get("name", "")
            args = (tc.get("function") or {}).get("arguments", "")
            chunks.append(f"[tool_call] {fn}({args})".strip())
        return "\n".join([c for c in chunks if c]).strip()

    # Anthropic content blocks
    if isinstance(content, list):
        chunks = []
        for block in content:
            if not isinstance(block, dict):
                chunks.append(str(block))
                continue
            btype = block.get("type")
            if isinstance(block.get("text"), str):
                chunks.append(block["text"])
            elif btype == "tool_use":
                name = block.get("name", "")
                tool_input = json.dumps(block.get("input", {}), ensure_ascii=False)
                chunks.append(f"[tool_use] {name}({tool_input})".strip())
            elif btype == "tool_result":
                tool_output = block.get("content", "")
                chunks.append(f"[tool_result] {tool_output}".strip())
            else:
                chunks.append(json.dumps(block, ensure_ascii=False))
        return "\n".join([c for c in chunks if str(c).strip()]).strip()

    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, str):
        return content

    # Top-level items (rare, but keep for robustness)
    if msg.get("type") == "function_call":
        return f"[function_call] {msg.get('name','')}({msg.get('arguments','')})".strip()
    if msg.get("type") == "function_call_output":
        return f"[function_call_output] {msg.get('output','')}".strip()

    return str(content or "")


def _storage_row_from_message(msg: dict) -> dict:
    role = str(msg.get("role") or "assistant")
    if role not in {"user", "assistant", "system", "tool"}:
        role = "assistant"
    preview = _message_preview_text(msg)
    if not preview:
        preview = json.dumps(msg, ensure_ascii=False)
    return {
        "role": role,
        "content": preview[:20000],
        "content_type": "json",
        "meta": msg,
    }


def _assistant_row_from_response(provider: str, response_obj: dict) -> dict:
    if provider == "claude":
        # Anthropic response format
        content = response_obj.get("content", [])
        message = {"role": "assistant", "content": content}
        return _storage_row_from_message(message)

    # OpenAI-compatible response format
    choice = (response_obj.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    if "role" not in message:
        message["role"] = "assistant"
    return _storage_row_from_message(message)


def _compat_external_key(request: Request, provider: str, model: str, messages: list[dict]) -> str:
    # If client provides its own conversation/session id, prefer it.
    for hdr in (
        "x-freehive-session-id",
        "x-conversation-id",
        "openai-conversation-id",
        "anthropic-conversation-id",
        "x-session-id",
    ):
        value = request.headers.get(hdr, "").strip()
        if value:
            return f"hdr:{hdr}:{value}"

    api_key = _get_api_key(request).strip()
    first_user = _first_user_text(messages).strip()
    digest = hashlib.sha256(
        f"{provider}|{model}|{api_key}|{first_user}".encode("utf-8", errors="ignore")
    ).hexdigest()[:40]
    return f"auto:{digest}"


def _persist_compat_conversation(
    request: Request,
    *,
    provider: str,
    model: str,
    endpoint: str,
    incoming_messages: list[dict],
    response_obj: dict | None = None,
    fallback_text: str | None = None,
) -> None:
    cm = request.app.state.conversation_manager
    first_user = _first_user_text(incoming_messages)
    external_key = _compat_external_key(request, provider, model, incoming_messages)
    session = cm.get_or_create_external_session(
        source="compat",
        provider=provider,
        model=model,
        external_key=external_key,
        title=_title_from_text(first_user),
        metadata={
            "endpoint": endpoint,
            "user_agent": request.headers.get("user-agent", "")[:300],
        },
    )

    cm.replace_messages(
        session["id"],
        [_storage_row_from_message(m) for m in incoming_messages],
    )

    if response_obj is not None:
        row = _assistant_row_from_response(provider, response_obj)
        cm.add_message(
            session["id"],
            row["role"],
            row["content"],
            content_type=row["content_type"],
            meta=row.get("meta"),
        )
    elif fallback_text is not None:
        cm.add_message(session["id"], "assistant", fallback_text)


# ---------------------------------------------------------------------------
# Anthropic /v1/messages
# ---------------------------------------------------------------------------

class AnthropicRequest(BaseModel):
    model: str = "claude-haiku-4-5"
    messages: list[dict]
    max_tokens: int = 8096
    system: str | None = None
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    tools: list[dict] | None = None
    tool_choice: dict | None = None


@compat_router.post("/v1/messages")
async def anthropic_messages(body: AnthropicRequest, request: Request):
    api_key = _check_auth(request)
    adapter, provider = _adapter_for_request(api_key, body.model)
    resolved_model = getattr(adapter, "_model", body.model)

    # Claude: pass the full request through to the real Anthropic API.
    # This preserves tools, tool_choice, multi-content messages, and tool_result turns
    # so agentic clients (Claude Code, SDK agents) work correctly.
    if provider == "claude" and hasattr(adapter, "raw_request"):
        try:
            result = await adapter.raw_request(
                body.messages,
                max_tokens=body.max_tokens,
                system=body.system,
                tools=body.tools,
                tool_choice=body.tool_choice,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except Exception as exc:
            logger.exception("[compat/messages] Unexpected error")
            raise HTTPException(status_code=500, detail=str(exc))

        if body.stream:
            _persist_compat_conversation(
                request,
                provider=provider,
                model=resolved_model,
                endpoint="/v1/messages",
                incoming_messages=body.messages,
                response_obj=result,
            )
            return StreamingResponse(
                _anthropic_sse_from_response(result),
                media_type="text/event-stream",
            )
        _persist_compat_conversation(
            request,
            provider=provider,
            model=resolved_model,
            endpoint="/v1/messages",
            incoming_messages=body.messages,
            response_obj=result,
        )
        return result

    # Non-Claude providers: text-only path
    history, last_user_text = _extract_messages(body.messages)
    if history:
        adapter.load_history(history)

    try:
        text = await adapter.send_message(last_user_text)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.exception("[compat/messages] Unexpected error")
        raise HTTPException(status_code=500, detail=str(exc))

    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    if body.stream:
        _persist_compat_conversation(
            request,
            provider=provider,
            model=resolved_model,
            endpoint="/v1/messages",
            incoming_messages=body.messages,
            fallback_text=text,
        )
        return StreamingResponse(
            _anthropic_sse(msg_id, body.model, text),
            media_type="text/event-stream",
        )

    _persist_compat_conversation(
        request,
        provider=provider,
        model=resolved_model,
        endpoint="/v1/messages",
        incoming_messages=body.messages,
        fallback_text=text,
    )
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": body.model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def _anthropic_sse_from_response(response: dict):
    """
    Convert a full Anthropic API response dict into proper SSE events.
    Handles text blocks and tool_use blocks so agentic clients work correctly.
    """
    def evt(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    msg_id = response.get("id", f"msg_{uuid.uuid4().hex[:24]}")
    model = response.get("model", "claude")
    usage = response.get("usage", {"input_tokens": 0, "output_tokens": 0})

    yield evt("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": usage,
        },
    })

    for i, block in enumerate(response.get("content", [])):
        block_type = block.get("type")

        if block_type == "text":
            yield evt("content_block_start", {
                "type": "content_block_start",
                "index": i,
                "content_block": {"type": "text", "text": ""},
            })
            yield evt("ping", {"type": "ping"})
            text = block.get("text", "")
            chunk_size = 20
            for j in range(0, len(text), chunk_size):
                yield evt("content_block_delta", {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "text_delta", "text": text[j:j + chunk_size]},
                })
            yield evt("content_block_stop", {"type": "content_block_stop", "index": i})

        elif block_type == "tool_use":
            yield evt("content_block_start", {
                "type": "content_block_start",
                "index": i,
                "content_block": {
                    "type": "tool_use",
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "input": {},
                },
            })
            # Stream the tool input as JSON deltas
            input_json = json.dumps(block.get("input", {}))
            chunk_size = 20
            for j in range(0, len(input_json), chunk_size):
                yield evt("content_block_delta", {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "input_json_delta", "partial_json": input_json[j:j + chunk_size]},
                })
            yield evt("content_block_stop", {"type": "content_block_stop", "index": i})

    stop_reason = response.get("stop_reason", "end_turn")
    yield evt("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": usage.get("output_tokens", 0)},
    })
    yield evt("message_stop", {"type": "message_stop"})


def _anthropic_sse(msg_id: str, model: str, text: str):
    """Emit Anthropic streaming SSE events for the full response text."""

    def evt(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    yield evt("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })

    yield evt("content_block_start", {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    })

    yield evt("ping", {"type": "ping"})

    # Stream the text in chunks of ~20 chars so clients see progressive output
    chunk_size = 20
    for i in range(0, len(text), chunk_size):
        yield evt("content_block_delta", {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text[i:i + chunk_size]},
        })

    yield evt("content_block_stop", {"type": "content_block_stop", "index": 0})

    yield evt("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 0},
    })

    yield evt("message_stop", {"type": "message_stop"})


# ---------------------------------------------------------------------------
# OpenAI /v1/chat/completions
# ---------------------------------------------------------------------------

class OpenAIRequest(BaseModel):
    model: str = "gpt-5.2"
    messages: list[dict]
    max_tokens: int | None = None
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    tools: list[dict] | None = None
    tool_choice: object | None = None


@compat_router.post("/v1/chat/completions")
async def openai_chat_completions(body: OpenAIRequest, request: Request):
    api_key = _check_auth(request)
    adapter, provider = _adapter_for_request(api_key, body.model)
    resolved_model = getattr(adapter, "_model", body.model)

    # ChatGPT + Gemini: pass the full request through — supports tools, tool_choice,
    # and multi-turn tool_result messages. Both adapters return Chat Completions format.
    if provider in ("chatgpt", "gemini") and hasattr(adapter, "raw_request"):
        try:
            result = await adapter.raw_request(
                body.messages,
                tools=body.tools,
                tool_choice=body.tool_choice,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except Exception as exc:
            logger.exception("[compat/completions] Unexpected error")
            raise HTTPException(status_code=500, detail=str(exc))

        if body.stream:
            _persist_compat_conversation(
                request,
                provider=provider,
                model=resolved_model,
                endpoint="/v1/chat/completions",
                incoming_messages=body.messages,
                response_obj=result,
            )
            return StreamingResponse(
                _openai_sse_from_response(result),
                media_type="text/event-stream",
            )
        _persist_compat_conversation(
            request,
            provider=provider,
            model=resolved_model,
            endpoint="/v1/chat/completions",
            incoming_messages=body.messages,
            response_obj=result,
        )
        return result

    # Other providers: text-only path
    history, last_user_text = _extract_messages(body.messages)
    if history:
        adapter.load_history(history)

    try:
        text = await adapter.send_message(last_user_text)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.exception("[compat/completions] Unexpected error")
        raise HTTPException(status_code=500, detail=str(exc))

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    if body.stream:
        _persist_compat_conversation(
            request,
            provider=provider,
            model=resolved_model,
            endpoint="/v1/chat/completions",
            incoming_messages=body.messages,
            fallback_text=text,
        )
        return StreamingResponse(
            _openai_sse(completion_id, body.model, created, text),
            media_type="text/event-stream",
        )

    _persist_compat_conversation(
        request,
        provider=provider,
        model=resolved_model,
        endpoint="/v1/chat/completions",
        incoming_messages=body.messages,
        fallback_text=text,
    )
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": body.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _openai_sse_from_response(response: dict):
    """
    Convert a Chat Completions response dict into OpenAI streaming SSE events.
    Handles both text responses and tool_calls responses.
    """
    completion_id = response.get("id", f"chatcmpl-{uuid.uuid4().hex[:24]}")
    model = response.get("model", "gpt-5.4")
    created = response.get("created", int(time.time()))
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason", "stop")
    tool_calls = message.get("tool_calls")
    text = message.get("content") or ""

    def chunk(delta: dict, finish: str | None = None) -> str:
        data = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(data)}\n\n"

    # Role chunk
    yield chunk({"role": "assistant", "content": "" if not tool_calls else None})

    if tool_calls:
        # Emit each tool call with its full arguments
        for i, tc in enumerate(tool_calls):
            # Opening chunk with id + name
            yield chunk({"tool_calls": [{
                "index": i,
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["function"]["name"], "arguments": ""},
            }]})
            # Arguments in pieces
            args = tc["function"]["arguments"]
            chunk_size = 20
            for j in range(0, len(args), chunk_size):
                yield chunk({"tool_calls": [{
                    "index": i,
                    "function": {"arguments": args[j:j + chunk_size]},
                }]})
    else:
        # Stream text content
        chunk_size = 20
        for i in range(0, len(text), chunk_size):
            yield chunk({"content": text[i:i + chunk_size]})

    yield chunk({}, finish=finish_reason)
    yield "data: [DONE]\n\n"


def _openai_sse(completion_id: str, model: str, created: int, text: str):
    """Emit OpenAI streaming SSE events for the full response text."""

    def chunk(delta: dict) -> str:
        data = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        return f"data: {json.dumps(data)}\n\n"

    # Role chunk first
    yield chunk({"role": "assistant", "content": ""})

    # Content in small pieces
    chunk_size = 20
    for i in range(0, len(text), chunk_size):
        yield chunk({"content": text[i:i + chunk_size]})

    # Stop chunk
    stop_data = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(stop_data)}\n\n"
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Model list endpoints (some clients check these on startup)
# ---------------------------------------------------------------------------

@compat_router.get("/v1/keys")
async def list_keys():
    """Returns the fixed API keys and which provider each connects to."""
    return {
        "keys": [
            {
                "key": "freehive-claude",
                "provider": "claude",
                "default_model": "claude-haiku-4-5",
                "available_models": ["claude-haiku-4-5", "claude-sonnet-4-5"],
            },
            {
                "key": "freehive-chatgpt",
                "provider": "chatgpt",
                "default_model": "gpt-5.2",
                "available_models": ["gpt-5.2"],
            },
            {
                "key": "freehive-gemini",
                "provider": "gemini",
                "default_model": "gemini-3-flash-preview",
                "available_models": ["gemini-3-flash-preview", "gemini-2.5-flash-lite"],
            },
        ]
    }


@compat_router.get("/v1/models")
async def list_models(request: Request):
    _check_auth(request)
    from backend.model_discovery import get_cached_discovery

    owned_by = {"claude": "anthropic", "chatgpt": "openai", "gemini": "google"}
    discovery = get_cached_discovery()
    models = []

    if discovery:
        for provider, data in discovery.items():
            for m in data.get("models", []):
                models.append({
                    "id": m["id"],
                    "object": "model",
                    "owned_by": owned_by.get(provider, provider),
                    "display_name": m.get("display_name", m["id"]),
                    "note": m.get("note", ""),
                })
    else:
        # Fallback if no discovery has run yet
        models = [
            {"id": "claude-haiku-4-5",       "object": "model", "owned_by": "anthropic"},
            {"id": "claude-sonnet-4-5",       "object": "model", "owned_by": "anthropic"},
            {"id": "gpt-5.2",                 "object": "model", "owned_by": "openai"},
            {"id": "gemini-3-flash-preview",  "object": "model", "owned_by": "google"},
            {"id": "gemini-2.5-flash-lite",   "object": "model", "owned_by": "google"},
        ]

    return {"object": "list", "data": models}
