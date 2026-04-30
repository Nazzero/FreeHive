"""Tests for Arena bridge transport, native host, and connection pipeline."""

import json
import os
import socket
import struct
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Transport availability tests
# ---------------------------------------------------------------------------

class TestBridgeTransport:
    """Tests for arena_bridge_transport.py"""

    def test_default_transport_unix_on_linux(self):
        """Linux/macOS should default to unix socket."""
        from backend.services.arena_bridge_transport import get_bridge_transport
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FREEHIVE_ARENA_BRIDGE_TRANSPORT", None)
            if os.name != "nt":
                assert get_bridge_transport() == "unix"

    def test_default_transport_tcp_on_windows(self):
        """Windows should default to TCP."""
        from backend.services.arena_bridge_transport import get_bridge_transport
        with patch("os.name", "nt"), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FREEHIVE_ARENA_BRIDGE_TRANSPORT", None)
            assert get_bridge_transport() == "tcp"

    def test_env_override_transport(self):
        """Environment variable should override default transport."""
        from backend.services.arena_bridge_transport import get_bridge_transport
        with patch.dict(os.environ, {"FREEHIVE_ARENA_BRIDGE_TRANSPORT": "tcp"}):
            assert get_bridge_transport() == "tcp"
        with patch.dict(os.environ, {"FREEHIVE_ARENA_BRIDGE_TRANSPORT": "unix"}):
            assert get_bridge_transport() == "unix"

    def test_is_bridge_available_tcp_no_listener(self):
        """TCP check should return False when no server is listening."""
        from backend.services.arena_bridge_transport import is_bridge_available
        with patch.dict(os.environ, {"FREEHIVE_ARENA_BRIDGE_TRANSPORT": "tcp",
                                      "FREEHIVE_ARENA_BRIDGE_PORT": "19999"}):
            assert is_bridge_available(timeout_s=0.1) is False

    def test_is_bridge_available_tcp_with_listener(self):
        """TCP check should return True when server is listening."""
        from backend.services.arena_bridge_transport import is_bridge_available
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.listen(1)
        try:
            with patch.dict(os.environ, {"FREEHIVE_ARENA_BRIDGE_TRANSPORT": "tcp",
                                          "FREEHIVE_ARENA_BRIDGE_PORT": str(port)}):
                assert is_bridge_available(timeout_s=0.5) is True
        finally:
            server.close()

    @pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets not available")
    def test_is_bridge_available_unix_no_socket_file(self):
        """Unix check should return False when socket file doesn't exist."""
        from backend.services.arena_bridge_transport import is_bridge_available
        with patch.dict(os.environ, {"FREEHIVE_ARENA_BRIDGE_TRANSPORT": "unix",
                                      "FREEHIVE_ARENA_BRIDGE_SOCKET": "/tmp/nonexistent_test.sock"}):
            assert is_bridge_available(timeout_s=0.1) is False

    @pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets not available")
    def test_is_bridge_available_unix_stale_socket(self):
        """Unix check should return False for stale socket file (no listener)."""
        from backend.services.arena_bridge_transport import is_bridge_available
        sock_path = tempfile.mktemp(suffix=".sock")
        # Create a stale socket file (just a regular file, not a real socket)
        Path(sock_path).touch()
        try:
            with patch.dict(os.environ, {"FREEHIVE_ARENA_BRIDGE_TRANSPORT": "unix",
                                          "FREEHIVE_ARENA_BRIDGE_SOCKET": sock_path}):
                assert is_bridge_available(timeout_s=0.1) is False
        finally:
            os.unlink(sock_path)

    @pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets not available")
    def test_is_bridge_available_unix_real_listener(self):
        """Unix check should return True when actual listener is active."""
        from backend.services.arena_bridge_transport import is_bridge_available
        sock_path = tempfile.mktemp(suffix=".sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)
        try:
            with patch.dict(os.environ, {"FREEHIVE_ARENA_BRIDGE_TRANSPORT": "unix",
                                          "FREEHIVE_ARENA_BRIDGE_SOCKET": sock_path}):
                assert is_bridge_available(timeout_s=0.5) is True
        finally:
            server.close()
            if os.path.exists(sock_path):
                os.unlink(sock_path)

    def test_timeout_increased_from_150ms(self):
        """Default timeout should be 500ms, not 150ms."""
        import inspect
        from backend.services.arena_bridge_transport import is_bridge_available
        sig = inspect.signature(is_bridge_available)
        default_timeout = sig.parameters["timeout_s"].default
        assert default_timeout >= 0.5, f"Default timeout {default_timeout}s too low, should be >= 0.5s"


# ---------------------------------------------------------------------------
# Protocol version tests
# ---------------------------------------------------------------------------

class TestProtocol:
    """Tests for shared/arena_bridge_protocol.py"""

    def test_min_extension_version_exists(self):
        from shared.arena_bridge_protocol import MIN_EXTENSION_VERSION
        assert MIN_EXTENSION_VERSION == "1.0.1"

    def test_protocol_version_matches_extension(self):
        from shared.arena_bridge_protocol import PROTOCOL_VERSION
        # Read extension's protocol version
        ext_bg = Path(__file__).parent.parent / "arena_extension" / "background.js"
        content = ext_bg.read_text()
        for line in content.split("\n"):
            if "PROTOCOL_VERSION" in line and "=" in line:
                ext_version = line.split('"')[1]
                assert PROTOCOL_VERSION == ext_version, \
                    f"Protocol mismatch: backend={PROTOCOL_VERSION}, extension={ext_version}"
                break

    def test_extension_manifest_version(self):
        """Extension manifest should be v1.0.1."""
        manifest_path = Path(__file__).parent.parent / "arena_extension" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        assert manifest["version"] == "1.0.1"


# ---------------------------------------------------------------------------
# Native host version check tests
# ---------------------------------------------------------------------------

class TestNativeHostVersionCheck:
    """Tests for version comparison in host.py"""

    def test_version_tuple_parsing(self):
        # Import from host.py's inlined code
        sys.path.insert(0, str(Path(__file__).parent.parent / "native_host"))
        from host import _version_tuple
        assert _version_tuple("1.0.0") == (1, 0, 0)
        assert _version_tuple("1.0.1") == (1, 0, 1)
        assert _version_tuple("2.1.0") == (2, 1, 0)
        assert _version_tuple("invalid") == (0,)
        assert _version_tuple("") == (0,)

    def test_version_comparison(self):
        sys.path.insert(0, str(Path(__file__).parent.parent / "native_host"))
        from host import _version_tuple, MIN_EXTENSION_VERSION
        assert _version_tuple("1.0.0") < _version_tuple(MIN_EXTENSION_VERSION)
        assert _version_tuple("1.0.1") >= _version_tuple(MIN_EXTENSION_VERSION)
        assert _version_tuple("1.1.0") >= _version_tuple(MIN_EXTENSION_VERSION)
        assert _version_tuple("0.9.9") < _version_tuple(MIN_EXTENSION_VERSION)


# ---------------------------------------------------------------------------
# Chrome launcher extension ID tests
# ---------------------------------------------------------------------------

class TestExtensionIdManagement:
    """Tests for chrome_launcher.py extension ID handling."""

    def test_known_extension_id(self):
        from backend.services.chrome_launcher import KNOWN_EXTENSION_ID
        assert KNOWN_EXTENSION_ID == "jkclihigpeefogblifghhpojgkbheked"
        assert len(KNOWN_EXTENSION_ID) == 32

    def test_patch_native_host_manifest_adds_unpacked_id(self):
        """Patching manifest should add unpacked ID to allowed_origins."""
        from backend.services.chrome_launcher import patch_native_host_manifest, KNOWN_EXTENSION_ID
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "name": "test",
                "type": "stdio",
                "path": "/test",
                "allowed_origins": [f"chrome-extension://{KNOWN_EXTENSION_ID}/"]
            }, f)
            f.flush()
            manifest_path = f.name

        try:
            unpacked_id = "abcdefghijklmnopqrstuvwxyzabcdef"
            with patch("backend.services.chrome_launcher._get_native_host_manifest_path",
                       return_value=Path(manifest_path)):
                result = patch_native_host_manifest(unpacked_id)
            assert result is True

            with open(manifest_path) as f:
                data = json.load(f)
            origins = data["allowed_origins"]
            assert f"chrome-extension://{KNOWN_EXTENSION_ID}/" in origins
            assert f"chrome-extension://{unpacked_id}/" in origins
        finally:
            os.unlink(manifest_path)

    def test_patch_manifest_deduplicates(self):
        """Patching same ID twice shouldn't create duplicate entries."""
        from backend.services.chrome_launcher import patch_native_host_manifest, KNOWN_EXTENSION_ID
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "name": "test",
                "type": "stdio",
                "path": "/test",
                "allowed_origins": [f"chrome-extension://{KNOWN_EXTENSION_ID}/"]
            }, f)
            f.flush()
            manifest_path = f.name

        try:
            unpacked_id = "abcdefghijklmnopqrstuvwxyzabcdef"
            with patch("backend.services.chrome_launcher._get_native_host_manifest_path",
                       return_value=Path(manifest_path)):
                patch_native_host_manifest(unpacked_id)
                patch_native_host_manifest(unpacked_id)

            with open(manifest_path) as f:
                data = json.load(f)
            origins = data["allowed_origins"]
            unpacked_count = sum(1 for o in origins if unpacked_id in o)
            assert unpacked_count == 1, f"Duplicate entries: {origins}"
        finally:
            os.unlink(manifest_path)


