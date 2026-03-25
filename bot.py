import os
import random
from dataclasses import dataclass

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes


DEFAULT_BROADCAST_CHAT_ID = -1003744224655
ONLINE_NOW_MESSAGES = (
    "{bot_name} is online now.",
    "{bot_name} just came online.",
    "{bot_name} is up and running.",
    "{bot_name} is online now and ready.",
    "{bot_name} woke up and is online now.",
)


@dataclass(frozen=True)
class VouchRequest:
    action_label: str
    target: str
    reason: str


def _get_required_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Set the TELEGRAM_BOT_TOKEN environment variable before starting the bot."
        )
    return token


def _get_broadcast_chat_id() -> int:
    raw_chat_id = os.getenv("TELEGRAM_BROADCAST_CHAT_ID")
    if raw_chat_id is None:
        return DEFAULT_BROADCAST_CHAT_ID
    return int(raw_chat_id)


def _build_online_now_message(bot_name: str) -> str:
    template = random.choice(ONLINE_NOW_MESSAGES)
    return template.format(bot_name=bot_name)


def _build_actor_name(update: Update) -> str:
    user = update.effective_user
    if user is None:
        raise RuntimeError("A Telegram user is required to create a vouch action.")

    if user.username:
        return f"{user.full_name} (@{user.username})"
    return user.full_name


def _build_source_chat(update: Update) -> str:
    chat = update.effective_chat
    if chat is None:
        raise RuntimeError("A Telegram chat is required to create a vouch action.")

    title = chat.title or chat.full_name or chat.username or "Direct Message"
    return f"{title} ({chat.id})"


def _parse_vouch_request(
    command_name: str,
    action_label: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> VouchRequest:
    if not context.args or len(context.args) < 2:
        raise ValueError(f"Usage: /{command_name} @username reason")

    target = context.args[0].strip()
    reason = " ".join(context.args[1:]).strip()

    if not target.startswith("@"):
        raise ValueError(f"Usage: /{command_name} @username reason")

    if not reason:
        raise ValueError(f"Usage: /{command_name} @username reason")

    return VouchRequest(
        action_label=action_label,
        target=target,
        reason=reason,
    )


async def _handle_vouch_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command_name: str,
    action_label: str,
) -> None:
    message = update.effective_message
    if message is None:
        raise RuntimeError("A Telegram message is required to process a vouch action.")

    try:
        request = _parse_vouch_request(
            command_name=command_name,
            action_label=action_label,
            context=context,
        )
    except ValueError as error:
        await message.reply_text(str(error))
        return

    actor = _build_actor_name(update)
    source_chat = _build_source_chat(update)
    broadcast_message = (
        f"{request.action_label}\n"
        f"Target: {request.target}\n"
        f"From: {actor}\n"
        f"Reason: {request.reason}\n"
        f"Source chat: {source_chat}"
    )

    await context.bot.send_message(
        chat_id=_get_broadcast_chat_id(),
        text=broadcast_message,
    )
    await message.reply_text(
        f"{request.action_label} for {request.target} was broadcast successfully."
    )


async def vouch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_vouch_action(update, context, "vouch", "Vouch")


async def unvouch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_vouch_action(update, context, "unvouch", "Unvouch")


async def negvouch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_vouch_action(update, context, "negvouch", "Negative vouch")


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("vouch", "Broadcast a positive vouch"),
            BotCommand("unvouch", "Broadcast an unvouch"),
            BotCommand("negvouch", "Broadcast a negative vouch"),
        ]
    )
    me = await application.bot.get_me()
    bot_name = f"@{me.username}" if me.username else me.full_name
    await application.bot.send_message(
        chat_id=_get_broadcast_chat_id(),
        text=_build_online_now_message(bot_name),
    )


def run_bot() -> None:
    application = (
        Application.builder()
        .token(_get_required_token())
        .post_init(post_init)
        .build()
    )
    application.add_handler(CommandHandler("vouch", vouch))
    application.add_handler(CommandHandler("unvouch", unvouch))
    application.add_handler(CommandHandler("negvouch", negvouch))
    application.run_polling()
