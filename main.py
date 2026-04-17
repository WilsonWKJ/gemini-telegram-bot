#!/usr/bin/env python3
"""
Gemini Telegram Bot — personal AI chatbot powered by Gemini CLI.

Polls Telegram for messages, routes to Gemini CLI, returns responses.
"""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.ai_client import AIClient
from bot.executor import CommandExecutor
from bot.handlers import setup_handlers
from bot.security import SecurityManager

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("gemini-telegram-bot")

# Reduce noise from httpx and telegram
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def load_config() -> dict:
    """Load configuration from environment variables."""
    # Try loading from secrets file
    secrets_path = Path.home() / "Documents" / "secrets" / "gemini-telegram-agent.env"
    if secrets_path.exists():
        load_dotenv(secrets_path)
        logger.info(f"Loaded secrets from {secrets_path}")

    config = {
        "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
    }

    # Validate required config
    missing = []
    if not config["telegram_bot_token"]:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not config["telegram_chat_id"]:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        logger.error(
            f"Missing required environment variables: {', '.join(missing)}\n"
            f"Set them in {secrets_path} or as environment variables.\n"
            f"See config/secrets.env.template for reference."
        )
        sys.exit(1)

    return config


def main():
    """Main entry point."""
    logger.info("Starting Gemini Telegram Bot...")

    # Load config
    config = load_config()
    # Parse chat IDs (support comma-separated list)
    chat_ids = set()
    for cid in config["telegram_chat_id"].split(","):
        cid = cid.strip()
        if cid:
            try:
                chat_ids.add(int(cid))
            except ValueError:
                logger.error(f"Invalid chat ID: {cid}")
                sys.exit(1)

    logger.info(f"Authorized chat IDs: {chat_ids}")

    # Initialize components
    security = SecurityManager(allowed_chat_ids=chat_ids)
    executor = CommandExecutor()
    ai_client = AIClient(executor=executor)

    # Setup Telegram bot
    app = ApplicationBuilder().token(config["telegram_bot_token"]).build()

    # Register handlers
    handlers = setup_handlers(security, executor, ai_client)

    # Command handlers
    app.add_handler(CommandHandler("start", handlers["start"]))
    app.add_handler(CommandHandler("help", handlers["help"]))
    app.add_handler(CommandHandler("clear", handlers["clear"]))
    app.add_handler(CommandHandler("last_error", handlers["last_error"]))

    # Global error handler
    async def error_handler(update, context):
        logger.error(f"Unhandled error: {context.error}", exc_info=context.error)
        if update and update.effective_chat:
            try:
                await update.effective_chat.send_message(
                    f"❌ Unexpected error (bot still running): {str(context.error)[:200]}\n"
                    f"Use /last_error for details."
                )
            except Exception:
                pass

    app.add_error_handler(error_handler)

    # Catch-all text handler for AI chat (must be last)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handlers["ai_chat"])
    )

    # Start polling
    logger.info("Bot started! Polling for messages...")
    app.run_polling(
        poll_interval=2.0,
        timeout=30,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
