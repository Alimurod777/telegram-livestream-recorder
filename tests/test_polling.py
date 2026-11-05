import unittest
import urllib.error
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch


class PollingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import bot
        self.bot = bot

    async def test_timeout_retry_preserves_offset_and_http_margin(self):
        response = {"ok": True, "result": [{"update_id": 42}]}
        with patch.object(self.bot, "bot_api", side_effect=[TimeoutError(), response]) as api:
            with self.assertRaises(TimeoutError):
                await self.bot.get_updates(42)
            self.assertEqual(await self.bot.get_updates(42), response)
        for call in api.call_args_list:
            self.assertEqual(call.args[1]["offset"], 42)
            self.assertGreaterEqual(call.kwargs["timeout"], call.args[1]["timeout"] + 10)

    async def test_connection_timeout_is_normalized_for_polling_retry(self):
        with patch.object(self.bot, "bot_api", side_effect=urllib.error.URLError(TimeoutError())):
            with self.assertRaises(TimeoutError):
                await self.bot.get_updates(None)

    async def test_other_network_errors_keep_their_cause(self):
        error = urllib.error.URLError("DNS failure")
        with patch.object(self.bot, "bot_api", side_effect=error):
            with self.assertRaises(urllib.error.URLError) as raised:
                await self.bot.get_updates(42)
        self.assertIs(raised.exception, error)

    async def test_first_poll_timeout_retries_immediately_and_preserves_offset(self):
        await self.check_poll_recovery(TimeoutError(), "polling timed out")

    async def test_connection_reset_retries_without_error_incidents(self):
        await self.check_poll_recovery(
            urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer")),
            "polling connection interrupted")

    async def test_direct_connection_reset_retries(self):
        await self.check_poll_recovery(ConnectionResetError(104, "Connection reset by peer"),
                                       "polling connection interrupted")

    async def test_poll_network_error_is_logged_only_by_retry_loop(self):
        error = urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
        with patch.object(self.bot.urllib.request, "urlopen", side_effect=error), \
                patch.object(self.bot.logger, "error") as log_error:
            with self.assertRaises(urllib.error.URLError):
                await self.bot.get_updates(43)
        log_error.assert_not_called()

    async def check_poll_recovery(self, error, message):
        polls = []
        sleeps = []

        async def get_updates(offset):
            polls.append(offset)
            if len(polls) == 1:
                return {"result": [{"update_id": 42}]}
            if len(polls) < 4:
                raise error
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
            stack.enter_context(patch.object(self.bot, "bot_api", return_value={"result": {"username": "test"}}))
            stack.enter_context(patch.object(self.bot.db, "list_waiting", return_value=[]))
            stack.enter_context(patch.object(self.bot, "restore_user_clients", AsyncMock()))
            stack.enter_context(patch.object(self.bot, "shutdown_clients", AsyncMock()))
            stack.enter_context(patch.object(self.bot.mtproto_uploader, "close", AsyncMock()))
            stack.enter_context(patch.object(self.bot.account_manager, "shutdown", AsyncMock()))
            stack.enter_context(patch.object(self.bot, "UserUpdateDispatcher"))
            self.bot.UserUpdateDispatcher.return_value.close = AsyncMock()
            stack.enter_context(patch.object(self.bot, "get_updates", side_effect=get_updates))
            stack.enter_context(patch.object(self.bot.asyncio, "sleep", AsyncMock(side_effect=sleeps.append)))
            stack.enter_context(patch.object(self.bot, "_shutdown_event", None))
            with self.assertLogs(self.bot.logger, level="INFO") as logs:
                await self.bot._run_bot()
        timeouts = [record for record in logs.records if message in record.getMessage()]
        self.assertEqual([record.levelname for record in timeouts], ["INFO", "WARNING"])
        self.assertTrue(all(record.exc_info is None for record in timeouts))
        self.assertEqual(timeouts[0].diagnostic_key, timeouts[1].diagnostic_key)
        self.assertEqual(polls, [None, 43, 43, 43])
        self.assertEqual(sleeps, [0, 2])


if __name__ == "__main__":
    unittest.main()
