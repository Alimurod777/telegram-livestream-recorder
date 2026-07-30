"""StreamRecorder — orchestrates recording sessions via PyTgCalls.

This is the core module that bot.py interacts with. It manages:
- Session lifecycle (start, stop, recover)
- PyTgCalls client creation and event binding
- File delivery coordination
"""

import asyncio
import glob
import json
import os
import subprocess
import threading
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional
import logging
import re

from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls.types import GroupCallConfig, RecordStream
from pytgcalls.types.raw import AudioParameters, VideoParameters

from .metadata import VideoSource, SourceType
from .state_machine import StateMachine, RecorderState
from .source_manager import SourceManager
from .audio import AudioMixer
from .muxer import MuxerManager, valid_media
from .uploader import Uploader
from .event_handler import EventHandler

from config import (
    OUTPUT_DIR,
    AUDIO_SAMPLE_RATE,
    AUDIO_CHANNELS,
    VIDEO_WIDTH,
    VIDEO_HEIGHT,
    VIDEO_FPS,
    RECORDING_CHUNK_BYTES,
)
from database import db
from ffmpeg_helper import get_ffmpeg_path

logger = logging.getLogger(__name__)

CompletionCallback = Callable[["RecordingSession"], Awaitable[None]]
NotifyCallback = Callable[[int, str], Awaitable[None]]


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

        # Synchronization
        self.capture_started = threading.Event()
        self.stop_event = threading.Event()

        # Active video source (set by EventHandler)
        self.active_video_source: Optional[VideoSource] = None

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


