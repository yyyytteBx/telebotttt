import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import (
    Channel,
    Chat,
    ChatForbidden,
    ChannelForbidden,
)

load_dotenv()

_SESSION_NAME = "userbot_session"


def _get_api_credentials() -> tuple[int, str]:
    api_id_raw = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    if not api_id_raw or not api_hash:
        raise RuntimeError(
            "Set TELEGRAM_API_ID and TELEGRAM_API_HASH environment variables."
        )
    return int(api_id_raw), api_hash


async def scrape_groups() -> list[dict]:
    """Return a list of all groups/supergroups/channels the user account is in."""
    api_id, api_hash = _get_api_credentials()

    async with TelegramClient(_SESSION_NAME, api_id, api_hash) as client:
        dialogs = await client.get_dialogs()

    results = []
    for dialog in dialogs:
        entity = dialog.entity

        if isinstance(entity, (ChatForbidden, ChannelForbidden)):
            continue

        if isinstance(entity, Chat):
            results.append(
                {
                    "title": entity.title,
                    "id": entity.id,
                    "type": "group",
                    "username": None,
                    "link": None,
                }
            )

        elif isinstance(entity, Channel):
            username = getattr(entity, "username", None)
            link = f"https://t.me/{username}" if username else None
            chat_type = "channel" if entity.broadcast else "supergroup"
            results.append(
                {
                    "title": entity.title,
                    "id": entity.id,
                    "type": chat_type,
                    "username": username,
                    "link": link,
                }
            )

    return results
