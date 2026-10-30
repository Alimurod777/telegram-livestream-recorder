"""Run private-chat updates concurrently, preserving each user's order."""

import asyncio
import logging
from collections import deque

logger = logging.getLogger(__name__)


def private_user_id(update):
    payload = update.get("callback_query") or update.get("message") or update.get("edited_message")
    if not payload:
        return None
    message = payload.get("message", {}) if update.get("callback_query") else payload
    chat = message.get("chat", {})
    user_id = payload.get("from", {}).get("id")
    if user_id and chat.get("type") == "private" and chat.get("id") == user_id:
        return int(user_id)
    return None


class UserUpdateDispatcher:
    def __init__(self, handler):
        self._handler = handler
        self._queues = {}
        self._workers = {}
        self._closed = False

    def submit(self, update):
        """Enqueue without waiting for another user's network requests."""
        if self._closed:
            raise RuntimeError("Update dispatcher is closed")
        user_id = private_user_id(update)
        if user_id is None:
            return
        self._queues.setdefault(user_id, deque()).append(update)
        if user_id not in self._workers:
            self._workers[user_id] = asyncio.create_task(self._run(user_id))

    async def _run(self, user_id):
        try:
            queue = self._queues[user_id]
            while queue:
                update = queue.popleft()
                try:
                    await self._handler(update)
                except Exception:
                    logger.exception("Update handler failed for user %s", user_id)
        finally:
            self._queues.pop(user_id, None)
            self._workers.pop(user_id, None)

    async def close(self):
        """Cancel pending commands before recording and client cleanup."""
        self._closed = True
        workers = list(self._workers.values())
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self._workers.clear()
        self._queues.clear()
