"""Telegram command handlers — quick commands and AI chat routing."""

import logging
import traceback
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode, ChatAction

from .ai_client import AIClient
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

# Predefined quick commands
QUICK_COMMANDS = {
    "pods": {
        "description": "List Jenkins namespace pods",
        "command": (
            "export KUBECONFIG=/home/rogueone/.kube/config-merged; "
            "kubectl get pods -n jenkins --context kubernetes-admin@Lift-Cluster "
            "--no-headers 2>&1 | head -60"
        ),
    },
    "nodes": {
        "description": "Show K8s node status",
        "command": (
            "export KUBECONFIG=/home/rogueone/.kube/config-merged; "
            "kubectl get nodes --context kubernetes-admin@Lift-Cluster -o wide 2>&1"
        ),
    },
    "queue": {
        "description": "Show Redis job queue count + config",
        "command": (
            "export KUBECONFIG=/home/rogueone/.kube/config-merged; "
            "echo '=== Queued Jobs ==='; "
            "kubectl exec -n default svc/lift-redis-master "
            "--context kubernetes-admin@Lift-Cluster -- "
            "redis-cli keys 'LIFTMASS:*' 2>/dev/null | wc -l; "
            "echo ''; echo '=== Config ==='; "
            "kubectl exec -n default svc/lift-redis-master "
            "--context kubernetes-admin@Lift-Cluster -- "
            "redis-cli hgetall '_LIFTMASS:CONFIG' 2>&1"
        ),
    },
    "context": {
        "description": "Show current K8s context",
        "command": (
            "export KUBECONFIG=/home/rogueone/.kube/config-merged; "
            "kubectl config current-context 2>&1; "
            "echo ''; echo '=== Available Contexts ==='; "
            "kubectl config get-contexts -o name 2>&1"
        ),
    },
}

# Telegram max message length
MAX_MSG_LEN = 4096


def _sanitize_markdown(text: str) -> str:
    """Fix common markdown issues that break Telegram's parser.

    Telegram's Markdown parser is strict about matched backticks.
    """
    import re
    # Count backticks - if odd number of ``` blocks, close the last one
    triple_count = text.count("```")
    if triple_count % 2 != 0:
        text += "\n```"
    # Count single backticks (not part of ```) - if odd, add one
    # Remove triple backticks first for counting
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

        # Try to split at a newline
        split_at = text.rfind("\n", 0, max_length)
        if split_at == -1:
            split_at = max_length

        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    return chunks


