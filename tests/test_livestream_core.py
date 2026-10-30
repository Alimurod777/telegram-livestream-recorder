from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from unittest.mock import AsyncMock
import asyncio
import struct
import threading

from ffmpeg_helper import write_all
from livestream.audio import AudioMixer, TICK_BYTES
from livestream.metadata import media_kind

from livestream.metadata import SourceType, VideoSource
from livestream.priority_manager import PriorityManager
from livestream.source_manager import SourceManager
from livestream.state_machine import RecorderState, StateMachine
from livestream.uploader import Uploader


def source(
    source_id: int,
    source_type: SourceType,
    *,
    seen: float,
    width: int = 1280,
    height: int = 720,
) -> VideoSource:
    return VideoSource(
        source_id=source_id,
        source_type=source_type,
        width=width,
        height=height,
        last_seen=seen,
    )


class SourceSelectionTests(unittest.TestCase):
    def test_screen_share_preempts_camera_then_expires(self) -> None:
        manager = SourceManager()
        camera = source(20, SourceType.CAMERA, seen=10.0)
        screen = source(30, SourceType.SCREEN_SHARE, seen=11.0)
        manager.update_sources([camera, screen])

        selected = PriorityManager.get_active_source(manager.get_sources(), camera.source_id)
        self.assertEqual(selected, screen)

        refreshed_camera = source(20, SourceType.CAMERA, seen=13.0)
        manager.update_sources([refreshed_camera])
        expired = manager.prune_stale(max_age=2.0, now=13.1)
        self.assertEqual([item.source_id for item in expired], [screen.source_id])
        self.assertEqual(
            PriorityManager.get_active_source(manager.get_sources()).source_id,
            camera.source_id,
        )

    def test_current_camera_stays_stable_between_equal_priority_sources(self) -> None:
        cameras = [
            source(100, SourceType.CAMERA, seen=1.0),
            source(50, SourceType.CAMERA, seen=1.0),
        ]
        self.assertEqual(
            PriorityManager.get_active_source(cameras, current_active_id=100).source_id,
            100,
        )
        self.assertEqual(PriorityManager.get_active_source(cameras).source_id, 50)

    def test_frame_size_comes_from_native_resolution(self) -> None:
        native = source(
            1,
            SourceType.CAMERA,
            seen=1.0,
            width=640,
            height=480,
        )
        self.assertEqual(native.resolution, (640, 480))
        self.assertEqual(native.expected_frame_size, 640 * 480 * 3 // 2)


class StateMachineTests(unittest.TestCase):
    def test_camera_screen_camera_transition(self) -> None:
        machine = StateMachine()
        self.assertEqual(machine.current_state, RecorderState.WAITING)
        self.assertTrue(machine.transition(RecorderState.CAMERA))
        self.assertTrue(machine.transition(RecorderState.SCREEN_SHARE))
        self.assertTrue(machine.transition(RecorderState.CAMERA))
        self.assertFalse(machine.transition(RecorderState.CAMERA))

    def test_invalid_transition_rejected(self) -> None:
        machine = StateMachine()
        # WAITING -> STOPPED is not allowed
        self.assertFalse(machine.transition(RecorderState.STOPPED))
        self.assertEqual(machine.current_state, RecorderState.WAITING)

    def test_is_recording_property(self) -> None:
        machine = StateMachine()
        self.assertFalse(machine.is_recording)
        machine.transition(RecorderState.CAMERA)
        self.assertTrue(machine.is_recording)
        machine.transition(RecorderState.SCREEN_SHARE)
        self.assertTrue(machine.is_recording)
        machine.transition(RecorderState.STOPPING)
        self.assertFalse(machine.is_recording)


class FinalizationTests(unittest.TestCase):
    def test_audio_only_output_is_m4a_and_accounted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "recording.mp4")
            with open(output, "wb") as handle:
                handle.write(b"0" * 2048)
            session = SimpleNamespace(
                user_id=1,
                output_path=output,
                output_files=[],
                archive_files=[],
                output_sizes={},
                is_video=False,
                muxer=SimpleNamespace(finalize_media=Mock(return_value=output)),
            )
            with patch("livestream.uploader.valid_media", return_value=True):
                Uploader(session).finalize()

            audio = os.path.join(directory, "recording.m4a")
            self.assertEqual(session.archive_files, [audio])
            self.assertEqual(session.output_files, [])
            self.assertTrue(os.path.exists(audio))
            self.assertFalse(os.path.exists(output))


