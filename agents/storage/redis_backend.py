"""
Redis cache backend — production multi-node deployment.

Requires the `redis` package. Falls back with a clear error
if not installed — this is optional for local dev.

Connection via VIBE_REDIS_URL or individual REDIS_* env vars.
"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from .base import CacheBackend, DistributedLock

logger = logging.getLogger(__name__)


def _get_redis_url() -> str:
    """Build Redis URL from env vars."""
    url = os.getenv("VIBE_REDIS_URL") or os.getenv("REDIS_URL")
    if url:
        return url
    host = os.getenv("REDIS_HOST", "localhost")
    port = os.getenv("REDIS_PORT", "6379")
    db = os.getenv("REDIS_DB", "0")
    password = os.getenv("REDIS_PASSWORD", "")
    if password:
        return f"redis://:{password}@{host}:{port}/{db}"
    return f"redis://{host}:{port}/{db}"


class RedisCacheBackend(CacheBackend):
    """Redis implementation of CacheBackend.

    Thread-safe via redis-py's connection pooling.
    All operations are atomic at the Redis level.
    """

    def __init__(self, url: Optional[str] = None, prefix: str = "vibe:"):
        try:
            import redis as redis_lib
        except ImportError:
            raise ImportError(
                "Redis backend requires the redis package. "
                "Install with: pip install redis"
            )

        self._url = url or _get_redis_url()
        self._prefix = prefix
        self._client = redis_lib.from_url(
            self._url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )

        # Verify connection
        try:
            self._client.ping()
            logger.info("RedisCacheBackend connected to %s", self._url.split("@")[-1])
        except Exception as e:
            logger.warning("Redis connection failed: %s", e)
            raise

    def _key(self, key: str) -> str:
        """Add namespace prefix to avoid collisions."""
        return f"{self._prefix}{key}"

    # ── CacheBackend interface ──

    def get(self, key: str) -> Optional[str]:
        return self._client.get(self._key(key))

    def set(self, key: str, value: str, ttl_seconds: int = 0) -> None:
        k = self._key(key)
        if ttl_seconds > 0:
            self._client.setex(k, ttl_seconds, value)
        else:
            self._client.set(k, value)

    def delete(self, key: str) -> bool:
        return bool(self._client.delete(self._key(key)))

    def exists(self, key: str) -> bool:
        return bool(self._client.exists(self._key(key)))

    def incr(self, key: str, amount: int = 1) -> int:
        return self._client.incr(self._key(key), amount)

    def get_json(self, key: str) -> Optional[Dict[str, Any]]:
        raw = self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    def set_json(self, key: str, value: Dict[str, Any], ttl_seconds: int = 0) -> None:
        self.set(key, json.dumps(value, default=str), ttl_seconds)

    def keys(self, pattern: str = "*") -> List[str]:
        full_pattern = self._key(pattern)
        raw_keys = self._client.keys(full_pattern)
        prefix_len = len(self._prefix)
        return [k[prefix_len:] for k in raw_keys]

    def flush(self) -> None:
        # Only flush keys with our prefix, not the whole Redis
        for key in self._client.keys(f"{self._prefix}*"):
            self._client.delete(key)

    def close(self) -> None:
        self._client.close()
        logger.info("RedisCacheBackend closed")


class RedisDistributedLock(DistributedLock):
    """Redis-backed distributed lock using SET NX EX pattern.

    Provides mutual exclusion across multiple nodes. Each lock has
    a TTL to prevent deadlocks if the holder crashes.
    """

    def __init__(
        self,
        client,
        name: str,
        ttl_seconds: int = 30,
        prefix: str = "vibe:lock:",
    ):
        self._client = client
        self._name = f"{prefix}{name}"
        self._ttl = ttl_seconds
        self._token = f"{os.getpid()}-{threading.get_ident()}-{time.monotonic()}"
        self._owned = False

    def acquire(self, timeout: float = -1) -> bool:
        deadline = None if timeout < 0 else time.monotonic() + timeout

        while True:
            # SET NX with TTL — atomic acquire
            if self._client.set(self._name, self._token, nx=True, ex=self._ttl):
                self._owned = True
                return True

            if timeout == 0:
                return False

            if deadline is not None and time.monotonic() >= deadline:
                return False

            # Brief sleep before retry
            time.sleep(0.05)

    def release(self) -> None:
        if not self._owned:
            return

        # Lua script for atomic check-and-delete (only release our own lock)
        lua = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        self._client.eval(lua, 1, self._name, self._token)
        self._owned = False

    def locked(self) -> bool:
        return bool(self._client.exists(self._name))


# ── In-memory fallbacks (local dev) ──────────────────────────────

class MemoryCacheBackend(CacheBackend):
    """In-process dict-based cache. Single-node only.

    Used as the default when VIBE_CACHE_BACKEND=memory (or unset).
    Thread-safe via threading.Lock.
    """

    def __init__(self):
        self._store: Dict[str, Any] = {}
        self._expiry: Dict[str, float] = {}
        self._lock = threading.Lock()

    def _is_expired(self, key: str) -> bool:
        exp = self._expiry.get(key)
        if exp is None:
            return False
        return time.monotonic() > exp

    def _cleanup_key(self, key: str) -> None:
        self._store.pop(key, None)
        self._expiry.pop(key, None)

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            if key not in self._store:
                return None
            if self._is_expired(key):
                self._cleanup_key(key)
                return None
            return self._store[key]

    def set(self, key: str, value: str, ttl_seconds: int = 0) -> None:
        with self._lock:
            self._store[key] = value
            if ttl_seconds > 0:
                self._expiry[key] = time.monotonic() + ttl_seconds
            else:
                self._expiry.pop(key, None)

    def delete(self, key: str) -> bool:
        with self._lock:
            existed = key in self._store
            self._cleanup_key(key)
            return existed

    def exists(self, key: str) -> bool:
        with self._lock:
            if key not in self._store:
                return False
            if self._is_expired(key):
                self._cleanup_key(key)
                return False
            return True

    def incr(self, key: str, amount: int = 1) -> int:
        with self._lock:
            if key not in self._store or self._is_expired(key):
                self._store[key] = str(amount)
                return amount
            val = int(self._store[key]) + amount
            self._store[key] = str(val)
            return val

    def get_json(self, key: str) -> Optional[Dict[str, Any]]:
        raw = self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    def set_json(self, key: str, value: Dict[str, Any], ttl_seconds: int = 0) -> None:
        self.set(key, json.dumps(value, default=str), ttl_seconds)

    def keys(self, pattern: str = "*") -> List[str]:
        import fnmatch
        with self._lock:
            # Lazy cleanup of expired keys
            all_keys = list(self._store.keys())
            result = []
            for k in all_keys:
                if self._is_expired(k):
                    self._cleanup_key(k)
                    continue
                if pattern == "*" or fnmatch.fnmatch(k, pattern):
                    result.append(k)
            return result

    def flush(self) -> None:
        with self._lock:
            self._store.clear()
            self._expiry.clear()

    def close(self) -> None:
        self.flush()


class LocalLock(DistributedLock):
    """threading.Lock wrapper implementing DistributedLock interface.

    Used for single-node deployment. Drop-in replacement for
    RedisDistributedLock when no Redis is configured.
    """

    def __init__(self, name: str = ""):
        self._lock = threading.Lock()
        self._name = name

    def acquire(self, timeout: float = -1) -> bool:
        if timeout < 0:
            self._lock.acquire()
            return True
        return self._lock.acquire(timeout=max(timeout, 0))

    def release(self) -> None:
        try:
            self._lock.release()
        except RuntimeError:
            pass  # Already released

    def locked(self) -> bool:
        if self._lock.acquire(blocking=False):
            self._lock.release()
            return False
        return True
