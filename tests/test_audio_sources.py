import io
import logging
import queue
import struct
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from livestream.audio import AudioMixer, TICK_BYTES
from livestream.diagnostics import TelegramDiagnostics, category


def pcm(value, ticks=1):
    return struct.pack("<h", value) * (TICK_BYTES // 2) * ticks


def frame(source, data):
    return SimpleNamespace(ssrc=source, frame=data)


class AudioSourceTests(unittest.TestCase):
    def test_participants_in_separate_callbacks_mix_into_one_tick(self):
        mixer = AudioMixer()
        for _ in range(2200):
            mixer.handle_source_frames([frame(11, pcm(100))])
            mixer.handle_source_frames([frame(22, pcm(300))])
            self.assertEqual(mixer._next_audio_tick(), pcm(200))
        self.assertIsNone(mixer._next_audio_tick())
        self.assertEqual(mixer.audio_dropped, 0)

    def test_long_payloads_and_subticks_preserve_order_per_source(self):
        mixer = AudioMixer()
        data = pcm(100) + pcm(200) + pcm(300)
        mixer.handle_source_frames([frame(11, data[:17])])
        self.assertIsNone(mixer._next_audio_tick())
        mixer.handle_source_frames([frame(11, data[17:])])
        self.assertEqual(b"".join(mixer._next_audio_tick() for _ in range(3)), data)
        self.assertIsNone(mixer._next_audio_tick())

    def test_writer_drains_mixed_sources_and_partial_tick_at_stop(self):
        mixer = AudioMixer()
        mixer.handle_source_frames([frame(11, pcm(100, 3) + pcm(400)[:TICK_BYTES // 2])])
        mixer.handle_source_frames([frame(22, pcm(300, 3))])

        class RetainedBuffer(io.BytesIO):
            def close(self):
                self.closed_by_writer = True

        sink = RetainedBuffer()
        capture, stop = threading.Event(), threading.Event()
        capture.set()
        stop.set()
        mixer.start_writer(SimpleNamespace(stdin=sink), capture, stop)
        mixer.join_writer()
        self.assertEqual(sink.getvalue(), pcm(200, 3) + pcm(400)[:TICK_BYTES // 2] + b"\0" * (TICK_BYTES // 2))
        self.assertTrue(sink.closed_by_writer)
        self.assertFalse(mixer._has_audio())

    def test_queue_bound_is_shared_across_sources_and_warning_is_throttled(self):
        mixer = AudioMixer()
        mixer.audio_queue = queue.Queue(maxsize=2)
        with patch("livestream.audio.time.monotonic", return_value=10), \
                self.assertLogs("livestream.audio", level="WARNING") as logs:
            mixer.handle_source_frames([frame(11, pcm(100, 2))])
            mixer.handle_source_frames([frame(22, pcm(300, 10))])
        self.assertEqual(mixer._source_ticks, 2)
        self.assertEqual(mixer.audio_dropped, 10)
        self.assertEqual(len(logs.records), 1)
        self.assertTrue(hasattr(logs.records[0], "diagnostic_key"))
        with patch("livestream.audio.time.monotonic", return_value=41), \
                self.assertLogs("livestream.audio", level="WARNING") as logs:
            mixer.handle_source_frames([frame(22, pcm(300))])
        self.assertEqual(mixer.audio_dropped, 11)
        self.assertIn("dropped=11", logs.output[0])
        mixer._next_audio_tick()
        mixer._next_audio_tick()
        with patch("livestream.audio.time.monotonic", return_value=80), \
                patch("livestream.audio.logger.warning") as warning:
            mixer.handle_source_frames([frame(11, pcm(100))])
        warning.assert_not_called()


class AudioDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_changing_drop_count_deduplicates_per_recording(self):
        handler = TelegramDiagnostics(Mock(), owner_id=7)
        # Set the event loop without starting network delivery.
        import asyncio
        handler.loop = asyncio.get_running_loop()
        with patch("livestream.diagnostics.logging.getLogger"):
            for scope, dropped in (("chat=1 capture=A", 1), ("chat=1 capture=A", 2),
                                   ("chat=2 capture=B", 3)):
                record = logging.LogRecord("livestream.audio", logging.WARNING, "audio.py", 1,
                                           "Audio queue full: dropped=%s", (dropped,), None)
                record.diagnostic_key = "audio_queue_full " + scope
                handler.emit(record)
            await asyncio.sleep(0)
        self.assertEqual(handler.queue.qsize(), 2)
        self.assertEqual(category("Audio queue full: dropped=3"), "media")
        self.assertEqual(category("Call lost for user 7"), "network")
