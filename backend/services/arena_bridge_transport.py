"""
Shared transport resolution helpers for the Arena bridge.
"""

from __future__ import annotations

import os
import socket

DEFAULT_UNIX_SOCKET_PATH = "/tmp/freehive_arena_bridge.sock"
DEFAULT_TCP_HOST = "127.0.0.1"
DEFAULT_TCP_PORT = 8766


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
    """
    Returns either "unix" or "tcp".
    Defaults to TCP on Windows and Unix sockets elsewhere.
    """
    raw = str(os.getenv("FREEHIVE_ARENA_BRIDGE_TRANSPORT", "")).strip().lower()
    if raw in {"unix", "tcp"}:
        return raw
    return "tcp" if os.name == "nt" else "unix"


def get_bridge_socket_path() -> str:
    return str(
        os.getenv("FREEHIVE_ARENA_BRIDGE_SOCKET", DEFAULT_UNIX_SOCKET_PATH)
    ).strip() or DEFAULT_UNIX_SOCKET_PATH


def get_bridge_tcp_host() -> str:
    return str(os.getenv("FREEHIVE_ARENA_BRIDGE_HOST", DEFAULT_TCP_HOST)).strip() or DEFAULT_TCP_HOST


def get_bridge_tcp_port() -> int:
    return _env_int("FREEHIVE_ARENA_BRIDGE_PORT", DEFAULT_TCP_PORT)


def describe_bridge_endpoint() -> str:
    transport = get_bridge_transport()
    if transport == "tcp":
        return f"tcp://{get_bridge_tcp_host()}:{get_bridge_tcp_port()}"
    return f"unix://{get_bridge_socket_path()}"


def is_bridge_available(timeout_s: float = 0.5) -> bool:
    """Check if bridge is actually listening (not just file exists)."""
    transport = get_bridge_transport()
    if transport == "tcp":
        try:
            with socket.create_connection((get_bridge_tcp_host(), get_bridge_tcp_port()), timeout=timeout_s):
                return True
        except OSError:
            return False
    # Unix socket: actually connect instead of just checking file exists
    sock_path = get_bridge_socket_path()
    if not os.path.exists(sock_path):
        return False
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout_s)
        sock.connect(sock_path)
        sock.close()
        return True
    except OSError:
        return False
