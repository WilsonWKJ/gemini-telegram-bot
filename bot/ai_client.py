"""AI client — talks to Gemini via OpenAI-compatible API with function calling."""

import json
import logging
from pathlib import Path

import httpx

from .executor import CommandExecutor, CommandResult, CLAUDE_TIMEOUT
from .security import CommandRisk, SecurityManager

logger = logging.getLogger(__name__)

# Tool definition for command execution (OpenAI function calling format)
EXECUTE_COMMAND_TOOL = {
    "type": "function",
    "function": {
        "name": "execute_command",
        "description": (
            "Execute a shell command on the kube-controller machine. "
            "Use this to run kubectl, redis-cli, govc, ssh, or any other "
            "infrastructure command. Always set KUBECONFIG and use --context for kubectl."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "requires_confirmation": {
                    "type": "boolean",
                    "description": (
                        "Set to true if this command modifies or destroys resources "
                        "(delete, kill, scale, restart, etc.)"
                    ),
                },
            },
            "required": ["command"],
        },
    },
}

# Max conversation history to keep
MAX_HISTORY = 40
# Max tool use iterations per message
MAX_TOOL_ITERATIONS = 25

# Gemini CLI path (installed via nvm)
GEMINI_CLI_PATH = Path.home() / ".nvm" / "versions" / "node" / "v22.22.2" / "bin" / "gemini"


