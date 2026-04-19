"""
arena_bridge_adapter.py — FreeHive v0.7.1
Arena adapter that talks to the Extension Bridge via Native Messaging.
Supports tool calling and OpenAI Chat Completions format via raw_request().
"""

import hashlib
import logging
import asyncio
import json
import random
import re
import time
import uuid
from typing import Optional, List, Dict, Any
from backend.services.arena_bridge_client import ArenaBridgeClient
from backend.services.arena_model_health import ArenaModelHealthStore
from backend.services.arena_bridge_transport import is_bridge_available

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool Translation Layer
#
# Arena.ai models (GPT-5.2, Claude, Gemini, etc.) understand tool calling
# but arena.ai's wrapper API only accepts a text message — no tools field.
# These functions inject tool definitions into the prompt text and parse
# structured tool calls from the model's response.
# ---------------------------------------------------------------------------

_TOOL_CALL_PATTERN = re.compile(
    r'<tool_call>\s*(.*?)\s*</tool_call>',
    re.DOTALL,
)

_CODE_BLOCK_PATTERN = re.compile(r'```.*?```', re.DOTALL)

_MAX_CONTEXT_CHARS = 80_000


def _extract_text_content(content) -> str:
    """Extract plain text from string or content-block array."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content") or ""
                if isinstance(text, str) and text.strip():
                    parts.append(text)
        return "\n".join(parts)
    return str(content or "")


def _format_param_schema(properties: dict, required: list | None) -> str:
    """Convert JSON Schema properties to readable parameter descriptions."""
    if not properties:
        return "  (none)"
    required = required or []
    lines = []
    for name, schema in properties.items():
        ptype = schema.get("type", "any")
        desc = schema.get("description", "")
        req = "required" if name in required else "optional"
        enum_vals = schema.get("enum")
        line = f"  - {name} ({ptype}, {req})"
        if desc:
            line += f": {desc}"
        if enum_vals:
            line += f" [values: {', '.join(str(v) for v in enum_vals)}]"
        lines.append(line)
    return "\n".join(lines)


def _format_tool_definitions(tools: list[dict]) -> str:
    """Convert OpenAI tools array to a text prompt describing available tools."""
    header = """# Available Tools

You have access to the following tools. When you need to use a tool, include a tool_call block in your response using this exact XML format:

<tool_call>
{"name": "tool_name", "arguments": {"param1": "value1"}}
</tool_call>

Rules:
- The content inside <tool_call> must be valid JSON with "name" and "arguments" keys.
- "arguments" must be a JSON object matching the tool's parameter schema.
- You may include multiple <tool_call> blocks if you need to call multiple tools.
- If you do NOT need to call any tool, respond with plain text only — no <tool_call> tags.
- Do not include any text inside <tool_call> tags other than the JSON object.

## Tools

