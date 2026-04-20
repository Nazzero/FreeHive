"""
GeminiDirectAdapter — FreeHive v0.5.4

Calls Gemini via the cloudcode-pa.googleapis.com Code Assist endpoint using
OAuth tokens from the Gemini CLI (~/.gemini/oauth_creds.json).

Mimics the exact request shape of the Gemini CLI (v0.38.0) so that the
server treats FreeHive identically to the CLI for quota purposes.

Key parity points vs Gemini CLI:
  - User-Agent:  GeminiCLI/<version>/<model> (<os>; <arch>; terminal)
  - user_prompt_id:  main#<N>  (incrementing counter, NOT random UUID)
  - loadCodeAssist body includes cloudaicompanionProject + duetProject
  - No x-goog-api-client header (CLI doesn't send it for Code Assist)
  - Same session_id reused across all turns
  - Retry with backoff on 429, same model, no model rotation
"""

import asyncio
import json
import logging
import platform
import random
import re
import time
import uuid
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from backend.resilience.cli_introspection import get_gemini_config

logger = logging.getLogger(__name__)

# Credentials written by `gemini auth login`
TOKENS_FILE = Path.home() / ".gemini" / "oauth_creds.json"

# Google OAuth2 refresh endpoint
REFRESH_URL = "https://oauth2.googleapis.com/token"

_PLATFORM = platform.system().lower()
_ARCH = "x64" if platform.machine() in ("x86_64", "AMD64") else platform.machine()


def _get_gemini_cfg():
    """Get dynamically extracted Gemini CLI config."""
    return get_gemini_config()


def _get_endpoints(cfg: dict) -> tuple[str, str]:
    """Derive endpoint URLs from config base."""
    base = cfg["endpoint_base"]
    return (
        f"{base}:streamGenerateContent?alt=sse",
        f"{base}:loadCodeAssist",
    )

DEFAULT_MODEL = "gemini-3-flash-preview"
_EXPIRY_BUFFER_MS = 5 * 60 * 1000

# Code Assist OAuth has tighter rate limits than provider API-key tiers.
# OpenCode may emit parallel tool calls, so cap FreeHive's own in-flight Gemini
# requests to avoid self-inflicted 429 bursts.
_GEMINI_REQUEST_SEM = asyncio.Semaphore(1)

# Mirror Gemini CLI behavior:
# - stable session_id reused across all turns (CLI does this)
# - cached project ID from loadCodeAssist (CLI caches 30s)
# - incrementing turn counter for user_prompt_id (CLI uses main#N)
_SHARED_SESSION_ID = str(uuid.uuid4())
_PROJECT_CACHE_LOCK = asyncio.Lock()
_PROJECT_ID_CACHE: str | None = None
_TURN_COUNTER: int = 0
_TURN_COUNTER_LOCK = asyncio.Lock()

# Retry/backoff defaults based on Gemini CLI's retry utility.
# CLI uses: maxAttempts=10, initialDelayMs=5000, maxDelayMs=30000
_MAX_ATTEMPTS = 10
_INITIAL_DELAY_SECONDS = 5.0
_MAX_DELAY_SECONDS = 30.0
_MAX_RETRYABLE_DELAY_SECONDS = 300.0

# Google One AI Pro overage credits — only these models can draw from paid credits.
# Ref: Gemini CLI OVERAGE_ELIGIBLE_MODELS at chunk-UKTSS4BC.js:251276
_OVERAGE_ELIGIBLE_MODELS = {
    "gemini-3-pro-preview",
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
}
_G1_CREDIT_TYPE = "GOOGLE_ONE_AI"

# Models with tight capacity when processing tool declarations.
# Tool payloads are reduced for these to avoid capacity 429s.
_TOOL_CONSTRAINED_MODELS = {
    "gemini-3.1-pro-preview",
}

# Essential tools to keep for constrained models (OpenCode core tools).
# Order = priority — first N kept, rest dropped.
_ESSENTIAL_TOOL_NAMES = [
    "Read", "read",
    "Edit", "edit",
    "Write", "write",
    "Bash", "bash",
    "Grep", "grep",
    "Glob", "glob",
    "Agent", "agent",
]