class MediaRegressionTests(unittest.TestCase):
    def test_pipe_short_writes_preserve_complete_frame(self):
        received = bytearray()

        def short_write(data):
            received.extend(data[:3])
            return min(3, len(data))

        write_all(SimpleNamespace(write=short_write), b"0123456789")
        self.assertEqual(received, b"0123456789")

    def test_long_audio_payload_preserves_all_samples(self):
        mixer = AudioMixer()
        payload = struct.pack("<h", 1234) * (TICK_BYTES * 3 // 2)
        mixer.handle_audio_frames([payload])
        self.assertEqual(mixer.audio_queue.qsize(), 3)
        self.assertEqual(b"".join(mixer.audio_queue.get_nowait() for _ in range(3)), payload)

    def test_subtick_audio_is_buffered_without_padding_between_payloads(self):
        mixer = AudioMixer()
        first = b"\x01\x00" * (TICK_BYTES // 4)
        second = b"\x02\x00" * (TICK_BYTES // 4)
        mixer.handle_audio_frames([first])
        self.assertTrue(mixer.audio_queue.empty())
        mixer.handle_audio_frames([second])
        self.assertEqual(mixer.audio_queue.get_nowait(), first + second)

    def test_ogg_audio_is_not_inferred_to_be_a_voice_message(self):
        self.assertEqual(media_kind("recording.ogg", {"has_audio": True}), "audio")
        self.assertEqual(media_kind("voice.ogg", {"has_audio": True, "media_type": "voice"}), "voice")
        self.assertEqual(media_kind("unknown.bin", {}), "document")
        self.assertEqual(media_kind("round.mp4", {"media_type": "video_note"}), "video_note")


class EventRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_video_frame_in_a_batch_is_processed(self):
        from livestream.event_handler import EventHandler
        from pytgcalls.types import StreamFrames, Direction, Device
        session = SimpleNamespace(channel_id=123, finalizing=False, media_lock=asyncio.Lock())
        handler = EventHandler(SimpleNamespace(active_sessions={1: session}))
        handler._handle_video_frames = AsyncMock()
        frames = [object(), object(), object()]
        await handler.handle_update(1, StreamFrames(123, Direction.INCOMING, Device.CAMERA, frames))
        self.assertEqual([call.args[1].frames[0] for call in handler._handle_video_frames.call_args_list], frames)

    async def test_sequential_audio_frames_are_not_mixed_together(self):
        from livestream.event_handler import EventHandler
        from pytgcalls.types import StreamFrames, Direction, Device
        mixer = AudioMixer()
        session = SimpleNamespace(channel_id=123, finalizing=False, audio_mixer=mixer)
        handler = EventHandler(SimpleNamespace(active_sessions={1: session}))
        payloads = [b"\x01\x00" * (TICK_BYTES // 2), b"\x02\x00" * (TICK_BYTES // 2)]
        frames = [SimpleNamespace(ssrc=9, frame=data, info=SimpleNamespace(capture_time=i * 10))
                  for i, data in enumerate(payloads, 1)]
        await handler.handle_update(1, StreamFrames(123, Direction.INCOMING, Device.MICROPHONE, frames))
        self.assertEqual([mixer.audio_queue.get_nowait() for _ in range(2)], payloads)


class BotRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import bot
        self.bot = bot

    async def test_call_detection_uses_raw_full_channel_and_excludes_schedules(self):
        from pyrogram import raw
        client = SimpleNamespace(
            resolve_peer=AsyncMock(return_value=raw.types.InputPeerChannel(channel_id=42, access_hash=99)),
            invoke=AsyncMock(side_effect=[
                SimpleNamespace(full_chat=SimpleNamespace(call="input_call"),
                                chats=[SimpleNamespace(id=42, title="test")]),
                SimpleNamespace(call=SimpleNamespace(schedule_date=123)),
            ]),
        )
        self.assertIsNone(await self.bot.get_channel_call(client, SimpleNamespace(id=-1000000000042)))
        self.assertIsInstance(client.invoke.call_args_list[0].args[0], raw.functions.channels.GetFullChannel)

    async def test_flood_wait_is_not_misreported_as_no_livestream(self):
        from pyrogram.errors import FloodWait
        client = SimpleNamespace(resolve_peer=AsyncMock(side_effect=FloodWait(10)))
        with self.assertRaises(FloodWait):
            await self.bot.get_channel_call(client, SimpleNamespace(id=-1000000000042))

    async def test_private_channel_id_warms_dialog_cache(self):
        chat = SimpleNamespace(id=-1000000000042)

        async def dialogs():
            yield SimpleNamespace(chat=chat)

        client = SimpleNamespace(resolve_peer=AsyncMock(side_effect=ValueError("peer unknown")), get_dialogs=dialogs)
        self.assertIs(await self.bot.resolve_chat(client, chat.id), chat)

    async def test_record_reuses_full_chat_and_call_lookup(self):
        from pyrogram import raw
        call = raw.types.InputGroupCall(id=9, access_hash=8)
        client = SimpleNamespace(
            resolve_peer=AsyncMock(return_value=raw.types.InputPeerChannel(channel_id=42, access_hash=99)),
            invoke=AsyncMock(side_effect=[
                SimpleNamespace(full_chat=SimpleNamespace(call=call),
                                chats=[SimpleNamespace(id=42, title="test")]),
                SimpleNamespace(call=SimpleNamespace(schedule_date=None)),
            ]),
        )
        with patch.object(self.bot, "get_user_client", new=AsyncMock(return_value=client)), patch.object(
            self.bot, "send_message", new=AsyncMock()
        ), patch.object(self.bot.stream_recorder, "get_session", return_value=None), patch.object(
            self.bot.stream_recorder, "is_recording", return_value=False
        ), patch.object(self.bot.stream_recorder, "start_recording", new=AsyncMock()) as start:
            try:
                with patch.object(self.bot, "recording_progress", new=AsyncMock()):
                    await self.bot._handle_record_once(987, 987, "/record https://t.me/XToolsOfficial?livestream")
                self.assertEqual(client.invoke.await_count, 2)
                self.assertIsInstance(client.invoke.call_args_list[0].args[0], raw.functions.channels.GetFullChannel)
                self.assertIsInstance(client.invoke.call_args_list[1].args[0], raw.functions.phone.GetGroupCall)
                start.assert_awaited_once()
                self.assertEqual(start.call_args.kwargs["telegram_call_id"], 9)
            finally:
                self.bot.user_states.pop(987, None)

    async def test_record_waits_for_flood_then_retries_automatically(self):
        from pyrogram.errors import FloodWait
        with patch.object(self.bot, "_handle_record_once", new=AsyncMock(side_effect=[FloodWait(11), None])) as start, patch.object(
            self.bot, "send_message", new=AsyncMock()
        ), patch.object(self.bot.asyncio, "sleep", new=AsyncMock()) as sleep:
            await self.bot._record_with_retry(987, 987, "/record @test")
            self.assertEqual(start.await_count, 2)
            sleep.assert_awaited_once_with(12)

    async def test_pending_record_is_deduplicated_and_stop_cancels_it(self):
        entered = asyncio.Event()

        async def pending(*args):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(self.bot, "_handle_record_once", new=AsyncMock(side_effect=pending)) as start, patch.object(
            self.bot, "send_message", new=AsyncMock()
        ), patch.object(self.bot.stream_recorder, "is_recording", return_value=False):
            try:
                await self.bot.handle_record(987, 987, "/record @test")
                request = self.bot.record_requests[987]
                await asyncio.wait_for(entered.wait(), 1)
                await self.bot.handle_record(987, 987, "/record @test")
                self.assertIs(self.bot.record_requests[987], request)
                self.assertEqual(start.await_count, 1)
                await self.bot.handle_stop(987, 987)
                self.assertTrue(request.cancelled())
                self.assertNotIn(987, self.bot.record_requests)
            finally:
                request = self.bot.record_requests.pop(987, None)
                if request:
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)

    async def test_stop_starts_before_notification_finishes(self):
        stopped, send_started, allow_send = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def slow_send(*args):
            send_started.set()
            await allow_send.wait()

        async def stop(*args):
            stopped.set()

        with patch.object(self.bot, "send_message", new=AsyncMock(side_effect=slow_send)), patch.object(
            self.bot, "stop_recording_background", new=AsyncMock(side_effect=stop)
        ), patch.object(self.bot.stream_recorder, "is_recording", return_value=True):
            task = asyncio.create_task(self.bot.handle_stop(987, 987))
            try:
                await asyncio.wait_for(send_started.wait(), 1)
                await asyncio.wait_for(stopped.wait(), 1)
            finally:
                allow_send.set()
                await task

    async def test_native_upload_retries_keep_media_type(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as handle, patch.object(
            self.bot, "probe_media", return_value={"has_video": True, "duration": 1, "width": 64, "height": 48}
        ), patch.object(self.bot, "extract_thumbnail", return_value=None), patch.object(
            self.bot, "bot_api_upload_file", side_effect=[OSError("network"), {"ok": True}]
        ) as upload, patch.object(self.bot.asyncio, "sleep", new=AsyncMock()):
            await self.bot.send_recording_file(1, handle.name, "caption")
            self.assertEqual([call.args[0] for call in upload.call_args_list], ["sendVideo", "sendVideo"])

    async def test_invalid_code_keeps_login_pending(self):
        from pyrogram.errors import PhoneCodeInvalid
        self.bot.user_states[987] = self.bot.UserState.WAITING_CODE
        self.bot.user_login_data[987] = {"phone": "123", "phone_code_hash": "hash"}
        self.bot.user_clients[987] = SimpleNamespace(sign_in=AsyncMock(side_effect=PhoneCodeInvalid()))
        try:
            with patch.object(self.bot, "send_message", new=AsyncMock()):
                await self.bot.handle_text(987, 987, "12345")
            self.assertEqual(self.bot.user_states[987], self.bot.UserState.WAITING_CODE)
        finally:
            for mapping in (self.bot.user_states, self.bot.user_login_data, self.bot.user_clients):
                mapping.pop(987, None)

    async def test_app_code_prompts_for_single_message_and_high_level_signin(self):
        from pyrogram import raw
        client = SimpleNamespace(sign_in=AsyncMock(return_value=SimpleNamespace(id=42)))
        self.bot.user_login_data[987] = {"phone": "123"}
        self.bot.user_clients[987] = client
        try:
            with patch.object(self.bot, "send_message", new=AsyncMock()) as send, patch.object(
                self.bot, "activate_user_client", new=AsyncMock()
            ), patch.object(self.bot, "save_user_session", new=AsyncMock()) as save:
                await self.bot.accept_sent_code(987, 987, SimpleNamespace(
                    phone_code_hash="hash", type=raw.types.auth.SentCodeTypeApp(length=5)))
                self.assertEqual(self.bot.user_states[987], self.bot.UserState.WAITING_CODE)
                self.assertIn("12345", send.call_args.args[1])
                await self.bot.handle_text(987, 987, "12345")
                client.sign_in.assert_awaited_once_with(phone_number="123", phone_code_hash="hash", phone_code="12345")
                save.assert_awaited_once_with(987, client)
        finally:
            for mapping in (self.bot.user_states, self.bot.user_login_data, self.bot.user_clients):
                mapping.pop(987, None)

    async def test_send_code_migrates_and_uses_source_project_settings(self):
        from pyrogram import raw
        from pyrogram.errors import PhoneMigrate
        sent = SimpleNamespace(phone_code_hash="hash")
        client = SimpleNamespace(invoke=AsyncMock(side_effect=[PhoneMigrate(4), sent]))
        with patch.object(self.bot, "migrate_login_dc", new=AsyncMock()) as migrate:
            self.assertIs(await self.bot.send_code_raw(client, "+123"), sent)
            migrate.assert_awaited_once_with(client, 4)
        for call in client.invoke.call_args_list:
            request = call.args[0]
            self.assertIsInstance(request, raw.functions.auth.SendCode)
            self.assertEqual(request.phone_number, "123")
            self.assertTrue(request.settings.allow_app_hash)
            self.assertFalse(request.settings.allow_firebase)

    async def test_three_invalid_codes_close_pending_client(self):
        from pyrogram.errors import PhoneCodeInvalid
        client = SimpleNamespace(sign_in=AsyncMock(side_effect=PhoneCodeInvalid()),
                                 is_connected=True, disconnect=AsyncMock())
        self.bot.user_states[987] = self.bot.UserState.WAITING_CODE
        self.bot.user_login_data[987] = {"phone": "123", "phone_code_hash": "hash"}
        self.bot.user_clients[987] = client
        try:
            with patch.object(self.bot, "send_message", new=AsyncMock()):
                for _ in range(3):
                    await self.bot.handle_text(987, 987, "12345")
            client.disconnect.assert_awaited_once()
            self.assertEqual(self.bot.user_states[987], self.bot.UserState.IDLE)
            self.assertNotIn(987, self.bot.user_clients)
            self.assertNotIn(987, self.bot.user_login_data)
        finally:
            self.bot.user_states.pop(987, None)

    async def test_email_setup_requests_complete_verification_code(self):
        from pyrogram import raw
        self.bot.user_login_data[987] = {"phone": "123"}
        sent = SimpleNamespace(phone_code_hash="hash", type=raw.types.auth.SentCodeTypeSetUpEmailRequired())
        try:
            with patch.object(self.bot, "send_message", new=AsyncMock()):
                await self.bot.accept_sent_code(987, 987, sent)
                self.assertEqual(self.bot.user_states[987], self.bot.UserState.WAITING_EMAIL)
                client = SimpleNamespace(invoke=AsyncMock(return_value=SimpleNamespace(email_pattern="a***@test.com")))
                self.bot.user_clients[987] = client
                await self.bot.handle_text(987, 987, "a@test.com")
                self.assertEqual(self.bot.user_states[987], self.bot.UserState.WAITING_EMAIL_CODE)
                request = client.invoke.call_args.args[0]
                self.assertIsInstance(request, raw.functions.account.SendVerifyEmailCode)
                self.assertEqual(request.purpose.phone_code_hash, "hash")
        finally:
            for mapping in (self.bot.user_states, self.bot.user_login_data, self.bot.user_clients):
                mapping.pop(987, None)

    async def test_raw_group_call_update_starts_only_confirmed_wait(self):
        from pyrogram import raw
        client = Mock()
        handlers = []
        client.add_handler.side_effect = lambda handler, group: handlers.append(handler)
        self.bot.attach_user_client_handlers(987, client)
        self.bot.waiting_sessions[987] = {"channel_id": -1000000000042, "chat_id": 987, "title": "test", "task": None}
        update = raw.types.UpdateGroupCall(chat_id=42, call=raw.types.GroupCall(
            id=3, access_hash=4, participants_count=1, unmuted_video_limit=30, version=1))
        try:
            with patch.object(self.bot, "start_recording_for_user", new=AsyncMock()) as start, patch.object(
                self.bot.db, "delete_waiting"
            ):
                await handlers[1].callback(client, update, {}, {42: object()})
                start.assert_not_awaited()
                channel = raw.types.Channel(id=42, title="test", photo=raw.types.ChatPhotoEmpty(), date=0, broadcast=True)
                await handlers[1].callback(client, update, {}, {42: channel})
                start.assert_awaited_once()
                self.assertNotIn(987, self.bot.waiting_sessions)
        finally:
            self.bot.waiting_sessions.pop(987, None)
            self.bot.user_client_handlers.pop(987, None)


class AccountRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_accounts_are_isolated_by_owner_and_reserved(self):
        from database import Database
        from livestream.accounts import AccountManager
        with tempfile.TemporaryDirectory() as directory:
            database = Database(os.path.join(directory, "test.db"))
            own = database.add_account("session-a", "A", 1)
            other = database.add_account("session-b", "B", 2)
            self.assertEqual([r["account_id"] for r in database.list_accounts(1)], [own])
            manager = AccountManager()
            with patch("livestream.accounts.db", database), patch.object(manager, "get_client", new=AsyncMock(return_value=object())):
                first, second = await asyncio.gather(manager.pick_account(1), manager.pick_account(1))
                self.assertEqual(first[1], own)
                self.assertIsNone(second)
                self.assertEqual((await manager.pick_account(2))[1], other)

    async def test_waiting_consent_survives_database_reopen(self):
        from database import Database
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.db")
            database = Database(path)
            database.save_waiting(1, -1000000000042, "Private channel")
            reopened = Database(path)
            self.assertEqual(reopened.list_waiting(), [{"user_id": 1, "channel_id": -1000000000042, "title": "Private channel"}])
            reopened.delete_waiting(1)
            self.assertEqual(reopened.list_waiting(), [])


class RecorderCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_join_closes_encoders_and_removes_session(self):
        from livestream import recorder as module
        recorder = module.StreamRecorder()
        entered = asyncio.Event()

        async def join(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        session = SimpleNamespace(
            user_id=987, channel_id=-1000000000042,
            client=None, uploader=Mock(), muxer=Mock(), audio_mixer=Mock(),
            capture_started=threading.Event(), stop_event=threading.Event(),
            state_machine=Mock(), recording_id=-1,
        )
        call_client = SimpleNamespace(record=AsyncMock(side_effect=join))
        with patch.object(module, "RecordingSession", return_value=session), patch.object(
            module.db, "start_recording", return_value=1
        ), patch.object(module.db, "finish_recording"), patch.object(
            recorder, "_get_call_client", new=AsyncMock(return_value=call_client)
        ), patch.object(recorder, "_leave_call", new=AsyncMock()) as leave:
            task = asyncio.create_task(recorder.start_recording(987, session.channel_id, "test", object()))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertNotIn(987, recorder.active_sessions)
            self.assertTrue(session.stop_event.is_set())
            session.muxer.close_all.assert_called_once()
            leave.assert_awaited_once()


class RecorderCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_current_part_keeps_call_and_session_running(self):
        from livestream.recorder import StreamRecorder
        recorder = StreamRecorder()
        source = VideoSource(1, SourceType.CAMERA, 640, 480)
        call = SimpleNamespace(leave_call=AsyncMock())
        session = SimpleNamespace(
            user_id=987, finalizing=False, media_lock=asyncio.Lock(),
            stop_event=threading.Event(), active_video_source=source,
            audio_mixer=Mock(), muxer=Mock(), call_client=call,
            pending_actions={}, uploader=Mock(),
        )
        session.muxer.eras = [SimpleNamespace(index=0)]
        session.muxer.current_era_bytes.return_value = 4096
        session.muxer.encoder_fps = 25.0
        session.muxer.start_video_encoder.return_value = 2
        recorder.active_sessions[987] = session
        self.assertTrue(await recorder.send_current_part(987))
        self.assertIs(recorder.get_session(987), session)
        self.assertFalse(session.stop_event.is_set())
        call.leave_call.assert_not_awaited()
        self.assertEqual(recorder._event_handler._session_generation[id(session)], 2)
        session.audio_mixer.request_rotate.assert_called_once()

    async def test_send_refuses_empty_or_finalizing_recording(self):
        from livestream.recorder import StreamRecorder
        recorder = StreamRecorder()
        self.assertFalse(await recorder.send_current_part(987))
        session = SimpleNamespace(user_id=987, finalizing=False, media_lock=asyncio.Lock(),
                                  stop_event=threading.Event(), muxer=Mock())
        recorder.active_sessions[987] = session
        session.muxer.current_era_bytes.return_value = 0
        self.assertFalse(await recorder.send_current_part(987))
        session.finalizing = True
        session.muxer.current_era_bytes.return_value = 4096
        self.assertFalse(await recorder.send_current_part(987))
        session.muxer.request_rotation.assert_not_called()

    async def test_four_hour_limit_stops_recording(self):
        from livestream import recorder as module
        recorder = module.StreamRecorder()
        session = SimpleNamespace(user_id=987, finalizing=False)
        recorder.active_sessions[987] = session
        stopped = asyncio.Event()

        async def stop(*args, **kwargs):
            stopped.set()

        with patch.object(module, "MAX_RECORDING_SECONDS", 14400), patch.object(
            module.asyncio, "sleep", new=AsyncMock()
        ) as sleep, patch.object(recorder, "stop_recording", new=AsyncMock(side_effect=stop)) as stop_recording:
            await recorder._duration_limit_loop(session)
            await asyncio.wait_for(stopped.wait(), 1)
            sleep.assert_awaited_once_with(14400)
            stop_recording.assert_awaited_once_with(987, reason="4 hour recording limit")

    async def test_progress_edits_one_message_every_five_seconds(self):
        import bot
        from livestream.recorder import RecordingSession
        session = RecordingSession(987, -1000000000042, "progress")
        edits = 0

        async def edit(*args):
            nonlocal edits
            edits += 1
            if edits == 2:
                session.finalizing = True
                session.delivery_complete = True

        with patch.object(bot, "send_message", new=AsyncMock(return_value={"ok": True, "result": {"message_id": 55}})) as send, patch.object(
            bot, "edit_message_text", new=AsyncMock(side_effect=edit)
        ), patch.object(bot, "progress_text", side_effect=["recording 0", "recording 1", "recording 2"]), patch.object(bot.asyncio, "sleep", new=AsyncMock()) as sleep:
            await bot.recording_progress(session)
            self.assertEqual(session.progress_message_id, 55)
            send.assert_awaited_once()
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 5])
        if os.path.exists(session.manifest_path):
            os.remove(session.manifest_path)


class SessionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.session_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.session_directory.cleanup)
        session_path = os.path.join(self.session_directory.name, "sessions")
        setting = patch("config.SESSION_DIR", session_path)
        setting.start()
        self.addCleanup(setting.stop)

    def test_new_offline_session_wins_over_stale_mongo_after_restart(self):
        from database import Database
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.db")
            database = Database(path)
            with patch.object(database, "_mongo_sessions", return_value=None):
                self.assertTrue(database.save_session(987, "user_987", "new-session"))
            reopened = Database(path)
            collection = Mock()
            stale = {"user_id": 987, "session_string": "old-session", "session_saved_at": 1}
            collection.find_one.return_value = stale
            collection.find.return_value = [stale]
            with patch.object(reopened, "_mongo_sessions", return_value=collection):
                self.assertEqual(reopened.get_session_string(987), "new-session")
                self.assertEqual(reopened.get_all_sessions()[0]["session_string"], "new-session")

    def test_mongo_and_local_copy_survive_sqlite_failure(self):
        from database import Database
        with tempfile.TemporaryDirectory() as directory:
            database = Database(os.path.join(directory, "test.db"))
            collection = Mock()
            collection.find_one.return_value = {"user_id": 987, "session_string": "test-session"}
            with patch.object(database, "_mongo_sessions", return_value=collection), patch.object(
                database, "_get_connection", side_effect=OSError("SQLite unavailable")
            ):
                self.assertTrue(database.save_session(987, "user_987", "test-session"))
                self.assertEqual(database.get_session_string(987), "test-session")
                collection.update_one.assert_called_once()
            reopened = Database(os.path.join(directory, "test.db"))
            self.assertEqual(reopened.get_session_string(987), "test-session")
            self.assertEqual(reopened.get_all_sessions()[0]["user_id"], 987)
            path = reopened.sessions_dir / "user_987.session"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(reopened.delete_session(987))
            self.assertIsNone(reopened.get_session_string(987))

    def test_mongo_outage_keeps_local_login_available(self):
        from database import Database
        with tempfile.TemporaryDirectory() as directory:
            database = Database(os.path.join(directory, "test.db"))
            collection = Mock()
            collection.update_one.side_effect = OSError("Mongo unavailable")
            collection.find_one.side_effect = OSError("Mongo unavailable")
            with patch.object(database, "_mongo_sessions", return_value=collection):
                self.assertTrue(database.save_session(987, "user_987", "test-session"))
                self.assertEqual(database.get_session_string(987), "test-session")
            self.assertTrue((database.sessions_dir / "user_987.session").exists())


if __name__ == "__main__":
    unittest.main()