class StreamRecorder:
    """Singleton recorder that manages all active recording sessions."""

    def __init__(self):
        self.active_sessions: Dict[int, RecordingSession] = {}
        self.call_clients: Dict[int, PyTgCalls] = {}
        self.backend_name = "py-tgcalls/ntgcalls (modular)"
        self.captures_media = True
        self._notify_callback: Optional[NotifyCallback] = None
        self._completion_callback: Optional[CompletionCallback] = None
        self._state_lock = asyncio.Lock()
        self._stop_tasks: Dict[int, asyncio.Task] = {}
        self._event_handler = EventHandler(self)

    # ── Callback registration ────────────────────────────────────────────

    def set_notify_callback(self, callback: NotifyCallback) -> None:
        self._notify_callback = callback

    def set_completion_callback(self, callback: CompletionCallback) -> None:
        self._completion_callback = callback

    async def _notify(self, user_id: int, text: str) -> None:
        if not self._notify_callback:
            return
        try:
            await self._notify_callback(user_id, text)
        except Exception:
            logger.exception("Recording notification failed for user %s", user_id)

    # ── Session queries (called by bot.py) ───────────────────────────────

    def is_recording(self, user_id: int) -> bool:
        """Return True if the user has an active recording session."""
        return user_id in self.active_sessions

    def get_session(self, user_id: int) -> Optional[RecordingSession]:
        """Return the active recording session for a user, if any."""
        return self.active_sessions.get(user_id)

    def mark_delivered(self, session: RecordingSession) -> None:
        """Mark a session as delivered and clean up temp files."""
        try:
            # Remove manifest
            if os.path.exists(session.manifest_path):
                os.remove(session.manifest_path)
            # Clean up temp segment files
            user_dir = os.path.dirname(session.output_path)
            stem = session.muxer.stem
            for temp_f in glob.glob(os.path.join(user_dir, f"{stem}*")):
                if temp_f not in (session.output_files or []) and not temp_f.endswith(".mp4"):
                    try:
                        os.remove(temp_f)
                    except OSError:
                        pass
        except Exception as exc:
            logger.warning("mark_delivered cleanup error: %s", exc)

    def update_output_files(
        self, session: RecordingSession, files: List[str]
    ) -> None:
        """Update the session's output file list (after splitting)."""
        session.output_files = list(files)

    async def split_if_needed(
        self,
        file_path: str,
        max_bytes: int,
        delete_original: bool = True,
    ) -> List[str]:
        """Split a file into chunks if it exceeds max_bytes.

        Returns list of chunk file paths.
        """
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
        ffmpeg = get_ffmpeg_path()
        base, ext = os.path.splitext(file_path)
        chunk_pattern = f"{base}.part%03d{ext}"

        # Estimate segment time from file size and desired chunk size
        file_size = os.path.getsize(file_path)
        # Rough: get duration from ffprobe
        try:
            from ffmpeg_helper import get_ffprobe_path

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
            )
            duration = float(probe_result.stdout.strip())
            # Calculate segment time to produce chunks under max_bytes
            num_chunks = max(2, int(file_size / max_bytes) + 1)
            segment_time = int(duration / num_chunks) + 1
        except Exception:
            segment_time = 3600  # fallback: 1 hour segments

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

    async def start_recording(
        self,
        user_id: int,
        channel_id: int,
        channel_title: str,
        client: Client,
        is_video: bool = True,
    ) -> RecordingSession:
        async with self._state_lock:
            current = self.active_sessions.get(user_id)
            if current:
                return current

            session = RecordingSession(user_id, int(channel_id), channel_title)
            session.recording_id = await asyncio.to_thread(
                db.start_recording, user_id, int(channel_id), channel_title, is_video
            )
            self.active_sessions[user_id] = session
            session.uploader.write_manifest()

        call_client: Optional[PyTgCalls] = None
        try:
            process = session.muxer.start_audio_encoder()
            session.audio_mixer.start_writer(
                process, session.capture_started, session.stop_event
            )

            call_client = await self._get_call_client(user_id, client)

            record_stream = RecordStream(
                audio=True,
                audio_parameters=AudioParameters(
                    bitrate=AUDIO_SAMPLE_RATE,
                    channels=AUDIO_CHANNELS,
                ),
                camera=is_video,
                screen=is_video,
            )
            fixed_video = VideoParameters(
                width=VIDEO_WIDTH,
                height=VIDEO_HEIGHT,
                frame_rate=VIDEO_FPS,
                adjust_by_height=False,
            )
            if record_stream.camera:
                record_stream.camera.parameters = fixed_video
            if record_stream.screen:
                record_stream.screen.parameters = fixed_video

            session.capture_started.set()
            await call_client.record(
                int(channel_id),
                stream=record_stream,
                config=GroupCallConfig(auto_start=False),
            )
            # Stay in WAITING — transition to CAMERA/SCREEN_SHARE happens
            # in EventHandler when the first video frame arrives.
            # This correctly handles join-at-any-time.
            logger.info(
                "Recording started for user %s in channel %s (%s)",
                user_id,
                channel_id,
                channel_title,
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

    async def _get_call_client(
        self, user_id: int, client: Client
    ) -> PyTgCalls:
        existing = self.call_clients.get(user_id)
        if existing and existing.mtproto_client is client:
            return existing

        call_client = PyTgCalls(client)

        @call_client.on_update()
        async def recorder_update(_: PyTgCalls, update: Any) -> None:
            await self._event_handler.handle_update(user_id, update)

        await call_client.start()
        self.call_clients[user_id] = call_client
        return call_client

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
        session.state_machine.transition(RecorderState.STOPPING)
        session.stop_reason = reason
        session.uploader.write_manifest()

        call_client = self.call_clients.get(session.user_id)
        if call_client:
            await self._leave_call(call_client, session.channel_id)

        session.stop_event.set()

        # Wait for audio writer to finish
        session.audio_mixer.join_writer()

        session.stopped_at = datetime.now()

        try:
            await asyncio.to_thread(session.uploader.finalize)
            if not session.output_files:
                raise RuntimeError("No decodable audio or video frames were received")
            session.state_machine.transition(RecorderState.STOPPED)
            session.status = "completed"

            # Clean up temp segment files
            for temp_f in glob.glob(
                os.path.join(session.muxer.output_dir, f"{session.muxer.stem}*")
            ):
                if temp_f not in session.output_files and not temp_f.endswith(".json"):
                    try:
                        os.remove(temp_f)
                    except OSError:
                        pass

        except Exception as exc:
            logger.exception("Finalization failed for user %s", session.user_id)
            session.state_machine.transition(RecorderState.ERROR)
            session.error = str(exc)
            session.status = "failed"

        finally:
            session.uploader.write_manifest()

            size = (
                sum(
                    os.path.getsize(f)
                    for f in session.output_files
                    if os.path.exists(f)
                )
                if session.output_files
                else 0
            )
            db_path = session.output_files[0] if session.output_files else ""

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
                            # Already finalized — just remove the manifest
                            os.remove(manifest)
                            continue
                        logger.info(
                            "Recovering orphaned recording: %s", manifest
                        )
                        # Mark as failed in DB if we have a recording_id
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
        """Wait for any pending stop/finalization tasks to complete."""
        tasks = list(self._stop_tasks.values())
        if not tasks:
            return
        try:
            await asyncio.wait(tasks, timeout=timeout)
        except Exception:
            pass
