"""AI client — delegates to Gemini CLI for all AI interactions.

Uses `gemini -p` (non-interactive mode) with `--yolo` (auto-approve actions).
Gemini CLI handles its own authentication via the user's Google account.
Supports multiple Google accounts with automatic fallback on quota errors.
"""

import json
import logging
import re
import shutil
import time
from pathlib import Path

from .executor import CommandExecutor, CommandResult, GEMINI_TIMEOUT
from .memory_manager import MemoryManager

logger = logging.getLogger(__name__)

# Minimum interval between progress messages to avoid spamming
_PROGRESS_DEBOUNCE_SECS = 2.0

# Bot's own project directory — so Gemini CLI can read its source code
PROJECT_DIR = Path(__file__).parent.parent.resolve()

# Patterns that indicate a quota/rate-limit error
_QUOTA_PATTERNS = [
    re.compile(r"status:\s*429", re.IGNORECASE),
    re.compile(r"RESOURCE_EXHAUSTED", re.IGNORECASE),
    re.compile(r"quota", re.IGNORECASE),
    re.compile(r"rate.?limit", re.IGNORECASE),
]


class CredentialManager:
    """Manages multiple Google OAuth credentials for Gemini CLI fallback."""

    def __init__(self):
        self.gemini_dir = Path.home() / ".gemini"
        self.accounts_dir = self.gemini_dir / "accounts"
        self.active_creds = self.gemini_dir / "oauth_creds.json"
        self._accounts: list[str] = []
        self._active_index: int = 0
        self._discover_accounts()

    def _discover_accounts(self):
        """Find all available account profiles."""
        if not self.accounts_dir.exists():
            logger.warning("No accounts directory found at %s", self.accounts_dir)
            return
        self._accounts = sorted([
            d.name for d in self.accounts_dir.iterdir()
            if d.is_dir() and (d / "oauth_creds.json").exists()
        ])
        if not self._accounts:
            logger.warning("No account profiles found in %s", self.accounts_dir)
            return
        # Determine which account is currently active by comparing creds
        current = self._read_creds(self.active_creds)
        for i, name in enumerate(self._accounts):
            stored = self._read_creds(self.accounts_dir / name / "oauth_creds.json")
            if current and stored and current.get("refresh_token") == stored.get("refresh_token"):
                self._active_index = i
                break
        logger.info(
            "Credential manager: %d accounts available %s, active: %s",
            len(self._accounts), self._accounts, self.active_account,
        )

    @staticmethod
    def _read_creds(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text())
        except Exception:
            return None

    @property
    def active_account(self) -> str | None:
        if not self._accounts:
            return None
        return self._accounts[self._active_index]

    @property
    def account_count(self) -> int:
        return len(self._accounts)

    def switch_to_next(self) -> str | None:
        """Switch to the next available account. Returns the new account name, or None if only one account."""
        if len(self._accounts) <= 1:
            return None
        self._active_index = (self._active_index + 1) % len(self._accounts)
        new_account = self._accounts[self._active_index]
        src = self.accounts_dir / new_account / "oauth_creds.json"
        shutil.copy2(src, self.active_creds)
        logger.info("Switched credentials to account: %s", new_account)
        return new_account


def _is_quota_error(output: str) -> bool:
    """Check if the output contains quota/rate-limit errors."""
    return any(p.search(output) for p in _QUOTA_PATTERNS)


