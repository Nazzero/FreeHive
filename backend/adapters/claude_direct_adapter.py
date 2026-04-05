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
    ~/.claude/.credentials.json with the anthropic-beta: oauth-2025-04-20 header
    that enables OAuth Bearer auth on the messages endpoint.
    Auto-refreshes the token when expired.
    """

    def __init__(self):
        self.conversation_history: list[dict] = []

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
        """Exchange refresh token for a fresh access token and update credentials file."""
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
            pass  # non-fatal

        return access_token

    async def _get_token(self) -> str:
        """Return a valid access token, refreshing if within 5 min of expiry."""
        oauth = self._read_oauth()
        access_token = oauth["accessToken"]
        refresh_token = oauth.get("refreshToken")
        expires_at = oauth.get("expiresAt", 0)

        buffer_ms = 5 * 60 * 1000
        if refresh_token and (time.time() * 1000 + buffer_ms) >= expires_at:
            access_token = await self._refresh_oauth_token(refresh_token)

        return access_token

    async def send_message(self, message: str) -> str:
        self.conversation_history.append({"role": "user", "content": message})

        for attempt in range(2):
            # On second attempt force a token refresh in case it just expired
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
                    json={
                        "model": DEFAULT_MODEL,
                        "max_tokens": 8096,
                        "messages": self.conversation_history,
                    },
                )

            if response.status_code == 401 and attempt == 0:
                continue  # retry with fresh token
            if response.status_code == 429:
                raise RuntimeError("Rate limited. Wait a moment and try again.")
            if response.status_code != 200:
                raise RuntimeError(
                    f"API error {response.status_code}: {response.text[:200]}"
                )

            assistant_text = response.json()["content"][0]["text"]
            self.conversation_history.append(
                {"role": "assistant", "content": assistant_text}
            )
            return assistant_text

        raise RuntimeError("Claude session expired. Re-authenticate in Setup.")

    def clear_history(self):
        self.conversation_history = []