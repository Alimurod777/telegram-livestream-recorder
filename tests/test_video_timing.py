"""Video capture clocks must not turn real-time recordings into slow motion."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from livestream.event_handler import EventHandler
from livestream.metadata import SourceType
from livestream.source_manager import SourceManager
from livestream.state_machine import StateMachine


class VideoTimingTests(unittest.TestCase):
    def test_capture_clock_units_are_checked_against_elapsed_time(self):
        for scale in (1, 1000, 1_000_000, 4_294_967.296):
            with self.subTest(capture_units_per_millisecond=scale):
                handler = EventHandler(SimpleNamespace())
                session = SimpleNamespace(muxer=SimpleNamespace(encoder_fps=30))
                for index in range(90):
                    fps = handler._track_fps(
                        session, 100 + index / 15,
                        int((10_000 + index * 1000 / 15) * scale),
                    )
                self.assertAlmostEqual(fps, 15, delta=0.1)
                session.muxer.encoder_fps = fps
                self.assertFalse(handler._needs_fps_restart(session, fps))

    def test_missing_reset_or_repeated_capture_time_uses_arrival_clock(self):
        for kind in ("missing", "repeated", "reset", "intermittent"):
            with self.subTest(kind=kind):
                handler = EventHandler(SimpleNamespace())
                session = SimpleNamespace()
                for index in range(90):
                    capture = 10_000 + index * 1000 // 30
                    if kind == "missing" or (kind == "intermittent" and index % 2):
                        capture = 0
                    elif kind == "repeated":
                        capture = 10_000
                    elif kind == "reset" and index > 45:
                        capture -= 5000
                    fps = handler._track_fps(session, 100 + index / 30, capture)
                self.assertAlmostEqual(fps, 30, delta=0.1)

    def test_batched_arrivals_do_not_trigger_extreme_fps_restart(self):
        handler = EventHandler(SimpleNamespace())
        session = SimpleNamespace(muxer=SimpleNamespace(encoder_fps=30))
        for index in range(90):
            fps = handler._track_fps(session, 100 + index / 10000)
        self.assertFalse(handler._needs_fps_restart(session, fps))

    def test_unrepresentable_fps_does_not_restart_forever_at_clamped_rate(self):
        handler = EventHandler(SimpleNamespace())
        session = SimpleNamespace(muxer=SimpleNamespace(encoder_fps=1))
        for fps in (0, 0.001, 0.5, 121, float("nan"), float("inf")):
            with self.subTest(fps=fps):
                self.assertFalse(handler._needs_fps_restart(session, fps))

    def test_forget_removes_recording_clock_samples(self):
        handler = EventHandler(SimpleNamespace())
        session = SimpleNamespace()
        handler._track_fps(session, 100, 10000)
        handler.forget(session)
        self.assertNotIn(id(session), handler._fps_windows)


class VideoTimingEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_resizes_keep_measured_fps_without_restart_loop(self):
        from pytgcalls.types import Device

        session = SimpleNamespace(
            channel_id=123, user_id=7, source_manager=SourceManager(),
            active_video_source=None, state_machine=StateMachine(),
            video_frames=0, audio_mixer=SimpleNamespace(request_rotate=Mock()),
        )
        generations = []

        def start_encoder(source, fps=None):
            session.muxer.encoder_fps = fps or 30
            generations.append((source.resolution, session.muxer.encoder_fps))
            return len(generations)

        session.muxer = SimpleNamespace(
            video_segments=["existing.mkv"], encoder_fps=30,
            video_writer_dead=False, request_rotation=Mock(),
            start_video_encoder=Mock(side_effect=start_encoder),
            write_video_frame_async=AsyncMock(return_value=True),
        )
        handler = EventHandler(SimpleNamespace(_notify_callback=None))
        clock = SimpleNamespace(monotonic=lambda: now)
        with patch("livestream.event_handler.time", clock):
            for index in range(750):
                now = 100 + index / 15
                width, height = (64, 48) if (index // 150) % 2 == 0 else (32, 24)
                frame = SimpleNamespace(
                    ssrc=11, frame=b"\0" * (width * height * 3 // 2),
                    info=SimpleNamespace(width=width, height=height, rotation=0,
                                         capture_time=10_000_000_000 + index * 66_666_667),
                )
                await handler._handle_video_frames(
                    session, SimpleNamespace(device=Device.SCREEN, frames=[frame])
                )
        self.assertEqual(len(generations), 6)  # Initial, FPS correction, four resizes.
        self.assertEqual(generations[0][1], 30)
        for _, fps in generations[1:]:
            self.assertAlmostEqual(fps, 15, delta=0.1)
        self.assertEqual(session.video_frames, 750)
        self.assertEqual(session.muxer.write_video_frame_async.await_count, 750)
        self.assertEqual(session.active_video_source.source_type, SourceType.SCREEN_SHARE)
