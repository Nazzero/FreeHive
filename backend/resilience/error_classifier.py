"""
error_classifier.py — FreeHive Resilience

Classifies raw HTTP/WebSocket errors into semantic categories so the adapter
cascade knows whether to retry, refresh, advance, or give up.
"""

import json
import logging
import re
from enum import Enum

logger = logging.getLogger(__name__)


class ErrorCategory(str, Enum):
    AUTH_EXPIRED = "auth_expired"           # Token expired, refresh might fix
    AUTH_REVOKED = "auth_revoked"           # Client ID or OAuth app revoked
    BLOCKED_FINGERPRINT = "blocked_fingerprint"  # Identity check failed
    RATE_LIMITED = "rate_limited"           # 429, transient
    QUOTA_EXHAUSTED = "quota_exhausted"     # Daily/monthly quota gone
    FORMAT_CHANGED = "format_changed"       # Response/request shape changed
    ENDPOINT_GONE = "endpoint_gone"         # 404, DNS fail on known host
    CONTENT_BLOCKED = "content_blocked"     # Content filter / scrub-map miss
    CONNECTION_LOST = "connection_lost"     # WebSocket/TCP dropped
    UNKNOWN = "unknown"


class ClassifiedError:
    __slots__ = ("category", "message", "retry_after", "original")

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        retry_after: float | None = None,
        original: Exception | None = None,
    ):
        self.category = category
        self.message = message
        self.retry_after = retry_after
        self.original = original

    def __repr__(self):
        return f"ClassifiedError({self.category.value}, {self.message!r})"

    @property
    def should_cascade(self) -> bool:
        """True if this error means current strategy is dead — advance to next."""
        return self.category in {
            ErrorCategory.AUTH_REVOKED,
            ErrorCategory.BLOCKED_FINGERPRINT,
            ErrorCategory.ENDPOINT_GONE,
            ErrorCategory.FORMAT_CHANGED,
        }

    @property
    def is_terminal(self) -> bool:
        """True if no fallback can help — quota exhausted across all strategies."""
        return self.category == ErrorCategory.QUOTA_EXHAUSTED

    @property
    def is_retryable(self) -> bool:
        """True if same strategy should be retried (with delay)."""
        return self.category in {
            ErrorCategory.AUTH_EXPIRED,
            ErrorCategory.RATE_LIMITED,
            ErrorCategory.CONTENT_BLOCKED,
            ErrorCategory.CONNECTION_LOST,
        }


# ---------------------------------------------------------------------------
# Provider-specific classifiers
# ---------------------------------------------------------------------------

def classify_claude_error(
    status_code: int,
    body: str,
    headers: dict | None = None,
) -> ClassifiedError:
    """Classify an Anthropic API error response."""
    body_lower = body.lower() if body else ""

    if status_code == 401:
        return ClassifiedError(ErrorCategory.AUTH_EXPIRED, "Claude OAuth token expired")

    if status_code == 403:
        return ClassifiedError(ErrorCategory.AUTH_REVOKED, "Claude OAuth access revoked")

    if status_code == 429:
        retry_after = _parse_retry_after(headers)
        return ClassifiedError(
            ErrorCategory.RATE_LIMITED,
            f"Claude rate limited: {body[:200]}",
            retry_after=retry_after,
        )

    if status_code == 400:
        # Anthropic's misleading "out of extra usage" = identity/content check
        if "out of extra usage" in body_lower or "extra usage" in body_lower:
            # Could be fingerprint block OR content block — disambiguate
            return ClassifiedError(
                ErrorCategory.BLOCKED_FINGERPRINT,
                "Claude identity check failed — request doesn't look like CLI",
            )
        if "beta" in body_lower and "not yet available" in body_lower:
            return ClassifiedError(
                ErrorCategory.FORMAT_CHANGED,
                "Claude beta header rejected — API format may have changed",
            )
        if "tool" in body_lower and ("reserved" in body_lower or "collision" in body_lower):
            return ClassifiedError(
                ErrorCategory.CONTENT_BLOCKED,
                f"Claude tool name blocked: {body[:200]}",
            )

    if status_code == 404:
        return ClassifiedError(ErrorCategory.ENDPOINT_GONE, "Claude endpoint not found")

    if status_code >= 500:
        return ClassifiedError(ErrorCategory.RATE_LIMITED, f"Claude server error {status_code}", retry_after=5.0)

    return ClassifiedError(ErrorCategory.UNKNOWN, f"Claude error {status_code}: {body[:200]}")


