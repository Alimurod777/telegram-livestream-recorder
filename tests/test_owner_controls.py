import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from database import Database
from livestream.chat_log import ChatLogger, LogPolicy
from livestream.owner import OwnerControls, MaintenanceActive


def update(text, uid=7):
    return {'update_id': 1, 'message': {'message_id': 2, 'text': text,
            'chat': {'id': uid, 'type': 'private'}, 'from': {'id': uid}}}


class OwnerControlsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import bot
        self.bot = bot
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(os.path.join(self.temp.name, 'settings.db'))
        self.config = SimpleNamespace(OWNER_ID=1, MAINTENANCE_MODE=False, CHAT_LOG_CHANNEL_ID=-1002)
        self.logs = ChatLogger(1)
        self.controls = OwnerControls(self.db, self.config, self.logs)
        await self.controls.load()
        self.send = AsyncMock()
        self.api = Mock()
        self.recorder = SimpleNamespace(active_sessions={(7, -1001): object()})

    async def command(self, text, uid=1):
        return await self.controls.handle(uid, text, self.api, self.send, self.recorder, {})

    async def test_non_owner_cannot_change_settings_or_get_stats(self):
        for text in ('/maintenance on', '/logchannel remove', '/stats', '/owner'):
            await self.command(text, 7)
        self.assertFalse(self.controls.maintenance)
        self.assertEqual(self.logs.policy.chat_id, -1002)
        self.assertEqual(self.db.get_owner_settings(), {})
        self.api.assert_not_called()

    async def test_maintenance_survives_restart_and_existing_sessions_remain(self):
        active = dict(self.recorder.active_sessions)
        await self.command('/maintenance on')
        restored = OwnerControls(Database(self.db.db_path), self.config, ChatLogger(1))
        await restored.load()
        self.assertTrue(restored.blocked(7))
        self.assertFalse(restored.blocked(1))
        self.assertEqual(self.recorder.active_sessions, active)
        await self.command('/maintenance off')
        self.assertFalse(self.controls.blocked(7))

    async def test_failed_persistence_does_not_enable_maintenance(self):
        with patch.object(self.db, 'set_owner_setting', side_effect=OSError()):
            await self.command('/maintenance on')
        self.assertFalse(self.controls.maintenance)

    async def test_log_destination_validation_and_persistence(self):
        self.api.side_effect = [
            {'ok': True, 'result': {'id': -1009, 'type': 'channel'}},
            {'ok': True, 'result': {'id': 99}},
            {'ok': True, 'result': {'status': 'administrator', 'can_post_messages': True}},
        ]
        await self.command('/logchannel set @logs')
        self.assertEqual(self.logs.policy, LogPolicy(-1009, True))
        self.assertEqual([c.args[0] for c in self.api.call_args_list], ['getChat', 'getMe', 'getChatMember'])
        await self.command('/logchannel exclude 7')
        await self.command('/logchannel off')
        restored = OwnerControls(self.db, self.config, ChatLogger(1))
        await restored.load()
        self.assertEqual(restored.chat_logger.policy, LogPolicy(-1009, False, (7,)))

    async def test_non_admin_destination_does_not_replace_existing_channel(self):
        self.api.side_effect = [
            {'ok': True, 'result': {'id': -1009, 'type': 'channel'}},
            {'ok': True, 'result': {'id': 99}},
            {'ok': True, 'result': {'status': 'member'}},
        ]
        await self.command('/logchannel set -1009')
        self.assertEqual(self.logs.policy.chat_id, -1002)
        self.assertNotIn('chat_log', self.db.get_owner_settings())

    async def test_remove_overrides_env_and_reset_retains_exclusions(self):
        await self.command('/logchannel exclude 7')
        await self.command('/logchannel remove')
        restored = OwnerControls(self.db, self.config, ChatLogger(1))
        await restored.load()
        self.assertFalse(restored.chat_logger.policy.enabled)
        self.config.CHAT_LOG_CHANNEL_ID = 0
        await self.command('/logchannel reset')
        self.config.CHAT_LOG_CHANNEL_ID = -1003
        restored = OwnerControls(self.db, self.config, ChatLogger(1))
        await restored.load()
        self.assertEqual(restored.chat_logger.policy, LogPolicy(-1003, True, (7,)))

    async def test_maintenance_gates_commands_and_callbacks_before_subscription_or_login(self):
        self.controls.maintenance = True
        callback = {'callback_query': {'id': 'x', 'from': {'id': 7},
                    'message': {'chat': {'id': 7, 'type': 'private'}}, 'data': 'wait:-1002'}}
        with patch.object(self.bot, 'owner_controls', self.controls), \
                patch.object(self.bot, 'send_message', self.send), \
                patch.object(self.bot, 'answer_callback_query', AsyncMock()) as answer, \
                patch.object(self.bot, '_handle_update', AsyncMock()) as handle, \
                patch.object(self.bot.SubscriptionGate, 'enforce', AsyncMock(return_value=True)) as gate:
            for request in (update('/record -1001'), update('/login_session secret'), update('12345'), callback):
                await self.bot.handle_update(request)
            handle.assert_not_awaited()
            gate.assert_not_awaited()
            answer.assert_awaited_once()
            for command in ('/stop', '/status', '/send', '/cancel', '/logout', '/help'):
                await self.bot.handle_update(update(command))
            await self.bot.handle_update(update('/record -1001', 1))
            self.assertEqual(handle.await_count, 7)

    async def test_direct_automatic_start_cannot_bypass_maintenance(self):
        self.controls.maintenance = True
        with patch.object(self.bot, 'owner_controls', self.controls), \
                patch.object(self.bot.stream_recorder, 'start_recording', AsyncMock()) as start:
            with self.assertRaises(MaintenanceActive):
                await self.bot.start_recording_for_user(7, 7, object(), -1001, 'test')
        start.assert_not_awaited()

    async def test_waiting_probe_is_preserved_without_telegram_requests(self):
        self.controls.maintenance = True
        state = {'channel_id': -1002, 'title': 'test', 'chat_id': 7}
        with patch.object(self.bot, 'owner_controls', self.controls), \
                patch.dict(self.bot.waiting_sessions, {7: state}, clear=True), \
                patch.object(self.bot, 'wait_for_next_probe', AsyncMock(side_effect=asyncio.CancelledError())), \
                patch.object(self.bot, 'get_user_client', AsyncMock()) as client:
            with self.assertRaises(asyncio.CancelledError):
                await self.bot.wait_probe_loop(7, -1002)
            self.assertIs(self.bot.waiting_sessions[7], state)
        client.assert_not_awaited()


class ConversationLogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.logs = ChatLogger(1, ('known-secret',))
        self.api = Mock(return_value={'ok': True, 'result': {}})
        self.logs.start(self.api)
        self.addAsyncCleanup(self.logs.stop)
        await self.logs.configure(LogPolicy(-1001, True))

    async def test_forward_and_header_use_bot_api_without_downloading_media(self):
        self.logs.submit(update('hello')['message'])
        await asyncio.wait_for(self.logs.queue.join(), 2)
        self.assertEqual(self.logs.sent, 1)
        self.assertEqual([c.args[0] for c in self.api.call_args_list], ['sendMessage', 'forwardMessage'])
        self.assertEqual(self.api.call_args.args[1], {'chat_id': -1001, 'from_chat_id': 7, 'message_id': 2})

    async def test_owner_commands_auth_codes_sessions_and_progress_never_queue(self):
        for text in ('/login_session secret', '12345', '+998901234567', 'a' * 150):
            self.logs.submit(update(text)['message'])
        self.logs.submit(update('private', 1)['message'])
        self.logs.submit(update('password')['message'], sensitive=True)
        self.logs.submit(update('Yozuv boshlanganiga: 00:00:01')['message'], outbound=True)
        self.assertTrue(self.logs.queue.empty())
        self.api.assert_not_called()

    async def test_sensitive_text_is_masked_instead_of_forwarded(self):
        self.logs.submit(update('password=known-secret')['message'])
        await asyncio.wait_for(self.logs.queue.join(), 2)
        self.assertNotIn('known-secret', str(self.api.call_args_list))
        self.assertNotIn('forwardMessage', str(self.api.call_args_list))

    async def test_policy_change_discards_queued_events_and_excluded_users(self):
        self.logs.submit(update('hello')['message'])
        await self.logs.configure(LogPolicy(-1002, True, (7,)))
        self.logs.submit(update('again')['message'])
        await asyncio.wait_for(self.logs.queue.join(), 2)
        self.api.assert_not_called()
        self.assertEqual(self.logs.dropped, 1)

    async def test_logging_failure_does_not_break_delivery_or_stop_worker(self):
        self.api.side_effect = OSError('offline')
        self.logs.submit(update('hello')['message'])
        await asyncio.wait_for(self.logs.queue.join(), 2)
        self.assertEqual(self.logs.failed, 1)
        self.assertFalse(self.logs.task.done())

    async def test_duplicate_events_and_overflow_remain_bounded(self):
        message = update('hello')['message']
        self.logs.submit(message)
        self.logs.submit(message)
        self.assertEqual(self.logs.queue.qsize(), 1)
        for number in range(3, 220):
            self.logs.submit(dict(message, message_id=number))
        self.assertEqual(self.logs.queue.qsize(), 200)
        self.assertGreater(self.logs.dropped, 0)
