"""StreamRecorder — orchestrates recording sessions via PyTgCalls.

This is the core module that bot.py interacts with. It manages:
- Session lifecycle (start, stop, recover)
- PyTgCalls client creation and event binding
- Rolling segment rotation (upload while recording continues)
- Watchdog: call rejoin, encoder health, progress stats
- File delivery coordination
"""

import asyncio
import glob
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls.types import GroupCallConfig, RecordStream
from pytgcalls.types.raw import AudioParameters

import config as _config
from .metadata import VideoSource
from .state_machine import StateMachine, RecorderState
from .source_manager import SourceManager
from .audio import AudioMixer
from .muxer import MuxerManager, VIDEO_CRF
from .uploader import Uploader
from .event_handler import EventHandler

from config import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    OUTPUT_DIR,
)

from database import db

logger = logging.getLogger(__name__)

SEGMENT_ROTATE_BYTES = int(getattr(_config, "SEGMENT_ROTATE_BYTES", 1_500_000_000))
SEGMENT_CHECK_INTERVAL = int(getattr(_config, "SEGMENT_CHECK_INTERVAL", 15))
WATCHDOG_INTERVAL = int(getattr(_config, "WATCHDOG_INTERVAL", 30))
REJOIN_COOLDOWN = int(getattr(_config, "REJOIN_COOLDOWN", 60))
FRAME_TIMEOUT_WARN = int(getattr(_config, "FRAME_TIMEOUT_WARN", 180))

CompletionCallback = Callable[["RecordingSession"], Awaitable[None]]
NotifyCallback = Callable[[int, str], Awaitable[None]]
PartCallback = Callable[["RecordingSession", str], Awaitable[None]]
RecoveryCallback = Callable[
    ["RecordingSession", Exception],
    Awaitable[Optional[Tuple[Client, Optional[int]]]],
]


class RecordingSession:
    """Holds all state for a single recording session."""

    def __init__(self, user_id: int, channel_id: int, channel_title: str):
        self.user_id = user_id
        self.channel_id = channel_id
        self.channel_title = channel_title
        self.recording_id = -1

        user_dir = os.path.abspath(os.path.join(OUTPUT_DIR, str(user_id)))
        os.makedirs(user_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        def _safe_name(value: str, fallback: str = "recording") -> str:
            value = re.sub(r"[^A-Za-z0-9 _-]+", "_", value or "")
            value = re.sub(r"\s+", " ", value).strip(" ._-")
            return (value[:48] or fallback).strip()

        stem = f"{_safe_name(channel_title)}_{stamp}"
        self.output_path = os.path.join(user_dir, f"{stem}.mp4")
        self.output_files: List[str] = []
        self.output_sizes: Dict[str, int] = {}
        self.uploaded_parts: set[str] = set()
        self.part_upload_tasks: Dict[str, asyncio.Task] = {}
        self.upload_lock = asyncio.Lock()
        self.manifest_path = os.path.join(user_dir, f"{stem}.recording.json")
        self.started_at = datetime.now()
        self.stopped_at: Optional[datetime] = None
        self.error: Optional[str] = None
        self.stop_reason = "manual"
        self.is_video = False
        self.status = "recording"

        # Sub-components
        self.state_machine = StateMachine()
        self.source_manager = SourceManager()
        self.audio_mixer = AudioMixer()
        self.muxer = MuxerManager(user_dir, stem)
        self.uploader = Uploader(self)

        # Synchronization (threading.Event: shared with mixer/writer threads)
        self.capture_started = threading.Event()
        self.stop_event = threading.Event()

        # Runtime
        self.active_video_source: Optional[VideoSource] = None
        self.client: Optional[Client] = None
        self.account_id: Optional[int] = None
        self.call_client: Optional[PyTgCalls] = None
        self.video_frames = 0
        self.last_video_frame_at = time.monotonic()
        self.reconnects = 0
        self.finalizing = False
        self._rotation_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._last_rejoin = 0.0

    def get_duration_str(self) -> str:
        end = self.stopped_at or datetime.now()
        total = max(0, int((end - self.started_at).total_seconds()))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}h {minutes}m {seconds}s"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    def stats_line(self) -> str:
        muxer = self.muxer
        source = self.active_video_source
        resolution = f"{source.width}x{source.height}" if source else "audio-only"
        duration = max(1, int((datetime.now() - self.started_at).total_seconds()))
        total_bytes = sum(self.output_sizes.values())
        bitrate_kbps = total_bytes * 8 // duration // 1000
        return (
            f"chat={self.channel_id} stream={self.recording_id} "
            f"duration={self.get_duration_str()} resolution={resolution} "
            f"video_codec=h264 audio_codec=aac fps={muxer.encoder_fps:.3g} "
            f"crf={VIDEO_CRF} bitrate={bitrate_kbps}kbps bytes={total_bytes} "
            f"era_bytes={muxer.current_era_bytes()} "
            f"video_frames={muxer.video_frames_written} "
            f"video_dropped={muxer.video_frames_dropped} "
            f"audio_frames={self.audio_mixer.audio_frames} "
            f"audio_dropped={self.audio_mixer.audio_dropped} "
            f"pipe_recoveries={self.audio_mixer.pipe_recoveries} "
            f"reconnects={self.reconnects} parts={len(self.output_files)}"
        )


