"""
gemini_subprocess_adapter.py — FreeHive Resilience Fallback

Shells out to the installed Gemini CLI binary.
Lets the CLI handle its own OAuth — bypasses any FreeHive-specific auth issues.
"""

import asyncio
import logging
import shutil

logger = logging.getLogger(__name__)


class GeminiSubprocessAdapter:
    """Gemini via subprocess — lets CLI handle its own auth."""

    def __init__(self, model: str | None = None):
        self.conversation_history: list[dict] = []
        self._model = model
        self._cli = self._find_cli()

    def _find_cli(self) -> str | None:
        for name in ["gemini", "gemini-cli"]:
            path = shutil.which(name)
            if path:
                return path
        return None

    def load_history(self, messages: list[dict]):
        self.conversation_history = [
            {"role": m["role"], "content": m["content"]} for m in messages
        ]

    async def send_message(self, message: str, history: list[dict] = None) -> str:
        if not self._cli:
            raise RuntimeError("Gemini CLI not found. Install: npm install -g @google/gemini-cli")

        if not self.conversation_history and history:
            self.load_history(history)

        self.conversation_history.append({"role": "user", "content": message})

        try:
            args = [self._cli, "-p", message]
            if self._model:
                args.extend(["-m", self._model])

            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120.0)

            if process.returncode != 0:
                error = stderr.decode().strip()
                raise RuntimeError(f"Gemini CLI error: {error}")

            text = stdout.decode().strip()
            self.conversation_history.append({"role": "assistant", "content": text})
            return text

        except asyncio.TimeoutError:
            process.kill()
            raise RuntimeError("Gemini CLI timed out after 120 seconds")

    async def raw_request(self, messages: list[dict], **kwargs) -> dict:
        """raw_request not supported via subprocess — raise so cascade advances."""
        raise RuntimeError("Gemini subprocess adapter does not support raw_request")

    def clear_history(self):
        self.conversation_history = []

    def is_authenticated(self) -> bool:
        return self._cli is not None
