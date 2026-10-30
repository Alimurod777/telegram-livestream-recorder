import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from livestream.dispatcher import UserUpdateDispatcher, private_user_id


def message(user_id, text):
    return {"message": {"from": {"id": user_id},
                        "chat": {"id": user_id, "type": "private"}, "text": text}}


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_user_does_not_block_another_and_own_order_is_preserved(self):
        entered, release, other_done, finished = (asyncio.Event() for _ in range(4))
        seen = []

        async def handler(update):
            text = update["message"]["text"]
            if text == "login":
                entered.set()
                await release.wait()
            seen.append(text)
            if text == "status":
                other_done.set()
            if text == "code":
                finished.set()

        dispatcher = UserUpdateDispatcher(handler)
        try:
            dispatcher.submit(message(1, "login"))
            await asyncio.wait_for(entered.wait(), 1)
            dispatcher.submit(message(1, "code"))
            dispatcher.submit(message(2, "status"))
            await asyncio.wait_for(other_done.wait(), 1)
            self.assertEqual(seen, ["status"])
            release.set()
            await asyncio.wait_for(finished.wait(), 1)
            self.assertEqual(seen, ["status", "login", "code"])
            self.assertFalse(dispatcher._workers)
            self.assertFalse(dispatcher._queues)
        finally:
            await dispatcher.close()

    async def test_failure_does_not_drop_next_command(self):
        done = asyncio.Event()

        async def handler(update):
            if update["message"]["text"] == "fail":
                raise ValueError("test failure")
            done.set()

        dispatcher = UserUpdateDispatcher(handler)
        try:
            dispatcher.submit(message(1, "fail"))
            dispatcher.submit(message(1, "next"))
            with self.assertLogs("livestream.dispatcher", level="ERROR"):
                await asyncio.wait_for(done.wait(), 1)
        finally:
            await dispatcher.close()

    async def test_shutdown_cancels_running_and_queued_commands(self):
        entered = asyncio.Event()
        seen = []

        async def handler(update):
            seen.append(update["message"]["text"])
            entered.set()
            await asyncio.Event().wait()

        dispatcher = UserUpdateDispatcher(handler)
        dispatcher.submit(message(1, "running"))
        dispatcher.submit(message(1, "queued"))
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(dispatcher.close(), 1)
        self.assertEqual(seen, ["running"])
        self.assertFalse(dispatcher._workers)
        self.assertFalse(dispatcher._queues)
        with self.assertRaises(RuntimeError):
            dispatcher.submit(message(2, "late"))

    def test_private_callbacks_and_edited_messages_use_same_user(self):
        payload = message(7, "command")["message"]
        self.assertEqual(private_user_id({"edited_message": payload}), 7)
        callback = {"from": {"id": 7}, "message": payload}
        self.assertEqual(private_user_id({"callback_query": callback}), 7)
        callback["from"] = {"id": 8}
        self.assertIsNone(private_user_id({"callback_query": callback}))
        payload["chat"]["type"] = "group"
        self.assertIsNone(private_user_id({"message": payload}))
        self.assertIsNone(private_user_id({"callback_query": {"from": {"id": 7}}}))


class RecorderRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_frames_stay_with_their_client_and_chat(self):
        from livestream import recorder as module

        recorder = module.StreamRecorder()
        callbacks = []

        def make_call(client):
            def on_update():
                def register(callback):
                    callbacks.append(callback)
                    return callback
                return register
            return SimpleNamespace(start=AsyncMock(), on_update=on_update)

        with patch.object(module, "PyTgCalls", side_effect=make_call), patch.object(module, "install_broadcast_fix"):
            clients = [object(), object()]
            first = await recorder._get_call_client(1, clients[0])
            second = await recorder._get_call_client(2, clients[1])
        recorder.active_sessions = {
            1: SimpleNamespace(channel_id=100, call_client=first),
            2: SimpleNamespace(channel_id=100, call_client=second),
            3: SimpleNamespace(channel_id=200, call_client=first),
        }
        handler = AsyncMock()
        recorder._event_handler.handle_update = handler
        update = SimpleNamespace(chat_id=100)
        await callbacks[1](second, update)
        handler.assert_awaited_once_with(2, update)
        handler.reset_mock()
        await callbacks[0](first, SimpleNamespace(chat_id=999))
        await callbacks[0](first, SimpleNamespace())
        handler.assert_not_awaited()
        update = SimpleNamespace(chat_id=200)
        await callbacks[0](first, update)
        handler.assert_awaited_once_with(3, update)


if __name__ == "__main__":
    unittest.main()
