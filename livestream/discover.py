"""Discover — extract video source metadata from PyTgCalls StreamFrames."""

import time
from typing import List

from pytgcalls.types import StreamFrames, Device

from .metadata import SourceType, VideoSource


class Discover:
    @staticmethod
    def extract_sources(
        update: StreamFrames, now: float = None
    ) -> List[VideoSource]:
        """Extract VideoSource metadata from a StreamFrames update.

        Each frame in the update may have a unique SSRC (source ID).
        The Device type tells us whether it's Camera or Screen Share.

        Args:
            update: The StreamFrames update from PyTgCalls.
            now: Monotonic timestamp to set as last_seen. Defaults to
                 time.monotonic() if not provided.
        """
        sources = []
        if not getattr(update, "frames", None):
            return sources

        if now is None:
            now = time.monotonic()

        # Determine source type from device
        source_type = (
            SourceType.SCREEN_SHARE
            if update.device == Device.SCREEN
            else SourceType.CAMERA
        )

        seen_ssrcs = set()
        for frame in update.frames:
            ssrc = getattr(frame, "ssrc", None)
            info = getattr(frame, "info", None)
            if not ssrc or not info:
                continue
            # Avoid duplicates within the same update batch
            if ssrc in seen_ssrcs:
                continue
            seen_ssrcs.add(ssrc)

            w = info.width or 0
            h = info.height or 0
            rot = info.rotation or 0

            sources.append(
                VideoSource(
                    source_id=ssrc,
                    source_type=source_type,
                    width=w,
                    height=h,
                    rotation=int(rot),
                    last_seen=now,
                )
            )
        return sources
