"""
SQLite database manager for the Telegram live recorder bot.
"""

import sqlite3
import os
import tempfile
import time
import logging
from pathlib import Path
import config
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

from config import DATABASE_FILE

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str = DATABASE_FILE):
        self.db_path = db_path
        self.sessions_dir = Path(getattr(config, "SESSION_DIR", Path(db_path).resolve().parent / "sessions"))
        self._mongo_client = None
        self._mongo_retry_at = 0.0
        self._init_database()

    def _mongo_sessions(self):
        uri = getattr(config, "MONGO_URI", "")
        if not uri or time.monotonic() < self._mongo_retry_at:
            return None
        try:
            if self._mongo_client is None:
                from pymongo import MongoClient
                self._mongo_client = MongoClient(uri, serverSelectionTimeoutMS=3000,
                                                  connectTimeoutMS=3000, socketTimeoutMS=3000)
            return self._mongo_client[getattr(config, "MONGO_DATABASE", "telegram_livestream_recorder")]["user_sessions"]
        except Exception:
            self._mongo_retry_at = time.monotonic() + 60
            logger.warning("Mongo session store unavailable; using local storage")
            return None

    def _mongo_failed(self) -> None:
        self._mongo_retry_at = time.monotonic() + 60
        logger.warning("Mongo session request failed; using local storage for 60s")

    def _backup_session(self, user_id: int, session_string: str) -> bool:
        """Atomic local fallback adapted from savebotnew/TechVJ/generate.py."""
        temporary = None
        try:
            self.sessions_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(dir=self.sessions_dir, prefix=".session-")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(session_string)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.sessions_dir / f"user_{int(user_id)}.session")
            return True
        except OSError:
            logger.warning("Local session backup failed: user=%s", user_id)
            return False
        finally:
            if temporary and os.path.exists(temporary):
                os.remove(temporary)

    def _read_session_backup(self, user_id: int) -> Optional[str]:
        try:
            return (self.sessions_dir / f"user_{int(user_id)}.session").read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _session_from_mongo(self, row: Dict[str, Any]) -> str:
        """Keep a newer offline login when MongoDB becomes available again."""
        user_id = int(row["user_id"])
        backup = self._read_session_backup(user_id)
        if backup and backup != row["session_string"]:
            try:
                modified = (self.sessions_dir / f"user_{user_id}.session").stat().st_mtime
                if modified > float(row.get("session_saved_at", 0)):
                    return backup
            except OSError:
                pass
        return row["session_string"]

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_database(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_targets (
                    user_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    channel_title TEXT,
                    channel_username TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS recordings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    channel_title TEXT,
                    start_time TIMESTAMP NOT NULL,
                    end_time TIMESTAMP,
                    is_video BOOLEAN DEFAULT FALSE,
                    file_path TEXT,
                    file_size INTEGER,
                    status TEXT DEFAULT 'recording',
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_sessions (
                    user_id INTEGER PRIMARY KEY,
                    session_file TEXT,
                    session_string TEXT,
                    logged_in_at TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    label TEXT,
                    session_string TEXT NOT NULL,
                    flood_until TIMESTAMP,
                    last_error TEXT,
                    active_recordings INTEGER NOT NULL DEFAULT 0,
                    last_used TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # Active recordings cannot survive a process restart.
            cursor.execute("UPDATE accounts SET active_recordings = 0")
            columns = {row[1] for row in cursor.execute("PRAGMA table_info(accounts)")}
            if "owner_id" not in columns:
                cursor.execute("ALTER TABLE accounts ADD COLUMN owner_id INTEGER")
            cursor.execute("""CREATE TABLE IF NOT EXISTS waiting_streams (
                user_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
                title TEXT NOT NULL
            )""")

    def save_waiting(self, user_id: int, channel_id: int, title: str) -> None:
        with self._get_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO waiting_streams VALUES (?, ?, ?)",
                         (user_id, channel_id, title))

    def delete_waiting(self, user_id: int) -> None:
        with self._get_connection() as conn:
            conn.execute("DELETE FROM waiting_streams WHERE user_id = ?", (user_id,))

    def list_waiting(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM waiting_streams")]

    # ── Recording account pool (multi-account support) ───────────────────

    def add_account(self, session_string: str, label: Optional[str] = None,
                    owner_id: Optional[int] = None) -> Optional[int]:
        try:
            with self._get_connection() as conn:
                existing = conn.execute(
                    "SELECT account_id, owner_id FROM accounts WHERE session_string = ?",
                    (session_string,),
                ).fetchone()
                if existing:
                    if existing["owner_id"] is None and owner_id is not None:
                        conn.execute("UPDATE accounts SET owner_id = ? WHERE account_id = ?",
                                     (owner_id, existing["account_id"]))
                        return int(existing["account_id"])
                    return int(existing["account_id"]) if existing["owner_id"] == owner_id else None
                cursor = conn.execute(
                    "INSERT INTO accounts (label, session_string, owner_id) VALUES (?, ?, ?)",
                    (label, session_string, owner_id),
                )
                return int(cursor.lastrowid)
        except Exception as exc:
            print(f"Database error adding account: {exc}")
            return None

    def list_accounts(self, owner_id: Optional[int] = None) -> List[Dict[str, Any]]:
        try:
            with self._get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT account_id, owner_id, label, flood_until, last_error,
                           active_recordings, last_used, created_at
                    FROM accounts WHERE (? IS NULL OR owner_id = ?) ORDER BY account_id
                    """, (owner_id, owner_id),
                ).fetchall()
            return [dict(row) for row in rows]
        except Exception as exc:
            print(f"Database error listing accounts: {exc}")
            return []

    def get_account_session(self, account_id: int) -> Optional[str]:
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT session_string FROM accounts WHERE account_id = ?",
                    (account_id,),
                ).fetchone()
            return row["session_string"] if row else None
        except Exception as exc:
            print(f"Database error getting account: {exc}")
            return None

    def delete_account(self, account_id: int) -> bool:
        try:
            with self._get_connection() as conn:
                conn.execute("DELETE FROM accounts WHERE account_id = ?", (account_id,))
            return True
        except Exception as exc:
            print(f"Database error deleting account: {exc}")
            return False

    def mark_account_flood(self, account_id: int, until: datetime, error: Optional[str] = None) -> bool:
        try:
            with self._get_connection() as conn:
                conn.execute(
                    "UPDATE accounts SET flood_until = ?, last_error = ? WHERE account_id = ?",
                    (until, error, account_id),
                )
            return True
        except Exception:
            return False

    def set_account_active(self, account_id: int, count: int) -> bool:
        try:
            with self._get_connection() as conn:
                conn.execute(
                    "UPDATE accounts SET active_recordings = ?, last_used = ? WHERE account_id = ?",
                    (count, datetime.now(), account_id),
                )
            return True
        except Exception:
            return False

    def get_account_active(self, account_id: int) -> int:
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT active_recordings FROM accounts WHERE account_id = ?",
                    (account_id,),
                ).fetchone()
            return int(row["active_recordings"]) if row else 0
        except Exception:
            return 0

    def set_target(
        self,
        user_id: int,
        channel_id: int,
        channel_title: Optional[str] = None,
        channel_username: Optional[str] = None,
    ) -> bool:
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO user_targets
                    (user_id, channel_id, channel_title, channel_username, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (user_id, channel_id, channel_title, channel_username, datetime.now()),
                )
            return True
        except Exception as exc:
            print(f"Database error setting target: {exc}")
            return False

    def get_target(self, user_id: int) -> Optional[Dict[str, Any]]:
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT channel_id, channel_title, channel_username, updated_at
                    FROM user_targets WHERE user_id = ?
                    """,
                    (user_id,),
                ).fetchone()
            if not row:
                return None
            return {
                "id": row["channel_id"],
                "title": row["channel_title"],
                "username": row["channel_username"],
                "updated_at": row["updated_at"],
            }
        except Exception as exc:
            print(f"Database error getting target: {exc}")
            return None

    def delete_target(self, user_id: int) -> bool:
        try:
            with self._get_connection() as conn:
                conn.execute("DELETE FROM user_targets WHERE user_id = ?", (user_id,))
            return True
        except Exception as exc:
            print(f"Database error deleting target: {exc}")
            return False

    def start_recording(
        self, user_id: int, channel_id: int, channel_title: str, is_video: bool
    ) -> int:
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO recordings
                    (user_id, channel_id, channel_title, start_time, is_video, status)
                    VALUES (?, ?, ?, ?, ?, 'recording')
                    """,
                    (user_id, channel_id, channel_title, datetime.now(), is_video),
                )
                return int(cursor.lastrowid)
        except Exception as exc:
            print(f"Database error starting recording: {exc}")
            return -1

    def finish_recording(
        self,
        recording_id: int,
        file_path: Optional[str] = None,
        file_size: Optional[int] = None,
        status: str = "completed",
        error_message: Optional[str] = None,
        is_video: Optional[bool] = None,
    ) -> bool:
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    UPDATE recordings
                    SET end_time = ?, file_path = ?, file_size = ?,
                        status = ?, error_message = ?,
                        is_video = COALESCE(?, is_video)
                    WHERE id = ?
                    """,
                    (
                        datetime.now(),
                        file_path,
                        file_size,
                        status,
                        error_message,
                        is_video,
                        recording_id,
                    ),
                )
            return True
        except Exception as exc:
            print(f"Database error finishing recording: {exc}")
            return False

    def get_user_recordings(self, user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        try:
            with self._get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM recordings
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (user_id, limit),
                ).fetchall()
            return [dict(row) for row in rows]
        except Exception as exc:
            print(f"Database error getting recordings: {exc}")
            return []

    def save_session(
        self, user_id: int, session_file: str, session_string: Optional[str] = None
    ) -> bool:
        saved = self._backup_session(user_id, session_string) if session_string else False
        collection = self._mongo_sessions()
        if collection is not None and session_string:
            try:
                collection.update_one({"user_id": int(user_id)}, {"$set": {
                    "session_string": session_string, "logged_in": True, "last_active": datetime.now(),
                    "session_saved_at": time.time(),
                }}, upsert=True)
                saved = True
            except Exception:
                self._mongo_failed()
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO user_sessions
                    (user_id, session_file, session_string, logged_in_at, last_active)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (user_id, session_file, session_string, datetime.now(), datetime.now()),
                )
            return True
        except Exception as exc:
            print(f"Database error saving session: {exc}")
            return saved

    def get_session(self, user_id: int) -> Optional[str]:
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT session_file FROM user_sessions WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
            return row["session_file"] if row else None
        except Exception as exc:
            print(f"Database error getting session: {exc}")
            return None

    def get_session_string(self, user_id: int) -> Optional[str]:
        collection = self._mongo_sessions()
        if collection is not None:
            try:
                row = collection.find_one({"user_id": int(user_id)})
                if row and row.get("session_string"):
                    return self._session_from_mongo({**row, "user_id": int(user_id)})
            except Exception:
                self._mongo_failed()
        backup = self._read_session_backup(user_id)
        if backup:
            return backup
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT session_string FROM user_sessions WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
            return row["session_string"] if row else None
        except Exception as exc:
            print(f"Database error getting session string: {exc}")
            return None

    def get_all_sessions(self) -> List[Dict[str, Any]]:
        sessions = {}
        try:
            with self._get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT user_id, session_string, last_active
                    FROM user_sessions
                    WHERE session_string IS NOT NULL
                    """
                ).fetchall()
            sessions.update({int(row["user_id"]): dict(row) for row in rows})
        except Exception as exc:
            print(f"Database error getting all sessions: {exc}")
        for path in self.sessions_dir.glob("user_*.session"):
            identity = path.stem.removeprefix("user_")
            if identity.isdigit():
                session = self._read_session_backup(int(identity))
                if session:
                    sessions[int(identity)] = {"user_id": int(identity), "session_string": session}
        collection = self._mongo_sessions()
        if collection is not None:
            try:
                for row in collection.find({"session_string": {"$type": "string"}}, {"_id": 0}):
                    row["session_string"] = self._session_from_mongo(row)
                    sessions[int(row["user_id"])] = row
            except Exception:
                self._mongo_failed()
        return list(sessions.values())

    def delete_session(self, user_id: int) -> bool:
        collection = self._mongo_sessions()
        if collection is not None:
            try:
                collection.delete_one({"user_id": int(user_id)})
            except Exception:
                self._mongo_failed()
                return False
        elif getattr(config, "MONGO_URI", ""):
            return False
        try:
            with self._get_connection() as conn:
                conn.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
            (self.sessions_dir / f"user_{int(user_id)}.session").unlink(missing_ok=True)
            return True
        except Exception as exc:
            print(f"Database error deleting session: {exc}")
            return False

    def update_activity(self, user_id: int) -> bool:
        try:
            with self._get_connection() as conn:
                conn.execute(
                    "UPDATE user_sessions SET last_active = ? WHERE user_id = ?",
                    (datetime.now(), user_id),
                )
            return True
        except Exception:
            return False


db = Database()
