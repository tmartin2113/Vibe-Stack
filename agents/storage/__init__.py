"""
Storage abstraction layer for multi-node deployment.

Provides pluggable backends so stores can swap between local (SQLite)
and distributed (PostgreSQL, Redis) storage via environment config:

    VIBE_STORAGE_BACKEND=sqlite    # default — local dev, single node
    VIBE_STORAGE_BACKEND=postgres  # production multi-node

    VIBE_CACHE_BACKEND=memory      # default — in-process dict
    VIBE_CACHE_BACKEND=redis       # production multi-node

Stores keep their business logic. The abstraction sits underneath,
handling connection lifecycle, SQL dialect differences, and locking.
"""

from .base import StorageBackend, CacheBackend, DistributedLock
from .factory import create_storage_backend, create_cache_backend, create_lock

__all__ = [
    "StorageBackend",
    "CacheBackend",
    "DistributedLock",
    "create_storage_backend",
    "create_cache_backend",
    "create_lock",
]
