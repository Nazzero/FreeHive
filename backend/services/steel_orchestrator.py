"""
steel_orchestrator.py — FreeHive v0.6.0
Manages Steel browser session lifecycle for the Arena integration.

Steel Docker container (self-hosted):
    docker run -d --name steel-browser \
      -p 3000:3000 -p 9223:9223 \
      ghcr.io/steel-dev/steel-browser:latest

Users log in to arena.ai once via http://localhost:3000/ui.
The session (including cookies) persists in the Docker volume.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

STEEL_BASE_URL_DEFAULT = "http://localhost:3000"
SESSION_CACHE_PATH = Path.home() / ".freehive" / "steel_session.json"


class SteelOrchestrator:
    """
    Creates and manages a persistent Steel browser session.
    Persists session ID across backend restarts so the user doesn't need
    to re-authenticate to arena.ai.
    """

    def __init__(self, base_url: str | None = None):
        import os
        self.base_url = (
            base_url or os.getenv("STEEL_BASE_URL", STEEL_BASE_URL_DEFAULT)
        ).rstrip("/")
        self.proxy = os.getenv("STEEL_PROXY", "")
        self._session_id: str | None = None
        self._cdp_url: str | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def is_available(self) -> bool:
        """Check if the Steel Docker container is running."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{self.base_url}/v1/health")
                return resp.status_code < 500
        except Exception:
            return False

    async def get_or_create_session(self) -> tuple[str, str]:
        """
        Get an existing Steel session or create a new one.
        Returns (session_id, cdp_url).
        """
        # Attempt to restore from disk
        cached = self._load_cached_session()
        if cached:
            session_id, cdp_url = cached
            if await self._is_session_live(session_id):
                self._session_id = session_id
                self._cdp_url = cdp_url
                logger.info("[SteelOrchestrator] Restored session %s", session_id)
                return session_id, cdp_url
            logger.info("[SteelOrchestrator] Cached session dead, creating new one")

        return await self._create_session()

    async def release_session(self, session_id: str) -> None:
        """Release a Steel session (graceful shutdown)."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.delete(f"{self.base_url}/v1/sessions/{session_id}")
            SESSION_CACHE_PATH.unlink(missing_ok=True)
            logger.info("[SteelOrchestrator] Released session %s", session_id)
        except Exception as exc:
            logger.warning("[SteelOrchestrator] Failed to release session: %s", exc)

    def get_viewer_url(self) -> str:
        """URL to view the Steel browser session live (for login)."""
        return f"{self.base_url}/ui"

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_cached_session(self) -> tuple[str, str] | None:
        if not SESSION_CACHE_PATH.exists():
            return None
        try:
            data = json.loads(SESSION_CACHE_PATH.read_text())
            session_id = data.get("session_id", "")
            cdp_url = data.get("cdp_url", "")
            if session_id and cdp_url:
                return session_id, cdp_url
        except Exception:
            pass
        return None

    def _save_session(self, session_id: str, cdp_url: str) -> None:
        SESSION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        SESSION_CACHE_PATH.write_text(
            json.dumps({"session_id": session_id, "cdp_url": cdp_url})
        )

    async def _is_session_live(self, session_id: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self.base_url}/v1/sessions/{session_id}")
                if resp.status_code == 200:
                    data = resp.json()
                    status = str(data.get("status", "")).lower()
                    return status in ("live", "active", "running", "")
        except Exception:
            pass
        return False

    async def _create_session(self) -> tuple[str, str]:
        """Create a new Steel browser session."""
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{self.base_url}/v1/sessions",
                json={
                    "sessionTimeout": 86400000,  # 24 hours
                    **({"proxy": self.proxy} if self.proxy else {}),
                },
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Steel session creation failed ({resp.status_code}). "
                "Is the Steel Docker container running? "
                f"docker run -d -p 3000:3000 -p 9223:9223 ghcr.io/steel-dev/steel-browser:latest"
            )
        data = resp.json()

        session_id = str(
            data.get("id") or data.get("sessionId") or data.get("session_id") or ""
        ).strip()
        if not session_id:
            raise RuntimeError(f"Steel response missing session ID. Got: {data}")

        # CDP URL — Steel self-hosted returns websocketUrl like "ws://0.0.0.0:3000/"
        # Replace 0.0.0.0 with localhost so connections work from the host machine.
        raw_ws = str(
            data.get("websocketUrl") or data.get("wsUrl") or data.get("cdpUrl") or ""
        ).strip()
        if raw_ws.startswith("ws"):
            cdp_url = raw_ws.replace("0.0.0.0", "localhost")
        else:
            # Fallback: Steel's API runs on port 3000 and proxies CDP on the same port
            cdp_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")

        self._session_id = session_id
        self._cdp_url = cdp_url
        self._save_session(session_id, cdp_url)

        logger.info("[SteelOrchestrator] Created session %s, CDP: %s", session_id, cdp_url)
        return session_id, cdp_url
