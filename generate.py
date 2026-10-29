import asyncio

from pyrogram import Client

from config import API_ID, API_HASH


async def generate_session_string():
    phone = input("Phone: ").strip()
    async with Client("session_generator", api_id=API_ID, api_hash=API_HASH,
                      phone_number=phone, hide_password=True, in_memory=True) as app:
        print(await app.export_session_string())


if __name__ == "__main__":
    asyncio.run(generate_session_string())
