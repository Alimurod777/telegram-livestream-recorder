"""Keep reused PyTgCalls clients from joining a previous livestream.

PyTgCalls 2.2.8 retains InputGroupCall and participant/DC caches per chat.
Its join path refreshes GROUPCALL_FORBIDDEN but not GROUPCALL_INVALID.
Keep this workaround on the bridge instance, without changing dependencies.
"""

import logging
from types import MethodType

from pyrogram.errors import GroupcallInvalid
from pytgcalls.mtproto.pyrogram_client import PyrogramClient

logger = logging.getLogger(__name__)


def refresh_call_cache(call_client, channel_id):
    bridge = getattr(getattr(call_client, "_app", None), "mtproto_client", None)
    if isinstance(bridge, PyrogramClient):
        bridge._cache.drop_cache(channel_id)


def cached_call_id(call_client, channel_id):
    bridge = getattr(getattr(call_client, "_app", None), "mtproto_client", None)
    if isinstance(bridge, PyrogramClient):
        call = bridge._cache._full_chat_cache.get(channel_id)
        return getattr(call, "id", None)
    return None


def install_call_cache_fix(call_client):
    bridge = getattr(getattr(call_client, "_app", None), "mtproto_client", None)
    if not isinstance(bridge, PyrogramClient) or getattr(bridge, "_recorder_join_refresh", False):
        return
    original_join = bridge.join_group_call

    async def join_group_call(self, chat_id, *args, **kwargs):
        try:
            return await original_join(chat_id, *args, **kwargs)
        except GroupcallInvalid:
            # The host may replace the call between discovery and joining.
            # Retry once only; real invalid calls and other RPC errors propagate.
            logger.info("Refreshing invalid group call before retry: chat=%s", chat_id)
            self._cache.drop_cache(chat_id)
            return await original_join(chat_id, *args, **kwargs)

    bridge.join_group_call = MethodType(join_group_call, bridge)
    bridge._recorder_join_refresh = True
