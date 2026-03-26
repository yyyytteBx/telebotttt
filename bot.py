import os
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

load_dotenv()
load_dotenv(os.path.join('.venv', '.env'))


DEFAULT_BROADCAST_CHAT_ID = -1003744224655
VOUCH_COOLDOWN_HOURS = 24
ELITE_THRESHOLD = 20
ONLINE_NOW_MESSAGES = (
    "loading loading loading\nme: 🧍‍♂️\nstill loading loading loading",
    "loading...\nscrolling...\nstill loading...\ngo back to top 🔁",
    "loading [■■■■□□□□□□] 40%\nloading [■■■■■■■□□□] 70%\nloading [■■■■■■■■■■] 100%\njust kidding... loading again",
    "loading...\njust loading...\nalways loading...",
    "LOADING LOADING LOADING LOADING LOADING LOADING LOADING\nLOADING LOADING LOADING LOADING LOADING LOADING",
    "loading...\nloading...\nLOADING...\nwhy is it still loading",
)
RANDOM_VOUCH_LINES = (
    "Built different 💪",
    "Certified legend 🏆",
    "Smooth deal 🔥",
    "Trusted like WiFi 📶",
)

# ---------------- DB SETUP ----------------
_db = sqlite3.connect("vouch.db", check_same_thread=False)
_cur = _db.cursor()

_cur.executescript("""
CREATE TABLE IF NOT EXISTS vouches (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user      TEXT,
    from_user TEXT,
    text      TEXT,
    date      TEXT
);
CREATE TABLE IF NOT EXISTS limits (
    user  TEXT,
    date  TEXT,
    count INTEGER
);
CREATE TABLE IF NOT EXISTS blacklist (
    user     TEXT PRIMARY KEY,
    reason   TEXT,
    added_by TEXT,
    date     TEXT
);
""")
_db.commit()


# ---------------- DB HELPERS ----------------
def _get_rank(count: int) -> str:
    if count < 5:
        return "Newbie 🐣"
    elif count < ELITE_THRESHOLD:
        return "Trusted ✅"
    return "Elite 🏆"


def _can_vouch(user: str) -> bool:
    today = datetime.now().date().isoformat()
    _cur.execute("SELECT count FROM limits WHERE user=? AND date=?", (user, today))
    row = _cur.fetchone()
    if not row:
        _cur.execute("INSERT INTO limits VALUES (?, ?, ?)", (user, today, 1))
        _db.commit()
        return True
    if row[0] >= 3:
        return False
    _cur.execute(
        "UPDATE limits SET count = count + 1 WHERE user=? AND date=?", (user, today)
    )
    _db.commit()
    return True


def _on_cooldown(from_user: str, target: str) -> bool:
    cutoff = (datetime.now() - timedelta(hours=VOUCH_COOLDOWN_HOURS)).isoformat()
    _cur.execute(
        "SELECT 1 FROM vouches WHERE user=? AND from_user=? AND date > ?",
        (target, from_user, cutoff),
    )
    return _cur.fetchone() is not None


def _is_blacklisted(user: str) -> tuple[bool, str]:
    _cur.execute("SELECT reason FROM blacklist WHERE user=?", (user,))
    row = _cur.fetchone()
    return (True, row[0]) if row else (False, "")


# ---------------- BROADCAST HELPERS ----------------
@dataclass(frozen=True)
class VouchRequest:
    action_label: str
    target: str
    reason: str


def _get_required_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN. Put it in project .env (preferred) or .venv/.env."
        )
    return token


def _get_broadcast_chat_id() -> int:
    raw_chat_id = os.getenv("TELEGRAM_BROADCAST_CHAT_ID")
    if raw_chat_id is None:
        return DEFAULT_BROADCAST_CHAT_ID
    return int(raw_chat_id)


def _build_online_now_message(bot_name: str) -> str:
    return random.choice(ONLINE_NOW_MESSAGES).format(bot_name=bot_name)


