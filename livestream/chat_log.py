"""Bounded private-conversation log, adapted from savebotnew/core/chat_log.py.

Native forwards preserve media without downloading it. Authentication inputs,
commands, owner conversations and progress UI never enter the queue.
"""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import logging
import re
import time

from .diagnostics import sanitize

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LogPolicy:
    chat_id: int = 0
    enabled: bool = False
    excluded: tuple = ()


class ChatLogger:
    def __init__(self, owner_id=0, secrets=()):
        self.owner_id = owner_id
        self.secrets = secrets
        self.policy = LogPolicy()
        self.revision = 0
        self.queue = asyncio.Queue(maxsize=200)
        self.lock = asyncio.Lock()
        self.task = None
        self.api = None
        self.sent = self.failed = self.dropped = 0
        self._seen = OrderedDict()
        self._last_header = None
        self._last_header_at = 0

    async def configure(self, policy):
        # Wait for any current RPC; queued events from an old policy are dropped.
        async with self.lock:
            self.policy = policy
            self.revision += 1

    def start(self, api):
        self.api = api
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
            self.dropped += 1

    def submit(self, message, *, outbound=False, sensitive=False):
        policy = self.policy
        chat = message.get('chat', {})
        uid, mid = chat.get('id'), message.get('message_id')
        if (not self.task or self.task.done() or sensitive or not policy.enabled
                or chat.get('type') != 'private' or not uid or uid <= 0 or not mid
                or uid == self.owner_id or uid in policy.excluded):
            return
        text = message.get('text') or message.get('caption') or ''
        if text.lstrip().startswith('/'):
            return
        if outbound and ('Yozuv boshlanganiga:' in text or text.startswith('📤')):
            return
        # Never forward raw text if masking would be required. Standalone codes
        # and phone numbers are excluded even outside the interactive login state.
        if re.fullmatch(r'[+\d\s-]{4,}', text.strip()) or re.search(r'[A-Za-z0-9_+/=-]{100,}', text):
            return
        safe = sanitize(text, self.secrets)
        key = (self.revision, uid, mid, message.get('edit_date'), text)
        if key in self._seen:
            return
        event = (self.revision, policy.chat_id, uid, mid, outbound,
                 safe if safe != text else None)
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            return
        self._seen[key] = None
        if len(self._seen) > 2000:
            self._seen.popitem(last=False)

    async def _deliver(self, event):
        revision, target, uid, mid, outbound, masked = event
        async with self.lock:
            if (revision != self.revision or not self.policy.enabled
                    or uid in self.policy.excluded or uid == self.owner_id):
                self.dropped += 1
                return
            header = f"👤 User ID: {uid} | {'Bot → user' if outbound else 'User → bot'}"
            header_key = (revision, uid, outbound)
            if self._last_header != header_key or time.monotonic() - self._last_header_at >= 300:
                await self._api('sendMessage', {'chat_id': target, 'text': header})
                self._last_header = header_key
                self._last_header_at = time.monotonic()
            if masked is not None:
                await self._api('sendMessage', {'chat_id': target, 'text': masked[:3500]})
            else:
                await self._api('forwardMessage', {'chat_id': target, 'from_chat_id': uid, 'message_id': mid})
            self.sent += 1

    async def _api(self, method, data):
        result = await asyncio.to_thread(self.api, method, data, timeout=10)
        if not result.get('ok'):
            raise RuntimeError('Chat log Bot API request failed')

    async def _run(self):
        while True:
            event = await self.queue.get()
            try:
                for attempt in range(3):
                    try:
                        await self._deliver(event)
                        break
                    except Exception as error:
                        delay = getattr(error, 'retry_after', 0)
                        if not delay or delay > 300 or attempt == 2:
                            raise
                        await asyncio.sleep(max(1, delay))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.failed += 1
                # Do not recursively enqueue a warning into Telegram diagnostics.
                logger.info('Chat log delivery failed: %s', type(error).__name__)
                delay = getattr(error, 'retry_after', 0)
                if delay:
                    await asyncio.sleep(max(1, delay))
            finally:
                self.queue.task_done()
            await asyncio.sleep(0.4)
