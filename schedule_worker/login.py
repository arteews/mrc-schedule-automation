import asyncio
import os
from pathlib import Path

from telethon import TelegramClient


async def main():
    data = Path(os.getenv("DATA_DIR", "/data"))
    data.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(data / "user"), int(os.environ["API_ID"]), os.environ["API_HASH"])
    await client.start(phone=os.environ["PHONE"])
    me = await client.get_me()
    print(f"Telegram account authorized: {me.id}")
    await client.disconnect()


asyncio.run(main())
