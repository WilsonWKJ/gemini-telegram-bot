"""AI client — delegates to Gemini CLI for all AI interactions.

Uses `gemini -p` (non-interactive mode) with `--yolo` (auto-approve actions).
Gemini CLI handles its own authentication via the user's Google account.
No API keys or system prompts needed.
"""

import logging
from pathlib import Path

from .executor import CommandExecutor, CommandResult, GEMINI_TIMEOUT

logger = logging.getLogger(__name__)

# Path to gemini CLI (requires nvm Node 22)
GEMINI_CLI = Path.home() / ".nvm/versions/node/v22.22.2/bin/gemini"

# Max conversation history (for context passed to gemini -p)
MAX_HISTORY = 10


class AIClient:
    """Client that delegates to Gemini CLI."""

    def __init__(self, executor: CommandExecutor):
        self.executor = executor
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
        """Build a prompt with conversation context for gemini -p."""
        history = self._get_history(chat_id)

        # If no history, just use the user message directly
        if not history:
            return user_message

        # Build context from recent history
        parts = ["[Previous conversation context]"]
        for msg in history[-6:]:  # Last 3 exchanges
            role = "User" if msg["role"] == "user" else "Assistant"
            text = msg["content"][:500]  # Truncate old messages
            parts.append(f"{role}: {text}")
        parts.append(f"\n[Current question]\nUser: {user_message}")

        return "\n".join(parts)

    async def chat(self, chat_id: int, user_message: str, progress_callback=None) -> str:
        """Send a message to Gemini CLI and return the response."""
        history = self._get_history(chat_id)

        async def _notify(msg: str):
            if progress_callback:
                try:
                    await progress_callback(msg)
                except Exception:
                    pass

        await _notify("🤖 Calling Gemini CLI...")

        # Build the prompt
        prompt = self._build_prompt(chat_id, user_message)

        # Escape the prompt for shell
        escaped_prompt = prompt.replace("'", "'\\''")

        # Call gemini CLI in non-interactive mode with yolo (auto-approve)
        command = (
            f'export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh" && nvm use 22 >/dev/null 2>&1 && '
            f"timeout 600 gemini -p '{escaped_prompt}' --yolo 2>&1"
        )

        result = await self.executor.execute(command, timeout=GEMINI_TIMEOUT)

        if result.timed_out:
            response = "⏰ Gemini CLI timed out. Try a simpler question."
        elif not result.success:
            response = f"❌ Gemini CLI error (rc={result.return_code}):\n{result.output[:500]}"
        else:
            response = result.output.strip()
            if not response:
                response = "(no response from Gemini)"

        # Store in history
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": response[:1000]})
        self._trim_history(chat_id)

        return response

    async def close(self):
        """No cleanup needed for CLI-based client."""
        pass
