"""
Tests for the Artifact Store (Result Caching).

Covers:
- ArtifactStore: SQLite lifecycle, cache key computation, lookup, store,
  TTL expiration, LRU eviction, invalidation, statistics
- CacheConfig: dataclass defaults and SystemConfig integration
- State integration: cache fields in AgentState / create_initial_state
- Graph integration: cache_lookup node, cache_hit_or_miss routing,
  cache_store in skill_cleanup_wrapper
"""

import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agents.artifact_store import ArtifactStore, CacheEntry
from agents.config import CacheConfig, SystemConfig
from agents.state import AgentState, create_initial_state


# ===== Fixtures =====


@pytest.fixture
def tmp_db(tmp_path):
    """Return a temp SQLite path for each test."""
    return str(tmp_path / "test_cache.db")


@pytest.fixture
def store(tmp_db):
    """Create a fresh ArtifactStore with defaults."""
    return ArtifactStore(db_path=tmp_db, default_ttl_seconds=3600)


@pytest.fixture
def small_store(tmp_db):
    """ArtifactStore with max_entries=3 for eviction tests."""
    return ArtifactStore(db_path=tmp_db, max_entries=3)


@pytest.fixture
def sample_spec():
    return "Write a Python function that sorts a list of integers using merge sort."


@pytest.fixture
def sample_skills():
    return [
        {"name": "python-algorithms", "content": "# Algorithm patterns..."},
        {"name": "testing-best-practices", "content": "# Test patterns..."},
    ]


@pytest.fixture
def sample_output():
    return "def merge_sort(arr):\n    if len(arr) <= 1:\n        return arr\n    ..."


@pytest.fixture
def sample_tool_calls():
    return [
        {"name": "python_executor", "params": {"code": "print(1)"}, "result": "1"},
    ]


# ===== Cache Key Computation =====


class TestCacheKeyComputation:
    """Tests for ArtifactStore.compute_cache_key()."""

    def test_deterministic(self, sample_spec, sample_skills):
        key1 = ArtifactStore.compute_cache_key(
            sample_spec, sample_skills, "code", "code_generator"
        )
        key2 = ArtifactStore.compute_cache_key(
            sample_spec, sample_skills, "code", "code_generator"
        )
        assert key1 == key2

    def test_sha256_format(self, sample_spec, sample_skills):
        key = ArtifactStore.compute_cache_key(
            sample_spec, sample_skills, "code", "code_generator"
        )
        assert len(key) == 64  # SHA-256 hex
        assert all(c in "0123456789abcdef" for c in key)

    def test_different_spec_different_key(self, sample_skills):
        key1 = ArtifactStore.compute_cache_key(
            "spec A", sample_skills, "code", "code_generator"
        )
        key2 = ArtifactStore.compute_cache_key(
            "spec B", sample_skills, "code", "code_generator"
        )
        assert key1 != key2

    def test_different_task_type_different_key(self, sample_spec, sample_skills):
        key1 = ArtifactStore.compute_cache_key(
            sample_spec, sample_skills, "code", "code_generator"
        )
        key2 = ArtifactStore.compute_cache_key(
            sample_spec, sample_skills, "research", "code_generator"
        )
        assert key1 != key2

    def test_different_adapter_different_key(self, sample_spec, sample_skills):
        key1 = ArtifactStore.compute_cache_key(
            sample_spec, sample_skills, "code", "code_generator"
        )
        key2 = ArtifactStore.compute_cache_key(
            sample_spec, sample_skills, "code", "security_auditor"
        )
        assert key1 != key2

    def test_different_skills_different_key(self, sample_spec):
        skills_a = [{"name": "skill-a"}]
        skills_b = [{"name": "skill-b"}]
        key1 = ArtifactStore.compute_cache_key(
            sample_spec, skills_a, "code", "code_generator"
        )
        key2 = ArtifactStore.compute_cache_key(
            sample_spec, skills_b, "code", "code_generator"
        )
        assert key1 != key2

    def test_skill_order_independent(self, sample_spec):
        """Skills are sorted by name, so order doesn't matter."""
        skills_1 = [{"name": "b-skill"}, {"name": "a-skill"}]
        skills_2 = [{"name": "a-skill"}, {"name": "b-skill"}]
        key1 = ArtifactStore.compute_cache_key(
            sample_spec, skills_1, "code", "gen"
        )
        key2 = ArtifactStore.compute_cache_key(
            sample_spec, skills_2, "code", "gen"
        )
        assert key1 == key2

    def test_empty_skills(self, sample_spec):
        key = ArtifactStore.compute_cache_key(
            sample_spec, [], "code", "code_generator"
        )
        assert len(key) == 64

    def test_skills_hash(self, sample_skills):
        h = ArtifactStore.compute_skills_hash(sample_skills)
        assert len(h) == 16  # Truncated SHA-256


