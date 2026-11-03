import unittest
from unittest.mock import AsyncMock, patch


class CommandLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import bot
        self.bot = bot

    async def test_message_timeout_does_not_send_duplicate_or_hold_queue_twice(self):
        with patch.object(self.bot, "bot_api", side_effect=TimeoutError()) as api:
            with self.assertLogs(self.bot.logger, level="ERROR"):
                self.assertIsNone(await self.bot.send_message(7, "hello"))
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.kwargs["timeout"], 10)

    async def test_forbidden_and_rate_limit_are_not_retried_as_markdown_errors(self):
        for code in (400, 403, 429):
            error = self.bot.BotAPIError({"error_code": code, "description": "chat unavailable"})
            with patch.object(self.bot, "bot_api", side_effect=error) as api:
                with self.assertLogs(self.bot.logger, level="ERROR"):
                    await self.bot.send_message(7, "hello")
            self.assertEqual(api.call_count, 1)

    async def test_invalid_markdown_still_delivers_plain_text_and_keyboard(self):
        error = self.bot.BotAPIError({"error_code": 400, "description": "Bad Request: can't parse entities"})
        response = {"ok": True, "result": {"message_id": 1}}
        with patch.object(self.bot, "bot_api", side_effect=[error, response]) as api:
            with self.assertLogs(self.bot.logger, level="WARNING"):
                result = await self.bot.send_message(7, "bad *", "keyboard")
        self.assertEqual(result, response)
        first, second = api.call_args_list
        self.assertEqual(first.args[1]["parse_mode"], "Markdown")
        self.assertNotIn("parse_mode", second.args[1])
        self.assertEqual(second.args[1]["reply_markup"], "keyboard")

    async def test_start_answers_without_reconnecting_cached_account(self):
        with patch.dict(self.bot.user_clients, {7: object()}, clear=True), \
                patch.object(self.bot, "get_user_client", AsyncMock()) as restore, \
                patch.object(self.bot.db, "get_session_string") as storage, \
                patch.object(self.bot.db, "get_target", return_value=None), \
                patch.object(self.bot, "send_message", AsyncMock()) as send:
            await self.bot.handle_start(7, 7)
        restore.assert_not_awaited()
        storage.assert_not_called()
        self.assertIn("Welcome back", send.call_args.args[1])

    async def test_start_with_saved_session_does_not_connect_to_telegram(self):
        with patch.dict(self.bot.user_clients, {}, clear=True), \
                patch.object(self.bot, "get_user_client", AsyncMock()) as restore, \
                patch.object(self.bot.db, "get_session_string", return_value="saved"), \
                patch.object(self.bot.db, "get_target", return_value=None), \
                patch.object(self.bot, "send_message", AsyncMock()) as send:
            await self.bot.handle_start(7, 7)
        restore.assert_not_awaited()
        self.assertIn("Welcome back", send.call_args.args[1])

    async def test_start_without_session_shows_login(self):
        with patch.dict(self.bot.user_clients, {}, clear=True), \
                patch.object(self.bot.db, "get_session_string", return_value=None), \
                patch.object(self.bot, "send_message", AsyncMock()) as send:
            await self.bot.handle_start(7, 7)
        self.assertIn("Login first", send.call_args.args[1])
