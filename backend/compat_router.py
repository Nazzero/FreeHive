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
#   freehive-qwen      →  QwenDirectAdapter     (model prefix: qwen*)
#
# The model name in the request body overrides the provider default.
# If no key matches a provider, routing falls back to the model name prefix.
# ---------------------------------------------------------------------------

# Legacy 3-key shortcuts (still supported for backward compat)
_KEY_PROVIDER = {
    "freehive-claude":  "claude",
    "freehive-chatgpt": "chatgpt",
    "freehive-gemini":  "gemini",
    "freehive-qwen":    "qwen",
    "freehive-arena":   "arena",
}

# --------------------------------------------------------------------------- #
# Claude OAuth tool-name collision workaround
#
# Anthropic's OAuth path rejects requests that ship tools whose names collide
# with Claude Code's first-party tool names in the *exact lowercase spelling*
# Claude Code doesn't use. Empirically confirmed on 2026-04-14 against
# claude-sonnet-4-6 via claude-cli/2.1.92 OAuth:
#
#     tool name         result
#     ----------------  -------
#     todowrite         400 "out of extra usage"  ← blocked
#     TodoWrite         200 OK                    ← Claude Code's real name
#     taskwrite         200 OK
#     zzz_todo_test     200 OK
#
# The rejection message is a misleading "out of extra usage" 400 — not a real
# quota error. This appears to be an anti-spoofing check: third-party clients
# may not register a tool under a first-party Claude Code tool name with a
# non-matching schema.
#
# To keep OpenCode (and anything else using /ai-sdk/openai-compatible) working
# without forcing them to rename their tools, we rewrite reserved names to a
# safe "fh_" prefix on the outbound Anthropic request, and rewrite them back
# on the inbound response. The client never sees the aliasing.
# --------------------------------------------------------------------------- #

# Known lowercase tool names Anthropic's OAuth tier rejects. Extend as we
# discover more (probably matches every lowercase variant of Claude Code's
# first-party tool list: Bash, Read, Edit, Write, Glob, Grep, TodoWrite, etc).
_CLAUDE_OAUTH_RESERVED_TOOL_NAMES = {
    "todowrite",
}

_CLAUDE_OAUTH_ALIAS_PREFIX = "fh_"


def _alias_tool_name(name: str) -> str:
    """Return the Anthropic-safe alias for a tool name if it collides with a
    reserved first-party name, otherwise return the name unchanged."""
    if name and name.lower() in _CLAUDE_OAUTH_RESERVED_TOOL_NAMES:
        return f"{_CLAUDE_OAUTH_ALIAS_PREFIX}{name}"
    return name


def _unalias_tool_name(name: str) -> str:
    """Inverse of _alias_tool_name — strip the fh_ prefix if the underlying
    name is in the reserved set."""
    if name and name.startswith(_CLAUDE_OAUTH_ALIAS_PREFIX):
        stripped = name[len(_CLAUDE_OAUTH_ALIAS_PREFIX):]
        if stripped.lower() in _CLAUDE_OAUTH_RESERVED_TOOL_NAMES:
            return stripped
    return name


def _get_api_key(request: Request) -> str:
    return (
        request.headers.get("x-api-key")
        or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    )


def _adapter_for_model_id(model_id: str):
    """
    Instantiate the right adapter cascade for an exact model ID string.
    Returns (adapter_cascade_or_instance, provider_name).

    Each provider adapter is wrapped in a cascade with fallback strategies:
      Claude:  direct_oauth → subprocess_cli → user_api_key
      ChatGPT: direct_ws → rest_fallback → user_api_key
      Gemini:  direct_codeassist → subprocess_cli → user_api_key
      Arena:   direct (already has 4 internal fallback layers)
    """
    from backend.resilience.cascade_factory import (
        build_claude_cascade,
        build_chatgpt_cascade,
        build_gemini_cascade,
        build_qwen_cascade,
    )

    m = model_id.lower()
    # Arena MUST be checked first — arena models like "arena/claude-*" or "arena/gpt-*"
    # would otherwise match provider prefixes
    if m.startswith("arena/") or m.startswith("arena-"):
        from backend.adapters.arena_bridge_adapter import ArenaBridgeAdapter
        adapter = ArenaBridgeAdapter()
        adapter._model = model_id
        return adapter, "arena"
    if m == "claude" or m.startswith("claude-"):
        model = None if m == "claude" else model_id
        return build_claude_cascade(model=model), "claude"
    if m == "chatgpt" or any(m.startswith(p) for p in ("gpt-", "o1-", "o3-", "o4-", "codex-", "chatgpt")):
        model = None if m == "chatgpt" else model_id
        return build_chatgpt_cascade(model=model), "chatgpt"
    if m == "gemini" or m.startswith("gemini-"):
        model = None if m == "gemini" else model_id
        return build_gemini_cascade(model=model), "gemini"
    if m == "qwen" or m.startswith("qwen"):
        model = None if m == "qwen" else model_id
        return build_qwen_cascade(model=model), "qwen"
    # Unknown model — default to claude cascade
    return build_claude_cascade(), "claude"


