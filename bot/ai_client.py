"""AI client — delegates to Gemini CLI for all AI interactions.

Uses `gemini -p` (non-interactive mode) with `--yolo` (auto-approve actions).
Gemini CLI handles its own authentication via the user's Google account.
No API keys or system prompts needed.
"""

import logging
import re
import time
from pathlib import Path

from .executor import CommandExecutor, CommandResult, GEMINI_TIMEOUT

logger = logging.getLogger(__name__)

# Patterns that indicate Gemini is narrating what it's doing
_PROGRESS_PATTERNS = [
    re.compile(r"^I will ", re.IGNORECASE),
    re.compile(r"^I'll ", re.IGNORECASE),
    re.compile(r"^Let me ", re.IGNORECASE),
    re.compile(r"^Now I ", re.IGNORECASE),
    re.compile(r"^Searching ", re.IGNORECASE),
    re.compile(r"^Reading ", re.IGNORECASE),
    re.compile(r"^Checking ", re.IGNORECASE),
]

# Minimum interval between progress messages to avoid spamming
_PROGRESS_DEBOUNCE_SECS = 2.0

# Bot's own project directory — so Gemini CLI can read its source code
PROJECT_DIR = Path(__file__).parent.parent.resolve()

# Max conversation history (for context passed to gemini -p)
MAX_HISTORY = 10
MAX_HISTORY_CHARS_PER_MSG = 1000
MAX_HISTORY_CHARS_TOTAL = 5000


def _load_system_prompt() -> str:
    """Load system prompt from config file."""
    prompt_path = PROJECT_DIR / "config" / "system_prompt.md"
    if prompt_path.exists():
        return prompt_path.read_text().strip()
    return "You are a personal AI assistant running as a Telegram bot."


class AIClient:
    """Client that delegates to Gemini CLI."""

    def __init__(self, executor: CommandExecutor):
        self.executor = executor
        self.start_time = time.time()
        # Simple conversation history per chat_id (text only)
        self.conversations: dict[int, list[dict]] = {}

    def _get_history(self, chat_id: int) -> list[dict]:
        if chat_id not in self.conversations:
            self.conversations[chat_id] = []
        return self.conversations[chat_id]

    def _trim_history(self, chat_id: int):
        history = self._get_history(chat_id)
        if len(history) > MAX_HISTORY:
            self.conversations[chat_id] = history[-MAX_HISTORY:]

    def clear_history(self, chat_id: int):
        self.conversations.pop(chat_id, None)

    def _build_prompt(self, chat_id: int, user_message: str) -> str:
        """Build a prompt with system context and conversation history."""
        system_prompt = _load_system_prompt()
        parts = [f"[System]\n{system_prompt}"]

        history = self._get_history(chat_id)
        if history:
            parts.append("\n[Previous conversation]")
            total_chars = 0
            for msg in history[-MAX_HISTORY:]:
                role = "User" if msg["role"] == "user" else "Assistant"
                text = msg["content"][:MAX_HISTORY_CHARS_PER_MSG]
                if total_chars + len(text) > MAX_HISTORY_CHARS_TOTAL:
                    text = text[:MAX_HISTORY_CHARS_TOTAL - total_chars]
                    parts.append(f"{role}: {text}")
                    break
                total_chars += len(text)
                parts.append(f"{role}: {text}")

        parts.append(f"\n[Current message]\nUser: {user_message}")

        return "\n".join(parts)

    @staticmethod
    def _is_progress_line(line: str) -> bool:
        """Check if a line is Gemini narrating its actions."""
        stripped = line.strip()
        return any(p.match(stripped) for p in _PROGRESS_PATTERNS)

    async def chat(self, chat_id: int, user_message: str, progress_callback=None) -> str:
        """Send a message to Gemini CLI and return the response."""
        history = self._get_history(chat_id)
        last_progress_time = 0.0

        async def _notify(msg: str):
            if progress_callback:
                try:
                    await progress_callback(msg)
                except Exception:
                    pass

        await _notify("🤖 Calling Gemini CLI...")

        async def _on_line(line: str):
            """Called for each stdout line as it arrives from Gemini CLI."""
            nonlocal last_progress_time
            if not progress_callback:
                return
            if self._is_progress_line(line):
                now = time.monotonic()
                if now - last_progress_time >= _PROGRESS_DEBOUNCE_SECS:
                    last_progress_time = now
                    # Truncate long progress lines for Telegram readability
                    text = line.strip()[:200]
                    await _notify(f"⏳ {text}")

        # Build the prompt
        prompt = self._build_prompt(chat_id, user_message)

        # Escape the prompt for shell
        escaped_prompt = prompt.replace("'", "'\\''")

        # Call gemini CLI from home dir so it can access both the bot and travel plan repos
        command = (
            f'export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh" && nvm use 22 >/dev/null 2>&1 && '
            f"cd ~ && "
            f"timeout 600 gemini -p '{escaped_prompt}' --yolo 2>&1"
        )

        result = await self.executor.execute_streaming(
            command, line_callback=_on_line, timeout=GEMINI_TIMEOUT
        )

        if result.timed_out:
            response = "⏰ Gemini CLI timed out. Try a simpler question."
        elif not result.success:
            response = f"❌ Gemini CLI error (rc={result.return_code}):\n{result.output[:500]}"
        else:
            response = self._clean_output(result.output)
            if not response:
                response = "(no response from Gemini)"

        # Store in history
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": response[:MAX_HISTORY_CHARS_PER_MSG]})
        self._trim_history(chat_id)

        return response

    @staticmethod
    def _clean_output(raw: str) -> str:
        """Strip Gemini CLI boilerplate and error noise from output."""
        lines = raw.strip().splitlines()
        cleaned = []
        in_stack_trace = False
        for line in lines:
            stripped = line.strip()
            # YOLO mode banner
            if stripped.startswith("YOLO mode is enabled"):
                continue
            # IDE connection errors (start of a stack trace block)
            if stripped.startswith("[ERROR]"):
                in_stack_trace = True
                continue
            # Stack trace internals
            if in_stack_trace:
                if (stripped.startswith("at ")
                        or stripped.startswith("{")
                        or stripped.startswith("}")
                        or "errno:" in stripped
                        or "code:" in stripped
                        or "syscall:" in stripped
                        or "address:" in stripped
                        or "port:" in stripped
                        or stripped.startswith("[cause]:")):
                    continue
                # End of stack trace block
                in_stack_trace = False
            # Gemini tool execution errors (workspace path restrictions)
            if stripped.startswith("Error executing tool"):
                continue
            # Gemini thinking-out-loud lines (already sent as progress)
            if any(p.match(stripped) for p in _PROGRESS_PATTERNS):
                continue
            cleaned.append(line)
        # Remove leading/trailing blank lines after stripping
        while cleaned and not cleaned[0].strip():
            cleaned.pop(0)
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()
        return "\n".join(cleaned)

    async def close(self):
        """No cleanup needed for CLI-based client."""
        pass
