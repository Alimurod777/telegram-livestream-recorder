import asyncio
import logging
import unittest
from unittest.mock import Mock, patch

from livestream.diagnostics import TelegramDiagnostics, sanitize, request_context
from livestream.subscription import SubscriptionGate


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_failure_falls_back_to_owner_without_recursion(self):
        def api(method, payload, **kwargs):
            logging.getLogger('transport').error('transport failed')
            if payload['chat_id'] == -100:
                raise OSError('unreachable')
            return {'ok': True}
        api = Mock(side_effect=api)
        handler = TelegramDiagnostics(api, channel_id=-100, owner_id=42)
        handler.start()
        logging.getLogger().addHandler(handler)
        try:
            self.assertTrue(await handler._deliver('incident'))
            await asyncio.sleep(0)
            self.assertEqual([c.args[1]['chat_id'] for c in api.call_args_list], [-100, 42])
            self.assertTrue(handler.queue.empty())
        finally:
            logging.getLogger().removeHandler(handler)
            await handler.shutdown()

    async def test_successful_channel_does_not_notify_owner(self):
        api = Mock(return_value={'ok': True})
        handler = TelegramDiagnostics(api, channel_id=-100, owner_id=42)
        self.assertTrue(await handler._deliver('incident'))
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[1]['chat_id'], -100)

    async def test_negative_api_result_also_falls_back(self):
        api = Mock(side_effect=[{'ok': False}, {'ok': True}])
        handler = TelegramDiagnostics(api, channel_id=-100, owner_id=42)
        self.assertTrue(await handler._deliver('incident'))
        self.assertEqual(api.call_count, 2)

    async def test_worker_thread_context_redaction_dedup_and_traceback(self):
        api = Mock(return_value={'ok': True})
        handler = TelegramDiagnostics(api, owner_id=42, secrets=('SECRET_HASH',))
        handler.start()
        logger = logging.getLogger('test.diagnostics')
        logger.addHandler(handler)
        context = request_context.set('user=7 update=1 command=/record')
        def fail():
            try:
                raise RuntimeError('encoder failed api_hash=SECRET_HASH')
            except RuntimeError:
                logger.exception('capture failed')
        try:
            await asyncio.to_thread(fail)
            request_context.set('user=7 update=2 command=/record')
            await asyncio.to_thread(fail)
            await asyncio.sleep(0)
            await asyncio.wait_for(handler.queue.join(), 2)
            self.assertEqual(api.call_count, 1)
            text = api.call_args.args[1]['text']
            self.assertIn('user=7', text)
            self.assertIn('Traceback', text)
            self.assertNotIn('SECRET_HASH', text)
        finally:
            request_context.reset(context)
            logger.removeHandler(handler)
            await handler.shutdown()

    def test_redaction_and_bounded_queue(self):
        text = sanitize('session_string=' + 'A' * 200 + ' password=abc https://user:pass@host 123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ12345')
        for secret in ('A' * 200, 'abc', 'user:pass', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'):
            self.assertNotIn(secret, text)
        handler = TelegramDiagnostics(Mock(), owner_id=42)
        with patch('livestream.diagnostics.logging.getLogger'):
            for n in range(700):
                handler._enqueue(str(n), 'user=7', '😃' * 5000)
        self.assertEqual(handler.queue.qsize(), 200)
        self.assertLessEqual(len(handler.seen), 500)
        self.assertLessEqual(len(handler.queue.get_nowait().encode('utf-16-le')) // 2, 4096)


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_membership_statuses(self):
        for member, expected in [({'status': 'creator'}, True), ({'status': 'member'}, True),
                                 ({'status': 'administrator'}, True), ({'status': 'left'}, False),
                                 ({'status': 'kicked'}, False),
                                 ({'status': 'restricted', 'is_member': True}, True),
                                 ({'status': 'restricted', 'is_member': False}, False)]:
            api = Mock(return_value={'ok': True, 'result': member})
            gate = SubscriptionGate(api, enabled=True, channel=-100)
            self.assertIs(await gate.check(7), expected)

    async def test_disabled_and_owner_skip_network(self):
        api = Mock()
        for gate in (SubscriptionGate(api), SubscriptionGate(api, enabled=True, owner_id=7)):
            self.assertTrue(await gate.enforce(7))
        api.assert_not_called()

    async def test_api_failure_does_not_grant_access(self):
        api = Mock(side_effect=[OSError('network'), {'ok': True}])
        gate = SubscriptionGate(api, enabled=True, channel='@test')
        with self.assertLogs('livestream.subscription', level='ERROR'):
            self.assertFalse(await gate.enforce(7))
        self.assertIn('https://t.me/test', api.call_args.args[1]['reply_markup'])


if __name__ == '__main__':
    unittest.main()
