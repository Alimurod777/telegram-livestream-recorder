"""AccountManager — pool of extra Telegram accounts for recording.

Primary source for a recording is always the bot user's own session.
Pool accounts are used for failover (FloodWait / auth loss) and for
spreading concurrent recordings across accounts.

Health model:
- flood_until (DB) — account recently hit FloodWait; skip until expiry
- active_recordings (DB) — load balancing counter
- connected clients cached in memory
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from pyrogram import Client

from config import API_HASH, API_ID
import config
from database import db

logger = logging.getLogger(__name__)


class AccountManager:
    def __init__(self):
        self._clients: Dict[int, Client] = {}
        self._locks: Dict[int, asyncio.Lock] = {}
        # in-memory active counter mirrors DB (faster, self-healing on restart)
        self._active: Dict[int, int] = {}
        self._selection_lock = asyncio.Lock()

    # ── Client lifecycle ──────────────────────────────────────────────────

    def _lock(self, account_id: int) -> asyncio.Lock:
        return self._locks.setdefault(account_id, asyncio.Lock())

    async def get_client(self, account_id: int) -> Optional[Client]:
        session_string = await asyncio.to_thread(db.get_account_session, account_id)
        if not session_string:
            return None
        async with self._lock(account_id):
            client = self._clients.get(account_id)
            if client and client.is_connected:
                return client
            try:
                client = Client(
                    f"account_{account_id}",
                    api_id=API_ID,
                    api_hash=API_HASH,
                    session_string=session_string,
                    in_memory=True,
                    sleep_threshold=0,
                )
                if not await client.connect():
                    await client.disconnect()
                    raise RuntimeError("Stored account session is no longer authorized")
                client.me = await client.get_me()
                await client.initialize()
                self._clients[account_id] = client
                return client
            except Exception as exc:
                logger.warning("Account %s failed to start: %s", account_id, exc)
                self._clients.pop(account_id, None)
                if client and client.is_connected:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                seconds = int(getattr(exc, "value", 0) or 300)
                self.mark_flood(account_id, seconds, type(exc).__name__)
                return None

    async def stop_client(self, account_id: int) -> None:
        client = self._clients.pop(account_id, None)
        if client:
            try:
                await client.stop()
            except Exception:
                pass

    # ── Selection / health ────────────────────────────────────────────────

    @staticmethod
    def _is_flooded(row: Dict) -> bool:
        until = row.get("flood_until")
        if not until:
            return False
        try:
            return datetime.now() < datetime.fromisoformat(str(until))
        except (TypeError, ValueError):
            return False

    async def healthy_accounts(self, owner_id: int) -> List[Dict]:
        rows = await asyncio.to_thread(db.list_accounts, owner_id)
        return [row for row in rows if not self._is_flooded(row)]

    async def pick_account(self, owner_id: int, exclude: Optional[set] = None) -> Optional[Tuple[Client, int]]:
        async with self._selection_lock:
            return await self._pick_account(owner_id, exclude)

    async def _pick_account(self, owner_id: int, exclude: Optional[set]) -> Optional[Tuple[Client, int]]:
        """Return (client, account_id) for the healthiest, least-loaded account."""
        exclude = exclude or set()
        rows = await self.healthy_accounts(owner_id)
        rows.sort(
            key=lambda row: (
                self._active.get(row["account_id"], row.get("active_recordings") or 0),
                row["account_id"],
            )
        )
        for row in rows:
            account_id = int(row["account_id"])
            if account_id in exclude or self._active.get(account_id, 0) >= max(
                1, int(getattr(config, "MAX_RECORDINGS_PER_ACCOUNT", 1))
            ):
                continue
            client = await self.get_client(account_id)
            if client:
                self.mark_busy(account_id)
                return client, account_id
        return None

    def mark_flood(self, account_id: int, seconds: int, error: str = "") -> None:
        until = datetime.now() + timedelta(seconds=max(seconds, 60))
        try:
            db.mark_account_flood(account_id, until, error)
            logger.warning("Account %s flooded for %ss: %s", account_id, seconds, error)
        except Exception:
            pass

    def mark_busy(self, account_id: int) -> None:
        self._active[account_id] = self._active.get(account_id, 0) + 1
        try:
            db.set_account_active(account_id, self._active[account_id])
        except Exception:
            pass

    def mark_free(self, account_id: int) -> None:
        count = max(0, self._active.get(account_id, 0) - 1)
        self._active[account_id] = count
        try:
            db.set_account_active(account_id, count)
        except Exception:
            pass

    async def shutdown(self) -> None:
        for account_id in list(self._clients):
            await self.stop_client(account_id)


account_manager = AccountManager()
