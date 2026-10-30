import unittest
import urllib.error
from unittest.mock import patch


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


if __name__ == "__main__":
    unittest.main()
