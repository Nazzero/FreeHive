"""Tests for Arena extension path and folder-open functions."""

import os
import platform
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from backend.services.chrome_launcher import (
    get_extension_path,
    open_extension_folder,
    get_chrome_status,
)


class TestGetExtensionPath:
    """Tests for get_extension_path()."""

    def test_returns_correct_shape(self):
        result = get_extension_path()
        assert "path" in result
        assert "exists" in result
        assert isinstance(result["path"], str)
        assert isinstance(result["exists"], bool)

    def test_path_is_nonempty(self):
        result = get_extension_path()
        assert len(result["path"]) > 0

    def test_exists_true_when_dir_and_manifest(self, tmp_path):
        """exists=True only when dir AND manifest.json both exist."""
        ext_dir = tmp_path / "arena_extension"
        ext_dir.mkdir()
        (ext_dir / "manifest.json").write_text("{}")
        with patch("backend.services.chrome_launcher.EXTENSION_DIR", ext_dir):
            result = get_extension_path()
        assert result["exists"] is True

    def test_exists_false_when_no_dir(self):
        fake = Path("/tmp/_nonexistent_ext_test_" + os.urandom(4).hex())
        with patch("backend.services.chrome_launcher.EXTENSION_DIR", fake):
            result = get_extension_path()
        assert result["exists"] is False
        assert result["path"] != ""

    def test_exists_false_when_dir_but_no_manifest(self, tmp_path):
        ext_dir = tmp_path / "arena_extension"
        ext_dir.mkdir()
        # No manifest.json
        with patch("backend.services.chrome_launcher.EXTENSION_DIR", ext_dir):
            result = get_extension_path()
        assert result["exists"] is False


class TestOpenExtensionFolder:
    """Tests for open_extension_folder()."""

    def test_returns_error_when_dir_missing(self):
        fake = Path("/tmp/_nonexistent_ext_test_" + os.urandom(4).hex())
        with patch("backend.services.chrome_launcher.EXTENSION_DIR", fake):
            result = open_extension_folder()
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    def test_success_when_dir_exists(self, tmp_path):
        ext_dir = tmp_path / "arena_extension"
        ext_dir.mkdir()
        with patch("backend.services.chrome_launcher.EXTENSION_DIR", ext_dir):
            with patch("subprocess.Popen") as mock_popen:
                result = open_extension_folder()
        assert result["success"] is True
        assert result["path"] == str(ext_dir.resolve())

    def test_linux_uses_xdg_open(self, tmp_path):
        ext_dir = tmp_path / "arena_extension"
        ext_dir.mkdir()
        with patch("backend.services.chrome_launcher.EXTENSION_DIR", ext_dir):
            with patch("platform.system", return_value="Linux"):
                with patch("subprocess.Popen") as mock_popen:
                    open_extension_folder()
                    mock_popen.assert_called_once()
                    args = mock_popen.call_args[0][0]
                    assert args[0] == "xdg-open"

    def test_macos_uses_open(self, tmp_path):
        ext_dir = tmp_path / "arena_extension"
        ext_dir.mkdir()
        with patch("backend.services.chrome_launcher.EXTENSION_DIR", ext_dir):
            with patch("platform.system", return_value="Darwin"):
                with patch("subprocess.Popen") as mock_popen:
                    open_extension_folder()
                    mock_popen.assert_called_once()
                    args = mock_popen.call_args[0][0]
                    assert args[0] == "open"

    def test_windows_uses_startfile(self, tmp_path):
        ext_dir = tmp_path / "arena_extension"
        ext_dir.mkdir()
        with patch("backend.services.chrome_launcher.EXTENSION_DIR", ext_dir):
            with patch("platform.system", return_value="Windows"):
                mock_startfile = MagicMock()
                with patch("os.startfile", mock_startfile, create=True):
                    result = open_extension_folder()
                    mock_startfile.assert_called_once_with(str(ext_dir.resolve()))
                    assert result["success"] is True

    def test_handles_popen_exception(self, tmp_path):
        ext_dir = tmp_path / "arena_extension"
        ext_dir.mkdir()
        with patch("backend.services.chrome_launcher.EXTENSION_DIR", ext_dir):
            with patch("subprocess.Popen", side_effect=OSError("no display")):
                result = open_extension_folder()
        assert result["success"] is False
        assert "no display" in result["error"]
        assert "path" in result


class TestChromeStatusIncludesExtPath:
    """Verify get_chrome_status() includes extension_path."""

    def test_extension_path_in_status(self):
        with patch("backend.services.chrome_launcher._find_chrome_binary", return_value=None):
            result = get_chrome_status()
        assert "extension_path" in result
        assert isinstance(result["extension_path"], str)
        assert len(result["extension_path"]) > 0
