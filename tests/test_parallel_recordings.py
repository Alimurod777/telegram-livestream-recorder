import asyncio
from contextlib import ExitStack
import threading
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from livestream import recorder as module


def fake_session(user_id, channel_id, title):
    return SimpleNamespace(
        user_id=user_id, channel_id=channel_id, channel_title=title,
        client=None, account_id=None, call_client=None, recording_id=-1,
        telegram_call_id=None, muxer=Mock(), uploader=Mock(), audio_mixer=Mock(audio_frames=1),
        capture_started=threading.Event(), stop_event=threading.Event(), state_machine=Mock(),
        finalizing=False, video_frames=1, output_files=[], archive_files=['retained'],
        output_sizes={}, error=None, status='recording', is_video=True,
        stats_line=lambda: 'test stats',
    )


class ParallelRecorderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import bot
        self.bot = bot
        self.recorder = module.StreamRecorder()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(module, 'RecordingSession', side_effect=fake_session))
        self.stack.enter_context(patch.object(module.db, 'start_recording', side_effect=range(1, 100)))
        self.stack.enter_context(patch.object(module.db, 'finish_recording'))
        for name in ('_rotation_loop', '_watchdog_loop', '_duration_limit_loop', '_leave_call'):
            self.stack.enter_context(patch.object(self.recorder, name, AsyncMock()))
        self.call_clients = {}

        async def get_call(user_id, client):
            return self.call_clients.setdefault(id(client), SimpleNamespace(record=AsyncMock()))
        self.stack.enter_context(patch.object(self.recorder, '_get_call_client', AsyncMock(side_effect=get_call)))
        self.stack.enter_context(patch.object(self.bot, 'stream_recorder', self.recorder))
        self.stack.enter_context(patch.object(self.bot, 'recording_progress', AsyncMock()))
        self.stack.enter_context(patch.object(self.bot, 'send_message', AsyncMock()))
        self.primary = SimpleNamespace(me=SimpleNamespace(id=101))
        self.extra = SimpleNamespace(me=SimpleNamespace(id=202))

    async def test_one_owner_two_accounts_two_chats_and_third_request_is_rejected(self):
        first = await self.recorder.start_recording(7, -1001, 'A', self.primary)
        second = await self.recorder.start_recording(7, -1002, 'B', self.extra, account_id=20)
        self.assertEqual(len(self.recorder.get_sessions(7)), 2)
        self.assertIsNone(self.recorder.get_session(7))
        self.assertIs(self.recorder.get_session(7, -1002), second)
        self.assertIs(self.recorder.get_session((7, -1001)), first)
        with self.assertRaisesRegex(RuntimeError, 'capacity reached'):
            await self.recorder.start_recording(7, -1003, 'C', self.primary)
        with self.assertRaisesRegex(RuntimeError, 'capacity reached'):
            await self.recorder.start_recording(8, -1003, 'C', SimpleNamespace(me=SimpleNamespace(id=202)))
        self.assertEqual(len(self.recorder.active_sessions), 2)

    async def test_repeating_same_chat_returns_existing_recording(self):
        first = await self.recorder.start_recording(7, -1001, 'A', self.primary)
        again = await self.recorder.start_recording(7, -1001, 'A', self.extra)
        self.assertIs(first, again)
        self.assertEqual(len(self.recorder.active_sessions), 1)

    async def test_busy_primary_automatically_uses_second_owned_account(self):
        call = SimpleNamespace(id=55)
        with patch.object(self.bot, 'resolve_chat', AsyncMock(return_value=object())), patch.object(self.bot, 'get_channel_call', AsyncMock(return_value=call)), patch.object(self.bot.account_manager, 'pick_account', AsyncMock(return_value=(self.extra, 20))), patch.object(self.bot.account_manager, 'mark_free') as free:
            first = await self.bot.start_recording_for_user(7, 7, self.primary, -1001, 'A', known_call=call)
            second = await self.bot.start_recording_for_user(7, 7, self.primary, -1002, 'B', known_call=call)
            self.assertIs(first.client, self.primary)
            self.assertIs(second.client, self.extra)
            self.assertEqual(second.account_id, 20)
            self.assertEqual(len(self.recorder.get_sessions(7)), 2)
            free.assert_not_called()

    async def test_stopping_one_recording_preserves_the_other(self):
        first = await self.recorder.start_recording(7, -1001, 'A', self.primary)
        second = await self.recorder.start_recording(7, -1002, 'B', self.extra)
        await self.recorder.stop_recording((7, -1001))
        self.assertTrue(first.stop_event.is_set())
        self.assertFalse(second.stop_event.is_set())
        self.assertEqual(self.recorder.get_sessions(7), [second])
        self.assertTrue(self.recorder.is_recording(7))
        self.assertNotIn((7, -1001), self.recorder.active_sessions)

    async def test_concurrent_start_with_same_account_reserves_only_one_chat(self):
        results = await asyncio.gather(
            self.recorder.start_recording(7, -1001, 'A', self.primary),
            self.recorder.start_recording(7, -1002, 'B', self.primary), return_exceptions=True)
        self.assertEqual(sum(isinstance(x, RuntimeError) for x in results), 1)
        self.assertEqual(len(self.recorder.active_sessions), 1)

    async def test_stop_and_send_require_selection_and_enforce_owner(self):
        first = await self.recorder.start_recording(7, -1001, 'A', self.primary)
        second = await self.recorder.start_recording(7, -1002, 'B', self.extra)
        with patch.object(self.bot, 'stop_recording_background', AsyncMock()) as stop, patch.object(self.recorder, 'send_current_part', AsyncMock(return_value=True)) as send:
            await self.bot.handle_stop(7, 7)
            await self.bot.handle_send(7, 7)
            stop.assert_not_awaited()
            send.assert_not_awaited()
            await self.bot.handle_stop(7, 7, f'/stop #{first.recording_id}')
            await asyncio.sleep(0)
            stop.assert_awaited_once_with(7, -1001)
            await self.bot.handle_send(7, 7, '/send -1002')
            send.assert_awaited_once_with((7, -1002))
            stop.reset_mock()
            await self.bot.handle_stop(8, 8, f'/stop #{first.recording_id}')
            stop.assert_not_awaited()
            await self.bot.handle_stop(7, 7, '/stop all')
            await asyncio.sleep(0)
            self.assertEqual(stop.await_count, 2)

    async def test_simultaneous_stop_tasks_are_separate_per_recording(self):
        await self.recorder.start_recording(7, -1001, 'A', self.primary)
        await self.recorder.start_recording(7, -1002, 'B', self.extra)
        results = await asyncio.gather(self.recorder.stop_recording((7, -1001)), self.recorder.stop_recording((7, -1002)))
        self.assertEqual({s.channel_id for s in results}, {-1001, -1002})
        self.assertFalse(self.recorder.active_sessions)

    async def test_private_chat_can_be_resolved_by_extra_account(self):
        chat = SimpleNamespace(id=-1002, title='Private')
        with patch.object(self.bot, 'resolve_chat', AsyncMock(side_effect=[RuntimeError('CHANNEL_PRIVATE'), chat])), patch.object(self.bot, 'get_channel_call', AsyncMock(return_value=SimpleNamespace(id=55))), patch.object(self.bot.account_manager, 'healthy_accounts', AsyncMock(return_value=[{'account_id': 20}])), patch.object(self.bot.account_manager, 'get_client', AsyncMock(return_value=self.extra)):
            client, account_id, result, call = await self.bot.resolve_record_target(7, self.primary, -1002)
            self.assertIs(client, self.extra)
            self.assertEqual(account_id, 20)
            self.assertIs(result, chat)

    async def test_duration_limit_stops_only_its_recording(self):
        first = await self.recorder.start_recording(7, -1001, 'A', self.primary)
        second = await self.recorder.start_recording(7, -1002, 'B', self.extra)
        with patch.object(module.asyncio, 'sleep', AsyncMock()), patch.object(self.recorder, 'stop_recording', AsyncMock()) as stop:
            await module.StreamRecorder._duration_limit_loop(self.recorder, second)
            # Yield without the patched asyncio.sleep.
            await asyncio.wait_for(asyncio.create_task(self._yield_once()), 1)
            stop.assert_awaited_once_with((7, -1002), reason='4 hour recording limit')
        self.assertFalse(first.stop_event.is_set())

    async def _yield_once(self):
        return None

    async def test_other_owners_pool_account_cannot_be_selected(self):
        with patch.object(self.bot.db, 'list_accounts', return_value=[{'account_id': 99}]), patch.object(self.bot.account_manager, 'get_client', AsyncMock()) as client:
            with self.assertRaisesRegex(ValueError, 'tegishli emas'):
                await self.bot.owned_account_client(7, 20)
            client.assert_not_awaited()

    async def test_waiting_is_allowed_alongside_another_active_recording(self):
        await self.recorder.start_recording(7, -1001, 'A', self.primary)
        with patch.object(self.bot.db, 'save_waiting') as save, patch.object(self.bot, 'wait_probe_loop', AsyncMock()):
            try:
                await self.bot.start_waiting_for_stream(7, 7, -1002, 'B', 20)
                save.assert_called_once_with(7, -1002, 'B', 20)
                self.assertEqual(self.bot.waiting_sessions[7]['account_id'], 20)
                self.assertEqual(len(self.recorder.get_sessions(7)), 1)
            finally:
                waiting = self.bot.waiting_sessions.pop(7, None)
                if waiting and waiting.get('task'):
                    waiting['task'].cancel()
                    await asyncio.gather(waiting['task'], return_exceptions=True)


class PoolReservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_account_reservation_is_owned_and_atomic(self):
        from database import Database
        from livestream.accounts import AccountManager
        with tempfile.TemporaryDirectory() as directory:
            database = Database(directory + '/test.db')
            account = database.add_account('session-a', 'A', 7)
            manager = AccountManager()
            with patch('livestream.accounts.db', database), patch.object(manager, 'get_client', AsyncMock(return_value=object())):
                self.assertIsNone(await manager.reserve_account(8, account))
                results = await asyncio.gather(manager.reserve_account(7, account), manager.reserve_account(7, account))
                self.assertEqual(sum(result is not None for result in results), 1)
                self.assertEqual(database.get_account_active(account), 1)
                manager.mark_free(account)
                self.assertIsNotNone(await manager.reserve_account(7, account))

    def test_waiting_selected_account_survives_database_reopen(self):
        from database import Database
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/test.db'
            database = Database(path)
            database.save_waiting(7, -1002, 'Private', 20)
            reopened = Database(path)
            self.assertEqual(reopened.list_waiting(), [{'user_id': 7, 'channel_id': -1002, 'title': 'Private', 'account_id': 20}])


if __name__ == '__main__':
    unittest.main()