def _build_actor_name(update: Update) -> str:
    user = update.effective_user
    if user is None:
        raise RuntimeError("A Telegram user is required to create a vouch action.")
    return f"{user.full_name} (@{user.username})" if user.username else user.full_name


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
    return VouchRequest(action_label=action_label, target=target, reason=reason)


async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return False
    if chat.type == "private":
        return True
    member = await context.bot.get_chat_member(chat.id, user.id)
    return member.status in ("administrator", "creator")


async def _broadcast_vouch(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command_name: str,
    action_label: str,
    admin_only: bool = False,
) -> None:
    message = update.effective_message
    if message is None:
        return
    if admin_only and not await _is_admin(update, context):
        await message.reply_text("❌ Only group admins can use this command.")
        return
    try:
        request = _parse_vouch_request(command_name, action_label, context)
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
    await context.bot.send_message(chat_id=_get_broadcast_chat_id(), text=broadcast_message)
    await message.reply_text(
        f"{request.action_label} for {request.target} was broadcast successfully."
    )


# ---------------- COMMANDS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        "👋 Welcome to Vouch Bot\n\n"
        "Track reputation, view profiles, and manage trusted deals.\n\n"
        "Quick commands:\n"
        "/vouch @user message\n"
        "/vouchanon @user message\n"
        "/profile @user\n"
        "/vouches @user\n"
        "/top\n"
        "/recent\n"
        "/stats\n"
        "/groupinfo\n\n"
        "Use /search @user for more details."
    )


async def vouch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not context.args:
        await message.reply_text("Usage: /vouch @user message")
        return

    from_user = update.effective_user.username or str(update.effective_user.id)
    target = context.args[0]

    if not _can_vouch(from_user):
        await message.reply_text("❌ Daily vouch limit reached (3)")
        return

    if _on_cooldown(from_user, target):
        await message.reply_text(
            f"⏱️ You already vouched for {target} in the last {VOUCH_COOLDOWN_HOURS}h"
        )
        return

    text = " ".join(context.args[1:]).strip() or random.choice(RANDOM_VOUCH_LINES)

    _cur.execute(
        "INSERT INTO vouches (user, from_user, text, date) VALUES (?, ?, ?, ?)",
        (target, from_user, text, datetime.now().isoformat()),
    )
    _db.commit()

    # Check if target just crossed Elite threshold — announce it
    _cur.execute("SELECT COUNT(*) FROM vouches WHERE user=?", (target,))
    new_count = _cur.fetchone()[0]
    if new_count == ELITE_THRESHOLD:
        await context.bot.send_message(
            chat_id=_get_broadcast_chat_id(),
            text=f"🏆 {target} just reached Elite rank with {new_count} vouches!",
        )

    actor = _build_actor_name(update)
    source_chat = _build_source_chat(update)
    await context.bot.send_message(
        chat_id=_get_broadcast_chat_id(),
        text=(
            f"Vouch\nTarget: {target}\nFrom: {actor}\nReason: {text}\n"
            f"Source chat: {source_chat}\n"
            "👍🔥❌"
        ),
    )

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("👍", callback_data="legit"),
            InlineKeyboardButton("🔥", callback_data="fire"),
            InlineKeyboardButton("❌", callback_data="cap"),
        ]]
    )
    await message.reply_text(
        f"🧾 VOUCH\nFrom: @{from_user}\nTo: {target}\n💬 {text}",
        reply_markup=keyboard,
    )


async def vouchanon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not context.args:
        if message:
            await message.reply_text("Usage: /vouchanon @user message")
        return
    target = context.args[0]
    text = " ".join(context.args[1:]).strip() or random.choice(RANDOM_VOUCH_LINES)
    _cur.execute(
        "INSERT INTO vouches (user, from_user, text, date) VALUES (?, ?, ?, ?)",
        (target, "anonymous", text, datetime.now().isoformat()),
    )
    _db.commit()
    await message.reply_text(f"👀 Someone vouched for {target}\n💬 {text}")


