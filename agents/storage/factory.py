"""
Factory functions for creating storage backends from configuration.

Reads VIBE_STORAGE_BACKEND and VIBE_CACHE_BACKEND env vars to
determine which implementations to instantiate.  Defaults to
SQLite + in-memory cache (current single-node behavior).
"""

import logging
import os
from typing import Optional

from .base import CacheBackend, DistributedLock, StorageBackend

logger = logging.getLogger(__name__)

# ── Storage backend (SQL) ─────────────────────────────────────────

_STORAGE_BACKEND = os.getenv("VIBE_STORAGE_BACKEND", "sqlite").lower()


def create_storage_backend(
    db_name: str,
    db_path: Optional[str] = None,
    backend_type: Optional[str] = None,
) -> StorageBackend:
    """Create a storage backend for a specific store.

    Args:
        db_name: Logical name of the store (e.g., "messages", "memories",
                 "artifacts", "spending"). Used to derive default paths.
        db_path: Explicit database path (overrides defaults).
        backend_type: Override VIBE_STORAGE_BACKEND for this store.

    Returns:
        StorageBackend instance (SQLite or PostgreSQL).
    """
    backend = backend_type or _STORAGE_BACKEND

    if backend == "postgres":
        from .postgres import PostgresBackend
        logger.info("Creating PostgreSQL backend for %s", db_name)
        return PostgresBackend()

    # Default: SQLite
    if db_path is None:
        from pathlib import Path
        db_path = str(Path.home() / ".vibe" / f"{db_name}.db")

    from .sqlite import SQLiteBackend
    logger.info("Creating SQLite backend for %s at %s", db_name, db_path)
    return SQLiteBackend(db_path=db_path)


# ── Cache backend (key-value) ────────────────────────────────────

_CACHE_BACKEND = os.getenv("VIBE_CACHE_BACKEND", "memory").lower()


def create_cache_backend(
    backend_type: Optional[str] = None,
) -> CacheBackend:
    """Create a cache backend.

    Args:
        backend_type: Override VIBE_CACHE_BACKEND for this instance.

    Returns:
        CacheBackend instance (in-memory dict or Redis).
    """
    backend = backend_type or _CACHE_BACKEND

    if backend == "redis":
        from .redis_backend import RedisCacheBackend
        logger.info("Creating Redis cache backend")
        return RedisCacheBackend()

    # Default: in-memory
    from .redis_backend import MemoryCacheBackend
    logger.info("Creating in-memory cache backend")
    return MemoryCacheBackend()


# ── Distributed lock ─────────────────────────────────────────────

def create_lock(
    name: str,
    backend_type: Optional[str] = None,
    ttl_seconds: int = 30,
) -> DistributedLock:
    """Create a distributed lock.

    Uses Redis when VIBE_CACHE_BACKEND=redis, otherwise threading.Lock.

    Args:
        name: Lock name (must be unique across the system).
        backend_type: Override VIBE_CACHE_BACKEND.
        ttl_seconds: Lock TTL for Redis (prevents deadlocks on crash).

    Returns:
        DistributedLock instance.
    """
    backend = backend_type or _CACHE_BACKEND

    if backend == "redis":
        from .redis_backend import RedisCacheBackend, RedisDistributedLock
        import redis as redis_lib
        url = os.getenv("VIBE_REDIS_URL") or os.getenv("REDIS_URL") or "redis://localhost:6379/0"
        client = redis_lib.from_url(url, decode_responses=True)
        return RedisDistributedLock(client, name, ttl_seconds=ttl_seconds)

    from .redis_backend import LocalLock
    return LocalLock(name)