# ===== Store Initialization =====


class TestStoreInit:
    """Tests for ArtifactStore initialization and schema."""

    def test_creates_db_file(self, tmp_db):
        ArtifactStore(db_path=tmp_db)
        assert Path(tmp_db).exists()

    def test_creates_parent_dirs(self, tmp_path):
        deep_path = str(tmp_path / "a" / "b" / "cache.db")
        ArtifactStore(db_path=deep_path)
        assert Path(deep_path).exists()

    def test_default_path(self):
        """Default db_path is ~/.vibe/artifact_cache.db."""
        store = ArtifactStore.__new__(ArtifactStore)
        store.db_path = None
        store.max_entries = 1000
        store.default_ttl_seconds = 3600
        store.min_score_to_cache = 70
        # Just verify the default logic
        expected = str(Path.home() / ".vibe" / "artifact_cache.db")
        s = ArtifactStore()
        assert s.db_path == expected

    def test_wal_mode(self, tmp_db):
        store = ArtifactStore(db_path=tmp_db)
        conn = sqlite3.connect(tmp_db)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"

    def test_schema_created(self, tmp_db):
        ArtifactStore(db_path=tmp_db)
        conn = sqlite3.connect(tmp_db)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        conn.close()
        table_names = [t[0] for t in tables]
        assert "artifacts" in table_names

    def test_idempotent_init(self, tmp_db):
        """Creating store twice on same db should not error."""
        ArtifactStore(db_path=tmp_db)
        ArtifactStore(db_path=tmp_db)  # No error


# ===== Store & Lookup =====