def auth_check(security: SecurityManager):
    """Decorator factory for auth + crash protection.

    Every handler is wrapped with try/except so that ANY unhandled exception
    is caught, logged, stored for /last_error, and reported to the user —
    without crashing the bot process.
    """
    def decorator(func):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            chat_id = update.effective_chat.id
            if not security.is_authorized(chat_id):
                logger.warning(f"Unauthorized access attempt from chat_id={chat_id}")
                return  # Silently ignore unauthorized users
            if not security.check_rate_limit(chat_id):
                await update.message.reply_text("⚠️ Rate limit exceeded. Try again in a minute.")
                return
            try:
                return await func(update, context)
            except Exception as e:
                # Store error for /last_error
                _store_error(chat_id, e, context_msg=f"Handler: {func.__name__}")
                logger.error(f"Handler {func.__name__} crashed: {e}", exc_info=True)
                # Notify user but DON'T crash the bot
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
                        pass  # Can't even send error message, just log it
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
            "I'm your AI-powered infrastructure ops assistant.\n\n"
            "📋 *Quick commands:*\n"
            "/status — Overview (pods + queue + nodes)\n"
            "/pods — Jenkins pods\n"
            "/nodes — K8s nodes\n"
            "/queue — Redis job queue\n"
            "/context — K8s context\n"
            "/clear — Clear conversation\n"
            "/help — All commands\n\n"
            "💬 Or just type any question in natural language!",
            parse_mode=ParseMode.MARKDOWN,
        )

    @check_auth
    async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command."""
        await update.message.reply_text(
            "🤖 *Gemini Telegram Bot — Commands*\n\n"
            "📋 *Quick Commands:*\n"
            "`/status` — Combined overview\n"
            "`/pods` — Jenkins namespace pods\n"
            "`/nodes` — K8s node status\n"
            "`/queue` — Redis job queue + config\n"
            "`/context` — Show/switch K8s context\n"
            "`/clear` — Clear conversation history\n"
            "`/confirm` — Confirm a pending command\n"
            "`/cancel` — Cancel a pending command\n\n"
            "💬 *AI Chat:*\n"
            "Just type any question without `/`:\n"
            "• _Check why autopilot jobs are failing_\n"
            "• _How many pods are on worker3?_\n"
            "• _What's the concurrent\\_job\\_limit?_\n\n"
            "🔒 *Safety:*\n"
            "🟢 Read-only → auto-execute\n"
            "🟡 Mutating → needs /confirm\n"
            "🔴 Destructive → needs /confirm",
            parse_mode=ParseMode.MARKDOWN,
        )

    @check_auth
    async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status — combined overview."""
        await update.message.reply_chat_action(ChatAction.TYPING)

        commands = [
            (
                "Nodes",
                "export KUBECONFIG=/home/rogueone/.kube/config-merged; "
                "kubectl get nodes --context kubernetes-admin@Lift-Cluster "
                "--no-headers 2>&1"
            ),
            (
                "Pod Summary",
                "export KUBECONFIG=/home/rogueone/.kube/config-merged; "
                "echo \"Running: $(kubectl get pods -n jenkins "
                "--context kubernetes-admin@Lift-Cluster "
                "--field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l)\"; "
                "echo \"Pending: $(kubectl get pods -n jenkins "
                "--context kubernetes-admin@Lift-Cluster "
                "--field-selector=status.phase=Pending --no-headers 2>/dev/null | wc -l)\"; "
                "echo \"Failed:  $(kubectl get pods -n jenkins "
                "--context kubernetes-admin@Lift-Cluster "
                "--field-selector=status.phase=Failed --no-headers 2>/dev/null | wc -l)\""
            ),
            (
                "Queue",
                "export KUBECONFIG=/home/rogueone/.kube/config-merged; "
                "echo \"Queued jobs: $(kubectl exec -n default svc/lift-redis-master "
                "--context kubernetes-admin@Lift-Cluster -- "
                "redis-cli keys 'LIFTMASS:*' 2>/dev/null | wc -l)\"; "
                "echo \"concurrent_job_limit: $(kubectl exec -n default svc/lift-redis-master "
                "--context kubernetes-admin@Lift-Cluster -- "
                "redis-cli hget '_LIFTMASS:CONFIG' 'concurrent_job_limit' 2>/dev/null)\""
            ),
        ]

        parts = ["📊 *Lift Status Overview*\n"]

        for label, cmd in commands:
            result = await executor.execute(cmd, timeout=60)
            status_icon = "✅" if result.success else "❌"
            output = result.output[:800]
            parts.append(f"\n{status_icon} *{label}*\n```\n{output}\n```")

        response = "\n".join(parts)
        for chunk in split_message(response):
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)

    @check_auth
    async def quick_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle predefined quick commands (/pods, /nodes, /queue, /context)."""
        command_name = update.message.text.strip("/").split()[0].lower()

        if command_name not in QUICK_COMMANDS:
            await update.message.reply_text(f"Unknown command: /{command_name}")
            return

        await update.message.reply_chat_action(ChatAction.TYPING)

        cmd_info = QUICK_COMMANDS[command_name]
        result = await executor.execute(cmd_info["command"], timeout=60)

        response = result.format_for_telegram()
        for chunk in split_message(response):
            try:
                await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                # Fallback without markdown if parsing fails
                await update.message.reply_text(chunk)

    @check_auth
    async def confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /confirm — execute a pending command."""
        chat_id = update.effective_chat.id
        pending = security.get_pending(chat_id)

        if not pending:
            await update.message.reply_text("No pending command to confirm.")
            return

        await update.message.reply_chat_action(ChatAction.TYPING)
        await update.message.reply_text(f"⏳ Executing confirmed command...")

        response = await ai_client.execute_confirmed(chat_id, pending.command)

        for chunk in split_message(response):
            try:
                await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                await update.message.reply_text(chunk)

    @check_auth
    async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /cancel — cancel a pending command."""
        chat_id = update.effective_chat.id
        security.clear_pending(chat_id)
        await update.message.reply_text("✅ Pending command cancelled.")

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
        """Handle freeform text messages — route to AI."""
        chat_id = update.effective_chat.id
        user_message = update.message.text

        if not user_message:
            return

        await update.message.reply_chat_action(ChatAction.TYPING)

        # Progress callback — sends real-time status updates to Telegram
        async def send_progress(msg: str):
            try:
                await update.message.reply_chat_action(ChatAction.TYPING)
                await update.effective_chat.send_message(
                    f"💭 {msg}",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                try:
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
                # Fallback: try without markdown (strip all backticks)
                try:
                    await update.message.reply_text(chunk)
                except Exception as e:
                    logger.error(f"Failed to send message: {e}")

    return {
        "start": start_handler,
        "help": help_handler,
        "status": status_handler,
        "pods": quick_command_handler,
        "nodes": quick_command_handler,
        "queue": quick_command_handler,
        "context": quick_command_handler,
        "confirm": confirm_handler,
        "cancel": cancel_handler,
        "clear": clear_handler,
        "last_error": last_error_handler,
        "ai_chat": ai_chat_handler,
    }
