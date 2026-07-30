"""Uploader — finalize recordings and write session manifests."""

import glob
import os
import json
import logging
from datetime import datetime

from config import RECORDING_CHUNK_BYTES
from .muxer import valid_media

logger = logging.getLogger(__name__)


class Uploader:
    def __init__(self, session):
        self.session = session

    def finalize(self):
        """Finalize the media by merging segments and splitting if needed."""
        # Close all encoders and merge to final MP4
        output_path = self.session.muxer.finalize_media(self.session.output_path)

        if not output_path or not os.path.exists(output_path):
            logger.error(
                "Failed to finalize media for user %s", self.session.user_id
            )
            return

        # Validate the output
        if not valid_media(output_path):
            logger.error(
                "Finalized media is not valid: %s", output_path
            )
            return

        size = os.path.getsize(output_path)
        if size < 1024:
            logger.warning(
                "Finalized media too small (%d bytes): %s", size, output_path
            )
            return

        if size > RECORDING_CHUNK_BYTES:
            logger.info(
                "File exceeds chunk limit (%d > %d), splitting...",
                size,
                RECORDING_CHUNK_BYTES,
            )
            self._split_file(output_path)
        else:
            self.session.output_files.append(output_path)

    def _split_file(self, file_path):
        from ffmpeg_helper import get_ffmpeg_path
        import subprocess

        ffmpeg = get_ffmpeg_path()
        base, ext = os.path.splitext(file_path)
        chunk_pattern = f"{base}.part%03d{ext}"

        creationflags = (
            subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )
        command = [
            ffmpeg, "-y", "-i", file_path,
            "-c", "copy", "-f", "segment",
            "-segment_time", "3600",  # Split approx every hour
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
            self.session.output_files.extend(parts)
            try:
                os.remove(file_path)  # Remove original huge file
            except OSError:
                pass
        except subprocess.CalledProcessError as e:
            logger.error("Splitting failed: %s", e)
            self.session.output_files.append(file_path)

    def write_manifest(self):
        end = self.session.stopped_at or datetime.now()
        duration = int((end - self.session.started_at).total_seconds())
        data = {
            "version": 2,
            "user_id": self.session.user_id,
            "channel_id": self.session.channel_id,
            "channel_title": self.session.channel_title,
            "recording_id": getattr(self.session, "recording_id", -1),
            "status": self.session.state_machine.current_state.name.lower(),
            "started_at": self.session.started_at.isoformat(),
            "stopped_at": end.isoformat() if self.session.stopped_at else None,
            "duration_seconds": duration,
            "is_video": self.session.is_video,
            "stop_reason": self.session.stop_reason,
            "error": self.session.error,
            "active_source": (
                {
                    "type": self.session.active_video_source.source_type.name,
                    "ssrc": self.session.active_video_source.source_id,
                    "resolution": f"{self.session.active_video_source.width}x{self.session.active_video_source.height}",
                }
                if self.session.active_video_source
                else None
            ),
        }
        try:
            with open(self.session.manifest_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            logger.warning("Could not write manifest: %s", exc)
