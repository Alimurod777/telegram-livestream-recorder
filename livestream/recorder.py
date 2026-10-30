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
import shutil
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from pyrogram import Client, raw
from pytgcalls import PyTgCalls
from pytgcalls.types import GroupCallConfig, RecordStream
from pytgcalls.types.raw import AudioParameters

import config as _config
from .metadata import VideoSource
from .state_machine import StateMachine, RecorderState
from .source_manager import SourceManager
from .audio import AudioMixer
from .muxer import MuxerManager, VIDEO_CRF, valid_media
from .uploader import Uploader
from .event_handler import EventHandler
from .broadcast import install_broadcast_fix

from config import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    OUTPUT_DIR,
)

from database import db

logger = logging.getLogger(__name__)

SEGMENT_ROTATE_BYTES = int(getattr(_config, "SEGMENT_ROTATE_BYTES", 1_970_000_000))
SEGMENT_CHECK_INTERVAL = max(0.25, float(getattr(_config, "SEGMENT_CHECK_INTERVAL", 1)))
MAX_RECORDING_SECONDS = min(14400, max(1, int(getattr(_config, "MAX_RECORDING_SECONDS", 14400))))
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
        self.telegram_call_id: Optional[int] = None

        user_dir = os.path.abspath(os.path.join(OUTPUT_DIR, str(user_id)))
        os.makedirs(user_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        def _safe_name(value: str, fallback: str = "recording") -> str:
            value = re.sub(r"[^A-Za-z0-9 _-]+", "_", value or "")
            value = re.sub(r"\s+", " ", value).strip(" ._-")
            return (value[:48] or fallback).strip()

        stem = f"{_safe_name(channel_title)}_{stamp}_{uuid.uuid4().hex[:8]}"
        self.output_path = os.path.join(user_dir, f"{stem}.mp4")
        self.output_files: List[str] = []
        self.archive_files: List[str] = []
        self.part_actions: Dict[str, str] = {}
        self.pending_actions: Dict[int, str] = {}
        self.delivery_sources: Dict[str, List[str]] = {}
        self.delivery_outputs: Dict[str, List[str]] = {}
        self.capacity_pending = False
        self.delivery_complete = False
        self.progress_message_id: Optional[int] = None
        self._duration_task: Optional[asyncio.Task] = None
        self._progress_task: Optional[asyncio.Task] = None
        self.output_sizes: Dict[str, int] = {}
        self.uploaded_parts: set[str] = set()
        self.part_upload_tasks: Dict[str, asyncio.Task] = {}
        self.upload_lock = asyncio.Lock()
        self.media_lock = asyncio.Lock()
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

    def pending_bytes(self) -> int:
        boundaries = [index for index, action in self.pending_actions.items() if action == "capacity"]
        boundaries += [int(re.search(r"\.part(\d+)\.", p)[1]) for p, action in self.part_actions.items()
                       if action == "capacity" and re.search(r"\.part(\d+)\.", p)]
        committed_boundary = max(boundaries, default=-1)
        paths = {p for p in self.archive_files if not re.search(r"\.part(\d+)\.", p)
                 or int(re.search(r"\.part(\d+)\.", p)[1]) > committed_boundary}
        for era in self.muxer.eras:
            if era.index > committed_boundary:
                paths.update(era.audio_parts + era.video_parts)
        total = 0
        for path in paths:
            try:
                total += os.path.getsize(path)
            except FileNotFoundError:
                pass  # Finalization or upload may have removed the file.
        return total

    def stats_line(self) -> str:
        muxer = self.muxer
        source = self.active_video_source
        resolution = f"{source.width}x{source.height}" if source else "audio-only"
        duration = max(1, int(((self.stopped_at or datetime.now()) - self.started_at).total_seconds()))
        total_bytes = sum(self.output_sizes.values()) + self.pending_bytes()
        bitrate_kbps = total_bytes * 8 // duration // 1000
        return (
            f"chat={self.channel_id} stream={self.telegram_call_id or self.recording_id} recording={self.recording_id} "
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
        self._call_locks: Dict[int, asyncio.Lock] = {}
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
            for path in getattr(session, "archive_files", []):
                if os.path.isfile(path):
                    os.remove(path)
            for temp_f in glob.glob(os.path.join(user_dir, f"{stem}*")):
                try:
                    if os.path.isdir(temp_f):
                        shutil.rmtree(temp_f)
                    else:
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
        split_dir = base + f".split-{max_bytes}"
        os.makedirs(split_dir, exist_ok=True)
        chunk_pattern = os.path.join(split_dir, f"part%05d{ext}")
        pattern = os.path.join(split_dir, f"part*{ext}")
        existing = sorted(glob.glob(pattern))
        if os.path.exists(os.path.join(split_dir, ".complete")) and existing:
            return existing

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
            segment_time = duration * max_bytes / file_size * 0.8
            if duration <= 0:
                raise ValueError("Invalid media duration")
        except Exception:
            raise RuntimeError(f"Cannot determine split duration for {file_path}")

        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        for _ in range(8):
            for stale in glob.glob(pattern):
                os.remove(stale)
            command = [
                ffmpeg, "-nostdin", "-y", "-v", "error", "-i", file_path,
                "-map", "0", "-c", "copy", "-f", "segment",
                "-segment_time", str(max(segment_time, 0.01)),
                "-segment_format", "mp4" if ext == ".m4a" else ext.lstrip("."),
                "-reset_timestamps", "1", chunk_pattern,
            ]
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=1800, creationflags=creationflags)
            parts = sorted(glob.glob(pattern))
            largest = max((os.path.getsize(p) for p in parts), default=0)
            if parts and 0 < largest <= max_bytes:
                with open(os.path.join(split_dir, ".complete"), "w") as handle:
                    handle.write("complete")
                if delete_original:
                    os.remove(file_path)
                return parts
            segment_time *= min(0.5, max_bytes / max(largest, 1) * 0.8)
        raise RuntimeError("Cannot split below the upload limit without re-encoding keyframes")

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
        telegram_call_id: Optional[int] = None,
    ) -> RecordingSession:
        self._loop = asyncio.get_running_loop()
        async with self._state_lock:
            current = self.active_sessions.get(user_id)
            if current:
                return current
            identity = getattr(getattr(client, "me", None), "id", None)
            def same_account(other) -> bool:
                return other is client or (identity is not None and identity == getattr(getattr(other, "me", None), "id", None))
            if any(same_account(s.client) and s.channel_id == int(channel_id)
                   for s in self.active_sessions.values()):
                raise RuntimeError("This account is already recording this chat")
            if sum(same_account(s.client) for s in self.active_sessions.values()) >= max(
                1, int(getattr(_config, "MAX_RECORDINGS_PER_ACCOUNT", 1))
            ):
                raise RuntimeError("Account recording capacity reached; use another account")

            session = RecordingSession(user_id, int(channel_id), channel_title)
            session.client = client
            session.account_id = account_id
            session.telegram_call_id = telegram_call_id
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
                "Capture joined; awaiting media: user=%s chat=%s stream=%s (%s) native_source=true",
                user_id, channel_id, session.recording_id, channel_title,
            )

            session._rotation_task = asyncio.create_task(
                self._rotation_loop(session)
            )
            session._watchdog_task = asyncio.create_task(
                self._watchdog_loop(session, is_video)
            )
            session._duration_task = asyncio.create_task(self._duration_limit_loop(session))
            return session
        except (Exception, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                logger.info("Recording start cancelled: user=%s chat=%s", user_id, channel_id)
            else:
                logger.exception("Could not start recording")
            session.state_machine.transition(RecorderState.ERROR)
            session.error = str(exc) or "Recording start cancelled"
            session.status = "failed"
            session.stopped_at = datetime.now()
            session.stop_event.set()

            if call_client:
                await self._leave_call(call_client, session.channel_id)

            await asyncio.to_thread(session.audio_mixer.join_writer)
            await asyncio.to_thread(session.muxer.close_all)
            session.muxer.close_logs()
            self._event_handler.forget(session)
            async with self._state_lock:
                self.active_sessions.pop(user_id, None)

            await asyncio.to_thread(
                db.finish_recording,
                recording_id=session.recording_id,
                status="failed",
                error_message=session.error,
            )
            session.uploader.write_manifest()
            raise

    def _on_part_ready_sync(self, session: RecordingSession, path: str) -> None:
        """Called from the muxer finalizer thread when a part mp4 is ready."""
        if path not in session.archive_files:
            session.archive_files.append(path)
        match = re.search(r"\.part(\d+)\.", path)
        action = session.pending_actions.pop(int(match[1]), None) if match else None
        if action:
            session.part_actions[path] = action
        if action and self._part_callback and self._loop and not session.finalizing:
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
        async with self._call_locks.setdefault(id(client), asyncio.Lock()):
            return await self._create_call_client(user_id, client)

    async def _create_call_client(
        self, user_id: int, client: Client
    ) -> PyTgCalls:
        key = id(client)
        existing = self.call_clients.get(key)
        if existing:
            return existing

        call_client = PyTgCalls(client)
        install_broadcast_fix(call_client)

        @call_client.on_update()
        async def recorder_update(_: PyTgCalls, update: Any) -> None:
            # route by chat: find the session that owns this channel
            chat_id = getattr(update, "chat_id", None)
            if chat_id is not None:
                for uid, sess in list(self.active_sessions.items()):
                    if sess.channel_id == int(chat_id) and sess.call_client is call_client:
                        await self._event_handler.handle_update(uid, update)
                        return
                return
            # fallback: session that shares this client (single-session case)
            owners = [uid for uid, sess in list(self.active_sessions.items())
                      if sess.call_client is call_client]
            if len(owners) == 1:
                await self._event_handler.handle_update(owners[0], update)

        await call_client.start()
        self.call_clients[key] = call_client
        return call_client

    # ── Rolling segment rotation ─────────────────────────────────────────

    async def _rotation_loop(self, session: RecordingSession) -> None:
        previous_bytes, previous_time = 0, time.monotonic()
        while not session.stop_event.is_set():
            await asyncio.sleep(SEGMENT_CHECK_INTERVAL)
            if session.finalizing:
                return
            try:
                free = await asyncio.to_thread(shutil.disk_usage, session.muxer.output_dir)
                minimum = int(getattr(_config, "MIN_FREE_DISK_BYTES", 2_000_000_000))
                if free.free < minimum:
                    await self._notify(session.user_id, "Recording stopped: insufficient disk space. Completed segments are retained for delivery.")
                    asyncio.create_task(self.stop_recording(session.user_id, reason="disk space limit"))
                    return
                size, now = session.pending_bytes(), time.monotonic()
                rate = max(0, size - previous_bytes) / max(now - previous_time, 0.01)
                previous_bytes, previous_time = size, now
                limit = int(getattr(_config, "EFFECTIVE_UPLOAD_LIMIT", 1_990_000_000))
                reserve = max(8_000_000, rate * SEGMENT_CHECK_INTERVAL * 3, size * 0.01)
                threshold = min(SEGMENT_ROTATE_BYTES, max(1, limit - reserve))
                if size >= threshold:
                    await self._rotate_session(session, action="capacity")
            except Exception:
                logger.exception(
                    "Rotation check failed for user %s", session.user_id
                )

    async def send_current_part(self, user_id: int) -> bool:
        """Deliver captured media through the rolling uploader and keep capturing."""
        session = self.get_session(user_id)
        if session is None:
            return False
        return await self._rotate_session(session, notify=False, minimum_bytes=1024, action="preview")

    async def _rotate_session(
        self, session: RecordingSession, notify: bool = True, minimum_bytes: int = 0,
        action: Optional[str] = None,
    ) -> bool:
        logger.info(
            "Rotating segment for user %s (era reached %.2f GB)",
            session.user_id,
            session.muxer.current_era_bytes() / 1e9,
        )
        async with session.media_lock:
            if session.finalizing or session.stop_event.is_set():
                return False
            if session.muxer.current_era_bytes() < minimum_bytes:
                return False
            if action:
                session.pending_actions[session.muxer.eras[-1].index] = action
                if action == "capacity":
                    session.capacity_pending = True
                session.uploader.write_manifest()
            session.muxer.request_rotation()
            source = session.active_video_source
            if source is not None:
                generation = session.muxer.start_video_encoder(
                    source=source, fps=session.muxer.encoder_fps
                )
                self._event_handler.set_generation(session, generation)
            session.audio_mixer.request_rotate()
        if notify:
            await self._notify(
                session.user_id,
                f"📦 Segment reached ~{session.pending_bytes() // 1_000_000} MB — "
                "it will be uploaded while recording continues.",
            )
        return True

    async def _duration_limit_loop(self, session: RecordingSession) -> None:
        await asyncio.sleep(MAX_RECORDING_SECONDS)
        if self.get_session(session.user_id) is session and not session.finalizing:
            asyncio.create_task(self.stop_recording(session.user_id, reason="4 hour recording limit"))

    # ── Watchdog ─────────────────────────────────────────────────────────

    async def _call_has_ended(self, session: RecordingSession) -> bool:
        """Read fresh Telegram state; the native calls cache can outlive a call."""
        client = session.client
        peer = await client.resolve_peer(session.channel_id)
        if isinstance(peer, raw.types.InputPeerChannel):
            full = await client.invoke(raw.functions.channels.GetFullChannel(
                channel=raw.types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)))
        elif isinstance(peer, raw.types.InputPeerChat):
            full = await client.invoke(raw.functions.messages.GetFullChat(chat_id=peer.chat_id))
        else:
            raise ValueError("Recording target must be a group or channel")
        call = full.full_chat.call
        if call is None:
            return True
        if session.telegram_call_id is not None and call.id != session.telegram_call_id:
            return True
        result = await client.invoke(raw.functions.phone.GetGroupCall(call=call, limit=1))
        return isinstance(result.call, raw.types.GroupCallDiscarded)

    async def _stop_if_call_ended(self, session: RecordingSession) -> bool:
        try:
            ended = await asyncio.wait_for(self._call_has_ended(session), timeout=15)
        except Exception as exc:
            logger.warning("Could not verify Telegram call state: user=%s error=%s", session.user_id, type(exc).__name__)
            return False
        if ended and not session.finalizing:
            logger.info("Telegram confirmed stream ended: user=%s chat=%s", session.user_id, session.channel_id)
            # A separate task survives cancellation of this watchdog during stop.
            asyncio.create_task(self.stop_recording(session.user_id, reason="Telegram stream ended"))
        return ended

    async def _watchdog_loop(self, session: RecordingSession, is_video: bool) -> None:
        last_stats = 0.0
        while not session.stop_event.is_set():
            await asyncio.sleep(WATCHDOG_INTERVAL)
            if session.finalizing:
                return
            try:
                if await self._stop_if_call_ended(session):
                    return
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
                auth_lost = any(key in str(exc).lower() for key in (
                    "auth_key_unregistered", "session_revoked", "session_expired", "user_deactivated"
                ))
                if not (flood_like or auth_lost) or not self._recovery_callback:
                    break
                replacement = await self._recovery_callback(session, exc)
                if not replacement:
                    break
                client, account_id = replacement
                old_call_client = session.call_client
                if old_call_client:
                    await self._leave_call(old_call_client, session.channel_id)
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

        for task in (session._rotation_task, session._watchdog_task, session._duration_task):
            if task and not task.done():
                task.cancel()

        call_client = session.call_client
        if call_client:
            await self._leave_call(call_client, session.channel_id)

        session.stop_event.set()

        session.stopped_at = datetime.now()

        try:
            await asyncio.to_thread(session.audio_mixer.join_writer)
            if session.video_frames == 0 and session.audio_mixer.audio_frames == 0:
                raise RuntimeError("Telegram efiridan audio/video kadrlar kelmadi; yozuv bo‘sh. Efir va ulanishni tekshiring.")
            await asyncio.to_thread(session.uploader.finalize)
            if not session.archive_files and not session.output_files:
                raise RuntimeError("No decodable audio or video frames were received")
            session.state_machine.transition(RecorderState.STOPPED)
            session.status = "completed"

        except Exception as exc:
            logger.exception("Finalization failed for user %s", session.user_id)
            try:
                await asyncio.to_thread(session.muxer.close_all)
            except Exception:
                logger.exception("Could not close failed encoders for user %s", session.user_id)
            session.state_machine.transition(RecorderState.ERROR)
            session.error = str(exc)
            session.status = "failed"

        finally:
            self._event_handler.forget(session)
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
                session.error = str(e)
                session.uploader.write_manifest()
                logger.error("Error in completion callback: %s", e)
                await self._notify(session.user_id, f"Yuborish tugamadi: {e}. Yozuv fayllari saqlab qolindi.")

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
        """Salvage media and retry undelivered files; retain failed manifests."""
        try:
            for user_dir in glob.glob(os.path.join(OUTPUT_DIR, "*")):
                if not os.path.isdir(user_dir):
                    continue
                for manifest in glob.glob(
                    os.path.join(user_dir, "*.recording.json")
                ):
                    try:
                        if any(os.path.abspath(s.manifest_path) == os.path.abspath(manifest)
                               for s in self.active_sessions.values()):
                            continue
                        with open(manifest, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        logger.info("Recovering orphaned recording: %s", manifest)
                        session = RecordingSession(int(data["user_id"]), int(data["channel_id"]), data["channel_title"])
                        session.recording_id = data.get("recording_id", -1)
                        session.telegram_call_id = data.get("telegram_call_id")
                        session.started_at = datetime.fromisoformat(data["started_at"])
                        session.stopped_at = datetime.fromisoformat(data["stopped_at"]) if data.get("stopped_at") else datetime.now()
                        session.manifest_path = os.path.abspath(manifest)
                        session.muxer = MuxerManager(os.path.abspath(user_dir), os.path.basename(manifest).removesuffix(".recording.json"))
                        session.output_path = os.path.join(session.muxer.output_dir, session.muxer.stem + ".mp4")
                        session.uploaded_parts = set(data.get("uploaded_parts", []))
                        session.delivery_outputs = data.get("delivery_outputs", {})
                        session.delivery_sources = data.get("delivery_sources", {})
                        session.part_actions = data.get("part_actions", {})
                        session.pending_actions = {int(k): v for k, v in data.get("pending_actions", {}).items()}
                        session.progress_message_id = data.get("progress_message_id")
                        parts = await asyncio.to_thread(session.muxer.recover_parts)
                        candidates = parts or [session.output_path, os.path.splitext(session.output_path)[0] + ".m4a"]
                        candidates += data.get("archive_files", [])
                        for path in dict.fromkeys(candidates):
                            if await asyncio.to_thread(valid_media, path):
                                session.archive_files.append(path)
                                match = re.search(r"\.part(\d+)\.", path)
                                action = session.pending_actions.pop(int(match[1]), None) if match else None
                                if action:
                                    session.part_actions[path] = action
                        session.output_files = [row["path"] for row in data.get("output_files", [])]
                        session.output_sizes = {row["path"]: row.get("size") or 0 for row in data.get("output_files", [])}
                        session.is_video = bool(data.get("is_video"))
                        if not session.archive_files and not session.output_files and not session.uploaded_parts:
                            logger.warning("No recoverable media; retained manifest %s", manifest)
                            continue
                        session.finalizing = True
                        remaining_raw = glob.glob(os.path.join(session.muxer.output_dir, f"{session.muxer.stem}.e*.mkv")) + glob.glob(os.path.join(session.muxer.output_dir, f"{session.muxer.stem}.e*.mka"))
                        session.status = "failed" if remaining_raw else "completed"
                        session.error = "Some encoder files could not be recovered" if remaining_raw else None
                        session.state_machine.transition(RecorderState.STOPPING)
                        session.state_machine.transition(RecorderState.STOPPED)
                        session.stop_reason = "recovered after restart; capture gap may exist"
                        await asyncio.to_thread(db.finish_recording,
                            recording_id=session.recording_id, status=session.status,
                            file_size=sum(session.output_sizes.values()),
                            error_message=session.stop_reason)
                        if self._completion_callback:
                            await self._completion_callback(session)
                    except Exception as exc:
                        logger.warning(
                            "Could not recover manifest %s: %s", manifest, exc
                        )
        except Exception as exc:
            logger.warning("recover_incomplete error: %s", exc)

    async def cleanup_all(self) -> None:
        """Stop all active recording sessions (used during shutdown)."""
        async def stop(user_id: int) -> None:
            try:
                await self.stop_recording(user_id, reason="bot shutdown")
            except Exception as exc:
                logger.warning(
                    "Could not stop recording for user %s during cleanup: %s",
                    user_id,
                    exc,
                )
        await asyncio.gather(*(stop(uid) for uid in list(self.active_sessions)))

    async def wait_for_deliveries(self, timeout: float = 30) -> None:
        tasks = list(self._stop_tasks.values())
        if not tasks:
            return
        try:
            await asyncio.wait(tasks, timeout=timeout)
        except Exception:
            pass
