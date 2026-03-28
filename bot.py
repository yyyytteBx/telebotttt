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


DEFAULT_BROADCAST_CHAT_ID = -1003305030576
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
REACTION_LABELS = {
    "legit": "👍",
    "fire": "🔥",
    "cap": "❌",
}


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


def _display_vouch_actor(giver_id: str) -> str:
    if (giver_id or "").strip().lower() == "anonymous":
        return "anonymous"
    return _display_user_key(giver_id)


def _format_broadcast_vouch(target_user_key: str, reason: str, is_anonymous: bool = False) -> str:
    lock_marker = " 🔒" if is_anonymous else ""
    return f"+1 VOUCH{lock_marker} {_display_user_key(target_user_key)}\n{reason}"


def _format_pending_confirm_vouch(target: str, giver: str, reason: str) -> str:
    return (
        "VOUCH LOG 🔐\n\n"
        f"User: {_display_user_key(target)}\n"
        f"From: {_display_user_key(giver)}\n\n"
        "Status: +1\n"
        "Confirmation: Pending 👀\n\n"
        f"“{reason}”"
    )


def _format_confirmed_vouch(target: str, giver: str, reason: str) -> str:
    return (
        "VOUCH LOG 🔐\n\n"
        f"User: {_display_user_key(target)}\n"
        f"From: {_display_user_key(giver)}\n\n"
        "Status: +1\n"
        "Confirmation: Confirmed 🤝\n\n"
        f"“{reason}”"
    )


def _format_broadcast_negvouch(target_user_key: str, reason: str) -> str:
    return f"⚠️ Negative vouch {_display_user_key(target_user_key)} ❌\n\n“{reason}”"


def _format_pending_negvouch(target_user_key: str, reason: str) -> str:
    return (
        "⚠️ NEG VOUCH (PENDING)\n\n"
        f"{_display_user_key(target_user_key)}\n\n"
        f"“{reason}”\n\n"
        "Status: Awaiting response 👀\n"
        "Resolve to avoid further impact."
    )


def _format_resolved_negvouch(
    target_user_key: str,
    reason: str,
    resolution_note: str,
    resolved_by: str,
) -> str:
    return (
        "✅ NEG VOUCH (RESOLVED)\n\n"
        f"{_display_user_key(target_user_key)}\n\n"
        f"“{reason}”\n\n"
        f"Status: Resolved by {resolved_by}\n"
        f"Resolution: {resolution_note}"
    )


def _format_action_log(
    staff_user_key: str,
    action: str,
    target_user_key: str | None = None,
    reason: str | None = None,
    details: str | None = None,
    source_chat: str | None = None,
) -> str:
    lines = [
        "🧾 BOT ACTION",
        f"Action: {action}",
        f"By: {_display_user_key(staff_user_key)}",
    ]
    if target_user_key:
        lines.append(f"Target: {_display_user_key(target_user_key)}")
    if reason:
        lines.append(f"Reason: {reason}")
    if details:
        lines.append(f"Details: {details}")
    if source_chat:
        lines.append(f"Source: {source_chat}")
    return "\n".join(lines)


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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            giver_id TEXT,
            target_username TEXT,
            reason TEXT,
            type TEXT DEFAULT 'positive',
            confirmed BOOLEAN DEFAULT 0,
            resolved BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            source_chat   TEXT,
            status        TEXT NOT NULL DEFAULT 'pending',
            resolution_note TEXT,
            resolved_by   TEXT,
            resolved_at   TEXT,
            broadcast_chat_id INTEGER,
            broadcast_message_id INTEGER
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

        CREATE TABLE IF NOT EXISTS message_reactions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id          INTEGER NOT NULL,
            message_id       INTEGER NOT NULL,
            reactor_user_key TEXT NOT NULL,
            reaction         TEXT NOT NULL CHECK(reaction IN ('legit', 'fire', 'cap')),
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            UNIQUE(chat_id, message_id, reactor_user_key)
        );

        CREATE TABLE IF NOT EXISTS user_stats (
            username TEXT PRIMARY KEY,
            total_vouches INTEGER DEFAULT 0,
            confirmed_vouches INTEGER DEFAULT 0,
            neg_vouches INTEGER DEFAULT 0,
            trust_score INTEGER DEFAULT 0,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )


def _create_indexes(cursor: sqlite3.Cursor) -> None:
    cursor.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_vouches_target_username ON vouches(target_username);
        CREATE INDEX IF NOT EXISTS idx_vouches_giver_id ON vouches(giver_id);
        CREATE INDEX IF NOT EXISTS idx_vouches_created_at ON vouches(created_at);
        CREATE INDEX IF NOT EXISTS idx_limits_day ON limits(day);
        CREATE INDEX IF NOT EXISTS idx_anon_pending_status_created ON anon_vouch_pending(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_neg_vouches_user_key ON neg_vouches(user_key);
        CREATE INDEX IF NOT EXISTS idx_neg_vouches_from_user_key ON neg_vouches(from_user_key);
        CREATE INDEX IF NOT EXISTS idx_neg_vouches_created_at ON neg_vouches(created_at);
        CREATE INDEX IF NOT EXISTS idx_staff_logs_created_at ON staff_logs(created_at);
        CREATE INDEX IF NOT EXISTS idx_staff_logs_staff_action ON staff_logs(staff_user_key, action);
        CREATE INDEX IF NOT EXISTS idx_message_reactions_message ON message_reactions(chat_id, message_id);
        CREATE INDEX IF NOT EXISTS idx_message_reactions_reaction ON message_reactions(reaction);
        """
    )


def _migrate_schema(db: sqlite3.Connection, cursor: sqlite3.Cursor) -> None:
    # vouches: migrate any legacy shape to target_username/giver_id/reason schema
    if _table_exists(cursor, "vouches"):
        cols = _table_columns(cursor, "vouches")
        if "target_username" not in cols or "giver_id" not in cols or "reason" not in cols:
            cursor.execute("ALTER TABLE vouches RENAME TO vouches_legacy")
            _create_schema(cursor)
            legacy_cols = _table_columns(cursor, "vouches_legacy")
            select_parts = [
                "chat_id" if "chat_id" in legacy_cols else "NULL AS chat_id",
                "user_key" if "user_key" in legacy_cols else ("user" if "user" in legacy_cols else "NULL AS user_key"),
                "from_user_key" if "from_user_key" in legacy_cols else ("from_user" if "from_user" in legacy_cols else "NULL AS from_user_key"),
                "text" if "text" in legacy_cols else ("reason" if "reason" in legacy_cols else "'' AS text"),
                "type" if "type" in legacy_cols else "'positive' AS type",
                "confirmed" if "confirmed" in legacy_cols else "0 AS confirmed",
                "resolved" if "resolved" in legacy_cols else "0 AS resolved",
                "created_at" if "created_at" in legacy_cols else ("date" if "date" in legacy_cols else "CURRENT_TIMESTAMP AS created_at"),
                "is_anonymous" if "is_anonymous" in legacy_cols else "0 AS is_anonymous",
            ]
            cursor.execute(f"SELECT {', '.join(select_parts)} FROM vouches_legacy")
            rows = cursor.fetchall()
            migrated: list[tuple[int | None, str, str, str, int, int, str]] = []
            for chat_id, user, from_user, text, vouch_type, confirmed, resolved, created_at, is_anonymous in rows:
                target_key = _normalize_user_key(str(user) if user is not None else "")
                actor_key = "anonymous" if int(is_anonymous or 0) == 1 else _normalize_user_key(str(from_user) if from_user is not None else "")
                if not target_key or not actor_key:
                    continue
                migrated.append(
                    (
                        int(chat_id) if chat_id is not None else None,
                        actor_key,
                        target_key,
                        str(text or "").strip(),
                        int(confirmed or 0),
                        int(resolved or 0),
                        str(created_at or datetime.now().isoformat()),
                    )
                )
            if migrated:
                cursor.executemany(
                    """
                    INSERT INTO vouches (
                        chat_id, giver_id, target_username, reason, type, confirmed, resolved, created_at
                    ) VALUES (?, ?, ?, ?, 'positive', ?, ?, ?)
                    """,
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
    _ensure_column(cursor, "vouches", "chat_id", "INTEGER")
    _ensure_column(cursor, "vouches", "giver_id", "TEXT")
    _ensure_column(cursor, "vouches", "target_username", "TEXT")
    _ensure_column(cursor, "vouches", "reason", "TEXT")
    _ensure_column(cursor, "vouches", "type", "TEXT DEFAULT 'positive'")
    _ensure_column(cursor, "vouches", "confirmed", "BOOLEAN DEFAULT 0")
    _ensure_column(cursor, "vouches", "resolved", "BOOLEAN DEFAULT 0")
    _ensure_column(cursor, "anon_vouch_pending", "decision_reason", "TEXT")
    _ensure_column(cursor, "anon_vouch_pending", "decided_by", "TEXT")
    _ensure_column(cursor, "anon_vouch_pending", "decided_at", "TEXT")
    _ensure_column(cursor, "neg_vouches", "status", "TEXT NOT NULL DEFAULT 'pending'")
    _ensure_column(cursor, "neg_vouches", "resolution_note", "TEXT")
    _ensure_column(cursor, "neg_vouches", "resolved_by", "TEXT")
    _ensure_column(cursor, "neg_vouches", "resolved_at", "TEXT")
    _ensure_column(cursor, "neg_vouches", "broadcast_chat_id", "INTEGER")
    _ensure_column(cursor, "neg_vouches", "broadcast_message_id", "INTEGER")
    if _table_exists(cursor, "user_stats"):
        user_stats_cols = _table_columns(cursor, "user_stats")
        if "user_key" in user_stats_cols and "username" not in user_stats_cols:
            cursor.execute("ALTER TABLE user_stats RENAME TO user_stats_legacy")
            _create_schema(cursor)
            cursor.execute(
                """
                SELECT user_key, total_vouches, confirmed_vouches, neg_vouches, trust_score, last_updated
                FROM user_stats_legacy
                """
            )
            rows = cursor.fetchall()
            if rows:
                cursor.executemany(
                    """
                    INSERT INTO user_stats (
                        username, total_vouches, confirmed_vouches, neg_vouches, trust_score, last_updated
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            cursor.execute("DROP TABLE user_stats_legacy")

    _create_schema(cursor)
    _create_indexes(cursor)
    db.commit()

# ---------------- DB SETUP ----------------
_db = sqlite3.connect("vouch.db", check_same_thread=False)
_db.row_factory = sqlite3.Row
_cur = _db.cursor()
_migrate_schema(_db, _cur)


def _parse_allowed_chat_ids() -> set[int]:
    raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if not raw:
        return set()

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
def _get_rank(score: int) -> str:
    if score < 5:
        return "Watchlist 👀"
    if score < 15:
        return "Trusted ✅"
    return "Elite 🔒"


def _calculate_trust_score(confirmed: int, total: int, neg: int) -> int:
    unconfirmed = max(total - confirmed, 0)
    return (confirmed * 3) + (unconfirmed * 1) - (neg * 4)


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
        "SELECT 1 FROM vouches WHERE target_username=? AND giver_id=? AND created_at > ?",
        (target, from_user, cutoff),
    )
    return _cur.fetchone() is not None


def _on_negative_cooldown(from_user: str, target: str) -> bool:
    cutoff = (datetime.now() - timedelta(hours=VOUCH_COOLDOWN_HOURS)).isoformat()
    _cur.execute(
        "SELECT 1 FROM vouches WHERE target_username=? AND giver_id=? AND type='negative' AND created_at > ?",
        (target, from_user, cutoff),
    )
    return _cur.fetchone() is not None


def _is_blacklisted(user: str) -> tuple[bool, str]:
    _cur.execute("SELECT reason FROM blacklist WHERE user_key=?", (user,))
    row = _cur.fetchone()
    return (True, row[0]) if row else (False, "")


def _update_user_stats(username: str) -> sqlite3.Row:
    _cur.execute(
        """
        SELECT
            COUNT(*) AS total_vouches,
            COALESCE(SUM(CASE WHEN confirmed=1 THEN 1 ELSE 0 END), 0) AS confirmed_vouches
        FROM vouches
        WHERE target_username=?
        """,
        (username,),
    )
    row = _cur.fetchone()
    total_vouches = int(row["total_vouches"] or 0)
    confirmed_vouches = int(row["confirmed_vouches"] or 0)
    _cur.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN type='negative' AND resolved=0 THEN 1 ELSE 0 END), 0) AS neg_vouches
        FROM vouches
        WHERE target_username=?
        """,
        (username,),
    )
    neg_vouches = int(_cur.fetchone()["neg_vouches"] or 0)
    trust_score = _calculate_trust_score(confirmed_vouches, total_vouches, neg_vouches)
    now = datetime.now().isoformat()

    _cur.execute(
        """
        INSERT INTO user_stats (
            username,
            total_vouches,
            confirmed_vouches,
            neg_vouches,
            trust_score,
            last_updated
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(username) DO UPDATE SET
            total_vouches=excluded.total_vouches,
            confirmed_vouches=excluded.confirmed_vouches,
            neg_vouches=excluded.neg_vouches,
            trust_score=excluded.trust_score,
            last_updated=excluded.last_updated
        """,
        (
            username,
            total_vouches,
            confirmed_vouches,
            neg_vouches,
            trust_score,
            now,
        ),
    )
    _db.commit()
    _cur.execute(
        """
        SELECT username, total_vouches, confirmed_vouches, neg_vouches, trust_score, last_updated
        FROM user_stats
        WHERE username=?
        """,
        (username,),
    )
    row = _cur.fetchone()
    if row is None:
        raise RuntimeError("Failed to update user stats.")
    return row


def _sync_all_user_stats() -> None:
    _cur.execute(
        """
        SELECT DISTINCT user_key FROM (
            SELECT target_username AS user_key FROM vouches
        )
        WHERE user_key IS NOT NULL AND user_key != ''
        """
    )
    rows = _cur.fetchall()
    for row in rows:
        _update_user_stats(str(row["user_key"]))


def _get_vouch_for_confirmation(vouch_id: int) -> sqlite3.Row | None:
    _cur.execute(
        """
        SELECT id, giver_id, target_username, reason, confirmed
        FROM vouches
        WHERE id=? AND type='positive'
        """,
        (vouch_id,),
    )
    return _cur.fetchone()


def _get_pending_negvouch(negvouch_id: int) -> sqlite3.Row | None:
    _cur.execute(
        """
        SELECT id, user_key, from_user_key, reason, status, broadcast_chat_id, broadcast_message_id
        FROM neg_vouches
        WHERE id=?
        """,
        (negvouch_id,),
    )
    return _cur.fetchone()


def _build_profile_card(
    user_key: str,
    total_vouches: int,
    confirmed_vouches: int,
    neg_vouches: int,
    trust_score: int,
    blacklisted: bool,
    blacklist_reason: str,
) -> str:
    badge = "NTN VERIFIED" if trust_score >= 5 and not blacklisted else "NTN WATCH"
    rank = _get_rank(trust_score)
    score_icon = "🟢" if trust_score >= 0 else "🔴"
    card = [
        "┏━━━━━━━━━━━━━━━━━━━━━━━┓",
        "┃      NTN PROFILE      ┃",
        "┗━━━━━━━━━━━━━━━━━━━━━━━┛",
        f"👤 User: {_display_user_key(user_key)}",
        f"🪪 Badge: {badge}",
        f"📈 Total Vouches: {total_vouches}",
        f"✅ Confirmed: {confirmed_vouches}",
        f"📉 Negative: {neg_vouches}",
        f"{score_icon} Trust Score: {trust_score}",
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
            """
            INSERT INTO vouches (
                chat_id, giver_id, target_username, reason, type, confirmed, resolved, created_at
            ) VALUES (?, ?, ?, ?, 'positive', 1, 0, ?)
            """,
            (
                update.effective_chat.id if update.effective_chat else None,
                "anonymous",
                target,
                text,
                now,
            ),
        )

    _db.commit()
    if decision == "approved":
        _update_user_stats(target)

    if decision == "approved":
        await context.bot.send_message(
            chat_id=_get_broadcast_chat_id(),
            text=_format_broadcast_vouch(target, text, is_anonymous=True),
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
    await _send_action_log(
        context,
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


def _record_message_reaction(
    chat_id: int,
    message_id: int,
    reactor_user_key: str,
    reaction: str,
) -> dict[str, int]:
    if reaction not in REACTION_LABELS:
        raise ValueError("Unsupported reaction type")

    now = datetime.now().isoformat()
    _cur.execute(
        """
        INSERT INTO message_reactions (
            chat_id,
            message_id,
            reactor_user_key,
            reaction,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, message_id, reactor_user_key)
        DO UPDATE SET
            reaction=excluded.reaction,
            updated_at=excluded.updated_at
        """,
        (chat_id, message_id, reactor_user_key, reaction, now, now),
    )

    _cur.execute(
        """
        SELECT reaction, COUNT(*) AS c
        FROM message_reactions
        WHERE chat_id=? AND message_id=?
        GROUP BY reaction
        """,
        (chat_id, message_id),
    )
    rows = _cur.fetchall()
    _db.commit()

    counts = {key: 0 for key in REACTION_LABELS}
    for row in rows:
        counts[str(row[0])] = int(row[1])
    return counts


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

    user = update.effective_user
    admin_id = _get_admin_id()
    if chat.type == "private" and admin_id is not None and user is not None and user.id == admin_id:
        return True

    if chat.id in ALLOWED_CHAT_IDS:
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
    configured_admin_id = _get_admin_id()
    if configured_admin_id is not None and user.id == configured_admin_id:
        return True
    if chat.type == "private":
        return False
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
        await _send_action_log(
            context,
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
        "/neg @user reason\n"
        "/resolve @user note\n"
        "/flag @user note\n"
        "/profile @user\n"
        "/vouches @user\n"
        "/leaderboard\n"
        "/top\n"
        "/recent\n"
        "/stats\n"
        "/groupinfo\n\n"
        "Admin: /resolvenegvouch <case_id> resolution\n\n"
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

    if _on_cooldown(from_user_key, target):
        await message.reply_text(
            f"⏱️ You already vouched for {_display_user_key(target)} in the last {VOUCH_COOLDOWN_HOURS}h"
        )
        return

    if not _can_vouch(from_user_key):
        await message.reply_text("❌ Daily vouch limit reached (3)")
        return

    text = " ".join(context.args[1:]).strip() or random.choice(RANDOM_VOUCH_LINES)

    _cur.execute(
        """
        INSERT INTO vouches (
            chat_id, giver_id, target_username, reason, type, confirmed, resolved, created_at
        ) VALUES (?, ?, ?, ?, 'positive', 0, 0, ?)
        """,
        (
            update.effective_chat.id if update.effective_chat else None,
            from_user_key,
            target,
            text,
            datetime.now().isoformat(),
        ),
    )
    vouch_id = int(_cur.lastrowid)
    _db.commit()
    _update_user_stats(target)

    # Check if target just crossed Elite threshold — announce it
    _cur.execute("SELECT COUNT(*) FROM vouches WHERE target_username=?", (target,))
    new_count = _cur.fetchone()[0]
    if new_count == ELITE_THRESHOLD:
        await context.bot.send_message(
            chat_id=_get_broadcast_chat_id(),
            text=f"🏆 {_display_user_key(target)} just reached Elite rank with {new_count} vouches!",
        )

    broadcast_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🤝 Confirm Deal", callback_data=f"confirm_{vouch_id}")]]
    )
    await context.bot.send_message(
        chat_id=_get_broadcast_chat_id(),
        text=_format_pending_confirm_vouch(target, from_user_key, text),
        reply_markup=broadcast_keyboard,
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

    if not _can_vouch(from_user_key):
        await message.reply_text("❌ Daily vouch limit reached (3)")
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
            (
        "SELECT id, chat_id, giver_id, target_username, reason, type, confirmed, resolved, created_at "
        "FROM vouches ORDER BY created_at DESC"
            ),
            ["id", "chat_id", "giver_id", "target_username", "reason", "type", "confirmed", "resolved", "created_at"],
        ),
        "negvouches": (
            (
                "SELECT id, user_key, from_user_key, reason, created_at, source_chat, "
                "status, resolution_note, resolved_by, resolved_at, broadcast_chat_id, broadcast_message_id "
                "FROM neg_vouches ORDER BY created_at DESC"
            ),
            [
                "id",
                "user_key",
                "from_user_key",
                "reason",
                "created_at",
                "source_chat",
                "status",
                "resolution_note",
                "resolved_by",
                "resolved_at",
                "broadcast_chat_id",
                "broadcast_message_id",
            ],
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
    await _send_action_log(
        context,
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
        "  SELECT id FROM vouches WHERE target_username=? AND giver_id=? ORDER BY created_at DESC LIMIT 1"
        ")",
        (target, from_user),
    )
    _db.commit()
    if _cur.rowcount:
        _update_user_stats(target)
        await message.reply_text(
            f"🗑️ Your most recent vouch for {_display_user_key(target)} was removed."
        )
    else:
        await message.reply_text(
            f"No vouch from you to {_display_user_key(target)} found."
        )


async def unvouch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    if not context.args:
        await message.reply_text("Usage: /unvouch @username reason")
        return

    from_user_key = _get_actor_user_key(update)
    try:
        target_user_key = _normalize_target_arg(context.args[0])
    except ValueError:
        await message.reply_text("Usage: /unvouch @username reason")
        return

    if from_user_key == target_user_key:
        await message.reply_text("❌ You cannot unvouch yourself.")
        return

    reason = " ".join(context.args[1:]).strip() or "No reason provided"
    _cur.execute(
        "DELETE FROM vouches WHERE id = ("
        "  SELECT id FROM vouches WHERE target_username=? AND giver_id=? ORDER BY created_at DESC LIMIT 1"
        ")",
        (target_user_key, from_user_key),
    )
    _db.commit()

    if not _cur.rowcount:
        await message.reply_text(
            f"No stored vouch from you to {_display_user_key(target_user_key)} was found to reverse."
        )
        return

    _update_user_stats(target_user_key)

    actor = _build_actor_name(update)
    source_chat = _build_source_chat(update)
    await context.bot.send_message(
        chat_id=_get_broadcast_chat_id(),
        text=(
            "Unvouch\n"
            f"Target: {_display_user_key(target_user_key)}\n"
            f"From: {actor}\n"
            f"Reason: {reason}\n"
            f"Source chat: {source_chat}\n"
            "Result: latest stored vouch removed"
        ),
    )
    await message.reply_text(
        f"✅ Unvouch complete. Latest stored vouch to {_display_user_key(target_user_key)} was removed."
    )


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
    now = datetime.now().isoformat()
    _cur.execute(
        """
        INSERT INTO neg_vouches (
            user_key,
            from_user_key,
            reason,
            created_at,
            source_chat,
            status
        ) VALUES (?, ?, ?, ?, ?, 'pending')
        """,
        (
            request.target_user_key,
            actor_user_key,
            request.reason,
            now,
            source_chat,
        ),
    )
    negvouch_id = int(_cur.lastrowid)
    _db.commit()

    _log_staff_action(
        staff_user_key=actor_user_key,
        action="negvouch",
        target_user_key=request.target_user_key,
        reason=request.reason,
        details=f"pending_case_id={negvouch_id}",
        source_chat=source_chat,
    )
    await _send_action_log(
        context,
        staff_user_key=actor_user_key,
        action="negvouch",
        target_user_key=request.target_user_key,
        reason=request.reason,
        details=f"pending_case_id={negvouch_id}",
        source_chat=source_chat,
    )

    reply_to_message_id = None
    if (
        message.reply_to_message is not None
        and update.effective_chat is not None
        and update.effective_chat.id == _get_broadcast_chat_id()
    ):
        reply_to_message_id = message.reply_to_message.message_id

    broadcast_message = await context.bot.send_message(
        chat_id=_get_broadcast_chat_id(),
        text=_format_pending_negvouch(request.target_user_key, request.reason),
        reply_to_message_id=reply_to_message_id,
    )
    _cur.execute(
        """
        UPDATE neg_vouches
        SET broadcast_chat_id=?, broadcast_message_id=?
        WHERE id=?
        """,
        (_get_broadcast_chat_id(), broadcast_message.message_id, negvouch_id),
    )
    _db.commit()
    _update_user_stats(request.target_user_key)
    await message.reply_text(
        f"Pending negative vouch #{negvouch_id} for {_display_user_key(request.target_user_key)} was posted."
    )


async def _send_action_log(
    context: ContextTypes.DEFAULT_TYPE,
    staff_user_key: str,
    action: str,
    target_user_key: str | None = None,
    reason: str | None = None,
    details: str | None = None,
    source_chat: str | None = None,
) -> None:
    await context.bot.send_message(
        chat_id=_get_broadcast_chat_id(),
        text=_format_action_log(
            staff_user_key=staff_user_key,
            action=action,
            target_user_key=target_user_key,
            reason=reason,
            details=details,
            source_chat=source_chat,
        ),
    )


async def resolvenegvouch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    if not await _is_admin(update, context):
        await message.reply_text("❌ Only group admins can use this command.")
        return
    if not context.args or len(context.args) < 2 or not context.args[0].isdigit():
        await message.reply_text("Usage: /resolvenegvouch <case_id> resolution")
        return

    negvouch_id = int(context.args[0])
    resolution_note = " ".join(context.args[1:]).strip()
    record = _get_pending_negvouch(negvouch_id)
    if record is None:
        await message.reply_text("❌ Negative vouch case not found.")
        return
    if str(record["status"]) != "pending":
        await message.reply_text(f"❌ Negative vouch case #{negvouch_id} is already {record['status']}.")
        return

    resolved_by = _get_actor_user_key(update)
    resolved_at = datetime.now().isoformat()
    _cur.execute(
        """
        UPDATE neg_vouches
        SET status='resolved', resolution_note=?, resolved_by=?, resolved_at=?
        WHERE id=?
        """,
        (resolution_note, resolved_by, resolved_at, negvouch_id),
    )
    _db.commit()
    _update_user_stats(str(record["user_key"]))

    _log_staff_action(
        staff_user_key=resolved_by,
        action="resolve_negvouch",
        target_user_key=str(record["user_key"]),
        reason=resolution_note,
        details=f"case_id={negvouch_id}",
        source_chat=_build_source_chat(update),
    )
    await _send_action_log(
        context,
        staff_user_key=resolved_by,
        action="resolve_negvouch",
        target_user_key=str(record["user_key"]),
        reason=resolution_note,
        details=f"case_id={negvouch_id}",
        source_chat=_build_source_chat(update),
    )

    if record["broadcast_chat_id"] and record["broadcast_message_id"]:
        try:
            await context.bot.edit_message_text(
                chat_id=int(record["broadcast_chat_id"]),
                message_id=int(record["broadcast_message_id"]),
                text=_format_resolved_negvouch(
                    str(record["user_key"]),
                    str(record["reason"]),
                    resolution_note,
                    resolved_by,
                ),
            )
        except Exception:
            pass

    await message.reply_text(
        f"✅ Negative vouch case #{negvouch_id} for {_display_user_key(str(record['user_key']))} was resolved."
    )


async def resolve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    if not await _is_admin(update, context):
        await message.reply_text("❌ Only group admins can use this command.")
        return
    if not context.args:
        await message.reply_text("Usage: /resolve @user [note]")
        return
    try:
        target_user_key = _normalize_target_arg(context.args[0])
    except ValueError:
        await message.reply_text("Usage: /resolve @user [note]")
        return

    resolution_note = " ".join(context.args[1:]).strip() or "Issue resolved"
    resolved_by = _get_actor_user_key(update)
    source_chat = _build_source_chat(update)
    _cur.execute(
        """
        SELECT id
        FROM vouches
        WHERE target_username=? AND type='negative' AND resolved=0
        """,
        (target_user_key,),
    )
    rows = _cur.fetchall()
    if not rows:
        await message.reply_text(f"❌ No pending negative vouches for {_display_user_key(target_user_key)}.")
        return

    _cur.execute(
        """
        UPDATE vouches
        SET resolved=1
        WHERE target_username=? AND type='negative' AND resolved=0
        """,
        (target_user_key,),
    )
    _db.commit()
    _update_user_stats(target_user_key)

    _log_staff_action(
        staff_user_key=resolved_by,
        action="resolve",
        target_user_key=target_user_key,
        reason=resolution_note,
        details=f"resolved_cases={len(rows)}",
        source_chat=source_chat,
    )
    await _send_action_log(
        context,
        staff_user_key=resolved_by,
        action="resolve",
        target_user_key=target_user_key,
        reason=resolution_note,
        details=f"resolved_cases={len(rows)}",
        source_chat=source_chat,
    )

    await message.reply_text(
        f"✅ ISSUE RESOLVED\n\nUser: {_display_user_key(target_user_key)}\n\nStatus: Cleared 🔒"
    )


async def flag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    if len(context.args) < 2:
        await message.reply_text("Usage: /flag @user note")
        return
    try:
        target_user_key = _normalize_target_arg(context.args[0])
    except ValueError:
        await message.reply_text("Usage: /flag @user note")
        return

    note = " ".join(context.args[1:]).strip()
    actor_user_key = _get_actor_user_key(update)
    source_chat = _build_source_chat(update)
    text = (
        "⚠️ NTN FLAG\n\n"
        f"User: {_display_user_key(target_user_key)}\n\n"
        f"Note:\n“{note}”\n\n"
        "Status: Under review 👀"
    )
    await context.bot.send_message(chat_id=_get_broadcast_chat_id(), text=text)
    _log_staff_action(
        staff_user_key=actor_user_key,
        action="flag",
        target_user_key=target_user_key,
        reason=note,
        details="soft_warning",
        source_chat=source_chat,
    )
    await _send_action_log(
        context,
        staff_user_key=actor_user_key,
        action="flag",
        target_user_key=target_user_key,
        reason=note,
        details="soft_warning",
        source_chat=source_chat,
    )
    await message.reply_text(text)


async def neg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    if len(context.args) < 2:
        await message.reply_text("Usage: /neg @user reason")
        return

    giver = _get_actor_user_key(update)
    try:
        target = _normalize_target_arg(context.args[0])
    except ValueError:
        await message.reply_text("Usage: /neg @user reason")
        return
    reason = " ".join(context.args[1:]).strip()
    if giver == target:
        await message.reply_text("❌ You cannot neg-vouch yourself.")
        return
    if _on_negative_cooldown(giver, target):
        await message.reply_text(
            f"⏱️ You already neg-vouched {_display_user_key(target)} in the last {VOUCH_COOLDOWN_HOURS}h"
        )
        return

    _cur.execute(
        """
        INSERT INTO vouches (chat_id, giver_id, target_username, reason, type, confirmed, resolved, created_at)
        VALUES (?, ?, ?, ?, 'negative', 0, 0, ?)
        """,
        (
            update.effective_chat.id if update.effective_chat else None,
            giver,
            target,
            reason,
            datetime.now().isoformat(),
        ),
    )
    _db.commit()
    _update_user_stats(target)

    text = (
        f"⚠️ NEG VOUCH LOG\n\nUser: {_display_user_key(target)}\n"
        f"From: {_display_user_key(giver)}\n\nReason:\n“{reason}”\n\nImpact: -4 Score ❌"
    )
    await context.bot.send_message(chat_id=_get_broadcast_chat_id(), text=text)
    await message.reply_text(text)


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
    await _send_action_log(
        context,
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
    await _send_action_log(
        context,
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
    _cur.execute(
        "SELECT giver_id, reason FROM vouches WHERE target_username=?",
        (target,),
    )
    rows = _cur.fetchall()
    if not rows:
        await message.reply_text("No vouches yet.")
        return
    msg = f"📜 Vouches for {_display_user_key(target)}:\n\n"
    for r in rows[-10:]:
        msg += f"{_display_vouch_actor(r[0])}: {r[1]}\n"
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
    stats_row = _update_user_stats(target)
    blacklisted, bl_reason = _is_blacklisted(target)
    await message.reply_text(
        _build_profile_card(
            user_key=target,
            total_vouches=int(stats_row["total_vouches"]),
            confirmed_vouches=int(stats_row["confirmed_vouches"]),
            neg_vouches=int(stats_row["neg_vouches"]),
            trust_score=int(stats_row["trust_score"]),
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
    stats_row = _update_user_stats(me)
    _cur.execute("SELECT COUNT(*) FROM vouches WHERE giver_id=?", (me,))
    given = _cur.fetchone()[0]
    await message.reply_text(
        f"📊 Your Stats ({_display_user_key(me)})\n"
        f"✅ Vouches given: {given}\n"
        f"⭐ Total received: {int(stats_row['total_vouches'])}\n"
        f"🔒 Confirmed received: {int(stats_row['confirmed_vouches'])}\n"
        f"❌ Negative received: {int(stats_row['neg_vouches'])}\n"
        f"⚖️ Trust score: {int(stats_row['trust_score'])}\n"
        f"🏆 Rank: {_get_rank(int(stats_row['trust_score']))}"
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
    _cur.execute(
        "SELECT giver_id, reason FROM vouches WHERE target_username=?",
        (target,),
    )
    received = _cur.fetchall()
    _cur.execute(
        "SELECT target_username, reason FROM vouches WHERE giver_id=? AND giver_id!='anonymous'",
        (target,),
    )
    given = _cur.fetchall()
    parts = [f"🔍 Search results for {_display_user_key(target)}"]
    if received:
        parts.append(f"\n📥 Received ({len(received)}):")
        for r in received[-5:]:
            parts.append(f"  {_display_vouch_actor(r[0])}: {r[1]}")
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
        "SELECT giver_id, target_username, reason, created_at FROM vouches ORDER BY created_at DESC LIMIT 5"
    )
    rows = _cur.fetchall()
    if not rows:
        await message.reply_text("No vouches recorded yet.")
        return
    msg = "📌 Recent Vouches:\n\n"
    for r in rows:
        ts = r[3][:10]
        msg += f"{_display_vouch_actor(r[0])} → {_display_user_key(r[1])}: {r[2]} [{ts}]\n"
    await message.reply_text(msg)


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    _sync_all_user_stats()
    _cur.execute(
        "SELECT username, trust_score FROM user_stats ORDER BY trust_score DESC, confirmed_vouches DESC, total_vouches DESC LIMIT 10"
    )
    rows = _cur.fetchall()
    if not rows:
        await message.reply_text("No vouches recorded yet.")
        return
    msg = "🏆 NTN LEADERBOARD — TRUST 🔒\n\n"
    for i, r in enumerate(rows, 1):
        msg += f"{i}. {_display_user_key(r[0])} — {r[1]} 🔒\n"
    await message.reply_text(msg)


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not await _ensure_chat_allowed(update):
        return
    _sync_all_user_stats()
    _cur.execute(
        """
        SELECT username, total_vouches, confirmed_vouches, neg_vouches, trust_score
        FROM user_stats
        ORDER BY trust_score DESC, confirmed_vouches DESC, total_vouches DESC, username ASC
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
            f"{i}. {_display_user_key(row['username'])} | total {row['total_vouches']} | confirmed {row['confirmed_vouches']} | neg {row['neg_vouches']} | score {row['trust_score']}"
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

    if data.startswith("confirm_"):
        await _handle_confirm_vouch_callback(update, context)
        return

    # Reaction buttons on regular vouches
    if data in REACTION_LABELS:
        message = query.message
        user = query.from_user
        if message is None or user is None:
            await query.answer("Unable to save reaction.", show_alert=True)
            return

        reactor_user_key = _normalize_user_key(user.username) if user.username else f"id:{user.id}"
        if not reactor_user_key:
            await query.answer("Unable to identify user for reaction.", show_alert=True)
            return

        counts = _record_message_reaction(
            chat_id=message.chat_id,
            message_id=message.message_id,
            reactor_user_key=reactor_user_key,
            reaction=data,
        )
        summary = " ".join(
            f"{REACTION_LABELS[key]} {counts.get(key, 0)}" for key in ("legit", "fire", "cap")
        )
        await query.answer(f"Saved {REACTION_LABELS[data]} | {summary}")
        return

    await query.answer()


async def _handle_confirm_vouch_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return

    data = query.data or ""
    _, _, raw_vouch_id = data.partition("_")
    if not raw_vouch_id.isdigit():
        await query.answer("Invalid confirmation link.", show_alert=True)
        return

    vouch_id = int(raw_vouch_id)
    record = _get_vouch_for_confirmation(vouch_id)
    if record is None:
        await query.answer("Vouch not found.", show_alert=True)
        return
    if int(record["confirmed"] or 0) == 1:
        await query.answer("This vouch is already confirmed.", show_alert=True)
        return

    user = query.from_user
    if user is None:
        await query.answer("Unable to verify user.", show_alert=True)
        return

    actor_user_key = _normalize_user_key(user.username) if user.username else f"id:{user.id}"
    target_username = str(record["target_username"] or "")
    if actor_user_key != target_username:
        await query.answer("Only the vouched user can confirm this deal.", show_alert=True)
        return

    _cur.execute("UPDATE vouches SET confirmed=1 WHERE id=?", (vouch_id,))
    _db.commit()
    _update_user_stats(target_username)

    try:
        await query.edit_message_text(
            text=_format_confirmed_vouch(
                target_username,
                str(record["giver_id"] or ""),
                str(record["reason"] or ""),
            )
        )
    except Exception:
        pass

    await query.answer("Deal confirmed.")


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
    _sync_all_user_stats()
    await application.bot.set_my_commands([
        BotCommand("start", "Welcome message and command summary"),
        BotCommand("vouch", "Vouch for a user (stored + broadcast)"),
        BotCommand("vouchanon", "Vouch anonymously (requires admin approval)"),
        BotCommand("neg", "Create a negative vouch entry (admin only)"),
        BotCommand("resolve", "Resolve all pending negatives for a user (admin only)"),
        BotCommand("flag", "Post an NTN review flag"),
        BotCommand("pending_vouches", "View pending anonymous vouches (admin only)"),
        BotCommand("approveanon", "Approve anon vouch with a reason (admin only)"),
        BotCommand("rejectanon", "Reject anon vouch with a reason (admin only)"),
        BotCommand("removevouch", "Remove your last vouch for a user"),
        BotCommand("unvouch", "Reverse your latest stored vouch for a user"),
        BotCommand("negvouch", "Open a pending negative-vouch case (admin only)"),
        BotCommand("resolvenegvouch", "Resolve a pending negative-vouch case (admin only)"),
        BotCommand("vouches", "View vouches for a user"),
        BotCommand("profile", "View NTN-style profile with trust score"),
        BotCommand("stats", "View your own positive/negative rep stats"),
        BotCommand("leaderboard", "Top users by NTN trust score"),
        BotCommand("search", "Search vouches given and received by a user"),
        BotCommand("recent", "Last 5 vouches across all users"),
        BotCommand("top", "Top 10 users by trust score"),
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
    application.add_handler(CommandHandler("neg", neg))
    application.add_handler(CommandHandler("resolve", resolve))
    application.add_handler(CommandHandler("flag", flag))
    application.add_handler(CommandHandler("pending_vouches", pending_vouches))
    application.add_handler(CommandHandler("approveanon", approveanon))
    application.add_handler(CommandHandler("rejectanon", rejectanon))
    application.add_handler(CommandHandler("removevouch", removevouch))
    application.add_handler(CommandHandler("unvouch", unvouch))
    application.add_handler(CommandHandler("negvouch", negvouch))
    application.add_handler(CommandHandler("resolvenegvouch", resolvenegvouch))
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