# Cached paid-tier detection from loadCodeAssist
_PAID_TIER_CACHE_LOCK = asyncio.Lock()
_PAID_TIER_AVAILABLE: bool | None = None  # None = not yet checked


# ---------------------------------------------------------------------------
# Format converters: Chat Completions ↔ Gemini Code Assist
# ---------------------------------------------------------------------------


def _convert_messages_to_gemini(messages: list[dict]) -> tuple[str | None, list]:
    """
    Convert Chat Completions messages to (system_instruction_text, gemini_contents).

    System messages → system_instruction (Gemini doesn't use a system role in contents).
    tool role messages → functionResponse parts (need function name, recovered from prior
    assistant tool_calls via a call_id→name map built during conversion).
    """
    system_text = None
    contents = []
    call_id_to_name: dict[str, str] = {}

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content")

        if role == "system":
            if content:
                system_text = content if isinstance(content, str) else str(content)
            continue

        if role == "user":
            if isinstance(content, list):
                text = "\n".join(
                    b.get("text", "") for b in content if b.get("type") == "text"
                )
            else:
                text = content or ""
            contents.append({"role": "user", "parts": [{"text": text}]})

        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                parts = []
                if content:
                    parts.append({"text": content})
                for tc in tool_calls:
                    name = tc["function"]["name"]
                    call_id_to_name[tc["id"]] = name
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    fc_part = {"functionCall": {"name": name, "args": args}}
                    # Re-attach Gemini's thoughtSignature for thinking models
                    if "thought_signature" in tc:
                        fc_part["thoughtSignature"] = tc["thought_signature"]
                    parts.append(fc_part)
                contents.append({"role": "model", "parts": parts})
            else:
                contents.append({"role": "model", "parts": [{"text": content or ""}]})

        elif role == "tool":
            call_id = msg.get("tool_call_id", "")
            # Recover function name from the prior assistant tool_calls mapping
            func_name = call_id_to_name.get(call_id, call_id)
            result_content = (
                content if isinstance(content, str) else json.dumps(content)
            )
            try:
                result_data = json.loads(result_content)
            except (json.JSONDecodeError, TypeError):
                result_data = {"result": result_content}
            # Gemini's functionResponse.response proto field expects an object
            # (google.protobuf.Struct). If the parsed tool result is a list,
            # scalar, or None, wrap it so `response` is always a dict — otherwise
            # Gemini rejects with "Proto field is not repeating, cannot start
            # list."
            if not isinstance(result_data, dict):
                result_data = {"result": result_data}
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": func_name,
                                "response": result_data,
                            }
                        }
                    ],
                }
            )

    return system_text, contents


def _strip_schema_metadata(schema) -> dict:
    """Recursively remove fields Gemini rejects in function_declaration parameters.

    Note on `ref` (no `$`): OpenCode's `question` tool ships a stray
    `"ref": "QuestionOption"` inside its nested items schema. Gemini interprets
    any `ref` field as a schema reference, sees it alongside `type`/`properties`,
    and rejects with: "Schema.ref 'QuestionOption' was set alongside unsupported
    fields. If a schema node has Schema.ref set, then only description and
    default can be set alongside it." The `ref` has no corresponding $defs entry,
    so it's just noise from OpenCode's schema generator — safe to strip.
    """
    if not isinstance(schema, dict):
        return schema
    _UNSUPPORTED = {
        "$schema",
        "$defs",
        "definitions",
        "$id",
        "$comment",
        "$ref",
        "ref",
        "additionalProperties",
        "exclusiveMinimum",
        "exclusiveMaximum",
    }
    result = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED:
            continue
        if isinstance(value, dict):
            result[key] = _strip_schema_metadata(value)
        elif isinstance(value, list):
            result[key] = [
                _strip_schema_metadata(v) if isinstance(v, dict) else v for v in value
            ]
        else:
            result[key] = value
    return result


