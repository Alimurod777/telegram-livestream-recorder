"""MuxerManager — FFmpeg encoding and muxing for audio/video streams.

Key design decisions:
- All video segments use a fixed canvas (VIDEO_WIDTH × VIDEO_HEIGHT) so that
  ffmpeg concat works seamlessly across resolution changes.
- Each call to start_video_encoder() increments a generation counter;
  write_video_frame() only writes if the caller's generation matches the
  current one, preventing stale writes after a source switch.
"""

import os
import subprocess
import logging
import re
from typing import List, Optional

from ffmpeg_helper import get_ffmpeg_path, get_ffprobe_path
from config import (
    AUDIO_SAMPLE_RATE,
    AUDIO_BITRATE,
    VIDEO_FPS,
    VIDEO_BITRATE,
    VIDEO_WIDTH,
    VIDEO_HEIGHT,
)
from .metadata import VideoSource

logger = logging.getLogger(__name__)


def valid_media(path: str) -> bool:
    """Return True if *path* contains at least one decodable stream."""
    if not path or not os.path.exists(path):
        return False
    try:
        ffprobe = get_ffprobe_path()
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


class MuxerManager:
    def __init__(self, output_dir: str, stem: str):
        self.output_dir = output_dir
        self.stem = stem
        self.audio_temp_path = os.path.join(output_dir, f"{stem}.audio.mka")

        # Canvas dimensions — all video segments use these for concat compat
        self._canvas_w = VIDEO_WIDTH
        self._canvas_h = VIDEO_HEIGHT

        self.video_segments: List[str] = []
        self._current_video_process: Optional[subprocess.Popen] = None
        self._audio_process: Optional[subprocess.Popen] = None
        self._stderr_handles = []

        # Generation counter for write guards
        self._video_generation: int = 0

    # ── Internal helpers ──────────────────────────────────────────────────

    def _popen(self, command: List[str], log_path: str) -> tuple:
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

    @staticmethod
    def _double_bitrate(value: str) -> str:
        match = re.fullmatch(r"(\d+)([kKmM]?)", str(value).strip())
        if not match:
            return value
        return f"{int(match.group(1)) * 2}{match.group(2)}"

    # ── Audio ─────────────────────────────────────────────────────────────

    def start_audio_encoder(self) -> subprocess.Popen:
        ffmpeg = get_ffmpeg_path()
        command = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
            "-f", "s16le", "-ar", str(AUDIO_SAMPLE_RATE), "-ac", "1",
            "-i", "pipe:0", "-vn",
            "-af", "dynaudnorm=f=250:p=0.9",
            "-c:a", "aac", "-b:a", AUDIO_BITRATE,
            "-flush_packets", "1",
            "-f", "matroska", self.audio_temp_path,
        ]
        process, handle = self._popen(command, self.audio_temp_path + ".ffmpeg.log")
        self._audio_process = process
        self._stderr_handles.append(handle)
        return process

    # ── Video ─────────────────────────────────────────────────────────────

    def start_video_encoder(self, source: VideoSource = None,
                            width: int = 0, height: int = 0,
                            rotation: int = 0) -> int:
        """Start a new video encoder for the given source.

        Closes any previous encoder first. All output is scaled/padded to
        the fixed canvas size for seamless concatenation.

        Returns the generation number for use with write_video_frame().

        Accepts either a VideoSource object or explicit width/height/rotation
        for backward compatibility.
        """
        if self._current_video_process:
            self.close_video_encoder()

        self._video_generation += 1
        generation = self._video_generation

        segment_path = os.path.join(
            self.output_dir,
            f"{self.stem}.video.part{len(self.video_segments)}.mkv",
        )
        self.video_segments.append(segment_path)

        ffmpeg = get_ffmpeg_path()
        fps = VIDEO_FPS

        if source is not None:
            w = source.width or VIDEO_WIDTH
            h = source.height or VIDEO_HEIGHT
            rot = source.rotation
        else:
            w = width or VIDEO_WIDTH
            h = height or VIDEO_HEIGHT
            rot = rotation

        command = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
            "-f", "rawvideo", "-pixel_format", "yuv420p",
            "-video_size", f"{w}x{h}", "-framerate", str(fps),
            "-i", "pipe:0", "-an",
        ]

        # Build filter chain: rotation → canvas scaling/padding
        filters = []

        # Rotation filters
        rotation_map = {
            90: "transpose=clock",
            180: "hflip,vflip",
            270: "transpose=cclock",
        }
        if rot in rotation_map:
            filters.append(rotation_map[rot])

        # Canvas normalization: scale to fit within canvas, then pad to exact canvas size
        cw, ch = self._canvas_w, self._canvas_h
        # After rotation, effective dimensions swap for 90/270
        eff_w, eff_h = (h, w) if rot in (90, 270) else (w, h)

        if eff_w != cw or eff_h != ch:
            filters.append(
                f"scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
                f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black"
            )

        if filters:
            command += ["-vf", ",".join(filters)]

        command += [
            "-c:v", "libx264", "-preset", "superfast", "-crf", "20",
            "-maxrate", VIDEO_BITRATE,
            "-bufsize", self._double_bitrate(VIDEO_BITRATE),
            "-g", str(max(1, fps * 2)),
            "-keyint_min", str(max(1, fps * 2)),
            "-sc_threshold", "0", "-pix_fmt", "yuv420p",
            "-f", "matroska", segment_path,
        ]

        logger.info(
            "Starting video encoder gen=%d source=%sx%s canvas=%sx%s -> %s",
            generation, w, h, cw, ch, os.path.basename(segment_path),
        )

        process, handle = self._popen(command, segment_path + ".ffmpeg.log")
        self._current_video_process = process
        self._stderr_handles.append(handle)
        return generation

    def write_video_frame(self, generation: int, frame: bytes) -> bool:
        """Write a raw video frame if generation matches.

        Returns True if the frame was written, False if the generation
        is stale (a newer encoder has been started).
        """
        if generation != self._video_generation:
            return False
        process = self._current_video_process
        if not process or not process.stdin:
            return False
        try:
            process.stdin.write(frame)
            return True
        except (BrokenPipeError, OSError):
            return False

    def close_video_encoder(self):
        if self._current_video_process:
            try:
                if self._current_video_process.stdin:
                    self._current_video_process.stdin.close()
                self._current_video_process.wait(timeout=15)
            except Exception as e:
                logger.warning("Error closing video encoder: %s", e)
            self._current_video_process = None

    # ── Cleanup ───────────────────────────────────────────────────────────

    def close_all(self):
        self.close_video_encoder()
        if self._audio_process:
            try:
                if self._audio_process.stdin:
                    self._audio_process.stdin.close()
                self._audio_process.wait(timeout=15)
            except Exception as e:
                logger.warning("Error closing audio encoder: %s", e)
            self._audio_process = None

        for handle in self._stderr_handles:
            try:
                handle.close()
            except OSError:
                pass
        self._stderr_handles.clear()

    # ── Finalization ──────────────────────────────────────────────────────

    def finalize_media(self, final_output_path: str) -> str:
        """Close all encoders, merge segments + audio into a single MP4.

        Returns the path of the finalized file (same as final_output_path
        if successful, or the audio-only fallback).
        """
        self.close_all()
        return self._merge_to_mp4(final_output_path)

    def _merge_to_mp4(self, final_output_path: str) -> str:
        """Merge video segments and audio track into one MP4."""
        ffmpeg = get_ffmpeg_path()
        has_audio = os.path.exists(self.audio_temp_path) and os.path.getsize(self.audio_temp_path) > 0
        has_video = bool(self.video_segments) and any(
            os.path.exists(seg) and os.path.getsize(seg) > 0
            for seg in self.video_segments
        )

        if not has_audio and not has_video:
            logger.error("No audio or video data to merge")
            return ""

        if not has_video:
            # Audio-only output
            command = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
                "-i", self.audio_temp_path,
                "-c:a", "aac", "-movflags", "+faststart",
                final_output_path,
            ]
            self._run_ffmpeg(command, "audio-only merge")
            return final_output_path if os.path.exists(final_output_path) else ""

        # Filter valid video segments
        valid_segments = [
            seg for seg in self.video_segments
            if os.path.exists(seg) and os.path.getsize(seg) > 0
        ]

        if not valid_segments:
            if has_audio:
                command = [
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
                    "-i", self.audio_temp_path,
                    "-c:a", "aac", "-movflags", "+faststart",
                    final_output_path,
                ]
                self._run_ffmpeg(command, "audio-only merge (no valid video)")
                return final_output_path if os.path.exists(final_output_path) else ""
            return ""

        if len(valid_segments) == 1:
            inputs = ["-i", valid_segments[0]]
        else:
            list_path = os.path.join(self.output_dir, f"{self.stem}.concat.txt")
            with open(list_path, "w", encoding="utf-8") as f:
                for seg in valid_segments:
                    f.write(f"file '{os.path.abspath(seg)}'\n")
            inputs = ["-f", "concat", "-safe", "0", "-i", list_path]

        command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning"]
        command += inputs
        if has_audio:
            command += ["-i", self.audio_temp_path]
        # Stream copy video (all segments are same resolution thanks to canvas normalization)
        command += ["-c:v", "copy"]
        if has_audio:
            command += ["-c:a", "aac"]
        command += ["-movflags", "+faststart", final_output_path]

        self._run_ffmpeg(command, "final merge")
        return final_output_path if os.path.exists(final_output_path) else ""

    @staticmethod
    def _run_ffmpeg(command: List[str], description: str) -> bool:
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,
                creationflags=creationflags,
            )
            if result.returncode != 0:
                logger.error("FFmpeg %s failed (rc=%d): %s", description, result.returncode, result.stderr[-500:] if result.stderr else "")
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.error("FFmpeg %s timed out", description)
            return False
        except Exception as e:
            logger.error("FFmpeg %s error: %s", description, e)
            return False

    # ── Legacy compatibility ──────────────────────────────────────────────

    def merge_segments_to_mp4(self, final_output_path: str) -> bool:
        """Legacy method — delegates to finalize_media."""
        result = self._merge_to_mp4(final_output_path)
        return bool(result)
