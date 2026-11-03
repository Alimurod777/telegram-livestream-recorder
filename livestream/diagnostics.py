"""Bounded Telegram diagnostics, with local correlation IDs and owner fallback."""

import asyncio
import contextvars
import hashlib
import logging
import re
import time
import uuid
from collections import OrderedDict

request_context = contextvars.ContextVar("diagnostic_context", default="")
_reporting = contextvars.ContextVar("diagnostic_reporting", default=False)


def sanitize(text, secrets=()):
    text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[REDACTED]")
    text = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", "[BOT_TOKEN]", text)
    text = re.sub(r"\+\d{7,15}\b", "[PHONE_REDACTED]", text)
    text = re.sub(r"(https?://|socks5?h?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"(?i)((?:session(?:_string)?|password|api_hash|phone(?:_number)?|code)\s*[=:]\s*)\S+", r"\1[REDACTED]", text)
    # Exported authorization strings may appear without a field name.
    return re.sub(r"[A-Za-z0-9_+/=-]{100,}", "[SESSION_REDACTED]", text)


def category(text):
    lower = text.lower()
    if any(s in lower for s in ("flood", "too many requests", "429")):
        return "rate_limit"
    if any(s in lower for s in ("auth_key", "session_revoked", "session_expired", "user_deactivated")):
        return "session"
    if any(s in lower for s in ("timeout", "network", "connection", "dns")):
        return "network"
    if any(s in lower for s in ("participant", "private", "forbidden", "admin_required", "banned")):
        return "access"
    if any(s in lower for s in ("ffmpeg", "encoder", "mux", "disk")):
        return "media"
    return "unexpected"


class TelegramDiagnostics(logging.Handler):
    """Never block capture or recursively report errors from the report sender."""

    def __init__(self, api, *, channel_id=0, owner_id=0, dedup_seconds=120, secrets=()):
        super().__init__(logging.WARNING)
        self.api = api
        self.channel_id = channel_id
        self.owner_id = owner_id
        self.dedup_seconds = dedup_seconds
        self.secrets = secrets
        self.queue = asyncio.Queue(maxsize=200)
        self.seen = OrderedDict()
        self.loop = None
        self.task = None
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s:%(lineno)d %(message)s"))

    def start(self):
        self.loop = asyncio.get_running_loop()
        self.task = self.loop.create_task(self._run())

    def emit(self, record):
        if _reporting.get() or self.loop is None or self.loop.is_closed():
            return
        try:
            detail = sanitize(self.format(record), self.secrets)
            context = sanitize(request_context.get(), self.secrets)
            stable_context = re.sub(r"update=\S+", "", context)
            key = hashlib.sha256((record.name + record.getMessage() + stable_context).encode()).hexdigest()
            self.loop.call_soon_threadsafe(self._enqueue, key, context, detail)
        except Exception:
            self.handleError(record)

    def _enqueue(self, key, context, detail):
        now = time.monotonic()
        if now - self.seen.get(key, -float("inf")) < self.dedup_seconds:
            return
        if self.queue.full():
            return  # All original records remain in the rotating local log.
        self.seen[key] = now
        self.seen.move_to_end(key)
        while len(self.seen) > 500:
            self.seen.popitem(last=False)
        incident_id = uuid.uuid4().hex[:12]
        text = f"Recorder incident={incident_id} category={category(detail)}\n{context}\n{detail}"
        # Telegram measures text length in UTF-16 code units; keep both ends of tracebacks.
        encoded = text.encode("utf-16-le", errors="replace")
        if len(encoded) > 7800:
            text = encoded[:3800].decode("utf-16-le", errors="ignore") + "\n…\n" + encoded[-3800:].decode("utf-16-le", errors="ignore")
        self.queue.put_nowait(text)
        token = _reporting.set(True)
        try:
            logging.getLogger(__name__).info("incident=%s %s\n%s", incident_id, context, detail)
        finally:
            _reporting.reset(token)

    async def _deliver(self, text):
        token = _reporting.set(True)
        try:
            for chat_id in dict.fromkeys((self.channel_id, self.owner_id)):
                if not chat_id:
                    continue
                try:
                    result = await asyncio.to_thread(self.api, "sendMessage", {
                        "chat_id": chat_id, "text": text, "disable_web_page_preview": True,
                    }, timeout=10)
                    if result.get("ok"):
                        return True
                except Exception:
                    pass
            logging.getLogger(__name__).error("Telegram diagnostic delivery failed; see local incident log")
            return False
        finally:
            _reporting.reset(token)

    async def _run(self):
        while True:
            text = await self.queue.get()
            try:
                await self._deliver(text)
            finally:
                self.queue.task_done()

    async def shutdown(self):
        if self.task:
            try:
                await asyncio.wait_for(self.queue.join(), timeout=25)
            except asyncio.TimeoutError:
                pass
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.loop = None
