"""
Integration tests for pluggable storage backends.

These tests verify that all four stores work correctly when backed by:
- SQLiteBackend (via the storage abstraction, not direct sqlite3)
- PostgresBackend (requires PostgreSQL service)
- RedisCacheBackend (requires Redis service)

Run locally with services:
    docker run -d -p 5432:5432 -e POSTGRES_DB=vibe_test -e POSTGRES_USER=vibe \
        -e POSTGRES_PASSWORD=test pgvector/pgvector:pg16
    docker run -d -p 6379:6379 redis:7
    VIBE_DATABASE_URL=postgresql://vibe:test@localhost:5432/vibe_test \
    VIBE_REDIS_URL=redis://localhost:6379/0 \
    python -m pytest tests/test_storage_integration.py -x -o "addopts="

In CI, PostgreSQL and Redis are provided as GitHub Actions services.
Tests are skipped when services are unavailable (local dev without Docker).
"""

import os
import time
from pathlib import Path

import pytest

from agents.storage.sqlite import SQLiteBackend
from agents.storage.redis_backend import MemoryCacheBackend, LocalLock


# ── Skip markers ─────────────────────────────────────────────────

def _postgres_available() -> bool:
    url = os.getenv("VIBE_DATABASE_URL")
    if not url:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=3)
        conn.close()
        return True
    except Exception:
        return False


def _redis_available() -> bool:
    url = os.getenv("VIBE_REDIS_URL")
    if not url:
        return False
    try:
        import redis as redis_lib
        client = redis_lib.from_url(url, socket_connect_timeout=3)
        client.ping()
        client.close()
        return True
    except Exception:
        return False


skip_no_postgres = pytest.mark.skipif(
    not _postgres_available(),
    reason="PostgreSQL not available (set VIBE_DATABASE_URL)",
)

skip_no_redis = pytest.mark.skipif(
    not _redis_available(),
    reason="Redis not available (set VIBE_REDIS_URL)",
)


# ── SQLiteBackend tests (always run) ─────────────────────────────


