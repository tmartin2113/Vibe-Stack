"""
Abstract interfaces for storage, caching, and distributed locking.

Three concerns, three interfaces:

- StorageBackend: SQL-like persistent storage (SQLite, PostgreSQL)
- CacheBackend: Key-value with TTL (in-memory dict, Redis)
- DistributedLock: Mutual exclusion (threading.Lock, Redis lock)
"""

from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union


# ── Persistent storage (SQL) ──────────────────────────────────────

class StorageBackend(ABC):
    """Abstract SQL-like storage backend.

    Handles connection lifecycle, SQL dialect differences (placeholder
    syntax, schema DDL), and transaction management.  Stores call
    these methods instead of managing their own sqlite3 connections.

    Thread safety: implementations must be safe for concurrent use
    from multiple threads (connection pooling, per-call connections, etc.).
    """

    @abstractmethod
    def execute(self, sql: str, params: Sequence = ()) -> None:
        """Execute a write statement (INSERT, UPDATE, DELETE)."""

    @abstractmethod
    def executemany(self, sql: str, params_list: Sequence[Sequence]) -> None:
        """Execute a statement with multiple parameter sets."""

    @abstractmethod
    def fetchone(self, sql: str, params: Sequence = ()) -> Optional[Tuple]:
        """Execute a query and return one row, or None."""

    @abstractmethod
    def fetchall(self, sql: str, params: Sequence = ()) -> List[Tuple]:
        """Execute a query and return all rows."""

    @abstractmethod
    def fetchval(self, sql: str, params: Sequence = ()) -> Any:
        """Execute a query and return the first column of the first row."""

    @contextmanager
    def transaction(self):
        """Context manager for explicit transactions.

        Default implementation is a no-op (each execute auto-commits).
        Backends with real transaction support should override.
        """
        yield self

    @property
    @abstractmethod
    def placeholder(self) -> str:
        """Parameter placeholder for this backend.

        Returns '?' for SQLite, '%s' for PostgreSQL.
        Stores use this to build SQL strings that work across backends.
        """

    @abstractmethod
    def execute_script(self, sql: str) -> None:
        """Execute a multi-statement SQL script (DDL, migrations).

        Used for schema creation where statements are separated by ';'.
        """

    @abstractmethod
    def table_exists(self, table_name: str) -> bool:
        """Check if a table exists in the database."""

    @abstractmethod
    def close(self) -> None:
        """Release all connections and resources."""

    def fetchone_dict(self, sql: str, params: Sequence = ()):
        """Execute a query and return one row as a dict-like object, or None.

        Default delegates to fetchone(); backends should override to return
        dict-compatible rows (e.g. RealDictRow, sqlite3.Row).
        """
        return self.fetchone(sql, params)

    def fetchall_dict(self, sql: str, params: Sequence = ()) -> list:
        """Execute a query and return all rows as dict-like objects.

        Default delegates to fetchall(); backends should override to return
        dict-compatible rows.
        """
        return self.fetchall(sql, params)

    @property
    def supports_fts(self) -> bool:
        """Whether this backend supports full-text search natively.

        SQLite uses FTS5, PostgreSQL uses tsvector/tsquery.
        Stores can fall back to LIKE queries when False.
        """
        return False

    @property
    def supports_vector(self) -> bool:
        """Whether this backend supports vector similarity search.

        PostgreSQL with pgvector returns True.
        SQLite always returns False (vectors handled in Python).
        """
        return False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ── Cache (key-value with TTL) ────────────────────────────────────

class CacheBackend(ABC):
    """Abstract key-value cache with TTL support.

    Used for artifact caching, circuit breaker state, and any
    ephemeral shared state that needs sub-millisecond reads.
    """

    @abstractmethod
    def get(self, key: str) -> Optional[str]:
        """Get a value by key, or None if missing/expired."""

    @abstractmethod
    def set(self, key: str, value: str, ttl_seconds: int = 0) -> None:
        """Set a value with optional TTL (0 = no expiry)."""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete a key. Returns True if it existed."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Check if a key exists and is not expired."""

    @abstractmethod
    def incr(self, key: str, amount: int = 1) -> int:
        """Atomic increment. Creates key with value=amount if missing."""

    @abstractmethod
    def get_json(self, key: str) -> Optional[Dict[str, Any]]:
        """Get a JSON-serialized value, or None."""

    @abstractmethod
    def set_json(self, key: str, value: Dict[str, Any], ttl_seconds: int = 0) -> None:
        """Set a JSON-serializable value with optional TTL."""

    @abstractmethod
    def keys(self, pattern: str = "*") -> List[str]:
        """List keys matching a glob pattern."""

    @abstractmethod
    def flush(self) -> None:
        """Remove all keys (use with caution)."""

    @abstractmethod
    def close(self) -> None:
        """Release connections."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ── Distributed locking ──────────────────────────────────────────

class DistributedLock(ABC):
    """Abstract mutual exclusion lock.

    Drop-in replacement for threading.Lock that works across nodes
    when backed by Redis.  Falls back to threading.Lock for local dev.
    """

    @abstractmethod
    def acquire(self, timeout: float = -1) -> bool:
        """Acquire the lock. Returns True if acquired.

        Args:
            timeout: Max seconds to wait. -1 = block forever.
                     0 = non-blocking (try once).
        """

    @abstractmethod
    def release(self) -> None:
        """Release the lock."""

    @abstractmethod
    def locked(self) -> bool:
        """Check if the lock is currently held."""

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()
