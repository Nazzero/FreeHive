"""
arena_manager.py — FreeHive v0.5.1
Manages the ArenaPlaywrightAdapter lifecycle.
Legacy LMArenaBridge code kept below as dead code — do not delete.
"""

import asyncio
import logging
import subprocess
from pathlib import Path

from backend.services.arena_bridge_transport import is_bridge_available

logger = logging.getLogger(__name__)

ARENA_PROFILE_DIR = Path.home() / ".freehive" / "arena_profile"
CDP_URL = "http://localhost:9222"


# ===========================================================================
# NEW: Playwright-based Arena manager
# ===========================================================================

class ArenaManager:
    """
    Manages the Arena Extension Bridge.
    Called by router.py for /arena/* endpoints.
    """

    def __init__(self):
        from backend.adapters.arena_bridge_adapter import ArenaBridgeAdapter
        self._adapter = ArenaBridgeAdapter()
        self._lock = asyncio.Lock()
        self._running = True # Extension-based bridge is considered "running" if backend can talk to it

    async def is_logged_in(self) -> bool:
        """
        Check if the extension bridge is active and responsive.
        """
        return is_bridge_available()

    async def start(self, force_login: bool = False) -> dict:
        """
        In the extension model, 'starting' is just ensuring the browser is open.
        If force_login is true, we can try to open the Arena tab.
        """
        async with self._lock:
            # We assume Chrome is managed by the user or already running
            # In the future, we could launch Chrome here if missing.
            is_active = await self.is_logged_in()
            return {
                "status": "active" if is_active else "bridge_missing",
                "bridge": True,
                "login_required": not is_active, # TBD: real login check via bridge
            }

    async def stop(self):
        """No-op for extension-based bridge."""
        logger.info("[ArenaManager] Stop requested (no-op for extension bridge)")

    async def get_models(self) -> list[str]:
        """Fetch models from bridge; fallback to static list if unavailable."""
        if not self.get_status().get("bridge_active", False):
            return _fallback_models()
        try:
            models = await self._adapter.fetch_models()
            return models
        except Exception as e:
            logger.warning("[ArenaManager] Failed to fetch live models, using fallback: %s", e)
        return _fallback_models()

    def get_adapter(self):
        """Return the live bridge adapter instance."""
        return self._adapter

    def get_status(self) -> dict:
        bridge_active = is_bridge_available()
        return {
            "running": True,
            "bridge_active": bridge_active,
            "profile_present": True,
            "login_in_progress": False,
        }


def _fallback_models() -> list[str]:
    return [
        "arena/gpt-5.2-chat-latest",
        "arena/gpt-4o",
        "arena/gpt-4.5-preview",
        "arena/gemini-3.1-pro",
        "arena/gemini-2.0-flash-001",
        "arena/gemini-2.5-pro-preview-03-25",
        "arena/claude-opus-4-6",
        "arena/claude-opus-4-6-thinking",
        "arena/claude-3-7-sonnet-20250219",
        "arena/claude-3-5-sonnet-20241022",
        "arena/grok-3-beta",
        "arena/grok-3-mini-beta",
        "arena/deepseek-v3-0324",
        "arena/deepseek-r1",
        "arena/llama-4-maverick",
        "arena/llama-4-scout",
        "arena/mistral-large-2411",
        "arena/qwen-max-2025-01-25",
    ]


# ===========================================================================
# DEAD CODE — Legacy LMArenaBridge process manager
# Do not delete. Not actively used in v0.5.1+.
# ===========================================================================

class _LegacyLMArenaBridgeManager:
    """
    LEGACY — Managed the LMArenaBridge subprocess that called arena.ai.
    Broken as of early 2025 due to arena.ai domain migration + bot detection.
    Kept for reference only.
    """

    def __init__(self):
        self.process = None
        self.bridge_path = Path.home() / "Ilee_AI" / "LMArenaBridge" / "src" / "main.py"

    def start(self):
        if self.process and self.process.poll() is None:
            return {"status": "already_running"}
        try:
            self.process = subprocess.Popen(
                ["python", str(self.bridge_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            return {"status": "started", "pid": self.process.pid}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def stop(self):
        if self.process:
            self.process.terminate()
            self.process = None
        return {"status": "stopped"}

    def is_running(self):
        return self.process is not None and self.process.poll() is None