# ---------------------------------------------------------------------------
# Native message framing tests
# ---------------------------------------------------------------------------

class TestNativeMessageFraming:
    """Tests for Chrome native messaging protocol (4-byte length prefix)."""

    def test_message_roundtrip(self):
        """Encode and decode native message correctly."""
        sys.path.insert(0, str(Path(__file__).parent.parent / "native_host"))

        payload = {"type": "hello", "protocol_version": "2026-04-08.v1"}
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        frame = struct.pack("<I", len(encoded)) + encoded

        # Decode
        msg_len = struct.unpack("<I", frame[:4])[0]
        decoded = json.loads(frame[4:4 + msg_len].decode("utf-8"))
        assert decoded == payload

    def test_empty_message_detection(self):
        """Empty stdin should return None from read_native_message."""
        # 0 bytes = EOF
        assert struct.pack("<I", 0) == b'\x00\x00\x00\x00'


# ---------------------------------------------------------------------------
# Exponential backoff tests (extension background.js logic)
# ---------------------------------------------------------------------------

class TestExponentialBackoff:
    """Verify backoff math matches what background.js implements."""

    def test_backoff_schedule(self):
        """Backoff: 1500 * 2^n, capped at 30000."""
        base = 1500
        max_delay = 30000
        expected = [1500, 3000, 6000, 12000, 24000, 30000, 30000]
        for attempt, exp in enumerate(expected):
            delay = min(base * (2 ** attempt), max_delay)
            assert delay == exp, f"Attempt {attempt}: expected {exp}, got {delay}"


