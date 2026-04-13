import subprocess
import asyncio
import shutil

class ClaudeAdapter:

    def __init__(self):
        self.cli_command = "openclaude"
        self._verify_cli()

    def _verify_cli(self):
        if not shutil.which(self.cli_command):
            raise RuntimeError(
                f"'{self.cli_command}' not found. "
                "Make sure OpenClaude is installed and in your PATH."
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
                raise RuntimeError(f"OpenClaude error: {error}")

            return stdout.decode().strip()

        except asyncio.TimeoutError:
            process.kill()
            raise RuntimeError("OpenClaude timed out after 120 seconds")
        except Exception as e:
            raise RuntimeError(f"Claude adapter failed: {str(e)}")