"""
stealth_orchestrator.py — CloakBrowser lifecycle manager for arena.ai

Replaces SteelOrchestrator.  Instead of connecting to a Docker container
via REST + CDP, we launch a local CloakBrowser (patched Chromium) process
with a persistent profile.

Returns a Playwright BrowserContext directly — all existing Playwright
code in the adapter works unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

PROFILE_DIR = Path.home() / ".freehive" / "arena_profile"


class StealthOrchestrator:
    """Manage a CloakBrowser persistent context for arena.ai automation."""

    def __init__(self):
        proxy_env = os.getenv("ARENA_PROXY", "").strip()
        self._proxy: str | None = proxy_env or None
        # Default to headless — CloakBrowser gets 0.9 reCAPTCHA score in both modes.
        # Set ARENA_HEADED=1 to see the browser window (useful for debugging/login).
        self._headless = os.getenv("ARENA_HEADED", "").strip() not in ("1", "true", "yes")
        self._profile_dir = PROFILE_DIR
        self._context = None
        self._profile_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_or_create_context(self):
        """Return a live Playwright BrowserContext, launching if needed."""
        if self._context is not None:
            if await self._is_alive():
                return self._context
            # Dead context — clean up and relaunch
            logger.info("[StealthOrchestrator] Context dead, relaunching...")
            await self._close_context()

        from cloakbrowser import ensure_binary, launch_persistent_context_async

        # Ensure Chromium binary is downloaded
        binary_path = ensure_binary()
        logger.info("[StealthOrchestrator] CloakBrowser binary: %s", binary_path)

        headless = self._headless
        if not headless and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            logger.warning(
                "[StealthOrchestrator] ARENA_HEADED=1 but no DISPLAY set — "
                "falling back to headless. Run with a display server or Xvfb for headed mode."
            )
            headless = True

        self._context = await launch_persistent_context_async(
            user_data_dir=str(self._profile_dir),
            headless=headless,
            proxy=self._proxy,
            stealth_args=True,
            humanize=True,
            human_preset="default",
            viewport={"width": 1920, "height": 1080},
        )
        logger.info("[StealthOrchestrator] CloakBrowser context created (headless=%s)", headless)
        return self._context

    async def is_available(self) -> bool:
        """Check if CloakBrowser binary is installed (or can be downloaded)."""
        try:
            from cloakbrowser import binary_info
            info = binary_info()
            return info.get("installed", False)
        except Exception:
            return False

    async def close(self):
        """Shut down the CloakBrowser context and Chromium process."""
        await self._close_context()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _is_alive(self) -> bool:
        """Check if the cached context is still responsive."""
        try:
            pages = self._context.pages
            if pages:
                await pages[0].evaluate("() => 1")
                return True
            # No pages but context alive — still good
            return True
        except Exception:
            return False

    async def _close_context(self):
        """Gracefully close the context."""
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None

