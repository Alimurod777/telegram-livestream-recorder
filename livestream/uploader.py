"""Uploader — finalize recordings and write session manifests.

Rolling parts (partNNN.mp4) are produced by the MuxerManager finalizer
thread during recording and delivered via the recorder part callback.
This module only finalizes the LAST segment and handles the oversized-file
fallback split against the Bot API upload limit.
"""

import json
import logging
import os
import threading
from datetime import datetime

from .muxer import valid_media
from .muxer import probe_video
from ffmpeg_helper import get_ffmpeg_path
import subprocess

logger = logging.getLogger(__name__)

class Uploader:
    def __init__(self, session):
        self.session = session
        self._manifest_lock = threading.Lock()

    def finalize(self):
        """Finalize the last media segment and register output files."""
        output_path = self.session.muxer.finalize_media(self.session.output_path)

        if not output_path or not os.path.exists(output_path):
            logger.error(
                "Failed to finalize media for user %s", self.session.user_id
            )
            raise RuntimeError("No final media segment could be produced")

        if not valid_media(output_path):
            logger.error("Finalized media is not valid: %s", output_path)
            raise RuntimeError("Final media segment is invalid")

        size = os.path.getsize(output_path)
        if size < 1024:
            logger.warning(
                "Finalized media too small (%d bytes): %s", size, output_path
            )
            raise RuntimeError("Final media segment contains no usable media")

        if not self.session.is_video and output_path.lower().endswith(".mp4"):
            audio_path = os.path.splitext(output_path)[0] + ".m4a"
            os.replace(output_path, audio_path)
            output_path = audio_path

        if output_path not in self.session.archive_files:
            self.session.archive_files.append(output_path)

        # Delivery uses the recorder's size-aware splitter for every part.

    def prepare_delivery(self, sources: list[str], tag: str) -> list[str]:
        """Create a video plus extracted audio, or one audio-only delivery."""
        session = self.session
        key = os.path.join(session.muxer.output_dir, session.muxer.stem + "." + tag)
        previous = session.delivery_outputs.get(key)
        if previous and all(p in session.uploaded_parts or valid_media(p) for p in previous):
            return previous
        session.delivery_sources[key] = list(sources)
        existing = key + ".mp4" if valid_media(key + ".mp4") else key + ".m4a"
        output = existing if valid_media(existing) else session.muxer.combine_parts(sources, key + ".mp4")
        if not output or not valid_media(output):
            raise RuntimeError("No complete recording is available for delivery")
        outputs = [output]
        if probe_video(output):
            audio = key + ".audio.m4a"
            if not valid_media(audio):
                temporary = key + ".audio.tmp.m4a"
                try:
                    subprocess.run([get_ffmpeg_path(), "-y", "-v", "error", "-i", output,
                                    "-map", "0:a:0", "-vn", "-c:a", "copy", "-movflags", "+faststart", temporary],
                                   check=True, capture_output=True, timeout=1800)
                    if not valid_media(temporary):
                        raise RuntimeError("Extracted audio is invalid")
                    os.replace(temporary, audio)
                finally:
                    if os.path.exists(temporary):
                        os.remove(temporary)
            outputs.append(audio)
        session.delivery_outputs[key] = outputs
        if not tag.startswith("preview"):
            for path in outputs:
                if path not in session.output_files:
                    session.output_files.append(path)
                session.output_sizes[path] = os.path.getsize(path) if os.path.exists(path) else session.output_sizes.get(path, 0)
        self.write_manifest()
        return outputs

    def retire_sources(self, tag: str) -> None:
        session = self.session
        key = os.path.join(session.muxer.output_dir, session.muxer.stem + "." + tag)
        outputs = session.delivery_outputs.get(key, [])
        if not outputs or not all(path in session.uploaded_parts for path in outputs):
            raise RuntimeError("Cannot retire recording before all media uploads succeed")
        for path in session.delivery_sources.get(key, []):
            if os.path.exists(path):
                os.remove(path)
            if path in session.archive_files:
                session.archive_files.remove(path)
        self.write_manifest()

    def write_manifest(self):
        with self._manifest_lock:
            self._write_manifest()

    def _write_manifest(self):
        session = self.session
        end = session.stopped_at or datetime.now()
        duration = int((end - session.started_at).total_seconds())
        muxer = session.muxer
        data = {
            "version": 4,
            "user_id": session.user_id,
            "channel_id": session.channel_id,
            "channel_title": session.channel_title,
            "recording_id": getattr(session, "recording_id", -1),
            "telegram_call_id": getattr(session, "telegram_call_id", None),
            "status": session.state_machine.current_state.name.lower(),
            "started_at": session.started_at.isoformat(),
            "stopped_at": end.isoformat() if session.stopped_at else None,
            "duration_seconds": duration,
            "is_video": session.is_video,
            "output_files": [
                {"path": path, "size": session.output_sizes.get(path)}
                for path in session.output_files
            ],
            "uploaded_parts": sorted(session.uploaded_parts),
            "archive_files": session.archive_files,
            "part_actions": session.part_actions,
            "pending_actions": session.pending_actions,
            "delivery_sources": session.delivery_sources,
            "delivery_outputs": session.delivery_outputs,
            "progress_message_id": session.progress_message_id,
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
            temporary = session.manifest_path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, session.manifest_path)
        except Exception as exc:
            logger.warning("Could not write manifest: %s", exc)
