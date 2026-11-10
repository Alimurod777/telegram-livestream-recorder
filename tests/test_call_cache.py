import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pyrogram.errors import FloodWait, GroupcallInvalid
from pytgcalls.mtproto.client_cache import ClientCache
from pytgcalls.mtproto.pyrogram_client import PyrogramClient

from livestream.call_cache import cached_call_id, install_call_cache_fix, refresh_call_cache


class CallCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bridge = object.__new__(PyrogramClient)
        self.bridge.no_updates = lambda: False
        self.bridge._cache = ClientCache(3600, self.bridge)
        self.old = SimpleNamespace(id=1)
        self.new = SimpleNamespace(id=2)
        self.other = SimpleNamespace(id=3)
        self.bridge._cache.set_cache(-1001, self.old)
        self.bridge._cache.set_cache(-1002, self.other)
        self.bridge.get_call = AsyncMock(return_value=self.new)
        self.call = SimpleNamespace(_app=SimpleNamespace(mtproto_client=self.bridge))

    async def test_new_recording_refreshes_only_selected_chat_and_participants(self):
        refresh_call_cache(self.call, -1001)
        self.assertIs(await self.bridge._cache.get_full_chat(-1001), self.new)
        self.assertIs(await self.bridge._cache.get_full_chat(-1002), self.other)
        self.assertEqual(self.bridge._cache._call_participants_cache.get(-1001).input_id, 2)
        self.bridge.get_call.assert_awaited_once_with(-1001)

    async def test_invalid_join_refreshes_stale_call_and_retries_once(self):
        seen = []

        async def join(chat_id, *args, **kwargs):
            call = await self.bridge._cache.get_full_chat(chat_id)
            seen.append(call.id)
            if call.id == 1:
                raise GroupcallInvalid()
            return 'connected'

        self.bridge.join_group_call = AsyncMock(side_effect=join)
        install_call_cache_fix(self.call)
        install_call_cache_fix(self.call)
        self.assertEqual(await self.bridge.join_group_call(-1001, 'payload'), 'connected')
        self.assertEqual(seen, [1, 2])
        self.assertEqual(cached_call_id(self.call, -1001), 2)
        self.assertIs(await self.bridge._cache.get_full_chat(-1002), self.other)

    async def test_persistent_invalid_call_is_not_retried_forever(self):
        original = AsyncMock(side_effect=GroupcallInvalid())
        self.bridge.join_group_call = original
        install_call_cache_fix(self.call)
        with self.assertRaises(GroupcallInvalid):
            await self.bridge.join_group_call(-1001, 'payload')
        self.assertEqual(original.await_count, 2)

    async def test_flood_wait_propagates_without_retry_or_cache_change(self):
        original = AsyncMock(side_effect=FloodWait(5))
        self.bridge.join_group_call = original
        install_call_cache_fix(self.call)
        with self.assertRaises(FloodWait):
            await self.bridge.join_group_call(-1001, 'payload')
        original.assert_awaited_once()
        self.assertIs(await self.bridge._cache.get_full_chat(-1001), self.old)
