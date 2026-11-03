"""Optional subscription gate using the existing Bot API transport."""

import asyncio
import json
import logging

logger = logging.getLogger(__name__)


class SubscriptionGate:
    def __init__(self, api, *, enabled=False, channel=0, url="", owner_id=0):
        self.api = api
        self.enabled = enabled
        self.channel = channel
        self.url = url or (f"https://t.me/{channel.lstrip('@')}" if isinstance(channel, str) and channel.startswith("@") else "")
        self.owner_id = owner_id

    async def check(self, user_id):
        if not self.enabled or user_id == self.owner_id:
            return True
        try:
            result = await asyncio.to_thread(self.api, "getChatMember", {
                "chat_id": self.channel, "user_id": user_id,
            }, timeout=10)
            if not result.get("ok"):
                raise RuntimeError(result.get("description", "getChatMember failed"))
            member = result["result"]
            status = member.get("status")
            return status in {"creator", "administrator", "member"} or (status == "restricted" and member.get("is_member") is True)
        except Exception:
            logger.exception("Subscription check failed user=%s channel=%s", user_id, self.channel)
            return None

    async def enforce(self, user_id):
        state = await self.check(user_id)
        if state is True:
            return True
        buttons = []
        if self.url:
            buttons.append([{"text": "Kanalga a’zo bo‘lish", "url": self.url}])
        buttons.append([{"text": "Obunani tekshirish", "callback_data": "subscription:check"}])
        text = ("Botdan foydalanish uchun kanalga a’zo bo‘ling, so‘ng tekshirish tugmasini bosing."
                if state is False else "Obunani tekshirib bo‘lmadi. Birozdan keyin qayta urinib ko‘ring.")
        result = await asyncio.to_thread(self.api, "sendMessage", {
            "chat_id": user_id, "text": text, "reply_markup": json.dumps({"inline_keyboard": buttons}),
        }, timeout=10)
        if not result.get("ok"):
            logger.error("Subscription prompt failed user=%s: %s", user_id, result.get("description"))
        return False
