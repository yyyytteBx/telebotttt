# telebotttt Bot Documentation

## Checklist
- [x] Explain the bot's purpose
- [x] Describe the current architecture
- [x] Document configuration and dependencies
- [x] List user-facing and admin-facing features
- [x] Explain the main workflows
- [x] Clarify intended or partially implemented features

## 1. What This Bot Is

`telebotttt` is a Telegram reputation and deal-vouch bot.

Its main purpose is to let a community:
- record positive vouches for users,
- submit anonymous vouches for admin review,
- record negative reputation events,
- resolve reputation disputes,
- maintain a blacklist,
- inspect user trust profiles and leaderboards,
- export moderation and reputation data.

In practical terms, it is designed for deal/trading/service communities where members want a lightweight reputation ledger inside Telegram.

---

## 2. Core Purpose

The bot exists to solve three related problems:

### A. Reputation Tracking
It stores who vouched for whom, why they vouched, whether the target confirmed the deal, and how that affects the target's trust score.

### B. Moderation Support
It gives admins tools to:
- review anonymous vouches,
- blacklist or unblacklist users,
- view staff action logs,
- export internal data,
- resolve negative-reputation cases.

### C. Community Visibility
It publishes key events into a broadcast channel/group, such as:
- pending or confirmed vouch logs,
- anonymous vouches once approved,
- flags,
- blacklists,
- action logs,
- startup/online messages.

---

## 3. High-Level Architecture

The project is intentionally simple and centered around a single file:

- `bot.py` — the main application, business logic, database schema, migrations, handlers, and startup.
- `requirements.txt` — Python dependencies.
- `vouch.db` — SQLite database used at runtime.
- `README.md` — minimal setup notes.

### Runtime stack
- **Language:** Python
- **Telegram framework:** `python-telegram-bot` (async API)
- **Config loading:** `python-dotenv`
- **Persistence:** SQLite via `sqlite3`

### Application shape
The bot starts through `run_bot()` and builds a Telegram `Application` object. It then registers:
- command handlers for slash commands,
- one callback query handler for inline button actions,
- a `post_init()` hook that sets bot commands and sends an online message.

### Storage model
The bot uses a single SQLite database connection created at import time:
- `_db = sqlite3.connect("vouch.db", check_same_thread=False)`
- `_cur = _db.cursor()`

Schema migration is also executed at import/startup time via `_migrate_schema(_db, _cur)`.

---

## 4. Main Concepts in the Bot

### 4.1 User keys
The bot normalizes users into a consistent internal format:
- usernames become `@username`
- numeric IDs become `id:123456`

This helps the bot work even when a Telegram user has no username.

### 4.2 Positive vouches
A positive vouch is a stored reputation event that includes:
- who gave it,
- who received it,
- the reason/message,
- whether the deal was confirmed by the target.

### 4.3 Anonymous vouches
Anonymous vouches are not published immediately. They are first stored in a pending queue and must be approved or rejected by an admin.

### 4.4 Negative vouches
Negative entries reduce trust score and are treated as unresolved issues until resolved by an admin.

### 4.5 Trust profiles
The bot calculates aggregate stats for a target user and presents them as a profile card.

### 4.6 Broadcast channel/group
A dedicated broadcast chat is used for public or staff-visible event messages. The current default broadcast chat ID is:

`-1003744224655`

That broadcast chat is implicitly allowed by the central chat-allow check.

---

## 5. Configuration

The bot is configured through environment variables.

### Required
#### `TELEGRAM_BOT_TOKEN`
Telegram bot token used to connect and run polling.

### Important optional configuration
#### `TELEGRAM_ALLOWED_CHAT_IDS`
Comma-separated Telegram chat IDs that are allowed to use the bot.

Behavior:
- if empty/unset, normal groups are denied,
- configured admins can still use the bot in private DM,
- the configured broadcast chat is also automatically allowed.

#### `TELEGRAM_BROADCAST_CHAT_ID`
Broadcast chat for logs, vouches, and status messages.
If unset, the bot uses the built-in default:
- `-1003744224655`

#### `TELEGRAM_ADMIN_USER_IDS`
Comma-separated Telegram user IDs for configured admins.

#### `TELEGRAM_ADMIN_USER_ID`
Single admin user ID. This is supported alongside the multi-admin variable.

