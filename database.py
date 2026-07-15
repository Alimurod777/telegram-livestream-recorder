"""
SQLite database manager for the Telegram live recorder bot.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

from config import DATABASE_FILE


class Database:
    def __init__(self, db_path: str = DATABASE_FILE):
        self.db_path = db_path
        self._init_database()

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
            # session_string column is defined in CREATE TABLE above

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
            return False

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
        try:
            with self._get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT user_id, session_string, last_active
                    FROM user_sessions
                    WHERE session_string IS NOT NULL
                    """
                ).fetchall()
            return [dict(row) for row in rows]
        except Exception as exc:
            print(f"Database error getting all sessions: {exc}")
            return []

    def delete_session(self, user_id: int) -> bool:
        try:
            with self._get_connection() as conn:
                conn.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
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