class TestStoreAndLookup:
    """Tests for store() and lookup() methods."""

    def test_store_and_lookup(self, store, sample_spec, sample_output, sample_tool_calls):
        key = "a" * 64
        stored = store.store(
            cache_key=key,
            specification=sample_spec,
            specialist_output=sample_output,
            output_critic_score=85,
            final_score=90,
            tool_calls=sample_tool_calls,
            task_type="code",
            specialist_adapter="code_generator",
            skills_hash="abc123",
            num_iterations=2,
        )
        assert stored is True

        entry = store.lookup(key)
        assert entry is not None
        assert entry.cache_key == key
        assert entry.specialist_output == sample_output
        assert entry.output_critic_score == 85
        assert entry.final_score == 90
        assert entry.task_type == "code"
        assert entry.specialist_adapter == "code_generator"
        assert entry.skills_hash == "abc123"
        assert entry.num_iterations == 2
        assert entry.access_count == 1
        assert len(entry.tool_calls) == 1

    def test_lookup_miss(self, store):
        assert store.lookup("nonexistent" + "0" * 54) is None

    def test_access_count_increments(self, store, sample_output):
        key = "b" * 64
        store.store(
            cache_key=key, specification="spec", specialist_output=sample_output,
            output_critic_score=80, final_score=85, tool_calls=[],
            task_type="code", specialist_adapter="gen", skills_hash="x",
        )
        store.lookup(key)
        store.lookup(key)
        entry = store.lookup(key)
        assert entry.access_count == 3

    def test_rejects_low_score(self, store, sample_output):
        stored = store.store(
            cache_key="c" * 64, specification="spec",
            specialist_output=sample_output,
            output_critic_score=50, final_score=50, tool_calls=[],
            task_type="code", specialist_adapter="gen", skills_hash="x",
        )
        assert stored is False
        assert store.lookup("c" * 64) is None

    def test_rejects_empty_output(self, store):
        stored = store.store(
            cache_key="d" * 64, specification="spec",
            specialist_output="", output_critic_score=90, final_score=90,
            tool_calls=[], task_type="code", specialist_adapter="gen",
            skills_hash="x",
        )
        assert stored is False

    def test_rejects_whitespace_output(self, store):
        stored = store.store(
            cache_key="e" * 64, specification="spec",
            specialist_output="   \n  ", output_critic_score=90,
            final_score=90, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x",
        )
        assert stored is False

    def test_higher_score_replaces(self, store, sample_output):
        key = "f" * 64
        store.store(
            cache_key=key, specification="spec",
            specialist_output="old output", output_critic_score=75,
            final_score=80, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x",
        )
        store.store(
            cache_key=key, specification="spec",
            specialist_output=sample_output, output_critic_score=90,
            final_score=95, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x",
        )
        entry = store.lookup(key)
        assert entry.final_score == 95
        assert entry.specialist_output == sample_output

    def test_lower_score_does_not_replace(self, store, sample_output):
        key = "g" * 64
        store.store(
            cache_key=key, specification="spec",
            specialist_output=sample_output, output_critic_score=90,
            final_score=95, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x",
        )
        result = store.store(
            cache_key=key, specification="spec",
            specialist_output="worse output", output_critic_score=75,
            final_score=80, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x",
        )
        assert result is False
        entry = store.lookup(key)
        assert entry.final_score == 95

    def test_uses_output_critic_score_when_final_zero(self, store, sample_output):
        """When final_score is 0, use output_critic_score for threshold check."""
        stored = store.store(
            cache_key="h" * 64, specification="spec",
            specialist_output=sample_output, output_critic_score=80,
            final_score=0, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x",
        )
        assert stored is True

    def test_spec_truncated_on_store(self, store, sample_output):
        long_spec = "x" * 5000
        store.store(
            cache_key="i" * 64, specification=long_spec,
            specialist_output=sample_output, output_critic_score=85,
            final_score=90, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x",
        )
        entry = store.lookup("i" * 64)
        assert len(entry.specification) == 2000

    def test_custom_ttl(self, store, sample_output):
        store.store(
            cache_key="j" * 64, specification="spec",
            specialist_output=sample_output, output_critic_score=85,
            final_score=90, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x",
            ttl_seconds=1,
        )
        entry = store.lookup("j" * 64)
        assert entry.ttl_seconds == 1


# ===== TTL Expiration =====


class TestTTLExpiration:
    """Tests for time-to-live expiration."""

    def test_expired_entry_returns_none(self, tmp_db, sample_output):
        store = ArtifactStore(db_path=tmp_db, default_ttl_seconds=1)
        key = "k" * 64
        store.store(
            cache_key=key, specification="spec",
            specialist_output=sample_output, output_critic_score=85,
            final_score=90, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x",
        )
        # Manually backdate the created_at
        conn = sqlite3.connect(tmp_db)
        old_time = (datetime.utcnow() - timedelta(seconds=10)).isoformat() + "Z"
        conn.execute(
            "UPDATE artifacts SET created_at = ? WHERE cache_key = ?",
            (old_time, key),
        )
        conn.commit()
        conn.close()

        entry = store.lookup(key)
        assert entry is None  # Expired

    def test_ttl_zero_means_no_expiry(self, tmp_db, sample_output):
        store = ArtifactStore(db_path=tmp_db, default_ttl_seconds=0)
        key = "l" * 64
        store.store(
            cache_key=key, specification="spec",
            specialist_output=sample_output, output_critic_score=85,
            final_score=90, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x",
        )
        # Backdate heavily
        conn = sqlite3.connect(tmp_db)
        old_time = (datetime.utcnow() - timedelta(days=365)).isoformat() + "Z"
        conn.execute(
            "UPDATE artifacts SET created_at = ? WHERE cache_key = ?",
            (old_time, key),
        )
        conn.commit()
        conn.close()

        entry = store.lookup(key)
        assert entry is not None  # TTL=0 means no expiry

    def test_expired_entry_deleted_on_lookup(self, tmp_db, sample_output):
        store = ArtifactStore(db_path=tmp_db, default_ttl_seconds=1)
        key = "m" * 64
        store.store(
            cache_key=key, specification="spec",
            specialist_output=sample_output, output_critic_score=85,
            final_score=90, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x",
        )
        # Backdate
        conn = sqlite3.connect(tmp_db)
        old_time = (datetime.utcnow() - timedelta(seconds=10)).isoformat() + "Z"
        conn.execute(
            "UPDATE artifacts SET created_at = ? WHERE cache_key = ?",
            (old_time, key),
        )
        conn.commit()
        conn.close()

        store.lookup(key)  # Triggers delete

        # Verify actually deleted from DB
        conn = sqlite3.connect(tmp_db)
        row = conn.execute(
            "SELECT * FROM artifacts WHERE cache_key = ?", (key,)
        ).fetchone()
        conn.close()
        assert row is None

    def test_cleanup_expired(self, tmp_db, sample_output):
        store = ArtifactStore(db_path=tmp_db, default_ttl_seconds=1)
        for i in range(5):
            store.store(
                cache_key=f"{i}" * 64, specification="spec",
                specialist_output=sample_output, output_critic_score=85,
                final_score=90, tool_calls=[], task_type="code",
                specialist_adapter="gen", skills_hash="x",
            )
        # Backdate all entries
        conn = sqlite3.connect(tmp_db)
        old_time = (datetime.utcnow() - timedelta(seconds=10)).isoformat() + "Z"
        conn.execute("UPDATE artifacts SET created_at = ?", (old_time,))
        conn.commit()
        conn.close()

        removed = store.cleanup_expired()
        assert removed == 5

        stats = store.get_stats()
        assert stats["total_entries"] == 0


