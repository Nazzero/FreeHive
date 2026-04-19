"""
arena_manager.py — FreeHive v0.7.1
Manages Arena adapters: Extension Bridge (primary) + CloakBrowser (fallback).
"""

import asyncio
import logging
import platform
import subprocess

logger = logging.getLogger(__name__)


def _kill_browser_processes():
    """Kill stale CloakBrowser / Chromium processes (cross-platform)."""
    try:
        if platform.system() == "Windows":
            for proc in ("chromium.exe", "chrome.exe"):
                subprocess.run(
                    ["taskkill", "/F", "/IM", proc],
                    capture_output=True, timeout=5,
                )
        else:
            subprocess.run(
                ["pkill", "-f", "cloakbrowser|chromium-146"],
                capture_output=True, timeout=3,
            )
    except Exception:
        pass


class ArenaManager:
    """
    Lifecycle manager for the arena.ai adapter.

    Transport priority:
      1. Extension Bridge — Chrome extension + native messaging (fast, reliable)
      2. CloakBrowser     — Stealth Chromium automation (fallback)

    Setup (extension, recommended):
        ./scripts/setup_arena_bridge.sh
        Load extension in Chrome, open arena.ai tab, log in.

    Setup (CloakBrowser, fallback):
        pip install cloakbrowser>=0.3.25
        Start backend → CloakBrowser Chrome window appears.
        Sign in to arena.ai via Google OAuth in the browser window.
    """

    def __init__(self):
        # Extension bridge adapter (primary)
        from backend.adapters.arena_bridge_adapter import ArenaBridgeAdapter
        self._bridge_adapter = ArenaBridgeAdapter()

        # CloakBrowser adapter (fallback) — lazy init to avoid import errors
        self._cloakbrowser_adapter = None
        try:
            from backend.adapters.arena_steel_adapter import ArenaSteelAdapter
            self._cloakbrowser_adapter = ArenaSteelAdapter()
        except Exception as exc:
            logger.info("[ArenaManager] CloakBrowser fallback not available: %s", exc)

        self._lock = asyncio.Lock()
        self._login_session_active = False

    # ------------------------------------------------------------------
    # Transport detection
    # ------------------------------------------------------------------

    def _is_bridge_available(self) -> bool:
        from backend.services.arena_bridge_transport import is_bridge_available
        return is_bridge_available()

    @property
    def active_transport(self) -> str:
        """Return which transport is currently active."""
        if self._is_bridge_available():
            return "extension"
        if self._cloakbrowser_adapter is not None:
            return "cloakbrowser"
        return "offline"

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        transport = self.active_transport
        return {
            "running": transport != "offline",
            "bridge_active": self._is_bridge_available(),
            "cloakbrowser_available": self._cloakbrowser_adapter is not None,
            "transport": transport,
            "profile_present": True,
            "login_in_progress": False,
        }

    async def is_logged_in(self) -> bool:
        """Return True when any transport is available and authenticated."""
        if self._is_bridge_available():
            return True  # Extension in user's Chrome = already logged in
        try:
            if self._cloakbrowser_adapter and await self._cloakbrowser_adapter.is_available():
                return await self._cloakbrowser_adapter.is_authenticated()
        except Exception as exc:
            logger.debug("[ArenaManager] is_logged_in check failed: %s", exc)
        return False

    # ------------------------------------------------------------------
    # Lifecycle (kept for router.py compatibility)
    # ------------------------------------------------------------------

    async def start(self, force_login: bool = False) -> dict:
        """Check transport availability and return status.

        Extension bridge: just needs Chrome with extension + arena.ai tab.
        CloakBrowser fallback: manages its own browser lifecycle.
        """
        async with self._lock:
            # Extension bridge is the primary transport
            if self._is_bridge_available():
                return {
                    "status": "active",
                    "transport": "extension",
                    "login_required": False,
                    "message": None,
                }

            # CloakBrowser fallback
            if self._cloakbrowser_adapter is not None:
                try:
                    available = await self._cloakbrowser_adapter.is_available()
                except Exception:
                    available = False

                if available:
                    if force_login:
                        try:
                            await self._cloakbrowser_adapter.close()
                            _kill_browser_processes()
                            from pathlib import Path
                            profile = Path.home() / ".freehive" / "arena_profile"
                            for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                                (profile / lock).unlink(missing_ok=True)

                            orchestrator = self._cloakbrowser_adapter._orchestrator
                            orchestrator._headless = False
                            ctx = await orchestrator.get_or_create_context()
                            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                            await page.goto("https://arena.ai/text/direct", wait_until="load", timeout=30000)
                            self._login_session_active = True
                            return {
                                "status": "login_opened",
                                "transport": "cloakbrowser",
                                "login_required": True,
                                "message": "Browser window opened. Sign in, then refresh.",
                            }
                        except Exception as exc:
                            return {
                                "status": "login_failed",
                                "transport": "cloakbrowser",
                                "login_required": True,
                                "message": f"Could not open browser: {exc}",
                            }

                    logged_in = await self._cloakbrowser_adapter.is_authenticated()
                    return {
                        "status": "active" if logged_in else "login_required",
                        "transport": "cloakbrowser",
                        "login_required": not logged_in,
                        "message": None if logged_in else "Log in to arena.ai in CloakBrowser window.",
                    }

            # Nothing available
            return {
                "status": "offline",
                "transport": "offline",
                "login_required": True,
                "message": (
                    "Arena is not available. Install the Chrome extension "
                    "(./scripts/setup_arena_bridge.sh) or CloakBrowser "
                    "(pip install cloakbrowser>=0.3.25)."
                ),
            }

    async def stop(self):
        """Disconnect from CloakBrowser."""
        try:
            await self._adapter.close()
        except Exception as exc:
            logger.debug("[ArenaManager] stop: %s", exc)

    # ------------------------------------------------------------------
    # Models
    # ------------------------------------------------------------------

    async def get_models(self) -> list[str]:
        """Fetch live model list. Extension bridge first, CloakBrowser fallback."""
        if self._is_bridge_available():
            try:
                return await self._bridge_adapter.fetch_models()
            except Exception as exc:
                logger.warning("[ArenaManager] Extension bridge get_models failed: %s", exc)

        if self._cloakbrowser_adapter is not None:
            try:
                if await self._cloakbrowser_adapter.is_available():
                    return await self._cloakbrowser_adapter.fetch_models()
            except Exception as exc:
                logger.warning("[ArenaManager] CloakBrowser get_models failed: %s", exc)
        return []

    # ------------------------------------------------------------------
    # Adapter access (used by session_manager.py)
    # ------------------------------------------------------------------

    def get_adapter(self):
        """Return the best available adapter: extension first, CloakBrowser fallback."""
        if self._is_bridge_available():
            return self._bridge_adapter
        if self._cloakbrowser_adapter is not None:
            return self._cloakbrowser_adapter
        return None


# ---------------------------------------------------------------------------
# Fallback model list
# ---------------------------------------------------------------------------

def _fallback_models() -> list[str]:
    return []  # No guessing — slugs must come from the live arena.ai page
