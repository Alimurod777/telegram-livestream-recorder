from __future__ import annotations

import json
import math
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from livestream.metadata import SourceType, VideoSource
from livestream.muxer import MuxerManager, valid_media


ROOT = Path(__file__).resolve().parents[1]
FFMPEG_DIR = ROOT / "venv" / "Lib" / "site-packages" / "static_ffmpeg" / "bin" / "win32"
FFMPEG = FFMPEG_DIR / "ffmpeg.exe"
FFPROBE = FFMPEG_DIR / "ffprobe.exe"


def i420_frame(width: int, height: int, luma: int) -> bytes:
    y = bytes([luma]) * (width * height)
    chroma = bytes([128]) * (width * height // 4)
    return y + chroma + chroma


def pcm_tone(seconds: float = 1.0) -> bytes:
    samples = int(48000 * seconds)
    return b"".join(
        struct.pack("<h", int(9000 * math.sin(2 * math.pi * 440 * index / 48000)))
        for index in range(samples)
    )


@unittest.skipUnless(FFMPEG.exists() and FFPROBE.exists(), "bundled FFmpeg is unavailable")
class PipelineTests(unittest.TestCase):
    def test_resolution_and_source_switch_produces_one_av_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "livestream.muxer.get_ffmpeg_path", return_value=str(FFMPEG)
        ), patch(
            "livestream.muxer.get_ffprobe_path", return_value=str(FFPROBE)
        ):
            muxer = MuxerManager(directory, "switch")
            audio = muxer.start_audio_encoder()
            audio.stdin.write(pcm_tone(1.2))

            camera = VideoSource(10, SourceType.CAMERA, 64, 48)
            camera_generation = muxer.start_video_encoder(camera)
            camera_frame = i420_frame(64, 48, 70)
            for _ in range(18):
                self.assertTrue(muxer.write_video_frame(camera_generation, camera_frame))

            screen = VideoSource(20, SourceType.SCREEN_SHARE, 80, 60)
            screen_generation = muxer.start_video_encoder(screen)
            self.assertNotEqual(camera_generation, screen_generation)
            # Old generation should be rejected
            self.assertFalse(muxer.write_video_frame(camera_generation, camera_frame))
            screen_frame = i420_frame(80, 60, 180)
            for _ in range(18):
                self.assertTrue(muxer.write_video_frame(screen_generation, screen_frame))

            muxer.close_all()
            output = muxer.finalize_media(os.path.join(directory, "switch.mp4"))
            self.assertTrue(valid_media(output))

            probe = subprocess.run(
                [
                    str(FFPROBE),
                    "-v",
                    "error",
                    "-show_streams",
                    "-of",
                    "json",
                    output,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            streams = json.loads(probe.stdout)["streams"]
            kinds = {stream["codec_type"] for stream in streams}
            self.assertEqual(kinds, {"audio", "video"})
            video = next(stream for stream in streams if stream["codec_type"] == "video")
            # Both segments normalized to canvas size (1280x720)
            self.assertEqual((video["width"], video["height"]), (1280, 720))


if __name__ == "__main__":
    unittest.main()
