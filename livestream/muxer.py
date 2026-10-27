"""MuxerManager — FFmpeg encoding and muxing for audio/video streams.

Quality-first design:
- Video is encoded at the NATIVE resolution of each source (no fixed canvas,
  no forced downscale) using x264 CRF without a hard bitrate ceiling.
- Audio keeps its channel count (stereo passthrough); loudness normalization
  is opt-in (AUDIO_NORMALIZE=1) because it alters original dynamics.

Rolling segments ("eras"):
- request_rotation() starts a new era: new audio + video encoder files while
  recording continues. Completed eras are merged in a background thread into
  partNNN.mp4 files and reported via the part callback, enabling upload while
  recording continues with bounded disk usage.

Thread safety:
- Video frames are enqueued through a bounded FIFO and written by a dedicated
  writer thread; the async fast path avoids blocking the event loop.
- Generation guards prevent stale writes across source switches / rotations.
  The writer keeps one process per generation and retires a process's stdin
  only after every queued frame of that generation has been written (queue is
  FIFO, generations are monotonic), so no frame is lost on switches.
"""

import asyncio
import glob
import json
import logging
import os
import queue
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional

from ffmpeg_helper import get_ffmpeg_path, get_ffprobe_path
from config import (
    AUDIO_BITRATE,
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    VIDEO_FPS,
)

import config as _config
from .metadata import VideoSource

logger = logging.getLogger(__name__)

VIDEO_PRESET = getattr(_config, "VIDEO_ENCODER_PRESET", "veryfast")
VIDEO_CRF = str(getattr(_config, "VIDEO_CRF", "18"))
NORMALIZE = bool(getattr(_config, "AUDIO_NORMALIZE", False))

_CREATIONFLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def valid_media(path: str) -> bool:
    """Return True if *path* contains at least one decodable stream."""
    if not path or not os.path.exists(path):
        return False
    try:
        result = subprocess.run(
            [get_ffprobe_path(), "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
            creationflags=_CREATIONFLAGS,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def probe_video(path: str) -> Optional[Dict[str, int]]:
    """Return {width, height, fps} for the first video stream, or None."""
    try:
        result = subprocess.run(
            [get_ffprobe_path(), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,avg_frame_rate",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30,
            creationflags=_CREATIONFLAGS,
        )
        streams = json.loads(result.stdout).get("streams") or []
        if not streams:
            return None
        stream = streams[0]
        num, _, den = (stream.get("avg_frame_rate") or "0/1").partition("/")
        try:
            fps = int(num) / int(den or 1) if int(den or 1) else 0.0
        except (ValueError, ZeroDivisionError):
            fps = 0
        return {"width": stream.get("width", 0), "height": stream.get("height", 0), "fps": fps}
    except Exception:
        return None


def _close_stdin(process: Optional[subprocess.Popen]) -> None:
    if process and process.stdin:
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass


def _reap(process: Optional[subprocess.Popen], timeout: float = 20.0) -> None:
    """Wait for an encoder to exit on its own before forcing stdin closed.

    The self-exit grace covers the rotation race where the finalizer thread
    wakes before the writer thread has drained the last queued frames.
    """
    if not process:
        return
    # Rotation finalizers run concurrently with the audio/video writers.
    # Give their FIFO queues enough time to drain before forcing EOF.
    deadline = time.monotonic() + 30.0
    while (
        time.monotonic() < deadline
        and process.poll() is None
        and process.stdin is not None
        and not process.stdin.closed
    ):
        time.sleep(0.05)
    if process.poll() is None:
        logger.warning("Encoder did not finish within 30s; closing stdin")
        _close_stdin(process)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception:
            pass
    except Exception:
        pass


class _Era:
    """One recording era = one set of encoder files between rotations."""

    def __init__(self, index: int):
        self.index = index
        self.audio_parts: List[str] = []
        self.video_parts: List[str] = []
        self.procs: List[subprocess.Popen] = []
        self.audio_closed = False
        self.video_closed = False
        self.scheduled = False

    @property
    def complete(self) -> bool:
        return self.audio_closed and self.video_closed


class MuxerManager:
    def __init__(self, output_dir: str, stem: str):
        self.output_dir = output_dir
        self.stem = stem

        self.eras: List[_Era] = [_Era(0)]
        self.video_segments: List[str] = []  # all video parts (legacy compat)

        self._current_audio_process: Optional[subprocess.Popen] = None
        self._audio_retries = 0
        self._stderr_handles = []
        self._video_generation = 0

        # video writer thread
        self._write_queue: "queue.Queue" = queue.Queue(
            maxsize=int(getattr(_config, "VIDEO_QUEUE_FRAMES", 60))
        )
        self._writer_thread: Optional[threading.Thread] = None
        self._async_write_lock = asyncio.Lock()
        self._process_by_gen: Dict[int, subprocess.Popen] = {}
        self.video_writer_dead = False
        self.video_frames_written = 0
        self.video_frames_dropped = 0
        self.encoder_fps = VIDEO_FPS

        # era finalization
        self._finalize_queue: "queue.Queue" = queue.Queue()
        self._finalizer_thread: Optional[threading.Thread] = None
        self._part_callback: Optional[Callable[[str], None]] = None
        self._lock = threading.RLock()
        self.closed = False
        self._finalizing = False

    # ── Callbacks ─────────────────────────────────────────────────────────

    def set_part_callback(self, callback: Callable[[str], None]) -> None:
        """Called from a background thread with each completed part mp4 path."""
        self._part_callback = callback

    # ── Internal helpers ──────────────────────────────────────────────────

    @property
    def _current_era(self) -> _Era:
        return self.eras[-1]

    def _popen(self, command: List[str], log_path: str) -> subprocess.Popen:
        log_handle = open(log_path, "ab", buffering=0)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=log_handle,
            bufsize=0,
            creationflags=_CREATIONFLAGS,
        )
        self._stderr_handles.append(log_handle)
        return process

    # ── Audio ─────────────────────────────────────────────────────────────

    def _era_audio_path(self, suffix: str = "") -> str:
        return os.path.join(
            self.output_dir,
            f"{self.stem}.e{self._current_era.index}.audio{suffix}.mka",
        )

    def start_audio_encoder(self) -> subprocess.Popen:
        path = self._era_audio_path()
        process = self._spawn_audio_ffmpeg(path)
        self._current_audio_process = process
        return process

    def open_audio_process(self) -> subprocess.Popen:
        """Swap-factory for AudioMixer: fresh process for the current era.

        Used both for era rotation and pipe-break recovery. Each call appends
        a new part file so no already-written data is overwritten.
        """
        with self._lock:
            self._audio_retries += 1
            path = self._era_audio_path(f".r{self._audio_retries}")
            process = self._spawn_audio_ffmpeg(path)
            self._current_audio_process = process
            return process

    def _spawn_audio_ffmpeg(self, path: str) -> subprocess.Popen:
        ffmpeg = get_ffmpeg_path()
        command = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
            "-f", "s16le", "-ar", str(AUDIO_SAMPLE_RATE),
            "-ac", str(AUDIO_CHANNELS),
            "-i", "pipe:0", "-vn",
        ]
        if NORMALIZE:
            command += ["-af", "dynaudnorm=f=250:p=0.9"]
        command += [
            "-c:a", "aac", "-b:a", AUDIO_BITRATE,
            "-flush_packets", "1",
            "-f", "matroska", path,
        ]
        process = self._popen(command, path + ".ffmpeg.log")
        with self._lock:
            self._current_era.audio_parts.append(path)
            self._current_era.procs.append(process)
        logger.info(
            "Audio encoder -> %s (channels=%s)", os.path.basename(path), AUDIO_CHANNELS
        )
        return process

    # ── Video ─────────────────────────────────────────────────────────────

    def start_video_encoder(
        self,
        source: Optional[VideoSource] = None,
        width: int = 0,
        height: int = 0,
        rotation: int = 0,
        fps: Optional[float] = None,
    ) -> int:
        """Start a new video encoder at the source's native resolution.

        Returns the generation number for use with write_video_frame().
        """
        with self._lock:
            self._video_generation += 1
            generation = self._video_generation

            if source is not None:
                w = source.width or 1280
                h = source.height or 720
                rot = source.rotation
                fps = fps or source.fps or VIDEO_FPS
            else:
                w = width or 1280
                h = height or 720
                rot = rotation
                fps = fps or VIDEO_FPS

            fps = max(5.0, min(float(fps), 60.0))
            self.encoder_fps = fps

            segment_path = os.path.join(
                self.output_dir,
                f"{self.stem}.e{self._current_era.index}"
                f".v{len(self._current_era.video_parts)}.mkv",
            )

            filters = []
            rotation_map = {
                90: "transpose=clock",
                180: "hflip,vflip",
                270: "transpose=cclock",
            }
            if rot in rotation_map:
                filters.append(rotation_map[rot])

            ffmpeg = get_ffmpeg_path()
            command = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
                "-f", "rawvideo", "-pixel_format", "yuv420p",
                "-video_size", f"{w}x{h}", "-framerate", str(fps),
                "-i", "pipe:0", "-an",
            ]
            if filters:
                command += ["-vf", ",".join(filters)]
            command += [
                "-c:v", "libx264", "-preset", VIDEO_PRESET,
                "-crf", VIDEO_CRF,
                "-g", str(max(1, round(fps * 2))),
                "-keyint_min", str(max(1, round(fps * 2))),
                "-sc_threshold", "0",
                "-profile:v", "high",
                "-pix_fmt", "yuv420p",
                "-f", "matroska", segment_path,
            ]

            logger.info(
                "Video encoder gen=%d native=%sx%s@%.3gfps crf=%s -> %s",
                generation, w, h, fps, VIDEO_CRF, os.path.basename(segment_path),
            )
            process = self._popen(command, segment_path + ".ffmpeg.log")
            self._process_by_gen[generation] = process
            self._current_era.video_parts.append(segment_path)
            self._current_era.procs.append(process)
            self.video_segments.append(segment_path)
            self.video_writer_dead = False
            self._ensure_writer()
            return generation

    def _ensure_writer(self) -> None:
        if not self._writer_thread or not self._writer_thread.is_alive():
            self._writer_thread = threading.Thread(
                target=self._writer_loop, daemon=True
            )
            self._writer_thread.start()

    def _writer_loop(self) -> None:
        """Write queued frames to the process matching their generation.

        FIFO queue + monotonic generations: when a frame with a newer
        generation arrives, every frame of the older generation has already
        been consumed, so the older process's stdin can be closed here.
        """
        while True:
            item = self._write_queue.get()
            if item is None:
                break
            generation, frame = item
            process = self._process_by_gen.get(generation)
            if process is None or process.stdin is None:
                self.video_frames_dropped += 1
                continue
            # newer generation observed -> older generations fully drained
            for gen in [g for g in self._process_by_gen if g < generation]:
                _close_stdin(self._process_by_gen.pop(gen))
            try:
                process.stdin.write(frame)
                self.video_frames_written += 1
            except (BrokenPipeError, OSError, ValueError):
                self.video_frames_dropped += 1
                self.video_writer_dead = True
                logger.error("Video encoder pipe broken (gen=%d)", generation)

        for process in self._process_by_gen.values():
            _close_stdin(process)

    def write_video_frame(self, generation: int, frame: bytes) -> bool:
        """Enqueue in order, rechecking the generation while applying pressure."""
        while True:
            with self._lock:
                if self.closed or generation != self._video_generation:
                    return False
                try:
                    self._write_queue.put_nowait((generation, frame))
                    return True
                except queue.Full:
                    pass
            time.sleep(0.005)

    async def write_video_frame_async(self, generation: int, frame: bytes) -> bool:
        """Serialize producers so backpressure cannot reorder adjacent frames."""
        async with self._async_write_lock:
            with self._lock:
                if self.closed or generation != self._video_generation:
                    return False
                try:
                    self._write_queue.put_nowait((generation, frame))
                    return True
                except queue.Full:
                    pass
            return await asyncio.to_thread(self.write_video_frame, generation, frame)

    def close_video_encoder(self) -> None:
        with self._lock:
            if self._writer_thread and self._writer_thread.is_alive():
                self._write_queue.put(None)
                self._writer_thread.join(timeout=15)
                self._writer_thread = None
            self._current_era.video_closed = True
            self._schedule_era_finalize(self._current_era)

    # ── Era rotation (rolling upload) ─────────────────────────────────────

    def current_era_bytes(self) -> int:
        era = self._current_era
        total = 0
        for path in era.audio_parts + era.video_parts:
            try:
                if os.path.exists(path):
                    total += os.path.getsize(path)
            except OSError:
                pass
        return total

    def request_rotation(self) -> None:
        """Close the current era and start a new one.

        The caller must restart the video encoder (start_video_encoder with
        the current source) right after; audio swaps at the next tick via
        AudioMixer.request_rotate().
        """
        with self._lock:
            old = self._current_era
            old.audio_closed = True  # mixer closes the process at swap
            old.video_closed = True  # retired by the writer on generation bump
            self.eras.append(_Era(old.index + 1))
            self._audio_retries = 0
            self._schedule_era_finalize(old)
            self._ensure_finalizer()
            logger.info("Rotating to era %d (previous era queued)", old.index)

    def mark_audio_era_closed(self) -> None:
        with self._lock:
            self._current_era.audio_closed = True
            self._schedule_era_finalize(self._current_era)

    def _schedule_era_finalize(self, era: _Era) -> None:
        if self._finalizing:
            return
        if era.complete and not era.scheduled:
            era.scheduled = True
            self._finalize_queue.put(era)
            self._ensure_finalizer()

    def _ensure_finalizer(self) -> None:
        if not self._finalizer_thread or not self._finalizer_thread.is_alive():
            self._finalizer_thread = threading.Thread(
                target=self._finalizer_loop, daemon=True
            )
            self._finalizer_thread.start()

    def _finalizer_loop(self) -> None:
        while True:
            era = self._finalize_queue.get()
            if era is None:
                break
            try:
                part = self._finalize_era_sync(era)
                if part and self._part_callback:
                    try:
                        self._part_callback(part)
                    except Exception:
                        logger.exception("Part callback failed for %s", part)
            except Exception:
                logger.exception("Era %d finalization failed", era.index)

    # ── Era merge ─────────────────────────────────────────────────────────

    def _finalize_era_sync(
        self, era: _Era, output_path: Optional[str] = None
    ) -> str:
        if output_path is None:
            output_path = os.path.join(
                self.output_dir, f"{self.stem}.part{era.index:03d}.mp4"
            )
        # ensure encoder processes have exited so files are finalized
        for process in era.procs:
            _reap(process)

        video_parts = [
            p for p in era.video_parts if os.path.exists(p) and os.path.getsize(p) > 0
        ]
        audio_parts = [
            p for p in era.audio_parts if os.path.exists(p) and os.path.getsize(p) > 0
        ]
        if not video_parts and not audio_parts:
            return ""

        merged = self._merge_av(
            video_parts, audio_parts, output_path, description=f"era{era.index}"
        )
        if merged:
            for path in video_parts + audio_parts:
                try:
                    os.remove(path)
                except OSError:
                    pass
        return merged

    def _concat_input(self, parts: List[str], tag: str) -> List[str]:
        list_path = os.path.join(self.output_dir, f"{self.stem}.{tag}.concat.txt")
        with open(list_path, "w", encoding="utf-8") as handle:
            for part in parts:
                handle.write(f"file '{os.path.abspath(part)}'\n")
        return ["-f", "concat", "-safe", "0", "-i", list_path]

    def _merge_av(
        self,
        video_parts: List[str],
        audio_parts: List[str],
        output_path: str,
        description: str = "merge",
    ) -> str:
        ffmpeg = get_ffmpeg_path()
        command: List[str] = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning"]

        video_codec = ["-vn"]
        if len(video_parts) == 1:
            command += ["-i", video_parts[0]]
            video_codec = ["-c:v", "copy"]
        elif video_parts:
            tag = f"v{description.replace(' ', '')}"
            command += self._concat_input(video_parts, tag)

            # Remux only when every part has identical resolution and fps.
            probes = [probe_video(p) for p in video_parts]
            uniform = (
                len(probes) == len(video_parts)
                and all(probes)
                and len({(p["width"], p["height"], p["fps"]) for p in probes}) == 1
            )
            if uniform:
                video_codec = ["-c:v", "copy"]
            else:
                target = next(
                    (p for p in reversed(probes) if p),
                    {"width": 1280, "height": 720},
                )
                w = target["width"] - target["width"] % 2
                h = target["height"] - target["height"] % 2
                video_codec = [
                    "-c:v", "libx264", "-preset", VIDEO_PRESET,
                    "-vf", (
                        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"
                    ),
                    "-crf", VIDEO_CRF,
                    "-pix_fmt", "yuv420p",
                ]
        if len(audio_parts) == 1:
            command += ["-i", audio_parts[0]]
        elif audio_parts:
            tag = f"a{description.replace(' ', '')}"
            command += self._concat_input(audio_parts, tag)

        command += video_codec
        if audio_parts:
            # Inputs are already AAC produced by this session; avoid a second
            # lossy encode during concatenation/remuxing.
            command += ["-c:a", "copy"]
        command += ["-movflags", "+faststart", output_path]

        if self._run_ffmpeg(command, description):
            return output_path if os.path.exists(output_path) else ""
        return ""

    # ── Cleanup / finalization ────────────────────────────────────────────

    def close_all(self) -> None:
        """Close encoders (writer thread drains first). Era files remain."""
        with self._lock:
            self.closed = True
        self.close_video_encoder()
        _close_stdin(self._current_audio_process)
        self._current_audio_process = None
        self.mark_audio_era_closed()

    def finalize_media(self, final_output_path: str) -> str:
        """Close everything and produce the final file.

        Rotated eras were already merged into partNNN.mp4 by the finalizer
        thread; the last era becomes final_output_path (single era) or one
        more partNNN.mp4 (multi-era). Returns the final path, or "".
        """
        self._finalizing = True
        self.close_all()
        era = self._current_era

        if era.index == 0 and not era.scheduled:
            return self._finalize_era_sync(era, final_output_path)

        # multi-era: last era merges as a part via the finalizer thread
        era.audio_closed = era.video_closed = True
        self._finalize_queue.put(era)
        self._ensure_finalizer()
        if self._finalizer_thread and self._finalizer_thread.is_alive():
            self._finalize_queue.put(None)
            self._finalizer_thread.join(timeout=1800)
        return self._last_completed_part()

    def _last_completed_part(self) -> str:
        parts = sorted(
            glob.glob(os.path.join(self.output_dir, f"{self.stem}.part*.mp4"))
        )
        return parts[-1] if parts else ""

    def close_logs(self) -> None:
        for handle in self._stderr_handles:
            try:
                handle.close()
            except OSError:
                pass
        self._stderr_handles.clear()

    @staticmethod
    def _run_ffmpeg(command: List[str], description: str) -> bool:
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=1800,
                creationflags=_CREATIONFLAGS,
            )
            if result.returncode != 0:
                logger.error(
                    "FFmpeg %s failed (rc=%d): %s",
                    description, result.returncode,
                    result.stderr[-500:] if result.stderr else "",
                )
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.error("FFmpeg %s timed out", description)
            return False
        except Exception as exc:
            logger.error("FFmpeg %s error: %s", description, exc)
            return False

    # ── Legacy compatibility ──────────────────────────────────────────────

    def merge_segments_to_mp4(self, final_output_path: str) -> bool:
        return bool(self.finalize_media(final_output_path))