def _model_matches_provider(model: str, provider: str) -> bool:
    """Return True if the model name belongs to the given provider."""
    m = model.lower()
    if provider == "claude":
        return m.startswith("claude-")
    if provider == "chatgpt":
        return any(m.startswith(p) for p in ("gpt-", "o1-", "o3-", "o4-", "codex-", "chatgpt"))
    if provider == "gemini":
        return m.startswith("gemini-")
    if provider == "qwen":
        return m.startswith("qwen")
    if provider == "arena":
        return m.startswith("arena/") or m.startswith("arena-")
    return False


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
        if key_model and key_model not in ("claude", "chatgpt", "gemini", "qwen", "arena"):
            # Specific model key — ignore the model field in the request body
            return _adapter_for_model_id(key_model)
        # Legacy 3-key shortcuts — honour the model from the request body if it
        # matches the provider, otherwise fall back to the provider default.
        provider = _KEY_PROVIDER.get(key.lower())
        if provider:
            if model and _model_matches_provider(model, provider):
                return _adapter_for_model_id(model)
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
                   "Provider keys: freehive-claude, freehive-chatgpt, freehive-gemini, freehive-qwen",
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
    system: str | list | None = None  # String or Anthropic content-block array
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    tools: list[dict] | None = None
    tool_choice: dict | None = None
    thinking_effort: str | None = None


