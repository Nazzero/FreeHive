"""
Backend client for talking to the local Arena native host bridge.
"""

from __future__ import annotations

import asyncio
import errno
import json
import uuid
from dataclasses import dataclass
from typing import Any, AsyncGenerator

from backend.services.arena_bridge_transport import (
    get_bridge_socket_path,
    get_bridge_tcp_host,
    get_bridge_tcp_port,
    get_bridge_transport,
)
from shared.arena_bridge_protocol import (
    BridgeErrorCode,
    DEFAULT_JOB_TIMEOUT_MS,
    MAX_JOB_TIMEOUT_MS,
    ArenaBridgeError,
    ArenaBridgeJob,
    ArenaBridgeResult,
    build_run_job_message,
)


@dataclass(slots=True)
class ArenaBridgeSendResult:
    job_id: str
    text: str
    conversation_id: str | None
    effective_model: str | None
    raw_event_count: int


class ArenaBridgeClientError(RuntimeError):
    def __init__(self, message: str, *, code: str = BridgeErrorCode.INTERNAL_ERROR.value):
        super().__init__(message)
        self.code = code


class ArenaBridgeClient:
    def __init__(
        self,
        *,
        transport: str | None = None,
        socket_path: str | None = None,
        tcp_host: str | None = None,
        tcp_port: int | None = None,
    ):
        self.transport = (transport or get_bridge_transport()).lower()
        self.socket_path = socket_path or get_bridge_socket_path()
        self.tcp_host = tcp_host or get_bridge_tcp_host()
        self.tcp_port = int(tcp_port if tcp_port is not None else get_bridge_tcp_port())
        self.endpoint = (
            f"tcp://{self.tcp_host}:{self.tcp_port}"
            if self.transport == "tcp"
            else f"unix://{self.socket_path}"
        )

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self.transport == "tcp":
            return await asyncio.open_connection(self.tcp_host, self.tcp_port)
        return await asyncio.open_unix_connection(self.socket_path)

    async def send_chat(
        self,
        model: str,
        message: str,
        conversation_id: str | None = None,
        *,
        session_id: str = "default",
        timeout_ms: int = DEFAULT_JOB_TIMEOUT_MS,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        Backward-compatible streaming API used by ArenaBridgeAdapter.

        Yields event dictionaries with these types:
        - JOB_UPDATE   {"chunk": "..."}
        - JOB_COMPLETE {"full_text": "...", "conversation_id": "..."}
        - JOB_ERROR    {"error_code": "...", "message": "..."}
        """

        timeout_ms = max(1_000, min(int(timeout_ms), MAX_JOB_TIMEOUT_MS))
        job = ArenaBridgeJob(
            job_id=f"job_{uuid.uuid4().hex}",
            session_id=session_id,
            model=model,
            message=message,
            conversation_id=conversation_id,
            timeout_ms=timeout_ms,
            metadata=metadata or {},
        )

        try:
            reader, writer = await self._connect()
        except (FileNotFoundError, ConnectionRefusedError):
            yield {
                "type": "JOB_ERROR",
                "job_id": job.job_id,
                "error_code": BridgeErrorCode.HOST_UNREACHABLE.value,
                "message": (
                    "Arena Bridge is offline. Ensure Chrome is open with the FreeHive extension, "
                    "and native host is installed and running."
                ),
            }
            return
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ECONNREFUSED, errno.ECONNRESET, errno.EPERM}:
                yield {
                    "type": "JOB_ERROR",
                    "job_id": job.job_id,
                    "error_code": BridgeErrorCode.HOST_UNREACHABLE.value,
                    "message": (
                        "Arena Bridge is offline. Ensure Chrome is open with the FreeHive extension, "
                        "and native host is installed and running."
                    ),
                }
                return
            yield {
                "type": "JOB_ERROR",
                "job_id": job.job_id,
                "error_code": BridgeErrorCode.TRANSPORT_ERROR.value,
                "message": f"Failed to connect to Arena Bridge endpoint ({self.endpoint}): {exc}",
            }
            return
        except Exception as exc:
            yield {
                "type": "JOB_ERROR",
                "job_id": job.job_id,
                "error_code": BridgeErrorCode.TRANSPORT_ERROR.value,
                "message": f"Failed to connect to Arena Bridge endpoint ({self.endpoint}): {exc}",
            }
            return

        try:
            writer.write((json.dumps(build_run_job_message(job), ensure_ascii=True) + "\n").encode("utf-8"))
            await writer.drain()

            timeout_s = max(1.0, timeout_ms / 1000.0)
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=timeout_s)
                except asyncio.TimeoutError:
                    yield {
                        "type": "JOB_ERROR",
                        "job_id": job.job_id,
                        "error_code": BridgeErrorCode.JOB_TIMEOUT.value,
                        "message": f"Arena bridge timed out after {timeout_ms}ms",
                    }
                    return

                if not line:
                    yield {
                        "type": "JOB_ERROR",
                        "job_id": job.job_id,
                        "error_code": BridgeErrorCode.TRANSPORT_ERROR.value,
                        "message": "Arena bridge closed connection before completion",
                    }
                    return

                try:
                    event = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue

                mapped = self._map_host_event(event, expected_job_id=job.job_id)
                if mapped is None:
                    continue

                yield mapped
                if mapped.get("type") in {"JOB_COMPLETE", "JOB_ERROR"}:
                    return
        finally:
            writer.close()
            await writer.wait_closed()

    async def send_message(
        self,
        *,
        session_id: str,
        model: str,
        message: str,
        conversation_id: str | None = None,
        timeout_ms: int = DEFAULT_JOB_TIMEOUT_MS,
        metadata: dict[str, Any] | None = None,
    ) -> ArenaBridgeSendResult:
        """
        Structured non-streaming helper for newer call sites.
        """
        full_text = ""
        effective_model = None
        final_conversation_id = conversation_id
        raw_event_count = 0

        async for update in self.send_chat(
            model=model,
            message=message,
            conversation_id=conversation_id,
            session_id=session_id,
            timeout_ms=timeout_ms,
            metadata=metadata,
        ):
            update_type = update.get("type")
            if update_type == "JOB_UPDATE":
                full_text += update.get("chunk", "")
                raw_event_count += 1
            elif update_type == "JOB_COMPLETE":
                full_text = update.get("full_text", full_text)
                final_conversation_id = update.get("conversation_id", final_conversation_id)
                effective_model = update.get("effective_model")
            elif update_type == "JOB_ERROR":
                raise ArenaBridgeClientError(
                    update.get("message", "Arena bridge job failed"),
                    code=update.get("error_code", BridgeErrorCode.INTERNAL_ERROR.value),
                )

        if not full_text.strip():
            raise ArenaBridgeClientError("Arena bridge returned empty response text")

        return ArenaBridgeSendResult(
            job_id=f"job_result_{uuid.uuid4().hex}",
            text=full_text,
            conversation_id=final_conversation_id,
            effective_model=effective_model,
            raw_event_count=raw_event_count,
        )

    def _map_host_event(self, event: dict[str, Any], *, expected_job_id: str) -> dict[str, Any] | None:
        event_type = str(event.get("type", "")).strip().lower()
        job_id = str(event.get("job_id", "")).strip() or expected_job_id
        if job_id != expected_job_id:
            return None

        if event_type in {"job_started"}:
            return None

        if event_type in {"stream_event"}:
            payload = event.get("event") if isinstance(event.get("event"), dict) else {}
            chunk = payload.get("chunk")
            if not isinstance(chunk, str):
                chunk = ""
            return {
                "type": "JOB_UPDATE",
                "job_id": job_id,
                "chunk": chunk,
            }

        if event_type in {"job_complete"}:
            result_payload = event.get("result") if isinstance(event.get("result"), dict) else {}
            result = ArenaBridgeResult.from_payload(result_payload)
            mapped: dict[str, Any] = {
                "type": "JOB_COMPLETE",
                "job_id": job_id,
                "full_text": result.text,
                "conversation_id": result.conversation_id,
                "effective_model": result.effective_model,
                "raw_event_count": result.raw_event_count,
                "metadata": result.metadata,
            }
            if result.tool_calls:
                mapped["tool_calls"] = result.tool_calls
            return mapped

        if event_type in {"job_failed", "job_error"}:
            error_payload = event.get("error") if isinstance(event.get("error"), dict) else {}
            bridge_error = ArenaBridgeError.from_payload(error_payload)
            details = bridge_error.details if isinstance(bridge_error.details, dict) else {}
            detail_preview = ""
            if details:
                try:
                    detail_preview = json.dumps(details, ensure_ascii=True)[:2000]
                except Exception:
                    detail_preview = str(details)[:2000]
            full_message = bridge_error.message
            if detail_preview:
                full_message = f"{bridge_error.message} | details={detail_preview}"
            return {
                "type": "JOB_ERROR",
                "job_id": job_id,
                "error_code": bridge_error.code,
                "message": full_message,
                "retryable": bridge_error.retryable,
                "details": details,
            }

        return None

    async def fetch_models(self) -> list[str]:
        """
        Ask the extension/page bridge for the currently available Arena models.
        """
        async for update in self.send_chat(
            model="gpt-5.2-chat-latest",
            message="__FETCH_MODELS__",
            session_id="arena-models",
            timeout_ms=20_000,
            metadata={"operation": "fetch_models"},
        ):
            if update.get("type") == "JOB_ERROR":
                raise ArenaBridgeClientError(
                    update.get("message", "Failed to fetch models from Arena bridge"),
                    code=update.get("error_code", BridgeErrorCode.INTERNAL_ERROR.value),
                )
            if update.get("type") == "JOB_COMPLETE":
                metadata = update.get("metadata") if isinstance(update.get("metadata"), dict) else {}
                models = metadata.get("models")
                if isinstance(models, list):
                    cleaned = [str(m).strip() for m in models if str(m).strip()]
                    if cleaned:
                        return cleaned
                break
        return []
