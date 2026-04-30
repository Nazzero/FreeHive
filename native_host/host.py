#!/usr/bin/env python3
"""
Native Messaging Host for FreeHive Arena Bridge.

Bridges:
  Chrome Extension (native messaging stdio) <-> Backend (unix socket or TCP loopback)
"""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Inlined from backend.services.arena_bridge_transport and
# shared.arena_bridge_protocol so host.py is fully self-contained.
# This is required for frozen (PyInstaller) builds where these modules
# live inside the bundle and are NOT accessible to a system Python.
# DO NOT replace with imports — the native host runs as a standalone
# process launched by Chrome, outside of the PyInstaller environment.
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

# -- Transport config (from arena_bridge_transport) --

_DEFAULT_UNIX_SOCKET_PATH = "/tmp/freehive_arena_bridge.sock"
_DEFAULT_TCP_HOST = "127.0.0.1"
_DEFAULT_TCP_PORT = 8766


def _env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < 1 or value > 65535:
        return default
    return value


def get_bridge_transport() -> str:
    raw = str(os.getenv("FREEHIVE_ARENA_BRIDGE_TRANSPORT", "")).strip().lower()
    if raw in {"unix", "tcp"}:
        return raw
    return "tcp" if os.name == "nt" else "unix"


def get_bridge_socket_path() -> str:
    return str(
        os.getenv("FREEHIVE_ARENA_BRIDGE_SOCKET", _DEFAULT_UNIX_SOCKET_PATH)
    ).strip() or _DEFAULT_UNIX_SOCKET_PATH


def get_bridge_tcp_host() -> str:
    return str(os.getenv("FREEHIVE_ARENA_BRIDGE_HOST", _DEFAULT_TCP_HOST)).strip() or _DEFAULT_TCP_HOST


def get_bridge_tcp_port() -> int:
    return _env_int("FREEHIVE_ARENA_BRIDGE_PORT", _DEFAULT_TCP_PORT)


# -- Protocol (from arena_bridge_protocol) --

_PROTOCOL_VERSION = "2026-04-08.v1"
_DEFAULT_JOB_TIMEOUT_MS = 120_000
_MAX_JOB_TIMEOUT_MS = 300_000


class BridgeMessageType(str, Enum):
    HELLO = "hello"
    PING = "ping"
    PONG = "pong"
    RUN_JOB = "run_job"
    JOB_STARTED = "job_started"
    STREAM_EVENT = "stream_event"
    JOB_COMPLETE = "job_complete"
    JOB_FAILED = "job_failed"


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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_timeout(timeout_ms: int) -> int:
    return min(max(1_000, int(timeout_ms)), _MAX_JOB_TIMEOUT_MS)


@dataclass(slots=True)
class ArenaBridgeError:
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": dict(self.details),
        }


