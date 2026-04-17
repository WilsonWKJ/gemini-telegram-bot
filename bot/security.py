"""Security module — chat ID whitelist and rate limiting."""

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SecurityManager:
    """Manages authentication and rate limiting."""
    allowed_chat_ids: set[int] = field(default_factory=set)
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