async def removevouch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not context.args:
        await message.reply_text("Usage: /removevouch @user")
        return
    from_user = update.effective_user.username or str(update.effective_user.id)
    target = context.args[0]
    _cur.execute(
        "DELETE FROM vouches WHERE id = ("
        "  SELECT id FROM vouches WHERE user=? AND from_user=? ORDER BY date DESC LIMIT 1"
        ")",
        (target, from_user),
    )
    _db.commit()
    if _cur.rowcount:
        await message.reply_text(f"🗑️ Your most recent vouch for {target} was removed.")
    else:
        await message.reply_text(f"No vouch from you to {target} found.")


async def unvouch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _broadcast_vouch(update, context, "unvouch", "Unvouch")


async def negvouch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _broadcast_vouch(update, context, "negvouch", "Negative vouch", admin_only=True)


async def blacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _is_admin(update, context):
        await message.reply_text("❌ Only group admins can use this command.")
        return
    if not context.args or len(context.args) < 2:
        await message.reply_text("Usage: /blacklist @user reason")
        return
    target = context.args[0]
    reason = " ".join(context.args[1:])
    added_by = update.effective_user.username or str(update.effective_user.id)
    _cur.execute(
        "INSERT OR REPLACE INTO blacklist (user, reason, added_by, date) VALUES (?, ?, ?, ?)",
        (target, reason, added_by, datetime.now().isoformat()),
    )
    _db.commit()
    await message.reply_text(f"🚫 {target} has been blacklisted.\nReason: {reason}")
    await context.bot.send_message(
        chat_id=_get_broadcast_chat_id(),
        text=f"🚫 BLACKLIST\nUser: {target}\nReason: {reason}\nAdded by: @{added_by}",
    )


async def unblacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _is_admin(update, context):
        await message.reply_text("❌ Only group admins can use this command.")
        return
    if not context.args:
        await message.reply_text("Usage: /unblacklist @user")
        return
    target = context.args[0]
    _cur.execute("DELETE FROM blacklist WHERE user=?", (target,))
    _db.commit()
    if _cur.rowcount:
        await message.reply_text(f"✅ {target} has been removed from the blacklist.")
    else:
        await message.reply_text(f"{target} is not on the blacklist.")


