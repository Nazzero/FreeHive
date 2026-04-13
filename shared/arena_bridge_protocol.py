"""
Shared transport protocol for the Arena extension <-> native host <-> backend bridge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

PROTOCOL_VERSION = "2026-04-08.v1"
NATIVE_HOST_NAME = "com.freehive.arena_bridge"
DEFAULT_BRIDGE_HTTP_URL = "http://127.0.0.1:8765"
DEFAULT_JOB_TIMEOUT_MS = 120_000
MAX_JOB_TIMEOUT_MS = 300_000


class BridgeMessageType(str, Enum):
    HELLO = "hello"
    PING = "ping"
    PONG = "pong"
    RUN_JOB = "run_job"
    JOB_STARTED = "job_started"
    STREAM_EVENT = "stream_event"
    JOB_COMPLETE = "job_complete"
    JOB_FAILED = "job_failed"


class BridgeJobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class BridgeErrorCode(str, Enum):
    HOST_UNREACHABLE = "host_unreachable"
    EXTENSION_OFFLINE = "extension_offline"
    HOST_BAD_RESPONSE = "host_bad_response"
    JOB_NOT_FOUND = "job_not_found"
    JOB_TIMEOUT = "job_timeout"
    PROMPT_FAILED = "prompt_failed"
    MODEL_MISMATCH = "model_mismatch"
    LOGIN_REQUIRED = "login_required"
    TRANSPORT_ERROR = "transport_error"
    INTERNAL_ERROR = "internal_error"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_non_empty_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' must be a non-empty string")
    return value.strip()


def _as_optional_str(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"'{key}' must be a string when present")
    cleaned = value.strip()
    return cleaned or None


def _as_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise ValueError("'metadata' must be an object")
    return dict(metadata)


def _normalize_timeout(timeout_ms: int) -> int:
    timeout = max(1_000, int(timeout_ms))
    return min(timeout, MAX_JOB_TIMEOUT_MS)


@dataclass(slots=True)
class ArenaBridgeJob:
    job_id: str
    session_id: str
    model: str
    message: str
    conversation_id: str | None = None
    timeout_ms: int = DEFAULT_JOB_TIMEOUT_MS
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ArenaBridgeJob":
        timeout_raw = payload.get("timeout_ms", DEFAULT_JOB_TIMEOUT_MS)
        try:
            timeout = _normalize_timeout(int(timeout_raw))
        except (TypeError, ValueError) as exc:
            raise ValueError("'timeout_ms' must be an integer") from exc

        return cls(
            job_id=_as_non_empty_str(payload, "job_id"),
            session_id=_as_non_empty_str(payload, "session_id"),
            model=_as_non_empty_str(payload, "model"),
            message=_as_non_empty_str(payload, "message"),
            conversation_id=_as_optional_str(payload, "conversation_id"),
            timeout_ms=timeout,
            metadata=_as_metadata(payload),
            created_at=str(payload.get("created_at") or utc_now_iso()),
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "session_id": self.session_id,
            "model": self.model,
            "message": self.message,
            "timeout_ms": _normalize_timeout(self.timeout_ms),
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }
        if self.conversation_id:
            payload["conversation_id"] = self.conversation_id
        return payload


@dataclass(slots=True)
class ArenaBridgeError:
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ArenaBridgeError":
        code = payload.get("code") or BridgeErrorCode.INTERNAL_ERROR.value
        message = payload.get("message") or "Unknown bridge error"
        retryable = bool(payload.get("retryable", False))
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        return cls(code=str(code), message=str(message), retryable=retryable, details=dict(details))

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": dict(self.details),
        }


@dataclass(slots=True)
class ArenaBridgeResult:
    text: str
    conversation_id: str | None = None
    effective_model: str | None = None
    raw_event_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ArenaBridgeResult":
        text = payload.get("text")
        if not isinstance(text, str):
            raise ValueError("'text' is required in result payload")
        conversation_id = payload.get("conversation_id")
        if conversation_id is not None and not isinstance(conversation_id, str):
            raise ValueError("'conversation_id' must be a string when present")
        effective_model = payload.get("effective_model")
        if effective_model is not None and not isinstance(effective_model, str):
            raise ValueError("'effective_model' must be a string when present")
        raw_event_count = payload.get("raw_event_count", 0)
        try:
            event_count = max(0, int(raw_event_count))
        except (TypeError, ValueError) as exc:
            raise ValueError("'raw_event_count' must be an integer") from exc
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        return cls(
            text=text,
            conversation_id=conversation_id,
            effective_model=effective_model,
            raw_event_count=event_count,
            metadata=dict(metadata),
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "text": self.text,
            "raw_event_count": self.raw_event_count,
            "metadata": dict(self.metadata),
        }
        if self.conversation_id:
            payload["conversation_id"] = self.conversation_id
        if self.effective_model:
            payload["effective_model"] = self.effective_model
        return payload


def build_run_job_message(job: ArenaBridgeJob) -> dict[str, Any]:
    return {
        "type": BridgeMessageType.RUN_JOB.value,
        "protocol_version": PROTOCOL_VERSION,
        "sent_at": utc_now_iso(),
        "job": job.to_payload(),
    }

