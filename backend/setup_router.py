import asyncio
import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

try:
    import fcntl  # type: ignore
except Exception:
    fcntl = None

try:
    import pty  # type: ignore
except Exception:
    pty = None

setup_router = APIRouter()

CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
GEMINI_CREDENTIALS_FILE = Path.home() / ".gemini" / "oauth_creds.json"
CHATGPT_CREDENTIALS_FILE = Path.home() / ".codex" / "auth.json"
FREEHIVE_CONFIG_DIR = Path.home() / ".freehive"
FREEHIVE_CONFIG_FILE = FREEHIVE_CONFIG_DIR / "config.json"

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
HAS_PTY_SUPPORT = (not IS_WINDOWS) and (pty is not None) and (fcntl is not None)


def _refreshed_env() -> dict[str, str]:
    """Return os.environ with PATH refreshed from Windows registry.

    On Windows, tools installed mid-session (fnm, node) update the registry
    PATH but the current process still has the stale value.  This reads the
    live Machine+User PATH so subprocesses can find node/npm/cli wrappers.
    On non-Windows, returns os.environ unchanged.
    """
    env = os.environ.copy()
    if not IS_WINDOWS:
        return env
    try:
        import winreg
        parts: list[str] = []
        for root, sub in [
            (winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, r"Environment"),
        ]:
            try:
                with winreg.OpenKey(root, sub) as key:
                    val, _ = winreg.QueryValueEx(key, "Path")
                    if val:
                        parts.append(val)
            except OSError:
                pass
        if parts:
            env["PATH"] = ";".join(parts)
    except ImportError:
        pass
    return env

# Use login shell for UNIX so nvm/pyenv/etc are sourced.
_SHELL = ["cmd", "/c"] if IS_WINDOWS else ["bash", "-l", "-c"]

INSTALL_COMMANDS = {
    "claude_code": "npm install -g @anthropic-ai/claude-code",
    "gemini_cli": "npm install -g @google/gemini-cli",
    "chatgpt_cli": "npm install -g @openai/codex",
}

CLI_BINARIES = {
    "claude_code": "claude",
    "gemini_cli": "gemini",
    "chatgpt_cli": "codex",
}

AUTH_BINARIES = {
    **CLI_BINARIES,
    "chatgpt_cli": "codex",
}