class TestSQLiteBackend:
    """Verify SQLiteBackend implements the StorageBackend contract."""

    def test_create_table_and_crud(self, tmp_path):
        db = str(tmp_path / "test.db")
        backend = SQLiteBackend(db)

        backend.execute_script("""
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)

        # Insert
        backend.execute("INSERT INTO kv (key, value) VALUES (?, ?)", ("k1", "v1"))

        # Read
        row = backend.fetchone("SELECT value FROM kv WHERE key = ?", ("k1",))
        assert row is not None
        assert row[0] == "v1"

        # Update
        backend.execute("UPDATE kv SET value = ? WHERE key = ?", ("v2", "k1"))
        row = backend.fetchone("SELECT value FROM kv WHERE key = ?", ("k1",))
        assert row[0] == "v2"

        # fetchall
        backend.execute("INSERT INTO kv (key, value) VALUES (?, ?)", ("k2", "v3"))
        rows = backend.fetchall("SELECT key FROM kv ORDER BY key")
        assert len(rows) == 2

        # fetchval
        count = backend.fetchval("SELECT COUNT(*) FROM kv")
        assert count == 2

        # Delete
        backend.execute("DELETE FROM kv WHERE key = ?", ("k1",))
        assert backend.fetchone("SELECT 1 FROM kv WHERE key = ?", ("k1",)) is None

        # table_exists
        assert backend.table_exists("kv")
        assert not backend.table_exists("nonexistent")

        # placeholder
        assert backend.placeholder == "?"

        backend.close()

    def test_executemany(self, tmp_path):
        db = str(tmp_path / "test.db")
        backend = SQLiteBackend(db)

        backend.execute_script("CREATE TABLE nums (n INTEGER);")
        backend.executemany("INSERT INTO nums (n) VALUES (?)", [(1,), (2,), (3,)])

        count = backend.fetchval("SELECT COUNT(*) FROM nums")
        assert count == 3
        backend.close()


class TestMemoryCacheBackend:
    """Verify in-memory cache implements CacheBackend contract."""

    def test_get_set_delete(self):
        cache = MemoryCacheBackend()

        assert cache.get("missing") is None
        cache.set("k1", "v1")
        assert cache.get("k1") == "v1"
        assert cache.exists("k1")

        assert cache.delete("k1")
        assert not cache.exists("k1")
        assert not cache.delete("k1")  # already gone

        cache.close()

    def test_ttl_expiry(self):
        cache = MemoryCacheBackend()
        cache.set("k1", "v1", ttl_seconds=1)
        assert cache.get("k1") == "v1"
        time.sleep(1.1)
        assert cache.get("k1") is None
        cache.close()

    def test_json_roundtrip(self):
        cache = MemoryCacheBackend()
        cache.set_json("obj", {"a": 1, "b": [2, 3]})
        assert cache.get_json("obj") == {"a": 1, "b": [2, 3]}
        cache.close()

    def test_incr(self):
        cache = MemoryCacheBackend()
        assert cache.incr("counter") == 1
        assert cache.incr("counter") == 2
        assert cache.incr("counter", 5) == 7
        cache.close()

    def test_keys_and_flush(self):
        cache = MemoryCacheBackend()
        cache.set("a:1", "x")
        cache.set("a:2", "y")
        cache.set("b:1", "z")

        assert len(cache.keys("a:*")) == 2
        assert len(cache.keys("*")) == 3

        cache.flush()
        assert len(cache.keys("*")) == 0
        cache.close()


class TestLocalLock:
    """Verify LocalLock implements DistributedLock contract."""

    def test_acquire_release(self):
        lock = LocalLock("test")
        assert lock.acquire(timeout=0)
        assert lock.locked()
        lock.release()
        assert not lock.locked()

    def test_context_manager(self):
        lock = LocalLock("test")
        with lock:
            assert lock.locked()
        assert not lock.locked()


# ── ArtifactStore with SQLiteBackend ─────────────────────────────


class TestArtifactStoreWithSQLiteBackend:
    """Verify ArtifactStore works when given an explicit SQLiteBackend."""

    def test_store_and_lookup(self, tmp_path):
        from agents.artifact_store import ArtifactStore
        db_path = str(tmp_path / "artifacts.db")
        backend = SQLiteBackend(db_path)

        store = ArtifactStore(db_path=db_path, storage_backend=backend)

        stored = store.store(
            cache_key="abc123",
            specification="test spec",
            specialist_output="output text",
            output_critic_score=90,
            final_score=90,
            tool_calls=[],
            task_type="code_generation",
            specialist_adapter="code",
            skills_hash="deadbeef",
        )
        assert stored

        entry = store.lookup("abc123")
        assert entry is not None
        assert entry.specialist_output == "output text"
        assert entry.final_score == 90

        backend.close()


# ── SpendingTracker with SQLiteBackend ───────────────────────────


class TestSpendingTrackerWithSQLiteBackend:
    """Verify SpendingTracker works when given an explicit SQLiteBackend."""

    def test_record_and_check(self, tmp_path):
        from agents.spending_tracker import SpendingTracker
        db_path = str(tmp_path / "spending.db")
        backend = SQLiteBackend(db_path)

        tracker = SpendingTracker(db_path=db_path, storage_backend=backend)
        tracker.record_event(status="completed", cost_cents=10, model="test")

        status = tracker.get_status()
        assert status.total_cost_cents >= 10

        # Circuit breaker should be closed
        result = tracker.check_circuit_breaker()
        assert result is None  # None = CLOSED, proceed

        backend.close()


# ── MessageStore with SQLiteBackend ──────────────────────────────


class TestMessageStoreSchemaInit:
    """Verify MessageStore schema init delegates to storage_backend."""

    def test_schema_created_via_backend(self, tmp_path):
        from agents.message_store import MessageStore
        db_path = str(tmp_path / "messages.db")
        backend = SQLiteBackend(db_path)

        # Schema init goes through storage_backend.execute_script
        store = MessageStore(db_path=Path(db_path), storage_backend=backend)

        # Verify the table was created
        assert backend.table_exists("messages")

        # Direct query through backend works
        count = backend.fetchval("SELECT COUNT(*) FROM messages")
        assert count == 0

        backend.close()


# ── MemoryStore with SQLiteBackend ───────────────────────────────


class TestMemoryStoreSchemaInit:
    """Verify MemoryStore schema init delegates to storage_backend."""

    def test_schema_created_via_backend(self, tmp_path):
        from agents.memory_store import MemoryStore
        db_path = str(tmp_path / "memory.db")
        backend = SQLiteBackend(db_path)

        store = MemoryStore(db_path=Path(db_path), storage_backend=backend)

        # Verify the table was created
        assert backend.table_exists("memories")

        count = backend.fetchval("SELECT COUNT(*) FROM memories")
        assert count == 0

        backend.close()


# ══════════════════════════════════════════════════════════════════
# PostgreSQL integration tests (skipped when service unavailable)
# ══════════════════════════════════════════════════════════════════


@skip_no_postgres
class TestPostgresBackend:
    """Verify PostgresBackend implements StorageBackend contract."""

    def _make_backend(self):
        from agents.storage.postgres import PostgresBackend
        return PostgresBackend()

    def test_create_table_and_crud(self):
        backend = self._make_backend()

        # Use a unique table name to avoid collisions
        table = f"test_kv_{int(time.time())}"
        try:
            backend.execute_script(f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)

            ph = backend.placeholder  # %s

            backend.execute(f"INSERT INTO {table} (key, value) VALUES ({ph}, {ph})", ("k1", "v1"))

            row = backend.fetchone(f"SELECT value FROM {table} WHERE key = {ph}", ("k1",))
            assert row is not None
            assert row[0] == "v1"

            backend.execute(f"UPDATE {table} SET value = {ph} WHERE key = {ph}", ("v2", "k1"))
            row = backend.fetchone(f"SELECT value FROM {table} WHERE key = {ph}", ("k1",))
            assert row[0] == "v2"

            count = backend.fetchval(f"SELECT COUNT(*) FROM {table}")
            assert count == 1

            assert backend.table_exists(table)
            assert not backend.table_exists("nonexistent_table_xyz")

            assert backend.placeholder == "%s"

        finally:
            backend.execute_script(f"DROP TABLE IF EXISTS {table}")
            backend.close()


@skip_no_postgres
class TestArtifactStoreWithPostgres:
    """ArtifactStore backed by PostgreSQL."""

    def test_store_and_lookup(self):
        from agents.storage.postgres import PostgresBackend
        from agents.artifact_store import ArtifactStore

        backend = PostgresBackend()
        try:
            store = ArtifactStore(storage_backend=backend)

            key = f"pg_test_{int(time.time())}"
            stored = store.store(
                cache_key=key,
                specification="pg test spec",
                specialist_output="pg output",
                output_critic_score=85,
                final_score=85,
                tool_calls=[],
                task_type="code_generation",
                specialist_adapter="code",
                skills_hash="pgdeadbeef",
            )
            assert stored

            entry = store.lookup(key)
            assert entry is not None
            assert entry.specialist_output == "pg output"

            # Cleanup
            store.invalidate(key)
        finally:
            backend.close()


@skip_no_postgres
class TestSpendingTrackerWithPostgres:
    """SpendingTracker backed by PostgreSQL."""

    def test_record_and_status(self):
        from agents.storage.postgres import PostgresBackend
        from agents.spending_tracker import SpendingTracker

        backend = PostgresBackend()
        try:
            tracker = SpendingTracker(storage_backend=backend, agent_id="pg_test")
            tracker.record_event(status="completed", cost_cents=5, model="test-pg")

            status = tracker.get_status()
            assert status.total_cost_cents >= 5

            breaker = tracker.check_circuit_breaker()
            assert breaker is None  # CLOSED
        finally:
            backend.close()


# ══════════════════════════════════════════════════════════════════
# Redis integration tests (skipped when service unavailable)
# ══════════════════════════════════════════════════════════════════


@skip_no_redis
class TestRedisCacheBackend:
    """Verify RedisCacheBackend implements CacheBackend contract."""

    def _make_cache(self):
        from agents.storage.redis_backend import RedisCacheBackend
        url = os.getenv("VIBE_REDIS_URL", "redis://localhost:6379/0")
        return RedisCacheBackend(url=url, prefix="vibe_test:")

    def test_get_set_delete(self):
        cache = self._make_cache()
        try:
            cache.set("k1", "v1")
            assert cache.get("k1") == "v1"
            assert cache.exists("k1")

            assert cache.delete("k1")
            assert not cache.exists("k1")
        finally:
            cache.flush()
            cache.close()

    def test_ttl_expiry(self):
        cache = self._make_cache()
        try:
            cache.set("k1", "v1", ttl_seconds=1)
            assert cache.get("k1") == "v1"
            time.sleep(1.1)
            assert cache.get("k1") is None
        finally:
            cache.flush()
            cache.close()

    def test_json_roundtrip(self):
        cache = self._make_cache()
        try:
            cache.set_json("obj", {"x": 42, "y": [1, 2]})
            assert cache.get_json("obj") == {"x": 42, "y": [1, 2]}
        finally:
            cache.flush()
            cache.close()

    def test_incr(self):
        cache = self._make_cache()
        try:
            assert cache.incr("ctr") == 1
            assert cache.incr("ctr") == 2
            assert cache.incr("ctr", 3) == 5
        finally:
            cache.flush()
            cache.close()

    def test_keys_and_flush(self):
        cache = self._make_cache()
        try:
            cache.set("a:1", "x")
            cache.set("a:2", "y")
            cache.set("b:1", "z")

            assert len(cache.keys("a:*")) == 2

            cache.flush()
            assert len(cache.keys("*")) == 0
        finally:
            cache.close()


@skip_no_redis
class TestRedisDistributedLock:
    """Verify RedisDistributedLock implements DistributedLock contract."""

    def test_acquire_release(self):
        from agents.storage.redis_backend import RedisDistributedLock
        import redis as redis_lib

        url = os.getenv("VIBE_REDIS_URL", "redis://localhost:6379/0")
        client = redis_lib.from_url(url, decode_responses=True)
        try:
            lock = RedisDistributedLock(client, "test_lock", ttl_seconds=10, prefix="vibe_test:lock:")

            assert lock.acquire(timeout=1)
            assert lock.locked()
            lock.release()
            assert not lock.locked()
        finally:
            client.delete("vibe_test:lock:test_lock")
            client.close()

    def test_context_manager(self):
        from agents.storage.redis_backend import RedisDistributedLock
        import redis as redis_lib

        url = os.getenv("VIBE_REDIS_URL", "redis://localhost:6379/0")
        client = redis_lib.from_url(url, decode_responses=True)
        try:
            lock = RedisDistributedLock(client, "test_ctx_lock", ttl_seconds=10, prefix="vibe_test:lock:")

            with lock:
                assert lock.locked()
            assert not lock.locked()
        finally:
            client.delete("vibe_test:lock:test_ctx_lock")
            client.close()
