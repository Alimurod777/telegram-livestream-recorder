"""Uploader — finalize recordings and write session manifests.

Rolling parts (partNNN.mp4) are produced by the MuxerManager finalizer
thread during recording and delivered via the recorder part callback.
This module only finalizes the LAST segment and handles the oversized-file
fallback split against the Bot API upload limit.
"""

import glob
import json
import logging
import os
import subprocess
from datetime import datetime

import config as _config
from config import RECORDING_CHUNK_BYTES
from .muxer import valid_media

logger = logging.getLogger(__name__)

# Split at the *effective* delivery limit: 2 GB via MTProto userbot upload,
# not the 50 MB public Bot API cap (that applies only to the fallback path).
UPLOAD_SPLIT_LIMIT = int(
    getattr(_config, "EFFECTIVE_UPLOAD_LIMIT", 0) or RECORDING_CHUNK_BYTES
)


class Uploader:
    def __init__(self, session):
        self.session = session

    def finalize(self):
        """Finalize the last media segment and register output files."""
        output_path = self.session.muxer.finalize_media(self.session.output_path)

        if not output_path or not os.path.exists(output_path):
            logger.error(
                "Failed to finalize media for user %s", self.session.user_id
            )
            return

        if not valid_media(output_path):
            logger.error("Finalized media is not valid: %s", output_path)
            return

        size = os.path.getsize(output_path)
        if size < 1024:
            logger.warning(
                "Finalized media too small (%d bytes): %s", size, output_path
            )
            return

        if not self.session.is_video and output_path.lower().endswith(".mp4"):
            audio_path = os.path.splitext(output_path)[0] + ".m4a"
            os.replace(output_path, audio_path)
            output_path = audio_path

        if output_path not in self.session.output_files:
            self.session.output_files.append(output_path)
        self.session.output_sizes[output_path] = size

        if size > UPLOAD_SPLIT_LIMIT:
            logger.info(
                "File exceeds chunk limit (%d > %d), splitting...",
                size,
                UPLOAD_SPLIT_LIMIT,
            )
            self._split_file(output_path)

    def _split_file(self, file_path: str):
        from ffmpeg_helper import get_ffmpeg_path

        ffmpeg = get_ffmpeg_path()
        base, ext = os.path.splitext(file_path)
        chunk_pattern = f"{base}.part%03d{ext}"

        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        command = [
            ffmpeg, "-y", "-i", file_path,
            "-c", "copy", "-f", "segment",
            "-segment_time", "3600",
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
            if parts:
                if file_path in self.session.output_files:
                    self.session.output_files.remove(file_path)
                self.session.output_files.extend(parts)
                self.session.output_sizes.pop(file_path, None)
                for part in parts:
                    self.session.output_sizes[part] = os.path.getsize(part)
                try:
                    os.remove(file_path)
                except OSError:
                    pass
        except subprocess.CalledProcessError as exc:
            logger.error("Splitting failed: %s", exc)

    def write_manifest(self):
        session = self.session
        end = session.stopped_at or datetime.now()
        duration = int((end - session.started_at).total_seconds())
        muxer = session.muxer
        data = {
            "version": 3,
            "user_id": session.user_id,
            "channel_id": session.channel_id,
            "channel_title": session.channel_title,
            "recording_id": getattr(session, "recording_id", -1),
            "status": session.state_machine.current_state.name.lower(),
            "started_at": session.started_at.isoformat(),
            "stopped_at": end.isoformat() if session.stopped_at else None,
            "duration_seconds": duration,
            "is_video": session.is_video,
            "output_files": [
                {"path": path, "size": session.output_sizes.get(path)}
                for path in session.output_files
            ],
            "stop_reason": session.stop_reason,
            "error": session.error,
            "active_source": (
                {
                    "type": session.active_video_source.source_type.name,
                    "ssrc": session.active_video_source.source_id,
                    "resolution": (
                        f"{session.active_video_source.width}"
                        f"x{session.active_video_source.height}"
                    ),
                }
                if session.active_video_source
                else None
            ),
            "stats": {
                "video_frames": muxer.video_frames_written,
                "video_dropped": muxer.video_frames_dropped,
                "audio_frames": session.audio_mixer.audio_frames,
                "audio_dropped": session.audio_mixer.audio_dropped,
                "pipe_recoveries": session.audio_mixer.pipe_recoveries,
                "reconnects": session.reconnects,
                "parts": len(session.output_files),
            },
        }
        try:
            with open(session.manifest_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            logger.warning("Could not write manifest: %s", exc)