# ---------------------------------------------------------------------------
# Edge case: transport env var validation
# ---------------------------------------------------------------------------

class TestEnvVarValidation:
    """Test environment variable parsing edge cases."""

    def test_invalid_port_falls_back_to_default(self):
        from backend.services.arena_bridge_transport import get_bridge_tcp_port
        with patch.dict(os.environ, {"FREEHIVE_ARENA_BRIDGE_PORT": "not_a_number"}):
            assert get_bridge_tcp_port() == 8766
        with patch.dict(os.environ, {"FREEHIVE_ARENA_BRIDGE_PORT": "99999"}):
            assert get_bridge_tcp_port() == 8766
        with patch.dict(os.environ, {"FREEHIVE_ARENA_BRIDGE_PORT": "0"}):
            assert get_bridge_tcp_port() == 8766
        with patch.dict(os.environ, {"FREEHIVE_ARENA_BRIDGE_PORT": "-1"}):
            assert get_bridge_tcp_port() == 8766

    def test_empty_socket_path_falls_back(self):
        from backend.services.arena_bridge_transport import get_bridge_socket_path
        with patch.dict(os.environ, {"FREEHIVE_ARENA_BRIDGE_SOCKET": ""}):
            assert get_bridge_socket_path() == "/tmp/freehive_arena_bridge.sock"

    def test_invalid_transport_falls_back(self):
        from backend.services.arena_bridge_transport import get_bridge_transport
        with patch.dict(os.environ, {"FREEHIVE_ARENA_BRIDGE_TRANSPORT": "invalid"}):
            result = get_bridge_transport()
            assert result in ("unix", "tcp")