class AIClient:
    """Client for Gemini API (OpenAI-compatible) with function calling support."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        executor: CommandExecutor,
        security: SecurityManager,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.executor = executor
        self.security = security
        self.system_prompt = self._load_system_prompt()
        # Conversation history per chat_id
        self.conversations: dict[int, list[dict]] = {}
        self.http_client = httpx.AsyncClient(timeout=300.0)

    def _load_system_prompt(self) -> str:
        """Load the system prompt from config file."""
        prompt_path = Path(__file__).parent.parent / "config" / "system_prompt.md"
        if prompt_path.exists():
            return prompt_path.read_text()
        return "You are an infrastructure operations assistant."

    def _get_history(self, chat_id: int) -> list[dict]:
        """Get conversation history for a chat, creating if needed."""
        if chat_id not in self.conversations:
            self.conversations[chat_id] = []
        return self.conversations[chat_id]

    def _trim_history(self, chat_id: int):
        """Trim conversation history to MAX_HISTORY messages.

        Find a safe cut point that doesn't orphan tool_call/tool responses.
        """
        history = self._get_history(chat_id)
        if len(history) <= MAX_HISTORY:
            return

        target_start = len(history) - MAX_HISTORY
        safe_start = target_start

        for i in range(target_start, min(target_start + 10, len(history))):
            msg = history[i]
            role = msg.get("role")
            # Safe to start at a user message (plain text, not tool response)
            if role == "user":
                safe_start = i
                break
            # Don't start at a tool response (needs preceding assistant with tool_calls)
            if role == "tool":
                continue
            # Don't start at an assistant message with tool_calls
            if role == "assistant" and msg.get("tool_calls"):
                continue
            # Plain assistant message — safe
            if role == "assistant":
                safe_start = i
                break

        self.conversations[chat_id] = history[safe_start:]

    def clear_history(self, chat_id: int):
        """Clear conversation history for a chat."""
        self.conversations.pop(chat_id, None)

    async def _call_api(self, chat_id: int, messages: list[dict]) -> dict:
        """Call the OpenAI-compatible Gemini API."""
        url = f"{self.base_url}/v1/chat/completions"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
        }

        # Build messages with system prompt
        full_messages = [{"role": "system", "content": self.system_prompt}] + messages

        payload = {
            "model": self.model,
            "max_tokens": 16384,
            "messages": full_messages,
            "tools": [EXECUTE_COMMAND_TOOL],
            "tool_choice": "auto",
        }

        payload_str = json.dumps(payload)
        logger.info(f"Calling Gemini API with {len(messages)} messages, payload size: {len(payload_str)} chars")

        try:
            response = await self.http_client.post(url, headers=headers, json=payload)
            logger.info(f"Gemini API response status: {response.status_code}")
            response.raise_for_status()
            result = response.json()
            choice = result.get("choices", [{}])[0]
            finish_reason = choice.get("finish_reason", "stop")
            logger.info(f"Gemini API finish_reason: {finish_reason}")
            return result
        except httpx.HTTPStatusError as e:
            logger.error(f"Gemini API error: {e.response.status_code} - {e.response.text[:500]}")
            raise
        except httpx.TimeoutException as e:
            logger.error(f"Gemini API timeout: {e}")
            raise
        except Exception as e:
            logger.error(f"Gemini API call failed: {e}")
            raise

    async def chat(self, chat_id: int, user_message: str, progress_callback=None) -> str:
        """
        Send a message to the AI and handle function calling loop.

        Returns the final text response to send to Telegram.
        """
        history = self._get_history(chat_id)
        history.append({"role": "user", "content": user_message})

        async def _notify(msg: str):
            if progress_callback:
                try:
                    await progress_callback(msg)
                except Exception:
                    pass

        # Function calling loop
        for iteration in range(MAX_TOOL_ITERATIONS):
            if iteration > 0:
                await _notify(f"🔄 Thinking... (step {iteration + 1})")

            try:
                await _notify("🧠 Calling Gemini...")
                response = await self._call_api(chat_id, history)
            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code
                error_text = e.response.text[:300]
                if status_code == 400 and "tool" in error_text.lower():
                    self.clear_history(chat_id)
                    logger.warning(f"Auto-cleared corrupted history for chat {chat_id}")
                    return (
                        "⚠️ Conversation history was corrupted and has been auto-cleared.\n"
                        "Please re-send your question."
                    )
                if history:
                    history.pop()
                return f"❌ Gemini API error ({status_code}): {error_text}"
            except Exception as e:
                error_msg = f"❌ Gemini API error: {str(e)[:300]}"
                if history:
                    history.pop()
                return error_msg

            # Extract response
            choice = response.get("choices", [{}])[0]
            message = choice.get("message", {})
            finish_reason = choice.get("finish_reason", "stop")

            # Add assistant response to history
            history.append(message)

            # If no tool calls, return the text
            if finish_reason == "stop" or not message.get("tool_calls"):
                text = message.get("content", "") or ""
                self._trim_history(chat_id)
                return text if text else "(no response)"

            # Handle function calls
            if finish_reason == "tool_calls" or message.get("tool_calls"):
                tool_calls = message.get("tool_calls", [])
                pending_confirm_cmd = None
                pending_confirm_risk = None

                for tool_call in tool_calls:
                    func = tool_call.get("function", {})
                    func_name = func.get("name", "")
                    tool_call_id = tool_call.get("id", "")

                    try:
                        func_args = json.loads(func.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        func_args = {}

                    if func_name == "execute_command":
                        command = func_args.get("command", "")
                        needs_confirm = func_args.get("requires_confirmation", False)

                        # Check if blocked
                        if self.security.is_blocked(command):
                            history.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": "🚫 This command is blocked for safety reasons.",
                            })
                            continue

                        # Classify risk
                        risk = self.security.classify_command(command)

                        if risk in (CommandRisk.DESTRUCTIVE, CommandRisk.MUTATING) or needs_confirm:
                            pending_confirm_cmd = command
                            pending_confirm_risk = risk
                            history.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": "⏸️ Command requires user confirmation. Waiting for /confirm.",
                            })
                            continue

                        # Send progress
                        is_gemini_cli = "gemini" in command
                        if is_gemini_cli:
                            await _notify(f"🤖 Running Gemini CLI (may take a few minutes)...\n`{command[:120]}...`")
                        else:
                            cmd_short = command[:80].replace('\n', ' ')
                            await _notify(f"⚙️ Running: `{cmd_short}...`")

                        # Execute
                        cmd_timeout = CLAUDE_TIMEOUT if is_gemini_cli else None
                        result = await self.executor.execute(
                            command, **({"timeout": cmd_timeout} if cmd_timeout else {})
                        )
                        history.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": self._format_tool_result(result),
                        })
                    else:
                        history.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": f"Unknown function: {func_name}",
                        })

                # Handle confirmation after all tool results are in history
                if pending_confirm_cmd:
                    self.security.set_pending(
                        chat_id, pending_confirm_cmd, pending_confirm_risk,
                        context=message.get("content", ""),
                    )
                    risk_emoji = "🔴" if pending_confirm_risk == CommandRisk.DESTRUCTIVE else "🟡"
                    self._trim_history(chat_id)
                    return (
                        f"{risk_emoji} **Confirmation required**\n\n"
                        f"Command:\n```\n{pending_confirm_cmd}\n```\n"
                        f"Risk: {pending_confirm_risk.value}\n\n"
                        f"Send /confirm to execute or /cancel to abort."
                    )

                continue

            # Unknown finish reason
            text = message.get("content", "") or "(unexpected finish reason)"
            self._trim_history(chat_id)
            return text

        # Max iterations
        self._trim_history(chat_id)
        return "⚠️ Reached maximum tool iterations. Please try a simpler query."

    async def execute_confirmed(self, chat_id: int, command: str) -> str:
        """Execute a previously confirmed command and feed result back to AI."""
        result = await self.executor.execute(command)
        history = self._get_history(chat_id)

        status = "✅ succeeded" if result.success else "❌ failed"
        history.append({
            "role": "user",
            "content": (
                f"[The confirmed command has been executed]\n"
                f"Command: {command}\n"
                f"Status: {status}\n"
                f"Output:\n{result.output}"
            ),
        })

        try:
            response = await self._call_api(chat_id, history)
            choice = response.get("choices", [{}])[0]
            message = choice.get("message", {})
            history.append(message)
            self._trim_history(chat_id)
            return message.get("content", "") or result.format_for_telegram()
        except Exception:
            return result.format_for_telegram()

    def _format_tool_result(self, result: CommandResult) -> str:
        """Format a command result for the function call response."""
        status = "exit code 0 (success)" if result.success else f"exit code {result.return_code} (failed)"
        output = result.output[:10000]
        return f"Command: {result.command}\nStatus: {status}\nOutput:\n{output}"

    async def close(self):
        """Close the HTTP client."""
        await self.http_client.aclose()
