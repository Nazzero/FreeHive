"""
GeminiDirectAdapter — FreeHive v0.5.3

Calls Gemini via the cloudcode-pa.googleapis.com Code Assist endpoint using
OAuth tokens from the Gemini CLI (~/.gemini/oauth_creds.json).

No API key required. Free tier: 60 req/min, 1000 req/day.
Token refresh is handled automatically.

Discovered by proxying the Gemini CLI:
  Endpoint: https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse
  Project:  fetched from loadCodeAssist → cloudaicompanionProject

Tool use: the Code Assist endpoint wraps a standard Gemini API request body.
tools and tool_config go inside the request object. raw_request() accepts
Chat Completions format and converts to/from Gemini format so OpenAI-compatible
clients with function calling work correctly.
"""

import json
import time
import uuid
from pathlib import Path

import httpx

# Credentials written by `gemini auth login`
TOKENS_FILE = Path.home() / ".gemini" / "oauth_creds.json"

# Google OAuth2 refresh endpoint
REFRESH_URL = "https://oauth2.googleapis.com/token"

# Code Assist endpoint (discovered by proxying Gemini CLI traffic)
CODE_ASSIST_BASE = "https://cloudcode-pa.googleapis.com/v1internal"
GENERATE_URL = f"{CODE_ASSIST_BASE}:streamGenerateContent?alt=sse"
LOAD_CODE_ASSIST_URL = f"{CODE_ASSIST_BASE}:loadCodeAssist"

# Client credentials from Gemini CLI bundle (oauth2.ts / packages/core/src/code_assist/)
_CLIENT_ID = "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"
_CLIENT_SECRET = "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"

DEFAULT_MODEL = "gemini-3-flash-preview"
_EXPIRY_BUFFER_MS = 5 * 60 * 1000


# ---------------------------------------------------------------------------
# Format converters: Chat Completions ↔ Gemini Code Assist
# ---------------------------------------------------------------------------

def _convert_messages_to_gemini(messages: list[dict]) -> tuple[str | None, list]:
    """
    Convert Chat Completions messages to (system_instruction_text, gemini_contents).

    System messages → system_instruction (Gemini doesn't use a system role in contents).
    tool role messages → functionResponse parts (need function name, recovered from prior
    assistant tool_calls via a call_id→name map built during conversion).
    """
    system_text = None
    contents = []
    call_id_to_name: dict[str, str] = {}

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content")

        if role == "system":
            if content:
                system_text = content if isinstance(content, str) else str(content)
            continue

        if role == "user":
            if isinstance(content, list):
                text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
            else:
                text = content or ""
            contents.append({"role": "user", "parts": [{"text": text}]})

        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                parts = []
                if content:
                    parts.append({"text": content})
                for tc in tool_calls:
                    name = tc["function"]["name"]
                    call_id_to_name[tc["id"]] = name
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    parts.append({"functionCall": {"name": name, "args": args}})
                contents.append({"role": "model", "parts": parts})
            else:
                contents.append({"role": "model", "parts": [{"text": content or ""}]})

        elif role == "tool":
            call_id = msg.get("tool_call_id", "")
            # Recover function name from the prior assistant tool_calls mapping
            func_name = call_id_to_name.get(call_id, call_id)
            result_content = content if isinstance(content, str) else json.dumps(content)
            try:
                result_data = json.loads(result_content)
            except (json.JSONDecodeError, TypeError):
                result_data = {"result": result_content}
            contents.append({
                "role": "user",
                "parts": [{"functionResponse": {"name": func_name, "response": result_data}}],
            })

    return system_text, contents


def _convert_tools_to_gemini(tools: list[dict]) -> list:
    """Convert Chat Completions tool definitions to Gemini function_declarations format."""
    function_declarations = []
    for t in tools:
        if t.get("type") == "function":
            f = t["function"]
            function_declarations.append({
                "name": f["name"],
                "description": f.get("description", ""),
                "parameters": f.get("parameters", {}),
            })
    return [{"function_declarations": function_declarations}] if function_declarations else []


