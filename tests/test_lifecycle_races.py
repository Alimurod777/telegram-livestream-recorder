"""Regression coverage for cancellation, reservation transfer and stop races."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from livestream.recorder import StreamRecorder


class LifecycleRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import bot
        self.bot = bot
        self.recorder = StreamRecorder()
        self.old_client = SimpleNamespace(me=SimpleNamespace(id=101))
        self.new_client = SimpleNamespace(me=SimpleNamespace(id=202))
        self.old_call = SimpleNamespace(record=AsyncMock(side_effect=RuntimeError('flood')))
        self.session = SimpleNamespace(user_id=7, channel_id=-1001, account_id=10,
                                       client=self.old_client, call_client=self.old_call,
                                       finalizing=False, _last_rejoin=0, reconnects=0)
        self.recorder.active_sessions[(7, -1001)] = self.session

    async def test_cancelled_recovery_resolution_releases_only_new_reservation(self):
        with patch.object(self.bot, 'stream_recorder', self.recorder), patch.object(self.bot.account_manager, 'pick_account', AsyncMock(return_value=(self.new_client, 20))), patch.object(self.bot.account_manager, 'mark_free') as free, patch.object(self.bot.account_manager, 'mark_flood'), patch.object(self.bot, 'resolve_chat', AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await self.bot.recover_recording_account(self.session, RuntimeError('flood'))
            free.assert_called_once_with(20)
            self.assertEqual(self.session.account_id, 10)

    async def test_recovery_selection_keeps_previous_reservation_until_adoption(self):
        with patch.object(self.bot, 'stream_recorder', self.recorder), patch.object(self.bot.account_manager, 'pick_account', AsyncMock(return_value=(self.new_client, 20))), patch.object(self.bot.account_manager, 'mark_free') as free, patch.object(self.bot.account_manager, 'mark_flood'), patch.object(self.bot, 'resolve_chat', AsyncMock()):
            self.assertEqual(await self.bot.recover_recording_account(self.session, RuntimeError('flood')), (self.new_client, 20))
            free.assert_not_called()

    async def test_failed_or_cancelled_replacement_preparation_releases_new_account(self):
        for error in (RuntimeError('prepare failed'), asyncio.CancelledError()):
            self.session._last_rejoin = 0
            self.recorder.set_recovery_callback(AsyncMock(return_value=(self.new_client, 20)))
            with patch.object(self.recorder, '_leave_call', AsyncMock()), patch.object(self.recorder, '_get_call_client', AsyncMock(side_effect=error)), patch.object(self.bot.account_manager, 'mark_free') as free:
                with self.assertRaises(type(error)):
                    await self.recorder._try_rejoin(self.session, True)
                free.assert_called_once_with(20)
                self.assertEqual(self.session.account_id, 10)
                self.assertIs(self.session.client, self.old_client)
                self.assertIs(self.session.call_client, self.old_call)

    async def test_successful_adoption_releases_previous_account_once(self):
        new_call = SimpleNamespace(record=AsyncMock())
        self.recorder.set_recovery_callback(AsyncMock(return_value=(self.new_client, 20)))
        with patch.object(self.recorder, '_leave_call', AsyncMock()), patch.object(self.recorder, '_get_call_client', AsyncMock(return_value=new_call)), patch.object(self.bot.account_manager, 'mark_free') as free:
            await self.recorder._try_rejoin(self.session, True)
            free.assert_called_once_with(10)
            self.assertEqual(self.session.account_id, 20)
            self.assertIs(self.session.client, self.new_client)
            self.assertIs(self.session.call_client, new_call)
            new_call.record.assert_awaited_once()

    async def test_stop_during_recovery_does_not_adopt_reserved_account(self):
        async def select(*args):
            self.session.finalizing = True
            return self.new_client, 20
        self.recorder.set_recovery_callback(select)
        with patch.object(self.recorder, '_get_call_client', AsyncMock()) as prepare, patch.object(self.bot.account_manager, 'mark_free') as free:
            await self.recorder._try_rejoin(self.session, True)
            prepare.assert_not_awaited()
            free.assert_called_once_with(20)
            self.assertEqual(self.session.account_id, 10)

    async def test_cancelled_stop_waiter_does_not_duplicate_finalization(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def finalize(session, reason):
            entered.set()
            await release.wait()
            return session
        with patch.object(self.recorder, '_finalize_session', AsyncMock(side_effect=finalize)) as finalize_mock:
            first = asyncio.create_task(self.recorder.stop_recording((7, -1001)))
            await asyncio.wait_for(entered.wait(), 1)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            self.assertEqual(len(self.recorder._stop_tasks), 1)
            second = asyncio.create_task(self.recorder.stop_recording((7, -1001)))
            await asyncio.sleep(0)
            self.assertEqual(finalize_mock.await_count, 1)
            release.set()
            self.assertIs(await asyncio.wait_for(second, 1), self.session)
            self.assertFalse(self.recorder._stop_tasks)

    async def test_new_recording_can_stop_while_old_same_chat_is_delivering(self):
        entered, release = asyncio.Event(), asyncio.Event()
        newer = SimpleNamespace(user_id=7, channel_id=-1001)
        async def finalize(session, reason):
            if session is self.session:
                self.recorder.active_sessions.pop((7, -1001))
                entered.set()
                await release.wait()
            return session
        with patch.object(self.recorder, '_finalize_session', AsyncMock(side_effect=finalize)):
            first = asyncio.create_task(self.recorder.stop_recording((7, -1001)))
            await asyncio.wait_for(entered.wait(), 1)
            self.recorder.active_sessions[(7, -1001)] = newer
            self.assertIs(await self.recorder.stop_recording((7, -1001)), newer)
            self.assertFalse(first.done())
            release.set()
            self.assertIs(await first, self.session)

    async def test_stop_all_includes_recording_committed_during_start_cancellation(self):
        active = []
        entered = asyncio.Event()
        async def pending():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Capture can commit just before a notification gets cancelled.
                active.append(self.session)
                raise
        task = asyncio.create_task(pending())
        await entered.wait()
        with patch.dict(self.bot.record_requests, {7: task}), patch.object(self.bot.stream_recorder, 'get_sessions', side_effect=lambda owner: list(active)), patch.object(self.bot, 'stop_recording_background', AsyncMock()) as stop, patch.object(self.bot, 'send_message', AsyncMock()):
            await self.bot.handle_stop(7, 7, '/stop all')
            await asyncio.sleep(0)
            stop.assert_awaited_once_with(7, -1001)
            self.assertTrue(task.cancelled())

    async def test_cancellation_after_capture_start_keeps_pool_reservation(self):
        entered = asyncio.Event()
        async def notification(*args):
            entered.set()
            await asyncio.Event().wait()
        session = SimpleNamespace()
        with patch.object(self.bot.stream_recorder, 'get_session', return_value=None), patch.object(self.bot.account_manager, 'reserve_account', AsyncMock(return_value=(self.new_client, 20))), patch.object(self.bot.stream_recorder, 'start_recording', AsyncMock(return_value=session)), patch.object(self.bot.account_manager, 'mark_free') as free, patch.object(self.bot, 'recording_progress', AsyncMock()), patch.object(self.bot, 'send_message', AsyncMock(side_effect=notification)):
            task = asyncio.create_task(self.bot.start_recording_for_user(7, 7, self.new_client, -1001, 'A', automatic=True, known_call=SimpleNamespace(id=1), initial_account_id=20))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            free.assert_not_called()


if __name__ == '__main__':
    unittest.main()
