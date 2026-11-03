import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


class MembershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import bot
        self.bot = bot

    async def test_join_public_and_private_invites(self):
        client = SimpleNamespace(join_chat=AsyncMock(return_value=SimpleNamespace(id=-1001, title='Channel')))
        for reference in ('@channel', 'https://t.me/+secret', 'https://t.me/joinchat/secret'):
            with patch.object(self.bot, 'get_user_client', AsyncMock(return_value=client)), patch.object(self.bot, 'send_message', AsyncMock()):
                await self.bot.handle_join(7, 7, '/join ' + reference)
                client.join_chat.assert_awaited_with(reference)

    async def test_approval_request_is_not_reported_as_join_success(self):
        class InviteRequestSent(Exception):
            pass
        client = SimpleNamespace(join_chat=AsyncMock(side_effect=InviteRequestSent()))
        with patch.object(self.bot, 'get_user_client', AsyncMock(return_value=client)), patch.object(self.bot, 'send_message', AsyncMock()) as send:
            await self.bot.handle_join(7, 7, '/join @channel')
            self.assertIn('tasdiqlashini', send.call_args.args[1])

    def test_private_post_link_resolves_to_chat_id(self):
        self.assertEqual(self.bot.parse_channel_input('https://t.me/c/123456/99'), -100123456)

    async def test_subscription_gate_blocks_record_but_allows_stop(self):
        def update(text):
            return {'message': {'from': {'id': 7}, 'chat': {'id': 7, 'type': 'private'}, 'text': text}}
        with patch.object(self.bot, 'SubscriptionGate') as gate, patch.object(self.bot, '_handle_update', AsyncMock()) as handle:
            gate.return_value.enforce = AsyncMock(return_value=False)
            await self.bot.handle_update(update('/record'))
            handle.assert_not_awaited()
            await self.bot.handle_update(update('/stop'))
            handle.assert_awaited_once()
            self.assertEqual(gate.return_value.enforce.await_count, 1)

    async def test_group_callback_cannot_trigger_commands(self):
        update = {'callback_query': {'from': {'id': 7}, 'message': {'chat': {'id': -100, 'type': 'group'}}, 'data': 'wait:1'}}
        with patch.object(self.bot, '_handle_update', AsyncMock()) as handle:
            await self.bot.handle_update(update)
            handle.assert_not_awaited()

    async def test_different_clients_for_same_telegram_account_cannot_record_twice(self):
        from livestream.recorder import StreamRecorder
        recorder = StreamRecorder()
        first = SimpleNamespace(me=SimpleNamespace(id=55))
        second = SimpleNamespace(me=SimpleNamespace(id=55))
        recorder.active_sessions[1] = SimpleNamespace(client=first, channel_id=-1001)
        # Even a legacy deployment that explicitly raises the config cannot bypass this.
        with patch.object(self.bot._config, 'MAX_RECORDINGS_PER_ACCOUNT', 10, create=True):
            with self.assertRaisesRegex(RuntimeError, 'capacity reached'):
                await recorder.start_recording(2, -1002, 'Other', second)
            with self.assertRaisesRegex(RuntimeError, 'already recording'):
                await recorder.start_recording(2, -1001, 'Same', second)
        self.assertEqual(len(recorder.active_sessions), 1)

    async def test_busy_failover_identity_is_skipped(self):
        client = SimpleNamespace(me=SimpleNamespace(id=55))
        session = SimpleNamespace(user_id=7, account_id=10, channel_id=-1001)
        active = SimpleNamespace(client=SimpleNamespace(me=SimpleNamespace(id=55)))
        with patch.object(self.bot.stream_recorder, 'active_sessions', {8: active}), patch.object(self.bot.account_manager, 'pick_account', AsyncMock(side_effect=[(client, 20), None])), patch.object(self.bot.account_manager, 'mark_flood'), patch.object(self.bot.account_manager, 'mark_free') as free, patch.object(self.bot, 'resolve_chat', AsyncMock()) as resolve:
            self.assertIsNone(await self.bot.recover_recording_account(session, RuntimeError('flood')))
            free.assert_called_once_with(20)
            resolve.assert_not_awaited()

    async def test_account_becoming_busy_during_failover_cannot_be_joined(self):
        from livestream.recorder import StreamRecorder
        recorder = StreamRecorder()
        replacement = SimpleNamespace(me=SimpleNamespace(id=55))
        old = SimpleNamespace(record=AsyncMock(side_effect=RuntimeError('flood')))
        session = SimpleNamespace(user_id=7, channel_id=-1001, client=object(),
                                  call_client=old, account_id=10, _last_rejoin=0, reconnects=0)
        recorder.active_sessions[7] = session
        recorder.set_recovery_callback(AsyncMock(return_value=(replacement, 20)))

        async def leave(*args):
            recorder.active_sessions[8] = SimpleNamespace(client=SimpleNamespace(me=SimpleNamespace(id=55)))

        with patch.object(recorder, '_leave_call', AsyncMock(side_effect=leave)), patch.object(recorder, '_get_call_client', AsyncMock()) as get_call, patch.object(self.bot.account_manager, 'mark_free') as free:
            with self.assertLogs('livestream.recorder', level='WARNING'):
                await recorder._try_rejoin(session, True)
            get_call.assert_not_awaited()
            free.assert_called_once_with(20)
            self.assertIs(session.call_client, old)

    async def test_cancelled_pool_start_releases_reservation(self):
        own = object()
        pool = object()
        with patch.object(self.bot.stream_recorder, 'get_session', return_value=None), patch.object(self.bot, 'resolve_chat', AsyncMock(return_value=object())), patch.object(self.bot, 'get_channel_call', AsyncMock(return_value=SimpleNamespace(id=1))), patch.object(self.bot.stream_recorder, 'start_recording', AsyncMock(side_effect=[RuntimeError('capacity reached'), asyncio.CancelledError()])), patch.object(self.bot.account_manager, 'pick_account', AsyncMock(return_value=(pool, 20))), patch.object(self.bot.account_manager, 'mark_free') as free:
            with self.assertRaises(asyncio.CancelledError):
                await self.bot.start_recording_for_user(7, 7, own, -1001, 'Channel')
            free.assert_called_once_with(20)


if __name__ == '__main__':
    unittest.main()
