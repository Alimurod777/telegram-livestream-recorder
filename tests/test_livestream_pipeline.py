from __future__ import annotations

import asyncio
import json
import math
import os
import queue
import shutil
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock

from livestream.metadata import SourceType, VideoSource
from livestream.muxer import MuxerManager, valid_media


ROOT = Path(__file__).resolve().parents[1]
FFMPEG_DIR = ROOT / "venv" / "Lib" / "site-packages" / "static_ffmpeg" / "bin" / "win32"
FFMPEG = Path(shutil.which("ffmpeg") or FFMPEG_DIR / "ffmpeg.exe")
FFPROBE = Path(shutil.which("ffprobe") or FFMPEG_DIR / "ffprobe.exe")


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
            audio.stdin.close()  # the mixer performs this swap in production
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
            for path in (parts[0], output):
                result = subprocess.run(
                    [str(FFPROBE), "-v", "error", "-select_streams", "v:0",
                     "-count_frames", "-show_entries", "stream=nb_read_frames",
                     "-of", "csv=p=0", path],
                    capture_output=True, text=True, check=True,
                )
                self.assertEqual(int(result.stdout.strip()), 15)
            muxer.close_logs()

    def test_audio_only_rotation_uses_audio_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "livestream.muxer.get_ffmpeg_path", return_value=str(FFMPEG)
        ), patch("livestream.muxer.get_ffprobe_path", return_value=str(FFPROBE)):
            muxer = MuxerManager(directory, "audio")
            parts = []
            muxer.set_part_callback(parts.append)
            audio = muxer.start_audio_encoder()
            audio.stdin.write(pcm_tone(0.5))
            muxer.request_rotation()
            audio.stdin.close()
            audio = muxer.open_audio_process()
            audio.stdin.write(pcm_tone(0.5))
            output = muxer.finalize_media(os.path.join(directory, "audio.mp4"))
            self.assertEqual(len(parts), 2)
            self.assertTrue(all(path.endswith(".m4a") and valid_media(path) for path in parts))
            self.assertEqual(output, parts[-1])
            muxer.close_logs()

    def test_size_split_checks_every_part_without_reencoding(self) -> None:
        from livestream.recorder import StreamRecorder
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "input.mp4")
            subprocess.run(
                [str(FFMPEG), "-v", "error", "-f", "lavfi", "-i",
                 "testsrc2=size=64x48:rate=10", "-t", "8", "-c:v", "libx264",
                 "-g", "10", "-sc_threshold", "0", source], check=True,
            )
            limit = int(os.path.getsize(source) * 0.7)
            with patch("ffmpeg_helper.get_ffmpeg_path", return_value=str(FFMPEG)), patch(
                "ffmpeg_helper.get_ffprobe_path", return_value=str(FFPROBE)
            ):
                parts = StreamRecorder._split_file_sync(source, limit, False)
                self.assertGreater(len(parts), 1)
                self.assertTrue(all(os.path.getsize(path) <= limit for path in parts))
                self.assertTrue(os.path.exists(source))
                count = 0
                for path in parts:
                    result = subprocess.run(
                        [str(FFPROBE), "-v", "error", "-select_streams", "v:0",
                         "-count_frames", "-show_entries", "stream=nb_read_frames",
                         "-of", "csv=p=0", path], capture_output=True, text=True, check=True,
                    )
                    count += int(result.stdout.strip())
                self.assertEqual(count, 80)
                self.assertEqual(StreamRecorder._split_file_sync(source, limit, False), parts)

    def test_recovery_salvages_closed_encoder_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "livestream.muxer.get_ffmpeg_path", return_value=str(FFMPEG)
        ), patch("livestream.muxer.get_ffprobe_path", return_value=str(FFPROBE)):
            original = MuxerManager(directory, "recover")
            audio = original.start_audio_encoder()
            audio.stdin.write(pcm_tone(0.5))
            audio.stdin.close()
            audio.wait(timeout=5)
            recovered = MuxerManager(directory, "recover")
            parts = recovered.recover_parts()
            self.assertEqual(len(parts), 1)
            self.assertTrue(parts[0].endswith(".m4a"))
            self.assertTrue(valid_media(parts[0]))
            original.close_logs()