async def vouches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not context.args:
        await message.reply_text("Usage: /vouches @user")
        return
    target = context.args[0]
    _cur.execute("SELECT from_user, text FROM vouches WHERE user=?", (target,))
    rows = _cur.fetchall()
    if not rows:
        await message.reply_text("No vouches yet.")
        return
    msg = f"📜 Vouches for {target}:\n\n"
    for r in rows[-10:]:
        msg += f"@{r[0]}: {r[1]}\n"
    await message.reply_text(msg)


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not context.args:
        await message.reply_text("Usage: /history @user")
        return
    target = context.args[0]
    _cur.execute(
        "SELECT from_user, text, date FROM vouches WHERE user=? ORDER BY date ASC",
        (target,),
    )
    rows = _cur.fetchall()
    if not rows:
        await message.reply_text(f"No vouches found for {target}.")
        return
    header = f"📜 Full vouch history for {target} ({len(rows)} total):\n\n"
    lines_out = []
    for r in rows:
        from_user, text, date = r
        date_str = f" [{date}]" if date else ""
        lines_out.append(f"@{from_user}{date_str}: {text}")
    # Send in chunks to respect Telegram's 4096-character message limit
    chunk = header
    for line in lines_out:
        if len(chunk) + len(line) + 1 > 4095:
            await message.reply_text(chunk)
            chunk = f"📜 (continued) Vouches for {target}:\n\n"
        chunk += line + "\n"
    if chunk:
        await message.reply_text(chunk)


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not context.args:
        await message.reply_text("Usage: /profile @user")
        return
    target = context.args[0]
    _cur.execute("SELECT COUNT(*) FROM vouches WHERE user=?", (target,))
    count = _cur.fetchone()[0]
    blacklisted, bl_reason = _is_blacklisted(target)
    lines = [
        f"👤 {target}",
        f"⭐ Vouches: {count}",
        f"🏆 Rank: {_get_rank(count)}",
    ]
    if blacklisted:
        lines.append(f"\n🚫 BLACKLISTED\nReason: {bl_reason}")
    await message.reply_text("\n".join(lines))


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    me = update.effective_user.username or str(update.effective_user.id)
    _cur.execute("SELECT COUNT(*) FROM vouches WHERE from_user=?", (me,))
    given = _cur.fetchone()[0]
    _cur.execute("SELECT COUNT(*) FROM vouches WHERE user=?", (f"@{me}",))
    received = _cur.fetchone()[0]
    await message.reply_text(
        f"📊 Your Stats (@{me})\n"
        f"✅ Vouches given: {given}\n"
        f"⭐ Vouches received: {received}\n"
        f"🏆 Rank: {_get_rank(received)}"
    )


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not context.args:
        await message.reply_text("Usage: /search @user")
        return
    target = context.args[0]
    _cur.execute("SELECT from_user, text FROM vouches WHERE user=?", (target,))
    received = _cur.fetchall()
    _cur.execute("SELECT user, text FROM vouches WHERE from_user=?", (target.lstrip("@"),))
    given = _cur.fetchall()
    parts = [f"🔍 Search results for {target}"]
    if received:
        parts.append(f"\n📥 Received ({len(received)}):")
        for r in received[-5:]:
            parts.append(f"  @{r[0]}: {r[1]}")
    else:
        parts.append("\n📥 Received: none")
    if given:
        parts.append(f"\n📤 Given ({len(given)}):")
        for r in given[-5:]:
            parts.append(f"  → {r[0]}: {r[1]}")
    else:
        parts.append("\n📤 Given: none")
    await message.reply_text("\n".join(parts))


async def recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    _cur.execute(
        "SELECT from_user, user, text, date FROM vouches ORDER BY date DESC LIMIT 5"
    )
    rows = _cur.fetchall()
    if not rows:
        await message.reply_text("No vouches recorded yet.")
        return
    msg = "📌 Recent Vouches:\n\n"
    for r in rows:
        ts = r[3][:10]
        msg += f"@{r[0]} → {r[1]}: {r[2]} [{ts}]\n"
    await message.reply_text(msg)


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    _cur.execute(
        "SELECT user, COUNT(*) as c FROM vouches GROUP BY user ORDER BY c DESC LIMIT 10"
    )
    rows = _cur.fetchall()
    if not rows:
        await message.reply_text("No vouches recorded yet.")
        return
    msg = "🏆 Top Trusted Users:\n\n"
    for i, r in enumerate(rows, 1):
        msg += f"{i}. {r[0]} — {r[1]} vouches\n"
    await message.reply_text(msg)