### Dotenv behavior
The bot loads env values from:
1. the default `.env`
2. `.venv/.env`

---

## 6. Access Model

The bot has two overlapping access ideas:

### 6.1 Allowed chats
Most commands only work if the chat passes `_ensure_chat_allowed()`.

A chat is allowed when:
- it is in `TELEGRAM_ALLOWED_CHAT_IDS`, or
- it is the configured broadcast chat, or
- it is a private chat and the user is a configured admin.

### 6.2 Admin privileges
Admin behavior is checked in two ways depending on the feature:
- **configured admin IDs** via `_is_configured_admin_id()`
- **chat admins/creators** via `_is_admin()`

`_is_admin()` treats a user as admin if:
- their Telegram user ID is in configured admin IDs, or
- they are a Telegram admin/creator in a non-private chat.

This means some features are restricted to configured admins only, while others are available to chat admins too.

---

## 7. Database Schema

The bot creates and migrates several SQLite tables.

### `vouches`
Primary reputation ledger.

Fields include:
- `chat_id`
- `giver_id`
- `target_username`
- `reason`
- `type` (`positive` or `negative`)
- `confirmed`
- `resolved`
- `created_at`

This is the most important table in the live implementation.

### `limits`
Tracks daily vouch limits per user.

### `blacklist`
Stores blocked users and the reason they were blacklisted.

### `anon_vouch_pending`
Queue for anonymous vouches waiting for admin approval or rejection.

### `neg_vouches`
A dedicated table for negative-vouch case tracking exists in the schema, including fields for status, resolution note, and broadcast message IDs.

Important note: the current active negative-vouch flow is still mainly using the `vouches` table with `type='negative'`, so `neg_vouches` appears to be part of an unfinished or legacy transition.

### `staff_logs`
Stores internal staff/admin action history.

### `message_reactions`
Stores button reactions added to vouch messages.

### `user_stats`
Cached aggregate stats for each user:
- total vouches,
- confirmed vouches,
- negative unresolved vouches,
- trust score,
- last updated timestamp.

---

## 8. Trust Score and Ranking

The bot computes trust like this:
- confirmed positive vouch = `+3`
- unconfirmed positive vouch = `+1`
- unresolved negative vouch = `-4`
- additional penalty of `-5` when a user has 3 or more negative vouches

### Rank labels
- score `< 5` → `Watchlist 👀`
- score `< 15` → `Trusted ✅`
- otherwise → `Elite 🔒`

### Risk labels
- `0` negatives → `🔒 Clean`
- `1` negative → `👀 Review`
- `2+` negatives → `⚠️ Risk`

### Elite threshold
When a user reaches `20` total vouches, the bot announces an elite-rank message in the broadcast chat.

---

## 9. Command Reference

## 9.1 General user commands

### `/start`
Shows a short welcome/help message.

### `/vouch @user message`
Creates a positive vouch.

Behavior:
- checks chat access,
- checks self-vouch prevention,
- checks blacklist rules,
- checks per-user cooldown,
- checks daily limit,
- stores the vouch as unconfirmed,
- updates user stats,
- posts a confirmation request to broadcast chat,
- replies in-chat with a vouch message that includes reaction buttons.

### `/vouchanon @user message`
Creates an anonymous vouch request.

Behavior:
- same basic validation as `/vouch`,
- stores the request in `anon_vouch_pending`,
- sends admin review buttons to configured admin IDs,
- does not broadcast publicly until approved.

### `/removevouch @user`
Deletes the user's most recent stored vouch for that target.

### `/unvouch @user reason`
Also removes the latest stored vouch by that user for the target, but additionally broadcasts an “unvouch” event with the supplied reason.

### `/vouches @user`
Shows recent vouches received by a user.

### `/profile @user`
Displays an NTN-style reputation profile card.

### `/stats`
Displays the caller's own reputation statistics.

### `/search @user`
Shows both:
- recent vouches received by the target,
- recent vouches given by the target.

### `/recent`
Shows the last 5 vouches across the system.

### `/top`
Shows the top 10 users ranked by trust score.

### `/leaderboard`
Shows a more detailed leaderboard with totals, confirmed counts, negative counts, score, and risk label.

### `/groupinfo`
Fetches Telegram metadata about the current group/chat.

---

## 9.2 Admin and moderation commands

