from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from ntgcalls import MediaSegmentQuality
from pyrogram import raw
from pyrogram.raw.core import TLObject
from pytgcalls.mtproto.pyrogram_client import PyrogramClient

from livestream.broadcast import install_broadcast_fix


class BroadcastTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Avoid constructing a real Telegram client: test the bridge's TL wire format.
        self.bridge = object.__new__(PyrogramClient)
        self.bridge._cache = SimpleNamespace(get_full_chat=AsyncMock(
            return_value=raw.types.InputGroupCall(id=1, access_hash=2)))
        self.bridge._invoke = AsyncMock(return_value=SimpleNamespace(bytes=b"media"))
        self.call = SimpleNamespace(mtproto_client=SimpleNamespace(_bind_client=self.bridge))
        install_broadcast_fix(self.call)

    async def request(self, channel):
        result = await self.bridge.download_stream(-10042, 4000, 131072, channel, MediaSegmentQuality.FULL)
        self.assertEqual(result, b"media")
        args, kwargs = self.bridge._invoke.call_args
        wire = BytesIO(args[0].location.write())
        location = TLObject.read(wire)
        self.assertEqual(wire.tell(), len(wire.getvalue()))
        self.assertEqual(kwargs, {"chat_id": -10042, "sleep_threshold": 0})
        return location

    async def test_audio_omits_both_fields_sharing_flag_zero(self):
        location = await self.request(None)
        self.assertIsNone(location.video_channel)
        self.assertIsNone(location.video_quality)
        self.assertEqual(location.time_ms, 4000)

    async def test_video_keeps_channel_and_quality_together(self):
        location = await self.request(1)
        self.assertEqual(location.video_channel, 1)
        self.assertEqual(location.video_quality, 2)

    async def test_errors_are_reported_and_reraised_without_log_flooding(self):
        self.bridge._invoke.side_effect = RuntimeError("INPUT_FETCH_ERROR")
        with self.assertLogs("livestream.broadcast", level="WARNING") as logs:
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "INPUT_FETCH_ERROR"):
                    await self.bridge.download_stream(-10042, 4000, 131072, None, MediaSegmentQuality.FULL)
        self.assertEqual(len(logs.output), 1)

    async def test_no_active_call_makes_no_download(self):
        self.bridge._cache.get_full_chat.return_value = None
        self.assertIsNone(await self.bridge.download_stream(-10042, 4000, 131072, None, MediaSegmentQuality.FULL))
        self.bridge._invoke.assert_not_awaited()


class EmptyCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_reports_missing_frames_without_muxing_empty_container(self):
        from livestream import recorder as module
        from livestream.state_machine import StateMachine
        import threading

        recorder = module.StreamRecorder()
        session = SimpleNamespace(
            user_id=123, channel_id=-10042, recording_id=1,
            finalizing=False, state_machine=StateMachine(),
            uploader=Mock(), muxer=Mock(), audio_mixer=Mock(audio_frames=0),
            video_frames=0, is_video=False, _rotation_task=None, _watchdog_task=None,
            _duration_task=None, call_client=None, stop_event=threading.Event(),
            output_sizes={}, output_files=[], stats_line=Mock(return_value="empty"),
        )
        recorder.active_sessions[123] = session
        with patch.object(module.db, "finish_recording"), self.assertLogs("livestream.recorder", level="ERROR"):
            await recorder.stop_recording(123)
        session.uploader.finalize.assert_not_called()
        session.muxer.close_all.assert_called_once()
        self.assertEqual(session.status, "failed")
        self.assertIn("kadrlar kelmadi", session.error)
        self.assertNotIn(123, recorder.active_sessions)


if __name__ == "__main__":
    unittest.main()