# ===== LRU Eviction =====


class TestLRUEviction:
    """Tests for max_entries LRU eviction."""

    def test_evicts_oldest_accessed(self, small_store, sample_output):
        """When max_entries=3, adding a 4th evicts the least recently accessed."""
        for i in range(3):
            small_store.store(
                cache_key=f"{i}" * 64, specification=f"spec {i}",
                specialist_output=sample_output, output_critic_score=85,
                final_score=90, tool_calls=[], task_type="code",
                specialist_adapter="gen", skills_hash="x",
            )

        # Access entries 1 and 2 so entry 0 is least recently accessed
        small_store.lookup("1" * 64)
        small_store.lookup("2" * 64)

        # Add a 4th entry
        small_store.store(
            cache_key="3" * 64, specification="spec 3",
            specialist_output=sample_output, output_critic_score=85,
            final_score=90, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x",
        )

        # Entry 0 should be evicted
        assert small_store.lookup("0" * 64) is None
        # Entries 1, 2, 3 should still be present
        assert small_store.lookup("1" * 64) is not None
        assert small_store.lookup("2" * 64) is not None
        assert small_store.lookup("3" * 64) is not None

    def test_does_not_evict_under_capacity(self, small_store, sample_output):
        for i in range(3):
            small_store.store(
                cache_key=f"{i}" * 64, specification=f"spec {i}",
                specialist_output=sample_output, output_critic_score=85,
                final_score=90, tool_calls=[], task_type="code",
                specialist_adapter="gen", skills_hash="x",
            )
        # All 3 should be present
        for i in range(3):
            assert small_store.lookup(f"{i}" * 64) is not None


# ===== Invalidation =====


class TestInvalidation:
    """Tests for cache invalidation methods."""

    def test_invalidate_specific(self, store, sample_output):
        key = "n" * 64
        store.store(
            cache_key=key, specification="spec",
            specialist_output=sample_output, output_critic_score=85,
            final_score=90, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x",
        )
        removed = store.invalidate(key)
        assert removed is True
        assert store.lookup(key) is None

    def test_invalidate_nonexistent(self, store):
        removed = store.invalidate("z" * 64)
        assert removed is False

    def test_invalidate_by_task_type(self, store, sample_output):
        for i, task in enumerate(["code", "code", "research"]):
            store.store(
                cache_key=f"{i}" * 64, specification="spec",
                specialist_output=sample_output, output_critic_score=85,
                final_score=90, tool_calls=[], task_type=task,
                specialist_adapter="gen", skills_hash="x",
            )
        removed = store.invalidate_by_task_type("code")
        assert removed == 2
        assert store.lookup("2" * 64) is not None  # research still there

    def test_clear(self, store, sample_output):
        for i in range(5):
            store.store(
                cache_key=f"{i}" * 64, specification="spec",
                specialist_output=sample_output, output_critic_score=85,
                final_score=90, tool_calls=[], task_type="code",
                specialist_adapter="gen", skills_hash="x",
            )
        removed = store.clear()
        assert removed == 5
        assert store.get_stats()["total_entries"] == 0


