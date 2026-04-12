# FreeHive — Gemini Direct API Adapter Handoff

## Goal

Build `GeminiDirectAdapter` that calls Google's Gemini API directly using OAuth tokens
from the Gemini CLI — same pattern as `ClaudeDirectAdapter` but for Gemini.
No subprocess, no web scraping, no Playwright. Direct REST API call with OAuth Bearer token.

---

## Why This Is Easier Than Claude

Claude required:
- Reverse engineering a minified 19MB bundle (openclaude)
- mitmproxy traffic interception to find the beta header
- Discovering `anthropic-beta: oauth-2025-04-20` through trial and error

Gemini CLI is:
- Fully open source on GitHub: https://github.com/google-gemini/gemini-cli
- Apache 2.0 license — read the source directly
- No grepping minified bundles needed
- Endpoint and auth headers readable straight from source code

---

## Credentials File

After user runs `gemini auth login`, tokens are cached at:

```
~/.gemini/tokens.json
```

Structure (from source `packages/core/src/code_assist/oauth2.ts`):
```json
{
  "access_token": "...",
  "refresh_token": "...",
  "expiry_date": 1234567890000,
  "token_type": "Bearer",
  "id_token": "..."
}
```

This is standard Google OAuth2 token format.

---

## Free Tier

- **60 requests/minute**
- **1,000 requests/day**
- Personal Google account — no subscription needed
- Access to Gemini 2.5 Pro and Flash
- **1 million token context window**

---

## How to Find the Exact Endpoint and Headers

Since Gemini CLI is open source, read the source directly:

```bash
# Install Gemini CLI
npm install -g @google/gemini-cli

# Authenticate
gemini auth login

# Find the API call in source
find $(npm root -g)/@google/gemini-cli -name "*.js" | xargs grep -l "generateContent\|v1beta\|generativelanguage" 2>/dev/null | head -5
```

Or read directly on GitHub:
- `packages/core/src/code_assist/oauth2.ts` — token management
- `packages/core/src/core/contentGenerator.ts` — API call construction
- Look for `generativelanguage.googleapis.com` or `aiplatform.googleapis.com`

Expected endpoint based on Google's public Gemini API docs:
```
POST https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent
Authorization: Bearer <access_token>
Content-Type: application/json
```

**Verify this by checking the actual source before building.**

---

## Expected Request Format

Google's Gemini API uses a different format from Anthropic's:

```json
{
  "contents": [
    {"role": "user", "parts": [{"text": "hello"}]},
    {"role": "model", "parts": [{"text": "Hi there!"}]},
    {"role": "user", "parts": [{"text": "how are you?"}]}
  ],
  "generationConfig": {
    "maxOutputTokens": 8192
  }
}
```

Note differences from Claude:
- `contents` not `messages`
- `parts: [{text: "..."}]` not just `"content": "..."`
- Assistant role is `model` not `assistant`
- Response is in `candidates[0].content.parts[0].text`

---

## Token Refresh

Google OAuth2 refresh endpoint:
```
POST https://oauth2.googleapis.com/token
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token
&refresh_token=<refresh_token>
&client_id=<CLIENT_ID>
&client_secret=<CLIENT_SECRET>
```

Find the `CLIENT_ID` and `CLIENT_SECRET` in the Gemini CLI source:
```bash
grep -r "client_id\|client_secret\|CLIENT_ID" $(npm root -g)/@google/gemini-cli --include="*.js" | grep -v node_modules | head -20
```

From DeepWiki source analysis: client credentials are in
`packages/core/src/code_assist/oauth2.ts` lines 72-81.

---

## Step by Step — What to Do

### Step 1: Install and authenticate
```bash
npm install -g @google/gemini-cli
gemini auth login
cat ~/.gemini/tokens.json  # verify structure
```

### Step 2: Find exact endpoint from source
```bash
# Find the bundle
ls $(npm root -g)/@google/gemini-cli/dist/ 2>/dev/null || \
find $(npm root -g)/@google/gemini-cli -name "*.js" -size +1M | head -5

# Grep for API endpoint
grep -o 'https://[a-zA-Z0-9._/v-]*generateContent[a-zA-Z0-9._/v-]*' <bundle_path>

# Grep for any special headers
grep -o 'x-goog[a-zA-Z-]*' <bundle_path> | sort -u
```

### Step 3: Test raw curl
```bash
TOKEN=$(python3 -c "import json; print(json.load(open('/root/.gemini/tokens.json'))['access_token'])")

curl -s -X POST \
  "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
    "generationConfig": {"maxOutputTokens": 100}
  }' | python3 -m json.tool
```

If that works — build the adapter.
If 401 — check for required extra headers from source (same as `anthropic-beta` for Claude).

### Step 4: Build the adapter

---

## Adapter Implementation Plan

