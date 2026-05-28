import asyncio
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# How many fresh messages to keep un-summarized before triggering a summary
SUMMARY_THRESHOLD = 2

class MemoryManager:
    """Manages chat logs and background summarization using Gemini CLI (OAuth)."""
    
    def __init__(self, chat_id: int, executor):
        self.chat_id = chat_id
        self.executor = executor
        self.log_dir = Path(__file__).resolve().parent.parent.parent.parent / "knowledge" / "chat_history_telegram_bot" / str(chat_id)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        self.current_summary = ""
        # Keep track of recent unsummarized messages
        self.recent_messages = []
        
        # Load the latest summary if we restart
        self._load_latest_state()

    @property
    def _today_date_str(self) -> str:
        return datetime.now().strftime("%Y%m%d")

    @property
    def full_log_path(self) -> Path:
        return self.log_dir / f"{self._today_date_str}_full.md"

    @property
    def summary_path(self) -> Path:
        return self.log_dir / f"{self._today_date_str}_summary.md"

    def _load_latest_state(self):
        """Loads today's summary if it exists."""
        if self.summary_path.exists():
            try:
                self.current_summary = self.summary_path.read_text().strip()
                logger.info(f"Loaded existing summary for chat {self.chat_id}")
            except Exception as e:
                logger.error(f"Failed to load summary: {e}")

    def append_message(self, role: str, content: str):
        """Append a message to the daily log and the short-term buffer."""
        # Clean content
        content = content.strip()
        if not content:
            return

        # Write to daily log
        import socket
        hostname = socket.gethostname()
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_entry = f"**[{timestamp}] {role.capitalize()} (on {hostname})**:\n{content}\n\n"
        
        try:
            with open(self.full_log_path, "a", encoding="utf-8") as f:
                f.write(log_entry)
        except Exception as e:
            logger.error(f"Failed to write to full log: {e}")

        # Add to recent buffer
        self.recent_messages.append({"role": role, "content": content})

    async def maybe_summarize(self):
        """Trigger summarization if buffer is full."""
        if len(self.recent_messages) >= SUMMARY_THRESHOLD:
            # We don't want to block the main chat flow, so we fire and forget
            asyncio.create_task(self._summarize_background())

    async def _summarize_background(self):
        """Runs the LLM summarization in the background using Gemini CLI."""
        logger.info(f"Triggering background summarization for chat {self.chat_id} via Gemini CLI...")
        
        # Prepare the text to summarize
        messages_text = ""
        for msg in self.recent_messages:
            messages_text += f"{msg['role'].capitalize()}: {msg['content']}\n"
            
        prompt = (
            "You are a memory manager. Here is the current context summary of a conversation:\n"
            f"<current_summary>\n{self.current_summary}\n</current_summary>\n\n"
            "And here are the latest messages in the conversation:\n"
            f"<latest_messages>\n{messages_text}\n</latest_messages>\n\n"
            "Please provide a new, concise, updated context summary that incorporates the latest messages. "
            "Keep it under 300 words. Focus on facts, decisions, active tasks, and context needed for future replies. "
            "Return ONLY the new summary text."
        )

        escaped_prompt = prompt.replace("'", "'\\''")
        
        # Use gemini CLI so it shares the OAuth session instead of needing an API Key
        command = (
            f'export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh" && nvm use 22 >/dev/null 2>&1 && '
            f"cd ~/Workspace && "
            f"timeout 600 gemini -m gemini-3-flash-preview -p '{escaped_prompt}' --yolo 2>&1"
        )

        try:
            result = await self.executor.execute(command, timeout=600)
            
            if result.success and result.stdout:
                # Clean up CLI boilerplate from the output
                lines = result.stdout.strip().splitlines()
                cleaned = []
                for line in lines:
                    stripped = line.strip()
                    if stripped.startswith("YOLO mode is enabled"): continue
                    if stripped.startswith("Ripgrep is not available"): continue
                    cleaned.append(line)
                    
                self.current_summary = "\n".join(cleaned).strip()
                # Save to disk
                self.summary_path.write_text(self.current_summary, encoding="utf-8")
                # Keep only the last 2 messages for immediate continuity
                self.recent_messages = self.recent_messages[-2:]
                logger.info("Background summarization via CLI complete.")
            else:
                logger.error(f"Summarization CLI failed (rc={result.return_code}): {result.stderr or result.stdout}")
                self.recent_messages = self.recent_messages[-2:]
                
        except Exception as e:
            logger.error(f"Summarization failed: {e}")
            self.recent_messages = self.recent_messages[-2:]

    def get_prompt_context(self) -> str:
        """Returns the formatted string to prepend to the Gemini CLI prompt."""
        parts = []
        if self.current_summary:
            parts.append(f"[Context Summary]\n{self.current_summary}\n")
            
        if self.recent_messages:
            parts.append("[Recent Messages]")
            for msg in self.recent_messages:
                # Limit length of recent messages to avoid bloating
                content = msg['content'][:800] 
                parts.append(f"{msg['role'].capitalize()}: {content}")
                
        return "\n".join(parts)