def classify_chatgpt_error(
    event_type: str = "",
    error_data: dict | None = None,
    status_code: int | None = None,
    body: str = "",
    is_ws_close: bool = False,
) -> ClassifiedError:
    """Classify a ChatGPT WebSocket or HTTP error."""
    if is_ws_close:
        return ClassifiedError(ErrorCategory.CONNECTION_LOST, "ChatGPT WebSocket closed")

    error = error_data or {}
    code = error.get("code", "") if isinstance(error, dict) else ""
    msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
    status = status_code or error.get("status_code") or error.get("status")

    if code == "websocket_connection_limit_reached":
        return ClassifiedError(ErrorCategory.RATE_LIMITED, "ChatGPT connection limit", retry_after=2.0)

    if status == 401 or code == "invalid_api_key":
        return ClassifiedError(ErrorCategory.AUTH_EXPIRED, "ChatGPT token expired")

    if status == 403:
        return ClassifiedError(ErrorCategory.AUTH_REVOKED, "ChatGPT access revoked")

    if status == 429:
        return ClassifiedError(ErrorCategory.RATE_LIMITED, f"ChatGPT rate limited: {msg}", retry_after=10.0)

    if status == 404:
        return ClassifiedError(ErrorCategory.ENDPOINT_GONE, "ChatGPT WebSocket endpoint gone")

    if event_type == "response.failed":
        return ClassifiedError(ErrorCategory.UNKNOWN, f"ChatGPT response failed: {msg}")

    if event_type == "error":
        if "originator" in msg.lower() or "unauthorized" in msg.lower():
            return ClassifiedError(ErrorCategory.BLOCKED_FINGERPRINT, f"ChatGPT originator rejected: {msg}")
        return ClassifiedError(ErrorCategory.UNKNOWN, f"ChatGPT error: {msg}")

    return ClassifiedError(ErrorCategory.UNKNOWN, f"ChatGPT unknown: {msg or body[:200]}")


def classify_gemini_error(
    status_code: int,
    body: str,
    headers: dict | None = None,
) -> ClassifiedError:
    """Classify a Gemini Code Assist API error response."""
    body_lower = body.lower() if body else ""

    if status_code == 401:
        return ClassifiedError(ErrorCategory.AUTH_EXPIRED, "Gemini OAuth token expired")

    if status_code == 403:
        if "client_id" in body_lower or "oauth" in body_lower or "app" in body_lower:
            return ClassifiedError(ErrorCategory.AUTH_REVOKED, "Gemini OAuth app revoked")
        return ClassifiedError(ErrorCategory.AUTH_REVOKED, "Gemini access forbidden")

    if status_code == 429:
        # Parse structured error for terminal vs transient
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        details = error.get("details", []) if isinstance(error, dict) else []

        is_terminal = False
        retry_delay = _parse_retry_after(headers)

        for detail in (details if isinstance(details, list) else []):
            if not isinstance(detail, dict):
                continue
            dtype = detail.get("@type", "")
            if "ErrorInfo" in dtype:
                reason = detail.get("reason", "")
                if reason in ("QUOTA_EXHAUSTED", "INSUFFICIENT_G1_CREDITS_BALANCE"):
                    is_terminal = True
                metadata = detail.get("metadata", {})
                if isinstance(metadata, dict):
                    ql = metadata.get("quota_limit", "")
                    if "PerDay" in ql or "Daily" in ql:
                        is_terminal = True
            elif "RetryInfo" in dtype:
                rd = detail.get("retryDelay", "")
                parsed = _parse_duration(rd)
                if parsed:
                    retry_delay = parsed

        if is_terminal:
            return ClassifiedError(ErrorCategory.QUOTA_EXHAUSTED, f"Gemini quota exhausted: {body[:200]}")

        return ClassifiedError(
            ErrorCategory.RATE_LIMITED,
            f"Gemini rate limited: {body[:200]}",
            retry_after=retry_delay or 10.0,
        )

    if status_code == 404:
        return ClassifiedError(ErrorCategory.ENDPOINT_GONE, "Gemini Code Assist endpoint not found")

    if status_code >= 500:
        return ClassifiedError(ErrorCategory.RATE_LIMITED, f"Gemini server error {status_code}", retry_after=5.0)

    return ClassifiedError(ErrorCategory.UNKNOWN, f"Gemini error {status_code}: {body[:200]}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_retry_after(headers: dict | None) -> float | None:
    if not headers:
        return None
    val = headers.get("retry-after") or headers.get("Retry-After")
    if not val:
        return None
    try:
        return max(1.0, float(val))
    except (TypeError, ValueError):
        return None


def _parse_duration(duration: str) -> float | None:
    if not duration:
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