### `/pending_vouches`
Lists pending anonymous vouches for configured admins.

### `/approveanon <vouch_id> reason`
Approves a pending anonymous vouch.

### `/rejectanon <vouch_id> reason`
Rejects a pending anonymous vouch.

### `/resolve @user [note]`
Marks all unresolved negative vouches for the target as resolved.

### `/flag @user note`
Broadcasts a warning/review note for a user and logs the action.

### `/blacklist @user reason`
Adds or replaces a blacklist entry.

### `/unblacklist @user`
Removes a user from the blacklist.

### `/stafflogs [limit]`
Shows recent staff action history.

### `/export <dataset> <csv|json>`
Exports one of several datasets:
- `vouches`
- `negvouches`
- `blacklist`
- `anon`
- `stafflogs`

---

## 9.3 Negative-vouch aliases

### `/neg @user reason`
Creates a negative vouch entry.

### `/negvouch`
Currently behaves as an alias to `/neg`.

### `/resolvenegvouch`
Currently behaves as an alias to `/resolve`.

Important implementation note:
- The command descriptions suggest a richer case-based negative-vouch workflow.
- The current code mainly treats these as aliases rather than a separate full case-management system.

---

## 10. Callback / Button Features

The bot uses inline buttons for two main workflows.

### 10.1 Deal confirmation button
After a normal `/vouch`, the broadcast chat receives a message with:
- `🤝 Confirm Deal`

Only the vouched target user can press this button successfully.
When pressed:
- the vouch is marked as confirmed,
- user stats are recalculated,
- the broadcast message is updated to a confirmed format.

### 10.2 Anonymous admin review buttons
Pending anonymous vouches sent to admins include:
- `✅ Approve`
- `❌ Reject`

These buttons call `_handle_anon_decision()` and either:
- insert the approved anonymous vouch into `vouches`, or
- mark the pending request as rejected.

### 10.3 Reaction buttons
Regular vouch replies include reaction buttons:
- `👍`
- `🔥`
- `❌`

The bot stores these in `message_reactions` and returns updated reaction totals.

---

## 11. Main Workflows

## 11.1 Positive vouch workflow
1. User runs `/vouch @target reason`
2. Bot validates input and permissions
3. Bot writes a positive unconfirmed record to `vouches`
4. Bot updates cached `user_stats`
5. Bot sends a pending confirmation message to broadcast chat
6. Target user can later confirm it through the inline button
7. Confirmation increases trust score weight

## 11.2 Anonymous vouch workflow
1. User runs `/vouchanon @target reason`
2. Bot stores request in `anon_vouch_pending`
3. Configured admins receive review buttons by DM/message
4. Admin approves or rejects
5. On approval:
   - bot inserts an anonymous positive vouch,
   - bot updates stats,
   - bot broadcasts the approved anonymous vouch,
   - bot logs the action

## 11.3 Negative reputation workflow
1. A user/admin issues `/neg @target reason`
2. Bot writes a `negative` row into `vouches`
3. Trust score is recalculated with a penalty
4. Broadcast message is sent
5. Later, `/resolve @target [note]` can mark all unresolved negatives as resolved

## 11.4 Blacklist workflow
1. Admin uses `/blacklist @target reason`
2. Entry is stored or replaced in `blacklist`
3. Action is logged
4. Broadcast event is sent
5. Blacklisted users are prevented from creating vouches, and blacklisted targets cannot receive them through normal vouch flows

## 11.5 Export/moderation audit workflow
1. Admin uses `/export ...`
2. Data is pulled from the chosen table/query
3. Output is serialized to CSV or JSON
4. Export is sent back as a Telegram document
5. Export action is logged and broadcast internally

---

## 12. Anti-Abuse and Guardrails

The bot contains several safeguards.

### Daily vouch limit
A user can only create 3 vouches per day.

### Cooldown
The same giver cannot repeatedly vouch the same target within 24 hours.

A similar 24-hour cooldown exists for negative vouches.

### Self-vouch prevention
Users cannot vouch or neg-vouch themselves.

### Blacklist enforcement
- blacklisted users cannot vouch,
- blacklisted targets cannot receive vouches in the standard vouch flows,
- anonymous approvals also auto-reject if the actor or target is blacklisted.

### Target-only confirmation
Only the vouched target may confirm a deal.

---

