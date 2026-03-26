"""Run this once to authenticate your Telegram account and save the session."""
import os

from dotenv import load_dotenv
from telethon.sync import TelegramClient

load_dotenv()

api_id = os.getenv("TELEGRAM_API_ID")
api_hash = os.getenv("TELEGRAM_API_HASH")

if not api_id or not api_hash:
    print("ERROR: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in your .env file")
    exit(1)

print("Logging in...")
with TelegramClient("userbot_session", int(api_id), api_hash) as client:
    me = client.get_me()
    print(f"✅ Logged in as: {me.first_name} (@{me.username})")
    print("Session saved to userbot_session.session — you're good to go!")