class AIClient:
    """Client that delegates to Gemini CLI using stream-json."""

    def __init__(self, executor: CommandExecutor):
        self.executor = executor
        self.start_time = time.time()
        self.cred_manager = CredentialManager()
        # Memory manager per chat_id
        self.memories: dict[int, MemoryManager] = {}
        # Selected model per chat_id (defaults to "flash")
        self.models: dict[int, str] = {}

    def _get_memory(self, chat_id: int) -> MemoryManager:
        if chat_id not in self.memories:
            self.memories[chat_id] = MemoryManager(chat_id)
        return self.memories[chat_id]

    def _get_model(self, chat_id: int) -> str:
        return self.models.get(chat_id, "flash")

    def set_model(self, chat_id: int, model_name: str):
        self.models[chat_id] = model_name

    def clear_history(self, chat_id: int):
        # We don't delete files, we just reset the running buffer/summary
        if chat_id in self.memories:
            self.memories[chat_id].current_summary = ""
            self.memories[chat_id].recent_messages = []

    def _build_prompt(self, chat_id: int, user_message: str) -> str:
        """Build a prompt with summarized context."""
        parts = []
        memory = self._get_memory(chat_id)
        context = memory.get_prompt_context()
        if context:
            parts.append(context)
        parts.append(f"\n[Current message]\nUser: {user_message}")
        return "\n".join(parts)

    def _build_command(self, model: str, escaped_prompt: str) -> str:
        """Build the gemini CLI command string with stream-json format."""
        return (
            f'export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh" && nvm use 22 >/dev/null 2>&1 && '
            f"cd ~/Workspace && "
            f"timeout 600 gemini -m {model} -p '{escaped_prompt}' --output-format stream-json --yolo 2>&1"
        )

    async def chat(self, chat_id: int, user_message: str, progress_callback=None) -> str:
        """Send a message to Gemini CLI and return the response."""
        memory = self._get_memory(chat_id)
        model = self._get_model(chat_id)
        last_progress_time = 0.0

        async def _notify(msg: str):
            if progress_callback:
                try:
                    await progress_callback(msg)
                except Exception:
                    pass

        await _notify(f"🤖 Calling Gemini CLI ({model})...")

        # Buffers for streaming data
        assistant_text_parts = []
        final_result = None

        async def _on_line(line: str):
            """Called for each stdout line as it arrives from Gemini CLI."""
            nonlocal last_progress_time, final_result
            
            stripped = line.strip()
            if not stripped or not stripped.startswith("{"):
                return

            try:
                event = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                return

            now = time.monotonic()
            
            # Handle tool use events as progress updates
            if event.get("type") == "tool_use":
                if now - last_progress_time >= _PROGRESS_DEBOUNCE_SECS:
                    last_progress_time = now
                    tool_name = event.get("tool_name", "")
                    params = event.get("parameters", {})
                    
                    if tool_name == "update_topic":
                        intent = params.get("strategic_intent", "Thinking...")
                        await _notify(f"🎯 {intent}")
                    elif tool_name == "read_file":
                        path = params.get("file_path", "")
                        await _notify(f"📖 Reading: {Path(path).name}")
                    elif tool_name == "grep_search":
                        pattern = params.get("pattern", "")[:50]
                        await _notify(f"🔍 Searching: {pattern}")
                    elif tool_name == "run_shell_command":
                        cmd = params.get("command", "")[:50]
                        await _notify(f"💻 Executing: `{cmd}...`")
                    else:
                        await _notify(f"🛠️ Using tool: {tool_name}")

            # Collect assistant text chunks
            elif event.get("type") == "message" and event.get("role") == "assistant":
                content = event.get("content", "")
                if content:
                    assistant_text_parts.append(content)

            # Capture final result
            elif event.get("type") == "result":
                final_result = event.get("result", "")

        # Build the prompt
        prompt = self._build_prompt(chat_id, user_message)
        escaped_prompt = prompt.replace("'", "'\\''")
        command = self._build_command(model, escaped_prompt)

        result = await self.executor.execute_streaming(
            command, line_callback=_on_line, timeout=GEMINI_TIMEOUT
        )

        # Check for quota error and retry with next account
        if not result.timed_out and _is_quota_error(result.output):
            new_account = self.cred_manager.switch_to_next()
            if new_account:
                logger.warning("Quota hit, switching to account: %s", new_account)
                await _notify(f"⚠️ Quota exceeded, switching to {new_account}...")
                last_progress_time = 0.0
                # Reset buffers for retry
                assistant_text_parts = []
                final_result = None
                result = await self.executor.execute_streaming(
                    command, line_callback=_on_line, timeout=GEMINI_TIMEOUT
                )

        if result.timed_out:
            response = "⏰ Gemini CLI timed out. Try a simpler question."
        elif not result.success:
            # If we have some output but success=False, might be a partial crash but check if we got JSON error
            response = f"❌ Gemini CLI error (rc={result.return_code}):\n{result.output[:500]}"
        else:
            # Prefer the final consolidated result if available
            if final_result:
                response = final_result.strip()
            elif assistant_text_parts:
                response = "".join(assistant_text_parts).strip()
            else:
                response = "(no response from Gemini)"

        # Store in memory and potentially trigger summarization
        memory.append_message("user", user_message)
        memory.append_message("assistant", response)
        await memory.maybe_summarize()

        return response

    async def close(self):
        """No cleanup needed for CLI-based client."""
        pass
