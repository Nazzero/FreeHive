"""Tests for the Windows Node-install path in backend.setup_router.

Two regression areas:

1. `_is_installed("npm")` must NOT report npm as installed when node is
   missing. On Windows, an orphan ``%APPDATA%\\npm\\npm.cmd`` shim survives
   after node is uninstalled or never activated by a node manager — the shim
   is non-functional without node and was producing a confusing
   "Node ✗ / npm ✓" UI state.

2. The PowerShell snippet emitted to install Node via fnm must:
   - discover ``FNM_DIR`` from ``fnm env`` (NOT hardcode AppData\\Local)
   - add ``$FNM_DIR\\aliases\\default`` to user PATH (auto-following alias
     junction created by ``fnm default``) rather than a version-specific dir
   - never call ``fnm current`` (errors without shell integration)
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from backend import setup_router


# ---------------------------------------------------------------------------
# _is_installed("npm") gate
# ---------------------------------------------------------------------------

class TestNpmRequiresNode:
    def test_npm_present_node_present_returns_true(self):
        with patch.object(
            setup_router,
            "_get_binary_path",
            side_effect=lambda n: f"/fake/{n}.exe",
        ):
            assert setup_router._is_installed("npm") is True

    def test_npm_present_node_missing_returns_false(self):
        """The orphan-shim case — npm.cmd lingers after node is gone."""
        def fake(name):
            return "/fake/npm.cmd" if name == "npm" else None

        with patch.object(setup_router, "_get_binary_path", side_effect=fake):
            assert setup_router._is_installed("npm") is False

    def test_npm_missing_returns_false(self):
        with patch.object(setup_router, "_get_binary_path", return_value=None):
            assert setup_router._is_installed("npm") is False

    def test_node_check_unaffected_by_npm_logic(self):
        """node detection must not require npm — the gate is one-directional."""
        def fake(name):
            return "/fake/node.exe" if name == "node" else None

        with patch.object(setup_router, "_get_binary_path", side_effect=fake):
            assert setup_router._is_installed("node") is True

    def test_other_binaries_unaffected(self):
        """Only `npm` triggers the cross-check; other tools follow the simple rule."""
        def fake(name):
            return "/fake/claude.exe" if name == "claude" else None

        with patch.object(setup_router, "_get_binary_path", side_effect=fake):
            assert setup_router._is_installed("claude") is True
            assert setup_router._is_installed("gemini") is False


# ---------------------------------------------------------------------------
# Install-flow PowerShell snippet
# ---------------------------------------------------------------------------

def _install_node_source() -> str:
    """Return the source of the install_node coroutine for static inspection."""
    import inspect
    return inspect.getsource(setup_router.install_node)


def _install_node_code_only() -> str:
    """install_node source with comments and docstrings stripped — the actual
    runtime code, so static checks aren't fooled by prose in comments."""
    import io
    import tokenize

    src = _install_node_source()
    out_tokens = []
    g = tokenize.generate_tokens(io.StringIO(src).readline)
    seen_first_string = False  # track whether we've seen the function's docstring
    for tok in g:
        if tok.type == tokenize.COMMENT:
            continue
        # Skip the very first STRING token after the colon (the docstring).
        if (
            tok.type == tokenize.STRING
            and not seen_first_string
            and tok.string.startswith(('"""', "'''"))
        ):
            seen_first_string = True
            continue
        out_tokens.append(tok)
    return tokenize.untokenize(out_tokens)


class TestInstallFlowPowerShell:
    def test_uses_fnm_env_for_dir_discovery(self):
        src = _install_node_source()
        assert "fnm env --shell powershell" in src, (
            "Discovery must use `fnm env` — the only source that works without "
            "shell integration."
        )

    def test_does_not_call_fnm_current(self):
        # `fnm current` errors with "fnm env was not applied in this context"
        # in a non-integrated subprocess. Must not be in the runtime code.
        # (Comments mentioning it for documentation purposes are fine.)
        code = _install_node_code_only()
        assert "fnm current" not in code, (
            "fnm current must not be invoked — errors without shell integration."
        )

    def test_does_not_hardcode_node_versions_path(self):
        # Previous bug: built `%LOCALAPPDATA%\fnm\node-versions\<ver>\installation`,
        # but fnm uses Roaming AppData on many installs. Reading the
        # `node-versions` directory presence as a fallback hint is fine; what's
        # NOT fine is constructing a version-specific subpath there.
        code = _install_node_code_only()
        assert "node-versions\\" not in code and "node-versions/" not in code, (
            "Don't construct version-specific node-versions subpaths — use the "
            "alias junction instead."
        )

    def test_uses_default_alias_junction(self):
        src = _install_node_source()
        # aliases\default is the auto-following junction created by `fnm default`.
        assert r"aliases\default" in src, (
            "Must add the `aliases\\default` junction to PATH — auto-tracks "
            "future fnm default switches and contains both node.exe and npm.cmd."
        )

    def test_winget_skipped_when_fnm_already_present(self):
        src = _install_node_source()
        assert 'shutil.which("fnm"' in src, (
            "Must probe for existing fnm before invoking winget — winget "
            "exits 0x8A15002B on already-installed packages."
        )

    def test_persists_path_via_setenvironmentvariable(self):
        src = _install_node_source()
        # Persistence must use [Environment]::SetEnvironmentVariable("Path", ..., "User")
        # so the change survives across shells and the verify step in this run
        # reads it from registry.
        assert "SetEnvironmentVariable('Path'" in src or \
               'SetEnvironmentVariable("Path"' in src, (
            "Must persist via [Environment]::SetEnvironmentVariable on user scope."
        )

    def test_appends_to_existing_path_idempotently(self):
        src = _install_node_source()
        # Idempotence guard: must check `-contains $nodeDir` before appending.
        assert "-contains $nodeDir" in src, (
            "Must check membership before appending to avoid duplicate PATH entries."
        )


# ---------------------------------------------------------------------------
# Smoke test: actually run the PowerShell discovery against this machine.
# Skipped on non-Windows / when fnm isn't installed.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not setup_router.IS_WINDOWS, reason="Windows-only flow")
class TestPowerShellDiscoveryLive:
    def test_fnm_env_yields_fnm_dir(self):
        """If fnm is installed, its `fnm env` output must contain FNM_DIR."""
        import shutil
        import subprocess
        if not shutil.which("fnm"):
            pytest.skip("fnm not installed on this machine")

        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(fnm env --shell powershell 2>$null) | Out-String"],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0
        assert "$env:FNM_DIR" in result.stdout
