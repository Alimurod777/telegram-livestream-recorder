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
import re
from fractions import Fraction
from typing import Callable, Dict, List, Optional

from ffmpeg_helper import get_ffmpeg_path, get_ffprobe_path, write_all
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
        return result.returncode == 0 and bool(result.stdout.strip())
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
    if process.poll() is None and process.stdin is not None and not process.stdin.closed:
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
            maxsize=max(1, int(getattr(_config, "VIDEO_QUEUE_FRAMES", 12)))
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
        self.finalization_errors: List[int] = []

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
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=log_handle, bufsize=0, creationflags=_CREATIONFLAGS,
            )
        except BaseException:
            log_handle.close()
            raise
        process._recorder_log = log_handle
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

            fps = max(1.0, min(float(fps), 120.0))
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
            # Retire old encoders even if this source never delivers a frame.
            self._write_queue.put((generation, None))
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
            for gen, old_process in list(self._process_by_gen.items()):
                if gen < generation:
                    _close_stdin(old_process)
                    self._process_by_gen.pop(gen, None)
            if frame is None:
                continue
            try:
                write_all(process.stdin, frame)
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
        thread = self._writer_thread
        if thread and thread.is_alive():
            self._write_queue.put(None, timeout=30)
            thread.join(timeout=30)
            if thread.is_alive():
                raise TimeoutError("Video writer did not drain")
            self._writer_thread = None
        with self._lock:
            self._current_era.video_closed = True

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
                if not part and (era.audio_parts or era.video_parts):
                    self.finalization_errors.append(era.index)
                if part and self._part_callback:
                    try:
                        self._part_callback(part)
                    except Exception:
                        logger.exception("Part callback failed for %s", part)
            except Exception:
                self.finalization_errors.append(era.index)
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
        if not video_parts:
            output_path = os.path.splitext(output_path)[0] + ".m4a"

        merged = self._merge_av(
            video_parts, audio_parts, output_path, description=f"era{era.index}"
        )
        if merged and valid_media(merged):
            for path in video_parts + audio_parts:
                try:
                    os.remove(path)
                except OSError:
                    pass
            for process in era.procs:
                handle = getattr(process, "_recorder_log", None)
                if handle:
                    handle.close()
            era.procs.clear()
            self._stderr_handles[:] = [h for h in self._stderr_handles if not h.closed]
            return merged
        return ""

    def _concat_input(self, parts: List[str], tag: str) -> List[str]:
        list_path = os.path.join(self.output_dir, f"{self.stem}.{tag}.concat.txt")
        with open(list_path, "w", encoding="utf-8") as handle:
            for part in parts:
                escaped = os.path.abspath(part).replace("\\", "/").replace("'", "'\\''")
                handle.write(f"file '{escaped}'\n")
        return ["-f", "concat", "-safe", "0", "-i", list_path]

    def combine_parts(self, parts: List[str], output_path: str) -> str:
        """Join one delivery window. Compatible streams are copied unchanged."""
        if not parts:
            return ""
        metadata = []
        for path in parts:
            result = subprocess.run(
                [get_ffprobe_path(), "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                capture_output=True, text=True, check=True, timeout=30, creationflags=_CREATIONFLAGS,
            )
            metadata.append(json.loads(result.stdout))
        videos = [next((s for s in m["streams"] if s["codec_type"] == "video"), None) for m in metadata]
        if not any(videos):
            output_path = os.path.splitext(output_path)[0] + ".m4a"
        temporary = os.path.splitext(output_path)[0] + ".assembling" + os.path.splitext(output_path)[1]
        if len(parts) == 1:
            if os.path.abspath(parts[0]) != os.path.abspath(output_path):
                import shutil
                try:
                    os.link(parts[0], output_path)
                except OSError:
                    try:
                        shutil.copyfile(parts[0], temporary)
                        os.replace(temporary, output_path)
                    finally:
                        if os.path.exists(temporary):
                            os.remove(temporary)
            return output_path
        signatures = [tuple((s.get("codec_type"), s.get("codec_name"), s.get("width"), s.get("height"),
                             s.get("sample_rate"), s.get("channels"))
                            for s in m["streams"]) for m in metadata]
        normalized = []
        inputs = parts
        try:
            if any(videos) and len(set(signatures)) > 1:
                width = max(v["width"] for v in videos if v)
                height = max(v["height"] for v in videos if v)
                width += width % 2
                height += height % 2
                rates = []
                for video in videos:
                    if video:
                        try:
                            rates.append(float(Fraction(video.get("avg_frame_rate") or "0")))
                        except (ValueError, ZeroDivisionError):
                            pass
                fps = max(rates, default=0) or VIDEO_FPS
                for index, (path, data, video) in enumerate(zip(parts, metadata, videos)):
                    target = output_path + f".normalize{index}.mp4"
                    normalized.append(target)
                    command = [get_ffmpeg_path(), "-y", "-v", "warning", "-i", path]
                    has_audio = any(s["codec_type"] == "audio" for s in data["streams"])
                    video_input, audio_input = 0, 0
                    if video is None:
                        command += ["-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}"]
                        video_input = 1
                    if not has_audio:
                        audio_input = 2 if video is None else 1
                        command += ["-f", "lavfi", "-i", f"anullsrc=r={AUDIO_SAMPLE_RATE}:cl={'stereo' if AUDIO_CHANNELS == 2 else 'mono'}"]
                    duration = float(data["format"]["duration"])
                    command += ["-map", f"{video_input}:v:0", "-map", f"{audio_input}:a:0",
                                "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,tpad=stop_mode=clone:stop_duration={duration}",
                                "-t", str(duration), "-r", str(fps), "-c:v", "libx264", "-preset", VIDEO_PRESET,
                                "-crf", VIDEO_CRF, "-pix_fmt", "yuv420p", "-c:a", "copy" if has_audio else "aac", target]
                    if not self._run_ffmpeg(command, "normalize delivery part"):
                        raise RuntimeError("Cannot normalize changing stream formats")
                inputs = normalized
            command = [get_ffmpeg_path(), "-y", "-v", "warning"]
            command += self._concat_input(inputs, os.path.basename(output_path) + ".delivery")
            command += ["-map", "0", "-c", "copy", "-movflags", "+faststart", temporary]
            if not self._run_ffmpeg(command, "combine delivery") or not valid_media(temporary):
                raise RuntimeError("Could not combine recording parts")
            os.replace(temporary, output_path)
            return output_path
        finally:
            for path in normalized + [temporary]:
                if os.path.exists(path):
                    os.remove(path)

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

        # Queue each era once; close_all may have been called by a caller.
        if not era.scheduled:
            era.scheduled = True
            self._finalize_queue.put(era)
        self._ensure_finalizer()
        if self._finalizer_thread and self._finalizer_thread.is_alive():
            self._finalize_queue.put(None)
            self._finalizer_thread.join(timeout=1800)
            if self._finalizer_thread.is_alive():
                raise TimeoutError("Segment finalizer did not finish")
        if self.finalization_errors:
            raise RuntimeError(f"Segments failed to finalize: {self.finalization_errors}")
        return self._last_completed_part()

    def _last_completed_part(self) -> str:
        parts = sorted(
            glob.glob(os.path.join(self.output_dir, f"{self.stem}.part*.mp4"))
            + glob.glob(os.path.join(self.output_dir, f"{self.stem}.part*.m4a"))
        )
        return parts[-1] if parts else ""

    def recover_parts(self) -> List[str]:
        """Salvage closed/crashed Matroska files without touching live encoders."""
        eras = {}
        for path in glob.glob(os.path.join(self.output_dir, f"{self.stem}.e*")):
            match = re.fullmatch(re.escape(self.stem) + r"\.e(\d+)\.(audio.*\.mka|v\d+\.mkv)",
                                 os.path.basename(path))
            if not match:
                continue
            era = eras.setdefault(int(match[1]), _Era(int(match[1])))
            (era.audio_parts if match[2].startswith("audio") else era.video_parts).append(path)
        for era in sorted(eras.values(), key=lambda item: item.index):
            era.audio_parts.sort(key=lambda p: (".r" in p, int(re.search(r"\.r(\d+)", p)[1]) if re.search(r"\.r(\d+)", p) else 0))
            era.video_parts.sort(key=lambda p: int(re.search(r"\.v(\d+)\.mkv$", p)[1]))
            output = os.path.join(self.output_dir, f"{self.stem}.part{era.index:03d}.mp4")
            if not valid_media(output) and not valid_media(os.path.splitext(output)[0] + ".m4a"):
                self._finalize_era_sync(era, output)
        return sorted(glob.glob(os.path.join(self.output_dir, f"{self.stem}.part*.mp4"))
                      + glob.glob(os.path.join(self.output_dir, f"{self.stem}.part*.m4a")))

    def close_logs(self) -> None:
        for handle in self._stderr_handles:
            try:
                handle.close()
            except OSError:
                pass
        self._stderr_handles.clear()

    @staticmethod
    def _run_ffmpeg(command: List[str], description: str) -> bool:
        # File remuxes must not consume the bot's interactive terminal input.
        command = [command[0], "-nostdin", *command[1:]]
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=int(getattr(_config, "MERGE_TIMEOUT_SECONDS", 21600)),
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
