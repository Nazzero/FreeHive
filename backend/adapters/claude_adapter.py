import subprocess
import asyncio
import shutil

class ClaudeAdapter:

    def __init__(self):
        self.cli_command = self._find_cli()

    def _find_cli(self) -> str:
        """Find Claude Code CLI."""
        if shutil.which("claude"):
            return "claude"
        raise RuntimeError(
            "Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code"
        )

    async def send_message(self, message: str) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                self.cli_command,
                "--print",
                message,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=120.0
            )

            if process.returncode != 0:
                error = stderr.decode().strip()
                raise RuntimeError(f"Claude CLI error: {error}")

            return stdout.decode().strip()

        except asyncio.TimeoutError:
            process.kill()
            raise RuntimeError("Claude CLI timed out after 120 seconds")
        except Exception as e:
            raise RuntimeError(f"Claude adapter failed: {str(e)}")