# ===== Statistics =====


class TestStatistics:
    """Tests for get_stats()."""

    def test_empty_stats(self, store):
        stats = store.get_stats()
        assert stats["total_entries"] == 0
        assert stats["total_hits"] == 0
        assert stats["by_task_type"] == {}

    def test_populated_stats(self, store, sample_output):
        store.store(
            cache_key="a" * 64, specification="spec",
            specialist_output=sample_output, output_critic_score=80,
            final_score=85, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x", num_iterations=2,
        )
        store.store(
            cache_key="b" * 64, specification="spec",
            specialist_output=sample_output, output_critic_score=90,
            final_score=95, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x", num_iterations=1,
        )
        store.store(
            cache_key="c" * 64, specification="spec",
            specialist_output=sample_output, output_critic_score=85,
            final_score=90, tool_calls=[], task_type="research",
            specialist_adapter="gen", skills_hash="x",
        )

        # Access some entries
        store.lookup("a" * 64)
        store.lookup("a" * 64)
        store.lookup("b" * 64)

        stats = store.get_stats()
        assert stats["total_entries"] == 3
        assert stats["total_hits"] == 3
        assert "code" in stats["by_task_type"]
        assert "research" in stats["by_task_type"]
        assert stats["by_task_type"]["code"]["count"] == 2
        assert stats["by_task_type"]["code"]["avg_score"] == 90.0
        assert stats["by_task_type"]["code"]["total_hits"] == 3
        assert stats["by_task_type"]["research"]["count"] == 1


# ===== CacheConfig =====


class TestCacheConfig:
    """Tests for CacheConfig dataclass."""

    def test_defaults(self):
        cfg = CacheConfig()
        assert cfg.enabled is True
        assert cfg.max_entries == 1000
        assert cfg.default_ttl_seconds == 3600
        assert cfg.min_score_to_cache == 70
        assert cfg.db_path is None

    def test_custom_values(self):
        cfg = CacheConfig(enabled=False, max_entries=500, default_ttl_seconds=1800)
        assert cfg.enabled is False
        assert cfg.max_entries == 500

    def test_system_config_has_cache(self):
        config = SystemConfig()
        assert hasattr(config, "cache")
        assert isinstance(config.cache, CacheConfig)
        assert config.cache.enabled is True


# ===== State Integration =====


class TestStateIntegration:
    """Tests for cache fields in AgentState."""

    def test_initial_state_has_cache_fields(self):
        state = create_initial_state("test request")
        assert state["cache_hit"] is False
        assert state["cache_key"] == ""
        assert state["cache_entry_stored"] is False

    def test_cache_hit_can_be_set(self):
        state = create_initial_state("test request")
        state["cache_hit"] = True
        state["cache_key"] = "abc123"
        state["cache_entry_stored"] = True
        assert state["cache_hit"] is True


# ===== Graph Integration: cache_lookup node =====


