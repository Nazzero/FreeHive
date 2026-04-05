import asyncio
import re
import subprocess
from pathlib import Path

CODEX_WORKDIR = Path.home() / "Ilee_AI"
DEFAULT_MODEL = "gpt-5.4"


class ChatGPTAdapter:
    """
    Calls ChatGPT via the `codex exec` subprocess.
    Reads credentials from ~/.codex/auth.json (written by `codex login`).
    No token management needed — the codex CLI handles its own refresh.
    """

    def __init__(self):
        self.model = DEFAULT_MODEL

    async def send_message(self, message: str, conversation_history: list = None) -> str:
        """
        Sends a message via `codex exec` and returns the assistant reply.

        conversation_history: list of {"role": "user"|"assistant", "content": str}
        If provided, prepends prior turns to the prompt so codex has context.
        """
        prompt = self._build_prompt(message, conversation_history)
        
        async def send_message(self, message: str, conversation_history: list = None) -> str:
            prompt = self._build_prompt(message, conversation_history)
            result = await asyncio.to_thread(self._run_codex, prompt)
            return self._parse_output(result)

        try:
            result = await asyncio.to_thread(
                self._run_codex, prompt
            )
            return self._parse_output(result)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Codex process timed out after 90 seconds.")
        except FileNotFoundError:
            raise RuntimeError(
                "codex CLI not found. Make sure @openai/codex is installed globally: "
                "npm install -g @openai/codex"
            )

    def clear_history(self):
        pass

    def _build_prompt(self, message: str, history: list = None) -> str:
        """
        Injects prior conversation turns as plain text context before the new message.
        Codex has no memory between exec calls, so we rebuild context manually.
        """
        if not history:
            return message

        lines = ["Previous conversation context:\n"]
        for turn in history:
            role = "User" if turn["role"] == "user" else "Assistant"
            lines.append(f"{role}: {turn['content']}")
        lines.append(f"\nNow respond to this new message from the user:\n{message}")
        return "\n".join(lines)

    def _run_codex(self, prompt: str) -> str:
        result = subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", "-m", self.model, prompt],
            capture_output=True,
            text=True,
            cwd=str(CODEX_WORKDIR),
            timeout=90,
        )

        if result.returncode != 0 and not result.stdout:
            stderr = result.stderr.strip()
            raise RuntimeError(f"Codex process failed: {stderr}")

        return result.stdout

    def _parse_output(self, output: str) -> str:
        """
        Extracts the assistant reply from codex exec output.

        Codex has at least three output modes depending on session/model state:

        Mode A (full banner):
            OpenAI Codex v0.x.x ...
            --------
            user
            <prompt>
            codex
            <reply>
            tokens used
            X,XXX

        Mode B (partial banner, no tokens line):
            --------
            user
            <prompt>
            codex
            <reply>

        Mode C (bare — just the reply, no headers at all):
            <reply>
        """
        stripped = output.strip()

        if not stripped:
            raise RuntimeError("Codex returned empty output.")

        # Mode A: full structured output with tokens line
        match = re.search(r'\ncodex\n(.*?)\ntokens used', stripped, re.DOTALL)
        if match:
            return match.group(1).strip()

        # Mode B: has "codex\n" marker but no tokens line
        match = re.search(r'\ncodex\n(.*)', stripped, re.DOTALL)
        if match:
            return match.group(1).strip()

        # Mode B alt: starts directly with "codex\n" (no leading newline)
        if stripped.startswith("codex\n"):
            return stripped[len("codex\n"):].strip()

        # Mode C: bare reply — no banner, no markers, just the text.
        # Strip any leading header lines (lines that look like codex CLI metadata).
        # Heuristic: skip lines that match known header patterns.
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
        skip_next_if_prompt = False
        for line in lines:
            if any(re.match(p, line) for p in skip_patterns):
                skip_next_if_prompt = True
                continue
            if skip_next_if_prompt:
                # The line after "user" is the echoed prompt — skip it
                skip_next_if_prompt = False
                continue
            clean_lines.append(line)

        result = "\n".join(clean_lines).strip()
        if result:
            return result

        # Last resort: return raw stripped output rather than erroring
        return stripped

    def is_authenticated(self) -> bool:
        """Check if ~/.codex/auth.json exists and has tokens."""
        auth_path = Path.home() / ".codex" / "auth.json"
        if not auth_path.exists():
            return False
        try:
            import json
            data = json.loads(auth_path.read_text())
            return bool(data.get("tokens", {}).get("access_token"))
        except Exception:
            return False