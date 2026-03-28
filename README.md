# telebotttt

`telebotttt` is a Telegram reputation bot for communities that need to track trusted deals, public vouches, anonymous staff-reviewed submissions, blacklist actions, and basic moderation audit history.

## What the bot does

The bot currently supports:

- positive vouches with deal confirmation
- anonymous vouches that require admin approval
- negative reputation entries and resolution flows
- trust profiles and leaderboards
- blacklist and unblacklist commands
- staff logs and data export
- reaction buttons on vouch messages

For a full implementation-level reference, see [`BOT_DOCUMENTATION.md`](./BOT_DOCUMENTATION.md).

## Requirements

- Python 3.11+ recommended
- a Telegram bot token
- at least one Telegram admin user ID

Note: the bot validates configuration at startup and exits early if required values are missing or malformed.

## Install

```bash
pip install -r requirements.txt
```

## Configuration

Create a `.env` file in the project root and configure the bot with environment variables.

### Required

- `TELEGRAM_BOT_TOKEN` — Telegram bot token

### Recommended

- `TELEGRAM_ADMIN_USER_IDS` — comma-separated admin Telegram user IDs
- `TELEGRAM_ADMIN_USER_ID` — optional single-admin fallback
- `TELEGRAM_ALLOWED_CHAT_IDS` — comma-separated allowed chat IDs
- `TELEGRAM_BROADCAST_CHAT_ID` — broadcast/log chat ID

### Access behavior

- Chats in `TELEGRAM_ALLOWED_CHAT_IDS` can use the bot.
- If `TELEGRAM_ALLOWED_CHAT_IDS` is empty or unset, the bot refuses operation in normal groups.
- Configured admins can still use the bot in private DM.
- The configured broadcast chat is always allowed automatically.
- If `TELEGRAM_BROADCAST_CHAT_ID` is not set, the default broadcast chat ID is `-1003744224655`.

### Startup validation behavior

- `TELEGRAM_BOT_TOKEN` must be present.
- At least one admin must be configured (`TELEGRAM_ADMIN_USER_IDS` or `TELEGRAM_ADMIN_USER_ID`).
- `TELEGRAM_ADMIN_USER_IDS` must contain valid integer user IDs.
- `TELEGRAM_ADMIN_USER_ID` must be an integer when set.
- `TELEGRAM_BROADCAST_CHAT_ID` must be an integer when set.
- `TELEGRAM_ALLOWED_CHAT_IDS` must contain comma-separated integer chat IDs when set.

## Minimal `.env` example

```dotenv
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_ADMIN_USER_IDS=123456789
TELEGRAM_ALLOWED_CHAT_IDS=-1001111111111,-1002222222222
TELEGRAM_BROADCAST_CHAT_ID=-1003744224655
```

## Run

```bash
python bot.py
```

## Main commands

### Community commands

- `/start`
- `/help [command]`
- `/vouch @user reason`
- `/vouchanon @user reason`
- `/removevouch @user`
- `/unvouch @user reason`
- `/vouches @user`
- `/profile @user`
- `/stats`
- `/search @user`
- `/recent`
- `/top`
- `/leaderboard`
- `/groupinfo`

### Moderation/admin commands

- `/pending_vouches`
- `/approveanon <vouch_id> reason`
- `/rejectanon <vouch_id> reason`
- `/neg @user reason`
- `/negvouch`
- `/resolve @user [note]`
- `/resolvenegvouch`
- `/flag @user note`
- `/blacklist @user reason`
- `/unblacklist @user`
- `/stafflogs [limit]`
- `/export <dataset> <csv|json>`

## Storage

The bot stores runtime data in `vouch.db` using SQLite. It also performs automatic schema migration during startup.

## Notes

- The bot uses long polling, not webhooks.
- Important events are broadcast to the configured broadcast chat.
- The codebase is currently centered in a single main file: `bot.py`.