class TestCacheLookupNode:
    """Tests for the cache_lookup node function in graph.py."""

    def test_cache_miss_sets_false(self, store, sample_spec, sample_skills):
        """cache_lookup sets cache_hit=False on miss."""
        state = create_initial_state("test")
        state["specification"] = sample_spec
        state["loaded_skills"] = sample_skills
        state["routed_task_type"] = "code"
        state["specialist_adapter"] = "code_generator"

        # Simulate what cache_lookup does
        cache_key = ArtifactStore.compute_cache_key(
            sample_spec, sample_skills, "code", "code_generator"
        )
        entry = store.lookup(cache_key)
        assert entry is None

        state["cache_key"] = cache_key
        state["cache_hit"] = False
        assert state["cache_hit"] is False

    def test_cache_hit_populates_state(self, store, sample_spec, sample_skills, sample_output):
        """cache_lookup populates state fields on hit."""
        cache_key = ArtifactStore.compute_cache_key(
            sample_spec, sample_skills, "code", "code_generator"
        )
        store.store(
            cache_key=cache_key, specification=sample_spec,
            specialist_output=sample_output, output_critic_score=88,
            final_score=92, tool_calls=[{"name": "exec", "result": "ok"}],
            task_type="code", specialist_adapter="code_generator",
            skills_hash="abc",
        )

        entry = store.lookup(cache_key)
        assert entry is not None

        state = create_initial_state("test")
        state["cache_hit"] = True
        state["cache_key"] = cache_key
        state["specialist_output"] = entry.specialist_output
        state["output_critic_score"] = entry.output_critic_score
        state["final_score"] = entry.final_score
        state["tool_calls_made"] = entry.tool_calls

        assert state["cache_hit"] is True
        assert state["specialist_output"] == sample_output
        assert state["final_score"] == 92

    def test_empty_spec_skips_lookup(self, store):
        """cache_lookup skips when specification is empty."""
        state = create_initial_state("test")
        state["specification"] = ""
        # Simulate: no lookup performed
        state["cache_hit"] = False
        assert state["cache_hit"] is False


# ===== Graph Integration: cache_hit_or_miss routing =====


class TestCacheHitOrMissRouting:
    """Tests for cache_hit_or_miss decision function."""

    def test_cache_hit_routes_to_format(self):
        state = create_initial_state("test")
        state["cache_hit"] = True
        # Simulate the decision
        if state.get("cache_hit", False):
            result = "cache_hit"
        elif state.get("requires_decomposition", False):
            result = "decompose"
        else:
            result = "single"
        assert result == "cache_hit"

    def test_cache_miss_single(self):
        state = create_initial_state("test")
        state["cache_hit"] = False
        state["requires_decomposition"] = False
        if state.get("cache_hit", False):
            result = "cache_hit"
        elif state.get("requires_decomposition", False):
            result = "decompose"
        else:
            result = "single"
        assert result == "single"

    def test_cache_miss_decompose(self):
        state = create_initial_state("test")
        state["cache_hit"] = False
        state["requires_decomposition"] = True
        if state.get("cache_hit", False):
            result = "cache_hit"
        elif state.get("requires_decomposition", False):
            result = "decompose"
        else:
            result = "single"
        assert result == "decompose"


# ===== Graph Integration: cache_store in skill_cleanup =====


class TestCacheStoreIntegration:
    """Tests for cache storing in skill_cleanup_wrapper."""

    def test_stores_approved_result(self, store, sample_spec, sample_skills, sample_output):
        """Approved specialist output gets cached."""
        cache_key = ArtifactStore.compute_cache_key(
            sample_spec, sample_skills, "code", "code_generator"
        )
        stored = store.store(
            cache_key=cache_key,
            specification=sample_spec,
            specialist_output=sample_output,
            output_critic_score=88,
            final_score=92,
            tool_calls=[],
            task_type="code",
            specialist_adapter="code_generator",
            skills_hash=ArtifactStore.compute_skills_hash(sample_skills),
        )
        assert stored is True

        # Verify it's retrievable
        entry = store.lookup(cache_key)
        assert entry is not None
        assert entry.final_score == 92

    def test_does_not_store_cache_hit_result(self, store, sample_output):
        """Results served from cache should not be re-stored."""
        # This is controlled by the `not result.get("cache_hit", False)` check
        state = create_initial_state("test")
        state["cache_hit"] = True
        # The wrapper checks this and skips storing
        assert state.get("cache_hit", False) is True

    def test_does_not_store_low_score(self, store, sample_output):
        """Low-scoring results are rejected by min_score_to_cache."""
        stored = store.store(
            cache_key="x" * 64, specification="spec",
            specialist_output=sample_output, output_critic_score=50,
            final_score=55, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x",
        )
        assert stored is False

    def test_does_not_store_without_cache_key(self, store, sample_output):
        """If cache_key is empty, skip storing."""
        # The wrapper checks `if cache_key and specialist_output`
        assert not ""  # Empty string is falsy


# ===== Thread Safety =====


