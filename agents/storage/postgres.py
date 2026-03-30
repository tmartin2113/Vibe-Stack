"""
PostgreSQL storage backend — production multi-node deployment.

Requires psycopg2 (or psycopg2-binary). Falls back with a clear
error if the driver is not installed — this is optional for local dev.

Connection string via VIBE_DATABASE_URL or individual PG* env vars.
"""

import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from typing import Any, List, Optional, Sequence, Tuple

try:
    import psycopg2
    import psycopg2.pool
    import psycopg2.extras
except ImportError:
    psycopg2 = None  # type: ignore[assignment]

from .base import StorageBackend

logger = logging.getLogger(__name__)


def _get_connection_params() -> dict:
    """Build connection params from env vars."""
    url = os.getenv("VIBE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if url:
        return {"dsn": url}
    return {
        "host": os.getenv("PGHOST", "localhost"),
        "port": int(os.getenv("PGPORT", "5432")),
        "dbname": os.getenv("PGDATABASE", "vibe"),
        "user": os.getenv("PGUSER", "vibe"),
        "password": os.getenv("PGPASSWORD", ""),
    }


class PostgresBackend(StorageBackend):
    """PostgreSQL implementation of StorageBackend.

    Uses connection pooling via psycopg2.pool.ThreadedConnectionPool
    for thread-safe concurrent access from multiple specialist threads.

    SQL dialect:
    - Placeholder: %s (not ?)
    - FTS: tsvector/tsquery (not FTS5)
    - Vector: pgvector extension (not in-memory cosine)
    - Schema: standard DDL (no executescript)
    """

    def __init__(
        self,
        min_connections: int = 2,
        max_connections: int = 10,
        **connection_kwargs,
    ):
        if psycopg2 is None:
            raise ImportError(
                "PostgreSQL backend requires psycopg2. "
                "Install with: pip install psycopg2-binary"
            )

        self._psycopg2 = psycopg2

        params = connection_kwargs or _get_connection_params()

        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=min_connections,
            maxconn=max_connections,
            **params,
        )
        self._max_connections = max_connections
        self._local = threading.local()

        logger.info(
            "PostgresBackend connected (pool: %d-%d)",
            min_connections, max_connections,
        )

    def _getconn(self):
        """Get a connection from the pool."""
        conn = self._pool.getconn()
        # Pool monitoring: warn when utilization > 80%
        used = len(self._pool._used)
        total = self._max_connections
        if total > 0 and used / total > 0.8:
            logger.warning(
                "PostgresBackend pool utilization high: %d/%d connections in use (%.0f%%)",
                used, total, used / total * 100,
            )
        return conn

    def _putconn(self, conn):
        """Return a connection to the pool."""
        self._pool.putconn(conn)

    @contextmanager
    def transaction(self):
        """Context manager for explicit transactions.

        Gets a connection from the pool, yields a transactional backend view,
        and commits on success / rolls back on exception.
        """
        conn = self._getconn()
        try:
            yield _TransactionalBackend(conn, self._psycopg2)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._putconn(conn)

    # ── StorageBackend interface ──

    def execute(self, sql: str, params: Sequence = ()) -> None:
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        except psycopg2.Error:
            conn.rollback()
            raise
        finally:
            self._putconn(conn)

    def executemany(self, sql: str, params_list: Sequence[Sequence]) -> None:
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.executemany(sql, params_list)
            conn.commit()
        except psycopg2.Error:
            conn.rollback()
            raise
        finally:
            self._putconn(conn)

    def fetchone(self, sql: str, params: Sequence = ()) -> Optional[Tuple]:
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()
        finally:
            self._putconn(conn)

    def fetchall(self, sql: str, params: Sequence = ()) -> List[Tuple]:
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        finally:
            self._putconn(conn)

    def fetchval(self, sql: str, params: Sequence = ()) -> Any:
        row = self.fetchone(sql, params)
        return row[0] if row else None

    def execute_script(self, sql: str) -> None:
        # Translate SQLite-specific AUTOINCREMENT to PostgreSQL SERIAL
        sql = sql.replace(
            "INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"
        )
        # Strip SQLite-specific FTS5 virtual tables and triggers
        sql = re.sub(
            r"CREATE\s+VIRTUAL\s+TABLE\s+.*?;", "", sql, flags=re.DOTALL | re.IGNORECASE
        )
        sql = re.sub(
            r"CREATE\s+TRIGGER\s+.*?END\s*;", "", sql, flags=re.DOTALL | re.IGNORECASE
        )
        conn = self._getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
        except psycopg2.Error:
            conn.rollback()
            raise
        finally:
            self._putconn(conn)

    def fetchone_dict(self, sql: str, params: Sequence = ()):
        conn = self._getconn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                return cur.fetchone()
        finally:
            self._putconn(conn)

    def fetchall_dict(self, sql: str, params: Sequence = ()) -> list:
        conn = self._getconn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        finally:
            self._putconn(conn)

    def table_exists(self, table_name: str) -> bool:
        row = self.fetchone(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table_name,),
        )
        return row is not None

    @property
    def placeholder(self) -> str:
        return "%s"

    @property
    def supports_fts(self) -> bool:
        return True  # PostgreSQL tsvector

    @property
    def supports_vector(self) -> bool:
        """Check if pgvector extension is available."""
        try:
            row = self.fetchone(
                "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
            )
            return row is not None
        except psycopg2.Error:
            return False

    def close(self) -> None:
        if self._pool:
            self._pool.closeall()
            logger.info("PostgresBackend connection pool closed")

    def __repr__(self) -> str:
        return "PostgresBackend()"


class _TransactionalBackend:
    """Lightweight backend view that reuses a single connection.

    Used inside ``PostgresBackend.transaction()`` so that every SQL
    statement within the context manager runs on the same connection
    (and therefore the same database transaction) without checking
    connections in and out of the pool.
    """

    def __init__(self, conn, _psycopg2_module):
        self._conn = conn
        self._psycopg2 = _psycopg2_module

    def execute(self, sql: str, params: Sequence = ()) -> None:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)

    def fetchone(self, sql: str, params: Sequence = ()) -> Optional[Tuple]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def fetchall(self, sql: str, params: Sequence = ()) -> List[Tuple]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def fetchone_dict(self, sql: str, params: Sequence = ()):
        with self._conn.cursor(cursor_factory=self._psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def fetchall_dict(self, sql: str, params: Sequence = ()) -> list:
        with self._conn.cursor(cursor_factory=self._psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def fetchval(self, sql: str, params: Sequence = ()) -> Any:
        row = self.fetchone(sql, params)
        return row[0] if row else None
