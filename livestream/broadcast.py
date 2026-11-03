"""Work around PyTgCalls' malformed audio broadcast requests with Pyrofork.

InputGroupCallStream.video_channel and video_quality share TL flag 0. Both
must be omitted for audio; supplying quality alone produces invalid bytes.
The issue exists in PyTgCalls 2.2.8 and 3.0.0. Keep this shim on the bridge
instance rather than modifying installed packages.
"""

import asyncio
import logging
import time
from types import MethodType

from pyrogram import raw
from pyrogram.errors import BadRequest, FloodWait
from pytgcalls.mtproto.bridged_client import BridgedClient
from pytgcalls.mtproto.pyrogram_client import PyrogramClient

logger = logging.getLogger(__name__)


def install_broadcast_fix(call_client):
    # PyTgCalls.mtproto_client is the original Telegram client, not the
    # adapter used for broadcast downloads. MtProtoClient exposes the bridge.
    bridge = call_client._app.mtproto_client
    if not isinstance(bridge, PyrogramClient):
        return
    last_warning = {}

    async def download_stream(self, chat_id, timestamp, limit, video_channel, video_quality):
        call = await self._cache.get_full_chat(chat_id)
        if call is None:
            return None
        # These optional fields use the SAME TL flag. Audio has neither.
        quality = BridgedClient.parse_quality(video_quality) if video_channel is not None else None
        try:
            result = await self._invoke(
                raw.functions.upload.GetFile(
                    location=raw.types.InputGroupCallStream(
                        call=call, time_ms=timestamp, scale=0,
                        video_channel=video_channel, video_quality=quality,
                    ),
                    offset=0, limit=limit,
                ),
                chat_id=chat_id, sleep_threshold=0,
            )
            return result.bytes
        except FloodWait:
            await asyncio.sleep(0.1)
            return None
        except Exception as exc:
            # Pyrofork currently exposes this unknown RPC as generic BadRequest.
            # None maps to NOT_READY in PyTgCalls, preserving the pending chunk;
            # raising instead requests RESYNC_NEEDED and discards pending chunks.
            # https://core.telegram.org/api/group-calls#livestreams
            if isinstance(exc, BadRequest) and (
                getattr(exc, "ID", None) == "TIME_TOO_BIG"
                or getattr(exc, "value", None) == "[400 TIME_TOO_BIG]"
            ):
                await asyncio.sleep(0.1)
                return None
            # PyTgCalls catches this exception; surface it without log flooding.
            now = time.monotonic()
            if now - last_warning.get(chat_id, float("-inf")) >= 30:
                last_warning[chat_id] = now
                logger.warning("Broadcast download failed: chat=%s channel=%s: %s",
                               chat_id, video_channel, exc)
            raise

    bridge.download_stream = MethodType(download_stream, bridge)
