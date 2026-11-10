import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


class ScheduledWaitingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import bot
        self.bot = bot

    async def test_scheduled_call_is_not_joined_as_live(self):
        chat = SimpleNamespace(_recorder_call=object())
        client = SimpleNamespace(invoke=AsyncMock(return_value=SimpleNamespace(
            call=SimpleNamespace(schedule_date=200))))
        self.assertIsNone(await self.bot.get_channel_call(client, chat))
        self.assertEqual(chat._recorder_schedule_date, 200)
        client.invoke.return_value.call.schedule_date = None
        self.assertIs(await self.bot.get_channel_call(client, chat), chat._recorder_call)
        self.assertIsNone(chat._recorder_schedule_date)

    async def test_probe_wakes_at_schedule_then_checks_late_start_every_five_seconds(self):
        timeouts = []

        async def wait(awaitable, timeout):
            timeouts.append(timeout)
            awaitable.close()
            raise asyncio.TimeoutError()

        with patch.object(self.bot.time, "time", return_value=100), \
                patch.object(self.bot, "WAIT_PROBE_INTERVAL", 120), \
                patch.object(self.bot.asyncio, "wait_for", side_effect=wait):
            await self.bot.wait_for_next_probe({"schedule_date": 130})
            await self.bot.wait_for_next_probe({"schedule_date": 90})
            await self.bot.wait_for_next_probe({"schedule_date": 1000})
            await self.bot.wait_for_next_probe({})
        self.assertEqual(timeouts, [30, 5, 120, 120])

    async def test_rescheduling_wakes_sleeping_probe(self):
        waiting = {"schedule_date": 1000, "wake": asyncio.Event()}
        task = asyncio.create_task(self.bot.wait_for_next_probe(waiting))
        await asyncio.sleep(0)
        self.bot.update_wait_schedule(waiting, datetime.fromtimestamp(200, timezone.utc))
        await asyncio.wait_for(task, 1)
        self.assertEqual(waiting["schedule_date"], 200)
        self.assertFalse(waiting["wake"].is_set())

    async def test_schedule_service_update_only_wakes_matching_wait(self):
        waiting = {"channel_id": -1002, "title": "B", "wake": asyncio.Event()}
        client = SimpleNamespace(add_handler=MagicMock())
        with patch.dict(self.bot.user_client_handlers, {}, clear=True), \
                patch.dict(self.bot.waiting_sessions, {7: waiting}, clear=True), \
                patch.object(self.bot, "MessageHandler", side_effect=lambda callback, *_: SimpleNamespace(callback=callback)), \
                patch.object(self.bot, "send_message", AsyncMock()), \
                patch.object(self.bot, "start_recording_for_user", AsyncMock()) as start:
            self.bot.attach_user_client_handlers(7, client)
            handler = client.add_handler.call_args_list[0].args[0]
            message = SimpleNamespace(chat=SimpleNamespace(id=-1003),
                video_chat_started=None, video_chat_ended=None,
                video_chat_scheduled=SimpleNamespace(start_date=200))
            await handler.callback(client, message)
            self.assertFalse(waiting["wake"].is_set())
            message.chat.id = -1002
            await handler.callback(client, message)
            self.assertTrue(waiting["wake"].is_set())
            self.assertEqual(waiting["schedule_date"], 200)
            start.assert_not_awaited()

    async def test_restored_wait_rediscovers_schedule_and_starts_only_when_live(self):
        waiting = {"channel_id": -1002, "title": "B", "chat_id": 7,
                   "account_id": 20}
        scheduled = SimpleNamespace(_recorder_schedule_date=200)
        active = SimpleNamespace(_recorder_schedule_date=None)
        client, call = object(), object()
        delays = []

        async def next_probe(state):
            delays.append(state["schedule_date"])

        with patch.dict(self.bot.waiting_sessions, {7: waiting}, clear=True), \
                patch.object(self.bot.stream_recorder, "get_session", return_value=None), \
                patch.object(self.bot, "get_user_client", AsyncMock(return_value=client)), \
                patch.object(self.bot, "resolve_record_target", AsyncMock(side_effect=[
                    (client, 20, scheduled, None), (client, 20, scheduled, None),
                    (client, 20, active, call)])) as resolve, \
                patch.object(self.bot, "start_recording_for_user", AsyncMock()) as start, \
                patch.object(self.bot, "send_message", AsyncMock()), \
                patch.object(self.bot.db, "delete_waiting") as delete, \
                patch.object(self.bot, "wait_for_next_probe", side_effect=next_probe):
            await self.bot.wait_probe_loop(7, -1002)
            self.assertNotIn(7, self.bot.waiting_sessions)
        self.assertEqual(delays, [200, 200])
        self.assertEqual(resolve.await_count, 3)
        start.assert_awaited_once()
        self.assertIs(start.call_args.kwargs["known_call"], call)
        self.assertEqual(start.call_args.kwargs["initial_account_id"], 20)
        self.assertTrue(start.call_args.kwargs["automatic"])
        delete.assert_called_once_with(7)

    async def test_failed_start_keeps_waiting_and_retries_without_repeated_error_notice(self):
        waiting = {"channel_id": -1002, "title": "B", "chat_id": 7}
        client, call = object(), object()
        waits = []

        async def next_probe(state):
            self.assertIs(self.bot.waiting_sessions[7], waiting)
            waits.append(state)

        with patch.dict(self.bot.waiting_sessions, {7: waiting}, clear=True), \
                patch.object(self.bot.stream_recorder, "get_session", return_value=None), \
                patch.object(self.bot, "get_user_client", AsyncMock(return_value=client)), \
                patch.object(self.bot, "resolve_record_target", AsyncMock(return_value=(
                    client, None, SimpleNamespace(_recorder_schedule_date=None), call))), \
                patch.object(self.bot, "start_recording_for_user", AsyncMock(side_effect=[
                    RuntimeError('connection timed out'), RuntimeError('connection timed out'), object()])) as start, \
                patch.object(self.bot, "send_message", AsyncMock()) as send, \
                patch.object(self.bot.db, "delete_waiting") as delete, \
                patch.object(self.bot, "wait_for_next_probe", side_effect=next_probe):
            await self.bot.wait_probe_loop(7, -1002)
        self.assertEqual(len(waits), 2)
        self.assertEqual(start.await_count, 3)
        notices = [args.args[1] for args in send.await_args_list if '⚠️' in args.args[1]]
        self.assertEqual(len(notices), 1)
        delete.assert_called_once_with(7)

    async def test_probe_cancellation_does_not_start_recording(self):
        waiting = {"channel_id": -1002, "title": "B", "chat_id": 7, "schedule_date": 1}
        task = asyncio.create_task(self.bot.wait_for_next_probe(waiting))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
