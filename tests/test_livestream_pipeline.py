from __future__ import annotations

import asyncio
import json
import math
import os
import queue
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


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_backpressure_preserves_fifo_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            muxer = MuxerManager(directory, "queue")
            muxer._write_queue = queue.Queue(maxsize=1)
            muxer._video_generation = 1

            self.assertTrue(await muxer.write_video_frame_async(1, b"first"))
            blocked = asyncio.create_task(muxer.write_video_frame_async(1, b"second"))
            await asyncio.sleep(0.05)
            self.assertEqual(muxer._write_queue.get_nowait(), (1, b"first"))
            self.assertTrue(await asyncio.wait_for(blocked, timeout=1))
            self.assertEqual(muxer._write_queue.get_nowait(), (1, b"second"))

            muxer._video_generation = 2
            self.assertFalse(await muxer.write_video_frame_async(1, b"stale"))


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
            # Native resolution preserved — no canvas downscale
            self.assertEqual((video["width"], video["height"]), (80, 60))

    def test_era_rotation_produces_rolling_parts(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "livestream.muxer.get_ffmpeg_path", return_value=str(FFMPEG)
        ), patch(
            "livestream.muxer.get_ffprobe_path", return_value=str(FFPROBE)
        ):
            parts: list[str] = []
            muxer = MuxerManager(directory, "roll")
            muxer.set_part_callback(parts.append)

            audio = muxer.start_audio_encoder()
            audio.stdin.write(pcm_tone(0.8))
            camera = VideoSource(10, SourceType.CAMERA, 64, 48)
            gen1 = muxer.start_video_encoder(camera)
            frame = i420_frame(64, 48, 100)
            for _ in range(15):
                self.assertTrue(muxer.write_video_frame(gen1, frame))

            # rotate to era 1
            muxer.request_rotation()
            gen2 = muxer.start_video_encoder(camera)
            self.assertNotEqual(gen1, gen2)
            for _ in range(15):
                self.assertTrue(muxer.write_video_frame(gen2, frame))
            audio2 = muxer.open_audio_process()
            audio2.stdin.write(pcm_tone(0.8))
            audio2.stdin.close()

            # final: era 1 becomes part001, era 2 merges via finalize
            output = muxer.finalize_media(os.path.join(directory, "roll.mp4"))
            # era0 was finalized in the background -> part000.mp4
            deadline = __import__("time").time() + 60
            while not parts and __import__("time").time() < deadline:
                __import__("time").sleep(0.2)
            self.assertTrue(parts, "background era finalization produced no parts")
            self.assertTrue(valid_media(parts[0]))
            self.assertTrue(valid_media(output))
            self.assertNotEqual(parts[0], output)
            muxer.close_logs()



if __name__ == "__main__":
    unittest.main()