def _reduce_tools_for_model(tools: list[dict], model: str) -> list[dict]:
    """Reduce tool count and schema size for capacity-constrained models.

    Some models (e.g. gemini-3.1-pro-preview) hit capacity 429s when tool
    declarations are included.  This trims the payload two ways:
      1. Keep only essential tools (by name match), cap at 8.
      2. Strip parameter descriptions to shrink schema bytes.
    """
    if model not in _TOOL_CONSTRAINED_MODELS or not tools:
        return tools

    original_count = len(tools)

    # Score each tool: essential tools get priority by position in the list
    def _priority(t: dict) -> int:
        name = t.get("function", {}).get("name", "")
        # Handle namespaced names like "default_api:read"
        short = name.split(":")[-1] if ":" in name else name
        for i, ename in enumerate(_ESSENTIAL_TOOL_NAMES):
            if short.lower() == ename.lower():
                return i
        return 999

    sorted_tools = sorted(tools, key=_priority)
    kept = sorted_tools[:8]

    # Strip descriptions from parameter properties (not the function itself)
    reduced = []
    for t in kept:
        t_copy = json.loads(json.dumps(t))  # deep copy
        props = t_copy.get("function", {}).get("parameters", {}).get("properties", {})
        for prop in props.values():
            _strip_param_descriptions(prop)
        reduced.append(t_copy)

    if len(reduced) < original_count:
        logger.info(
            "[gemini_adapter] Reduced tools for %s: %d → %d",
            model, original_count, len(reduced),
        )
    return reduced