@dataclass(slots=True)
class ArenaBridgeJob:
    job_id: str
    session_id: str
    model: str
    message: str
    conversation_id: str | None = None
    timeout_ms: int = _DEFAULT_JOB_TIMEOUT_MS
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now_iso)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ArenaBridgeJob":
        def _req_str(key: str) -> str:
            v = payload.get(key)
            if not isinstance(v, str) or not v.strip():
                raise ValueError(f"'{key}' must be a non-empty string")
            return v.strip()

        timeout_raw = payload.get("timeout_ms", _DEFAULT_JOB_TIMEOUT_MS)
        try:
            timeout = _normalize_timeout(int(timeout_raw))
        except (TypeError, ValueError) as exc:
            raise ValueError("'timeout_ms' must be an integer") from exc

        conv_id = payload.get("conversation_id")
        if conv_id is not None and not isinstance(conv_id, str):
            raise ValueError("'conversation_id' must be a string when present")

        md = payload.get("metadata")
        metadata = dict(md) if isinstance(md, dict) else {}

        return cls(
            job_id=_req_str("job_id"),
            session_id=_req_str("session_id"),
            model=_req_str("model"),
            message=_req_str("message"),
            conversation_id=(conv_id.strip() or None) if conv_id else None,
            timeout_ms=timeout,
            metadata=metadata,
            created_at=str(payload.get("created_at") or _utc_now_iso()),
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


def build_run_job_message(job: ArenaBridgeJob) -> dict[str, Any]:
    return {
        "type": BridgeMessageType.RUN_JOB.value,
        "protocol_version": _PROTOCOL_VERSION,
        "sent_at": _utc_now_iso(),
        "job": job.to_payload(),
    }

LOG_DIR = Path.home() / ".freehive" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = str(LOG_DIR / "freehive_arena_host.log")

BRIDGE_TRANSPORT = get_bridge_transport()
SOCKET_PATH = get_bridge_socket_path()
TCP_HOST = get_bridge_tcp_host()
TCP_PORT = get_bridge_tcp_port()
PING_INTERVAL_S = 20.0
SOCKET_RECV_SIZE = 64 * 1024

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("freehive.arena.host")


@dataclass(slots=True)
class BackendConn:
    conn: socket.socket
    created_at: float


MIN_EXTENSION_VERSION = "1.0.1"


def _version_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return (0,)


state_lock = threading.Lock()
jobs_to_backend: dict[str, BackendConn] = {}
native_write_lock = threading.Lock()
extension_connected = False
extension_version: str | None = None
shutdown_flag = False


def read_native_message() -> dict[str, Any] | None:
    raw_len = sys.stdin.buffer.read(4)
    if not raw_len:
        return None
    if len(raw_len) < 4:
        raise RuntimeError("Truncated native message length")
    msg_len = struct.unpack("<I", raw_len)[0]
    payload = sys.stdin.buffer.read(msg_len)
    if len(payload) < msg_len:
        raise RuntimeError("Truncated native message payload")
    decoded = json.loads(payload.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise RuntimeError("Native message must decode to object")
    return decoded


def send_native_message(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    frame = struct.pack("<I", len(encoded)) + encoded
    with native_write_lock:
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()


def set_extension_connected(value: bool, version: str | None = None) -> None:
    global extension_connected, extension_version
    with state_lock:
        extension_connected = value
        if version is not None:
            extension_version = version
        if not value:
            extension_version = None


def is_extension_connected() -> bool:
    with state_lock:
        return extension_connected


def register_job_conn(job_id: str, conn: socket.socket) -> None:
    with state_lock:
        jobs_to_backend[job_id] = BackendConn(conn=conn, created_at=time.time())


def pop_job_conn(job_id: str) -> BackendConn | None:
    with state_lock:
        return jobs_to_backend.pop(job_id, None)


def get_job_conn(job_id: str) -> BackendConn | None:
    with state_lock:
        return jobs_to_backend.get(job_id)


def fail_backend_conn(conn: socket.socket, *, code: str, message: str, retryable: bool = False) -> None:
    payload = {
        "type": BridgeMessageType.JOB_FAILED.value,
        "error": ArenaBridgeError(code=code, message=message, retryable=retryable).to_payload(),
    }
    try:
        conn.sendall((json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8"))
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def parse_backend_request(raw: str) -> tuple[ArenaBridgeJob | None, str | None]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, "Backend request is not valid JSON"

    if not isinstance(payload, dict):
        return None, "Backend request must be a JSON object"

    if payload.get("type") == BridgeMessageType.RUN_JOB.value and isinstance(payload.get("job"), dict):
        job_payload = payload["job"]
    else:
        # Compatibility: allow direct job payload without wrapper
        job_payload = payload

    try:
        job = ArenaBridgeJob.from_payload(job_payload)
    except ValueError as exc:
        return None, str(exc)

    return job, None


def handle_backend_conn(conn: socket.socket) -> None:
    conn.settimeout(30)
    try:
        buffer = b""
        while True:
            chunk = conn.recv(SOCKET_RECV_SIZE)
            if not chunk:
                if buffer:
                    break
                conn.close()
                return
            buffer += chunk
            if b"\n" in buffer:
                raw, _, _rest = buffer.partition(b"\n")
                req = raw.decode("utf-8", errors="replace").strip()
                break
        else:
            req = ""
    except Exception as exc:
        logger.warning("Failed reading backend socket request: %s", exc)
        try:
            conn.close()
        except Exception:
            pass
        return

    if not req:
        fail_backend_conn(
            conn,
            code=BridgeErrorCode.HOST_BAD_RESPONSE.value,
            message="Empty backend request",
            retryable=False,
        )
        return

    job, error = parse_backend_request(req)
    if job is None:
        fail_backend_conn(
            conn,
            code=BridgeErrorCode.HOST_BAD_RESPONSE.value,
            message=error or "Invalid request",
            retryable=False,
        )
        return

    if not is_extension_connected():
        fail_backend_conn(
            conn,
            code=BridgeErrorCode.EXTENSION_OFFLINE.value,
            message="Chrome extension is not connected to native host",
            retryable=True,
        )
        return

    register_job_conn(job.job_id, conn)
    try:
        send_native_message(build_run_job_message(job))
        logger.info("Forwarded job %s to extension (model=%s)", job.job_id, job.model)
    except Exception as exc:
        logger.exception("Failed to forward job %s to extension: %s", job.job_id, exc)
        mapped = pop_job_conn(job.job_id)
        if mapped:
            fail_backend_conn(
                mapped.conn,
                code=BridgeErrorCode.TRANSPORT_ERROR.value,
                message=f"Failed to send job to extension: {exc}",
                retryable=True,
            )


def backend_listener() -> None:
    server: socket.socket | None = None
    using_unix = BRIDGE_TRANSPORT == "unix"

    try:
        if using_unix:
            if not hasattr(socket, "AF_UNIX"):
                logger.error("Unix socket transport selected but AF_UNIX is not available on this platform")
                return
            if os.path.exists(SOCKET_PATH):
                os.remove(SOCKET_PATH)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(SOCKET_PATH)
            server.listen(32)
            os.chmod(SOCKET_PATH, 0o666)
            logger.info("Backend unix socket listening: %s", SOCKET_PATH)
        else:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((TCP_HOST, TCP_PORT))
            server.listen(32)
            logger.info("Backend TCP listener active: %s:%s", TCP_HOST, TCP_PORT)
    except Exception as exc:
        logger.exception("Failed to start backend listener (%s): %s", BRIDGE_TRANSPORT, exc)
        return

    while not shutdown_flag and server is not None:
        try:
            conn, _ = server.accept()
        except Exception:
            if shutdown_flag:
                break
            continue
        thread = threading.Thread(target=handle_backend_conn, args=(conn,), daemon=True)
        thread.start()

    try:
        if server is not None:
            server.close()
    except Exception:
        pass
    if using_unix and os.path.exists(SOCKET_PATH):
        try:
            os.remove(SOCKET_PATH)
        except Exception:
            pass


def heartbeat_loop() -> None:
    while not shutdown_flag:
        time.sleep(PING_INTERVAL_S)
        if not is_extension_connected():
            continue
        try:
            send_native_message({"type": BridgeMessageType.PING.value})
        except Exception as exc:
            logger.warning("Ping send failed: %s", exc)


def forward_to_backend(message: dict[str, Any]) -> None:
    job_id = str(message.get("job_id", "")).strip()
    if not job_id:
        return

    mapped = get_job_conn(job_id)
    if not mapped:
        return

    conn = mapped.conn
    try:
        conn.sendall((json.dumps(message, ensure_ascii=True) + "\n").encode("utf-8"))
    except Exception as exc:
        logger.warning("Failed forwarding event for job %s: %s", job_id, exc)
        mapped = pop_job_conn(job_id)
        if mapped:
            try:
                mapped.conn.close()
            except Exception:
                pass
        return

    event_type = str(message.get("type", "")).strip().lower()
    if event_type in {BridgeMessageType.JOB_COMPLETE.value, BridgeMessageType.JOB_FAILED.value, "job_error"}:
        mapped = pop_job_conn(job_id)
        if mapped:
            try:
                mapped.conn.close()
            except Exception:
                pass


def close_all_backend_conns(reason: str) -> None:
    with state_lock:
        pending = list(jobs_to_backend.items())
        jobs_to_backend.clear()

    for job_id, backend_conn in pending:
        try:
            backend_conn.conn.sendall(
                (
                    json.dumps(
                        {
                            "type": BridgeMessageType.JOB_FAILED.value,
                            "job_id": job_id,
                            "error": ArenaBridgeError(
                                code=BridgeErrorCode.EXTENSION_OFFLINE.value,
                                message=reason,
                                retryable=True,
                            ).to_payload(),
                        },
                        ensure_ascii=True,
                    )
                    + "\n"
                ).encode("utf-8")
            )
        except Exception:
            pass
        finally:
            try:
                backend_conn.conn.close()
            except Exception:
                pass


def main() -> None:
    global shutdown_flag

    logger.info("FreeHive native host starting")
    threading.Thread(target=backend_listener, daemon=True, name="backend-listener").start()
    threading.Thread(target=heartbeat_loop, daemon=True, name="heartbeat").start()

    try:
        while True:
            msg = read_native_message()
            if msg is None:
                logger.warning("Extension native messaging stream closed")
                set_extension_connected(False)
                close_all_backend_conns("Extension disconnected from native host")
                break

            msg_type = str(msg.get("type", "")).strip().lower()
            if msg_type == BridgeMessageType.HELLO.value:
                ext_ver = msg.get("extension_version", "unknown")
                proto_ver = msg.get("protocol_version", "unknown")
                set_extension_connected(True, version=ext_ver)
                logger.info("Extension connected (v%s, protocol %s)", ext_ver, proto_ver)
                if ext_ver != "unknown" and _version_tuple(ext_ver) < _version_tuple(MIN_EXTENSION_VERSION):
                    logger.warning(
                        "Extension v%s is outdated (minimum: v%s). "
                        "Update from Chrome Web Store for best results.",
                        ext_ver, MIN_EXTENSION_VERSION,
                    )
                continue
            if msg_type == BridgeMessageType.PONG.value:
                continue

            if msg_type in {
                BridgeMessageType.JOB_STARTED.value,
                BridgeMessageType.STREAM_EVENT.value,
                BridgeMessageType.JOB_COMPLETE.value,
                BridgeMessageType.JOB_FAILED.value,
                "job_error",
            }:
                forward_to_backend(msg)
                continue

            logger.info("Ignoring unknown extension message type: %s", msg_type or "<empty>")

    except Exception:
        logger.error("Native host fatal error:\n%s", traceback.format_exc())
    finally:
        shutdown_flag = True
        set_extension_connected(False)
        close_all_backend_conns("Native host shutting down")
        if BRIDGE_TRANSPORT == "unix" and os.path.exists(SOCKET_PATH):
            try:
                os.remove(SOCKET_PATH)
            except Exception:
                pass
        logger.info("Native host stopped")


if __name__ == "__main__":
    main()