def _convert_tool_choice_to_gemini(tool_choice, has_tools: bool) -> dict | None:
    """Convert Chat Completions tool_choice to Gemini tool_config."""
    if not has_tools:
        return None
    if tool_choice is None or tool_choice == "auto":
        return {"function_calling_config": {"mode": "AUTO"}}
    if tool_choice == "none":
        return {"function_calling_config": {"mode": "NONE"}}
    if tool_choice == "required":
        return {"function_calling_config": {"mode": "ANY"}}
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        return {
            "function_calling_config": {
                "mode": "ANY",
                "allowed_function_names": [tool_choice["function"]["name"]],
            }
        }
    return {"function_calling_config": {"mode": "AUTO"}}


def _result_to_chat_completions(result: dict, model: str) -> dict:
    """Convert structured Gemini result to Chat Completions response dict."""
    text = result.get("text", "")
    function_calls = result.get("function_calls", [])

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    if function_calls:
        tool_calls = [
            {
                "id": fc["id"],
                "type": "function",
                "function": {"name": fc["name"], "arguments": fc["arguments"]},
            }
            for fc in function_calls
        ]
        message = {"role": "assistant", "content": text or None, "tool_calls": tool_calls}
        finish_reason = "tool_calls"
    else:
        message = {"role": "assistant", "content": text}
        finish_reason = "stop"

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class GeminiDirectAdapter:
    """
    Sends messages to Gemini via Google's internal Code Assist API,
    authenticated with the Gemini CLI's OAuth tokens.

    Maintains conversation history per session (in-memory).
    Project ID is fetched once from loadCodeAssist and cached.
    """

    def __init__(self, model: str | None = None):
        self.conversation_history: list[dict] = []  # Google format: [{role, parts}]
        self._project_id: str | None = None
        self._model = model or DEFAULT_MODEL

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def load_history(self, history: list[dict]):
        """Rebuild in-memory history from DB messages."""
        self.conversation_history = [
            {
                "role": "model" if m["role"] == "assistant" else "user",
                "parts": [{"text": m["content"]}],
            }
            for m in history
        ]

    async def send_message(self, message: str, history: list[dict] = None) -> str:
        if not self.conversation_history and history:
            self.load_history(history)

        self.conversation_history.append(
            {"role": "user", "parts": [{"text": message}]}
        )

        token = await self._get_token()
        project = await self._get_project(token)
        result = await self._call_api(token, project, contents=self.conversation_history)

        text = result["text"]
        self.conversation_history.append(
            {"role": "model", "parts": [{"text": text}]}
        )
        return text

    async def raw_request(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice=None,
    ) -> dict:
        """
        Pass-through for the compat layer.
        Takes Chat Completions format messages/tools, returns a Chat Completions response dict.
        Does not touch self.conversation_history (the client owns state).
        """
        system_text, contents = _convert_messages_to_gemini(messages)
        gemini_tools = _convert_tools_to_gemini(tools) if tools else []
        gemini_tool_config = _convert_tool_choice_to_gemini(tool_choice, has_tools=bool(gemini_tools))

        token = await self._get_token()
        project = await self._get_project(token)

        result = await self._call_api(
            token, project,
            contents=contents,
            tools=gemini_tools or None,
            tool_config=gemini_tool_config,
            system_instruction=system_text,
        )
        return _result_to_chat_completions(result, self._model)

    def clear_history(self):
        self.conversation_history = []

    def is_authenticated(self) -> bool:
        return TOKENS_FILE.exists()

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _read_tokens(self) -> dict:
        if not TOKENS_FILE.exists():
            raise RuntimeError("Gemini not authenticated. Run: gemini auth login")
        try:
            return json.loads(TOKENS_FILE.read_text())
        except Exception as exc:
            raise RuntimeError(f"Gemini tokens file corrupted: {exc}") from exc

    def _is_expired(self, tokens: dict) -> bool:
        expiry_ms = tokens.get("expiry_date", 0)
        return (time.time() * 1000 + _EXPIRY_BUFFER_MS) >= expiry_ms

    async def _refresh_token(self, refresh_token: str) -> str:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                REFRESH_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": _CLIENT_ID,
                    "client_secret": _CLIENT_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Gemini token refresh failed ({resp.status_code}). "
                "Re-authenticate with: gemini auth login"
            )
        data = resp.json()
        access_token = data["access_token"]
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
        if self._is_expired(tokens):
            refresh = tokens.get("refresh_token")
            if not refresh:
                raise RuntimeError(
                    "Gemini token expired and no refresh token found. "
                    "Re-authenticate with: gemini auth login"
                )
            return await self._refresh_token(refresh)
        return tokens["access_token"]

    # ------------------------------------------------------------------
    # Project ID (fetched once from loadCodeAssist)
    # ------------------------------------------------------------------

    async def _get_project(self, token: str) -> str:
        if self._project_id:
            return self._project_id
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                LOAD_CODE_ASSIST_URL,
                headers=self._headers(token),
                json={
                    "metadata": {
                        "ideType": "IDE_UNSPECIFIED",
                        "platform": "PLATFORM_UNSPECIFIED",
                        "pluginType": "GEMINI",
                    }
                },
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"loadCodeAssist failed ({resp.status_code}): {resp.text[:200]}"
            )
        data = resp.json()
        project = data.get("cloudaicompanionProject")
        if not project:
            raise RuntimeError(
                "loadCodeAssist returned no cloudaicompanionProject. "
                "Free tier may not be available in your region."
            )
        self._project_id = project
        return project

    # ------------------------------------------------------------------
    # Core API call
    # ------------------------------------------------------------------

    async def _call_api(
        self,
        token: str,
        project: str,
        *,
        contents: list,
        tools: list | None = None,
        tool_config: dict | None = None,
        system_instruction: str | None = None,
    ) -> dict:
        """
        Call the Code Assist streamGenerateContent endpoint.
        Returns {"text": str, "function_calls": [...]} dict.
        """
        request_body: dict = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": 8192},
            "session_id": "",
        }
        if tools:
            request_body["tools"] = tools
        if tool_config:
            request_body["tool_config"] = tool_config
        if system_instruction:
            request_body["systemInstruction"] = {
                "parts": [{"text": system_instruction}]
            }

        payload = {
            "model": self._model,
            "project": project,
            "user_prompt_id": str(uuid.uuid4()),
            "request": request_body,
        }

        for attempt in range(2):
            if attempt == 1:
                tokens = self._read_tokens()
                refresh = tokens.get("refresh_token")
                if refresh:
                    token = await self._refresh_token(refresh)

            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    GENERATE_URL,
                    headers=self._headers(token),
                    json=payload,
                )

            if resp.status_code == 401 and attempt == 0:
                continue
            if resp.status_code == 429:
                try:
                    msg = resp.json()["error"]["message"]
                except Exception:
                    msg = "Wait a moment and try again."
                raise RuntimeError(f"Gemini rate limited: {msg}")
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Gemini API error {resp.status_code}: {resp.text[:300]}"
                )

            return self._parse_sse(resp.text)

        raise RuntimeError("Gemini session expired. Re-authenticate with: gemini auth login")

    def _parse_sse(self, raw: str) -> dict:
        """
        Parse SSE stream, collect text parts and function calls.
        Returns {"text": str, "function_calls": [{"id", "name", "arguments"}, ...]}
        """
        text_parts: list[str] = []
        function_calls: list[dict] = []

        for line in raw.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                chunk = json.loads(line[6:])
                candidates = chunk.get("response", {}).get("candidates", [])
                for candidate in candidates:
                    for part in candidate.get("content", {}).get("parts", []):
                        if "text" in part:
                            text_parts.append(part["text"])
                        elif "functionCall" in part:
                            fc = part["functionCall"]
                            function_calls.append({
                                "id": f"call_{uuid.uuid4().hex[:16]}",
                                "name": fc.get("name", ""),
                                "arguments": json.dumps(fc.get("args", {})),
                            })
            except (json.JSONDecodeError, KeyError):
                continue

        if not text_parts and not function_calls:
            raise RuntimeError("Gemini returned empty response.")

        return {"text": "".join(text_parts), "function_calls": function_calls}

    @staticmethod
    def _headers(token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "x-goog-api-client": "gl-node/22.22.2",
            "Accept": "application/json",
        }