def _strip_param_descriptions(schema: dict) -> None:
    """Recursively remove 'description' from parameter schemas to save bytes."""
    if not isinstance(schema, dict):
        return
    schema.pop("description", None)
    for v in schema.values():
        if isinstance(v, dict):
            _strip_param_descriptions(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    _strip_param_descriptions(item)


def _convert_tools_to_gemini(tools: list[dict]) -> list:
    """Convert Chat Completions tool definitions to Gemini function_declarations format."""
    function_declarations = []
    for t in tools:
        if t.get("type") == "function":
            f = t["function"]
            function_declarations.append(
                {
                    "name": f["name"],
                    "description": f.get("description", ""),
                    "parameters": _strip_schema_metadata(f.get("parameters", {})),
                }
            )
    return (
        [{"function_declarations": function_declarations}]
        if function_declarations
        else []
    )


def _convert_tool_choice_to_gemini(tool_choice, has_tools: bool) -> dict | None:
    """Convert Chat Completions tool_choice to Gemini tool_config."""
    if not has_tools:
        return None
    if tool_choice is None or tool_choice == "auto":
        return {"function_calling_config": {"mode": "AUTO"}}
    if tool_choice == "none":
        return {"function_calling_config": {"mode": "NONE"}}
    if tool_choice == "required":
        return {"function_calling_config": {"mode": "ANY"}}
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        return {
            "function_calling_config": {
                "mode": "ANY",
                "allowed_function_names": [tool_choice["function"]["name"]],
            }
        }
    return {"function_calling_config": {"mode": "AUTO"}}


def _result_to_chat_completions(result: dict, model: str) -> dict:
    """Convert structured Gemini result to Chat Completions response dict."""
    text = result.get("text", "")
    function_calls = result.get("function_calls", [])

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    if function_calls:
        tool_calls = []
        for fc in function_calls:
            tc = {
                "id": fc["id"],
                "type": "function",
                "function": {"name": fc["name"], "arguments": fc["arguments"]},
            }
            # Preserve Gemini's thoughtSignature for round-trip
            if "thought_signature" in fc:
                tc["thought_signature"] = fc["thought_signature"]
            tool_calls.append(tc)
        message = {
            "role": "assistant",
            "content": text or None,
            "tool_calls": tool_calls,
        }
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


def _extract_rate_limit_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        return (
            data.get("error", {}).get("message")
            or data.get("message")
            or "Wait a moment and try again."
        )
    except Exception:
        return "Wait a moment and try again."


def _parse_duration_seconds(duration: str) -> float | None:
    if not duration or not isinstance(duration, str):
        return None
    duration = duration.strip().lower()
    if duration.endswith("ms"):
        try:
            return max(0.001, float(duration[:-2]) / 1000.0)
        except (TypeError, ValueError):
            return None
    if duration.endswith("s"):
        try:
            return max(0.001, float(duration[:-1]))
        except (TypeError, ValueError):
            return None
    return None


def _parse_retry_after_header(resp: httpx.Response) -> float | None:
    retry_after = resp.headers.get("retry-after")
    if not retry_after:
        return None

    # Numeric seconds
    try:
        return max(0.001, float(retry_after))
    except (TypeError, ValueError):
        pass

    # HTTP-date
    try:
        target = parsedate_to_datetime(retry_after)
        now = time.time()
        return max(0.001, target.timestamp() - now)
    except Exception:
        return None


def _parse_delay_from_message(message: str) -> float | None:
    """Extract an explicit wait duration from the error message body.

    Code Assist 429 bodies typically say things like:
      "Your quota will reset after 47s."
      "Please retry in 30s"
    This is the most accurate delay available — prefer it over generic defaults.
    """
    if not message:
        return None
    patterns = [
        r"reset after\s+([0-9.]+)\s*s",
        r"after\s+([0-9.]+)\s*s",
        r"please retry in\s+([0-9.]+(?:ms|s))",
    ]
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if not match:
            continue
        value = match.group(1)
        if value.endswith(("ms", "s")):
            parsed = _parse_duration_seconds(value)
        else:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                parsed = None
        if parsed is not None:
            return max(0.001, parsed)
    return None


def _extract_retry_delay_from_error_payload(
    resp: httpx.Response, message: str
) -> tuple[float | None, bool]:
    is_terminal = False

    try:
        payload = resp.json()
    except Exception:
        payload = {}

    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    details = error.get("details", []) if isinstance(error, dict) else []

    # --- Phase 1: parse the error message for an explicit delay. ---
    # Code Assist's 429 body almost always contains "reset after Ns" which
    # is the most accurate delay. This MUST take priority over the generic
    # 10s fallback from RATE_LIMIT_EXCEEDED which is far too short.
    message_delay = _parse_delay_from_message(message)

    # --- Phase 2: inspect structured error details for RetryInfo / terminal signals. ---
    structured_delay: float | None = None
    has_rate_limit_reason = False

    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            dtype = detail.get("@type", "")
            if dtype == "type.googleapis.com/google.rpc.RetryInfo":
                parsed = _parse_duration_seconds(str(detail.get("retryDelay", "")))
                if parsed is not None:
                    structured_delay = max(structured_delay or 0.0, parsed)
            elif dtype == "type.googleapis.com/google.rpc.QuotaFailure":
                for violation in detail.get("violations", []) or []:
                    if not isinstance(violation, dict):
                        continue
                    quota_id = str(violation.get("quotaId", ""))
                    if "PerMinute" in quota_id and structured_delay is None:
                        structured_delay = 60.0
                    if "PerDay" in quota_id or "Daily" in quota_id:
                        is_terminal = True
            elif dtype == "type.googleapis.com/google.rpc.ErrorInfo":
                reason = str(detail.get("reason", ""))
                metadata = (
                    detail.get("metadata", {})
                    if isinstance(detail.get("metadata"), dict)
                    else {}
                )
                quota_limit = str(metadata.get("quota_limit", ""))
                if reason in {"QUOTA_EXHAUSTED", "INSUFFICIENT_G1_CREDITS_BALANCE"}:
                    is_terminal = True
                if reason == "RATE_LIMIT_EXCEEDED":
                    has_rate_limit_reason = True
                if "PerDay" in quota_limit or "Daily" in quota_limit:
                    is_terminal = True
                if "PerMinute" in quota_limit and structured_delay is None:
                    structured_delay = 60.0

    # --- Phase 3: pick the best delay. ---
    # Priority: structured RetryInfo > message body > generic RATE_LIMIT_EXCEEDED fallback
    if structured_delay is not None:
        delay_seconds = structured_delay
    elif message_delay is not None:
        delay_seconds = message_delay
    elif has_rate_limit_reason:
        # Generic fallback — no explicit duration anywhere.  Use 10s as a
        # floor but this should rarely be hit for Code Assist errors.
        delay_seconds = 10.0
    else:
        delay_seconds = None

    return delay_seconds, is_terminal


def _compute_retry_delay_seconds(
    resp: httpx.Response, message: str, attempt: int
) -> float | None:
    header_delay = _parse_retry_after_header(resp)
    payload_delay, is_terminal = _extract_retry_delay_from_error_payload(resp, message)

    # Terminal quota conditions should not be retried.
    if is_terminal:
        return None
    if payload_delay is not None:
        delay_seconds = max(payload_delay, header_delay or 0.0)
        if delay_seconds > _MAX_RETRYABLE_DELAY_SECONDS:
            return None
        # Gemini CLI adds jitter when retrying quota errors.
        return delay_seconds + (delay_seconds * 0.2 * random.random())

    if header_delay is not None:
        if header_delay > _MAX_RETRYABLE_DELAY_SECONDS:
            return None
        return header_delay

    # Exponential fallback with bounded delay and jitter.
    base = min(_MAX_DELAY_SECONDS, _INITIAL_DELAY_SECONDS * (2**attempt))
    jitter = base * 0.3 * (random.random() * 2 - 1)
    return max(0.001, base + jitter)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class GeminiDirectAdapter:
    """
    Sends messages to Gemini via Google's internal Code Assist API,
    authenticated with the Gemini CLI's OAuth tokens.

    Maintains conversation history per session (in-memory).
    Project ID is fetched once from loadCodeAssist and cached.
    """

    def __init__(self, model: str | None = None):
        self.conversation_history: list[dict] = []  # Google format: [{role, parts}]
        self._project_id: str | None = None
        self._session_id = _SHARED_SESSION_ID
        self._model = model or DEFAULT_MODEL
        self._paid_tier_available: bool = False
        self._cfg = _get_gemini_cfg()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def load_history(self, history: list[dict]):
        """Rebuild in-memory history from DB messages."""
        self.conversation_history = [
            {
                "role": "model" if m["role"] == "assistant" else "user",
                "parts": [{"text": m["content"]}],
            }
            for m in history
        ]

    async def send_message(self, message: str, history: list[dict] = None) -> str:
        if not self.conversation_history and history:
            self.load_history(history)

        self.conversation_history.append({"role": "user", "parts": [{"text": message}]})

        token = await self._get_token()
        project = await self._get_project(token)
        result = await self._call_api(
            token, project, contents=self.conversation_history
        )

        text = result["text"]
        self.conversation_history.append({"role": "model", "parts": [{"text": text}]})
        return text

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
        from backend.thinking import gemini_thinking_config
        system_text, contents = _convert_messages_to_gemini(messages)
        reduced_tools = _reduce_tools_for_model(tools, self._model) if tools else None
        gemini_tools = _convert_tools_to_gemini(reduced_tools) if reduced_tools else []
        gemini_tool_config = _convert_tool_choice_to_gemini(
            tool_choice, has_tools=bool(gemini_tools)
        )

        token = await self._get_token()
        project = await self._get_project(token)

        result = await self._call_api(
            token,
            project,
            contents=contents,
            tools=gemini_tools or None,
            tool_config=gemini_tool_config,
            system_instruction=system_text,
            thinking_config=gemini_thinking_config(thinking_effort),
        )
        return _result_to_chat_completions(result, self._model)

    def clear_history(self):
        self.conversation_history = []

    def is_authenticated(self) -> bool:
        return TOKENS_FILE.exists()

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _read_tokens(self) -> dict:
        if not TOKENS_FILE.exists():
            raise RuntimeError("Gemini not authenticated. Run: gemini auth login")
        try:
            return json.loads(TOKENS_FILE.read_text())
        except Exception as exc:
            raise RuntimeError(f"Gemini tokens file corrupted: {exc}") from exc

    def _is_expired(self, tokens: dict) -> bool:
        expiry_ms = tokens.get("expiry_date", 0)
        return (time.time() * 1000 + _EXPIRY_BUFFER_MS) >= expiry_ms

    async def _refresh_token(self, refresh_token: str) -> str:
        cfg = self._cfg
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                REFRESH_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": cfg["client_id"],
                    "client_secret": cfg["client_secret"],
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Gemini token refresh failed ({resp.status_code}). "
                "Re-authenticate with: gemini auth login"
            )
        data = resp.json()
        access_token = data["access_token"]
        try:
            tokens = json.loads(TOKENS_FILE.read_text())
            tokens["access_token"] = access_token
            tokens["expiry_date"] = (
                int(time.time() * 1000) + data.get("expires_in", 3600) * 1000
            )
            TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
        except Exception:
            pass
        return access_token

    async def _get_token(self) -> str:
        tokens = self._read_tokens()
        if self._is_expired(tokens):
            refresh = tokens.get("refresh_token")
            if not refresh:
                raise RuntimeError(
                    "Gemini token expired and no refresh token found. "
                    "Re-authenticate with: gemini auth login"
                )
            return await self._refresh_token(refresh)
        return tokens["access_token"]

    # ------------------------------------------------------------------
    # Project ID (fetched once from loadCodeAssist)
    # ------------------------------------------------------------------

    async def _detect_paid_tier(self, load_response: dict) -> None:
        """Cache whether the user has Google One AI Pro overage credits."""
        global _PAID_TIER_AVAILABLE
        async with _PAID_TIER_CACHE_LOCK:
            if _PAID_TIER_AVAILABLE is not None:
                self._paid_tier_available = _PAID_TIER_AVAILABLE
                return
            paid_tier = load_response.get("paidTier", {})
            has_paid = paid_tier.get("id") == "g1-pro-tier"
            _PAID_TIER_AVAILABLE = has_paid
            self._paid_tier_available = has_paid
            if has_paid:
                logger.info(
                    "[gemini_adapter] Google One AI Pro tier detected — "
                    "will send enabled_credit_types for eligible models"
                )
            else:
                logger.info(
                    "[gemini_adapter] No paid overage tier detected — "
                    "using base-tier quota only"
                )

    async def _get_project(self, token: str) -> str:
        """Fetch the cloudaicompanionProject via loadCodeAssist.

        Matches the CLI's exact loadCodeAssist body shape:
        {
          "cloudaicompanionProject": "<projectId or undefined>",
          "metadata": {
            "ideType": "IDE_UNSPECIFIED",
            "platform": "PLATFORM_UNSPECIFIED",
            "pluginType": "GEMINI",
            "duetProject": "<projectId or undefined>"
          }
        }
        """
        global _PROJECT_ID_CACHE

        if self._project_id:
            return self._project_id
        if _PROJECT_ID_CACHE:
            self._project_id = _PROJECT_ID_CACHE
            return _PROJECT_ID_CACHE

        async with _PROJECT_CACHE_LOCK:
            if _PROJECT_ID_CACHE:
                self._project_id = _PROJECT_ID_CACHE
                return _PROJECT_ID_CACHE

            # First call: no project yet, send without cloudaicompanionProject
            load_body: dict = {
                "metadata": {
                    "ideType": "IDE_UNSPECIFIED",
                    "platform": "PLATFORM_UNSPECIFIED",
                    "pluginType": "GEMINI",
                }
            }

            _, load_url = _get_endpoints(self._cfg)
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    load_url,
                    headers=self._headers(token),
                    json=load_body,
                )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"loadCodeAssist failed ({resp.status_code}): {resp.text[:200]}"
                )
            data = resp.json()
            project = data.get("cloudaicompanionProject")
            if not project:
                raise RuntimeError(
                    "loadCodeAssist returned no cloudaicompanionProject. "
                    "Free tier may not be available in your region."
                )

            # Second call: CLI always does a follow-up loadCodeAssist with the
            # returned project + duetProject to complete setup/registration.
            setup_body = {
                "cloudaicompanionProject": project,
                "metadata": {
                    "ideType": "IDE_UNSPECIFIED",
                    "platform": "PLATFORM_UNSPECIFIED",
                    "pluginType": "GEMINI",
                    "duetProject": project,
                },
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp2 = await client.post(
                    load_url,
                    headers=self._headers(token),
                    json=setup_body,
                )
            if resp2.status_code == 200:
                data2 = resp2.json()
                # Use the project from the second call (may differ)
                project = data2.get("cloudaicompanionProject", project)
                tier_info = data2.get("currentTier", {})
                if tier_info:
                    logger.info(
                        "[gemini_adapter] Code Assist tier: %s",
                        tier_info.get("id", "unknown"),
                    )
                # Detect Google One AI Pro paid overage tier
                await self._detect_paid_tier(data2)
            else:
                # First call response may also have paidTier
                await self._detect_paid_tier(data)

            _PROJECT_ID_CACHE = project
            self._project_id = project
            return project

    # ------------------------------------------------------------------
    # Core API call
    # ------------------------------------------------------------------

    async def _next_turn_id(self) -> str:
        """Return the next user_prompt_id in CLI format: main#0, main#1, ..."""
        global _TURN_COUNTER
        async with _TURN_COUNTER_LOCK:
            turn = _TURN_COUNTER
            _TURN_COUNTER += 1
        return f"main#{turn}"

    async def _call_api(
        self,
        token: str,
        project: str,
        *,
        contents: list,
        tools: list | None = None,
        tool_config: dict | None = None,
        system_instruction: str | None = None,
        thinking_config: dict | None = None,
    ) -> dict:
        """
        Call the Code Assist streamGenerateContent endpoint.
        Returns {"text": str, "function_calls": [...]} dict.

        Matches Gemini CLI's exact request shape and retry behavior:
        - user_prompt_id: main#N (incrementing)
        - session_id: stable UUID across all turns
        - Retry with exponential backoff on 429 (max 10 attempts)
        - Same model, no rotation
        """
        generation_config: dict = {"maxOutputTokens": 8192}
        if thinking_config:
            generation_config["thinkingConfig"] = thinking_config
        request_body: dict = {
            "contents": contents,
            "generationConfig": generation_config,
            "session_id": self._session_id,
        }
        if tools:
            request_body["tools"] = tools
        if tool_config:
            request_body["tool_config"] = tool_config
        if system_instruction:
            request_body["systemInstruction"] = {
                "parts": [{"text": system_instruction}]
            }

        turn_id = await self._next_turn_id()
        payload = {
            "model": self._model,
            "project": project,
            "user_prompt_id": turn_id,
            "request": request_body,
        }
        # Opt into Google One AI Pro overage credits for eligible models.
        # Without this field, requests hit base-tier quota (~3s recovery on 429).
        # With it, paid-tier quota gives ~200ms recovery.
        if self._paid_tier_available and self._model in _OVERAGE_ELIGIBLE_MODELS:
            payload["enabled_credit_types"] = [_G1_CREDIT_TYPE]

        last_rate_limit_msg = "Wait a moment and try again."

        async with _GEMINI_REQUEST_SEM:
            for attempt in range(_MAX_ATTEMPTS):
                resp = await self._do_request_with_auth_retry(token, payload)

                if resp.status_code == 200:
                    return self._parse_sse(resp.text)

                if resp.status_code == 401:
                    raise RuntimeError(
                        "Gemini session expired. Re-authenticate with: gemini auth login"
                    )

                if resp.status_code == 429:
                    msg = _extract_rate_limit_message(resp)
                    last_rate_limit_msg = msg

                    if attempt >= _MAX_ATTEMPTS - 1:
                        break

                    # "No capacity available" = temporary server congestion.
                    # Use short fixed retry (2s) instead of exponential backoff.
                    if "no capacity available" in msg.lower():
                        delay = 2.0 + (random.random() * 1.0)
                        logger.info(
                            "[gemini_adapter] Capacity unavailable for %s — "
                            "short retry in %.1fs (attempt %s/%s)",
                            self._model, delay, attempt + 1, _MAX_ATTEMPTS,
                        )
                        await asyncio.sleep(delay)
                        continue

                    delay = _compute_retry_delay_seconds(resp, msg, attempt)
                    if delay is None:
                        # Terminal quota (daily exhausted, etc.)
                        raise RuntimeError(f"Gemini rate limited: {msg}")

                    logger.warning(
                        "[gemini_adapter] 429 on %s — retrying in %.1fs "
                        "(attempt %s/%s): %s",
                        self._model,
                        delay,
                        attempt + 1,
                        _MAX_ATTEMPTS,
                        msg[:200],
                    )
                    await asyncio.sleep(delay)
                    continue

                logger.error(
                    "[gemini_adapter] %s error on %s: %s",
                    resp.status_code, self._model, resp.text[:500],
                )
                raise RuntimeError(
                    f"Gemini API error {resp.status_code}: {resp.text[:300]}"
                )

            raise RuntimeError(f"Gemini rate limited: {last_rate_limit_msg}")

    async def _do_request_with_auth_retry(
        self, token: str, payload: dict
    ) -> httpx.Response:
        """Send one request with a single auth-refresh retry on 401."""
        generate_url, _ = _get_endpoints(self._cfg)

        # Anti-fingerprint jitter
        await asyncio.sleep(random.uniform(0.05, 0.2))

        for auth_attempt in range(2):
            if auth_attempt == 1:
                tokens_data = self._read_tokens()
                refresh = tokens_data.get("refresh_token")
                if refresh:
                    token = await self._refresh_token(refresh)
                else:
                    return httpx.Response(status_code=401)

            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    generate_url,
                    headers=self._headers(token),
                    json=payload,
                )

            if resp.status_code == 401 and auth_attempt == 0:
                continue
            return resp

        # Should not reach here, but return last response
        return resp

    def _parse_sse(self, raw: str) -> dict:
        """
        Parse SSE stream, collect text parts and function calls.
        Returns {"text": str, "function_calls": [{"id", "name", "arguments"}, ...]}
        """
        text_parts: list[str] = []
        function_calls: list[dict] = []

        for line in raw.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                chunk = json.loads(line[6:])
                candidates = chunk.get("response", {}).get("candidates", [])
                for candidate in candidates:
                    for part in candidate.get("content", {}).get("parts", []):
                        if "text" in part:
                            text_parts.append(part["text"])
                        elif "functionCall" in part:
                            fc = part["functionCall"]
                            fc_entry = {
                                "id": f"call_{uuid.uuid4().hex[:16]}",
                                "name": fc.get("name", ""),
                                "arguments": json.dumps(fc.get("args", {})),
                            }
                            # Gemini 3.x thinking models attach thoughtSignature
                            # to functionCall parts — must be preserved for
                            # round-trip or subsequent requests get 400.
                            if "thoughtSignature" in part:
                                fc_entry["thought_signature"] = part["thoughtSignature"]
                            function_calls.append(fc_entry)
            except (json.JSONDecodeError, KeyError):
                continue

        if not text_parts and not function_calls:
            raise RuntimeError("Gemini returned empty response.")

        return {"text": "".join(text_parts), "function_calls": function_calls}

    def _headers(self, token: str) -> dict:
        """Build headers matching what Gemini CLI sends.

        The CLI does NOT send x-goog-api-client for Code Assist requests.
        Version dynamically extracted from installed Gemini CLI.
        """
        cfg = self._cfg
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": f"GeminiCLI/{cfg['version']}/{self._model} ({_PLATFORM}; {_ARCH}; terminal)",
        }
