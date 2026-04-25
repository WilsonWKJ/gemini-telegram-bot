"""Telegram command handlers — simple wrapper around Gemini CLI."""

import logging
import time
import traceback
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode, ChatAction

from .ai_client import AIClient, _load_system_prompt
from .executor import CommandExecutor
from .security import SecurityManager

logger = logging.getLogger(__name__)

# Store last errors per chat_id for /last_error command
_last_errors: dict[int, dict] = {}


def _store_error(chat_id: int, error: Exception, context_msg: str = ""):
    """Store the last error for a chat so the user can query it later."""
    _last_errors[chat_id] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "error_type": type(error).__name__,
        "error_message": str(error)[:1000],
        "traceback": traceback.format_exc()[-2000:],
        "context": context_msg,
    }


# Telegram max message length
MAX_MSG_LEN = 4096


def _sanitize_markdown(text: str) -> str:
    """Fix common markdown issues that break Telegram's parser."""
    triple_count = text.count("```")
    if triple_count % 2 != 0:
        text += "\n```"
    temp = text.replace("```", "")
    single_count = temp.count("`")
    if single_count % 2 != 0:
        text += "`"
    return text


def split_message(text: str, max_length: int = MAX_MSG_LEN) -> list[str]:
    """Split a long message into multiple Telegram-safe chunks."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_length)
        if split_at == -1:
            split_at = max_length
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    return chunks


def auth_check(security: SecurityManager):
    """Decorator factory for auth + crash protection."""
    def decorator(func):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            chat_id = update.effective_chat.id
            if not security.is_authorized(chat_id):
                logger.warning(f"Unauthorized access attempt from chat_id={chat_id}")
                return
            if not security.check_rate_limit(chat_id):
                await update.message.reply_text("⚠️ Rate limit exceeded. Try again in a minute.")
                return
            try:
                return await func(update, context)
            except Exception as e:
                _store_error(chat_id, e, context_msg=f"Handler: {func.__name__}")
                logger.error(f"Handler {func.__name__} crashed: {e}", exc_info=True)
                try:
                    await update.message.reply_text(
                        f"❌ Something went wrong, but the bot is still running.\n\n"
                        f"Error: `{type(e).__name__}: {str(e)[:200]}`\n\n"
                        f"Use /last\\_error for full details.\n"
                        f"Use /clear to reset conversation.",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    try:
                        await update.message.reply_text(
                            f"❌ Error: {type(e).__name__}: {str(e)[:200]}\n"
                            f"Bot is still running. Use /last_error for details."
                        )
                    except Exception:
                        pass
        return wrapper
    return decorator


def setup_handlers(
    security: SecurityManager,
    executor: CommandExecutor,
    ai_client: AIClient,
):
    """Create handler functions bound to the shared instances."""
    check_auth = auth_check(security)

    @check_auth
    async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command."""
        await update.message.reply_text(
            "🤖 *Gemini Telegram Bot*\n\n"
            "I'm powered by Google Gemini CLI.\n\n"
            "📋 *Commands:*\n"
            "/status — Bot health check\n"
            "/model — View or switch AI model\n"
            "/check\\_system\\_prompt — View & explain system prompt\n"
            "/clear — Clear conversation history\n"
            "/last\\_error — Show last error details\n"
            "/help — Show this help\n\n"
            "💬 Just type anything to chat with Gemini!",
            parse_mode=ParseMode.MARKDOWN,
        )

    @check_auth
    async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command."""
        await update.message.reply_text(
            "🤖 *Gemini Telegram Bot*\n\n"
            "💬 Just type any message to chat with Gemini.\n"
            "Gemini can read its own source code and explain how it works.\n\n"
            "📋 *Commands:*\n"
            "`/status` — Bot health check\n"
            "`/model` — View or switch AI model\n"
            "`/check_system_prompt` — View & explain system prompt\n"
            "`/clear` — Clear conversation history\n"
            "`/last_error` — Show last error details\n"
            "`/help` — Show this help\n",
            parse_mode=ParseMode.MARKDOWN,
        )

    @check_auth
    async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status — show bot health and stats."""
        chat_id = update.effective_chat.id
        uptime_secs = time.time() - ai_client.start_time
        days, remainder = divmod(int(uptime_secs), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        if days > 0:
            uptime_str = f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            uptime_str = f"{hours}h {minutes}m"
        else:
            uptime_str = f"{minutes}m"

        active_chats = len(ai_client.conversations)
        current_model = ai_client._get_model(chat_id)

        # Quick check: can we reach gemini CLI?
        check_cmd = (
            'export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh" && '
            'nvm use 22 >/dev/null 2>&1 && gemini --version 2>&1'
        )
        result = await executor.execute(check_cmd, timeout=15)
        gemini_version = result.stdout.strip() if result.success else "unavailable"

        await update.message.reply_text(
            f"📊 *Bot Status*\n\n"
            f"✅ Running\n"
            f"⏱ Uptime: `{uptime_str}`\n"
            f"🤖 Gemini CLI: `{gemini_version}`\n"
            f"🧠 Model: `{current_model}`\n"
            f"💬 Active conversations: `{active_chats}`\n"
            f"🔒 Rate limit: `{security.rate_limit} req/min`",
            parse_mode=ParseMode.MARKDOWN,
        )

    @check_auth
    async def model_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /model — view or switch model."""
        chat_id = update.effective_chat.id
        args = context.args

        valid_models = ["flash", "pro", "flash-lite", "auto"]
        
        if not args:
            current = ai_client._get_model(chat_id)
            await update.message.reply_text(
                f"🧠 *Model Selection*\n\n"
                f"Current model: `{current}`\n\n"
                f"To switch, use:\n"
                f"`/model flash` — Fast & balanced (Default)\n"
                f"`/model pro` — High reasoning, lower quota\n"
                f"`/model flash-lite` — Fastest & lightest\n"
                f"`/model auto` — Best available\n",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        new_model = args[0].lower()
        if new_model not in valid_models:
            await update.message.reply_text(
                f"❌ Invalid model: `{new_model}`\n"
                f"Valid options: {', '.join(valid_models)}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        ai_client.set_model(chat_id, new_model)
        await update.message.reply_text(
            f"✅ Model switched to `{new_model}`.",
            parse_mode=ParseMode.MARKDOWN,
        )

    @check_auth
    async def check_system_prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /check_system_prompt — show current system prompt and ask Gemini to explain it."""
        chat_id = update.effective_chat.id
        system_prompt = _load_system_prompt()

        # Send the raw system prompt first
        header = "📋 *Current System Prompt:*\n\n"
        for chunk in split_message(header + f"```\n{system_prompt}\n```"):
            try:
                await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                await update.message.reply_text(chunk)

        # Ask Gemini to explain it
        await update.message.reply_chat_action(ChatAction.TYPING)

        async def send_progress(msg: str):
            try:
                await update.message.reply_chat_action(ChatAction.TYPING)
                await update.effective_chat.send_message(f"💭 {msg}")
            except Exception:
                pass

        explain_msg = (
            "以下是你目前的 system prompt，請用繁體中文簡要解釋每個段落會讓你做什麼事情，"
            "以及這些指令對你的行為有什麼影響：\n\n"
            f"{system_prompt}"
        )
        try:
            response = await ai_client.chat(chat_id, explain_msg, progress_callback=send_progress)
        except Exception as e:
            logger.error(f"check_system_prompt error: {e}", exc_info=True)
            response = f"❌ Error asking Gemini to explain: {str(e)[:200]}"

        for chunk in split_message(response):
            try:
                sanitized = _sanitize_markdown(chunk)
                await update.message.reply_text(sanitized, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                try:
                    await update.message.reply_text(chunk)
                except Exception as e:
                    logger.error(f"Failed to send message: {e}")

    @check_auth
    async def clear_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /clear — clear conversation history."""
        chat_id = update.effective_chat.id
        ai_client.clear_history(chat_id)
        await update.message.reply_text("🗑️ Conversation history cleared.")

    @check_auth
    async def last_error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /last_error — show the last error that occurred."""
        chat_id = update.effective_chat.id
        error_info = _last_errors.get(chat_id)

        if not error_info:
            await update.message.reply_text("✅ No errors recorded since bot started.")
            return

        msg = (
            f"🔍 *Last Error*\n\n"
            f"⏰ Time: `{error_info['timestamp']}`\n"
            f"📍 Context: {error_info['context']}\n"
            f"❌ Type: `{error_info['error_type']}`\n"
            f"💬 Message: `{error_info['error_message'][:300]}`\n\n"
            f"📋 Traceback:\n```\n{error_info['traceback'][:1500]}\n```"
        )
        for chunk in split_message(msg):
            try:
                await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                await update.message.reply_text(chunk)

    @check_auth
    async def ai_chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle freeform text messages — route to Gemini CLI."""
        chat_id = update.effective_chat.id
        user_message = update.message.text

        if not user_message:
            return

        await update.message.reply_chat_action(ChatAction.TYPING)

        # Progress callback
        async def send_progress(msg: str):
            try:
                await update.message.reply_chat_action(ChatAction.TYPING)
                await update.effective_chat.send_message(f"💭 {msg}")
            except Exception:
                pass

        try:
            response = await ai_client.chat(chat_id, user_message, progress_callback=send_progress)
        except Exception as e:
            logger.error(f"AI chat error: {e}", exc_info=True)
            response = f"❌ Error: {str(e)[:200]}"

        for chunk in split_message(response):
            try:
                sanitized = _sanitize_markdown(chunk)
                await update.message.reply_text(sanitized, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                try:
                    await update.message.reply_text(chunk)
                except Exception as e:
                    logger.error(f"Failed to send message: {e}")

    return {
        "start": start_handler,
        "help": help_handler,
        "status": status_handler,
        "check_system_prompt": check_system_prompt_handler,
        "clear": clear_handler,
        "last_error": last_error_handler,
        "ai_chat": ai_chat_handler,
    }
