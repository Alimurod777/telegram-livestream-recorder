"""Event handler for PyTgCalls updates.

Processes StreamFrames (audio + video) and ChatUpdate events.
Video frames are enqueued to the MuxerManager writer thread (non-blocking),
so the asyncio event loop never stalls on pipe writes.

Also:
- measures the real incoming FPS and restarts the encoder when it drifts
  from the declared framerate (prevents fast/slow-motion output)
- restarts the encoder automatically if its pipe broke
"""

import asyncio
import logging
import time
from collections import deque

from pytgcalls.types import StreamFrames, ChatUpdate, Direction, Device

from .discover import Discover
from .metadata import SourceType
from .priority_manager import PriorityManager
from .state_machine import RecorderState
from config import VIDEO_SOURCE_STALE_SECONDS

logger = logging.getLogger(__name__)

_FPS_WINDOW = 90          # frames used for FPS estimation (~3 s at 30 fps)
_FPS_MIN_SAMPLES = 30     # frames before we trust the estimate
_FPS_DRIFT = 0.25         # relative drift that triggers an encoder restart
_RESTART_COOLDOWN = 20.0  # seconds between encoder restarts


class EventHandler:
    def __init__(self, recorder):
        self.recorder = recorder
        # Track current generation per session for write guards
        self._session_generation: dict = {}
        self._fps_windows: dict = {}
        self._last_restart: dict = {}

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
                await self._handle_video_frames(session, update)
            return

        if isinstance(update, ChatUpdate):
            status = int(update.status.value)
            final_status = int(
                ChatUpdate.Status.CLOSED_VOICE_CHAT.value
                | ChatUpdate.Status.DISCARDED_CALL.value
            )
            reason = getattr(update.status, "name", None) or str(update.status)
            if status & final_status:
                logger.info("Call ended for user %s: %s", user_id, reason)
                asyncio.create_task(
                    self.recorder.stop_recording(user_id, reason=f"call ended: {reason}")
                )
            elif status & int(ChatUpdate.Status.LEFT_CALL.value):
                # KICKED/LEFT_GROUP are not proof that the livestream ended.
                # The watchdog will attempt to rejoin instead of destroying media.
                logger.warning("Call membership lost for user %s: %s", user_id, reason)

    def set_generation(self, session, generation: int) -> None:
        """Update the write-guard generation after an external encoder
        restart (e.g. segment rotation by StreamRecorder)."""
        if generation is not None and generation >= 0:
            self._session_generation[id(session)] = generation

    # ── FPS estimation ────────────────────────────────────────────────────

    def _measured_fps(self, session) -> float:
        window = self._fps_windows.get(id(session))
        if not window or len(window) < _FPS_MIN_SAMPLES:
            return 0.0
        span = window[-1] - window[0]
        if span <= 0:
            return 0.0
        return (len(window) - 1) / span

    def _track_fps(
        self, session, now: float, capture_time_ms: int = 0
    ) -> float:
        window = self._fps_windows.setdefault(
            id(session), deque(maxlen=_FPS_WINDOW)
        )
        timestamp = capture_time_ms / 1000.0 if capture_time_ms > 0 else now
        if window and timestamp <= window[-1]:
            window.clear()
        window.append(timestamp)
        return self._measured_fps(session)

    def _needs_fps_restart(self, session, fps: float) -> bool:
        if not fps:
            return False
        declared = session.muxer.encoder_fps
        return abs(fps - declared) / max(declared, 1) > _FPS_DRIFT

    def _restart_encoder(self, session, source, fps: float, why: str) -> int:
        now = time.monotonic()
        if now - self._last_restart.get(id(session), 0) < _RESTART_COOLDOWN:
            return self._session_generation.get(id(session), -1)
        self._last_restart[id(session)] = now
        logger.info(
            "Restarting video encoder (%s): measured_fps=%.1f declared_fps=%d",
            why, fps, session.muxer.encoder_fps,
        )
        generation = session.muxer.start_video_encoder(source=source, fps=fps)
        self._session_generation[id(session)] = generation
        return generation

    # ── Video pipeline ────────────────────────────────────────────────────

    async def _handle_video_frames(self, session, update: StreamFrames) -> None:
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

        frame = next(
            (f for f in update.frames if f.ssrc == best_source.source_id),
            None,
        )
        if not frame or not frame.frame:
            return
        if needs_switch:
            self._fps_windows.pop(id(session), None)
        capture_time = int(getattr(frame.info, "capture_time", 0) or 0)
        fps = self._track_fps(session, now, capture_time)

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

            # Start new encoder at native resolution with measured fps
            generation = session.muxer.start_video_encoder(
                source=best_source, fps=fps or None
            )
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
        else:
            # 8. Self-healing: restart encoder on pipe break or FPS drift
            generation = self._session_generation.get(id(session))
            if generation is None or session.muxer.video_writer_dead:
                generation = self._restart_encoder(
                    session, best_source, fps, "pipe broken"
                )
            elif self._needs_fps_restart(session, fps):
                generation = self._restart_encoder(
                    session, best_source, fps, "fps drift"
                )
            self._session_generation[id(session)] = generation

        if generation is None or generation < 0:
            return

        # 9. Enqueue the selected frame without blocking the event loop when
        # the encoder is busy; bounded backpressure avoids silent frame loss.
        session.video_frames += 1
        if await session.muxer.write_video_frame_async(generation, frame.frame):
            session.last_video_frame_at = time.monotonic()
