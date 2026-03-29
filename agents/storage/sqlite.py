"""
SQLite storage backend — local development and single-node deployment.

This is the default backend. It wraps the existing SQLite patterns
already used throughout the codebase (WAL mode, per-call connections,
threading lock) behind the StorageBackend interface.
"""

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from .base import StorageBackend

logger = logging.getLogger(__name__)


class SQLiteBackend(StorageBackend):
    """SQLite implementation of StorageBackend.

    Per-call connection pattern with WAL mode for concurrent reads.
    Thread-safe via internal lock on write operations.
    """

    def __init__(self, db_path: str, timeout: int = 15):
        self._db_path = db_path
        self._timeout = timeout
        self._write_lock = threading.Lock()

        # Ensure parent directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # Initialize WAL mode on first connection
        self._init_pragmas()

    def _init_pragmas(self) -> None:
        """Set WAL mode and other pragmas on first connect."""
        conn = sqlite3.connect(self._db_path, timeout=self._timeout)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=1000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        """Create a new per-call connection."""
        conn = sqlite3.connect(self._db_path, timeout=self._timeout)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ── StorageBackend interface ──

    def execute(self, sql: str, params: Sequence = ()) -> None:
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute(sql, params)
                conn.commit()
            finally:
                conn.close()

    def executemany(self, sql: str, params_list: Sequence[Sequence]) -> None:
        with self._write_lock:
            conn = self._connect()
            try:
                conn.executemany(sql, params_list)
                conn.commit()
            finally:
                conn.close()

    def fetchone(self, sql: str, params: Sequence = ()) -> Optional[Tuple]:
        conn = self._connect()
        try:
            cursor = conn.execute(sql, params)
            return cursor.fetchone()
        finally:
            conn.close()

    def fetchall(self, sql: str, params: Sequence = ()) -> List[Tuple]:
        conn = self._connect()
        try:
            cursor = conn.execute(sql, params)
            return cursor.fetchall()
        finally:
            conn.close()

    def fetchval(self, sql: str, params: Sequence = ()) -> Any:
        row = self.fetchone(sql, params)
        return row[0] if row else None

    def fetchone_dict(self, sql: str, params: Sequence = ()):
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(sql, params)
            return cursor.fetchone()
        finally:
            conn.close()

    def fetchall_dict(self, sql: str, params: Sequence = ()) -> list:
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(sql, params)
            return cursor.fetchall()
        finally:
            conn.close()

    def execute_script(self, sql: str) -> None:
        with self._write_lock:
            conn = self._connect()
            try:
                conn.executescript(sql)
            finally:
                conn.close()

    def table_exists(self, table_name: str) -> bool:
        row = self.fetchone(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return row is not None

    @property
    def placeholder(self) -> str:
        return "?"

    @property
    def supports_fts(self) -> bool:
        return True  # SQLite FTS5

    def close(self) -> None:
        pass  # Per-call connections, nothing to close

    def __repr__(self) -> str:
        return f"SQLiteBackend(db_path={self._db_path!r})"