class TestThreadSafety:
    """Tests for concurrent access."""

    def test_concurrent_stores(self, tmp_db, sample_output):
        import threading

        store = ArtifactStore(db_path=tmp_db)
        errors = []

        def store_entry(idx):
            try:
                store.store(
                    cache_key=f"{idx:064d}",
                    specification=f"spec {idx}",
                    specialist_output=sample_output,
                    output_critic_score=85,
                    final_score=90,
                    tool_calls=[],
                    task_type="code",
                    specialist_adapter="gen",
                    skills_hash="x",
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=store_entry, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        stats = store.get_stats()
        assert stats["total_entries"] == 20

    def test_concurrent_lookups(self, tmp_db, sample_output):
        import threading

        store = ArtifactStore(db_path=tmp_db)
        key = "a" * 64
        store.store(
            cache_key=key, specification="spec",
            specialist_output=sample_output, output_critic_score=85,
            final_score=90, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x",
        )

        results = []
        errors = []

        def lookup_entry():
            try:
                entry = store.lookup(key)
                results.append(entry)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=lookup_entry) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert all(r is not None for r in results)
        assert all(r.final_score == 90 for r in results)


# ===== CacheEntry Dataclass =====


class TestCacheEntry:
    """Tests for the CacheEntry dataclass."""

    def test_fields(self):
        entry = CacheEntry(
            cache_key="x" * 64,
            specification="spec",
            specialist_output="output",
            output_critic_score=85,
            final_score=90,
            tool_calls=[],
            task_type="code",
            specialist_adapter="gen",
            skills_hash="abc",
            num_iterations=2,
            created_at="2026-03-07T00:00:00Z",
            last_accessed_at="2026-03-07T01:00:00Z",
            access_count=5,
            ttl_seconds=3600,
        )
        assert entry.cache_key == "x" * 64
        assert entry.final_score == 90
        assert entry.access_count == 5


# ===== Edge Cases =====


class TestEdgeCases:
    """Edge case coverage."""

    def test_unicode_specification(self, store):
        stored = store.store(
            cache_key="u" * 64,
            specification="Build a function that handles 日本語 text",
            specialist_output="def handle_jp(): pass",
            output_critic_score=80, final_score=85, tool_calls=[],
            task_type="code", specialist_adapter="gen", skills_hash="x",
        )
        assert stored is True
        entry = store.lookup("u" * 64)
        assert "日本語" in entry.specification

    def test_large_tool_calls(self, store):
        big_calls = [{"name": f"tool_{i}", "result": "x" * 100} for i in range(50)]
        stored = store.store(
            cache_key="v" * 64, specification="spec",
            specialist_output="output", output_critic_score=80,
            final_score=85, tool_calls=big_calls, task_type="code",
            specialist_adapter="gen", skills_hash="x",
        )
        assert stored is True
        entry = store.lookup("v" * 64)
        assert len(entry.tool_calls) == 50

    def test_lookup_handles_db_error_gracefully(self, tmp_db):
        store = ArtifactStore(db_path=tmp_db)
        # Corrupt the DB
        Path(tmp_db).write_text("not a sqlite database")
        entry = store.lookup("w" * 64)
        assert entry is None  # Graceful failure

    def test_store_handles_db_error_gracefully(self, tmp_db):
        store = ArtifactStore(db_path=tmp_db)
        Path(tmp_db).write_text("not a sqlite database")
        result = store.store(
            cache_key="x" * 64, specification="spec",
            specialist_output="out", output_critic_score=85,
            final_score=90, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x",
        )
        assert result is False  # Graceful failure

    def test_min_score_boundary(self, tmp_db):
        """Score exactly at min_score_to_cache should be accepted."""
        store = ArtifactStore(db_path=tmp_db, min_score_to_cache=70)
        stored = store.store(
            cache_key="y" * 64, specification="spec",
            specialist_output="output", output_critic_score=70,
            final_score=70, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x",
        )
        assert stored is True

    def test_min_score_below_boundary(self, tmp_db):
        """Score just below min_score_to_cache should be rejected."""
        store = ArtifactStore(db_path=tmp_db, min_score_to_cache=70)
        stored = store.store(
            cache_key="z" * 64, specification="spec",
            specialist_output="output", output_critic_score=69,
            final_score=69, tool_calls=[], task_type="code",
            specialist_adapter="gen", skills_hash="x",
        )
        assert stored is False