## 13. Staff Logging and Auditability

Moderation-sensitive actions are written to `staff_logs` and often also mirrored to the broadcast chat via `_send_action_log()`.

Examples include:
- anonymous vouch approvals/rejections,
- exports,
- resolve actions,
- flags,
- blacklist/unblacklist actions.

This gives the project a lightweight audit trail for moderation work.

---

## 14. Intended Features vs Current Reality

This section is important because the codebase shows both current features and signs of future/partial design.

### Clearly implemented today
- positive vouches
- anonymous vouch approval flow
- user profiles and trust scores
- deal confirmation buttons
- blacklist management
- staff action logs
- exports
- reaction tracking
- leaderboards

### Present in schema/design but only partially realized
- a dedicated `neg_vouches` case-management table exists,
- helper functions for pending negative-vouch retrieval/formatting exist,
- command descriptions mention “pending negative-vouch case” behavior,
- current live command handling still uses the main `vouches` table for negative entries.

This suggests the project intends to support a richer moderation/case workflow for negative reports, but the implementation has not fully moved there yet.

### Other notable design intentions
- broad migration support for older database shapes,
- support for both configured admins and Telegram group admins,
- a broadcast-centric operational style where important actions become visible in one place.

---

## 15. Operational Behavior

### Startup
On startup, the bot:
1. loads environment variables,
2. opens the SQLite database,
3. migrates schema if needed,
4. syncs `user_stats`,
5. registers Telegram command descriptions,
6. sends an online/loading-style message to the broadcast chat,
7. starts polling.

### Polling model
The bot uses long polling rather than webhooks.

### State persistence
All persistent reputation/moderation state is stored in `vouch.db`.

---

## 16. Dependencies

From `requirements.txt`:
- `python-telegram-bot>=21,<22`
- `python-dotenv>=1.0,<2`

The bot otherwise relies on Python standard library modules such as:
- `sqlite3`
- `csv`
- `io`
- `json`
- `datetime`
- `dataclasses`

---

## 17. Intended Audience and Use Case

This bot is best suited for communities where reputation needs to be:
- fast,
- visible,
- semi-structured,
- manageable by moderators.

Examples:
- trading communities,
- marketplace groups,
- service-vendor groups,
- escrow/reputation communities,
- invite-only trust networks.

---

## 18. Strengths of the Current Design

- Simple deployment model
- No external database requirement
- Strong emphasis on moderation visibility
- Good support for anonymous-but-reviewed reputation input
- Clear trust scoring model
- Built-in export capability
- Migration logic for older database versions

---

## 19. Current Limitations and Known Gaps

These are not necessarily failures, but they are relevant to understanding the project.

- The entire application lives in one large `bot.py`, which makes future maintenance harder.
- Negative-vouch workflow appears only partially migrated to the dedicated `neg_vouches` table.
- Access control style is mixed between configured-admin-only checks and general Telegram admin checks.
- SQLite is used through a global connection/cursor, which is simple but may become harder to scale or reason about under heavy concurrency.
- Setup documentation in the original `README.md` is minimal.

---

## 20. Suggested Future Improvements

If the project continues to grow, the next logical improvements would be:

1. **Split `bot.py` into modules**
   - commands
   - database
   - models
   - config
   - formatting/helpers

2. **Unify admin policy**
   - clearly define which commands require configured admins only,
   - which can be used by Telegram group admins.

3. **Finish the negative-case system**
   - either fully adopt `neg_vouches`,
   - or remove the unused parallel design.

4. **Improve documentation and onboarding**
   - setup,
   - env examples,
   - admin policy,
   - expected chat topology.

5. **Add tests**
   - trust score calculation,
   - normalization,
   - access checks,
   - cooldown logic,
   - migration safety.

6. **Improve repository hygiene**
   - avoid committing live database files, secrets, IDE files, and caches.

---

## 21. Short Summary

`telebotttt` is a Telegram reputation-management bot built for communities that need lightweight trust tracking.

Its intended feature set includes:
- public and anonymous vouching,
- moderation review,
- trust scoring,
- blacklist and flag handling,
- searchable reputation history,
- leaderboards,
- exports and staff audit logs.

Its current implementation already covers most of that vision, especially for positive reputation and moderation visibility, while the negative-vouch case system appears to be only partially transitioned to a more structured design.

