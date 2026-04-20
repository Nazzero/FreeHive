import asyncio
import json
import logging
import random
import time
from pathlib import Path

import httpx

from backend.resilience.cli_introspection import get_claude_config
from backend.resilience.scrub_map import scrub_messages, scrub_blocks, load_scrub_map

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Concurrency cap — see original comment block for rationale
# --------------------------------------------------------------------------- #
_CLAUDE_REQUEST_SEM = asyncio.Semaphore(2)

CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
FREEHIVE_CONFIG_DIR = Path.home() / ".freehive"
DEFAULT_MODEL = "claude-haiku-4-5"


def _get_cli_config():
    """Get dynamically extracted CLI config with hardcoded fallbacks."""
    return get_claude_config()

CLAUDE_AI_OAUTH_SCOPES = [
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
]


class ClaudeDirectAdapter:
    """
    Calls api.anthropic.com/v1/messages directly using the OAuth token from
    ~/.claude/.credentials.json with the anthropic-beta: oauth-2025-04-20 header.
    Auto-refreshes the token when expired.

    All hardcoded values (Client ID, User-Agent, beta header, billing marker)
    are dynamically extracted from the installed CLI binary via cli_introspection.
    Falls back to last-known-good defaults if CLI not installed.
    """

    def __init__(self, model: str | None = None):
        self.conversation_history: list[dict] = []
        self._model = model or DEFAULT_MODEL
        self._cli_config = _get_cli_config()

    def load_history(self, history: list[dict]):
        """
        Rebuild internal history from DB messages.
        Called by SessionManager when the adapter's history is empty (fresh start or restart).
        history: list of {"role": "user"|"assistant", "content": str}
        """
        self.conversation_history = [
            {"role": m["role"], "content": m["content"]}
            for m in history
        ]

    def _read_oauth(self) -> dict:
        if not CREDENTIALS_FILE.exists():
            raise RuntimeError("Not authenticated. Open Setup and connect a Claude account.")
        try:
            data = json.loads(CREDENTIALS_FILE.read_text())
        except Exception:
            raise RuntimeError("Credentials file is corrupted. Re-authenticate in Setup.")
        oauth = data.get("claudeAiOauth", {})
        if not oauth.get("accessToken"):
            raise RuntimeError("No access token found. Re-authenticate in Setup.")
        return oauth

    async def _refresh_oauth_token(self, refresh_token: str) -> str:
        cfg = self._cli_config
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                cfg["token_url"],
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": cfg["client_id"],
                    "scope": " ".join(CLAUDE_AI_OAUTH_SCOPES),
                },
                headers={"Content-Type": "application/json"},
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"Token refresh failed ({response.status_code}). Re-authenticate in Setup."
            )
        data = response.json()
        access_token = data["access_token"]
        new_refresh_token = data.get("refresh_token", refresh_token)
        expires_at = int(time.time() * 1000) + data.get("expires_in", 3600) * 1000

        try:
            creds = json.loads(CREDENTIALS_FILE.read_text())
            creds["claudeAiOauth"]["accessToken"] = access_token
            creds["claudeAiOauth"]["refreshToken"] = new_refresh_token
            creds["claudeAiOauth"]["expiresAt"] = expires_at
            CREDENTIALS_FILE.write_text(json.dumps(creds, indent=2))
        except Exception:
            pass

        return access_token

    async def _get_token(self) -> str:
        oauth = self._read_oauth()
        access_token = oauth["accessToken"]
        refresh_token = oauth.get("refreshToken")
        expires_at = oauth.get("expiresAt", 0)

        buffer_ms = 5 * 60 * 1000
        if refresh_token and (time.time() * 1000 + buffer_ms) >= expires_at:
            access_token = await self._refresh_oauth_token(refresh_token)

        return access_token

    async def _call_api(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 8096,
        system: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
        thinking_effort: str = "off",
    ) -> dict:
        """
        Core API call — shared by send_message and raw_request.
        Returns the full Anthropic API response dict.
        """
        cfg = self._cli_config
        active_scrub_map = load_scrub_map()

        body: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": scrub_messages(messages, active_scrub_map),
        }
        # OAuth path requires a specific system-block layout that matches what
        # claude-cli sends: [billing marker, identity, ...caller blocks].
        # Values dynamically extracted from installed CLI binary.
        prefix_blocks = [
            {"type": "text", "text": cfg["billing_marker"]},
            {"type": "text", "text": cfg["identity"]},
        ]
        if system:
            if isinstance(system, list):
                caller_blocks = list(system)
            else:
                caller_blocks = [{"type": "text", "text": str(system)}]
            caller_blocks = scrub_blocks(caller_blocks, active_scrub_map)
            first_text = ""
            for blk in caller_blocks:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    first_text = blk.get("text", "")
                    break
            if first_text.startswith("x-anthropic-billing-header"):
                body["system"] = caller_blocks
            else:
                body["system"] = [*prefix_blocks, *caller_blocks]
        else:
            body["system"] = prefix_blocks
        if tools:
            body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice

        # ── Thinking / extended reasoning ──
        from backend.thinking import claude_thinking_params, EFFORT_OFF
        beta_header = cfg["beta_header"]
        if thinking_effort and thinking_effort != EFFORT_OFF:
            beta_addition, thinking_param = claude_thinking_params(thinking_effort)
            if thinking_param:
                body["thinking"] = thinking_param
                budget = thinking_param.get("budget_tokens", 0)
                if body["max_tokens"] < budget + 4096:
                    body["max_tokens"] = budget + 4096
            if beta_addition:
                beta_header = cfg["beta_header"] + "," + beta_addition

        token = await self._get_token()
        MAX_RETRIES = 3

        # Anti-fingerprint: small random jitter before each request
        await asyncio.sleep(random.uniform(0.05, 0.2))

        async with _CLAUDE_REQUEST_SEM:
            for rate_attempt in range(MAX_RETRIES + 1):
                for auth_attempt in range(2):
                    if auth_attempt == 1:
                        oauth = self._read_oauth()
                        refresh_token = oauth.get("refreshToken")
                        if refresh_token:
                            token = await self._refresh_oauth_token(refresh_token)
                        else:
                            raise RuntimeError("Claude session expired. Re-authenticate in Setup.")

                    async with httpx.AsyncClient(timeout=120.0) as client:
                        response = await client.post(
                            cfg["messages_url"],
                            headers={
                                "Authorization": f"Bearer {token}",
                                "anthropic-version": "2023-06-01",
                                "anthropic-beta": beta_header,
                                "anthropic-dangerous-direct-browser-access": "true",
                                "x-app": "cli",
                                "User-Agent": cfg["user_agent"],
                                "content-type": "application/json",
                                "accept": "application/json",
                            },
                            json=body,
                        )

                    if response.status_code == 401 and auth_attempt == 0:
                        continue
                    if response.status_code == 429:
                        break  # exit auth loop, handle rate limit below
                    # Thinking beta rejected — retry without thinking
                    if (
                        response.status_code == 400
                        and "thinking" in body
                        and ("beta" in response.text.lower() or "not yet available" in response.text.lower())
                    ):
                        logger.warning(
                            "[claude_adapter] Thinking beta rejected by API — "
                            "retrying without extended thinking"
                        )
                        body.pop("thinking", None)
                        beta_header = cfg["beta_header"]
                        continue
                    if response.status_code != 200:
                        raise RuntimeError(
                            f"API error {response.status_code}: {response.text[:200]}"
                        )
                    return response.json()

                if response.status_code == 429:
                    if rate_attempt < MAX_RETRIES:
                        # Always honour Retry-After — Anthropic sets it on every 429
                        retry_after = (
                            response.headers.get("retry-after")
                            or response.headers.get("x-ratelimit-reset-requests")
                        )
                        try:
                            delay = max(1, int(float(retry_after)))
                        except (TypeError, ValueError):
                            # Exponential backoff if header missing: 10s, 20s, 40s
                            delay = 10 * (2 ** rate_attempt)
                        logger.warning(
                            f"[claude_adapter] Rate limited (429) — retrying in {delay}s "
                            f"(attempt {rate_attempt + 1}/{MAX_RETRIES})"
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise RuntimeError(
                        "Claude is rate limited right now — too many requests sharing the same "
                        "OAuth token (FreeHive + Claude Code running simultaneously). "
                        "Wait 30–60 seconds, or pause Claude Code while using FreeHive."
                    )

            raise RuntimeError("Claude session expired. Re-authenticate in Setup.")

    async def raw_request(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 8096,
        system: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
        thinking_effort: str = "off",
    ) -> dict:
        """
        Pass-through for the compat layer — returns the full API response dict.
        Does not touch self.conversation_history (the client owns state).
        Supports tools, tool_choice, thinking_effort, and multi-content responses.
        """
        return await self._call_api(
            messages,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            tool_choice=tool_choice,
            thinking_effort=thinking_effort,
        )

    async def send_message(self, message: str, history: list[dict] = None) -> str:
        # If adapter history is empty and DB history is provided, rebuild it
        if not self.conversation_history and history:
            self.load_history(history)

        self.conversation_history.append({"role": "user", "content": message})

        result = await self._call_api(self.conversation_history)

        # Extract text from first text block (internal chat — no tool_use expected)
        content = result.get("content", [])
        assistant_text = next(
            (b["text"] for b in content if b.get("type") == "text"),
            "",
        )
        self.conversation_history.append(
            {"role": "assistant", "content": assistant_text}
        )
        return assistant_text

    def clear_history(self):
        self.conversation_history = []