```python
# backend/adapters/gemini_direct_adapter.py

import json
import time
from pathlib import Path
import httpx

TOKENS_FILE = Path.home() / ".gemini" / "tokens.json"
REFRESH_URL = "https://oauth2.googleapis.com/token"
MESSAGES_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-2.5-pro"

class GeminiDirectAdapter:
    def __init__(self):
        self.conversation_history = []  # Google format: [{role, parts}]

    def _read_tokens(self) -> dict:
        if not TOKENS_FILE.exists():
            raise RuntimeError("Not authenticated. Run: gemini auth login")
        try:
            return json.loads(TOKENS_FILE.read_text())
        except Exception:
            raise RuntimeError("Tokens file corrupted. Re-authenticate.")

    def _is_expired(self, tokens: dict) -> bool:
        expiry = tokens.get("expiry_date", 0)
        buffer_ms = 5 * 60 * 1000
        return (time.time() * 1000 + buffer_ms) >= expiry

    async def _refresh_token(self, refresh_token: str) -> str:
        # Need CLIENT_ID and CLIENT_SECRET from Gemini CLI source
        # Find them in: packages/core/src/code_assist/oauth2.ts
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                REFRESH_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": "<CLIENT_ID_FROM_SOURCE>",
                    "client_secret": "<CLIENT_SECRET_FROM_SOURCE>",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if response.status_code != 200:
            raise RuntimeError(f"Token refresh failed ({response.status_code})")
        data = response.json()
        access_token = data["access_token"]
        # Update tokens file
        try:
            tokens = json.loads(TOKENS_FILE.read_text())
            tokens["access_token"] = access_token
            tokens["expiry_date"] = int(time.time() * 1000) + data.get("expires_in", 3600) * 1000
            TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
        except Exception:
            pass
        return access_token

    async def _get_token(self) -> str:
        tokens = self._read_tokens()
        if self._is_expired(tokens) and tokens.get("refresh_token"):
            return await self._refresh_token(tokens["refresh_token"])
        return tokens["access_token"]

    def _to_google_format(self) -> list:
        """Convert conversation history to Google's contents format."""
        contents = []
        for msg in self.conversation_history:
            role = "model" if msg["role"] == "assistant" else "user"
            contents.append({
                "role": role,
                "parts": [{"text": msg["content"]}]
            })
        return contents

    async def send_message(self, message: str) -> str:
        self.conversation_history.append({"role": "user", "content": message})

        for attempt in range(2):
            token = await self._get_token()
            if attempt == 1:
                # Force refresh on retry
                tokens = self._read_tokens()
                if tokens.get("refresh_token"):
                    token = await self._refresh_token(tokens["refresh_token"])

            url = MESSAGES_URL.format(model=DEFAULT_MODEL)
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "contents": self._to_google_format(),
                        "generationConfig": {"maxOutputTokens": 8192},
                    },
                )

            if response.status_code == 401 and attempt == 0:
                continue
            if response.status_code == 429:
                raise RuntimeError("Rate limited. Wait a moment and try again.")
            if response.status_code != 200:
                raise RuntimeError(f"API error {response.status_code}: {response.text[:200]}")

            data = response.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            self.conversation_history.append({"role": "assistant", "content": text})
            return text

        raise RuntimeError("Gemini session expired. Run: gemini auth login")

    def clear_history(self):
        self.conversation_history = []
```

---

## SessionManager Update

Add to `backend/session_manager.py`:

```python
from backend.adapters.gemini_direct_adapter import GeminiDirectAdapter

# In _get_adapter():
elif model == "gemini":
    self._adapters[model] = GeminiDirectAdapter()

# In _available():
def _available(self) -> list[str]:
    return ["claude", "gemini", "chatgpt"]
```

---

## Setup Screen Update

Add Gemini as a third tool option in `SetupScreen.svelte` and `setup_router.py`:

- Install: `npm install -g @google/gemini-cli`
- Auth: `gemini auth login` (opens browser, writes `~/.gemini/tokens.json`)
- Binary to check: `gemini`
- Credential file: `~/.gemini/tokens.json`

Add to `INSTALL_COMMANDS` in `setup_router.py`:
```python
"gemini_cli": "npm install -g @google/gemini-cli"
```

Add to `CLI_BINARIES`:
```python
"gemini_cli": "gemini"
```

---

## Frontend Update

In `+page.svelte` sidebar, replace the "Gemini — soon" button with an active model button once the adapter is confirmed working.

---

## Key Unknown — Resolve First

**The CLIENT_ID and CLIENT_SECRET for token refresh.**

These are embedded in the Gemini CLI source at `packages/core/src/code_assist/oauth2.ts` lines 72-81. Read them from the installed bundle before writing the refresh function. Without these, token refresh won't work and sessions will expire after ~1 hour.

If the tokens file is managed by Gemini CLI itself (auto-refreshes when you use `gemini` normally), we may not need to implement refresh at all — same reasoning as Claude where we let openclaude manage the refresh cycle. Test this first:

1. Let the token expire naturally
2. Run `gemini -p "test"` once — this refreshes the token
3. FreeHive reads the refreshed token from `~/.gemini/tokens.json`

If that works — skip implementing refresh entirely, same as we did with Claude initially.

---

## Reference — How Claude's Adapter Was Built

1. Found `~/.claude/.credentials.json` credential file
2. Tried `Authorization: Bearer <token>` → got "OAuth not supported"
3. Grepped openclaude bundle for beta headers → found `oauth-2025-04-20`
4. Added `anthropic-beta: oauth-2025-04-20` header → worked
5. Implemented refresh via `platform.claude.com/v1/oauth/token`

For Gemini:
1. Find `~/.gemini/tokens.json` ✅ already known
2. Try `Authorization: Bearer <token>` → likely works without special headers (public API)
3. If fails → grep Gemini CLI bundle for special headers
4. Implement refresh via `oauth2.googleapis.com/token` with CLIENT_ID from source

Gemini should be faster to build since step 3 probably isn't needed.

---

## Priority

Build this **before** ChatGPT adapter. Gemini is:
- Direct API (fast, clean)
- Free tier with generous limits
- 1M token context window (best of all three)
- Open source CLI (no reverse engineering needed)
- Private — Google's standard API terms apply

Estimated build time: 2-3 hours once CLIENT_ID is confirmed from source.