# Matches all ANSI escape sequences and carriage returns
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;]*[mGKHFJsurA-Z]|\][^\x07]*\x07|[()][AB012])|[\r\x08]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _get_binary_path(name: str) -> str | None:
    # On Windows, refresh PATH from registry so mid-session installs are found
    path_override = _refreshed_env().get("PATH") if IS_WINDOWS else None
    found = shutil.which(name, path=path_override)
    if found:
        return found

    if IS_WINDOWS:
        try:
            env = _refreshed_env()
            result = subprocess.run(
                ["where", name],
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    path = line.strip()
                    if path:
                        return path
        except Exception:
            pass
        return None

    try:
        result = subprocess.run(
            _SHELL + [f"command -v {shlex.quote(name)}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            path = result.stdout.strip()
            if path:
                return path
    except Exception:
        pass
    return None


def _is_installed(name: str) -> bool:
    if _get_binary_path(name) is None:
        return False
    if name == "npm":
        # On Windows, an orphan %APPDATA%\npm\npm.cmd shim survives after node
        # is uninstalled or never activated by a node manager. The shim is
        # non-functional without node — don't claim npm is installed unless
        # node is too. (npm cannot run without node by definition.)
        return _get_binary_path("node") is not None
    return True


# ── CLI detection cache ────────────────────────────────────────────────────
# ⚠️ PERFORMANCE-CRITICAL — /setup/status is called on every app startup.
# Without caching, 6+ subprocess calls (each up to 10s timeout) execute on
# EVERY status poll. The cache avoids repeated shelling out for the same binary.
# Invalidate after installs so newly-installed CLIs are detected immediately.
_cli_cache: dict[str, tuple[bool, float]] = {}
_CLI_CACHE_TTL = 300  # 5 minutes

def _is_installed_cached(name: str) -> bool:
    """Cached wrapper around _is_installed. Returns cached result within TTL."""
    now = time.monotonic()
    if name in _cli_cache:
        val, ts = _cli_cache[name]
        if now - ts < _CLI_CACHE_TTL:
            return val
    result = _is_installed(name)
    _cli_cache[name] = (result, now)
    return result


def invalidate_cli_cache():
    """Clear CLI detection cache. Call after install/uninstall operations."""
    _cli_cache.clear()


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _read_auth_status() -> dict:
    if not CREDENTIALS_FILE.exists():
        return {
            "authenticated": False,
            "expired": False,
            "tier": None,
            "account_email": None,
            "account_name": None,
            "account_label": None,
        }
    try:
        data = json.loads(CREDENTIALS_FILE.read_text())
        oauth = data.get("claudeAiOauth", {})
        expires_at = oauth.get("expiresAt", 0)
        expired = False
        access_token = oauth.get("accessToken", "")
        payload = _decode_jwt_payload(access_token) if access_token else {}
        account_email = (
            oauth.get("email")
            or oauth.get("accountEmail")
            or payload.get("email")
            or payload.get("preferred_username")
            or payload.get("upn")
        )
        account_name = oauth.get("name") or payload.get("name")
        return {
            "authenticated": bool(access_token),
            "expired": expired,
            "tier": oauth.get("subscriptionType", "unknown"),
            "account_email": account_email,
            "account_name": account_name,
            "account_label": account_email or account_name,
        }
    except Exception:
        return {
            "authenticated": False,
            "expired": False,
            "tier": None,
            "account_email": None,
            "account_name": None,
            "account_label": None,
        }


def _read_gemini_auth_status() -> dict:
    if not GEMINI_CREDENTIALS_FILE.exists():
        return {
            "authenticated": False,
            "expired": False,
            "account_email": None,
            "account_name": None,
            "account_label": None,
        }
    try:
        data = json.loads(GEMINI_CREDENTIALS_FILE.read_text())
        access_token = data.get("access_token", "")
        id_token = data.get("id_token", "")
        payload = _decode_jwt_payload(id_token) if id_token else {}
        account_email = payload.get("email")
        account_name = payload.get("name")
        expiry_ms = data.get("expiry_date", 0)
        expired = (time.time() * 1000) >= expiry_ms
        return {
            "authenticated": bool(access_token),
            "expired": expired,
            "account_email": account_email,
            "account_name": account_name,
            "account_label": account_email or account_name,
        }
    except Exception:
        return {
            "authenticated": False,
            "expired": False,
            "account_email": None,
            "account_name": None,
            "account_label": None,
        }


def _decode_chatgpt_tier(token: str) -> str:
    try:
        payload = _decode_jwt_payload(token)
        tier = payload.get("chatgpt_plan_type")
        if not tier:
            auth_claim = payload.get("https://api.openai.com/auth", {})
            tier = auth_claim.get("chatgpt_plan_type")
        return tier or "unknown"
    except Exception:
        return "unknown"


def _read_chatgpt_auth_status() -> dict:
    if not CHATGPT_CREDENTIALS_FILE.exists():
        return {
            "authenticated": False,
            "tier": None,
            "account_email": None,
            "account_name": None,
            "account_label": None,
        }
    try:
        data = json.loads(CHATGPT_CREDENTIALS_FILE.read_text())
        tokens = data.get("tokens") or {}
        access_token = tokens.get("access_token", "")
        id_token = tokens.get("id_token", "")
        id_payload = _decode_jwt_payload(id_token) if id_token else {}
        access_payload = _decode_jwt_payload(access_token) if access_token else {}
        account_email = id_payload.get("email") or access_payload.get("email")
        account_name = id_payload.get("name") or access_payload.get("name")
        return {
            "authenticated": bool(access_token),
            "tier": _decode_chatgpt_tier(access_token) if access_token else None,
            "account_email": account_email,
            "account_name": account_name,
            "account_label": account_email or account_name,
        }
    except Exception:
        return {
            "authenticated": False,
            "tier": None,
            "account_email": None,
            "account_name": None,
            "account_label": None,
        }


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _read_config() -> dict:
    """Read ~/.freehive/config.json, returning empty dict if missing/corrupt."""
    if not FREEHIVE_CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(FREEHIVE_CONFIG_FILE.read_text())
    except Exception:
        return {}


def _write_config(data: dict) -> None:
    """Write to ~/.freehive/config.json, creating the directory if needed."""
    FREEHIVE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    FREEHIVE_CONFIG_FILE.write_text(json.dumps(data, indent=2))


def _get_selected_tool() -> str | None:
    return _read_config().get("selected_tool")


def _set_selected_tool(tool: str) -> None:
    config = _read_config()
    config["selected_tool"] = tool
    _write_config(config)


# ---------------------------------------------------------------------------
# Thinking effort config
# ---------------------------------------------------------------------------

class ThinkingEffortRequest(BaseModel):
    thinking_effort: str

@setup_router.get("/setup/thinking-effort")
async def get_thinking_effort():
    """Read default thinking effort from config."""
    config = _read_config()
    return {"thinking_effort": config.get("thinking_effort", "off")}

@setup_router.post("/setup/thinking-effort")
async def set_thinking_effort(body: ThinkingEffortRequest):
    """Persist default thinking effort to ~/.freehive/config.json."""
    from backend.thinking import VALID_EFFORTS
    if body.thinking_effort not in VALID_EFFORTS:
        return {"success": False, "error": f"Invalid effort: {body.thinking_effort}. Use: off, low, medium, high"}
    config = _read_config()
    config["thinking_effort"] = body.thinking_effort
    _write_config(config)
    return {"success": True, "thinking_effort": body.thinking_effort}


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

@setup_router.get("/setup/usage")
async def get_usage(request: Request):
    """Fetch live quota/usage from all providers + arena context tracking."""
    from backend.usage_fetcher import (
        fetch_claude_usage, fetch_chatgpt_usage, fetch_gemini_usage,
        fetch_arena_context_usage,
    )
    claude, chatgpt, gemini = await asyncio.gather(
        fetch_claude_usage(),
        fetch_chatgpt_usage(),
        fetch_gemini_usage(),
        return_exceptions=True,
    )
    providers = {}
    for result in [claude, chatgpt, gemini]:
        if isinstance(result, Exception):
            continue
        providers[result["provider"]] = result

    arena_sessions = []
    try:
        sm = getattr(request.app.state, "session_manager", None)
        cm = getattr(request.app.state, "conversation_manager", None)
        arena_sessions = await fetch_arena_context_usage(sm, cm)
    except Exception as exc:
        logger.warning("[usage] Arena context fetch failed: %s", exc)

    return {"providers": providers, "arena_sessions": arena_sessions}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

# ⚠️ PERFORMANCE-CRITICAL — Called on every app startup by frontend polling.
# CLI detection uses _is_installed_cached() with 5-min TTL to avoid repeated
# subprocess calls. Cache is invalidated after /setup/install completes.
# If adding new checks here, use _is_installed_cached() not _is_installed().
@setup_router.get("/setup/status")
async def get_setup_status():
    node_ok = _is_installed_cached("node")
    npm_ok = _is_installed_cached("npm")
    rg_ok = _is_installed_cached("rg")
    claude_ok = _is_installed_cached("claude")
    gemini_ok = _is_installed_cached("gemini")
    codex_ok = _is_installed_cached("codex")
    auth = _read_auth_status()
    gemini_auth = _read_gemini_auth_status()

    # Auto-refresh expired Gemini token so the UI doesn't falsely show disconnected
    if gemini_auth.get("authenticated") and gemini_auth.get("expired"):
        try:
            from backend.adapters.gemini_direct_adapter import GeminiDirectAdapter
            await GeminiDirectAdapter()._get_token()
            gemini_auth = _read_gemini_auth_status()
        except Exception:
            pass

    chatgpt_auth = _read_chatgpt_auth_status()
    authed = auth["authenticated"]
    selected_tool = _get_selected_tool()

    gemini_ready = gemini_auth["authenticated"] and not gemini_auth["expired"]
    chatgpt_ready = chatgpt_auth["authenticated"]
    any_provider_ready = authed or gemini_ready or chatgpt_ready

    # Arena status (extension bridge or CloakBrowser)
    browser_available = False
    arena_logged_in = False
    try:
        from backend.services.arena_bridge_transport import is_bridge_available
        if is_bridge_available():
            browser_available = True
            arena_logged_in = True  # Extension = user's Chrome = already logged in
        else:
            from backend.adapters.arena_steel_adapter import ArenaSteelAdapter
            _tmp_adapter = ArenaSteelAdapter()
            browser_available = await _tmp_adapter.is_available()
            if browser_available:
                arena_logged_in = await _tmp_adapter.is_authenticated()
    except Exception:
        pass

    return {
        "prerequisites": {
            "node": node_ok,
            "npm": npm_ok,
            "ripgrep": rg_ok,
        },
        "claude_code": {
            "installed": claude_ok,
            "authenticated": authed,
            "tier": auth["tier"],
            "account_email": auth["account_email"],
            "account_name": auth["account_name"],
            "account_label": auth["account_label"],
        },
        "gemini_cli": {
            "installed": gemini_ok,
            "authenticated": gemini_auth["authenticated"] and not gemini_auth["expired"],
            "tier": _read_config().get("model_discovery", {}).get("gemini", {}).get("tier"),
            "account_email": gemini_auth["account_email"],
            "account_name": gemini_auth["account_name"],
            "account_label": gemini_auth["account_label"],
        },
        "chatgpt_cli": {
            "installed": codex_ok,
            "authenticated": chatgpt_auth["authenticated"],
            "tier": chatgpt_auth["tier"],
            "account_email": chatgpt_auth["account_email"],
            "account_name": chatgpt_auth["account_name"],
            "account_label": chatgpt_auth["account_label"],
        },
        "selected_tool": selected_tool,
        "ready": any_provider_ready,
        "arena": {
            "steel_available": browser_available,
            "browser_available": browser_available,
            "authenticated": arena_logged_in,
            "viewer_url": None,
            "backend": "cloakbrowser",
        },
    }


@setup_router.post("/setup/logout/{tool}")
async def logout_tool(tool: str):
    tool_key = (tool or "").strip().lower()
    tool_to_file = {
        "openclaude": CREDENTIALS_FILE,
        "claude_code": CREDENTIALS_FILE,
        "claude": CREDENTIALS_FILE,
        "gemini_cli": GEMINI_CREDENTIALS_FILE,
        "gemini": GEMINI_CREDENTIALS_FILE,
        "chatgpt_cli": CHATGPT_CREDENTIALS_FILE,
        "chatgpt": CHATGPT_CREDENTIALS_FILE,
    }
    creds_file = tool_to_file.get(tool_key)
    if not creds_file:
        return {"success": False, "error": f"Unknown tool: {tool}"}

    try:
        removed = False
        if creds_file.exists():
            creds_file.unlink()
            removed = True

        # Gemini: also clear active account so CLI doesn't auto-reauthenticate
        if tool_key in ("gemini_cli", "gemini"):
            accounts_file = GEMINI_CREDENTIALS_FILE.parent / "google_accounts.json"
            if accounts_file.exists():
                try:
                    accounts = json.loads(accounts_file.read_text())
                    if accounts.get("active"):
                        old = accounts.get("old", [])
                        active = accounts.pop("active")
                        if active and active not in old:
                            old.append(active)
                        accounts["old"] = old
                        accounts_file.write_text(json.dumps(accounts, indent=2))
                except Exception:
                    pass

        return {"success": True, "tool": tool_key, "removed": removed}
    except Exception as exc:
        return {"success": False, "tool": tool_key, "error": str(exc)}


class SelectToolRequest(BaseModel):
    tool: str  # "openclaude" or "claude_code"


@setup_router.post("/setup/select-tool")
async def select_tool(request: SelectToolRequest):
    """Persist the user's tool choice to ~/.freehive/config.json."""
    if request.tool not in CLI_BINARIES:
        return {"success": False, "error": f"Unknown tool: {request.tool}"}
    _set_selected_tool(request.tool)
    return {"success": True, "selected_tool": request.tool}


class InstallRequest(BaseModel):
    tool: str  # "openclaude" or "claude_code"


@setup_router.post("/setup/install")
async def install_tool(request: InstallRequest):
    if request.tool not in INSTALL_COMMANDS:
        return {"error": f"Unknown tool: {request.tool}"}

    cmd = INSTALL_COMMANDS[request.tool]
    binary_name = CLI_BINARIES[request.tool]

    async def stream():
        yield _sse({"status": "starting", "msg": f"Running: {cmd}"})

        process = await asyncio.create_subprocess_exec(
            *_SHELL,
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_refreshed_env(),
        )

        async for raw_line in process.stdout:
            line = _strip_ansi(raw_line.decode(errors="replace").strip())
            if line:
                yield _sse({"status": "output", "msg": line})

        await process.wait()

        if process.returncode == 0:
            invalidate_cli_cache()
            installed = _is_installed(binary_name)
            yield _sse({"status": "done", "success": installed})
            if not installed:
                yield _sse({
                    "status": "warn",
                    "msg": (
                        "npm finished but the binary wasn't found in PATH. "
                        "You may need to restart the backend after adding npm's "
                        "global bin directory to your PATH."
                    ),
                })
        else:
            yield _sse({
                "status": "done",
                "success": False,
                "msg": "Install failed. Make sure Node.js and npm are installed.",
            })

    return StreamingResponse(stream(), media_type="text/event-stream")


_node_install_lock = False

_NODE_INSTALL_TIMEOUT = 300  # 5 minutes max for entire install


async def _run_with_timeout(cmd, timeout, stream_cb, env=None):
    """Run subprocess with timeout. Collects lines via stream_cb. Returns exit code."""
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        async def _drain():
            async for raw in process.stdout:
                line = _strip_ansi(raw.decode(errors="replace").strip())
                if line:
                    stream_cb(line)
            await process.wait()

        await asyncio.wait_for(_drain(), timeout=timeout)
        return process.returncode
    except asyncio.TimeoutError:
        try:
            process.kill()
            await process.wait()
        except Exception:
            pass
        return -1


@setup_router.post("/setup/install-node")
async def install_node():
    """Install Node.js via nvm (Linux/macOS) or fnm (Windows)."""
    global _node_install_lock

    if _node_install_lock:
        return StreamingResponse(
            _single_event({"status": "done", "success": False,
                           "msg": "Installation already in progress."}),
            media_type="text/event-stream",
        )

    # Skip if already installed
    if _is_installed("node") and _is_installed("npm"):
        return StreamingResponse(
            _single_event({"status": "done", "success": True,
                           "msg": "Node.js and npm are already installed."}),
            media_type="text/event-stream",
        )

    async def stream():
        global _node_install_lock
        _node_install_lock = True
        lines = []

        def collect(line):
            lines.append(line)

        try:
            yield _sse({"status": "starting", "msg": "Detecting platform..."})

            if IS_WINDOWS:
                # Step 1: install fnm via winget — but skip if already present.
                # winget exits 0x8A15002B (APPINSTALLER_CLI_ERROR_UPDATE_NOT_APPLICABLE)
                # when the package is already at the newest available version,
                # which we treat as success — re-probe via PATH after the call.
                if shutil.which("fnm", path=_refreshed_env().get("PATH")):
                    yield _sse({"status": "output",
                                "msg": "fnm already installed — skipping winget install."})
                else:
                    yield _sse({"status": "output",
                                "msg": "Windows detected — installing fnm (Fast Node Manager)..."})

                    rc = await _run_with_timeout(
                        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                         "winget install Schniz.fnm --accept-package-agreements --accept-source-agreements"],
                        timeout=120, stream_cb=collect,
                    )
                    for l in lines:
                        yield _sse({"status": "output", "msg": l})
                    lines.clear()

                    if rc == -1:
                        yield _sse({"status": "done", "success": False,
                                    "msg": "fnm installation timed out. Check your network connection."})
                        return

                    # Trust PATH over winget's exit code: a non-zero rc with fnm
                    # actually present means "already installed, no upgrade".
                    if not shutil.which("fnm", path=_refreshed_env().get("PATH")):
                        yield _sse({"status": "done", "success": False,
                                    "msg": "fnm installation failed."})
                        return

                # Step 2: install Node LTS via fnm, set as default, AND persist
                # `$FNM_DIR\aliases\default` to user PATH. That junction is
                # auto-maintained by `fnm default …` and contains both node.exe
                # and npm.cmd, so a single PATH entry exposes node globally and
                # auto-follows future fnm-default switches without re-running
                # this code.
                #
                # WHY NOT `fnm current`?  In a non-shell-integrated subprocess
                # (no `fnm env` applied), `fnm current` errors with
                # "fnm env was not applied in this context" — useless for path
                # discovery here. We use `fnm env --shell powershell` instead,
                # which always emits FNM_DIR regardless of integration state.
                #
                # WHY NOT `%LOCALAPPDATA%\fnm`?  fnm picks Roaming AppData on
                # some installs (its current default) and Local on others. The
                # only reliable source is `fnm env`'s FNM_DIR.
                yield _sse({"status": "output", "msg": "Installing Node.js LTS via fnm..."})

                fnm_install_ps = r"""
$ErrorActionPreference = 'Continue'
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')

fnm install --lts
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

fnm default lts-latest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# Discover FNM_DIR via `fnm env` (single source of truth).
$fnmDir = $null
$envOut = (fnm env --shell powershell 2>$null) | Out-String
if ($envOut -match '\$env:FNM_DIR\s*=\s*"([^"]+)"') { $fnmDir = $matches[1] }
if (-not $fnmDir) {
    foreach ($cand in @((Join-Path $env:APPDATA 'fnm'), (Join-Path $env:LOCALAPPDATA 'fnm'))) {
        if (Test-Path (Join-Path $cand 'node-versions')) { $fnmDir = $cand; break }
    }
}
if (-not $fnmDir) {
    Write-Host 'WARN: could not determine FNM_DIR; node will not be persisted on PATH.'
    exit 0
}

$nodeDir = Join-Path $fnmDir 'aliases\default'
$nodeExe = Join-Path $nodeDir 'node.exe'
if (-not (Test-Path $nodeExe)) {
    Write-Host "ERROR: $nodeExe not found after fnm install — alias junction missing."
    exit 1
}

$userPath = [Environment]::GetEnvironmentVariable('Path','User')
$entries = if ($userPath) { $userPath -split ';' } else { @() }
if ($entries -contains $nodeDir) {
    Write-Host "$nodeDir already on user PATH."
} else {
    $newPath = if ($userPath) { "$userPath;$nodeDir" } else { $nodeDir }
    [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
    Write-Host "Added $nodeDir to user PATH (fnm default-alias junction)."
}
"""

                rc = await _run_with_timeout(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", fnm_install_ps],
                    timeout=180, stream_cb=collect,
                )
                for l in lines:
                    yield _sse({"status": "output", "msg": l})
                lines.clear()

                if rc == -1:
                    yield _sse({"status": "done", "success": False,
                                "msg": "Node.js installation timed out."})
                    return
                if rc != 0:
                    yield _sse({"status": "done", "success": False,
                                "msg": "Node.js installation via fnm failed."})
                    return

            else:
                # Linux / macOS — use nvm
                if not shutil.which("curl"):
                    yield _sse({"status": "done", "success": False,
                                "msg": "curl is required but not found. Install curl first."})
                    return

                yield _sse({"status": "output",
                            "msg": f"{'macOS' if IS_MACOS else 'Linux'} detected — installing nvm..."})

                # Check if nvm already installed
                nvm_dir = Path.home() / ".nvm"
                if (nvm_dir / "nvm.sh").exists():
                    yield _sse({"status": "output", "msg": "nvm already installed, skipping download..."})
                else:
                    # Step 1: install nvm
                    rc = await _run_with_timeout(
                        ["bash", "-c",
                         "curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash"],
                        timeout=120, stream_cb=collect,
                    )
                    for l in lines:
                        yield _sse({"status": "output", "msg": l})
                    lines.clear()

                    if rc == -1:
                        yield _sse({"status": "done", "success": False,
                                    "msg": "nvm installation timed out. Check your network connection."})
                        return
                    if rc != 0:
                        yield _sse({"status": "done", "success": False,
                                    "msg": "nvm installation failed."})
                        return

                # Step 2: install Node LTS via nvm
                # Source nvm explicitly in case .bashrc has non-interactive guard
                yield _sse({"status": "output", "msg": "Installing Node.js LTS via nvm..."})
                nvm_cmd = (
                    'export NVM_DIR="$HOME/.nvm" && '
                    '[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" && '
                    'nvm install --lts'
                )
                rc = await _run_with_timeout(
                    ["bash", "-c", nvm_cmd],
                    timeout=180, stream_cb=collect,
                )
                for l in lines:
                    yield _sse({"status": "output", "msg": l})
                lines.clear()

                if rc == -1:
                    yield _sse({"status": "done", "success": False,
                                "msg": "Node.js installation timed out. Check your network connection."})
                    return
                if rc != 0:
                    yield _sse({"status": "done", "success": False,
                                "msg": "Node.js installation via nvm failed."})
                    return

            # Verify — invalidate cache so fresh check runs
            invalidate_cli_cache()
            node_ok = _is_installed("node")
            npm_ok = _is_installed("npm")
            if node_ok and npm_ok:
                yield _sse({"status": "done", "success": True,
                            "msg": "Node.js and npm installed successfully."})
            else:
                yield _sse({"status": "done", "success": False,
                            "msg": ("Installation completed but node/npm not found in PATH. "
                                    "You may need to restart the app.")})
        except Exception as exc:
            logger.exception("install-node failed")
            yield _sse({"status": "done", "success": False,
                        "msg": f"Unexpected error: {exc}"})
        finally:
            _node_install_lock = False

    return StreamingResponse(stream(), media_type="text/event-stream")


@setup_router.get("/setup/auth/{tool}")
async def start_auth(tool: str):
    if tool not in AUTH_BINARIES:
        return StreamingResponse(
            _single_event({"status": "error", "msg": f"Unknown tool: {tool}"}),
            media_type="text/event-stream",
        )

    binary_path = _get_binary_path(AUTH_BINARIES[tool])
    if not binary_path:
        return StreamingResponse(
            _single_event({
                "status": "error",
                "msg": f"{AUTH_BINARIES[tool]} is not installed. Install it first.",
            }),
            media_type="text/event-stream",
        )

    is_gemini = tool == "gemini_cli"
    is_chatgpt = tool == "chatgpt_cli"
    creds_file = (
        GEMINI_CREDENTIALS_FILE
        if is_gemini
        else CHATGPT_CREDENTIALS_FILE
        if is_chatgpt
        else CREDENTIALS_FILE
    )
    # Use explicit auth subcommand for CLIs that support it.
    # Gemini CLI has no "auth login" — it auto-prompts on startup when not authenticated.
    # Passing a no-op prompt triggers auth check without entering interactive TUI.
    if is_gemini:
        # Pre-select OAuth method so the CLI commits to the flow instead of
        # sitting on an interactive picker, then nudge it to run a trivial
        # prompt which fails fast and triggers the OAuth URL print.
        try:
            gemini_dir = Path.home() / ".gemini"
            gemini_dir.mkdir(parents=True, exist_ok=True)
            settings_path = gemini_dir / "settings.json"
            existing = {}
            if settings_path.exists():
                try:
                    existing = json.loads(settings_path.read_text(encoding="utf-8"))
                except Exception:
                    existing = {}
            if existing.get("selectedAuthType") != "oauth-personal":
                existing["selectedAuthType"] = "oauth-personal"
                settings_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("Could not pre-write gemini settings.json: %s", e)

        if IS_WINDOWS:
            # Use cmd.exe so the .cmd wrapper runs correctly. Single-word
            # prompt with `=` avoids nested-quote mangling by cmd.exe.
            quoted = binary_path if " " not in binary_path else f'"{binary_path}"'
            auth_cmd_str = f"{quoted} --prompt=hi"
            auth_cmd = ["cmd", "/c", auth_cmd_str]
        else:
            auth_cmd = [binary_path, "--prompt", "hi"]
    else:
        auth_cmd = [binary_path, "auth", "login"]
    # URL pattern to detect and open in browser
    url_pattern = (
        r"https://[^\s\x00-\x1f]+accounts\.google[^\s\x00-\x1f]*"
        if is_gemini
        else r"https://[^\s\x00-\x1f]+(?:auth\.openai\.com|chatgpt\.com)[^\s\x00-\x1f]*"
        if is_chatgpt
        else r"https://[^\s\x00-\x1f]+(?:claude\.ai|anthropic\.com)[^\s\x00-\x1f]*"
    )

    async def auth_stream():
        yield _sse({"status": "starting", "msg": "Launching auth flow..."})

        creds_mtime_before = creds_file.stat().st_mtime if creds_file.exists() else 0

        start_time = time.time()
        output_buf = ""
        browser_opened = False

        def _auth_complete() -> tuple[bool, dict]:
            if is_gemini:
                gemini_auth = _read_gemini_auth_status()
                return (
                    gemini_auth["authenticated"] and not gemini_auth["expired"],
                    {"selected_tool": tool},
                )
            if is_chatgpt:
                chatgpt_auth = _read_chatgpt_auth_status()
                return (
                    chatgpt_auth["authenticated"],
                    {
                        "tier": chatgpt_auth["tier"],
                        "account_label": chatgpt_auth["account_label"],
                    },
                )
            auth = _read_auth_status()
            return (
                auth["authenticated"] and not auth["expired"],
                {"tier": auth["tier"], "selected_tool": tool},
            )

        def _persist_selected_tool() -> bool:
            return tool in ("openclaude", "claude_code", "gemini_cli", "chatgpt_cli")

        if HAS_PTY_SUPPORT:
            master_fd, slave_fd = pty.openpty()
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            # TERM=dumb suppresses Gemini CLI's ink TUI while keeping PTY
            env = os.environ.copy()
            if is_gemini:
                env["TERM"] = "dumb"
                env["GOOGLE_GENAI_USE_GCA"] = "true"

            process = await asyncio.create_subprocess_exec(
                *auth_cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                preexec_fn=os.setsid,
                env=env,
            )
            os.close(slave_fd)

            try:
                while True:
                    if creds_file.exists():
                        mtime = creds_file.stat().st_mtime
                        if mtime != creds_mtime_before:
                            ok, payload = _auth_complete()
                            if ok:
                                if _persist_selected_tool():
                                    _set_selected_tool(tool)
                                asyncio.create_task(_run_model_discovery())
                                yield _sse({"status": "success", **payload})
                                return

                    if (time.time() - start_time) > 180:
                        yield _sse({
                            "status": "timeout",
                            "msg": "Auth timed out after 3 minutes. Try again.",
                        })
                        return

                    try:
                        chunk = os.read(master_fd, 4096)
                        if chunk:
                            clean = _strip_ansi(chunk.decode(errors="replace"))
                            output_buf += clean

                            # Auto-answer Y/n prompts (Gemini CLI asks before opening browser)
                            if re.search(r"\[Y/n\]", output_buf):
                                try:
                                    os.write(master_fd, b"Y\n")
                                except OSError:
                                    pass

                            url_match = re.search(url_pattern, output_buf)
                            if url_match and not browser_opened:
                                url = url_match.group(0).rstrip(".,);'\"")
                                webbrowser.open(url)
                                browser_opened = True
                                output_buf = ""
                                yield _sse({
                                    "status": "browser_opened",
                                    "msg": "Browser opened — complete the login then return here.",
                                })
                            else:
                                for line in re.split(r"[\r\n]+", clean):
                                    line = line.strip()
                                    if line:
                                        yield _sse({"status": "output", "msg": line[:300]})
                    except BlockingIOError:
                        pass
                    except OSError:
                        break

                    status_msg = (
                        "Waiting for browser login..."
                        if browser_opened
                        else "Waiting for auth flow to start..."
                    )
                    yield _sse({"status": "waiting", "msg": status_msg})
                    await asyncio.sleep(0.8)
            finally:
                try:
                    os.killpg(os.getpgid(process.pid), 9)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
                try:
                    os.close(master_fd)
                except Exception:
                    pass
        else:
            # Windows fallback: no PTY available — use pipes instead
            # Use refreshed PATH so node/npm installed mid-session are found
            env = _refreshed_env()
            if is_gemini:
                env["TERM"] = "dumb"
                env["GOOGLE_GENAI_USE_GCA"] = "true"

            process = await asyncio.create_subprocess_exec(
                *auth_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            try:
                while True:
                    if creds_file.exists():
                        mtime = creds_file.stat().st_mtime
                        if mtime != creds_mtime_before:
                            ok, payload = _auth_complete()
                            if ok:
                                if _persist_selected_tool():
                                    _set_selected_tool(tool)
                                asyncio.create_task(_run_model_discovery())
                                yield _sse({"status": "success", **payload})
                                return

                    if (time.time() - start_time) > 180:
                        yield _sse({
                            "status": "timeout",
                            "msg": "Auth timed out after 3 minutes. Try again.",
                        })
                        return

                    chunk = b""
                    try:
                        chunk = await asyncio.wait_for(process.stdout.read(4096), timeout=0.8)
                    except asyncio.TimeoutError:
                        chunk = b""

                    if chunk:
                        clean = _strip_ansi(chunk.decode(errors="replace")).strip()
                        if clean:
                            output_buf += f"{clean}\n"

                            # Auto-answer Y/n prompts (Gemini CLI asks before opening browser)
                            if re.search(r"\[Y/n\]", output_buf) and process.stdin:
                                try:
                                    process.stdin.write(b"Y\n")
                                    await process.stdin.drain()
                                except Exception:
                                    pass

                            url_match = re.search(url_pattern, output_buf)
                            if url_match and not browser_opened:
                                url = url_match.group(0).rstrip(".,);'\"")
                                webbrowser.open(url)
                                browser_opened = True
                                output_buf = ""
                                yield _sse({
                                    "status": "browser_opened",
                                    "msg": "Browser opened — complete the login then return here.",
                                })
                            else:
                                yield _sse({"status": "output", "msg": clean[:300]})
                    elif process.returncode is not None:
                        break

                    status_msg = (
                        "Waiting for browser login..."
                        if browser_opened
                        else "Waiting for auth flow to start..."
                    )
                    yield _sse({"status": "waiting", "msg": status_msg})
            finally:
                if process.returncode is None:
                    try:
                        process.kill()
                    except Exception:
                        pass

        if is_gemini:
            gemini_auth = _read_gemini_auth_status()
            if gemini_auth["authenticated"] and not gemini_auth["expired"]:
                _set_selected_tool(tool)
                yield _sse({"status": "success", "selected_tool": tool})
            else:
                yield _sse({
                    "status": "failed",
                    "msg": "Auth did not complete. Finish the Google login in the browser.",
                })
        elif is_chatgpt:
            chatgpt_auth = _read_chatgpt_auth_status()
            if chatgpt_auth["authenticated"]:
                asyncio.create_task(_run_model_discovery())
                yield _sse({
                    "status": "success",
                    "tier": chatgpt_auth["tier"],
                    "account_label": chatgpt_auth["account_label"],
                })
            else:
                yield _sse({
                    "status": "failed",
                    "msg": "Auth did not complete. Make sure you finish the ChatGPT login in the browser.",
                })
        else:
            auth = _read_auth_status()
            if auth["authenticated"] and not auth["expired"]:
                _set_selected_tool(tool)
                yield _sse({
                    "status": "success",
                    "tier": auth["tier"],
                    "selected_tool": tool,
                })
            else:
                yield _sse({
                    "status": "failed",
                    "msg": "Auth did not complete. Make sure you finish the login in the browser.",
                })

    return StreamingResponse(auth_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Model discovery endpoint
# ---------------------------------------------------------------------------

@setup_router.get("/setup/arena/status")
async def get_arena_steel_status(request: Request):
    """Check Arena transport availability (extension bridge or CloakBrowser)."""
    try:
        am = getattr(request.app.state, "arena_manager", None)
        if am is None:
            return {
                "steel_available": False, "browser_available": False,
                "bridge_active": False, "transport": "offline",
                "authenticated": False, "account": None, "viewer_url": None,
            }

        mgr_status = am.get_status()
        transport = mgr_status.get("transport", "offline")
        bridge_active = mgr_status.get("bridge_active", False)

        # Extension bridge: user's Chrome handles auth
        if bridge_active:
            # Include registered extension IDs so frontend knows which type is active
            extension_ids = {}
            try:
                from backend.services.chrome_launcher import get_active_extension_ids
                extension_ids = get_active_extension_ids()
            except Exception:
                pass
            return {
                "steel_available": True, "browser_available": True,
                "bridge_active": True, "transport": "extension",
                "authenticated": True, "account": None, "viewer_url": None,
                "extension_ids": extension_ids,
            }

        # CloakBrowser fallback
        adapter = am.get_adapter()
        browser_available = False
        account_info = {"logged_in": False}

        if adapter is not None and hasattr(adapter, "is_available"):
            browser_available = await adapter.is_available()
            if browser_available and hasattr(adapter, "get_account_info"):
                if getattr(am, "_login_session_active", False):
                    account_info = await adapter.get_account_info()
                    import os
                    want_headless = os.getenv("ARENA_HEADED", "").strip() not in ("1", "true", "yes")
                    if account_info.get("logged_in") and want_headless:
                        try:
                            await adapter.close()
                            import asyncio
                            await asyncio.sleep(1)
                            adapter._orchestrator._headless = True
                            am._login_session_active = False
                            logger.info("[arena/status] Login confirmed, switched to headless")
                        except Exception:
                            pass
                    elif account_info.get("logged_in"):
                        am._login_session_active = False
                else:
                    account_info = await adapter.get_account_info()

        return {
            "steel_available": browser_available,
            "browser_available": browser_available,
            "bridge_active": False,
            "transport": "cloakbrowser" if browser_available else "offline",
            "authenticated": account_info.get("logged_in", False),
            "account": account_info if account_info.get("logged_in") else None,
            "viewer_url": None,
        }
    except Exception as exc:
        return {
            "steel_available": False, "browser_available": False,
            "bridge_active": False, "transport": "offline",
            "authenticated": False, "account": None, "viewer_url": None,
            "error": str(exc),
        }


@setup_router.get("/setup/models")
async def get_available_models(refresh: bool = False):
    """
    Returns the available models and tier for each authenticated provider.
    Uses cached discovery unless refresh=true is passed.
    """
    from backend.model_discovery import discover_all_models, get_cached_discovery

    if refresh:
        result = await discover_all_models()
    else:
        result = get_cached_discovery()
        if not result:
            result = await discover_all_models()

    return result


@setup_router.post("/setup/models/refresh")
async def refresh_models():
    """Force re-discover models for all providers."""
    from backend.model_discovery import discover_all_models
    result = await discover_all_models()
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _run_model_discovery():
    """Background task: discover models after auth completes."""
    try:
        from backend.model_discovery import discover_all_models
        await discover_all_models()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Model discovery after auth failed: %s", exc)


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


async def _single_event(data: dict):
    yield _sse(data)
