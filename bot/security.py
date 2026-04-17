"""Security module — chat ID whitelist, command classification, confirmation flow."""

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class CommandRisk(Enum):
    """Risk level of a command."""
    SAFE = "safe"           # Read-only: get, describe, logs, status
    MUTATING = "mutating"   # Changes state: scale, restart, rollout, hset
    DESTRUCTIVE = "destructive"  # Destroys resources: delete, kill, destroy, drain


# Patterns for classifying commands by risk level
DESTRUCTIVE_PATTERNS = [
    r"\bdelete\b",
    r"\bkill\b",
    r"\bdestroy\b",
    r"\bdrain\b",
    r"\brm\s+-rf\b",
    r"\bvm\.destroy\b",
    r"\bpurge\b",
    r"\bdrop\b",
    r"\btruncate\b",
]

MUTATING_PATTERNS = [
    r"\bscale\b",
    r"\brestart\b",
    r"\brollout\b",
    r"\bpatch\b",
    r"\bapply\b",
    r"\bcreate\b",
    r"\bhset\b",
    r"\bhdel\b",
    r"\bset\b.*redis",
    r"\bcordon\b",
    r"\buncordon\b",
    r"\btaint\b",
    r"\blabel\b",
    r"\bannotate\b",
    r"\bsystemctl\s+(start|stop|restart|enable|disable)\b",
    r"\bkeactrl\s+(start|stop)\b",
]

# Commands that are NEVER allowed (even with confirmation)
BLOCKED_PATTERNS = [
    r"\brm\s+-rf\s+/\b",          # rm -rf /
    r"\bmkfs\b",                   # format disk
    r"\bdd\s+if=",                 # disk destroyer
    r"\b:(){ :|:& };:\b",         # fork bomb
    r"\bshutdown\b",
    r"\breboot\b",
    r"\binit\s+[06]\b",
    r"範本",                        # Never touch VM templates!
]


@dataclass
class PendingConfirmation:
    """A command waiting for user confirmation."""
    command: str
    risk: CommandRisk
    timestamp: float
    context: str = ""  # What the AI was trying to do

    def is_expired(self, timeout: float = 300.0) -> bool:
        """Confirmation expires after timeout seconds."""
        return time.time() - self.timestamp > timeout


@dataclass
class SecurityManager:
    """Manages authentication, command classification, and confirmation flow."""
    allowed_chat_ids: set[int] = field(default_factory=set)
    pending: dict[int, PendingConfirmation] = field(default_factory=dict)
    request_timestamps: dict[int, list[float]] = field(default_factory=dict)
    rate_limit: int = 30  # Max requests per minute

    def is_authorized(self, chat_id: int) -> bool:
        """Check if a chat ID is in the whitelist."""
        return chat_id in self.allowed_chat_ids

    def check_rate_limit(self, chat_id: int) -> bool:
        """Check if user has exceeded rate limit. Returns True if OK."""
        now = time.time()
        if chat_id not in self.request_timestamps:
            self.request_timestamps[chat_id] = []

        # Remove timestamps older than 60 seconds
        self.request_timestamps[chat_id] = [
            ts for ts in self.request_timestamps[chat_id]
            if now - ts < 60
        ]

        if len(self.request_timestamps[chat_id]) >= self.rate_limit:
            return False

        self.request_timestamps[chat_id].append(now)
        return True

    def classify_command(self, command: str) -> CommandRisk:
        """Classify a command by its risk level."""
        cmd_lower = command.lower()

        # Check blocked patterns first
        for pattern in BLOCKED_PATTERNS:
            if re.search(pattern, cmd_lower):
                return CommandRisk.DESTRUCTIVE  # Will be blocked in executor

        # Check destructive
        for pattern in DESTRUCTIVE_PATTERNS:
            if re.search(pattern, cmd_lower):
                return CommandRisk.DESTRUCTIVE

        # Check mutating
        for pattern in MUTATING_PATTERNS:
            if re.search(pattern, cmd_lower):
                return CommandRisk.MUTATING

        return CommandRisk.SAFE

    def is_blocked(self, command: str) -> bool:
        """Check if a command matches blocked patterns (never allowed)."""
        cmd_lower = command.lower()
        for pattern in BLOCKED_PATTERNS:
            if re.search(pattern, cmd_lower):
                logger.warning(f"BLOCKED command: {command}")
                return True
        return False

    def set_pending(self, chat_id: int, command: str, risk: CommandRisk, context: str = ""):
        """Store a command pending user confirmation."""
        self.pending[chat_id] = PendingConfirmation(
            command=command,
            risk=risk,
            timestamp=time.time(),
            context=context,
        )

    def get_pending(self, chat_id: int) -> PendingConfirmation | None:
        """Get and clear the pending confirmation for a chat."""
        pending = self.pending.pop(chat_id, None)
        if pending and pending.is_expired():
            logger.info(f"Pending confirmation expired for chat {chat_id}")
            return None
        return pending

    def clear_pending(self, chat_id: int):
        """Clear any pending confirmation."""
        self.pending.pop(chat_id, None)
