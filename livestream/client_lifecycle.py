"""Drain Pyrofork's detached update tasks before its session storage closes."""

import asyncio
from functools import wraps


def track_client_updates(client):
    """Install an instance-local guard; leave installed packages unchanged.

    Session.handle_packet creates handle_updates tasks without retaining them.
    Client.stop/disconnect otherwise closes SQLite while these tasks can still
    be fetching peers or awaiting Telegram. Ignore late arrivals during shutdown
    and cancel/await the in-flight tasks before calling the original lifecycle.
    """
    if getattr(client, "_recorder_update_guard", None) is not None:
        return client
    original = getattr(client, "handle_updates", None)
    if not callable(original):
        return client
    state = {"closing": False, "tasks": set()}
    client._recorder_update_guard = state

    @wraps(original)
    async def handle_updates(*args, **kwargs):
        if state["closing"]:
            return
        task = asyncio.current_task()
        state["tasks"].add(task)
        try:
            return await original(*args, **kwargs)
        finally:
            state["tasks"].discard(task)

    client.handle_updates = handle_updates

    def wrap_close(method):
        @wraps(method)
        async def close(*args, **kwargs):
            state["closing"] = True
            current = asyncio.current_task()
            tasks = [task for task in state["tasks"] if task is not current]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return await method(*args, **kwargs)
        return close

    for name in ("stop", "disconnect"):
        method = getattr(client, name, None)
        if callable(method):
            setattr(client, name, wrap_close(method))

    connect = getattr(client, "connect", None)
    if callable(connect):
        @wraps(connect)
        async def reopen(*args, **kwargs):
            state["closing"] = False
            return await connect(*args, **kwargs)
        client.connect = reopen
    return client
