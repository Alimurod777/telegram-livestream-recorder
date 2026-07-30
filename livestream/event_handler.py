"""Event handler for PyTgCalls updates.

Processes StreamFrames (audio + video) and ChatUpdate events.
Video frames are written directly in the callback — no separate writer
thread needed since PyTgCalls delivers frames at the native rate.
"""

import asyncio
import logging
import time

from pytgcalls.types import StreamFrames, ChatUpdate, Direction, Device

from .discover import Discover
from .metadata import SourceType
from .priority_manager import PriorityManager
from .state_machine import RecorderState
from config import VIDEO_SOURCE_STALE_SECONDS

logger = logging.getLogger(__name__)


class EventHandler:
    def __init__(self, recorder):
        self.recorder = recorder
        # Track current generation per session for write guards
        self._session_generation: dict = {}

    async def handle_update(self, user_id: int, update) -> None:
        session = self.recorder.active_sessions.get(user_id)
        if not session:
            return
        if getattr(update, "chat_id", None) != session.channel_id:
            return

        if isinstance(update, StreamFrames):
            if update.direction != Direction.INCOMING:
                return
            if update.device in (Device.MICROPHONE, Device.SPEAKER):
                payloads = [frame.frame for frame in update.frames if frame.frame]
                session.audio_mixer.handle_audio_frames(payloads)
            elif update.device in (Device.CAMERA, Device.SCREEN):
                self._handle_video_frames(session, update)
            return

        if isinstance(update, ChatUpdate):
            if int(update.status.value) & int(ChatUpdate.Status.LEFT_CALL.value):
                reason = getattr(update.status, "name", None) or str(update.status)
                logger.info("Call ended for user %s: %s", user_id, reason)
                asyncio.create_task(
                    self.recorder.stop_recording(user_id, reason=f"call ended: {reason}")
                )

    def _handle_video_frames(self, session, update: StreamFrames) -> None:
        """Process incoming video frames with source detection, priority, and writing."""
        now = time.monotonic()

        # 1. Discover sources from this update
        detected = Discover.extract_sources(update, now=now)
        if not detected:
            return

        session.is_video = True

        # 2. Update source manager with new/refreshed sources
        session.source_manager.update_sources(detected)

        # 3. Prune stale sources (handles disappearing Screen Share, etc.)
        pruned = session.source_manager.prune_stale(
            max_age=VIDEO_SOURCE_STALE_SECONDS, now=now
        )

        # 4. Check if the active source was pruned
        if pruned and session.active_video_source:
            for pruned_src in pruned:
                if pruned_src.source_id == session.active_video_source.source_id:
                    logger.info(
                        "Active source %s (%s) was pruned",
                        pruned_src.source_id,
                        pruned_src.source_type.name,
                    )
                    session.active_video_source = None
                    break

        # 5. Select best source by priority
        active_id = (
            session.active_video_source.source_id
            if session.active_video_source
            else None
        )
        all_sources = session.source_manager.get_sources()
        best_source = PriorityManager.get_active_source(all_sources, active_id)

        if not best_source:
            return

        # 6. Switch source if needed (different source, or resolution changed)
        needs_switch = (
            session.active_video_source is None
            or session.active_video_source.source_id != best_source.source_id
            or session.active_video_source.source_type != best_source.source_type
            or session.active_video_source.width != best_source.width
            or session.active_video_source.height != best_source.height
        )

        if needs_switch:
            old_type = (
                session.active_video_source.source_type.name
                if session.active_video_source
                else "none"
            )
            logger.info(
                "Switching video source: %s -> %s (ssrc=%s, %sx%s)",
                old_type,
                best_source.source_type.name,
                best_source.source_id,
                best_source.width,
                best_source.height,
            )
            session.active_video_source = best_source

            # Start new encoder and get generation
            generation = session.muxer.start_video_encoder(source=best_source)
            self._session_generation[id(session)] = generation

            # Transition state machine
            new_state = (
                RecorderState.SCREEN_SHARE
                if best_source.source_type == SourceType.SCREEN_SHARE
                else RecorderState.CAMERA
            )
            session.state_machine.transition(new_state)

            # Notify user about source switch (fire-and-forget)
            if self.recorder._notify_callback and old_type != "none":
                try:
                    asyncio.create_task(
                        self.recorder._notify(
                            session.user_id,
                            f"📹 Video source switched: {old_type} → {best_source.source_type.name} "
                            f"({best_source.width}×{best_source.height})",
                        )
                    )
                except Exception:
                    pass

        # 7. Write the best source's frame directly
        generation = self._session_generation.get(id(session))
        if generation is None:
            return

        frame = next(
            (f for f in update.frames if f.ssrc == best_source.source_id),
            None,
        )
        if frame and frame.frame:
            session.muxer.write_video_frame(generation, frame.frame)