@unittest.skipUnless(FFMPEG.exists() and FFPROBE.exists(), "FFmpeg is unavailable")
class DeliveryWindowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import bot
        self.bot = bot
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = self.temporary.name
        self.patches = [patch("livestream.muxer.get_ffmpeg_path", return_value=str(FFMPEG)),
                        patch("livestream.muxer.get_ffprobe_path", return_value=str(FFPROBE)),
                        patch("livestream.uploader.get_ffmpeg_path", return_value=str(FFMPEG)),
                        patch.object(bot, "send_message", new=AsyncMock())]
        for item in self.patches:
            item.start()
        from livestream.recorder import RecordingSession
        self.session = RecordingSession(987, -1000000000042, "test")
        self.session.muxer = MuxerManager(self.directory, "window")
        self.session.output_path = os.path.join(self.directory, "window.mp4")
        self.session.manifest_path = os.path.join(self.directory, "window.recording.json")
        self.sent = []

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def segment(self, index, video=True):
        path = os.path.join(self.directory, f"window.part{index:03d}.{'mp4' if video else 'm4a'}")
        command = [str(FFMPEG), "-v", "error", "-y"]
        if video:
            command += ["-f", "lavfi", "-i", "testsrc2=size=64x48:rate=10"]
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1"]
        if video:
            command += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
        command += ["-c:a", "aac", "-ac", "2", path]
        subprocess.run(command, capture_output=True, check=True)
        self.session.archive_files.append(path)
        return path

    async def upload(self, session, path, index, total, allow_split=True):
        self.assertFalse(allow_split, "committed windows must never be re-split")
        result = subprocess.run([str(FFPROBE), "-v", "error", "-count_frames", "-show_streams", "-of", "json", path],
                                capture_output=True, text=True, check=True)
        streams = json.loads(result.stdout)["streams"]
        video = next((s for s in streams if s["codec_type"] == "video"), None)
        self.sent.append((os.path.basename(path), int(video["nb_read_frames"]) if video else 0))
        session.uploaded_parts.add(path)
        session.uploader.write_manifest()
        os.remove(path)
        return True

    async def test_send_preview_then_final_combines_previous_and_remaining(self):
        first = self.segment(0)
        self.session.part_actions[first] = "preview"
        with patch.object(self.bot, "upload_part", new=AsyncMock(side_effect=self.upload)):
            await self.bot.on_part_ready(self.session, first)
            self.assertTrue(os.path.exists(first))
            self.segment(1)
            self.session.finalizing = True
            self.session.status = "completed"
            await self.bot.deliver_recording(self.session)
        self.assertEqual([frames for _, frames in self.sent if frames], [10, 20])
        self.assertEqual(len([name for name, frames in self.sent if not frames]), 2)
        self.assertFalse(os.path.exists(first))

    async def test_capacity_chunk_is_not_combined_or_uploaded_again(self):
        first = self.segment(0)
        self.session.part_actions[first] = "capacity"
        with patch.object(self.bot, "upload_part", new=AsyncMock(side_effect=self.upload)):
            await self.bot.on_part_ready(self.session, first)
            self.assertFalse(os.path.exists(first))
            self.segment(1)
            self.session.finalizing = True
            self.session.status = "completed"
            await self.bot.deliver_recording(self.session)
        self.assertEqual([frames for _, frames in self.sent if frames], [10, 10])
        self.assertEqual(len(self.sent), 4)

    async def test_preview_then_capacity_then_stop_delivers_only_remaining_tail(self):
        # Short real media stands in for the user's 1h / 1.5h / 3h timeline.
        first = self.segment(0)
        first_size = os.path.getsize(first)
        self.session.part_actions[first] = "preview"
        with patch.object(self.bot, "upload_part", new=AsyncMock(side_effect=self.upload)):
            await self.bot.on_part_ready(self.session, first)
            self.assertEqual(self.session.archive_files, [first])
            self.assertEqual(self.session.pending_bytes(), first_size)

            boundary = self.segment(1)
            self.assertEqual(self.session.pending_bytes(), first_size + os.path.getsize(boundary))
            self.session.part_actions[boundary] = "capacity"
            await self.bot.on_part_ready(self.session, boundary)
            self.assertEqual([frames for _, frames in self.sent if frames], [10, 20])
            self.assertEqual(self.session.archive_files, [])
            self.assertEqual(self.session.pending_bytes(), 0)
            self.assertFalse(os.path.exists(first))
            self.assertFalse(os.path.exists(boundary))

            tail = self.segment(2)
            self.assertEqual(self.session.pending_bytes(), os.path.getsize(tail))
            self.session.finalizing = True
            self.session.status = "completed"
            await self.bot.deliver_recording(self.session)

        # Preview = first interval; capacity = first + second; final = third only.
        self.assertEqual([frames for _, frames in self.sent if frames], [10, 20, 10])
        self.assertEqual(len(self.sent), 6)  # One video and one audio per delivery.
        self.assertTrue(self.session.delivery_complete)

    async def test_audio_retry_does_not_resend_successful_video(self):
        first = self.segment(0)
        self.session.part_actions[first] = "capacity"
        failed = False

        async def fail_audio_once(session, path, *args, **kwargs):
            nonlocal failed
            if path.endswith(".audio.m4a") and not failed:
                failed = True
                raise OSError("temporary upload failure")
            return await self.upload(session, path, *args, **kwargs)

        with patch.object(self.bot, "upload_part", new=AsyncMock(side_effect=fail_audio_once)):
            with self.assertRaises(OSError):
                await self.bot.deliver_window(self.session, first, "capacity")
            self.assertTrue(os.path.exists(first))
            await self.bot.deliver_window(self.session, first, "capacity")
        self.assertEqual(len(self.sent), 2)
        self.assertFalse(os.path.exists(first))

    async def test_audio_only_window_sends_one_audio_file(self):
        first = self.segment(0, video=False)
        self.segment(1, video=False)
        with patch.object(self.bot, "upload_part", new=AsyncMock(side_effect=self.upload)):
            await self.bot.deliver_window(self.session, None, "final")
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0][0].endswith(".m4a"))

    async def test_audio_then_video_can_be_combined(self):
        self.segment(0, video=False)
        self.segment(1)
        with patch.object(self.bot, "upload_part", new=AsyncMock(side_effect=self.upload)):
            await self.bot.deliver_window(self.session, None, "final")
        self.assertEqual(len(self.sent), 2)
        self.assertGreaterEqual(self.sent[0][1], 20)

    async def test_failed_merge_never_leaves_reusable_partial_output(self):
        sources = [self.segment(0), self.segment(1)]
        output = os.path.join(self.directory, "combined.mp4")

        def fail(command, description):
            shutil.copyfile(sources[0], command[-1])
            return False

        with patch.object(self.session.muxer, "_run_ffmpeg", side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.session.muxer.combine_parts(sources, output)
        self.assertFalse(os.path.exists(output))
        self.assertFalse(os.path.exists(os.path.join(self.directory, "combined.assembling.mp4")))
        self.assertTrue(all(os.path.exists(path) for path in sources))

    async def test_restart_after_video_upload_retries_only_audio(self):
        from livestream import recorder as module
        user_dir = os.path.join(self.directory, "987")
        os.mkdir(user_dir)
        self.session.muxer = MuxerManager(user_dir, "window")
        self.session.manifest_path = os.path.join(user_dir, "window.recording.json")
        first = self.segment(0)
        moved = os.path.join(user_dir, os.path.basename(first))
        shutil.move(first, moved)
        self.session.archive_files = [moved]
        self.session.part_actions[moved] = "capacity"
        outputs = self.session.uploader.prepare_delivery([moved], "chunk0")
        await self.upload(self.session, outputs[0], 1, 2, allow_split=False)
        self.sent.clear()
        recorder = module.StreamRecorder()
        recorder._completion_callback = self.bot.deliver_recording
        with patch.object(module, "OUTPUT_DIR", self.directory), patch.object(
            self.bot, "upload_part", new=AsyncMock(side_effect=self.upload)
        ):
            await recorder.recover_incomplete()
        self.assertEqual(self.sent, [("window.chunk0.audio.m4a", 0)])
        self.assertFalse(os.path.exists(self.session.manifest_path))


if __name__ == "__main__":
    unittest.main()