class StreamRecorder:
    """Singleton recorder that manages all active recording sessions."""

    def __init__(self):
        self.active_sessions: Dict[int, RecordingSession] = {}
        # PyTgCalls cache keyed by id(client): a shared pool account client
        # is reused across users instead of double-wrapping.
        self.call_clients: Dict[int, PyTgCalls] = {}
        self.backend_name = "py-tgcalls/ntgcalls (modular)"
        self.captures_media = True
        self._notify_callback: Optional[NotifyCallback] = None
        self._completion_callback: Optional[CompletionCallback] = None
        self._part_callback: Optional[PartCallback] = None
        self._recovery_callback: Optional[RecoveryCallback] = None
        self._state_lock = asyncio.Lock()
        self._stop_tasks: Dict[int, asyncio.Task] = {}
        self._event_handler = EventHandler(self)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── Callback registration ────────────────────────────────────────────

    def set_notify_callback(self, callback: NotifyCallback) -> None:
        self._notify_callback = callback

    def set_completion_callback(self, callback: CompletionCallback) -> None:
        self._completion_callback = callback

    def set_part_callback(self, callback: PartCallback) -> None:
        """Called with (session, part_path) each time a rotated part is ready."""
        self._part_callback = callback

    def set_recovery_callback(self, callback: RecoveryCallback) -> None:
        self._recovery_callback = callback

    async def _notify(self, user_id: int, text: str) -> None:
        if not self._notify_callback:
            return
        try:
            await self._notify_callback(user_id, text)
        except Exception:
            logger.exception("Recording notification failed for user %s", user_id)

    # ── Session queries (called by bot.py) ───────────────────────────────

    def is_recording(self, user_id: int) -> bool:
        return user_id in self.active_sessions

    def get_session(self, user_id: int) -> Optional[RecordingSession]:
        return self.active_sessions.get(user_id)

    def mark_delivered(self, session: RecordingSession) -> None:
        """Mark a session as delivered and clean up temp files."""
        try:
            if os.path.exists(session.manifest_path):
                os.remove(session.manifest_path)
            user_dir = os.path.dirname(session.output_path)
            stem = session.muxer.stem
            keep = set(session.output_files)
            for temp_f in glob.glob(os.path.join(user_dir, f"{stem}*")):
                if temp_f in keep or temp_f.endswith(".mp4"):
                    continue
                try:
                    os.remove(temp_f)
                except OSError:
                    pass
        except Exception as exc:
            logger.warning("mark_delivered cleanup error: %s", exc)

    def update_output_files(
        self, session: RecordingSession, files: List[str]
    ) -> None:
        session.output_files = list(files)

    async def split_if_needed(
        self,
        file_path: str,
        max_bytes: int,
        delete_original: bool = True,
    ) -> List[str]:
        if not os.path.exists(file_path):
            return [file_path]
        if os.path.getsize(file_path) <= max_bytes:
            return [file_path]
        return await asyncio.to_thread(
            self._split_file_sync, file_path, max_bytes, delete_original
        )

    @staticmethod
    def _split_file_sync(
        file_path: str, max_bytes: int, delete_original: bool
    ) -> List[str]:
        import subprocess

        from ffmpeg_helper import get_ffmpeg_path, get_ffprobe_path

        ffmpeg = get_ffmpeg_path()
        base, ext = os.path.splitext(file_path)
        chunk_pattern = f"{base}.part%03d{ext}"

        file_size = os.path.getsize(file_path)
        try:
            probe_result = subprocess.run(
                [
                    get_ffprobe_path(),
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "csv=p=0",
                    file_path,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            duration = float(probe_result.stdout.strip())
            num_chunks = max(2, int(file_size / max_bytes) + 1)
            segment_time = int(duration / num_chunks) + 1
        except Exception:
            segment_time = 3600

        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        command = [
            ffmpeg, "-y", "-i", file_path,
            "-c", "copy", "-f", "segment",
            "-segment_time", str(segment_time),
            "-segment_format", ext.lstrip(".") or "mp4",
            "-reset_timestamps", "1",
            chunk_pattern,
        ]
        try:
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            parts = sorted(glob.glob(f"{base}.part*{ext}"))
            if parts and delete_original:
                try:
                    os.remove(file_path)
                except OSError:
                    pass
            return parts if parts else [file_path]
        except subprocess.CalledProcessError as e:
            logger.error("Splitting failed: %s", e)
            return [file_path]

    # ── Start recording ──────────────────────────────────────────────────

    @staticmethod
    def _build_record_stream(is_video: bool) -> RecordStream:
        """Capture decoded media without imposing a resolution/FPS ceiling.

        RecordStream's native (-1, -1, 0) video parameters accept the source
        format selected by ntgcalls. AudioParameters.bitrate is PCM sample
        rate, not the compressed Opus/AAC bitrate.
        """
        return RecordStream(
            audio=True,
            audio_parameters=AudioParameters(
                bitrate=AUDIO_SAMPLE_RATE,
                channels=AUDIO_CHANNELS,
            ),
            camera=is_video,
            screen=is_video,
        )

    async def start_recording(
        self,
        user_id: int,
        channel_id: int,
        channel_title: str,
        client: Client,
        is_video: bool = True,
        account_id: Optional[int] = None,
    ) -> RecordingSession:
        self._loop = asyncio.get_running_loop()
        async with self._state_lock:
            current = self.active_sessions.get(user_id)
            if current:
                return current

            session = RecordingSession(user_id, int(channel_id), channel_title)
            session.client = client
            session.account_id = account_id
            session.recording_id = await asyncio.to_thread(
                db.start_recording, user_id, int(channel_id), channel_title, is_video
            )
            self.active_sessions[user_id] = session
            session.uploader.write_manifest()

        session.muxer.set_part_callback(
            lambda path, _session=session: self._on_part_ready_sync(_session, path)
        )

        call_client: Optional[PyTgCalls] = None
        try:
            process = session.muxer.start_audio_encoder()
            session.audio_mixer.set_swap_factory(session.muxer.open_audio_process)
            session.audio_mixer.start_writer(
                process, session.capture_started, session.stop_event
            )

            call_client = await self._get_call_client(user_id, client)
            session.call_client = call_client

            session.capture_started.set()
            await call_client.record(
                int(channel_id),
                stream=self._build_record_stream(is_video),
                config=GroupCallConfig(auto_start=False),
            )
            # Stay in WAITING — transition to CAMERA/SCREEN_SHARE happens
            # in EventHandler when the first video frame arrives.
            logger.info(
                "Recording started: user=%s chat=%s stream=%s (%s) native_source=true",
                user_id, channel_id, session.recording_id, channel_title,
            )

            session._rotation_task = asyncio.create_task(
                self._rotation_loop(session)
            )
            session._watchdog_task = asyncio.create_task(
                self._watchdog_loop(session, is_video)
            )
            return session
        except Exception as exc:
            logger.exception("Could not start recording")
            session.state_machine.transition(RecorderState.ERROR)
            session.error = str(exc)
            session.status = "failed"
            session.stopped_at = datetime.now()
            session.stop_event.set()

            if call_client:
                await self._leave_call(call_client, session.channel_id)

            session.muxer.close_all()
            session.muxer.close_logs()
            async with self._state_lock:
                self.active_sessions.pop(user_id, None)

            await asyncio.to_thread(
                db.finish_recording,
                recording_id=session.recording_id,
                status="failed",
                error_message=str(exc),
            )
            session.uploader.write_manifest()
            raise

    def _on_part_ready_sync(self, session: RecordingSession, path: str) -> None:
        """Called from the muxer finalizer thread when a part mp4 is ready."""
        session.output_files.append(path)
        try:
            session.output_sizes[path] = os.path.getsize(path)
        except OSError:
            pass
        if self._part_callback and self._loop and not session.finalizing:
            asyncio.run_coroutine_threadsafe(
                self._track_part_upload(session, path), self._loop
            )

    async def _track_part_upload(
        self, session: RecordingSession, path: str
    ) -> None:
        if not self._part_callback or path in session.part_upload_tasks:
            return
        task = asyncio.create_task(self._part_callback(session, path))
        session.part_upload_tasks[path] = task
        try:
            await task
        finally:
            session.part_upload_tasks.pop(path, None)

    async def _get_call_client(
        self, user_id: int, client: Client
    ) -> PyTgCalls:
        key = id(client)
        existing = self.call_clients.get(key)
        if existing:
            return existing

        call_client = PyTgCalls(client)

        @call_client.on_update()
        async def recorder_update(_: PyTgCalls, update: Any) -> None:
            # route by chat: find the session that owns this channel
            chat_id = getattr(update, "chat_id", None)
            if chat_id is not None:
                for uid, sess in list(self.active_sessions.items()):
                    if sess.channel_id == int(chat_id) and sess.call_client is call_client:
                        await self._event_handler.handle_update(uid, update)
                        return
            # fallback: session that shares this client (single-session case)
            for uid, sess in list(self.active_sessions.items()):
                if sess.call_client is call_client:
                    await self._event_handler.handle_update(uid, update)
                    return

        await call_client.start()
        self.call_clients[key] = call_client
        return call_client

    # ── Rolling segment rotation ─────────────────────────────────────────

    async def _rotation_loop(self, session: RecordingSession) -> None:
        while not session.stop_event.is_set():
            await asyncio.sleep(SEGMENT_CHECK_INTERVAL)
            if session.finalizing:
                return
            try:
                if (
                    SEGMENT_ROTATE_BYTES
                    and session.muxer.current_era_bytes() >= SEGMENT_ROTATE_BYTES
                ):
                    await self._rotate_session(session)
            except Exception:
                logger.exception(
                    "Rotation check failed for user %s", session.user_id
                )

    async def _rotate_session(self, session: RecordingSession) -> None:
        logger.info(
            "Rotating segment for user %s (era reached %.2f GB)",
            session.user_id,
            session.muxer.current_era_bytes() / 1e9,
        )
        session.muxer.request_rotation()
        source = session.active_video_source
        if source is not None:
            generation = session.muxer.start_video_encoder(source=source)
            self._event_handler.set_generation(session, generation)
        session.audio_mixer.request_rotate()
        await self._notify(
            session.user_id,
            f"📦 Segment reached ~{SEGMENT_ROTATE_BYTES // 1_000_000} MB — "
            "it will be uploaded while recording continues.",
        )

    # ── Watchdog ─────────────────────────────────────────────────────────

    async def _watchdog_loop(self, session: RecordingSession, is_video: bool) -> None:
        last_stats = 0.0
        while not session.stop_event.is_set():
            await asyncio.sleep(WATCHDOG_INTERVAL)
            if session.finalizing:
                return
            try:
                # call membership: rejoin if Telegram dropped us
                call_client = session.call_client
                if call_client and not session.finalizing:
                    try:
                        calls = await call_client.calls
                        if session.channel_id not in calls:
                            await self._try_rejoin(session, is_video)
                    except Exception as exc:
                        logger.warning(
                            "Watchdog call check failed (user %s): %s",
                            session.user_id, exc,
                        )

                now = time.monotonic()
                if (
                    session.is_video
                    and now - session.last_video_frame_at >= FRAME_TIMEOUT_WARN
                ):
                    logger.warning(
                        "No video frames for %.0fs: user=%s chat=%s stream=%s",
                        now - session.last_video_frame_at,
                        session.user_id, session.channel_id, session.recording_id,
                    )

                # periodic structured stats
                if now - last_stats >= 60:
                    last_stats = now
                    logger.info("[stats] %s", session.stats_line())
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception(
                    "Watchdog error for user %s", session.user_id
                )

    async def _try_rejoin(self, session: RecordingSession, is_video: bool) -> None:
        now = time.monotonic()
        if now - session._last_rejoin < REJOIN_COOLDOWN:
            return
        session._last_rejoin = now
        session.reconnects += 1
        logger.warning(
            "Call lost for user %s in chat %s — rejoining (attempt #%s)",
            session.user_id, session.channel_id, session.reconnects,
        )
        last_error: Optional[Exception] = None
        for _ in range(3):
            try:
                if not session.call_client:
                    raise RuntimeError("Call client is unavailable")
                await session.call_client.record(
                    int(session.channel_id),
                    stream=self._build_record_stream(is_video),
                    config=GroupCallConfig(auto_start=False),
                )
                await self._notify(
                    session.user_id,
                    f"⚠️ Connection to the stream was lost — rejoined "
                    f"(attempt {session.reconnects}). Recording continued.",
                )
                return
            except Exception as exc:
                last_error = exc
                flood_like = any(
                    key in str(exc).lower()
                    for key in ("flood", "too many requests")
                )
                if not flood_like or not self._recovery_callback:
                    break
                replacement = await self._recovery_callback(session, exc)
                if not replacement:
                    break
                client, account_id = replacement
                session.client = client
                session.account_id = account_id
                session.call_client = await self._get_call_client(
                    session.user_id, client
                )
        logger.error(
            "Rejoin failed for user %s chat=%s: %s",
            session.user_id, session.channel_id, last_error,
        )

    # ── Stop recording ───────────────────────────────────────────────────

    async def stop_recording(
        self, user_id: int, reason: str = "manual"
    ) -> Optional[RecordingSession]:
        existing = self._stop_tasks.get(user_id)
        if existing:
            return await asyncio.shield(existing)

        session = self.active_sessions.get(user_id)
        if not session:
            return None

        task = asyncio.create_task(self._finalize_session(session, reason))
        self._stop_tasks[user_id] = task
        try:
            return await asyncio.shield(task)
        finally:
            self._stop_tasks.pop(user_id, None)

    async def _finalize_session(
        self, session: RecordingSession, reason: str
    ) -> RecordingSession:
        session.finalizing = True
        session.state_machine.transition(RecorderState.STOPPING)
        session.stop_reason = reason
        session.uploader.write_manifest()

        for task in (session._rotation_task, session._watchdog_task):
            if task and not task.done():
                task.cancel()

        call_client = session.call_client
        if call_client:
            await self._leave_call(call_client, session.channel_id)

        session.stop_event.set()

        # Wait for audio writer without blocking other recordings/uploads.
        await asyncio.to_thread(session.audio_mixer.join_writer)

        session.stopped_at = datetime.now()

        try:
            await asyncio.to_thread(session.uploader.finalize)
            if not session.output_files:
                raise RuntimeError("No decodable audio or video frames were received")
            session.state_machine.transition(RecorderState.STOPPED)
            session.status = "completed"

        except Exception as exc:
            logger.exception("Finalization failed for user %s", session.user_id)
            session.state_machine.transition(RecorderState.ERROR)
            session.error = str(exc)
            session.status = "failed"

        finally:
            logger.info("[final] %s | stop_reason=%s", session.stats_line(), reason)
            session.muxer.close_logs()
            session.uploader.write_manifest()

            size = sum(session.output_sizes.values())
            db_path = next(
                (f for f in session.output_files if os.path.exists(f)), ""
            )

            await asyncio.to_thread(
                db.finish_recording,
                recording_id=session.recording_id,
                file_path=db_path,
                file_size=size,
                status=session.status,
                error_message=session.error,
                is_video=session.is_video,
            )

            async with self._state_lock:
                self.active_sessions.pop(session.user_id, None)

        if self._completion_callback:
            try:
                await self._completion_callback(session)
            except Exception as e:
                logger.error("Error in completion callback: %s", e)

        return session

    async def _leave_call(
        self, call_client: PyTgCalls, channel_id: int
    ) -> None:
        try:
            calls = await call_client.calls
            if channel_id in calls:
                await call_client.leave_call(channel_id)
        except Exception as exc:
            logger.info("Could not leave call %s cleanly: %s", channel_id, exc)

    # ── Recovery & cleanup (called by bot.py at startup/shutdown) ─────────

    async def recover_incomplete(self) -> None:
        """Scan for orphaned .recording.json manifests and mark them failed."""
        try:
            for user_dir in glob.glob(os.path.join(OUTPUT_DIR, "*")):
                if not os.path.isdir(user_dir):
                    continue
                for manifest in glob.glob(
                    os.path.join(user_dir, "*.recording.json")
                ):
                    try:
                        with open(manifest, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        status = data.get("status", "")
                        if status in ("stopped", "error", "completed"):
                            os.remove(manifest)
                            continue
                        logger.info("Recovering orphaned recording: %s", manifest)
                        rec_id = data.get("recording_id")
                        if rec_id and rec_id > 0:
                            await asyncio.to_thread(
                                db.finish_recording,
                                recording_id=rec_id,
                                status="failed",
                                error_message="Bot restarted during recording",
                            )
                        os.remove(manifest)
                    except Exception as exc:
                        logger.warning(
                            "Could not recover manifest %s: %s", manifest, exc
                        )
        except Exception as exc:
            logger.warning("recover_incomplete error: %s", exc)

    async def cleanup_all(self) -> None:
        """Stop all active recording sessions (used during shutdown)."""
        user_ids = list(self.active_sessions.keys())
        for user_id in user_ids:
            try:
                await self.stop_recording(user_id, reason="bot shutdown")
            except Exception as exc:
                logger.warning(
                    "Could not stop recording for user %s during cleanup: %s",
                    user_id,
                    exc,
                )

    async def wait_for_deliveries(self, timeout: float = 30) -> None:
        tasks = list(self._stop_tasks.values())
        if not tasks:
            return
        try:
            await asyncio.wait(tasks, timeout=timeout)
        except Exception:
            pass