async def groupinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    full_chat = await context.bot.get_chat(chat.id)
    title = full_chat.title or full_chat.full_name or full_chat.username or "N/A"
    username = f"@{full_chat.username}" if full_chat.username else "N/A"
    description = full_chat.description or "N/A"
    member_count = await context.bot.get_chat_member_count(chat.id)
    invite_link = full_chat.invite_link or "N/A"
    await message.reply_text(
        "\n".join([
            "📋 Group Info",
            f"Title: {title}",
            f"Type: {full_chat.type}",
            f"ID: {full_chat.id}",
            f"Username: {username}",
            f"Members: {member_count}",
            f"Description: {description}",
            f"Invite link: {invite_link}",
        ])
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        "📖 Vouch Bot — Command Reference\n\n"

        "── Vouching ──\n"
        "/vouch @user [message] — Vouch for a user (stored & broadcast)\n"
        "/vouchanon @user [message] — Vouch anonymously\n"
        "/removevouch @user — Remove your most recent vouch for a user\n"
        "/unvouch @user reason — Broadcast a public unvouch\n"
        "/negvouch @user reason — Broadcast a negative vouch (admin only)\n\n"

        "── Profiles & Stats ──\n"
        "/vouches @user — View all vouches for a user\n"
        "/profile @user — View a user's full vouch profile\n"
        "/stats — View your own vouch stats\n\n"

        "── Discovery ──\n"
        "/search @user — Search vouches given and received\n"
        "/recent — Last 5 vouches across all users\n"
        "/top — Top 10 most vouched users\n\n"

        "── Moderation (admin only) ──\n"
        "/blacklist @user reason — Blacklist a user\n"
        "/unblacklist @user — Remove a user from the blacklist\n\n"

        "── Group & Misc ──\n"
        "/groupinfo — Export info about this group\n"
        "/start — Welcome message\n"
        "/help — Show this help message",
    )


async def on_reaction_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    reactions = {"legit": "👍 Legit!", "fire": "🔥 Fire!", "cap": "❌ Cap!"}
    await query.answer(reactions.get(query.data, ""))


# ---------------- STARTUP ----------------
async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("start", "Welcome message and command summary"),
        BotCommand("help", "Show all commands and usage"),
        BotCommand("vouch", "Vouch for a user (stored + broadcast)"),
        BotCommand("vouchanon", "Vouch anonymously"),
        BotCommand("removevouch", "Remove your last vouch for a user"),
        BotCommand("unvouch", "Broadcast an unvouch"),
        BotCommand("negvouch", "Broadcast a negative vouch (admin only)"),
        BotCommand("vouches", "View vouches for a user"),
        BotCommand("history", "View full history of vouches received by a user"),
        BotCommand("profile", "View a user's vouch profile"),
        BotCommand("stats", "View your own vouch stats"),
        BotCommand("search", "Search vouches given and received by a user"),
        BotCommand("recent", "Last 5 vouches across all users"),
        BotCommand("top", "Top 10 most vouched users"),
        BotCommand("blacklist", "Blacklist a user (admin only)"),
        BotCommand("unblacklist", "Remove a user from blacklist (admin only)"),
        BotCommand("groupinfo", "Export info about this group"),
    ])
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
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("vouch", vouch))
    application.add_handler(CommandHandler("vouchanon", vouchanon))
    application.add_handler(CommandHandler("removevouch", removevouch))
    application.add_handler(CommandHandler("unvouch", unvouch))
    application.add_handler(CommandHandler("negvouch", negvouch))
    application.add_handler(CommandHandler("vouches", vouches_cmd))
    application.add_handler(CommandHandler("history", history))
    application.add_handler(CommandHandler("profile", profile))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("search", search))
    application.add_handler(CommandHandler("recent", recent))
    application.add_handler(CommandHandler("top", top))
    application.add_handler(CommandHandler("blacklist", blacklist_cmd))
    application.add_handler(CommandHandler("unblacklist", unblacklist_cmd))
    application.add_handler(CommandHandler("groupinfo", groupinfo))
    application.add_handler(CallbackQueryHandler(on_reaction_button))
    application.run_polling()


# ---------------- DB SETUP ----------------
_db = sqlite3.connect("vouch.db", check_same_thread=False)
_cur = _db.cursor()

_cur.executescript("""
CREATE TABLE IF NOT EXISTS vouches (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user      TEXT,
    from_user TEXT,
    text      TEXT,
    date      TEXT
);
CREATE TABLE IF NOT EXISTS limits (
    user  TEXT,
    date  TEXT,
    count INTEGER
);
""")
_db.commit()


# ---------------- DB HELPERS ----------------
def _get_rank(count: int) -> str:
    if count < 5:
        return "Newbie 🐣"
    elif count < 20:
        return "Trusted ✅"
    return "Elite 🏆"


