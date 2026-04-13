import json
import time
from pathlib import Path

import httpx

CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
FREEHIVE_CONFIG_DIR = Path.home() / ".freehive"

TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
DEFAULT_MODEL = "claude-haiku-4-5"

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
    History is rebuilt from DB on each session resume.
    """

    def __init__(self, model: str | None = None):
        self.conversation_history: list[dict] = []
        self._model = model or DEFAULT_MODEL

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
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                TOKEN_URL,
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CLIENT_ID,
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
    ) -> dict:
        """
        Core API call — shared by send_message and raw_request.
        Returns the full Anthropic API response dict.
        """
        body: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice

        for attempt in range(2):
            if attempt == 1:
                oauth = self._read_oauth()
                refresh_token = oauth.get("refreshToken")
                if refresh_token:
                    token = await self._refresh_oauth_token(refresh_token)
                else:
                    raise RuntimeError("Claude session expired. Re-authenticate in Setup.")
            else:
                token = await self._get_token()

            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    MESSAGES_URL,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "anthropic-version": "2023-06-01",
                        "anthropic-beta": "oauth-2025-04-20",
                        "content-type": "application/json",
                    },
                    json=body,
                )

            if response.status_code == 401 and attempt == 0:
                continue
            if response.status_code == 429:
                raise RuntimeError("Rate limited. Wait a moment and try again.")
            if response.status_code != 200:
                raise RuntimeError(
                    f"API error {response.status_code}: {response.text[:200]}"
                )
            return response.json()

        raise RuntimeError("Claude session expired. Re-authenticate in Setup.")

    async def raw_request(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 8096,
        system: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
    ) -> dict:
        """
        Pass-through for the compat layer — returns the full API response dict.
        Does not touch self.conversation_history (the client owns state).
        Supports tools, tool_choice, and multi-content responses.
        """
        return await self._call_api(
            messages,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            tool_choice=tool_choice,
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