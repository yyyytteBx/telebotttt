import os
import random
import sqlite3
import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
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


def _normalize_user_key(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("id:"):
        return raw
    if raw.startswith("@"):
        raw = raw[1:]
    if raw.isdigit():
        return f"id:{raw}"
    return f"@{raw}"


def _normalize_target_arg(value: str) -> str:
    raw = value.strip()
    if not raw.startswith("@"):
        raise ValueError("Target must be a username like @example")
    normalized = _normalize_user_key(raw)
    if normalized.startswith("id:") or normalized == "@":
        raise ValueError("Target must be a valid @username")
    return normalized


def _display_user_key(user_key: str) -> str:
    return user_key if user_key else "unknown"


def _get_actor_user_key(update: Update) -> str:
    user = update.effective_user
    if user is None:
        raise RuntimeError("A Telegram user is required to create a vouch action.")
    if user.username:
        return _normalize_user_key(user.username)
    return f"id:{user.id}"


def _table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    )
    return cursor.fetchone() is not None


def _table_columns(cursor: sqlite3.Cursor, table_name: str) -> set[str]:
    cursor.execute(f"PRAGMA table_info({table_name})")
    return {str(row[1]) for row in cursor.fetchall()}


def _ensure_column(
    cursor: sqlite3.Cursor,
    table_name: str,
    column_name: str,
    definition: str,
) -> None:
    if not _table_exists(cursor, table_name):
        return
    cols = _table_columns(cursor, table_name)
    if column_name not in cols:
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def _create_schema(cursor: sqlite3.Cursor) -> None:
    cursor.executescript(
        """
        CREATE TABLE IF NOT EXISTS vouches (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_key      TEXT NOT NULL,
            from_user_key TEXT NOT NULL,
            text          TEXT NOT NULL,
            created_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS limits (
            user_key TEXT NOT NULL,
            day      TEXT NOT NULL,
            count    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_key, day)
        );

        CREATE TABLE IF NOT EXISTS blacklist (
            user_key    TEXT PRIMARY KEY,
            reason      TEXT NOT NULL,
            added_by    TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS anon_vouch_pending (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            target_user_key    TEXT NOT NULL,
            real_from_user_key TEXT NOT NULL,
            from_user_id       INTEGER NOT NULL,
            text               TEXT NOT NULL,
            created_at         TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
            decision_reason    TEXT,
            decided_by         TEXT,
            decided_at         TEXT
        );

        CREATE TABLE IF NOT EXISTS neg_vouches (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_key      TEXT NOT NULL,
            from_user_key TEXT NOT NULL,
            reason        TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            source_chat   TEXT
        );

        CREATE TABLE IF NOT EXISTS staff_logs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_user_key  TEXT NOT NULL,
            action          TEXT NOT NULL,
            target_user_key TEXT,
            reason          TEXT,
            details         TEXT,
            created_at      TEXT NOT NULL,
            source_chat     TEXT
        );
        """
    )


