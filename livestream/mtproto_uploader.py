"""MTProto delivery via the pyrofork user session.

Telegram Bot API caps uploads at ~50 MB (2 GB with a self-hosted server),
but a user session uploads up to 2 GB per file (4 GB with Premium) —
and it's the same session that already records the stream. The file is
sent into the user<->bot chat so everything lands in one place.

Falls back to Bot API on failure (caller's responsibility).
"""

import asyncio
import logging
import os
import tempfile
from typing import Any, Dict, Optional
from pyrogram import Client, enums
from ffmpeg_helper import extract_video_thumbnail
from .metadata import media_kind

logger = logging.getLogger(__name__)

_THUMB_BYTES = 100


class MtprotoUploader:
    def __init__(self):
        self._bot_peer: Dict[int, Any] = {}  # id(client) -> Chat
        self._delivery_bot = None
        self._delivery_bot_lock = asyncio.Lock()

    async def get_delivery_bot(self, api_id, api_hash, bot_token):
        async with self._delivery_bot_lock:
            if self._delivery_bot is None:
                client = Client("delivery_bot", api_id=api_id, api_hash=api_hash,
                                bot_token=bot_token, in_memory=True, no_updates=True)
                try:
                    await client.start()
                except BaseException:
                    if client.is_connected:
                        await client.disconnect()
                    raise
                self._delivery_bot = client
            return self._delivery_bot

    async def close(self):
        async with self._delivery_bot_lock:
            if self._delivery_bot is not None:
                await self._delivery_bot.stop()
                self._delivery_bot = None

    async def _resolve_peer(self, client, bot_username: str):
        key = id(client)
        if key not in self._bot_peer:
            self._bot_peer[key] = await client.get_chat(bot_username)
        return self._bot_peer[key]

    @staticmethod
    def _write_thumb(thumb: Optional[bytes]) -> Optional[str]:
        if not thumb or len(thumb) < _THUMB_BYTES:
            return None
        fd, path = tempfile.mkstemp(suffix=".jpg")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(thumb)
            return path
        except OSError:
            return None

    @staticmethod
    def _progress_logger(info: Dict[str, Any]):
        last_percent = -10

        def log_progress(current: int, total: int) -> None:
            nonlocal last_percent
            percent = current * 100 // max(total, 1)
            if percent < last_percent + 10 and current < total:
                return
            last_percent = percent
            logger.info(
                "MTProto upload chat=%s stream=%s: %d/%d MB (%d%%)",
                info.get("chat_id"), info.get("stream_id"),
                current // 1048576,
                max(total, 1) // 1048576,
                percent,
            )

        return log_progress

    async def send(
        self,
        client,
        bot_username: str,
        file_path: str,
        caption: str,
        info: Dict[str, Any],
        channel_title: str = "",
        recipient_id: Optional[int] = None,
    ) -> bool:
        """Send one file as native media via MTProto.

        Raises on failure (incl. FloodWait) so the caller can fall back.
        """
        if recipient_id is None and not bot_username:
            raise RuntimeError("bot username unknown (not started?)")
        peer = await client.resolve_peer(recipient_id) if recipient_id is not None else await self._resolve_peer(client, bot_username)
        target = recipient_id if recipient_id is not None else peer.id
        kind = media_kind(file_path, info)
        thumbnail = info.get("thumb")
        if kind == "video" and not thumbnail:
            thumbnail = await asyncio.to_thread(
                extract_video_thumbnail, file_path, min(5.0, max(0.0, float(info.get("duration") or 0) / 2)),
            )
        thumb_path = self._write_thumb(thumbnail)
        title = os.path.splitext(os.path.basename(file_path))[0]
        duration = int(info.get("duration") or 0)
        progress = self._progress_logger(info)
        try:
            if kind == "video_note":
                result = await client.send_video_note(
                    target, video_note=file_path, duration=duration,
                    length=int(info.get("width") or 1), thumb=thumb_path,
                    progress=progress,
                )
            elif kind == "video":
                result = await client.send_video(
                    target,
                    video=file_path,
                    caption=caption,
                    parse_mode=enums.ParseMode.DISABLED,
                    duration=duration,
                    width=int(info.get("width") or 0),
                    height=int(info.get("height") or 0),
                    thumb=thumb_path,
                    supports_streaming=True,
                    progress=progress,
                )
            elif kind == "voice":
                result = await client.send_voice(
                    target,
                    voice=file_path,
                    caption=caption,
                    parse_mode=enums.ParseMode.DISABLED,
                    duration=duration,
                    progress=progress,
                )
            elif kind == "audio":
                result = await client.send_audio(
                    target,
                    audio=file_path,
                    caption=caption,
                    parse_mode=enums.ParseMode.DISABLED,
                    duration=duration,
                    performer=info.get("performer") or channel_title or "Telegram Live",
                    title=info.get("title") or title,
                    thumb=thumb_path,
                    progress=progress,
                )
            else:
                result = await client.send_document(
                    target, document=file_path, caption=caption,
                    parse_mode=enums.ParseMode.DISABLED, progress=progress,
                )
            if result is None:
                raise RuntimeError("Telegram did not confirm media delivery")
            return True
        finally:
            if thumb_path:
                try:
                    os.remove(thumb_path)
                except OSError:
                    pass


mtproto_uploader = MtprotoUploader()