@compat_router.post("/v1/messages")
async def anthropic_messages(body: AnthropicRequest, request: Request):
    api_key = _check_auth(request)

    # ── Thinking effort: parse model suffix, resolve priority ──
    from backend.thinking import parse_model_think_suffix, resolve_effort
    clean_model, suffix_effort = parse_model_think_suffix(body.model)
    effort = resolve_effort(body.thinking_effort, suffix_effort)

    adapter, provider = _adapter_for_request(api_key, clean_model)
    resolved_model = getattr(adapter, "_model", clean_model)

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
                thinking_effort=effort,
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

    # Arena via /v1/messages: route through raw_request() for tool support,
    # then convert OpenAI Chat Completions response to Anthropic Messages format.
    # This lets OpenClaude/Claude Code use arena models with full tool calling.
    if provider == "arena" and hasattr(adapter, "raw_request"):
        # Convert Anthropic tool definitions to OpenAI format
        openai_tools = None
        if body.tools:
            openai_tools = []
            for t in body.tools:
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                    },
                })
        openai_tool_choice = None
        if body.tool_choice and openai_tools:
            tc = body.tool_choice
            if isinstance(tc, dict):
                tc_type = tc.get("type", "auto")
                if tc_type == "any":
                    openai_tool_choice = "required"
                elif tc_type == "tool":
                    openai_tool_choice = {"type": "function", "function": {"name": tc.get("name", "")}}
                elif tc_type == "none":
                    openai_tool_choice = "none"
                else:
                    openai_tool_choice = "auto"

        # Prepend system prompt as a system message (arena extracts it in _assemble_context_message)
        arena_messages = list(body.messages)
        if body.system:
            sys_text = body.system
            if isinstance(sys_text, list):
                # Anthropic content-block array → extract text
                sys_text = "\n".join(
                    b.get("text", "") for b in sys_text
                    if isinstance(b, dict) and b.get("text")
                )
            if sys_text:
                arena_messages.insert(0, {"role": "system", "content": sys_text})

        try:
            result = await adapter.raw_request(
                arena_messages,
                tools=openai_tools,
                tool_choice=openai_tool_choice,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except Exception as exc:
            logger.exception("[compat/messages/arena] Unexpected error")
            raise HTTPException(status_code=500, detail=str(exc))

        # Convert Chat Completions response → Anthropic Messages format
        choice = (result.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        content_blocks = []
        text_content = msg.get("content") or ""
        if text_content:
            content_blocks.append({"type": "text", "text": text_content})
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            try:
                inp = json.loads(fn.get("arguments", "{}"))
            except Exception:
                inp = {}
            content_blocks.append({
                "type": "tool_use",
                "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                "name": fn.get("name", ""),
                "input": inp,
            })
        if not content_blocks:
            content_blocks.append({"type": "text", "text": ""})

        stop_reason = "end_turn"
        if msg.get("tool_calls"):
            stop_reason = "tool_use"

        anthropic_result = {
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "content": content_blocks,
            "model": clean_model,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

        if body.stream:
            _persist_compat_conversation(
                request,
                provider=provider,
                model=resolved_model,
                endpoint="/v1/messages",
                incoming_messages=body.messages,
                response_obj=anthropic_result,
            )
            return StreamingResponse(
                _anthropic_sse_from_response(anthropic_result),
                media_type="text/event-stream",
            )
        _persist_compat_conversation(
            request,
            provider=provider,
            model=resolved_model,
            endpoint="/v1/messages",
            incoming_messages=body.messages,
            response_obj=anthropic_result,
        )
        return anthropic_result

    # Non-Claude/non-Arena providers: text-only path
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
            _anthropic_sse(msg_id, clean_model, text),
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
        "model": clean_model,
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

        elif block_type == "thinking":
            yield evt("content_block_start", {
                "type": "content_block_start",
                "index": i,
                "content_block": {"type": "thinking", "thinking": ""},
            })
            thinking_text = block.get("thinking", "")
            for j in range(0, len(thinking_text), chunk_size):
                yield evt("content_block_delta", {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "thinking_delta", "thinking": thinking_text[j:j + chunk_size]},
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
# OpenAI ↔ Anthropic format converters (used when Claude is called via /v1/chat/completions)
# ---------------------------------------------------------------------------

def _openai_to_anthropic_messages(messages: list[dict]) -> tuple[list[dict], str | None]:
    """Convert OpenAI chat messages to Anthropic messages format.
    Returns (anthropic_messages, system_prompt).
    """
    system = None
    anthropic = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")

        if role == "system":
            # Collect all system messages into a single system prompt
            system = (system + "\n" + (content or "")) if system else (content or "")
            continue

        if role == "tool":
            # OpenAI tool result → Anthropic tool_result block in a user message
            anthropic.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content or "",
                }],
            })
            continue

        if role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                # Convert tool_calls to Anthropic tool_use blocks
                blocks = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    try:
                        inp = json.loads(fn.get("arguments", "{}"))
                    except Exception:
                        inp = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                        # Alias matches what we sent in the tool definition —
                        # otherwise the model won't recognise prior turns.
                        "name": _alias_tool_name(fn.get("name", "")),
                        "input": inp,
                    })
                anthropic.append({"role": "assistant", "content": blocks})
                continue
            # Plain assistant message
            anthropic.append({"role": "assistant", "content": content or ""})
            continue

        # user message — may be string or content-block array
        if isinstance(content, list):
            # Pass through content blocks (e.g. image blocks from vision requests)
            anthropic.append({"role": "user", "content": content})
        else:
            anthropic.append({"role": "user", "content": content or ""})

    return anthropic, system


