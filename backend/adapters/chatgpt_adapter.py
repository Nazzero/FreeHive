import asyncio
import re
import subprocess
import threading
from pathlib import Path
from typing import Optional

CODEX_WORKDIR = Path.home() / "Ilee_AI"
DEFAULT_MODEL = "gpt-5.4"


class ChatGPTAdapter:
    """
    Calls ChatGPT via codex exec with full history injection from DB.
    History is owned by SessionManager/ConversationManager — not this adapter.
    clear_history() just resets local state; real history lives in conversations.db.
    """

    def __init__(self):
        self.model = DEFAULT_MODEL

    async def send_message(self, message: str, conversation_history: list = None) -> str:
        prompt = self._build_prompt(message, conversation_history)
        try:
            result = await asyncio.to_thread(self._run_codex_exec, prompt)
            return self._parse_output(result)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Codex process timed out after 90 seconds.")
        except FileNotFoundError:
            raise RuntimeError(
                "codex CLI not found. Install with: npm install -g @openai/codex"
            )

    def _build_prompt(self, message: str, history: list = None) -> str:
        if not history:
            return message
        lines = ["This is a continuing conversation. Here is the history so far:\n"]
        for turn in history:
            role = "User" if turn["role"] == "user" else "Assistant"
            lines.append(f"{role}: {turn['content']}")
        lines.append(f"\nContinue the conversation. Respond to this new message:\n{message}")
        return "\n".join(lines)

    def _run_codex_exec(self, prompt: str) -> str:
        result = subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", "-m", self.model, prompt],
            capture_output=True,
            text=True,
            cwd=str(CODEX_WORKDIR),
            timeout=90,
        )
        if result.returncode != 0 and not result.stdout:
            raise RuntimeError(f"Codex failed: {result.stderr.strip()}")
        return result.stdout

    def _parse_output(self, output: str) -> str:
        stripped = output.strip()
        if not stripped:
            raise RuntimeError("Codex returned empty output.")

        match = re.search(r'\ncodex\n(.*?)\ntokens used', stripped, re.DOTALL)
        if match:
            return match.group(1).strip()

        match = re.search(r'\ncodex\n(.*)', stripped, re.DOTALL)
        if match:
            return match.group(1).strip()

        if stripped.startswith("codex\n"):
            return stripped[len("codex\n"):].strip()

        lines = stripped.splitlines()
        skip_patterns = [
            r'^OpenAI Codex',
            r'^-{4,}',
            r'^workdir:',
            r'^model:',
            r'^user$',
            r'^tokens used',
            r'^\d[\d,]+ tokens',
        ]
        clean_lines = []
        skip_next = False
        for line in lines:
            if any(re.match(p, line) for p in skip_patterns):
                skip_next = True
                continue
            if skip_next:
                skip_next = False
                continue
            clean_lines.append(line)

        result = "\n".join(clean_lines).strip()
        return result if result else stripped

    def clear_history(self):
        pass  # History lives in conversations.db, nothing to clear here

    def is_authenticated(self) -> bool:
        auth_path = Path.home() / ".codex" / "auth.json"
        if not auth_path.exists():
            return False
        try:
            import json
            data = json.loads(auth_path.read_text())
            return bool(data.get("tokens", {}).get("access_token"))
        except Exception:
            return False