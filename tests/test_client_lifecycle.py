import asyncio
import sqlite3
import unittest

from livestream.client_lifecycle import track_client_updates


class FakeClient:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled_with_storage_open = False
        self.calls = 0
        self.connect_storage()

    def connect_storage(self):
        self.storage = sqlite3.connect(":memory:")
        self.storage.execute("CREATE TABLE peers (id INTEGER)")

    async def connect(self):
        self.connect_storage()

    async def handle_updates(self, peer):
        self.calls += 1
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            # Reproduce an update task using SQLite while shutdown begins.
            self.storage.execute("SELECT 1")
            self.cancelled_with_storage_open = True
            raise
        self.storage.execute("INSERT INTO peers VALUES (?)", (peer,))

    async def disconnect(self):
        self.storage.close()

    async def stop(self):
        await self.disconnect()


class ClientLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_cancels_inflight_update_before_closing_sqlite_and_ignores_late_update(self):
        client = track_client_updates(FakeClient())
        task = asyncio.create_task(client.handle_updates(1))
        await client.entered.wait()
        await client.stop()
        self.assertTrue(task.cancelled())
        self.assertTrue(client.cancelled_with_storage_open)
        # Session.handle_packet can have queued this coroutine before stop.
        await client.handle_updates(2)
        self.assertEqual(client.calls, 1)
        self.assertFalse(client._recorder_update_guard["tasks"])

    async def test_disconnect_guard_and_reconnect_allow_updates_again(self):
        client = track_client_updates(FakeClient())
        await client.disconnect()
        await client.handle_updates(1)
        self.assertEqual(client.calls, 0)
        await client.connect()
        client.release.set()
        await client.handle_updates(2)
        self.assertEqual(client.storage.execute("SELECT id FROM peers").fetchall(), [(2,)])
        await client.stop()

    async def test_stopping_one_client_does_not_cancel_another_clients_updates(self):
        first, second = (track_client_updates(FakeClient()) for _ in range(2))
        tasks = [asyncio.create_task(client.handle_updates(1)) for client in (first, second)]
        await asyncio.gather(first.entered.wait(), second.entered.wait())
        await first.stop()
        self.assertFalse(tasks[1].done())
        second.release.set()
        await tasks[1]
        self.assertEqual(second.storage.execute("SELECT id FROM peers").fetchall(), [(1,)])
        await second.stop()

    async def test_guard_is_installed_once_and_normal_update_errors_are_not_hidden(self):
        client = track_client_updates(FakeClient())
        guarded = client.handle_updates
        self.assertIs(track_client_updates(client), client)
        self.assertIs(client.handle_updates, guarded)
        client.release.set()
        with self.assertRaises(sqlite3.ProgrammingError):
            await client.handle_updates(object())
        await client.stop()
