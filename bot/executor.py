"""Safe command executor — runs shell commands with timeout, output limits, and env setup."""

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

HOME = str(Path.home())

# Telegram message limit
MAX_MESSAGE_LENGTH = 4096
# Max output to capture (generous — let AI see everything)
MAX_OUTPUT_LENGTH = 100000
# Default command timeout
DEFAULT_TIMEOUT = 120
# Extended timeout for gemini CLI calls
GEMINI_TIMEOUT = 900  # 15 minutes for complex gemini -p operations


@dataclass
class CommandResult:
    """Result of a command execution."""
    command: str
    stdout: str
    stderr: str
    return_code: int
    timed_out: bool = False

    @property
    def success(self) -> bool:
        return self.return_code == 0 and not self.timed_out

    @property
    def output(self) -> str:
        """Combined output, preferring stdout."""
        if self.timed_out:
            return f"⏰ Command timed out after {DEFAULT_TIMEOUT}s\n\nPartial output:\n{self.stdout[:2000]}"
        if self.stdout and self.stderr:
            return f"{self.stdout}\n\n--- stderr ---\n{self.stderr}"
        return self.stdout or self.stderr or "(no output)"

    def format_for_telegram(self) -> str:
        """Format output for Telegram message, with truncation."""
        output = self.output
        status = "✅" if self.success else "❌"

        if len(output) > MAX_MESSAGE_LENGTH - 200:
            # Count total lines
            lines = output.split("\n")
            # Truncate and show count
            truncated = "\n".join(lines[:80])
            if len(truncated) > MAX_MESSAGE_LENGTH - 200:
                truncated = truncated[:MAX_MESSAGE_LENGTH - 200]
            remaining = len(lines) - 80
            output = f"{truncated}\n\n... ({remaining} more lines, output truncated)"

        return f"{status} `{self.command[:100]}`\n```\n{output}\n```"


class CommandExecutor:
    """Executes shell commands safely with proper environment setup."""

    def __init__(self, kubeconfig: str | None = None):
        self.env = os.environ.copy()
        # Set KUBECONFIG
        self.env["KUBECONFIG"] = kubeconfig or f"{HOME}/.kube/config-merged"
        # Ensure govc env is available if needed
        self.env.setdefault("PATH", f"{HOME}/.local/bin:{HOME}/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")

    async def execute(self, command: str, timeout: int = DEFAULT_TIMEOUT) -> CommandResult:
        """Execute a shell command asynchronously."""
        logger.info(f"Executing: {command}")

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
                cwd=HOME,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )
                stdout = stdout_bytes.decode("utf-8", errors="replace")[:MAX_OUTPUT_LENGTH]
                stderr = stderr_bytes.decode("utf-8", errors="replace")[:MAX_OUTPUT_LENGTH]

                result = CommandResult(
                    command=command,
                    stdout=stdout.strip(),
                    stderr=stderr.strip(),
                    return_code=process.returncode or 0,
                )

            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                result = CommandResult(
                    command=command,
                    stdout="",
                    stderr=f"Command timed out after {timeout}s",
                    return_code=-1,
                    timed_out=True,
                )

        except Exception as e:
            logger.error(f"Failed to execute command: {e}")
            result = CommandResult(
                command=command,
                stdout="",
                stderr=str(e),
                return_code=-1,
            )

        logger.info(f"Result: rc={result.return_code}, stdout={len(result.stdout)} chars")
        return result

    async def execute_multi(self, commands: list[str], timeout: int = DEFAULT_TIMEOUT) -> list[CommandResult]:
        """Execute multiple commands sequentially."""
        results = []
        for cmd in commands:
            result = await self.execute(cmd, timeout=timeout)
            results.append(result)
            if not result.success:
                break  # Stop on first failure
        return results