"""
    tool_sections = []
    for t in tools[:20]:
        if t.get("type") != "function":
            continue
        fn = t.get("function", {})
        name = fn.get("name", "unknown")
        desc = fn.get("description", "")
        params = fn.get("parameters", {})
        props = params.get("properties", {})
        req = params.get("required", [])

        section = f"### {name}"
        if desc:
            section += f"\n{desc}"
        section += f"\nParameters:\n{_format_param_schema(props, req)}"
        tool_sections.append(section)

    return header + "\n\n".join(tool_sections)


def _format_assistant_tool_calls(tool_calls: list[dict]) -> str:
    """Format previous assistant tool calls as text for context."""
    lines = []
    for tc in tool_calls:
        fn = tc.get("function", {})
        name = fn.get("name", "unknown")
        args = fn.get("arguments", "{}")
        lines.append(f'<tool_call>\n{{"name": "{name}", "arguments": {args}}}\n</tool_call>')
    return "\n".join(lines)


def _assemble_context_message(
    messages: list[dict],
    tools: list[dict] | None,
    tool_choice: object | None = None,
) -> str:
    """Collapse all messages + tool definitions into a single text string."""
    parts: list[str] = []

    # 1. System messages
    for msg in messages:
        if msg.get("role") == "system":
            parts.append(f"[System]\n{_extract_text_content(msg.get('content', ''))}")

    # 2. Tool definitions
    if tools and tool_choice != "none":
        tool_text = _format_tool_definitions(tools)
        if tool_choice == "required":
            tool_text += "\n\nIMPORTANT: You MUST call at least one tool in your response."
        elif isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
            fn_name = (tool_choice.get("function") or {}).get("name", "")
            if fn_name:
                tool_text += f"\n\nIMPORTANT: You MUST call the tool named '{fn_name}' in your response."
        parts.append(tool_text)

    # 3. Conversation history (non-system, in order)
    for msg in messages:
        role = msg.get("role", "user")
        if role == "system":
            continue

        if role == "user":
            parts.append(f"[User]\n{_extract_text_content(msg.get('content', ''))}")
        elif role == "assistant":
            tc = msg.get("tool_calls")
            content = _extract_text_content(msg.get("content", ""))
            if tc:
                tc_text = _format_assistant_tool_calls(tc)
                if content.strip():
                    parts.append(f"[Assistant]\n{content}\n{tc_text}")
                else:
                    parts.append(f"[Assistant]\n{tc_text}")
            elif content.strip():
                parts.append(f"[Assistant]\n{content}")
        elif role == "tool":
            tool_call_id = msg.get("tool_call_id", "unknown")
            content = msg.get("content", "")
            parts.append(f"[Tool Result (call_id: {tool_call_id})]\n{content}")

    assembled = "\n\n".join(parts)

    # Truncate old context if too large
    if len(assembled) > _MAX_CONTEXT_CHARS:
        first_break = assembled.find("[User]")
        if first_break > 0:
            header = assembled[:first_break]
            conversation = assembled[first_break:]
            max_conv = _MAX_CONTEXT_CHARS - len(header) - 100
            if len(conversation) > max_conv:
                conversation = "...(earlier turns truncated)...\n\n" + conversation[-max_conv:]
            assembled = header + conversation

    return assembled


def _assemble_incremental_message(new_messages: list[dict]) -> str:
    """Assemble only NEW messages for subsequent turns.

    When arena.ai already has conversation context via conversation_id,
    we only need to send messages added since the last turn. This avoids
    re-sending the system prompt, tool definitions, and full history.
    """
    parts: list[str] = []
    for msg in new_messages:
        role = msg.get("role", "user")
        if role == "system":
            continue  # system already in arena.ai context

        if role == "user":
            parts.append(f"[User]\n{_extract_text_content(msg.get('content', ''))}")
        elif role == "assistant":
            tc = msg.get("tool_calls")
            content = _extract_text_content(msg.get("content", ""))
            if tc:
                tc_text = _format_assistant_tool_calls(tc)
                if content.strip():
                    parts.append(f"[Assistant]\n{content}\n{tc_text}")
                else:
                    parts.append(f"[Assistant]\n{tc_text}")
            elif content.strip():
                parts.append(f"[Assistant]\n{content}")
        elif role == "tool":
            tool_call_id = msg.get("tool_call_id", "unknown")
            content = msg.get("content", "")
            parts.append(f"[Tool Result (call_id: {tool_call_id})]\n{content}")

    return "\n\n".join(parts)


def _compute_tool_hash(tools: list[dict] | None) -> str:
    """Stable hash of tool definitions for change detection."""
    if not tools:
        return ""
    return hashlib.md5(
        json.dumps(tools, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()


def _parse_tool_calls_from_text(text: str) -> tuple[list[dict], str]:
    """Extract <tool_call> blocks from model response text.

    Returns (tool_calls_list, cleaned_text_without_tool_blocks).
    """
    # Mask code blocks so we don't match <tool_call> inside ```...```
    masked = _CODE_BLOCK_PATTERN.sub('', text)

    matches = list(_TOOL_CALL_PATTERN.finditer(masked))
    if not matches:
        return [], text

    tool_calls = []
    for match in matches:
        raw_json = match.group(1).strip()
        parsed = None
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError:
            json_match = re.search(r'\{.*\}', raw_json, re.DOTALL)
            if json_match:
                try:
                    parsed = json.loads(json_match.group(0))
                except json.JSONDecodeError:
                    logger.warning("[ArenaBridge] Malformed tool_call JSON, skipping: %s", raw_json[:200])
                    continue
            else:
                logger.warning("[ArenaBridge] No JSON in tool_call block, skipping: %s", raw_json[:200])
                continue

        if not parsed:
            continue

        name = parsed.get("name", "")
        if not name:
            logger.warning("[ArenaBridge] tool_call missing 'name', skipping")
            continue

        arguments = parsed.get("arguments", {})
        if isinstance(arguments, dict):
            arguments_str = json.dumps(arguments)
        elif isinstance(arguments, str):
            arguments_str = arguments
        else:
            arguments_str = json.dumps(arguments)

        tool_calls.append({
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": arguments_str,
            },
        })

    # Strip tool_call blocks from original text
    cleaned = _TOOL_CALL_PATTERN.sub('', text).strip()
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)

    return tool_calls, cleaned


class ArenaBridgeAdapter:
    # Adaptive rate limiting: starts at 1.5s gap (fast for CLI agents),
    # backs off to 10s on 429, recovers after 60s of no errors.
    _base_gap_s: float = 1.5       # Default gap — fast enough for CLI tool loops
    _current_gap_s: float = 1.5    # Adapts based on 429 responses
    _max_gap_s: float = 10.0       # Max backoff after repeated 429s
    _last_request_time: float = 0.0
    _last_429_time: float = 0.0    # When we last saw a 429
    _consecutive_429s: int = 0     # Track consecutive 429 errors

    def __init__(self):
        self._client = ArenaBridgeClient()
        self._history: Dict[str, List[Dict[str, str]]] = {} # session_id -> history
        self._conversation_ids: Dict[str, Optional[str]] = {} # session_id -> conversation_id
        self._last_sent_count: Dict[str, int] = {}           # session_id -> non-system msgs already sent
        self._tool_hash: Dict[str, str] = {}                 # session_id -> hash of tool definitions
        self._invalid_models: set[str] = set()
        self._health = ArenaModelHealthStore()

    @classmethod
    def _on_429(cls):
        """Called when arena.ai returns 429. Increases gap."""
        cls._consecutive_429s += 1
        cls._last_429_time = time.time()
        old = cls._current_gap_s
        cls._current_gap_s = min(cls._current_gap_s * 1.5, cls._max_gap_s)
        logger.warning(
            "[ArenaBridge] 429 received (consecutive: %d). Rate limit gap: %.1fs → %.1fs",
            cls._consecutive_429s, old, cls._current_gap_s,
        )

    @classmethod
    def _on_success(cls):
        """Called on successful response. Gradually recovers gap."""
        cls._consecutive_429s = 0
        # If no 429 in last 60s, recover toward base gap
        if time.time() - cls._last_429_time > 60:
            if cls._current_gap_s > cls._base_gap_s:
                old = cls._current_gap_s
                cls._current_gap_s = max(cls._current_gap_s * 0.7, cls._base_gap_s)
                logger.info("[ArenaBridge] Rate limit recovering: %.1fs → %.1fs", old, cls._current_gap_s)

    async def _wait_for_rate_limit(self):
        """Enforce adaptive gap between requests."""
        now = time.time()
        elapsed = now - ArenaBridgeAdapter._last_request_time
        gap = ArenaBridgeAdapter._current_gap_s
        if elapsed < gap:
            wait = gap - elapsed + random.uniform(0.1, 0.5)
            logger.info(f"[ArenaBridge] Rate limit spacing: waiting {wait:.1f}s (gap={gap:.1f}s)")
            await asyncio.sleep(wait)
        ArenaBridgeAdapter._last_request_time = time.time()

    async def send_message(self, message: str, model: str = None, session_id: str = "default") -> str:
        """Sends a message via the bridge and returns the full response text."""
        await self._wait_for_rate_limit()
        # Default model from _model attr (set by compat_router)
        if not model:
            model = getattr(self, "_model", "gpt-5.2-chat-latest")
            model = model.removeprefix("arena/").removeprefix("arena-")
        logger.info(f"[ArenaBridge] Sending message for model '{model}' in session '{session_id}'")
        blocked_reason = self._health.get_block_reason(model)
        if blocked_reason:
            raise RuntimeError(blocked_reason)
        attempts = 3
        full_text = ""
        error_info = None
        for attempt in range(1, attempts + 1):
            full_text = ""
            error_info = None
            current_conv_id = self._conversation_ids.get(session_id)

            async for update in self._client.send_chat(
                model=model,
                message=message,
                conversation_id=current_conv_id,
                session_id=session_id,
            ):
                if update["type"] == "JOB_UPDATE":
                    chunk = update.get("chunk", "")
                    full_text += chunk

                elif update["type"] == "JOB_COMPLETE":
                    full_text = update.get("full_text", full_text)
                    self._conversation_ids[session_id] = update.get("conversation_id")
                    logger.info(f"[ArenaBridge] Job complete. Conv ID: {self._conversation_ids[session_id]}")

                elif update["type"] == "JOB_ERROR":
                    error_info = update
                    break

            if error_info:
                # Adaptive rate limit: signal 429 to increase gap
                _err_details = error_info.get("details") if isinstance(error_info.get("details"), dict) else {}
                _err_status = int(_err_details.get("status")) if str(_err_details.get("status", "")).isdigit() else None
                if _err_status == 429:
                    ArenaBridgeAdapter._on_429()
                if attempt < attempts and self._should_retry_error(error_info):
                    delay_s = self._retry_delay_seconds(error_info, attempt)
                    logger.warning(
                        "[ArenaBridge] transient error, retrying (%s/%s) in %.1fs: %s",
                        attempt,
                        attempts,
                        delay_s,
                        error_info.get("message", "unknown"),
                    )
                    await asyncio.sleep(delay_s)
                    continue
                err_msg = error_info.get("message", "Unknown error in Arena Bridge")
                err_code = error_info.get("error_code", "503")
                details = error_info.get("details") if isinstance(error_info.get("details"), dict) else {}
                status = int(details.get("status")) if str(details.get("status", "")).isdigit() else None
                diagnostics = details.get("diagnostics") if isinstance(details.get("diagnostics"), dict) else {}
                self._health.mark_error(
                    model,
                    status_code=status,
                    message=err_msg,
                    diagnostics=diagnostics,
                )
                err_msg_l = err_msg.lower()
                if status == 404 and "model not found" in err_msg_l:
                    self._mark_model_invalid(model)
                    self._conversation_ids[session_id] = None
                    err_msg = (
                        f"Arena model '{model}' is unavailable right now (Model not found). "
                        "Refresh models and choose another model."
                    )
                elif status == 422 and (
                    "not permitted to handle this type of question" in err_msg_l
                    or "please choose another model" in err_msg_l
                ):
                    self._mark_model_invalid(model)
                    self._conversation_ids[session_id] = None
                    err_msg = (
                        f"Arena model '{model}' cannot be used for conversational chat right now. "
                        "Refresh models and choose another model."
                    )
                elif status == 400 and "private models" in err_msg_l:
                    self._mark_model_invalid(model)
                    self._conversation_ids[session_id] = None
                    err_msg = (
                        f"Arena model '{model}' is private/battle-only and cannot be used in Direct mode. "
                        "Refresh models and choose another model."
                    )
                logger.error(f"[ArenaBridge] Job error: {err_msg} (code: {err_code})")
                raise RuntimeError(f"{err_msg}")

            if full_text.strip():
                break

        if not full_text.strip():
            raise RuntimeError("Arena Bridge returned an empty response.")
        self._health.mark_success(model)
        ArenaBridgeAdapter._on_success()

        if session_id not in self._history:
            self._history[session_id] = []
        self._history[session_id].append({"role": "user", "content": message})
        self._history[session_id].append({"role": "assistant", "content": full_text})
        
        return full_text

    def _should_retry_error(self, error_info: Dict[str, Any]) -> bool:
        message = str(error_info.get("message", "") or "")
        message_l = message.lower()
        error_code = str(error_info.get("error_code", "") or "").lower()
        details = error_info.get("details") if isinstance(error_info.get("details"), dict) else {}
        status = int(details.get("status")) if str(details.get("status", "")).isdigit() else None
        diagnostics = details.get("diagnostics") if isinstance(details.get("diagnostics"), dict) else {}
        retry_after_raw = str(diagnostics.get("retry_after_header", "") or "").strip()
        retry_after = self._parse_retry_after_seconds(retry_after_raw)

        if error_code == "job_timeout":
            return True
        if status == 429:
            if retry_after is not None and retry_after >= 120:
                return False
            return True
        if status in {500, 502, 503, 504}:
            return True
        if "prompt failed" in message_l:
            return True
        if "recaptcha validation failed" in message_l:
            return True
        return bool(error_info.get("retryable"))

    def _retry_delay_seconds(self, error_info: Dict[str, Any], attempt: int) -> float:
        details = error_info.get("details") if isinstance(error_info.get("details"), dict) else {}
        status = int(details.get("status")) if str(details.get("status", "")).isdigit() else None
        diagnostics = details.get("diagnostics") if isinstance(details.get("diagnostics"), dict) else {}
        retry_after_raw = str(diagnostics.get("retry_after_header", "") or "").strip()

        if status == 429:
            retry_after = self._parse_retry_after_seconds(retry_after_raw)
            if retry_after is not None:
                return min(max(float(retry_after), 1.0), 15.0)
            return min(2.0 + (attempt - 1) * 2.0 + random.uniform(0.2, 1.0), 12.0)

        if status in {500, 502, 503, 504}:
            return min(1.0 + attempt * 1.3 + random.uniform(0.1, 0.8), 8.0)

        message_l = str(error_info.get("message", "") or "").lower()
        if "timeout" in message_l:
            return min(1.5 + attempt * 1.2 + random.uniform(0.2, 0.8), 8.0)
        if "recaptcha validation failed" in message_l:
            return min(1.2 + attempt * 1.1 + random.uniform(0.1, 0.7), 8.0)

        return 1.5

    def _parse_retry_after_seconds(self, raw: str) -> float | None:
        text = str(raw or "").strip()
        if not text:
            return None
        if text.isdigit():
            return float(text)
        return None

    async def fetch_models(self) -> list[str]:
        """
        Fetch live model names from the extension/page bridge.
        The extension hits arena.ai's own API to get the Direct mode model list —
        no word-based filtering needed, this IS the authoritative list.
        Returns values normalized as `arena/<name>`.
        """
        models = await self._client.fetch_models()
        normalized: list[str] = []
        seen = set()
        for model in models:
            name = str(model).strip()
            if not name:
                continue
            model_id = name if name.startswith("arena/") else f"arena/{name}"
            if self._is_model_invalid(model_id):
                continue
            if model_id not in seen:
                seen.add(model_id)
                normalized.append(model_id)
        return self._health.filter_and_rank(normalized, unknown_cap=None)

    def _mark_model_invalid(self, model: str) -> None:
        key = str(model or "").strip().lower()
        if not key:
            return
        if key.startswith("arena/"):
            bare = key[len("arena/"):]
        else:
            bare = key
        self._invalid_models.add(key)
        self._invalid_models.add(f"arena/{bare}")
        self._invalid_models.add(bare)

    def _is_model_invalid(self, model: str) -> bool:
        key = str(model or "").strip().lower()
        if not key:
            return False
        if key.startswith("arena/"):
            bare = key[len("arena/"):]
        else:
            bare = key
        return key in self._invalid_models or bare in self._invalid_models or f"arena/{bare}" in self._invalid_models

    # Known provider prefixes for chat/text models
    _KNOWN_CHAT_PROVIDERS = [
        "gpt-", "o1-", "o3-", "o4-", "claude-", "gemini-", "gemma-",
        "llama-", "meta-llama", "mistral-", "mixtral-", "qwen", "qwq-",
        "deepseek", "phi-", "grok-", "glm-", "yi-", "command-",
        "jamba-", "reka-", "dbrx", "falcon-", "vicuna-",
        "wizardlm", "solar-", "orca-", "internlm", "baichuan",
        "chatglm", "nemotron", "athene", "olmo-", "ernie-",
        "kimi-", "minimax-", "hailuo-", "step-", "devstral",
        "intellect-", "ppl-sonar",
    ]

    # Known image/video/audio generation model prefixes — always reject
    _MEDIA_PREFIXES = [
        "flux-", "kling-", "wan-", "wan2", "runway-", "pika-", "mochi-",
        "kandinsky-", "recraft-", "reve-", "seed-1", "seedance-", "seededit-",
        "seedream-", "ideogram-", "ltx-", "pixverse-", "veo-", "hidream-",
        "mimo-v2",
    ]

    def _is_likely_chat_model(self, model: str) -> bool:
        text = str(model or "").strip().lower()
        if not text:
            return False
        bare = text[len("arena/"):] if text.startswith("arena/") else text

        # Reject very short names (battle-mode codenames)
        if len(bare) < 5:
            return False

        # Reject anonymous models
        if bare.startswith("anonymous"):
            return False

        # Reject known media generation models
        if any(bare.startswith(p) for p in self._MEDIA_PREFIXES):
            return False

        # Reject modality keywords
        blocked_words = [
            "image", "vision", "video", "audio", "speech", "transcrib",
            "tts", "asr", "embedding", "rerank", "ocr", "diffusion",
            "sdxl", "midjourney", "dall-e", "paint", "whisper",
            "stable-diffusion", "sora", "t2v", "i2v", "t2i", "i2i",
            "kontext",
        ]
        allow_override = ["chat", "instruct", "text", "reason", "code"]
        if any(word in bare for word in blocked_words):
            if not any(word in bare for word in allow_override):
                return False

        # Reject parenthesized names unless known provider
        if "(" in bare:
            if not any(bare.startswith(p) for p in self._KNOWN_CHAT_PROVIDERS):
                return False

        # Accept known chat providers
        if any(bare.startswith(p) for p in self._KNOWN_CHAT_PROVIDERS):
            return True

        # Reject everything else — if it's not a known provider, it's likely
        # a battle-mode codename (alligator, blue-forge, queen-bee, etc.)
        return False

    async def raw_request(
        self,
        messages: List[Dict[str, Any]],
        session_id: str = "default",
        tools: list[dict] | None = None,
        tool_choice: object | None = None,
        **kwargs,
    ) -> dict:
        """
        Returns OpenAI Chat Completions format with tool_calls support.
        Used by compat_router.py for /v1/chat/completions with arena/* models.

        Uses incremental context: on subsequent turns with a valid conversation_id,
        only sends NEW messages (arena.ai maintains server-side history). This
        reduces per-turn context from ~80K chars to ~1-2K chars on long sessions.
        """
        await self._wait_for_rate_limit()
        model = getattr(self, "_model", None) or kwargs.get("model", "gpt-5.2-chat-latest")
        bare_model = model.removeprefix("arena/").removeprefix("arena-")

        blocked_reason = self._health.get_block_reason(bare_model)
        if blocked_reason:
            raise RuntimeError(blocked_reason)

        current_conv_id = self._conversation_ids.get(session_id)

        # --- Incremental context detection ---
        non_system = [m for m in messages if m.get("role") != "system"]
        prev_count = self._last_sent_count.get(session_id, 0)
        tool_hash = _compute_tool_hash(tools)
        prev_tool_hash = self._tool_hash.get(session_id)
        tools_changed = prev_tool_hash is not None and prev_tool_hash != tool_hash

        can_increment = (
            current_conv_id is not None
            and prev_count > 0
            and len(non_system) > prev_count
            and not tools_changed
        )

        if can_increment:
            new_messages = non_system[prev_count:]
            assembled_message = _assemble_incremental_message(new_messages)
            logger.info(
                "[ArenaBridge] Incremental context: %d chars (%d new msgs). "
                "Full would be ~%d msgs.",
                len(assembled_message), len(new_messages), len(non_system),
            )
        else:
            assembled_message = _assemble_context_message(messages, tools, tool_choice)
            if tools_changed and current_conv_id:
                # Tools changed mid-conversation — start fresh so model sees new defs
                logger.info("[ArenaBridge] Tools changed, resetting conversation for fresh context")
                current_conv_id = None

        if not assembled_message.strip():
            raise RuntimeError("No message content found in messages")

        # Update tracking state
        self._last_sent_count[session_id] = len(non_system)
        self._tool_hash[session_id] = tool_hash

        if tools and tool_choice != "none":
            logger.info(
                "[ArenaBridge] Tool-aware request: %d tools, model '%s', incremental=%s",
                len([t for t in tools if t.get("type") == "function"]),
                bare_model, can_increment,
            )

        # --- Send to bridge ---
        full_text, native_tool_calls, error_info = await self._send_to_bridge(
            bare_model, assembled_message, current_conv_id, session_id,
        )

        # --- Fallback: if incremental failed, retry with full context ---
        if error_info and can_increment:
            logger.warning(
                "[ArenaBridge] Incremental request failed, retrying with full context: %s",
                error_info.get("message", "unknown"),
            )
            self._last_sent_count.pop(session_id, None)
            self._tool_hash.pop(session_id, None)
            self._conversation_ids[session_id] = None

            assembled_message = _assemble_context_message(messages, tools, tool_choice)
            self._last_sent_count[session_id] = len(non_system)
            self._tool_hash[session_id] = tool_hash

            full_text, native_tool_calls, error_info = await self._send_to_bridge(
                bare_model, assembled_message, None, session_id,
            )

        if error_info:
            _err_details = error_info.get("details") if isinstance(error_info.get("details"), dict) else {}
            _err_status = int(_err_details.get("status")) if str(_err_details.get("status", "")).isdigit() else None
            if _err_status == 429:
                ArenaBridgeAdapter._on_429()
            err_msg = error_info.get("message", "Unknown error in Arena Bridge")
            raise RuntimeError(err_msg)

        self._health.mark_success(bare_model)
        ArenaBridgeAdapter._on_success()

        # Determine tool_calls: prefer native SSE tool_calls, fall back to text parsing
        tool_calls = native_tool_calls
        cleaned_text = full_text

        if tools and tool_choice != "none" and not native_tool_calls:
            parsed_tool_calls, cleaned_text = _parse_tool_calls_from_text(full_text)
            if parsed_tool_calls:
                tool_calls = parsed_tool_calls
                logger.info(
                    "[ArenaBridge] Parsed %d tool_call(s) from text for model '%s'",
                    len(parsed_tool_calls), bare_model,
                )

        # Build OpenAI Chat Completions response
        message: Dict[str, Any] = {"role": "assistant", "content": cleaned_text or None}
        finish_reason = "stop"

        if tool_calls:
            message["tool_calls"] = tool_calls
            if not cleaned_text.strip():
                message["content"] = None
            finish_reason = "tool_calls"

        return {
            "id": f"chatcmpl-arena-{uuid.uuid4().hex[:16]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    async def _send_to_bridge(
        self,
        model: str,
        message: str,
        conversation_id: str | None,
        session_id: str,
    ) -> tuple[str, list | None, dict | None]:
        """Send message to bridge, return (full_text, native_tool_calls, error_info)."""
        full_text = ""
        native_tool_calls = None
        error_info = None

        async for update in self._client.send_chat(
            model=model,
            message=message,
            conversation_id=conversation_id,
            session_id=session_id,
        ):
            if update["type"] == "JOB_UPDATE":
                full_text += update.get("chunk", "")
            elif update["type"] == "JOB_COMPLETE":
                full_text = update.get("full_text", full_text)
                native_tool_calls = update.get("tool_calls")
                self._conversation_ids[session_id] = update.get("conversation_id")
            elif update["type"] == "JOB_ERROR":
                error_info = update
                break

        return full_text, native_tool_calls, error_info

    async def load_history(self, messages: List[Dict[str, str]], session_id: str = "default"):
        self._history[session_id] = [{"role": m["role"], "content": m["content"]} for m in messages]
        logger.info(f"[ArenaBridge] Loaded {len(self._history[session_id])} messages for session {session_id}")

    def clear_history(self, session_id: str = "default"):
        if session_id in self._history:
            self._history[session_id] = []
        if session_id in self._conversation_ids:
            self._conversation_ids[session_id] = None
        self._last_sent_count.pop(session_id, None)
        self._tool_hash.pop(session_id, None)
        logger.info(f"[ArenaBridge] History cleared for session {session_id}")

    async def is_authenticated(self) -> bool:
        return is_bridge_available()