def _can_vouch(user: str) -> bool:
    today = datetime.now().date().isoformat()
    _cur.execute("SELECT count FROM limits WHERE user=? AND date=?", (user, today))
    row = _cur.fetchone()
    if not row:
        _cur.execute("INSERT INTO limits VALUES (?, ?, ?)", (user, today, 1))
        _db.commit()
        return True
    if row[0] >= 3:
        return False
    _cur.execute(
        "UPDATE limits SET count = count + 1 WHERE user=? AND date=?", (user, today)
    )
    _db.commit()
    return True


# ---------------- BROADCAST HELPERS ----------------
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
    return random.choice(ONLINE_NOW_MESSAGES).format(bot_name=bot_name)


def _build_actor_name(update: Update) -> str:
    user = update.effective_user
    if user is None:
        raise RuntimeError("A Telegram user is required to create a vouch action.")
    return f"{user.full_name} (@{user.username})" if user.username else user.full_name


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
    return VouchRequest(action_label=action_label, target=target, reason=reason)


async def _broadcast_vouch(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command_name: str,
    action_label: str,
) -> None:
    message = update.effective_message
    if message is None:
        return
    try:
        request = _parse_vouch_request(command_name, action_label, context)
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
    await context.bot.send_message(chat_id=_get_broadcast_chat_id(), text=broadcast_message)
    await message.reply_text(
        f"{request.action_label} for {request.target} was broadcast successfully."
    )


# ---------------- COMMANDS ----------------
async def vouch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not context.args:
        await message.reply_text("Usage: /vouch @user message")
        return

    from_user = update.effective_user.username or str(update.effective_user.id)
    target = context.args[0]

    if not _can_vouch(from_user):
        await message.reply_text("❌ Daily vouch limit reached (3)")
        return

    text = " ".join(context.args[1:]).strip() or random.choice(RANDOM_VOUCH_LINES)

    _cur.execute(
        "INSERT INTO vouches (user, from_user, text, date) VALUES (?, ?, ?, ?)",
        (target, from_user, text, datetime.now().isoformat()),
    )
    _db.commit()

    # Also broadcast to the channel
    actor = _build_actor_name(update)
    source_chat = _build_source_chat(update)
    await context.bot.send_message(
        chat_id=_get_broadcast_chat_id(),
        text=(
            f"Vouch\nTarget: {target}\nFrom: {actor}\nReason: {text}\n"
            f"Source chat: {source_chat}\n"
            "👍🔥❌"
        ),
    )

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("👍", callback_data="legit"),
            InlineKeyboardButton("🔥", callback_data="fire"),
            InlineKeyboardButton("❌", callback_data="cap"),
        ]]
    )
    await message.reply_text(
        f"🧾 VOUCH\nFrom: @{from_user}\nTo: {target}\n💬 {text}",
        reply_markup=keyboard,
    )


async def vouchanon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not context.args:
        if message:
            await message.reply_text("Usage: /vouchanon @user message")
        return
    target = context.args[0]
    text = " ".join(context.args[1:]).strip() or random.choice(RANDOM_VOUCH_LINES)
    _cur.execute(
        "INSERT INTO vouches (user, from_user, text, date) VALUES (?, ?, ?, ?)",
        (target, "anonymous", text, datetime.now().isoformat()),
    )
    _db.commit()
    await message.reply_text(f"👀 Someone vouched for {target}\n💬 {text}")


async def unvouch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _broadcast_vouch(update, context, "unvouch", "Unvouch")


async def negvouch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _broadcast_vouch(update, context, "negvouch", "Negative vouch")


