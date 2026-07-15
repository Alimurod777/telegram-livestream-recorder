import asyncio

from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded

from config import API_ID, API_HASH


async def generate_session_string():
    phone = input("Phone: ").strip()
    async with Client("session_generator", api_id=API_ID, api_hash=API_HASH, in_memory=True) as app:
        sent = await app.send_code(phone)
        code = input("Code: ").strip().replace(" ", "")
        try:
            await app.sign_in(phone, sent.phone_code_hash, code)
        except SessionPasswordNeeded:
            await app.check_password(input("2FA password: "))
        print(await app.export_session_string())


if __name__ == "__main__":
    asyncio.run(generate_session_string())