def _create_indexes(cursor: sqlite3.Cursor) -> None:
    cursor.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_vouches_user_key ON vouches(user_key);
        CREATE INDEX IF NOT EXISTS idx_vouches_from_user_key ON vouches(from_user_key);
        CREATE INDEX IF NOT EXISTS idx_vouches_created_at ON vouches(created_at);
        CREATE INDEX IF NOT EXISTS idx_limits_day ON limits(day);
        CREATE INDEX IF NOT EXISTS idx_anon_pending_status_created ON anon_vouch_pending(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_neg_vouches_user_key ON neg_vouches(user_key);
        CREATE INDEX IF NOT EXISTS idx_neg_vouches_from_user_key ON neg_vouches(from_user_key);
        CREATE INDEX IF NOT EXISTS idx_neg_vouches_created_at ON neg_vouches(created_at);
        CREATE INDEX IF NOT EXISTS idx_staff_logs_created_at ON staff_logs(created_at);
        CREATE INDEX IF NOT EXISTS idx_staff_logs_staff_action ON staff_logs(staff_user_key, action);
        """
    )


def _migrate_schema(db: sqlite3.Connection, cursor: sqlite3.Cursor) -> None:
    # vouches: user/from_user/date -> user_key/from_user_key/created_at
    if _table_exists(cursor, "vouches"):
        cols = _table_columns(cursor, "vouches")
        if "user" in cols and "user_key" not in cols:
            cursor.execute("ALTER TABLE vouches RENAME TO vouches_legacy")
            _create_schema(cursor)
            cursor.execute("SELECT user, from_user, text, date FROM vouches_legacy")
            rows = cursor.fetchall()
            migrated: list[tuple[str, str, str, str]] = []
            for user, from_user, text, created_at in rows:
                target_key = _normalize_user_key(str(user) if user is not None else "")
                actor_key = _normalize_user_key(str(from_user) if from_user is not None else "")
                if not target_key or not actor_key:
                    continue
                migrated.append(
                    (
                        target_key,
                        actor_key,
                        str(text or "").strip(),
                        str(created_at or datetime.now().isoformat()),
                    )
                )
            if migrated:
                cursor.executemany(
                    "INSERT INTO vouches (user_key, from_user_key, text, created_at) VALUES (?, ?, ?, ?)",
                    migrated,
                )
            cursor.execute("DROP TABLE vouches_legacy")

    # limits: user/date/count -> user_key/day/count
    if _table_exists(cursor, "limits"):
        cols = _table_columns(cursor, "limits")
        if "user" in cols and "user_key" not in cols:
            cursor.execute("ALTER TABLE limits RENAME TO limits_legacy")
            _create_schema(cursor)
            cursor.execute("SELECT user, date, count FROM limits_legacy")
            rows = cursor.fetchall()
            merged_counts: dict[tuple[str, str], int] = {}
            for user, day, count in rows:
                user_key = _normalize_user_key(str(user) if user is not None else "")
                day_str = str(day or "").strip()
                if not user_key or not day_str:
                    continue
                key = (user_key, day_str)
                merged_counts[key] = merged_counts.get(key, 0) + int(count or 0)
            if merged_counts:
                cursor.executemany(
                    "INSERT INTO limits (user_key, day, count) VALUES (?, ?, ?)",
                    [(k[0], k[1], v) for k, v in merged_counts.items()],
                )
            cursor.execute("DROP TABLE limits_legacy")

    # blacklist: user/date -> user_key/created_at
    if _table_exists(cursor, "blacklist"):
        cols = _table_columns(cursor, "blacklist")
        if "user" in cols and "user_key" not in cols:
            cursor.execute("ALTER TABLE blacklist RENAME TO blacklist_legacy")
            _create_schema(cursor)
            cursor.execute("SELECT user, reason, added_by, date FROM blacklist_legacy")
            rows = cursor.fetchall()
            migrated: list[tuple[str, str, str, str]] = []
            for user, reason, added_by, created_at in rows:
                user_key = _normalize_user_key(str(user) if user is not None else "")
                admin_key = _normalize_user_key(str(added_by) if added_by is not None else "")
                if not user_key:
                    continue
                migrated.append(
                    (
                        user_key,
                        str(reason or "").strip() or "No reason provided",
                        admin_key or "unknown",
                        str(created_at or datetime.now().isoformat()),
                    )
                )
            if migrated:
                cursor.executemany(
                    "INSERT OR REPLACE INTO blacklist (user_key, reason, added_by, created_at) VALUES (?, ?, ?, ?)",
                    migrated,
                )
            cursor.execute("DROP TABLE blacklist_legacy")

    # anon_vouch_pending: target/real_from/from_user_id/date -> target_user_key/real_from_user_key/from_user_id/created_at
    if _table_exists(cursor, "anon_vouch_pending"):
        cols = _table_columns(cursor, "anon_vouch_pending")
        if "target" in cols and "target_user_key" not in cols:
            cursor.execute("ALTER TABLE anon_vouch_pending RENAME TO anon_vouch_pending_legacy")
            _create_schema(cursor)
            cursor.execute(
                "SELECT target, real_from, from_user_id, text, date, status FROM anon_vouch_pending_legacy"
            )
            rows = cursor.fetchall()
            migrated: list[tuple[str, str, int, str, str, str]] = []
            for target, real_from, from_user_id, text, created_at, status in rows:
                target_key = _normalize_user_key(str(target) if target is not None else "")
                actor_key = _normalize_user_key(str(real_from) if real_from is not None else "")
                if not target_key or not actor_key:
                    continue
                normalized_status = str(status or "pending").strip().lower()
                if normalized_status not in {"pending", "approved", "rejected"}:
                    normalized_status = "pending"
                migrated.append(
                    (
                        target_key,
                        actor_key,
                        int(from_user_id or 0),
                        str(text or "").strip(),
                        str(created_at or datetime.now().isoformat()),
                        normalized_status,
                    )
                )
            if migrated:
                cursor.executemany(
                    "INSERT INTO anon_vouch_pending (target_user_key, real_from_user_key, from_user_id, text, created_at, status) VALUES (?, ?, ?, ?, ?, ?)",
                    migrated,
                )
            cursor.execute("DROP TABLE anon_vouch_pending_legacy")

    # Make sure newly added anon decision columns exist for existing DBs
    _ensure_column(cursor, "anon_vouch_pending", "decision_reason", "TEXT")
    _ensure_column(cursor, "anon_vouch_pending", "decided_by", "TEXT")
    _ensure_column(cursor, "anon_vouch_pending", "decided_at", "TEXT")

    _create_schema(cursor)
    _create_indexes(cursor)
    db.commit()

# ---------------- DB SETUP ----------------
_db = sqlite3.connect("vouch.db", check_same_thread=False)
_db.row_factory = sqlite3.Row
_cur = _db.cursor()
_migrate_schema(_db, _cur)


def _parse_allowed_chat_ids() -> set[int] | None:
    raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if not raw:
        return None

    chat_ids: set[int] = set()
    for token in raw.split(","):
        value = token.strip()
        if not value:
            continue
        try:
            chat_ids.add(int(value))
        except ValueError as error:
            raise RuntimeError(
                "TELEGRAM_ALLOWED_CHAT_IDS must contain comma-separated chat IDs."
            ) from error
    return chat_ids


ALLOWED_CHAT_IDS = _parse_allowed_chat_ids()


# ---------------- DB HELPERS ----------------
def _get_rank(count: int) -> str:
    if count < 5:
        return "Newbie 🐣"
    elif count < ELITE_THRESHOLD:
        return "Trusted ✅"
    return "Elite 🏆"


def _can_vouch(user: str) -> bool:
    today = datetime.now().date().isoformat()
    _cur.execute("SELECT count FROM limits WHERE user_key=? AND day=?", (user, today))
    row = _cur.fetchone()
    if not row:
        _cur.execute(
            "INSERT INTO limits (user_key, day, count) VALUES (?, ?, ?)",
            (user, today, 1),
        )
        _db.commit()
        return True
    if row[0] >= 3:
        return False
    _cur.execute(
        "UPDATE limits SET count = count + 1 WHERE user_key=? AND day=?",
        (user, today),
    )
    _db.commit()
    return True


def _on_cooldown(from_user: str, target: str) -> bool:
    cutoff = (datetime.now() - timedelta(hours=VOUCH_COOLDOWN_HOURS)).isoformat()
    _cur.execute(
        "SELECT 1 FROM vouches WHERE user_key=? AND from_user_key=? AND created_at > ?",
        (target, from_user, cutoff),
    )
    return _cur.fetchone() is not None


def _on_negative_cooldown(from_user: str, target: str) -> bool:
    cutoff = (datetime.now() - timedelta(hours=VOUCH_COOLDOWN_HOURS)).isoformat()
    _cur.execute(
        "SELECT 1 FROM neg_vouches WHERE user_key=? AND from_user_key=? AND created_at > ?",
        (target, from_user, cutoff),
    )
    return _cur.fetchone() is not None


def _is_blacklisted(user: str) -> tuple[bool, str]:
    _cur.execute("SELECT reason FROM blacklist WHERE user_key=?", (user,))
    row = _cur.fetchone()
    return (True, row[0]) if row else (False, "")


def _get_positive_count(user_key: str) -> int:
    _cur.execute("SELECT COUNT(*) FROM vouches WHERE user_key=?", (user_key,))
    return int(_cur.fetchone()[0])


def _get_negative_count(user_key: str) -> int:
    _cur.execute("SELECT COUNT(*) FROM neg_vouches WHERE user_key=?", (user_key,))
    return int(_cur.fetchone()[0])


def _get_rep_score(user_key: str) -> tuple[int, int, int]:
    positive = _get_positive_count(user_key)
    negative = _get_negative_count(user_key)
    return positive, negative, positive - negative


def _build_profile_card(
    user_key: str,
    positive: int,
    negative: int,
    score: int,
    blacklisted: bool,
    blacklist_reason: str,
) -> str:
    badge = "NTN VERIFIED" if score >= 5 and not blacklisted else "NTN WATCH"
    rank = _get_rank(positive)
    score_icon = "🟢" if score >= 0 else "🔴"
    card = [
        "┏━━━━━━━━━━━━━━━━━━━━━━━┓",
        "┃      NTN PROFILE      ┃",
        "┗━━━━━━━━━━━━━━━━━━━━━━━┛",
        f"👤 User: {_display_user_key(user_key)}",
        f"🪪 Badge: {badge}",
        f"📈 +Positive: {positive}",
        f"📉 -Negative: {negative}",
        f"{score_icon} Rep Score: {score}",
        f"🏅 Rank: {rank}",
    ]
    if blacklisted:
        card.append("🚫 Status: BLACKLISTED")
        card.append(f"📝 Reason: {blacklist_reason}")
    else:
        card.append("✅ Status: Clear")
    return "\n".join(card)


def _log_staff_action(
    staff_user_key: str,
    action: str,
    target_user_key: str | None = None,
    reason: str | None = None,
    details: str | None = None,
    source_chat: str | None = None,
) -> None:
    _cur.execute(
        "INSERT INTO staff_logs (staff_user_key, action, target_user_key, reason, details, created_at, source_chat) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            staff_user_key,
            action,
            target_user_key,
            reason,
            details,
            datetime.now().isoformat(),
            source_chat,
        ),
    )
    _db.commit()


async def _handle_anon_decision(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    vouch_id: int,
    decision: str,
    decision_reason: str,
    edit_query_message: bool = True,
) -> str:
    query = update.callback_query
    _cur.execute(
        "SELECT target_user_key, real_from_user_key, from_user_id, text FROM anon_vouch_pending"
        " WHERE id=? AND status='pending'",
        (vouch_id,),
    )
    row = _cur.fetchone()
    if not row:
        raise ValueError("Vouch not found or already processed.")

    target, real_from, from_user_id, text = row
    staff_user_key = _get_actor_user_key(update)
    source_chat = str(update.effective_chat.id) if update.effective_chat else None

    actor_blacklisted, actor_reason = _is_blacklisted(real_from)
    target_blacklisted, target_reason = _is_blacklisted(target)
    if decision == "approved" and (actor_blacklisted or target_blacklisted):
        decision = "rejected"
        if actor_blacklisted:
            decision_reason = f"Auto-rejected: submitter blacklisted ({actor_reason})"
        else:
            decision_reason = f"Auto-rejected: target blacklisted ({target_reason})"

    now = datetime.now().isoformat()
    _cur.execute(
        "UPDATE anon_vouch_pending SET status=?, decision_reason=?, decided_by=?, decided_at=? WHERE id=?",
        (decision, decision_reason, staff_user_key, now, vouch_id),
    )

    if decision == "approved":
        _cur.execute(
            "INSERT INTO vouches (user_key, from_user_key, text, created_at) VALUES (?, ?, ?, ?)",
            (target, "anonymous", text, now),
        )

    _db.commit()

    if decision == "approved":
        await context.bot.send_message(
            chat_id=_get_broadcast_chat_id(),
            text=(
                f"👀 Someone vouched for {_display_user_key(target)}\n"
                f"💬 {text}\n"
                f"📝 Staff note: {decision_reason}"
            ),
        )

    if from_user_id:
        try:
            status_text = "approved" if decision == "approved" else "rejected"
            await context.bot.send_message(
                chat_id=from_user_id,
                text=(
                    f"Your anonymous vouch for {_display_user_key(target)} was {status_text}.\n"
                    f"Reason: {decision_reason}"
                ),
            )
        except Exception:
            pass

    _log_staff_action(
        staff_user_key=staff_user_key,
        action=f"anon_{decision}",
        target_user_key=target,
        reason=decision_reason,
        details=f"anon_id={vouch_id}",
        source_chat=source_chat,
    )

    decision_label = "Approved" if decision == "approved" else "Rejected"
    result_text = (
        f"✅ {decision_label}\n\n"
        f"Target: {_display_user_key(target)}\n"
        f"Message: {text}\n"
        f"Reason: {decision_reason}"
    )
    if decision == "rejected":
        result_text = result_text.replace("✅", "❌", 1)

    if query is not None and edit_query_message:
        await query.edit_message_text(result_text)

    return result_text


def _rows_to_csv_bytes(rows: list[sqlite3.Row], headers: list[str]) -> bytes:
    sio = io.StringIO()
    writer = csv.writer(sio)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([row[h] for h in headers])
    return sio.getvalue().encode("utf-8")


def _rows_to_json_bytes(rows: list[sqlite3.Row], headers: list[str]) -> bytes:
    payload = [{h: row[h] for h in headers} for row in rows]
    return json.dumps(payload, indent=2).encode("utf-8")


async def _enforce_vouch_blacklist(
    message: Any,
    from_user_key: str,
    target_user_key: str,
) -> bool:
    from_blacklisted, from_reason = _is_blacklisted(from_user_key)
    if from_blacklisted:
        await message.reply_text(
            f"❌ You are blacklisted and cannot vouch right now. Reason: {from_reason}"
        )
        return False

    target_blacklisted, target_reason = _is_blacklisted(target_user_key)
    if target_blacklisted:
        await message.reply_text(
            f"❌ {_display_user_key(target_user_key)} is blacklisted. Reason: {target_reason}"
        )
        return False

    return True


async def _ensure_chat_allowed(update: Update) -> bool:
    chat = update.effective_chat
    if chat is None:
        return False

    if ALLOWED_CHAT_IDS is None or chat.id in ALLOWED_CHAT_IDS:
        return True

    message = update.effective_message
    if message is not None:
        await message.reply_text("❌ This chat is not allowed to use this bot.")
        return False

    query = update.callback_query
    if query is not None:
        await query.answer("❌ This chat is not allowed.", show_alert=True)
        return False

    return False


def _get_admin_id() -> int | None:
    raw = os.getenv("TELEGRAM_ADMIN_USER_ID")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# ---------------- BROADCAST HELPERS ----------------
@dataclass(frozen=True)
class VouchRequest:
    action_label: str
    target_user_key: str
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
    target = _normalize_target_arg(context.args[0])
    reason = " ".join(context.args[1:]).strip()
    if not reason:
        raise ValueError(f"Usage: /{command_name} @username reason")
    return VouchRequest(action_label=action_label, target_user_key=target, reason=reason)


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
    if not await _ensure_chat_allowed(update):
        return
    if admin_only and not await _is_admin(update, context):
        await message.reply_text("❌ Only group admins can use this command.")
        return
    try:
        request = _parse_vouch_request(command_name, action_label, context)
    except ValueError as error:
        await message.reply_text(str(error))
        return
    actor_user_key = _get_actor_user_key(update)
    if not await _enforce_vouch_blacklist(message, actor_user_key, request.target_user_key):
        return
    actor = _build_actor_name(update)
    source_chat = _build_source_chat(update)
    broadcast_message = (
        f"{request.action_label}\n"
        f"Target: {_display_user_key(request.target_user_key)}\n"
        f"From: {actor}\n"
        f"Reason: {request.reason}\n"
        f"Source chat: {source_chat}"
    )
    await context.bot.send_message(chat_id=_get_broadcast_chat_id(), text=broadcast_message)
    if admin_only:
        _log_staff_action(
            staff_user_key=actor_user_key,
            action=command_name,
            target_user_key=request.target_user_key,
            reason=request.reason,
            details="broadcast_only",
            source_chat=str(update.effective_chat.id) if update.effective_chat else None,
        )
    await message.reply_text(
        f"{request.action_label} for {_display_user_key(request.target_user_key)} was broadcast successfully."
    )


# ---------------- COMMANDS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    await message.reply_text(
        "👋 Welcome to Vouch Bot\n\n"
        "Track reputation, view profiles, and manage trusted deals.\n\n"
        "Quick commands:\n"
        "/vouch @user message\n"
        "/vouchanon @user message\n"
        "/profile @user\n"
        "/vouches @user\n"
        "/leaderboard\n"
        "/top\n"
        "/recent\n"
        "/stats\n"
        "/groupinfo\n\n"
        "Use /search @user for more details."
    )


async def vouch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    if not context.args:
        await message.reply_text("Usage: /vouch @user message")
        return

    from_user_key = _get_actor_user_key(update)
    from_display = _display_user_key(from_user_key)
    try:
        target = _normalize_target_arg(context.args[0])
    except ValueError:
        await message.reply_text("Usage: /vouch @user message")
        return

    # Prevent self-vouching
    if from_user_key == target:
        await message.reply_text("❌ You cannot vouch for yourself!")
        return

    if not await _enforce_vouch_blacklist(message, from_user_key, target):
        return

    if not _can_vouch(from_user_key):
        await message.reply_text("❌ Daily vouch limit reached (3)")
        return

    if _on_cooldown(from_user_key, target):
        await message.reply_text(
            f"⏱️ You already vouched for {_display_user_key(target)} in the last {VOUCH_COOLDOWN_HOURS}h"
        )
        return

    text = " ".join(context.args[1:]).strip() or random.choice(RANDOM_VOUCH_LINES)

    _cur.execute(
        "INSERT INTO vouches (user_key, from_user_key, text, created_at) VALUES (?, ?, ?, ?)",
        (target, from_user_key, text, datetime.now().isoformat()),
    )
    _db.commit()

    # Check if target just crossed Elite threshold — announce it
    _cur.execute("SELECT COUNT(*) FROM vouches WHERE user_key=?", (target,))
    new_count = _cur.fetchone()[0]
    if new_count == ELITE_THRESHOLD:
        await context.bot.send_message(
            chat_id=_get_broadcast_chat_id(),
            text=f"🏆 {_display_user_key(target)} just reached Elite rank with {new_count} vouches!",
        )

    actor = _build_actor_name(update)
    source_chat = _build_source_chat(update)
    await context.bot.send_message(
        chat_id=_get_broadcast_chat_id(),
        text=(
            f"Vouch\nTarget: {_display_user_key(target)}\nFrom: {actor}\nReason: {text}\n"
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
        f"🧾 VOUCH\nFrom: {from_display}\nTo: {_display_user_key(target)}\n💬 {text}",
        reply_markup=keyboard,
    )


async def vouchanon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not context.args:
        if message:
            await message.reply_text("Usage: /vouchanon @user message")
        return
    if not await _ensure_chat_allowed(update):
        return

    from_user_key = _get_actor_user_key(update)
    from_user_id = user.id
    try:
        target = _normalize_target_arg(context.args[0])
    except ValueError:
        await message.reply_text("Usage: /vouchanon @user message")
        return

    # Prevent self-vouching
    if from_user_key == target:
        await message.reply_text("❌ You cannot vouch for yourself!")
        return

    if not await _enforce_vouch_blacklist(message, from_user_key, target):
        return

    if _on_cooldown(from_user_key, target):
        await message.reply_text(
            f"⏱️ You already vouched for {_display_user_key(target)} in the last {VOUCH_COOLDOWN_HOURS}h"
        )
        return

    text = " ".join(context.args[1:]).strip() or random.choice(RANDOM_VOUCH_LINES)

    # Store as pending (not immediately broadcast)
    _cur.execute(
        "INSERT INTO anon_vouch_pending (target_user_key, real_from_user_key, from_user_id, text, created_at, status)"
        " VALUES (?, ?, ?, ?, ?, 'pending')",
        (target, from_user_key, from_user_id, text, datetime.now().isoformat()),
    )
    _db.commit()
    vouch_id = _cur.lastrowid

    # Send DM to admin with approve/reject buttons
    admin_id = _get_admin_id()
    if admin_id:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"anon_approve:{vouch_id}:approved_by_admin_dm"),
            InlineKeyboardButton("❌ Reject", callback_data=f"anon_reject:{vouch_id}:rejected_by_admin_dm"),
        ]])
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"🔔 Pending Anonymous Vouch\n\n"
                    f"Target: {_display_user_key(target)}\n"
                    f"From: {_display_user_key(from_user_key)}\n"
                    f"Message: {text}\n"
                    f"Vouch ID: #{vouch_id}"
                ),
                reply_markup=keyboard,
            )
        except Exception:
            pass

    await message.reply_text(
        "✅ Your anonymous vouch has been submitted for admin review.\n"
        "It will be broadcast once approved."
    )


async def pending_vouches(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not await _ensure_chat_allowed(update):
        return

    admin_id = _get_admin_id()
    if admin_id is not None and user.id != admin_id:
        await message.reply_text("❌ This command is for admins only.")
        return

    _cur.execute(
        "SELECT id, target_user_key, real_from_user_key, text, created_at FROM anon_vouch_pending"
        " WHERE status='pending' ORDER BY created_at ASC"
    )
    rows = _cur.fetchall()
    if not rows:
        await message.reply_text("✅ No pending anonymous vouches.")
        return

    for row in rows:
        vouch_id, target, real_from, text, created_at = row
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"anon_approve:{vouch_id}:approved_by_staff_button"),
            InlineKeyboardButton("❌ Reject", callback_data=f"anon_reject:{vouch_id}:rejected_by_staff_button"),
        ]])
        await message.reply_text(
            f"🔔 Pending Vouch #{vouch_id}\n\n"
            f"Target: {_display_user_key(target)}\n"
            f"From: {_display_user_key(real_from)}\n"
            f"Message: {text}\n"
            f"Date: {created_at[:10]}",
            reply_markup=keyboard,
        )


async def approveanon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    admin_id = _get_admin_id()
    if admin_id is not None and user.id != admin_id:
        await message.reply_text("❌ This command is for admins only.")
        return

    if not context.args or not context.args[0].isdigit() or len(context.args) < 2:
        await message.reply_text("Usage: /approveanon <vouch_id> reason")
        return

    vouch_id = int(context.args[0])
    reason = " ".join(context.args[1:]).strip()
    try:
        result_text = await _handle_anon_decision(
            update,
            context,
            vouch_id=vouch_id,
            decision="approved",
            decision_reason=reason,
            edit_query_message=False,
        )
    except ValueError as error:
        await message.reply_text(str(error))
        return
    await message.reply_text(result_text)


async def rejectanon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    admin_id = _get_admin_id()
    if admin_id is not None and user.id != admin_id:
        await message.reply_text("❌ This command is for admins only.")
        return

    if not context.args or not context.args[0].isdigit() or len(context.args) < 2:
        await message.reply_text("Usage: /rejectanon <vouch_id> reason")
        return

    vouch_id = int(context.args[0])
    reason = " ".join(context.args[1:]).strip()
    try:
        result_text = await _handle_anon_decision(
            update,
            context,
            vouch_id=vouch_id,
            decision="rejected",
            decision_reason=reason,
            edit_query_message=False,
        )
    except ValueError as error:
        await message.reply_text(str(error))
        return
    await message.reply_text(result_text)


async def stafflogs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    admin_id = _get_admin_id()
    if admin_id is not None and user.id != admin_id:
        await message.reply_text("❌ This command is for admins only.")
        return

    limit = 15
    if context.args and context.args[0].isdigit():
        limit = max(1, min(50, int(context.args[0])))

    _cur.execute(
        "SELECT staff_user_key, action, target_user_key, reason, created_at FROM staff_logs ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    rows = _cur.fetchall()
    if not rows:
        await message.reply_text("No staff logs yet.")
        return

    lines = ["🧾 Staff/Admin Logs", ""]
    for row in rows:
        lines.append(
            f"[{row['created_at'][:16]}] {_display_user_key(row['staff_user_key'])} -> {row['action']} "
            f"target={_display_user_key(row['target_user_key'] or '')}"
        )
        if row["reason"]:
            lines.append(f"reason: {row['reason']}")
    await message.reply_text("\n".join(lines))


async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    admin_id = _get_admin_id()
    if admin_id is not None and user.id != admin_id:
        await message.reply_text("❌ This command is for admins only.")
        return

    if len(context.args) < 2:
        await message.reply_text("Usage: /export <vouches|negvouches|blacklist|anon|stafflogs> <csv|json>")
        return

    dataset = context.args[0].strip().lower()
    out_format = context.args[1].strip().lower()
    if out_format not in {"csv", "json"}:
        await message.reply_text("Format must be csv or json.")
        return

    query_map: dict[str, tuple[str, list[str]]] = {
        "vouches": (
            "SELECT id, user_key, from_user_key, text, created_at FROM vouches ORDER BY created_at DESC",
            ["id", "user_key", "from_user_key", "text", "created_at"],
        ),
        "negvouches": (
            "SELECT id, user_key, from_user_key, reason, created_at, source_chat FROM neg_vouches ORDER BY created_at DESC",
            ["id", "user_key", "from_user_key", "reason", "created_at", "source_chat"],
        ),
        "blacklist": (
            "SELECT user_key, reason, added_by, created_at FROM blacklist ORDER BY created_at DESC",
            ["user_key", "reason", "added_by", "created_at"],
        ),
        "anon": (
            "SELECT id, target_user_key, real_from_user_key, text, status, decision_reason, decided_by, decided_at, created_at FROM anon_vouch_pending ORDER BY created_at DESC",
            [
                "id",
                "target_user_key",
                "real_from_user_key",
                "text",
                "status",
                "decision_reason",
                "decided_by",
                "decided_at",
                "created_at",
            ],
        ),
        "stafflogs": (
            "SELECT id, staff_user_key, action, target_user_key, reason, details, created_at, source_chat FROM staff_logs ORDER BY created_at DESC",
            ["id", "staff_user_key", "action", "target_user_key", "reason", "details", "created_at", "source_chat"],
        ),
    }

    if dataset not in query_map:
        await message.reply_text("Unknown dataset. Use vouches, negvouches, blacklist, anon, or stafflogs.")
        return

    query, headers = query_map[dataset]
    _cur.execute(query)
    rows = _cur.fetchall()
    if not rows:
        await message.reply_text("No rows to export for that dataset.")
        return

    if out_format == "csv":
        content = _rows_to_csv_bytes(rows, headers)
    else:
        content = _rows_to_json_bytes(rows, headers)

    filename = f"{dataset}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{out_format}"
    bio = io.BytesIO(content)
    bio.name = filename
    bio.seek(0)
    _log_staff_action(
        staff_user_key=_get_actor_user_key(update),
        action="export",
        target_user_key=None,
        reason=f"dataset={dataset},format={out_format}",
        details=f"rows={len(rows)}",
        source_chat=str(update.effective_chat.id) if update.effective_chat else None,
    )
    await message.reply_document(document=InputFile(bio, filename=filename))


async def removevouch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    if not context.args:
        await message.reply_text("Usage: /removevouch @user")
        return
    from_user = _get_actor_user_key(update)
    try:
        target = _normalize_target_arg(context.args[0])
    except ValueError:
        await message.reply_text("Usage: /removevouch @user")
        return
    _cur.execute(
        "DELETE FROM vouches WHERE id = ("
        "  SELECT id FROM vouches WHERE user_key=? AND from_user_key=? ORDER BY created_at DESC LIMIT 1"
        ")",
        (target, from_user),
    )
    _db.commit()
    if _cur.rowcount:
        await message.reply_text(
            f"🗑️ Your most recent vouch for {_display_user_key(target)} was removed."
        )
    else:
        await message.reply_text(
            f"No vouch from you to {_display_user_key(target)} found."
        )


async def unvouch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _broadcast_vouch(update, context, "unvouch", "Unvouch")


async def negvouch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    if not await _is_admin(update, context):
        await message.reply_text("❌ Only group admins can use this command.")
        return

    try:
        request = _parse_vouch_request("negvouch", "Negative vouch", context)
    except ValueError as error:
        await message.reply_text(str(error))
        return

    actor_user_key = _get_actor_user_key(update)
    if actor_user_key == request.target_user_key:
        await message.reply_text("❌ You cannot neg-vouch yourself.")
        return

    if not await _enforce_vouch_blacklist(message, actor_user_key, request.target_user_key):
        return

    if _on_negative_cooldown(actor_user_key, request.target_user_key):
        await message.reply_text(
            f"⏱️ You already neg-vouched {_display_user_key(request.target_user_key)} in the last {VOUCH_COOLDOWN_HOURS}h"
        )
        return

    source_chat = _build_source_chat(update)
    _cur.execute(
        "INSERT INTO neg_vouches (user_key, from_user_key, reason, created_at, source_chat) VALUES (?, ?, ?, ?, ?)",
        (
            request.target_user_key,
            actor_user_key,
            request.reason,
            datetime.now().isoformat(),
            source_chat,
        ),
    )
    _db.commit()

    _log_staff_action(
        staff_user_key=actor_user_key,
        action="negvouch",
        target_user_key=request.target_user_key,
        reason=request.reason,
        details="stored_and_broadcast",
        source_chat=source_chat,
    )

    actor = _build_actor_name(update)
    await context.bot.send_message(
        chat_id=_get_broadcast_chat_id(),
        text=(
            "Negative vouch\n"
            f"Target: {_display_user_key(request.target_user_key)}\n"
            f"From: {actor}\n"
            f"Reason: {request.reason}\n"
            f"Source chat: {source_chat}"
        ),
    )
    await message.reply_text(
        f"Negative vouch for {_display_user_key(request.target_user_key)} was stored and broadcast successfully."
    )


async def blacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    if not await _is_admin(update, context):
        await message.reply_text("❌ Only group admins can use this command.")
        return
    if not context.args or len(context.args) < 2:
        await message.reply_text("Usage: /blacklist @user reason")
        return
    try:
        target = _normalize_target_arg(context.args[0])
    except ValueError:
        await message.reply_text("Usage: /blacklist @user reason")
        return
    reason = " ".join(context.args[1:])
    added_by = _get_actor_user_key(update)
    _cur.execute(
        "INSERT OR REPLACE INTO blacklist (user_key, reason, added_by, created_at) VALUES (?, ?, ?, ?)",
        (target, reason, added_by, datetime.now().isoformat()),
    )
    _db.commit()
    _log_staff_action(
        staff_user_key=added_by,
        action="blacklist",
        target_user_key=target,
        reason=reason,
        details="manual_blacklist",
        source_chat=str(update.effective_chat.id) if update.effective_chat else None,
    )
    await message.reply_text(
        f"🚫 {_display_user_key(target)} has been blacklisted.\nReason: {reason}"
    )
    await context.bot.send_message(
        chat_id=_get_broadcast_chat_id(),
        text=(
            f"🚫 BLACKLIST\nUser: {_display_user_key(target)}\nReason: {reason}\n"
            f"Added by: {_display_user_key(added_by)}"
        ),
    )


async def unblacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    if not await _is_admin(update, context):
        await message.reply_text("❌ Only group admins can use this command.")
        return
    if not context.args:
        await message.reply_text("Usage: /unblacklist @user")
        return
    try:
        target = _normalize_target_arg(context.args[0])
    except ValueError:
        await message.reply_text("Usage: /unblacklist @user")
        return
    _cur.execute("DELETE FROM blacklist WHERE user_key=?", (target,))
    _db.commit()
    _log_staff_action(
        staff_user_key=_get_actor_user_key(update),
        action="unblacklist",
        target_user_key=target,
        reason="removed from blacklist",
        details="manual_unblacklist",
        source_chat=str(update.effective_chat.id) if update.effective_chat else None,
    )
    if _cur.rowcount:
        await message.reply_text(
            f"✅ {_display_user_key(target)} has been removed from the blacklist."
        )
    else:
        await message.reply_text(f"{_display_user_key(target)} is not on the blacklist.")


async def vouches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    if not context.args:
        await message.reply_text("Usage: /vouches @user")
        return
    try:
        target = _normalize_target_arg(context.args[0])
    except ValueError:
        await message.reply_text("Usage: /vouches @user")
        return
    _cur.execute("SELECT from_user_key, text FROM vouches WHERE user_key=?", (target,))
    rows = _cur.fetchall()
    if not rows:
        await message.reply_text("No vouches yet.")
        return
    msg = f"📜 Vouches for {_display_user_key(target)}:\n\n"
    for r in rows[-10:]:
        msg += f"{_display_user_key(r[0])}: {r[1]}\n"
    await message.reply_text(msg)


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    if not context.args:
        await message.reply_text("Usage: /profile @user")
        return
    try:
        target = _normalize_target_arg(context.args[0])
    except ValueError:
        await message.reply_text("Usage: /profile @user")
        return
    positive, negative, score = _get_rep_score(target)
    blacklisted, bl_reason = _is_blacklisted(target)
    await message.reply_text(
        _build_profile_card(
            user_key=target,
            positive=positive,
            negative=negative,
            score=score,
            blacklisted=blacklisted,
            blacklist_reason=bl_reason,
        )
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or update.effective_user is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    me = _get_actor_user_key(update)
    _cur.execute("SELECT COUNT(*) FROM vouches WHERE from_user_key=?", (me,))
    given = _cur.fetchone()[0]
    _cur.execute("SELECT COUNT(*) FROM vouches WHERE user_key=?", (me,))
    received = _cur.fetchone()[0]
    _cur.execute("SELECT COUNT(*) FROM neg_vouches WHERE user_key=?", (me,))
    negatives = _cur.fetchone()[0]
    score = received - negatives
    await message.reply_text(
        f"📊 Your Stats ({_display_user_key(me)})\n"
        f"✅ Vouches given: {given}\n"
        f"⭐ Positive received: {received}\n"
        f"❌ Negative received: {negatives}\n"
        f"⚖️ Rep score: {score}\n"
        f"🏆 Rank: {_get_rank(received)}"
    )


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    if not context.args:
        await message.reply_text("Usage: /search @user")
        return
    try:
        target = _normalize_target_arg(context.args[0])
    except ValueError:
        await message.reply_text("Usage: /search @user")
        return
    _cur.execute("SELECT from_user_key, text FROM vouches WHERE user_key=?", (target,))
    received = _cur.fetchall()
    _cur.execute("SELECT user_key, text FROM vouches WHERE from_user_key=?", (target,))
    given = _cur.fetchall()
    parts = [f"🔍 Search results for {_display_user_key(target)}"]
    if received:
        parts.append(f"\n📥 Received ({len(received)}):")
        for r in received[-5:]:
            parts.append(f"  {_display_user_key(r[0])}: {r[1]}")
    else:
        parts.append("\n📥 Received: none")
    if given:
        parts.append(f"\n📤 Given ({len(given)}):")
        for r in given[-5:]:
            parts.append(f"  → {_display_user_key(r[0])}: {r[1]}")
    else:
        parts.append("\n📤 Given: none")
    await message.reply_text("\n".join(parts))


async def recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    _cur.execute(
        "SELECT from_user_key, user_key, text, created_at FROM vouches ORDER BY created_at DESC LIMIT 5"
    )
    rows = _cur.fetchall()
    if not rows:
        await message.reply_text("No vouches recorded yet.")
        return
    msg = "📌 Recent Vouches:\n\n"
    for r in rows:
        ts = r[3][:10]
        msg += f"{_display_user_key(r[0])} → {_display_user_key(r[1])}: {r[2]} [{ts}]\n"
    await message.reply_text(msg)


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    _cur.execute(
        "SELECT user_key, COUNT(*) as c FROM vouches GROUP BY user_key ORDER BY c DESC LIMIT 10"
    )
    rows = _cur.fetchall()
    if not rows:
        await message.reply_text("No vouches recorded yet.")
        return
    msg = "🏆 Top Trusted Users:\n\n"
    for i, r in enumerate(rows, 1):
        msg += f"{i}. {_display_user_key(r[0])} — {r[1]} positive vouches\n"
    await message.reply_text(msg)


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _ensure_chat_allowed(update):
        return

    _cur.execute(
        """
        WITH positives AS (
            SELECT user_key, COUNT(*) AS p FROM vouches GROUP BY user_key
        ),
        negatives AS (
            SELECT user_key, COUNT(*) AS n FROM neg_vouches GROUP BY user_key
        ),
        merged AS (
            SELECT p.user_key AS user_key, p.p AS positive, COALESCE(n.n, 0) AS negative
            FROM positives p
            LEFT JOIN negatives n ON n.user_key = p.user_key
            UNION
            SELECT n.user_key AS user_key, COALESCE(p.p, 0) AS positive, n.n AS negative
            FROM negatives n
            LEFT JOIN positives p ON p.user_key = n.user_key
        )
        SELECT user_key, positive, negative, (positive - negative) AS score
        FROM merged
        ORDER BY score DESC, positive DESC, user_key ASC
        LIMIT 10
        """
    )
    rows = _cur.fetchall()
    if not rows:
        await message.reply_text("No reputation data yet.")
        return

    out = ["🏁 Reputation Leaderboard", ""]
    for i, row in enumerate(rows, 1):
        out.append(
            f"{i}. {_display_user_key(row['user_key'])} | +{row['positive']} / -{row['negative']} | score {row['score']}"
        )
    await message.reply_text("\n".join(out))


async def groupinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    if not await _ensure_chat_allowed(update):
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


# ---------------- CALLBACK HANDLERS ----------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    data = query.data or ""

    if data.startswith("anon_approve:") or data.startswith("anon_reject:"):
        await _handle_anon_vouch_callback(update, context)
        return

    # Reaction buttons on regular vouches
    reactions = {"legit": "👍 Legit!", "fire": "🔥 Fire!", "cap": "❌ Cap!"}
    await query.answer(reactions.get(data, ""))


async def _handle_anon_vouch_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return

    user = update.effective_user
    admin_id = _get_admin_id()
    if admin_id is not None and (user is None or user.id != admin_id):
        await query.answer("❌ Only the admin can approve or reject vouches.")
        return

    data = query.data or ""

    if data.startswith("anon_approve:"):
        parts = data.split(":", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            await query.answer("Invalid callback data.")
            return
        vouch_id = int(parts[1])
        reason = parts[2].replace("_", " ") if len(parts) > 2 else "approved by staff"
        try:
            await _handle_anon_decision(
                update,
                context,
                vouch_id=vouch_id,
                decision="approved",
                decision_reason=reason,
                edit_query_message=True,
            )
        except ValueError as error:
            await query.answer(str(error))
            return
        await query.answer("✅ Vouch approved.")

    elif data.startswith("anon_reject:"):
        parts = data.split(":", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            await query.answer("Invalid callback data.")
            return
        vouch_id = int(parts[1])
        reason = parts[2].replace("_", " ") if len(parts) > 2 else "rejected by staff"
        try:
            await _handle_anon_decision(
                update,
                context,
                vouch_id=vouch_id,
                decision="rejected",
                decision_reason=reason,
                edit_query_message=True,
            )
        except ValueError as error:
            await query.answer(str(error))
            return
        await query.answer("❌ Vouch rejected.")


# ---------------- STARTUP ----------------
async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("start", "Welcome message and command summary"),
        BotCommand("vouch", "Vouch for a user (stored + broadcast)"),
        BotCommand("vouchanon", "Vouch anonymously (requires admin approval)"),
        BotCommand("pending_vouches", "View pending anonymous vouches (admin only)"),
        BotCommand("approveanon", "Approve anon vouch with a reason (admin only)"),
        BotCommand("rejectanon", "Reject anon vouch with a reason (admin only)"),
        BotCommand("removevouch", "Remove your last vouch for a user"),
        BotCommand("unvouch", "Broadcast an unvouch"),
        BotCommand("negvouch", "Store + broadcast a negative vouch (admin only)"),
        BotCommand("vouches", "View vouches for a user"),
        BotCommand("profile", "View NTN-style profile with rep score"),
        BotCommand("stats", "View your own positive/negative rep stats"),
        BotCommand("leaderboard", "Top users by reputation score"),
        BotCommand("search", "Search vouches given and received by a user"),
        BotCommand("recent", "Last 5 vouches across all users"),
        BotCommand("top", "Top 10 users by positive vouches"),
        BotCommand("blacklist", "Blacklist a user (admin only)"),
        BotCommand("unblacklist", "Remove a user from blacklist (admin only)"),
        BotCommand("stafflogs", "View staff/admin action logs (admin only)"),
        BotCommand("export", "Export data as CSV/JSON (admin only)"),
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
    application.add_handler(CommandHandler("vouch", vouch))
    application.add_handler(CommandHandler("vouchanon", vouchanon))
    application.add_handler(CommandHandler("pending_vouches", pending_vouches))
    application.add_handler(CommandHandler("approveanon", approveanon))
    application.add_handler(CommandHandler("rejectanon", rejectanon))
    application.add_handler(CommandHandler("removevouch", removevouch))
    application.add_handler(CommandHandler("unvouch", unvouch))
    application.add_handler(CommandHandler("negvouch", negvouch))
    application.add_handler(CommandHandler("vouches", vouches_cmd))
    application.add_handler(CommandHandler("profile", profile))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(CommandHandler("search", search))
    application.add_handler(CommandHandler("recent", recent))
    application.add_handler(CommandHandler("top", top))
    application.add_handler(CommandHandler("blacklist", blacklist_cmd))
    application.add_handler(CommandHandler("unblacklist", unblacklist_cmd))
    application.add_handler(CommandHandler("stafflogs", stafflogs))
    application.add_handler(CommandHandler("export", export_data))
    application.add_handler(CommandHandler("groupinfo", groupinfo))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.run_polling()


if __name__ == "__main__":
    run_bot()
