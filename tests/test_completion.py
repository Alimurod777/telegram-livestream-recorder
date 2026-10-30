import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import urllib.error
import subprocess
from unittest.mock import AsyncMock, Mock, patch

from pyrogram import raw
from pyrogram.errors import FloodWait
from livestream.recorder import StreamRecorder
from livestream.mtproto_uploader import MtprotoUploader


class EndDetectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.recorder = StreamRecorder()
        self.call = raw.types.InputGroupCall(id=8, access_hash=9)
        self.client = SimpleNamespace(resolve_peer=AsyncMock(return_value=raw.types.InputPeerChannel(channel_id=42, access_hash=7)), invoke=AsyncMock())
        self.session = SimpleNamespace(client=self.client, channel_id=-10042, user_id=1, telegram_call_id=8, finalizing=False)

    async def test_removed_call_is_ended(self):
        self.client.invoke.return_value = SimpleNamespace(full_chat=SimpleNamespace(call=None))
        self.assertTrue(await self.recorder._call_has_ended(self.session))

    async def test_replacement_call_does_not_extend_old_recording(self):
        self.client.invoke.return_value = SimpleNamespace(full_chat=SimpleNamespace(call=raw.types.InputGroupCall(id=10, access_hash=11)))
        self.assertTrue(await self.recorder._call_has_ended(self.session))

    async def test_active_call_with_no_frames_is_not_treated_as_ended(self):
        self.client.invoke.side_effect = [SimpleNamespace(full_chat=SimpleNamespace(call=self.call)), SimpleNamespace(call=SimpleNamespace(id=8))]
        self.assertFalse(await self.recorder._call_has_ended(self.session))

    async def test_network_error_does_not_stop_recording(self):
        with patch.object(self.recorder, "_call_has_ended", new=AsyncMock(side_effect=TimeoutError())), patch.object(self.recorder, "stop_recording", new=AsyncMock()) as stop:
            with self.assertLogs("livestream.recorder", level="WARNING"):
                self.assertFalse(await self.recorder._stop_if_call_ended(self.session))
            stop.assert_not_awaited()

    async def test_confirmed_end_schedules_finalization(self):
        done = asyncio.Event()
        async def stop(*args, **kwargs): done.set()
        with patch.object(self.recorder, "_call_has_ended", new=AsyncMock(return_value=True)), patch.object(self.recorder, "stop_recording", new=AsyncMock(side_effect=stop)) as stopped:
            self.assertTrue(await self.recorder._stop_if_call_ended(self.session))
            await asyncio.wait_for(done.wait(), 1)
            stopped.assert_awaited_once_with(1, reason="Telegram stream ended")


class DeliveryFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import bot
        self.bot = bot
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "recording.m4a"
        self.path.write_bytes(b"a" * 2048)
        self.session = SimpleNamespace(user_id=1, channel_id=-10042, telegram_call_id=8, recording_id=2, channel_title="Test", uploaded_parts=set(), uploader=Mock(), get_duration_str=lambda: "1m")

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_flooded_user_immediately_falls_back_to_bot(self):
        delivery_bot = object()
        user = SimpleNamespace(is_connected=True)
        with patch.dict(self.bot.user_clients, {1: user}), patch.object(self.bot, "USE_USERBOT_UPLOAD", True), patch.object(self.bot, "BOT_USERNAME", "testbot"), patch.object(self.bot, "probe_media", return_value={"has_audio": True}), patch.object(self.bot.mtproto_uploader, "get_delivery_bot", new=AsyncMock(return_value=delivery_bot)), patch.object(self.bot.mtproto_uploader, "send", new=AsyncMock(side_effect=[FloodWait(100), True])) as send, patch.object(self.bot.asyncio, "sleep", new=AsyncMock()) as sleep:
            self.assertTrue(await self.bot.upload_part(self.session, str(self.path), 1, 1, allow_split=False))
            self.assertIs(send.call_args.args[0], delivery_bot)
            self.assertEqual(send.call_args.kwargs["recipient_id"], 1)
            sleep.assert_not_awaited()
        self.assertIn(str(self.path), self.session.uploaded_parts)
        self.assertFalse(self.path.exists())

    async def test_failed_bot_upload_keeps_large_recording_for_retry(self):
        with patch.object(self.bot, "USE_USERBOT_UPLOAD", False), patch.object(self.bot, "BOT_API_UPLOAD_LIMIT", 1024), patch.object(self.bot.mtproto_uploader, "get_delivery_bot", new=AsyncMock(side_effect=OSError("unavailable"))):
            with self.assertRaisesRegex(RuntimeError, "retained"):
                await self.bot.upload_part(self.session, str(self.path), 1, 1, allow_split=False)
        self.assertTrue(self.path.exists())
        self.assertFalse(self.session.uploaded_parts)

    async def test_unchanged_progress_is_not_edited_repeatedly(self):
        self.session.delivery_complete = False
        self.session.progress_message_id = 10
        self.session.finalizing = True
        self.session.error = None
        ticks = 0
        async def sleep(_):
            nonlocal ticks
            ticks += 1
            if ticks == 3:
                self.session.delivery_complete = True
        with patch.object(self.bot, "progress_text", return_value="Preparing"), patch.object(self.bot, "edit_message_text", new=AsyncMock(return_value={"ok": True})) as edit, patch.object(self.bot.asyncio, "sleep", new=AsyncMock(side_effect=sleep)):
            await self.bot.recording_progress(self.session)
        edit.assert_awaited_once()

    def test_file_remux_has_no_access_to_terminal_commands(self):
        from livestream.muxer import MuxerManager
        with patch("livestream.muxer.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertTrue(MuxerManager._run_ffmpeg(["ffmpeg", "-i", "input.mkv", "output.mp4"], "test"))
        self.assertIn("-nostdin", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_telegram_not_modified_is_success(self):
        error = urllib.error.HTTPError("https://example.invalid", 400, "Bad request", {}, BytesIO(b'{"description":"Bad Request: message is not modified"}'))
        with patch.object(self.bot.urllib.request, "urlopen", side_effect=error):
            self.assertTrue(self.bot.bot_api("editMessageText")["ok"])

    async def test_bot_video_upload_generates_and_attaches_missing_thumbnail(self):
        uploader = MtprotoUploader()
        jpeg = b"\xff\xd8" + b"a" * 200
        async def send_video(*args, **kwargs):
            self.assertEqual(Path(kwargs["thumb"]).read_bytes(), jpeg)
            return SimpleNamespace(id=1)
        client = SimpleNamespace(resolve_peer=AsyncMock(), send_video=AsyncMock(side_effect=send_video))
        with patch("livestream.mtproto_uploader.extract_video_thumbnail", return_value=jpeg) as generate:
            await uploader.send(client, "", "video.mp4", "caption", {"has_video": True, "duration": 2}, recipient_id=1)
        generate.assert_called_once_with("video.mp4", 1.0)
        self.assertFalse(Path(client.send_video.call_args.kwargs["thumb"]).exists())