def _resolve_json_schema(schema: dict, defs: dict) -> dict:
    """
    Recursively resolve $ref references and strip sibling fields that Anthropic rejects.
    Anthropic requires: if $ref is set, only description/default may accompany it.
    We inline $ref so there are no references left in the final schema.
    """
    if not isinstance(schema, dict):
        return schema

    # Merge $defs from nested schemas into the shared defs pool
    local_defs = {**defs, **schema.get("$defs", {}), **schema.get("definitions", {})}

    if "$ref" in schema:
        ref = schema["$ref"]
        # Resolve "#/$defs/Foo" or "#/definitions/Foo"
        ref_name = ref.split("/")[-1]
        resolved = local_defs.get(ref_name, {})
        resolved = _resolve_json_schema(resolved, local_defs)
        # Merge in allowed sibling fields (description, default) from the ref node
        for key in ("description", "default"):
            if key in schema:
                resolved = {**resolved, key: schema[key]}
        return resolved

    result = {}
    for key, value in schema.items():
        if key in ("$defs", "definitions", "$schema"):
            continue  # strip — not needed after resolution
        if isinstance(value, dict):
            result[key] = _resolve_json_schema(value, local_defs)
        elif isinstance(value, list):
            result[key] = [
                _resolve_json_schema(item, local_defs) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            result[key] = value
    return result


def _openai_to_anthropic_tools(tools: list[dict]) -> list[dict]:
    """Convert OpenAI tool definitions to Anthropic tool definitions.
    Resolves $ref references so Anthropic doesn't reject the schema.
    Aliases tool names that collide with Claude Code first-party tool names
    (see _CLAUDE_OAUTH_RESERVED_TOOL_NAMES for background).
    """
    result = []
    for t in tools:
        if t.get("type") == "function":
            fn = t.get("function", {})
            raw_schema = fn.get("parameters", {"type": "object", "properties": {}})
            # Collect top-level $defs / definitions for resolution
            top_defs = {**raw_schema.get("$defs", {}), **raw_schema.get("definitions", {})}
            clean_schema = _resolve_json_schema(raw_schema, top_defs)
            result.append({
                "name": _alias_tool_name(fn.get("name", "")),
                "description": fn.get("description", ""),
                "input_schema": clean_schema,
            })
    return result


def _openai_to_anthropic_tool_choice(tool_choice) -> dict | None:
    """Convert OpenAI tool_choice to Anthropic tool_choice."""
    if not tool_choice:
        return None
    if isinstance(tool_choice, str):
        if tool_choice == "none":
            return {"type": "none"}
        if tool_choice == "required":
            return {"type": "any"}
        return {"type": "auto"}
    if isinstance(tool_choice, dict):
        tc_type = tool_choice.get("type")
        if tc_type == "function":
            name = (tool_choice.get("function") or {}).get("name", "")
            return {"type": "tool", "name": name}
        if tc_type == "none":
            return {"type": "none"}
        if tc_type == "required":
            return {"type": "any"}
    return {"type": "auto"}


def _anthropic_to_openai_response(response: dict, model: str) -> dict:
    """Convert an Anthropic Messages API response to OpenAI Chat Completions format."""
    content_blocks = response.get("content", [])
    stop_reason = response.get("stop_reason", "end_turn")
    usage = response.get("usage", {})

    text_parts = []
    tool_calls = []

    for block in content_blocks:
        btype = block.get("type")
        if btype == "thinking":
            continue  # thinking blocks are internal reasoning; omit from OpenAI format
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                "type": "function",
                "function": {
                    # Translate alias back to the name the client originally used
                    "name": _unalias_tool_name(block.get("name", "")),
                    "arguments": json.dumps(block.get("input", {})),
                },
            })

    message: dict = {"role": "assistant", "content": "\n".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
        message["content"] = None

    finish_reason = "tool_calls" if tool_calls else ("stop" if stop_reason == "end_turn" else stop_reason)

    return {
        "id": response.get("id", f"chatcmpl-{uuid.uuid4().hex[:24]}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }


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
    thinking_effort: str | None = None


@compat_router.post("/v1/chat/completions")
async def openai_chat_completions(body: OpenAIRequest, request: Request):
    api_key = _check_auth(request)

    # ── Thinking effort: parse model suffix, resolve priority ──
    from backend.thinking import parse_model_think_suffix, resolve_effort
    clean_model, suffix_effort = parse_model_think_suffix(body.model)
    effort = resolve_effort(body.thinking_effort, suffix_effort)

    adapter, provider = _adapter_for_request(api_key, clean_model)
    resolved_model = getattr(adapter, "_model", clean_model)

    # Claude via /v1/chat/completions — convert OpenAI format ↔ Anthropic format so
    # agentic clients (OpenCode, Cursor, Continue.dev) get full tool support.
    if provider == "claude" and hasattr(adapter, "raw_request"):
        anthropic_messages, system = _openai_to_anthropic_messages(body.messages)
        # Only attach tools/tool_choice when the client actually sent tools.
        # Plain conversation requests must not include them — Claude errors if
        # tool_choice is set without a tools array.
        anthropic_tools = _openai_to_anthropic_tools(body.tools) if body.tools else None
        anthropic_tool_choice = (
            _openai_to_anthropic_tool_choice(body.tool_choice)
            if (body.tool_choice and anthropic_tools)
            else None
        )
        try:
            result = await adapter.raw_request(
                anthropic_messages,
                max_tokens=body.max_tokens or 8096,
                system=system,
                tools=anthropic_tools,
                tool_choice=anthropic_tool_choice,
                thinking_effort=effort,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except Exception as exc:
            logger.exception("[compat/completions/claude] Unexpected error")
            raise HTTPException(status_code=500, detail=str(exc))

        openai_result = _anthropic_to_openai_response(result, clean_model)
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
                _openai_sse_from_response(openai_result),
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
        return openai_result

    # ChatGPT + Gemini + Arena: pass the full request through — supports tools,
    # tool_choice, and multi-turn tool_result messages. Returns Chat Completions format.
    if provider in ("chatgpt", "gemini", "arena") and hasattr(adapter, "raw_request"):
        try:
            result = await adapter.raw_request(
                body.messages,
                tools=body.tools,
                tool_choice=body.tool_choice,
                thinking_effort=effort,
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

    # Qwen: OpenAI-compatible — real SSE streaming via chat.qwen.ai
    if provider == "qwen" and hasattr(adapter, "stream_chat"):
        if body.stream:
            async def _qwen_stream():
                try:
                    async for chunk in adapter.stream_chat(
                        body.messages,
                        tools=body.tools,
                        temperature=body.temperature,
                        max_tokens=body.max_tokens,
                    ):
                        yield f"data: {json.dumps(chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                except Exception as exc:
                    logger.exception("[compat/completions/qwen] Stream error")
                    err = {"error": {"message": str(exc), "type": "server_error"}}
                    yield f"data: {json.dumps(err)}\n\n"
            return StreamingResponse(_qwen_stream(), media_type="text/event-stream")

        # Non-streaming: use regular call
        try:
            result = await adapter._call_api(
                body.messages,
                tools=body.tools,
                temperature=body.temperature,
                max_tokens=body.max_tokens,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
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
            _openai_sse(completion_id, clean_model, created, text),
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
        "model": clean_model,
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
                "available_models": [
                    "gemini-3-flash-preview",       # overage-eligible (G1 Pro)
                    "gemini-3.1-pro-preview",       # overage-eligible (G1 Pro)
                    "gemini-3-pro-preview",          # overage-eligible (G1 Pro)
                    "gemini-2.5-flash-lite",         # base-tier fallback
                ],
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
            {"id": "gemini-3.1-pro-preview",  "object": "model", "owned_by": "google"},
            {"id": "gemini-3-pro-preview",    "object": "model", "owned_by": "google"},
            {"id": "gemini-2.5-flash-lite",   "object": "model", "owned_by": "google"},
        ]

    # Append -think-low/-think-med/-think-high variants for supported models
    from backend.thinking import provider_supports_thinking
    owner_to_provider = {"anthropic": "claude", "openai": "chatgpt", "google": "gemini"}
    base_models = list(models)
    for m in base_models:
        prov = owner_to_provider.get(m.get("owned_by", ""), "")
        if prov and provider_supports_thinking(prov, m["id"]):
            for suffix, label in [("-think-low", " (Think Low)"), ("-think-med", " (Think Med)"), ("-think-high", " (Think High)")]:
                models.append({
                    "id": m["id"] + suffix,
                    "object": "model",
                    "owned_by": m.get("owned_by", ""),
                    "display_name": m.get("display_name", m["id"]) + label,
                    "note": f"thinking: {suffix.split('-')[-1]}",
                })

    return {"object": "list", "data": models}
