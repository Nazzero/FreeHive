import asyncio
import fcntl
import json
import os
import pty
import re
import shlex
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

setup_router = APIRouter()

CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
FREEHIVE_CONFIG_DIR = Path.home() / ".freehive"
FREEHIVE_CONFIG_FILE = FREEHIVE_CONFIG_DIR / "config.json"

# Use login shell for all subprocess calls so nvm/pyenv/etc are sourced
_SHELL = ["bash", "-l", "-c"]

INSTALL_COMMANDS = {
    "openclaude": "npm install -g @gitlawb/openclaude",
    "claude_code": "npm install -g @anthropic-ai/claude-code",
}

CLI_BINARIES = {
    "openclaude": "openclaude",
    "claude_code": "claude",
}

# Matches all ANSI escape sequences and carriage returns
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;]*[mGKHFJsurA-Z]|\][^\x07]*\x07|[()][AB012])|[\r\x08]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _get_binary_path(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
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
    return _get_binary_path(name) is not None


def _read_auth_status() -> dict:
    if not CREDENTIALS_FILE.exists():
        return {"authenticated": False, "expired": False, "tier": None}
    try:
        data = json.loads(CREDENTIALS_FILE.read_text())
        oauth = data.get("claudeAiOauth", {})
        expires_at = oauth.get("expiresAt", 0)
        expired = time.time() * 1000 >= expires_at
        return {
            "authenticated": bool(oauth.get("accessToken")),
            "expired": expired,
            "tier": oauth.get("subscriptionType", "unknown"),
        }
    except Exception:
        return {"authenticated": False, "expired": False, "tier": None}


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
# Endpoints
# ---------------------------------------------------------------------------

@setup_router.get("/setup/status")
async def get_setup_status():
    node_ok = _is_installed("node")
    npm_ok = _is_installed("npm")
    rg_ok = _is_installed("rg")
    openclaude_ok = _is_installed("openclaude")
    claude_ok = _is_installed("claude")
    auth = _read_auth_status()
    authed = auth["authenticated"] and not auth["expired"]
    selected_tool = _get_selected_tool()

    return {
        "prerequisites": {
            "node": node_ok,
            "npm": npm_ok,
            "ripgrep": rg_ok,
        },
        "openclaude": {
            "installed": openclaude_ok,
            "authenticated": authed,
            "tier": auth["tier"],
        },
        "claude_code": {
            "installed": claude_ok,
            "authenticated": authed,
            "tier": auth["tier"],
        },
        "selected_tool": selected_tool,
        "ready": authed and selected_tool is not None,
    }


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
        )

        async for raw_line in process.stdout:
            line = _strip_ansi(raw_line.decode(errors="replace").strip())
            if line:
                yield _sse({"status": "output", "msg": line})

        await process.wait()

        if process.returncode == 0:
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


@setup_router.get("/setup/auth/{tool}")
async def start_auth(tool: str):
    if tool not in CLI_BINARIES:
        return StreamingResponse(
            _single_event({"status": "error", "msg": f"Unknown tool: {tool}"}),
            media_type="text/event-stream",
        )

    binary_path = _get_binary_path(CLI_BINARIES[tool])
    if not binary_path:
        return StreamingResponse(
            _single_event({
                "status": "error",
                "msg": f"{CLI_BINARIES[tool]} is not installed. Install it first.",
            }),
            media_type="text/event-stream",
        )

    async def auth_stream():
        yield _sse({"status": "starting", "msg": "Launching auth flow..."})

        creds_mtime_before = (
            CREDENTIALS_FILE.stat().st_mtime if CREDENTIALS_FILE.exists() else 0
        )

        master_fd, slave_fd = pty.openpty()

        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        process = await asyncio.create_subprocess_exec(
            binary_path,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            preexec_fn=os.setsid,
        )
        os.close(slave_fd)

        start_time = time.time()
        output_buf = ""
        browser_opened = False

        try:
            while True:
                if CREDENTIALS_FILE.exists():
                    mtime = CREDENTIALS_FILE.stat().st_mtime
                    if mtime != creds_mtime_before:
                        auth = _read_auth_status()
                        if auth["authenticated"] and not auth["expired"]:
                            # Persist the tool choice on successful auth
                            _set_selected_tool(tool)
                            yield _sse({
                                "status": "success",
                                "tier": auth["tier"],
                                "selected_tool": tool,
                            })
                            return

                elapsed = time.time() - start_time
                if elapsed > 180:
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

                        url_match = re.search(
                            r"https://[^\s\x00-\x1f]+claude\.ai[^\s\x00-\x1f]*",
                            output_buf,
                        )
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
# Helpers
# ---------------------------------------------------------------------------

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


async def _single_event(data: dict):
    yield _sse(data)