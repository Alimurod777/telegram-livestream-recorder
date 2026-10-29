"""MTProto delivery via the pyrofork user session.

Telegram Bot API caps uploads at ~50 MB (2 GB with a self-hosted server),
but a user session uploads up to 2 GB per file (4 GB with Premium) —
and it's the same session that already records the stream. The file is
sent into the user<->bot chat so everything lands in one place.

Falls back to Bot API on failure (caller's responsibility).
"""

import logging
import os
import tempfile
from typing import Any, Dict, Optional
from pyrogram import enums
from .metadata import media_kind

logger = logging.getLogger(__name__)

_THUMB_BYTES = 100


class MtprotoUploader:
    def __init__(self):
        self._bot_peer: Dict[int, Any] = {}  # id(client) -> Chat

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
    ) -> bool:
        """Send one file as native media via MTProto.

        Raises on failure (incl. FloodWait) so the caller can fall back.
        """
        if not bot_username:
            raise RuntimeError("bot username unknown (not started?)")
        peer = await self._resolve_peer(client, bot_username)
        kind = media_kind(file_path, info)
        thumb_path = self._write_thumb(info.get("thumb"))
        title = os.path.splitext(os.path.basename(file_path))[0]
        duration = int(info.get("duration") or 0)
        progress = self._progress_logger(info)
        try:
            if kind == "video_note":
                result = await client.send_video_note(
                    peer.id, video_note=file_path, duration=duration,
                    length=int(info.get("width") or 1), thumb=thumb_path,
                    progress=progress,
                )
            elif kind == "video":
                result = await client.send_video(
                    peer.id,
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
                    peer.id,
                    voice=file_path,
                    caption=caption,
                    parse_mode=enums.ParseMode.DISABLED,
                    duration=duration,
                    progress=progress,
                )
            elif kind == "audio":
                result = await client.send_audio(
                    peer.id,
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
                    peer.id, document=file_path, caption=caption,
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