async def vouches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not context.args:
        await message.reply_text("Usage: /vouches @user")
        return
    target = context.args[0]
    _cur.execute("SELECT from_user, text FROM vouches WHERE user=?", (target,))
    rows = _cur.fetchall()
    if not rows:
        await message.reply_text("No vouches yet.")
        return
    msg = f"📜 Vouches for {target}:\n\n"
    for r in rows[-10:]:
        msg += f"@{r[0]}: {r[1]}\n"
    await message.reply_text(msg)


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not context.args:
        await message.reply_text("Usage: /profile @user")
        return
    target = context.args[0]
    _cur.execute("SELECT COUNT(*) FROM vouches WHERE user=?", (target,))
    count = _cur.fetchone()[0]
    await message.reply_text(
        f"👤 {target}\n⭐ Vouches: {count}\n🏆 Rank: {_get_rank(count)}"
    )


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    _cur.execute(
        "SELECT user, COUNT(*) as c FROM vouches GROUP BY user ORDER BY c DESC LIMIT 10"
    )
    rows = _cur.fetchall()
    if not rows:
        await message.reply_text("No vouches recorded yet.")
        return
    msg = "🏆 Top Trusted Users:\n\n"
    for i, r in enumerate(rows, 1):
        msg += f"{i}. {r[0]} — {r[1]} vouches\n"
    await message.reply_text(msg)


async def groupinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    full_chat = await context.bot.get_chat(chat.id)
    title = full_chat.title or full_chat.full_name or full_chat.username or "N/A"
    username = f"@{full_chat.username}" if full_chat.username else "N/A"
    description = full_chat.description or "N/A"
    member_count = await context.bot.get_chat_member_count(chat.id)
    invite_link = full_chat.invite_link or "N/A"
    await message.reply_text(
        "\n".join([
            "📋 Group Info",
            f"Title: {title}",
            f"Type: {full_chat.type}",
            f"ID: {full_chat.id}",
            f"Username: {username}",
            f"Members: {member_count}",
            f"Description: {description}",
            f"Invite link: {invite_link}",
        ])
    )


async def scrape(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text("🔍 Scraping your account for groups and channels...")
    try:
        groups = await scrape_groups()
    except RuntimeError as error:
        await message.reply_text(f"❌ Error: {error}")
        return
    if not groups:
        await message.reply_text("No groups or channels found.")
        return
    lines = [
        f"[{g['type']}] {g['title']} ({g['id']}) — {g['link'] or 'no public link'}"
        for g in groups
    ]
    chunks: list[str] = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > 4000:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    header = f"📋 Found {len(groups)} chats:\n\n"
    for i, chunk in enumerate(chunks):
        await message.reply_text((header + chunk) if i == 0 else chunk)


async def on_reaction_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    reactions = {"legit": "👍 Legit!", "fire": "🔥 Fire!", "cap": "❌ Cap!"}
    await query.answer(reactions.get(query.data, ""))


# ---------------- STARTUP ----------------
async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("vouch", "Vouch for a user (stored + broadcast)"),
        BotCommand("vouchanon", "Vouch anonymously"),
        BotCommand("unvouch", "Broadcast an unvouch"),
        BotCommand("negvouch", "Broadcast a negative vouch"),
        BotCommand("vouches", "View vouches for a user"),
        BotCommand("history", "View full history of vouches received by a user"),
        BotCommand("profile", "View a user's vouch profile"),
        BotCommand("top", "Top 10 most vouched users"),
        BotCommand("groupinfo", "Export info about this group"),
        BotCommand("scrape", "Scrape your account for all group/channel links"),
    ])
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
    application.add_handler(CommandHandler("vouchanon", vouchanon))
    application.add_handler(CommandHandler("unvouch", unvouch))
    application.add_handler(CommandHandler("negvouch", negvouch))
    application.add_handler(CommandHandler("vouches", vouches_cmd))
    application.add_handler(CommandHandler("history", history))
    application.add_handler(CommandHandler("profile", profile))
    application.add_handler(CommandHandler("top", top))
    application.add_handler(CommandHandler("groupinfo", groupinfo))
    application.add_handler(CommandHandler("scrape", scrape))
    application.add_handler(CallbackQueryHandler(on_reaction_button))
    application.run_polling()

