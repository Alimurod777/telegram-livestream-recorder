"""Regression coverage for the 2026-11-17 review fixes."""
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from livestream.recorder import StreamRecorder


def stopping_session(recorder):
    session = SimpleNamespace(
        user_id=7, channel_id=-1001, account_id=10, recording_id=5, finalizing=False,
        client=SimpleNamespace(me=SimpleNamespace(id=101)), call_client=None,
        state_machine=MagicMock(), uploader=MagicMock(), muxer=MagicMock(),
        audio_mixer=MagicMock(audio_frames=10), video_frames=10,
        archive_files=["a.mp4"], output_files=[], output_sizes={},
        _rotation_task=None, _watchdog_task=None, _duration_task=None,
        stop_event=threading.Event(), stats_line=lambda: "stats",
        status="recording", error=None, is_video=True, stop_reason=None,
    )
    recorder.active_sessions[(7, -1001)] = session
    return session


class RecorderReviewFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.recorder = StreamRecorder()
        self.session = stopping_session(self.recorder)

    async def test_bookkeeping_failure_still_unregisters_session(self):
        completed = AsyncMock()
        self.recorder.set_completion_callback(completed)
        with patch("livestream.recorder.db.finish_recording", side_effect=RuntimeError("database is locked")):
            with self.assertLogs("livestream.recorder", level="ERROR"):
                await self.recorder.stop_recording((7, -1001))
        self.assertFalse(self.recorder.active_sessions)
        completed.assert_awaited_once_with(self.session)

    async def test_start_does_not_return_a_finalizing_session(self):
        self.session.finalizing = True
        with self.assertRaisesRegex(RuntimeError, "yakunlanmoqda"):
            await self.recorder.start_recording(7, -1001, "chat", SimpleNamespace(me=SimpleNamespace(id=202)))
        self.assertIs(self.recorder.active_sessions[(7, -1001)], self.session)

    async def test_rejoin_skips_stopping_session_and_leaves_late_join(self):
        call = SimpleNamespace(record=AsyncMock())
        self.session.call_client, self.session._last_rejoin, self.session.reconnects = call, 0, 0
        self.session.finalizing = True
        await self.recorder._try_rejoin(self.session, True)
        call.record.assert_not_awaited()

        async def record(*args, **kwargs):
            self.session.finalizing = True
        self.session.finalizing, self.session._last_rejoin = False, 0
        call.record = AsyncMock(side_effect=record)
        with patch.object(self.recorder, "_leave_call", AsyncMock()) as leave, \
                patch.object(self.recorder, "_notify", AsyncMock()) as notify:
            await self.recorder._try_rejoin(self.session, True)
        leave.assert_awaited_once_with(call, -1001)
        notify.assert_not_awaited()


class BotReviewFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import bot
        self.bot = bot

    async def test_waiting_entry_removed_while_arming_does_not_crash(self):
        waiting = {}

        async def send(*args, **kwargs):
            waiting.pop(7)
        with patch.object(self.bot, "waiting_sessions", waiting), \
                patch.object(self.bot.stream_recorder, "get_session", return_value=None), \
                patch.object(self.bot.db, "save_waiting"), \
                patch.object(self.bot, "send_message", AsyncMock(side_effect=send)), \
                patch.object(self.bot, "wait_probe_loop", MagicMock()) as probe:
            await self.bot.start_waiting_for_stream(7, 7, -1001, "chat")
        probe.assert_not_called()
        self.assertFalse(waiting)

    async def test_start_refuses_finalizing_session_before_reserving(self):
        stopping = SimpleNamespace(finalizing=True)
        with patch.object(self.bot.stream_recorder, "get_session", return_value=stopping), \
                patch.object(self.bot.owner_controls, "blocked", return_value=False), \
                patch.object(self.bot.account_manager, "reserve_account", AsyncMock()) as reserve:
            with self.assertRaisesRegex(RuntimeError, "yakunlanmoqda"):
                await self.bot.start_recording_for_user(
                    user_id=7, chat_id=7, client=object(), channel_id=-1001,
                    channel_title="chat", initial_account_id=3)
        reserve.assert_not_awaited()

    async def test_readding_existing_account_keeps_it_when_validation_fails(self):
        session_string = "s" * 60
        for known, deleted in (([{"account_id": 4}], False), ([], True)):
            with patch.object(self.bot.db, "list_accounts", return_value=known), \
                    patch.object(self.bot.db, "add_account", return_value=4), \
                    patch.object(self.bot.db, "delete_account") as delete, \
                    patch.object(self.bot.account_manager, "get_client", AsyncMock(return_value=None)), \
                    patch.object(self.bot, "send_message", AsyncMock()):
                await self.bot.handle_addaccount(7, 7, f"/addaccount {session_string}")
            self.assertEqual(delete.called, deleted)

    async def test_polling_conflict_from_bot_api_retries_delete_webhook(self):
        polls, sleeps = [], []

        async def get_updates(offset):
            polls.append(offset)
            if len(polls) == 1:
                raise self.bot.BotAPIError({"error_code": 409, "description": "Conflict"})
            self.bot._shutdown_event.set()
            return {"result": []}

        recorder = MagicMock()
        recorder.recover_incomplete = AsyncMock()
        recorder.cleanup_all = AsyncMock()
        recorder.wait_for_deliveries = AsyncMock()
        with ExitStack() as stack:
            for name, value in (("API_ID", 1), ("API_HASH", "test"), ("BOT_TOKEN", "test"),
                                ("stream_recorder", recorder), ("record_requests", {}),
                                ("waiting_sessions", {})):
                stack.enter_context(patch.object(self.bot, name, value))
            stack.enter_context(patch.object(self.bot, "acquire_single_instance_lock", return_value=True))
            stack.enter_context(patch.object(self.bot, "install_shutdown_handlers"))
            stack.enter_context(patch.object(self.bot, "get_ffmpeg_path", return_value="ffmpeg"))
            api = stack.enter_context(patch.object(self.bot, "bot_api", return_value={"result": {"username": "test"}}))
            stack.enter_context(patch.object(self.bot.db, "list_waiting", return_value=[]))
            stack.enter_context(patch.object(self.bot, "restore_user_clients", AsyncMock()))
            stack.enter_context(patch.object(self.bot, "shutdown_clients", AsyncMock()))
            stack.enter_context(patch.object(self.bot.mtproto_uploader, "close", AsyncMock()))
            stack.enter_context(patch.object(self.bot.account_manager, "shutdown", AsyncMock()))
            stack.enter_context(patch.object(self.bot, "UserUpdateDispatcher"))
            self.bot.UserUpdateDispatcher.return_value.close = AsyncMock()

            async def polled(offset):
                if not polls:
                    api.reset_mock()
                return await get_updates(offset)
            stack.enter_context(patch.object(self.bot, "get_updates", side_effect=polled))
            stack.enter_context(patch.object(self.bot.asyncio, "sleep", AsyncMock(side_effect=sleeps.append)))
            stack.enter_context(patch.object(self.bot, "_shutdown_event", None))
            with self.assertLogs(self.bot.logger, level="ERROR") as logs:
                await self.bot._run_bot()
        self.assertTrue(any("409 Conflict" in record.getMessage() for record in logs.records))
        self.assertTrue(all(record.exc_info is None for record in logs.records))
        self.assertIn("deleteWebhook", [call.args[0] for call in api.call_args_list])
        self.assertEqual(sleeps, [5])


if __name__ == "__main__":
    unittest.main()
