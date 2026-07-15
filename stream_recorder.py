"""Receive Telegram group-call media and encode it with FFmpeg.

This module targets py-tgcalls 2.x/ntgcalls 2.x.  PyTgCalls joins the
group call and exposes decoded incoming PCM and I420 frames through
``StreamFrames`` updates.  Small callback handlers enqueue audio and retain
the latest video frame; dedicated writer threads feed those bytes to FFmpeg.

The capture files use Matroska while a recording is active so that an
uncleanly stopped process leaves recoverable media.  On a clean stop they are
muxed/remuxed to MP4 or M4A and split below Telegram's configured size limit.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import math
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import numpy as np
from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls.types import (
    ChatUpdate,
    Device,
    Direction,
    GroupCallConfig,
    RecordStream,
    StreamFrames,
)
from pytgcalls.types.raw import AudioParameters, VideoParameters

from config import (
    AUDIO_BITRATE,
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    OUTPUT_DIR,
    RECORDING_CHUNK_BYTES,
    VIDEO_BITRATE,
    VIDEO_FPS,
    VIDEO_HEIGHT,
    VIDEO_WIDTH,
)
from database import db
from ffmpeg_helper import get_ffmpeg_path, get_ffprobe_path

logger = logging.getLogger(__name__)

MAX_CHUNK_BYTES = RECORDING_CHUNK_BYTES
MIN_MEDIA_BYTES = 1024
FFMPEG_STOP_TIMEOUT = 30
MANIFEST_SUFFIX = ".recording.json"

CompletionCallback = Callable[["RecordingSession"], Awaitable[None]]
NotifyCallback = Callable[[int, str], Awaitable[None]]


def _safe_name(value: str, fallback: str = "recording") -> str:
    value = re.sub(r"[^A-Za-z0-9 _-]+", "_", value or "")
    value = re.sub(r"\s+", " ", value).strip(" ._-")
    return (value[:48] or fallback).strip()


def _valid_media(path: str) -> bool:
    try:
        return os.path.getsize(path) >= MIN_MEDIA_BYTES
    except OSError:
        return False


@dataclass
class RecordingSession:
    user_id: int
    channel_id: int
    channel_title: str
    recording_id: int = -1
    audio_temp_path: str = ""
    video_temp_path: str = ""
    output_path: str = ""
    output_files: List[str] = field(default_factory=list)
    manifest_path: str = ""
    started_at: datetime = field(default_factory=datetime.now)
    stopped_at: Optional[datetime] = None
    status: str = "starting"
    stop_reason: str = "manual"
    is_video: bool = False
    error: Optional[str] = None
    video_started_offset: float = 0.0

    _started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    _audio_process: Optional[subprocess.Popen] = field(default=None, repr=False)
    _video_process: Optional[subprocess.Popen] = field(default=None, repr=False)
    _audio_thread: Optional[threading.Thread] = field(default=None, repr=False)
    _video_thread: Optional[threading.Thread] = field(default=None, repr=False)
    _audio_queue: queue.Queue = field(
        default_factory=lambda: queue.Queue(maxsize=2000), repr=False
    )
    _capture_started: threading.Event = field(default_factory=threading.Event, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _video_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _latest_video_frame: Optional[bytes] = field(default=None, repr=False)
    _video_device: Optional[Device] = field(default=None, repr=False)
    _video_ssrc: Optional[int] = field(default=None, repr=False)
    _video_rotation: int = field(default=0, repr=False)
    _video_width: int = field(default=0, repr=False)
    _video_height: int = field(default=0, repr=False)
    _stderr_handles: List[Any] = field(default_factory=list, repr=False)
    _audio_frames: int = field(default=0, repr=False)
    _video_frames: int = field(default=0, repr=False)
    _audio_dropped: int = field(default=0, repr=False)
    _capture_errors: List[str] = field(default_factory=list, repr=False)
    _monitor_task: Optional[asyncio.Task] = field(default=None, repr=False)

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
    """Manage one recording per bot user."""

    def __init__(self) -> None:
        self.active_sessions: Dict[int, RecordingSession] = {}
        self.call_clients: Dict[int, PyTgCalls] = {}
        self.backend_name = "py-tgcalls/ntgcalls"
        self.captures_media = True
        self._notify_callback: Optional[NotifyCallback] = None
        self._completion_callback: Optional[CompletionCallback] = None
        self._state_lock = asyncio.Lock()
        self._stop_tasks: Dict[int, asyncio.Task] = {}
        self._completion_tasks: set[asyncio.Task] = set()

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

    async def start_recording(
        self,
        user_id: int,
        channel_id: int,
        channel_title: str,
        client: Client,
        is_video: bool = True,
    ) -> RecordingSession:
        """Join an existing group call and begin receiving its media."""
        async with self._state_lock:
            current = self.active_sessions.get(user_id)
            if current:
                return current

            user_dir = os.path.abspath(os.path.join(OUTPUT_DIR, str(user_id)))
            os.makedirs(user_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            stem = f"{_safe_name(channel_title)}_{stamp}"
            session = RecordingSession(
                user_id=user_id,
                channel_id=int(channel_id),
                channel_title=channel_title,
                audio_temp_path=os.path.join(user_dir, f"{stem}.audio.mka"),
                video_temp_path=os.path.join(user_dir, f"{stem}.video.mkv"),
                output_path=os.path.join(user_dir, f"{stem}.mp4"),
                manifest_path=os.path.join(user_dir, f"{stem}{MANIFEST_SUFFIX}"),
            )
            session.recording_id = db.start_recording(
                user_id, int(channel_id), channel_title, is_video
            )
            self.active_sessions[user_id] = session
            self._write_manifest(session)

        call_client: Optional[PyTgCalls] = None
        try:
            self._start_audio_encoder(session)
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

            session._started_monotonic = time.monotonic()
            session._capture_started.set()
            await call_client.record(
                int(channel_id),
                stream=record_stream,
                config=GroupCallConfig(auto_start=False),
            )
            session.status = "recording"
            session._monitor_task = asyncio.create_task(
                self._monitor_call(session, call_client)
            )
            self._write_manifest(session)
            logger.info(
                "Recording started: user=%s channel=%s title=%r",
                user_id,
                channel_id,
                channel_title,
            )
            return session
        except Exception as exc:
            logger.exception(
                "Could not start recording: user=%s channel=%s", user_id, channel_id
            )
            session.status = "error"
            session.error = str(exc)
            session.stopped_at = datetime.now()
            session._stop_event.set()
            await self._stop_capture_processes(session)
            if call_client:
                await self._leave_call(call_client, session.channel_id)
            async with self._state_lock:
                self.active_sessions.pop(user_id, None)
            self._finish_database(session, "failed")
            self._write_manifest(session)
            raise

    async def _get_call_client(self, user_id: int, client: Client) -> PyTgCalls:
        existing = self.call_clients.get(user_id)
        if existing and existing.mtproto_client is client:
            return existing

        call_client = PyTgCalls(client)

        @call_client.on_update()
        async def recorder_update(_: PyTgCalls, update: Any) -> None:
            await self._handle_call_update(user_id, update)

        await call_client.start()
        self.call_clients[user_id] = call_client
        return call_client

    async def _handle_call_update(self, user_id: int, update: Any) -> None:
        session = self.active_sessions.get(user_id)
        if not session or getattr(update, "chat_id", None) != session.channel_id:
            return

        if isinstance(update, StreamFrames):
            if update.direction != Direction.INCOMING:
                return
            if update.device in (Device.MICROPHONE, Device.SPEAKER):
                self._handle_audio_frames(session, update)
            elif update.device in (Device.CAMERA, Device.SCREEN):
                self._handle_video_frames(session, update)
            return

        if isinstance(update, ChatUpdate) and (
            int(update.status.value) & int(ChatUpdate.Status.LEFT_CALL.value)
        ):
            reason = getattr(update.status, "name", None) or str(update.status)
            logger.info(
                "Call ended for user %s in %s: %s",
                user_id,
                session.channel_id,
                reason,
            )
            await self._notify(
                user_id,
                f"The live stream ended ({reason}). Finalizing the recording...",
            )
            asyncio.create_task(
                self.stop_recording(user_id, reason=f"call ended: {reason}")
            )

    async def _monitor_call(
        self, session: RecordingSession, call_client: PyTgCalls
    ) -> None:
        failures = 0
        while session.status == "recording":
            await asyncio.sleep(10)
            try:
                calls = await call_client.calls
                failures = 0
                if (
                    session.status == "recording"
                    and session.channel_id not in calls
                ):
                    await self._notify(
                        session.user_id,
                        "The call connection was lost. Finalizing the recording...",
                    )
                    await self.stop_recording(
                        session.user_id, reason="call connection lost"
                    )
                    return
            except asyncio.CancelledError:
                return
            except Exception as exc:
                failures += 1
                logger.warning(
                    "Call watchdog failure %s for user %s: %s",
                    failures,
                    session.user_id,
                    exc,
                )
                if failures >= 3 and session.status == "recording":
                    await self._notify(
                        session.user_id,
                        "The call stopped responding. Finalizing the recording...",
                    )
                    await self.stop_recording(
                        session.user_id, reason="call watchdog timeout"
                    )
                    return

    def _handle_audio_frames(
        self, session: RecordingSession, update: StreamFrames
    ) -> None:
        payloads = [frame.frame for frame in update.frames if frame.frame]
        if not payloads or session.status not in ("starting", "recording"):
            return

        try:
            if len(payloads) == 1:
                mixed = payloads[0]
            else:
                arrays = [
                    np.frombuffer(data[: len(data) - len(data) % 2], dtype="<i2")
                    for data in payloads
                ]
                sample_count = max(len(samples) for samples in arrays)
                accumulator = np.zeros(sample_count, dtype=np.int32)
                for samples in arrays:
                    accumulator[: len(samples)] += samples.astype(np.int32)
                accumulator = accumulator / math.sqrt(len(arrays))
                mixed = np.clip(accumulator, -32768, 32767).astype("<i2").tobytes()

            bytes_per_tick = AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * 2 // 100
            if len(mixed) < bytes_per_tick:
                mixed = mixed.ljust(bytes_per_tick, b"\0")
            elif len(mixed) > bytes_per_tick:
                mixed = mixed[:bytes_per_tick]
            session._audio_queue.put_nowait(mixed)
            session._audio_frames += 1
        except queue.Full:
            session._audio_dropped += 1
            try:
                session._audio_queue.get_nowait()
                session._audio_queue.put_nowait(mixed)
            except (queue.Empty, queue.Full):
                pass
        except Exception as exc:
            message = f"audio callback: {exc}"
            if not session._capture_errors or session._capture_errors[-1] != message:
                session._capture_errors.append(message)
                logger.exception("Audio frame processing failed for %s", session.user_id)

    def _handle_video_frames(
        self, session: RecordingSession, update: StreamFrames
    ) -> None:
        if not update.frames or session.status not in ("starting", "recording"):
            return

        # A presentation is the primary program feed.  Until one appears,
        # keep one camera source stable instead of switching between speakers.
        if session._video_device == Device.SCREEN and update.device != Device.SCREEN:
            return
        if update.device == Device.SCREEN and session._video_device != Device.SCREEN:
            session._video_device = Device.SCREEN
            session._video_ssrc = None
        elif session._video_device is None:
            session._video_device = update.device
        elif update.device != session._video_device:
            return

        frame = next(
            (item for item in update.frames if item.ssrc == session._video_ssrc),
            update.frames[0],
        )

        if not frame.frame:
            return

        # Use actual frame dimensions from frame metadata.
        # ntgcalls delivers frames at the sender's native resolution;
        # we accept whatever arrives and let FFmpeg encode it.
        actual_w = frame.info.width or VIDEO_WIDTH
        actual_h = frame.info.height or VIDEO_HEIGHT
        expected_size = actual_w * actual_h * 3 // 2   # I420 / YUV420p

        if len(frame.frame) != expected_size:
            logger.debug(
                "Skipping video frame size mismatch for user %s: "
                "got=%s expected=%s (%sx%s)",
                session.user_id,
                len(frame.frame),
                expected_size,
                actual_w,
                actual_h,
            )
            return

        session._video_ssrc = frame.ssrc

        if session._video_process is None:
            # Lock in the first valid frame's dimensions for the whole session.
            session._video_width = actual_w
            session._video_height = actual_h
            session.video_started_offset = max(
                0.0, time.monotonic() - session._started_monotonic
            )
            session._video_rotation = int(frame.info.rotation or 0)
            self._start_video_encoder(session)
        else:
            # After encoder started, only accept frames of the locked size.
            if actual_w != session._video_width or actual_h != session._video_height:
                return

        with session._video_lock:
            session._latest_video_frame = frame.frame
        session._video_frames += 1
        session.is_video = True

    def _popen(
        self, command: List[str], log_path: str
    ) -> tuple[subprocess.Popen, Any]:
        log_handle = open(log_path, "ab", buffering=0)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=log_handle,
            bufsize=0,
            creationflags=creationflags,
        )
        return process, log_handle

    def _start_audio_encoder(self, session: RecordingSession) -> None:
        ffmpeg = get_ffmpeg_path()
        command = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "s16le",
            "-ar",
            str(AUDIO_SAMPLE_RATE),
            "-ac",
            str(AUDIO_CHANNELS),
            "-i",
            "pipe:0",
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            AUDIO_BITRATE,
            "-flush_packets",
            "1",
            "-f",
            "matroska",
            session.audio_temp_path,
        ]
        process, handle = self._popen(command, session.audio_temp_path + ".ffmpeg.log")
        session._audio_process = process
        session._stderr_handles.append(handle)
        session._audio_thread = threading.Thread(
            target=self._audio_writer,
            args=(session,),
            name=f"audio-writer-{session.user_id}",
            daemon=True,
        )
        session._audio_thread.start()

    def _start_video_encoder(self, session: RecordingSession) -> None:
        ffmpeg = get_ffmpeg_path()
        # Use the actual frame dimensions recorded when the first valid frame arrived.
        w = session._video_width or VIDEO_WIDTH
        h = session._video_height or VIDEO_HEIGHT
        fps = VIDEO_FPS
        command = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "rawvideo",
            "-pixel_format",
            "yuv420p",
            "-video_size",
            f"{w}x{h}",
            "-framerate",
            str(fps),
            "-i",
            "pipe:0",
            "-an",
        ]
        rotation_filters = {
            90: "transpose=clock",
            180: "hflip,vflip",
            270: "transpose=cclock",
        }
        if session._video_rotation in rotation_filters:
            command += ["-vf", rotation_filters[session._video_rotation]]
        command += [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-b:v",
            VIDEO_BITRATE,
            "-maxrate",
            VIDEO_BITRATE,
            "-bufsize",
            self._double_bitrate(VIDEO_BITRATE),
            "-g",
            str(max(1, fps * 2)),
            "-keyint_min",
            str(max(1, fps * 2)),
            "-sc_threshold",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-f",
            "matroska",
            session.video_temp_path,
        ]
        process, handle = self._popen(command, session.video_temp_path + ".ffmpeg.log")
        session._video_process = process
        session._stderr_handles.append(handle)
        session._video_thread = threading.Thread(
            target=self._video_writer,
            args=(session,),
            name=f"video-writer-{session.user_id}",
            daemon=True,
        )
        session._video_thread.start()
        self._write_manifest(session)

    @staticmethod
    def _double_bitrate(value: str) -> str:
        match = re.fullmatch(r"(\d+)([kKmM]?)", str(value).strip())
        if not match:
            return value
        return f"{int(match.group(1)) * 2}{match.group(2)}"

    @staticmethod
    def _audio_writer(session: RecordingSession) -> None:
        process = session._audio_process
        if not process or not process.stdin:
            return
        silence = b"\0" * (AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * 2 // 100)
        while not session._stop_event.is_set():
            if session._capture_started.wait(0.2):
                break
        if session._stop_event.is_set() and not session._capture_started.is_set():
            try:
                process.stdin.close()
            except OSError:
                pass
            return
        deadline = time.monotonic()
        try:
            while not session._stop_event.is_set() or not session._audio_queue.empty():
                try:
                    chunk = session._audio_queue.get(timeout=0.01)
                except queue.Empty:
                    chunk = silence
                process.stdin.write(chunk)
                deadline += 0.01
                delay = deadline - time.monotonic()
                if delay > 0:
                    session._stop_event.wait(delay)
                else:
                    deadline = time.monotonic()
        except (BrokenPipeError, OSError) as exc:
            session._capture_errors.append(f"audio FFmpeg pipe: {exc}")
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    @staticmethod
    def _video_writer(session: RecordingSession) -> None:
        process = session._video_process
        if not process or not process.stdin:
            return
        interval = 1.0 / max(1, VIDEO_FPS)
        deadline = time.monotonic()
        try:
            while not session._stop_event.is_set():
                with session._video_lock:
                    frame = session._latest_video_frame
                if frame:
                    process.stdin.write(frame)
                deadline += interval
                delay = deadline - time.monotonic()
                if delay > 0:
                    session._stop_event.wait(delay)
                else:
                    deadline = time.monotonic()
        except (BrokenPipeError, OSError) as exc:
            session._capture_errors.append(f"video FFmpeg pipe: {exc}")
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    async def stop_recording(
        self, user_id: int, reason: str = "manual"
    ) -> Optional[RecordingSession]:
        """Stop, finalize, split, and enqueue delivery of a recording."""
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
        session.status = "stopping"
        session.stop_reason = reason
        self._write_manifest(session)

        if (
            session._monitor_task
            and session._monitor_task is not asyncio.current_task()
        ):
            session._monitor_task.cancel()

        call_client = self.call_clients.get(session.user_id)
        if call_client:
            await self._leave_call(call_client, session.channel_id)

        session._stop_event.set()
        await self._stop_capture_processes(session)
        session.stopped_at = datetime.now()

        try:
            await self._finalize_media(session)
            if not session.output_files:
                raise RuntimeError("No decodable audio or video frames were received")
            session.status = "completed"
            self._finish_database(session, "completed")
        except Exception as exc:
            logger.exception("Finalization failed for user %s", session.user_id)
            session.status = "error"
            session.error = str(exc)
            self._finish_database(session, "failed")
        finally:
            self._write_manifest(session)
            async with self._state_lock:
                self.active_sessions.pop(session.user_id, None)

        if session.status == "completed":
            self._cleanup_temporary_files(session)
        self._schedule_completion(session)
        return session

    async def _leave_call(self, call_client: PyTgCalls, channel_id: int) -> None:
        try:
            calls = await call_client.calls
            if channel_id in calls:
                await call_client.leave_call(channel_id)
        except Exception as exc:
            logger.info("Could not leave call %s cleanly: %s", channel_id, exc)

    async def _stop_capture_processes(self, session: RecordingSession) -> None:
        threads = [session._audio_thread, session._video_thread]
        live_threads = [thread for thread in threads if thread and thread.is_alive()]
        if live_threads:
            await asyncio.gather(
                *(
                    asyncio.to_thread(thread.join, FFMPEG_STOP_TIMEOUT)
                    for thread in live_threads
                )
            )

        for process, thread in (
            (session._audio_process, session._audio_thread),
            (session._video_process, session._video_thread),
        ):
            if thread and thread.is_alive() and process and process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass

        remaining_threads = [
            thread for thread in threads if thread and thread.is_alive()
        ]
        if remaining_threads:
            await asyncio.gather(
                *(asyncio.to_thread(thread.join, 5) for thread in remaining_threads)
            )

        for process in (session._audio_process, session._video_process):
            if not process:
                continue
            await asyncio.to_thread(self._wait_process, process)

        for handle in session._stderr_handles:
            try:
                handle.close()
            except OSError:
                pass
        session._stderr_handles.clear()

    @staticmethod
    def _wait_process(process: subprocess.Popen) -> None:
        try:
            process.wait(timeout=FFMPEG_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    async def _finalize_media(self, session: RecordingSession) -> None:
        audio_ok = _valid_media(session.audio_temp_path)
        video_ok = _valid_media(session.video_temp_path)
        stem = os.path.splitext(session.output_path)[0]

        if video_ok:
            session.output_path = stem + ".mp4"
            if audio_ok:
                await self._mux_audio_video(session)
            else:
                await self._remux(
                    session.video_temp_path,
                    session.output_path,
                    ["-map", "0:v:0", "-c:v", "copy", "-an"],
                )
            session.is_video = True
        elif audio_ok:
            session.output_path = stem + ".m4a"
            await self._remux(
                session.audio_temp_path,
                session.output_path,
                ["-map", "0:a:0", "-c:a", "copy", "-vn"],
            )
            session.is_video = False
        else:
            session.output_path = ""
            session.output_files = []
            return

        if not _valid_media(session.output_path):
            raise RuntimeError("FFmpeg produced an empty output file")
        session.output_files = await self.split_if_needed(
            session.output_path,
            max_bytes=MAX_CHUNK_BYTES,
            delete_original=True,
        )
        if session.output_files:
            session.output_path = session.output_files[0]

    async def _mux_audio_video(self, session: RecordingSession) -> None:
        ffmpeg = get_ffmpeg_path()
        command = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-i",
            session.audio_temp_path,
        ]
        if session.video_started_offset > 0.05:
            command += ["-itsoffset", f"{session.video_started_offset:.3f}"]
        command += [
            "-i",
            session.video_temp_path,
            "-map",
            "1:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-shortest",
            "-movflags",
            "+faststart",
            session.output_path,
        ]
        await self._run_ffmpeg(command, timeout=1800)

    async def _remux(
        self, input_path: str, output_path: str, mapping: List[str]
    ) -> None:
        command = [
            get_ffmpeg_path(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-i",
            input_path,
            *mapping,
            "-movflags",
            "+faststart",
            output_path,
        ]
        await self._run_ffmpeg(command, timeout=1800)

    async def _run_ffmpeg(self, command: List[str], timeout: int) -> None:
        def run() -> subprocess.CompletedProcess:
            return subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )

        result = await asyncio.to_thread(run)
        if result.returncode != 0:
            error = (result.stderr or "unknown FFmpeg error").strip()
            raise RuntimeError(f"FFmpeg exited with {result.returncode}: {error[-2000:]}")

    async def split_if_needed(
        self,
        file_path: str,
        max_bytes: int = MAX_CHUNK_BYTES,
        delete_original: bool = False,
    ) -> List[str]:
        """Split media at keyframes, targeting 85% of ``max_bytes``."""
        file_path = os.path.abspath(file_path)
        if not _valid_media(file_path):
            return []
        if os.path.getsize(file_path) <= max_bytes:
            return [file_path]
        if max_bytes < 1_000_000:
            raise ValueError("max_bytes must be at least 1 MB")

        parts = await self._split_recursive(file_path, max_bytes, depth=0)
        if not parts or any(os.path.getsize(path) > max_bytes for path in parts):
            raise RuntimeError(f"Could not split {file_path} below {max_bytes} bytes")
        if delete_original and file_path not in parts:
            try:
                os.remove(file_path)
            except OSError:
                logger.warning("Could not remove split source %s", file_path)
        return parts

    async def _split_recursive(
        self, file_path: str, max_bytes: int, depth: int
    ) -> List[str]:
        if os.path.getsize(file_path) <= max_bytes:
            return [file_path]
        if depth >= 5:
            return [file_path]

        duration = await self._get_duration(file_path)
        if duration <= 0:
            raise RuntimeError(f"Could not determine duration of {file_path}")
        size = os.path.getsize(file_path)
        segment_time = max(1.0, duration * (max_bytes * 0.85) / size)
        parts = await self._split_once(file_path, segment_time, depth)
        if len(parts) <= 1:
            return [file_path]

        result: List[str] = []
        for part in parts:
            if os.path.getsize(part) > max_bytes:
                nested = await self._split_recursive(part, max_bytes, depth + 1)
                if nested != [part]:
                    try:
                        os.remove(part)
                    except OSError:
                        pass
                result.extend(nested)
            else:
                result.append(part)
        return result

    async def _split_once(
        self, file_path: str, segment_time: float, depth: int
    ) -> List[str]:
        path = Path(file_path)
        pattern = str(path.with_name(f"{path.stem}_part{depth}_%03d{path.suffix}"))
        glob_pattern = pattern.replace("%03d", "*")
        for old_part in glob.glob(glob_pattern):
            try:
                os.remove(old_part)
            except OSError:
                pass

        command = [
            get_ffmpeg_path(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-i",
            file_path,
            "-map",
            "0",
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            f"{segment_time:.3f}",
            "-reset_timestamps",
            "1",
            "-avoid_negative_ts",
            "make_zero",
        ]
        if path.suffix.lower() in (".mp4", ".m4a", ".mov"):
            command += ["-segment_format_options", "movflags=+faststart"]
        command.append(pattern)
        await self._run_ffmpeg(command, timeout=3600)
        return sorted(glob.glob(glob_pattern))

    async def _get_duration(self, file_path: str) -> float:
        command = [
            get_ffprobe_path(),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            file_path,
        ]

        def run() -> float:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            try:
                return float(result.stdout.strip()) if result.returncode == 0 else 0.0
            except ValueError:
                return 0.0

        return await asyncio.to_thread(run)

    def _write_manifest(self, session: RecordingSession) -> None:
        if not session.manifest_path:
            return
        payload = {
            "user_id": session.user_id,
            "channel_id": session.channel_id,
            "channel_title": session.channel_title,
            "recording_id": session.recording_id,
            "audio_temp_path": session.audio_temp_path,
            "video_temp_path": session.video_temp_path,
            "output_path": session.output_path,
            "output_files": session.output_files,
            "manifest_path": session.manifest_path,
            "started_at": session.started_at.isoformat(),
            "stopped_at": session.stopped_at.isoformat() if session.stopped_at else None,
            "status": session.status,
            "stop_reason": session.stop_reason,
            "is_video": session.is_video,
            "error": session.error,
            "video_started_offset": session.video_started_offset,
        }
        temp_path = session.manifest_path + ".tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, session.manifest_path)
        except OSError:
            logger.exception("Could not write recording manifest %s", session.manifest_path)

    async def recover_incomplete(self) -> List[RecordingSession]:
        """Finalize capture files left by an interrupted bot process."""
        pattern = os.path.join(os.path.abspath(OUTPUT_DIR), "**", f"*{MANIFEST_SUFFIX}")
        recovered: List[RecordingSession] = []
        for manifest_path in glob.glob(pattern, recursive=True):
            try:
                with open(manifest_path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                session = RecordingSession(
                    user_id=int(payload["user_id"]),
                    channel_id=int(payload["channel_id"]),
                    channel_title=payload.get("channel_title") or str(payload["channel_id"]),
                    recording_id=int(payload.get("recording_id", -1)),
                    audio_temp_path=payload.get("audio_temp_path", ""),
                    video_temp_path=payload.get("video_temp_path", ""),
                    output_path=payload.get("output_path", ""),
                    output_files=list(payload.get("output_files") or []),
                    manifest_path=manifest_path,
                    started_at=datetime.fromisoformat(payload["started_at"]),
                    status="recovering",
                    stop_reason="process restart",
                    video_started_offset=float(payload.get("video_started_offset", 0.0)),
                )
                completed_files = [
                    path for path in session.output_files if _valid_media(path)
                ]
                if payload.get("status") == "completed" and completed_files:
                    session.output_files = completed_files
                    session.output_path = completed_files[0]
                    session.status = "completed"
                    recovered.append(session)
                    self._schedule_completion(session)
                    continue
                await self._finalize_media(session)
                session.stopped_at = datetime.now()
                session.status = "completed" if session.output_files else "error"
                if not session.output_files:
                    session.error = "No recoverable media was found after restart"
                self._finish_database(
                    session, "completed" if session.output_files else "failed"
                )
                self._write_manifest(session)
                if session.status == "completed":
                    self._cleanup_temporary_files(session)
                recovered.append(session)
                self._schedule_completion(session)
            except Exception:
                logger.exception("Could not recover recording manifest %s", manifest_path)
        return recovered

    def mark_delivered(self, session: RecordingSession) -> None:
        """Mark a completed session delivered and remove its retry manifest."""
        session.status = "delivered"
        try:
            os.remove(session.manifest_path)
        except OSError:
            pass

    def update_output_files(
        self, session: RecordingSession, output_files: List[str]
    ) -> None:
        session.output_files = output_files
        session.output_path = output_files[0] if output_files else ""
        self._write_manifest(session)

    def _finish_database(self, session: RecordingSession, status: str) -> None:
        if session.recording_id < 0:
            return
        existing = [path for path in session.output_files if os.path.exists(path)]
        total_size = sum(os.path.getsize(path) for path in existing)
        db.finish_recording(
            session.recording_id,
            file_path=json.dumps(existing) if len(existing) > 1 else (existing[0] if existing else None),
            file_size=total_size or None,
            status=status,
            error_message=session.error,
            is_video=session.is_video,
        )

    @staticmethod
    def _cleanup_temporary_files(session: RecordingSession) -> None:
        output_set = {os.path.abspath(path) for path in session.output_files}
        for path in (
            session.audio_temp_path,
            session.video_temp_path,
            session.audio_temp_path + ".ffmpeg.log",
            session.video_temp_path + ".ffmpeg.log",
        ):
            if path and os.path.abspath(path) not in output_set:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _schedule_completion(self, session: RecordingSession) -> None:
        if not self._completion_callback:
            return

        async def complete() -> None:
            try:
                await self._completion_callback(session)
            except Exception:
                logger.exception("Completion callback failed for user %s", session.user_id)

        task = asyncio.create_task(complete())
        self._completion_tasks.add(task)
        task.add_done_callback(self._completion_tasks.discard)

    def get_session(self, user_id: int) -> Optional[RecordingSession]:
        return self.active_sessions.get(user_id)

    def has_active_session(self, user_id: int) -> bool:
        return user_id in self.active_sessions

    def is_recording(self, user_id: int) -> bool:
        return user_id in self.active_sessions

    async def cleanup_all(self) -> None:
        tasks = [
            self.stop_recording(user_id, reason="bot shutdown")
            for user_id in list(self.active_sessions)
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_for_deliveries(self, timeout: Optional[float] = None) -> None:
        if not self._completion_tasks:
            return
        waiting = asyncio.gather(*list(self._completion_tasks), return_exceptions=True)
        if timeout is None:
            await waiting
        else:
            try:
                await asyncio.wait_for(waiting, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("Timed out waiting for recording deliveries")


stream_recorder = StreamRecorder()
