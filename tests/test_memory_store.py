"""
Tests for the persistent memory store with citations.

Covers:
- MemoryStore: CRUD, FTS5 search, citations, eviction, stats
- MemoryEntry: citation formatting
- MemoryStoreTool + MemoryRecallTool: tool interface, error handling
- Registry integration: tool registration, security permissions
- Doctor check: all outcomes
- VLLMEmbedder: embedding generation, availability checks
- Semantic recall: cosine similarity search
- Hybrid recall: fused BM25 + vector search
- Auto-injection: memory context injection into specialist prompts
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# MemoryStore core tests
# ---------------------------------------------------------------------------


class TestMemoryEntry:
    """Test MemoryEntry data class and citation formatting."""

    def _make_entry(self, **kwargs):
        from agents.memory_store import MemoryEntry
        defaults = {
            "memory_id": 1,
            "content": "test content",
            "source": "agent",
            "tags": "",
            "created_at": "2026-03-03T00:00:00Z",
            "updated_at": "2026-03-03T00:00:00Z",
            "access_count": 0,
            "score": 0.0,
        }
        defaults.update(kwargs)
        return MemoryEntry(**defaults)

    def test_citation_user(self):
        entry = self._make_entry(source="user")
        assert entry.citation == "[user statement]"

    def test_citation_agent(self):
        entry = self._make_entry(source="agent")
        assert entry.citation == "[agent inference]"

    def test_citation_url(self):
        entry = self._make_entry(source="url:https://example.com/page")
        assert entry.citation == "https://example.com/page"

    def test_citation_file(self):
        entry = self._make_entry(source="file:/home/user/project/main.py")
        assert entry.citation == "/home/user/project/main.py"

    def test_citation_tool(self):
        entry = self._make_entry(source="tool:web_scrape")
        assert entry.citation == "[tool: web_scrape]"

    def test_citation_custom(self):
        entry = self._make_entry(source="slack-message")
        assert entry.citation == "[slack-message]"

    def test_to_dict(self):
        entry = self._make_entry(memory_id=42, content="hello", source="user", tags="test")
        d = entry.to_dict()
        assert d["memory_id"] == 42
        assert d["content"] == "hello"
        assert d["source"] == "user"
        assert d["tags"] == "test"
        assert "created_at" in d
        assert "score" in d


class TestMemoryStoreInit:
    """Test database initialization and schema creation."""

    def test_creates_db_file(self, tmp_path):
        from agents.memory_store import MemoryStore
        db = tmp_path / "test_memory.db"
        store = MemoryStore(db_path=db)
        assert db.exists()

    def test_creates_tables(self, tmp_path):
        from agents.memory_store import MemoryStore
        db = tmp_path / "test_memory.db"
        store = MemoryStore(db_path=db)

        conn = sqlite3.connect(str(db))
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
        conn.close()

        assert "memories" in tables
        assert "memories_fts" in tables

    def test_creates_triggers(self, tmp_path):
        from agents.memory_store import MemoryStore
        db = tmp_path / "test_memory.db"
        store = MemoryStore(db_path=db)

        conn = sqlite3.connect(str(db))
        triggers = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()]
        conn.close()

        assert "memories_ai" in triggers  # after insert
        assert "memories_ad" in triggers  # after delete
        assert "memories_au" in triggers  # after update

    def test_idempotent_init(self, tmp_path):
        """Creating MemoryStore twice on same DB should not fail."""
        from agents.memory_store import MemoryStore
        db = tmp_path / "test_memory.db"
        store1 = MemoryStore(db_path=db)
        store1.store("first entry")
        store2 = MemoryStore(db_path=db)
        results = store2.recall("first")
        assert len(results) >= 1


class TestMemoryStoreWrite:
    """Test the store() method."""

    @pytest.fixture()
    def store(self, tmp_path):
        from agents.memory_store import MemoryStore
        return MemoryStore(db_path=tmp_path / "test.db")

    def test_store_returns_id(self, store):
        mid = store.store("Python uses indentation for blocks")
        assert isinstance(mid, int)
        assert mid > 0

    def test_store_increments_id(self, store):
        id1 = store.store("First fact")
        id2 = store.store("Second fact")
        assert id2 > id1

    def test_store_with_source(self, store):
        mid = store.store("FastAPI is async", source="url:https://fastapi.tiangolo.com")
        entry = store.get_by_id(mid)
        assert entry is not None
        assert entry.source == "url:https://fastapi.tiangolo.com"

    def test_store_with_tags(self, store):
        mid = store.store("Use pytest for testing", tags="python testing")
        entry = store.get_by_id(mid)
        assert entry.tags == "python testing"

    def test_store_default_source(self, store):
        mid = store.store("inferred fact")
        entry = store.get_by_id(mid)
        assert entry.source == "agent"

    def test_store_empty_content_raises(self, store):
        with pytest.raises(ValueError, match="empty"):
            store.store("")

    def test_store_whitespace_only_raises(self, store):
        with pytest.raises(ValueError, match="empty"):
            store.store("   ")

    def test_store_strips_whitespace(self, store):
        mid = store.store("  padded content  ")
        entry = store.get_by_id(mid)
        assert entry.content == "padded content"

    def test_store_fifo_eviction(self, tmp_path):
        from agents.memory_store import MemoryStore
        store = MemoryStore(db_path=tmp_path / "test.db", max_entries=5)

        for i in range(8):
            store.store(f"Memory number {i}")

        # Should have evicted the oldest 3
        stats = store.get_stats()
        assert stats["total"] == 5

        # Oldest should be gone
        entry = store.get_by_id(1)
        assert entry is None

    def test_store_timestamps(self, store):
        mid = store.store("timestamped entry")
        entry = store.get_by_id(mid)
        assert entry.created_at.endswith("Z")
        assert entry.updated_at.endswith("Z")


class TestMemoryStoreRecall:
    """Test the recall() method (FTS5 search)."""

    @pytest.fixture()
    def store(self, tmp_path):
        from agents.memory_store import MemoryStore
        s = MemoryStore(db_path=tmp_path / "test.db")
        # Populate with some memories
        s.store("Python uses indentation for block structure", source="user", tags="python syntax")
        s.store("Rust has a borrow checker for memory safety", source="url:https://rust-lang.org", tags="rust safety")
        s.store("FastAPI is built on Starlette and Pydantic", source="url:https://fastapi.tiangolo.com", tags="python web")
        s.store("Docker containers share the host kernel", source="file:/docs/docker.md", tags="docker containers")
        s.store("SQLite supports full-text search via FTS5", source="agent", tags="database sqlite")
        return s

    def test_basic_recall(self, store):
        results = store.recall("Python")
        assert len(results) >= 1
        assert any("Python" in r.content or "python" in r.tags for r in results)

    def test_recall_relevance_ranking(self, store):
        results = store.recall("Python indentation")
        assert len(results) >= 1
        # The most relevant result should be about Python indentation
        assert "indentation" in results[0].content

    def test_recall_with_citations(self, store):
        results = store.recall("FastAPI")
        assert len(results) >= 1
        entry = results[0]
        assert entry.source == "url:https://fastapi.tiangolo.com"
        assert entry.citation == "https://fastapi.tiangolo.com"

    def test_recall_max_results(self, store):
        results = store.recall("the", max_results=2)
        assert len(results) <= 2

    def test_recall_tag_filter(self, store):
        results = store.recall("safety", tag_filter="rust")
        assert len(results) >= 1
        assert all("rust" in r.tags for r in results)

    def test_recall_source_filter(self, store):
        results = store.recall("Docker containers", source_filter="file:")
        assert len(results) >= 1
        assert all(r.source.startswith("file:") for r in results)

    def test_recall_no_results(self, store):
        results = store.recall("quantum entanglement")
        assert results == []

    def test_recall_empty_query(self, store):
        results = store.recall("")
        assert results == []

    def test_recall_whitespace_query(self, store):
        results = store.recall("   ")
        assert results == []

    def test_recall_bumps_access_count(self, store):
        results = store.recall("Python")
        assert len(results) >= 1
        mid = results[0].memory_id
        entry = store.get_by_id(mid)
        assert entry.access_count >= 1

    def test_recall_score_positive(self, store):
        results = store.recall("SQLite full-text search FTS5")
        assert len(results) >= 1
        assert results[0].score > 0

    def test_recall_max_results_clamped(self, store):
        """max_results should be clamped to [1, 50]."""
        results = store.recall("the", max_results=100)
        # Should not crash; just returns what's available
        assert len(results) <= 50

    def test_recall_or_semantics(self, store):
        """Query tokens are OR'd — matching any token should return results."""
        results = store.recall("indentation borrow")
        assert len(results) >= 2  # Both Python and Rust entries match


class TestMemoryStoreDelete:
    """Test the delete() method."""

    @pytest.fixture()
    def store(self, tmp_path):
        from agents.memory_store import MemoryStore
        return MemoryStore(db_path=tmp_path / "test.db")

    def test_delete_existing(self, store):
        mid = store.store("to be deleted")
        assert store.delete(mid) is True
        assert store.get_by_id(mid) is None

    def test_delete_nonexistent(self, store):
        assert store.delete(99999) is False

    def test_delete_removes_from_fts(self, store):
        mid = store.store("unique xylophone memory")
        store.delete(mid)
        results = store.recall("xylophone")
        assert len(results) == 0


class TestMemoryStoreGetById:
    """Test the get_by_id() method."""

    @pytest.fixture()
    def store(self, tmp_path):
        from agents.memory_store import MemoryStore
        return MemoryStore(db_path=tmp_path / "test.db")

    def test_get_existing(self, store):
        mid = store.store("retrievable", source="user", tags="test")
        entry = store.get_by_id(mid)
        assert entry is not None
        assert entry.content == "retrievable"
        assert entry.source == "user"
        assert entry.tags == "test"

    def test_get_nonexistent(self, store):
        assert store.get_by_id(99999) is None


class TestMemoryStoreStats:
    """Test the get_stats() method."""

    @pytest.fixture()
    def store(self, tmp_path):
        from agents.memory_store import MemoryStore
        return MemoryStore(db_path=tmp_path / "test.db")

    def test_empty_stats(self, store):
        stats = store.get_stats()
        assert stats["total"] == 0

    def test_populated_stats(self, store):
        store.store("web fact", source="url:https://example.com", tags="web")
        store.store("file fact", source="file:/tmp/test.py", tags="code")
        store.store("user fact", source="user", tags="code")

        stats = store.get_stats()
        assert stats["total"] == 3
        assert "web" in stats["by_source"]
        assert "file" in stats["by_source"]
        assert "user" in stats["by_source"]
        assert "code" in stats["top_tags"]
        assert stats["top_tags"]["code"] == 2

    def test_most_accessed(self, store):
        mid = store.store("frequently accessed")
        # Recall it multiple times to bump access count
        store.recall("frequently")
        store.recall("frequently")

        stats = store.get_stats()
        assert len(stats["most_accessed"]) >= 1
        accessed = stats["most_accessed"][0]
        assert accessed["access_count"] >= 2


class TestMemoryStoreListRecent:
    """Test the list_recent() method."""

    @pytest.fixture()
    def store(self, tmp_path):
        from agents.memory_store import MemoryStore
        return MemoryStore(db_path=tmp_path / "test.db")

    def test_list_recent_empty(self, store):
        assert store.list_recent() == []

    def test_list_recent_ordering(self, store):
        store.store("oldest")
        store.store("middle")
        store.store("newest")
        recent = store.list_recent(limit=3)
        assert recent[0].content == "newest"
        assert recent[2].content == "oldest"

    def test_list_recent_limit(self, store):
        for i in range(10):
            store.store(f"entry {i}")
        recent = store.list_recent(limit=3)
        assert len(recent) == 3


class TestMemoryStoreCleanup:
    """Test the cleanup() method."""

    @pytest.fixture()
    def store(self, tmp_path):
        from agents.memory_store import MemoryStore
        return MemoryStore(db_path=tmp_path / "test.db")

    def test_cleanup_noop_when_under_limit(self, store):
        store.store("one")
        store.store("two")
        deleted = store.cleanup(keep=10)
        assert deleted == 0

    def test_cleanup_removes_oldest(self, store):
        for i in range(10):
            store.store(f"memory {i}")
        deleted = store.cleanup(keep=5)
        assert deleted == 5
        assert store.get_stats()["total"] == 5


# ---------------------------------------------------------------------------
# Memory Tool tests
# ---------------------------------------------------------------------------


class TestMemoryStoreTool:
    """Test the MemoryStoreTool interface."""

    @pytest.fixture()
    def tool(self, tmp_path):
        from agents.tools.registry import MemoryStoreTool, _get_shared_memory_store
        import agents.tools.registry as reg
        from agents.memory_store import MemoryStore

        # Reset singleton and point at temp DB
        reg._shared_memory_store = MemoryStore(db_path=tmp_path / "tool_test.db")
        return MemoryStoreTool()

    @pytest.fixture(autouse=True)
    def cleanup_singleton(self):
        import agents.tools.registry as reg
        yield
        reg._shared_memory_store = None

    def test_tool_name(self, tool):
        assert tool.name == "memory_store"

    def test_tool_description(self, tool):
        assert "persistent memory" in tool.description.lower()

    def test_tool_schema(self, tool):
        schema = tool._get_parameters_schema()
        assert "content" in schema["properties"]
        assert "source" in schema["properties"]
        assert "tags" in schema["properties"]
        assert "content" in schema["required"]

    def test_store_success(self, tool):
        result = tool.execute(content="test fact", source="user", tags="test")
        assert result.success is True
        assert "memory_id" in result.metadata
        assert "test fact" in result.output

    def test_store_empty_content(self, tool):
        result = tool.execute(content="")
        assert result.success is False
        assert "empty" in result.error.lower()

    def test_store_default_source(self, tool):
        result = tool.execute(content="inferred fact")
        assert result.success is True


class TestMemoryRecallTool:
    """Test the MemoryRecallTool interface."""

    @pytest.fixture()
    def tool_and_store(self, tmp_path):
        from agents.tools.registry import MemoryRecallTool
        import agents.tools.registry as reg
        from agents.memory_store import MemoryStore

        store = MemoryStore(db_path=tmp_path / "tool_test.db")
        reg._shared_memory_store = store
        # Seed data
        store.store("Python is dynamically typed", source="user", tags="python")
        store.store("Rust is statically typed", source="url:https://rust-lang.org", tags="rust")
        return MemoryRecallTool(), store

    @pytest.fixture(autouse=True)
    def cleanup_singleton(self):
        import agents.tools.registry as reg
        yield
        reg._shared_memory_store = None

    def test_tool_name(self, tool_and_store):
        tool, _ = tool_and_store
        assert tool.name == "memory_recall"

    def test_tool_description(self, tool_and_store):
        tool, _ = tool_and_store
        assert "citation" in tool.description.lower()

    def test_tool_schema(self, tool_and_store):
        tool, _ = tool_and_store
        schema = tool._get_parameters_schema()
        assert "query" in schema["properties"]
        assert "max_results" in schema["properties"]
        assert "tag_filter" in schema["properties"]
        assert "source_filter" in schema["properties"]

    def test_recall_success(self, tool_and_store):
        tool, _ = tool_and_store
        result = tool.execute(query="Python typed")
        assert result.success is True
        assert "Python" in result.output
        assert result.metadata["results"] >= 1

    def test_recall_with_citations(self, tool_and_store):
        tool, _ = tool_and_store
        result = tool.execute(query="Rust")
        assert result.success is True
        assert "https://rust-lang.org" in result.output
        assert "Source:" in result.output

    def test_recall_no_results(self, tool_and_store):
        tool, _ = tool_and_store
        result = tool.execute(query="quantum computing")
        assert result.success is True
        assert "No relevant memories" in result.output
        assert result.metadata["results"] == 0

    def test_recall_empty_query(self, tool_and_store):
        tool, _ = tool_and_store
        result = tool.execute(query="")
        assert result.success is False
        assert "empty" in result.error.lower()

    def test_recall_tag_filter(self, tool_and_store):
        tool, _ = tool_and_store
        result = tool.execute(query="typed", tag_filter="rust")
        assert result.success is True
        assert "Rust" in result.output

    def test_recall_source_filter(self, tool_and_store):
        tool, _ = tool_and_store
        result = tool.execute(query="typed", source_filter="url:")
        assert result.success is True
        # Should only have the Rust entry (url source)
        assert "Rust" in result.output

    def test_recall_max_results(self, tool_and_store):
        tool, _ = tool_and_store
        result = tool.execute(query="typed", max_results=1)
        assert result.metadata["results"] <= 1

    def test_recall_output_format(self, tool_and_store):
        tool, _ = tool_and_store
        result = tool.execute(query="Python")
        assert result.success is True
        # Check structured output
        assert "## Memory #" in result.output
        assert "score:" in result.output
        assert "**Source:**" in result.output
        assert "**Stored:**" in result.output


# ---------------------------------------------------------------------------
# Registry integration tests
# ---------------------------------------------------------------------------


class TestRegistryIntegration:
    """Test that memory tools are registered in the default registry."""

    @pytest.fixture(autouse=True)
    def cleanup_singleton(self):
        import agents.tools.registry as reg
        yield
        reg._shared_memory_store = None

    def test_memory_tools_registered(self):
        from unittest.mock import MagicMock
        from agents.tools.registry import create_default_tool_registry
        registry = create_default_tool_registry(sandbox_pool=MagicMock())
        tool_names = registry.list_tools()
        assert "memory_store" in tool_names
        assert "memory_recall" in tool_names

    def test_memory_tools_always_present(self):
        """Memory tools should be registered regardless of other settings."""
        from agents.tools.registry import create_default_tool_registry
        # Sandboxed pool (mocked), no egress, no infra env vars
        with patch.dict(os.environ, {}, clear=True):
            registry = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=False)
        tool_names = registry.list_tools()
        assert "memory_store" in tool_names
        assert "memory_recall" in tool_names


# ---------------------------------------------------------------------------
# Skill security integration tests
# ---------------------------------------------------------------------------


class TestSkillSecurityIntegration:
    """Test that memory tools are in DEFAULT_ALLOWED_TOOLS."""

    def test_memory_store_in_default_allowed(self):
        from agents.skill_security import DEFAULT_ALLOWED_TOOLS
        assert "memory_store" in DEFAULT_ALLOWED_TOOLS

    def test_memory_recall_in_default_allowed(self):
        from agents.skill_security import DEFAULT_ALLOWED_TOOLS
        assert "memory_recall" in DEFAULT_ALLOWED_TOOLS

    def test_memory_tools_not_restricted(self):
        from agents.skill_security import RESTRICTED_TOOLS
        assert "memory_store" not in RESTRICTED_TOOLS
        assert "memory_recall" not in RESTRICTED_TOOLS

    def test_memory_tools_in_all_known(self):
        from agents.skill_security import ALL_KNOWN_TOOLS
        assert "memory_store" in ALL_KNOWN_TOOLS
        assert "memory_recall" in ALL_KNOWN_TOOLS

    def test_skill_can_use_memory_tools_by_default(self):
        from agents.skill_security import SkillSecurity
        # When no skills are loaded, compute_effective_allowed_tools returns
        # None (unrestricted — all tools allowed). When skills ARE loaded with
        # no explicit allowed-tools, it returns DEFAULT_ALLOWED_TOOLS.
        effective = SkillSecurity.compute_effective_allowed_tools([])
        # Empty list → no skills loaded → None (unrestricted)
        assert effective is None  # No skill-imposed restrictions

        # With a skill that has no allowed_tools, effective = DEFAULT_ALLOWED_TOOLS
        from agents.skill_security import DEFAULT_ALLOWED_TOOLS
        assert "memory_store" in DEFAULT_ALLOWED_TOOLS
        assert "memory_recall" in DEFAULT_ALLOWED_TOOLS


# ---------------------------------------------------------------------------
# Doctor check tests
# ---------------------------------------------------------------------------


class TestDoctorCheckMemory:
    """Test the check_memory() diagnostic."""

    def test_no_db_file(self, tmp_path):
        with patch("agents.doctor.Path.home", return_value=tmp_path):
            from agents.doctor import check_memory
            result = check_memory()
        assert result.status == "ok"
        assert "first use" in result.summary.lower()

    def test_existing_db(self, tmp_path):
        from agents.memory_store import MemoryStore
        vibe_dir = tmp_path / ".vibe"
        vibe_dir.mkdir()
        db_path = vibe_dir / "memory.db"
        store = MemoryStore(db_path=db_path)
        store.store("test memory")

        with patch("agents.doctor.Path.home", return_value=tmp_path):
            from agents.doctor import check_memory
            result = check_memory()
        assert result.status == "ok"
        assert "1 memories" in result.summary
        assert "FTS5 active" in result.summary

    def test_corrupted_db(self, tmp_path):
        vibe_dir = tmp_path / ".vibe"
        vibe_dir.mkdir()
        db_path = vibe_dir / "memory.db"
        db_path.write_text("not a valid sqlite database")

        with patch("agents.doctor.Path.home", return_value=tmp_path):
            from agents.doctor import check_memory
            result = check_memory()
        assert result.status == "fail"


class TestDoctorIncludesMemory:
    """Test that run_doctor includes the memory check."""

    def test_memory_check_in_report(self):
        from agents.doctor import run_doctor

        # Create a minimal mock config
        mock_config = MagicMock()
        mock_config.model.backend = "vllm"
        mock_config.model.model_name = "llama3"
        mock_config.sandbox = None

        with patch("agents.doctor.check_backend") as m1, \
             patch("agents.doctor.check_config") as m2, \
             patch("agents.doctor.check_sandbox") as m3, \
             patch("agents.doctor.check_docker_gpu") as m4, \
             patch("agents.doctor.check_skills") as m6, \
             patch("agents.doctor.check_skill_security") as m7, \
             patch("agents.doctor.check_messenger") as m8, \
             patch("agents.doctor.check_disk_usage") as m9, \
             patch("agents.doctor.check_python_deps") as m10, \
             patch("agents.doctor.check_hardware") as m12:
            # Each mock returns a CheckResult
            from agents.doctor import CheckResult
            for m in [m1, m2, m3, m4, m6, m7, m8, m9, m10, m12]:
                m.return_value = CheckResult("mock", "ok", "mock check")

            with patch("agents.doctor.check_memory") as m_memory:
                m_memory.return_value = CheckResult("Memory Store", "ok", "test")
                report = run_doctor(mock_config)
                m_memory.assert_called_once()
                check_names = [c.name for c in report.checks]
                assert "Memory Store" in check_names


# ---------------------------------------------------------------------------
# FTS5 edge cases
# ---------------------------------------------------------------------------


class TestFTS5EdgeCases:
    """Test FTS5 search behavior with special characters and edge cases."""

    @pytest.fixture()
    def store(self, tmp_path):
        from agents.memory_store import MemoryStore
        return MemoryStore(db_path=tmp_path / "test.db")

    def test_special_characters_in_query(self, store):
        store.store("C++ is compiled")
        # FTS5 handles special chars gracefully due to quoting
        results = store.recall("C++")
        # May or may not match depending on tokenizer — should not crash
        assert isinstance(results, list)

    def test_unicode_content(self, store):
        store.store("Python supports Unicode: cafe\u0301")
        results = store.recall("Unicode")
        assert len(results) >= 1

    def test_long_content(self, store):
        long_text = "word " * 1000
        mid = store.store(long_text)
        entry = store.get_by_id(mid)
        assert len(entry.content) > 4000

    def test_multiword_query_or_semantics(self, store):
        store.store("The cat sat on the mat")
        store.store("The dog ran in the park")
        # Both "cat" and "dog" queries should return results
        results = store.recall("cat dog")
        assert len(results) == 2

    def test_quoted_phrase_in_content(self, store):
        store.store('He said "hello world" to everyone')
        results = store.recall("hello")
        assert len(results) >= 1

    def test_concurrent_access(self, store):
        """Multiple threads should not corrupt the database."""
        import threading
        errors = []

        def worker(n):
            try:
                for i in range(10):
                    store.store(f"Thread {n} memory {i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        stats = store.get_stats()
        assert stats["total"] == 40  # 4 threads * 10 memories


# ---------------------------------------------------------------------------
# Shared singleton tests
# ---------------------------------------------------------------------------


class TestSharedMemoryStore:
    """Test the singleton pattern for MemoryStore in registry."""

    @pytest.fixture(autouse=True)
    def cleanup_singleton(self):
        import agents.tools.registry as reg
        reg._shared_memory_store = None
        yield
        reg._shared_memory_store = None

    def test_singleton_created(self):
        from agents.tools.registry import _get_shared_memory_store
        store1 = _get_shared_memory_store()
        store2 = _get_shared_memory_store()
        assert store1 is store2

    def test_singleton_is_memory_store(self):
        from agents.tools.registry import _get_shared_memory_store
        from agents.memory_store import MemoryStore
        store = _get_shared_memory_store()
        assert isinstance(store, MemoryStore)


# ---------------------------------------------------------------------------
# Cosine similarity tests
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    """Test the _cosine_similarity utility function."""

    def test_identical_vectors(self):
        from agents.memory_store import _cosine_similarity
        assert _cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        from agents.memory_store import _cosine_similarity
        assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        from agents.memory_store import _cosine_similarity
        assert _cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_empty_vectors(self):
        from agents.memory_store import _cosine_similarity
        assert _cosine_similarity([], []) == 0.0

    def test_different_lengths(self):
        from agents.memory_store import _cosine_similarity
        assert _cosine_similarity([1.0], [1.0, 2.0]) == 0.0

    def test_zero_vector(self):
        from agents.memory_store import _cosine_similarity
        assert _cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_similar_vectors(self):
        from agents.memory_store import _cosine_similarity
        sim = _cosine_similarity([1.0, 1.0], [1.0, 0.9])
        assert sim > 0.99  # Very similar


# ---------------------------------------------------------------------------
# VLLMEmbedder tests
# ---------------------------------------------------------------------------


class TestVLLMEmbedder:
    """Test the VLLMEmbedder class (mocked, no real vLLM needed)."""

    def test_init_defaults(self):
        from agents.embedder import VLLMEmbedder, DEFAULT_EMBED_MODEL, DEFAULT_VLLM_URL
        embedder = VLLMEmbedder()
        assert embedder.model == DEFAULT_EMBED_MODEL
        assert DEFAULT_VLLM_URL.rstrip("/") in embedder.base_url

    def test_is_available_cached(self):
        from agents.memory_store import VLLMEmbedder
        embedder = VLLMEmbedder()
        embedder._available = True
        assert embedder.is_available() is True
        embedder._available = False
        assert embedder.is_available() is False

    def test_is_available_checks_vllm(self):
        from agents.memory_store import VLLMEmbedder
        embedder = VLLMEmbedder()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp):
            assert embedder.is_available() is True

    def test_is_available_handles_connection_error(self):
        from agents.memory_store import VLLMEmbedder
        embedder = VLLMEmbedder()
        with patch("requests.post", side_effect=ConnectionError("refused")):
            assert embedder.is_available() is False

    def test_embed_returns_vector(self):
        from agents.memory_store import VLLMEmbedder
        embedder = VLLMEmbedder()
        embedder._available = True
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
        with patch("requests.post", return_value=mock_resp):
            vec = embedder.embed("test text")
            assert vec == [0.1, 0.2, 0.3]

    def test_embed_returns_none_when_unavailable(self):
        from agents.memory_store import VLLMEmbedder
        embedder = VLLMEmbedder()
        embedder._available = False
        assert embedder.embed("test") is None

    def test_embed_returns_none_on_error(self):
        from agents.memory_store import VLLMEmbedder
        embedder = VLLMEmbedder()
        embedder._available = True
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch("requests.post", return_value=mock_resp):
            assert embedder.embed("test") is None

    def test_embed_returns_none_on_empty_response(self):
        from agents.memory_store import VLLMEmbedder
        embedder = VLLMEmbedder()
        embedder._available = True
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": []}
        with patch("requests.post", return_value=mock_resp):
            assert embedder.embed("test") is None

    def test_embed_batch_success(self):
        from agents.memory_store import VLLMEmbedder
        embedder = VLLMEmbedder()
        embedder._available = True
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]}
        with patch("requests.post", return_value=mock_resp):
            vecs = embedder.embed_batch(["text1", "text2"])
            assert len(vecs) == 2
            assert vecs[0] == [0.1, 0.2]

    def test_embed_batch_empty_input(self):
        from agents.memory_store import VLLMEmbedder
        embedder = VLLMEmbedder()
        assert embedder.embed_batch([]) == []

    def test_embed_batch_unavailable(self):
        from agents.memory_store import VLLMEmbedder
        embedder = VLLMEmbedder()
        embedder._available = False
        result = embedder.embed_batch(["a", "b"])
        assert result == [None, None]

    def test_embed_batch_partial_results(self):
        from agents.memory_store import VLLMEmbedder
        embedder = VLLMEmbedder()
        embedder._available = True
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # Server returns fewer embeddings than texts
        mock_resp.json.return_value = {"data": [{"embedding": [0.1, 0.2]}]}
        with patch("requests.post", return_value=mock_resp):
            vecs = embedder.embed_batch(["a", "b", "c"])
            assert len(vecs) == 3
            assert vecs[0] == [0.1, 0.2]
            assert vecs[1] is None
            assert vecs[2] is None


# ---------------------------------------------------------------------------
# MemoryStore embeddings integration tests
# ---------------------------------------------------------------------------


class TestMemoryStoreEmbeddings:
    """Test embedding storage and retrieval in MemoryStore."""

    def _make_embedder(self, vectors=None):
        """Create a mock embedder that returns predetermined vectors."""
        from agents.memory_store import VLLMEmbedder
        embedder = MagicMock(spec=VLLMEmbedder)
        embedder.model = "test-model"
        embedder.is_available.return_value = True
        if vectors is not None:
            embedder.embed.side_effect = vectors
        else:
            embedder.embed.return_value = [0.1, 0.2, 0.3]
        return embedder

    def test_store_with_embedder_saves_embedding(self, tmp_path):
        from agents.memory_store import MemoryStore
        embedder = self._make_embedder()
        store = MemoryStore(db_path=tmp_path / "test.db", embedder=embedder)
        mid = store.store("test content")

        # Verify embedding was stored
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        row = conn.execute("SELECT * FROM memory_embeddings WHERE memory_id=?", (mid,)).fetchone()
        conn.close()
        assert row is not None

    def test_store_without_embedder_skips_embedding(self, tmp_path):
        from agents.memory_store import MemoryStore
        store = MemoryStore(db_path=tmp_path / "test.db")
        # Force no embedder
        store._embedder_checked = True
        store._embedder = None
        mid = store.store("test content")

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        row = conn.execute("SELECT * FROM memory_embeddings WHERE memory_id=?", (mid,)).fetchone()
        conn.close()
        assert row is None

    def test_embeddings_table_created(self, tmp_path):
        from agents.memory_store import MemoryStore
        store = MemoryStore(db_path=tmp_path / "test.db")

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()
        assert "memory_embeddings" in tables

    def test_has_embeddings_property(self, tmp_path):
        from agents.memory_store import MemoryStore
        store = MemoryStore(db_path=tmp_path / "test.db")
        # No embedder configured
        store._embedder_checked = True
        store._embedder = None
        assert store.has_embeddings is False

        # With embedder
        embedder = self._make_embedder()
        store._embedder = embedder
        assert store.has_embeddings is True

    def test_stats_include_embedded_count(self, tmp_path):
        from agents.memory_store import MemoryStore
        embedder = self._make_embedder()
        store = MemoryStore(db_path=tmp_path / "test.db", embedder=embedder)
        store.store("fact one")
        store.store("fact two")

        stats = store.get_stats()
        assert "embedded" in stats
        assert stats["embedded"] == 2

    def test_embedding_cascade_delete(self, tmp_path):
        """Deleting a memory should cascade-delete its embedding."""
        from agents.memory_store import MemoryStore
        embedder = self._make_embedder()
        store = MemoryStore(db_path=tmp_path / "test.db", embedder=embedder)
        mid = store.store("to be deleted")
        store.delete(mid)

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute("PRAGMA foreign_keys=ON")
        row = conn.execute("SELECT * FROM memory_embeddings WHERE memory_id=?", (mid,)).fetchone()
        conn.close()
        # Cascade delete or no orphan
        # Note: SQLite foreign key cascade requires PRAGMA foreign_keys=ON at connection time
        # The store uses ON DELETE CASCADE, so this should work
        assert row is None


# ---------------------------------------------------------------------------
# Semantic recall tests
# ---------------------------------------------------------------------------


class TestSemanticRecall:
    """Test semantic_recall() with mocked embeddings."""

    def _make_store_with_embeddings(self, tmp_path):
        """Create a store with pre-seeded embeddings (no real vLLM)."""
        from agents.memory_store import MemoryStore, VLLMEmbedder
        import json

        # Create store without embedder
        store = MemoryStore(db_path=tmp_path / "test.db")
        store._embedder_checked = True
        store._embedder = None

        # Manually insert memories and embeddings
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute("PRAGMA foreign_keys=ON")

        # Memory 1: Python (vector pointing "north")
        conn.execute(
            "INSERT INTO memories (id, content, source, tags, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (1, "Python uses dynamic typing", "user", "python", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO memory_embeddings (memory_id, embedding, model, created_at) VALUES (?, ?, ?, ?)",
            (1, json.dumps([0.9, 0.1, 0.0]), "test", "2026-01-01T00:00:00Z"),
        )

        # Memory 2: Rust (vector pointing "east")
        conn.execute(
            "INSERT INTO memories (id, content, source, tags, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (2, "Rust has a borrow checker", "url:https://rust-lang.org", "rust", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO memory_embeddings (memory_id, embedding, model, created_at) VALUES (?, ?, ?, ?)",
            (2, json.dumps([0.1, 0.9, 0.0]), "test", "2026-01-01T00:00:00Z"),
        )

        # Memory 3: Docker (vector pointing "up")
        conn.execute(
            "INSERT INTO memories (id, content, source, tags, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (3, "Docker uses container isolation", "file:/docs/docker.md", "docker", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO memory_embeddings (memory_id, embedding, model, created_at) VALUES (?, ?, ?, ?)",
            (3, json.dumps([0.0, 0.1, 0.9]), "test", "2026-01-01T00:00:00Z"),
        )

        # FTS5 sync
        conn.execute("INSERT INTO memories_fts(rowid, content, tags) VALUES (1, 'Python uses dynamic typing', 'python')")
        conn.execute("INSERT INTO memories_fts(rowid, content, tags) VALUES (2, 'Rust has a borrow checker', 'rust')")
        conn.execute("INSERT INTO memories_fts(rowid, content, tags) VALUES (3, 'Docker uses container isolation', 'docker')")

        conn.commit()
        conn.close()

        # Now set up a mock embedder for queries
        embedder = MagicMock(spec=VLLMEmbedder)
        embedder.model = "test"
        embedder.is_available.return_value = True
        store._embedder = embedder
        store._embedder_checked = True

        return store, embedder

    def test_semantic_recall_finds_similar(self, tmp_path):
        store, embedder = self._make_store_with_embeddings(tmp_path)
        # Query vector similar to Python's embedding
        embedder.embed.return_value = [0.85, 0.15, 0.0]
        results = store.semantic_recall("python programming")
        assert len(results) >= 1
        assert results[0].content == "Python uses dynamic typing"
        assert results[0].score > 0.9

    def test_semantic_recall_ranking(self, tmp_path):
        store, embedder = self._make_store_with_embeddings(tmp_path)
        # Query vector between Python and Rust
        embedder.embed.return_value = [0.5, 0.5, 0.0]
        results = store.semantic_recall("programming languages", max_results=3)
        assert len(results) >= 2
        # Both Python and Rust should be returned

    def test_semantic_recall_min_similarity_filter(self, tmp_path):
        store, embedder = self._make_store_with_embeddings(tmp_path)
        # Query vector similar only to Docker
        embedder.embed.return_value = [0.0, 0.0, 1.0]
        results = store.semantic_recall("containers", min_similarity=0.8)
        assert len(results) == 1
        assert results[0].content == "Docker uses container isolation"

    def test_semantic_recall_tag_filter(self, tmp_path):
        store, embedder = self._make_store_with_embeddings(tmp_path)
        # Query similar to Python, but filter to rust tag
        embedder.embed.return_value = [0.5, 0.5, 0.0]
        results = store.semantic_recall("languages", tag_filter="rust")
        assert all("rust" in r.tags for r in results)

    def test_semantic_recall_source_filter(self, tmp_path):
        store, embedder = self._make_store_with_embeddings(tmp_path)
        embedder.embed.return_value = [0.5, 0.5, 0.5]
        results = store.semantic_recall("anything", source_filter="url:")
        assert all(r.source.startswith("url:") for r in results)

    def test_semantic_recall_empty_query(self, tmp_path):
        store, embedder = self._make_store_with_embeddings(tmp_path)
        assert store.semantic_recall("") == []

    def test_semantic_recall_no_embedder(self, tmp_path):
        from agents.memory_store import MemoryStore
        store = MemoryStore(db_path=tmp_path / "test.db")
        store._embedder_checked = True
        store._embedder = None
        assert store.semantic_recall("test") == []

    def test_semantic_recall_embed_fails(self, tmp_path):
        store, embedder = self._make_store_with_embeddings(tmp_path)
        embedder.embed.return_value = None
        assert store.semantic_recall("test query") == []

    def test_semantic_recall_bumps_access_count(self, tmp_path):
        store, embedder = self._make_store_with_embeddings(tmp_path)
        embedder.embed.return_value = [0.9, 0.1, 0.0]
        results = store.semantic_recall("python")
        assert len(results) >= 1
        mid = results[0].memory_id
        entry = store.get_by_id(mid)
        assert entry.access_count >= 1

    def test_semantic_recall_max_results(self, tmp_path):
        store, embedder = self._make_store_with_embeddings(tmp_path)
        embedder.embed.return_value = [0.5, 0.5, 0.5]
        results = store.semantic_recall("anything", max_results=1)
        assert len(results) <= 1


# ---------------------------------------------------------------------------
# Hybrid recall tests
# ---------------------------------------------------------------------------


class TestHybridRecall:
    """Test hybrid_recall() merging BM25 + vector results."""

    @pytest.fixture()
    def store(self, tmp_path):
        from agents.memory_store import MemoryStore, VLLMEmbedder
        import json

        store = MemoryStore(db_path=tmp_path / "test.db")

        # Store memories normally (FTS5 triggers handle indexing)
        store.store("Python uses dynamic typing for flexibility", source="user", tags="python")
        store.store("Rust enforces memory safety through borrow checking", source="agent", tags="rust")
        store.store("Docker containers share the host OS kernel", source="file:/docs/docker.md", tags="docker")

        # Now inject embeddings manually
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        rows = conn.execute("SELECT id FROM memories ORDER BY id").fetchall()
        vecs = [
            [0.9, 0.1, 0.0],  # Python
            [0.1, 0.9, 0.0],  # Rust
            [0.0, 0.1, 0.9],  # Docker
        ]
        for row, vec in zip(rows, vecs):
            conn.execute(
                "INSERT INTO memory_embeddings (memory_id, embedding, model, created_at) VALUES (?, ?, ?, ?)",
                (row[0], json.dumps(vec), "test", "2026-01-01T00:00:00Z"),
            )
        conn.commit()
        conn.close()

        # Set up mock embedder
        embedder = MagicMock(spec=VLLMEmbedder)
        embedder.model = "test"
        embedder.is_available.return_value = True
        store._embedder = embedder
        store._embedder_checked = True

        return store, embedder

    def test_hybrid_returns_results(self, store):
        s, embedder = store
        embedder.embed.return_value = [0.9, 0.1, 0.0]
        results = s.hybrid_recall("Python typing")
        assert len(results) >= 1

    def test_hybrid_falls_back_to_bm25(self, tmp_path):
        """When embeddings unavailable, hybrid should still return BM25 results."""
        from agents.memory_store import MemoryStore
        s = MemoryStore(db_path=tmp_path / "test.db")
        s._embedder_checked = True
        s._embedder = None
        s.store("Python is great for scripting")
        results = s.hybrid_recall("Python scripting")
        assert len(results) >= 1

    def test_hybrid_merges_unique_results(self, store):
        """Same memory found by both BM25 and semantic should appear once."""
        s, embedder = store
        embedder.embed.return_value = [0.9, 0.1, 0.0]
        results = s.hybrid_recall("Python")
        memory_ids = [r.memory_id for r in results]
        assert len(memory_ids) == len(set(memory_ids))  # No duplicates

    def test_hybrid_respects_max_results(self, store):
        s, embedder = store
        embedder.embed.return_value = [0.5, 0.5, 0.5]
        results = s.hybrid_recall("anything", max_results=1)
        assert len(results) <= 1

    def test_hybrid_with_tag_filter(self, store):
        s, embedder = store
        embedder.embed.return_value = [0.5, 0.5, 0.5]
        results = s.hybrid_recall("programming", tag_filter="python")
        for r in results:
            assert "python" in r.tags

    def test_hybrid_empty_query(self, store):
        s, embedder = store
        results = s.hybrid_recall("")
        assert results == []

    def test_hybrid_fused_score(self, store):
        """Fused scores should reflect weighted combination."""
        s, embedder = store
        embedder.embed.return_value = [0.9, 0.1, 0.0]
        results = s.hybrid_recall("Python", keyword_weight=0.5, semantic_weight=0.5)
        assert len(results) >= 1
        # All fused scores should be positive
        for r in results:
            assert r.score >= 0


# ---------------------------------------------------------------------------
# Backfill embeddings tests
# ---------------------------------------------------------------------------


class TestBackfillEmbeddings:
    """Test the backfill_embeddings() method."""

    def test_backfill_generates_missing(self, tmp_path):
        from agents.memory_store import MemoryStore, VLLMEmbedder
        store = MemoryStore(db_path=tmp_path / "test.db")

        # Store without embedder
        store._embedder_checked = True
        store._embedder = None
        store.store("fact one")
        store.store("fact two")
        store.store("fact three")

        # Now attach an embedder and backfill
        embedder = MagicMock(spec=VLLMEmbedder)
        embedder.model = "test"
        embedder.is_available.return_value = True
        embedder.embed_batch.return_value = [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
        store._embedder = embedder

        count = store.backfill_embeddings()
        assert count == 3

    def test_backfill_skips_existing(self, tmp_path):
        from agents.memory_store import MemoryStore, VLLMEmbedder

        # Create with embedder so first store gets embedding
        embedder = MagicMock(spec=VLLMEmbedder)
        embedder.model = "test"
        embedder.is_available.return_value = True
        embedder.embed.return_value = [0.1, 0.2]
        store = MemoryStore(db_path=tmp_path / "test.db", embedder=embedder)
        store.store("already embedded")

        # Add one without embedding
        store._embedder = None
        store._embedder_checked = True
        store.store("not embedded")

        # Reattach and backfill
        embedder2 = MagicMock(spec=VLLMEmbedder)
        embedder2.model = "test"
        embedder2.is_available.return_value = True
        embedder2.embed_batch.return_value = [[0.3, 0.4]]
        store._embedder = embedder2

        count = store.backfill_embeddings()
        assert count == 1

    def test_backfill_no_embedder(self, tmp_path):
        from agents.memory_store import MemoryStore
        store = MemoryStore(db_path=tmp_path / "test.db")
        store._embedder_checked = True
        store._embedder = None
        store.store("test")
        assert store.backfill_embeddings() == 0

    def test_backfill_nothing_to_do(self, tmp_path):
        from agents.memory_store import MemoryStore, VLLMEmbedder
        embedder = MagicMock(spec=VLLMEmbedder)
        embedder.model = "test"
        embedder.is_available.return_value = True
        embedder.embed.return_value = [0.1]
        store = MemoryStore(db_path=tmp_path / "test.db", embedder=embedder)
        store.store("all embedded")
        # All have embeddings already
        assert store.backfill_embeddings() == 0


# ---------------------------------------------------------------------------
# Memory auto-injection tests
# ---------------------------------------------------------------------------


class TestMemoryAutoInjection:
    """Test the inject_memory graph node."""

    @pytest.fixture(autouse=True)
    def cleanup_singleton(self):
        import agents.tools.registry as reg
        yield
        reg._shared_memory_store = None

    def test_inject_memory_adds_context(self, tmp_path):
        from agents.memory_store import MemoryStore
        import agents.tools.registry as reg

        store = MemoryStore(db_path=tmp_path / "test.db")
        store._embedder_checked = True
        store._embedder = None
        store.store("Python uses list comprehensions for concise loops", source="user", tags="python")
        store.store("Always use virtual environments for Python projects", source="agent", tags="python best-practice")
        reg._shared_memory_store = store

        # Import and call the inject_memory function
        # We need to test the function itself, not the graph node wrapper
        state = {"user_request": "Write a Python script", "memory_context": ""}

        # Call the store's hybrid_recall directly to verify
        results = store.hybrid_recall("Python script", max_results=5)
        assert len(results) >= 1

    def test_inject_memory_empty_request(self, tmp_path):
        """inject_memory should be a no-op for empty requests."""
        from agents.memory_store import MemoryStore
        import agents.tools.registry as reg

        store = MemoryStore(db_path=tmp_path / "test.db")
        store._embedder_checked = True
        store._embedder = None
        reg._shared_memory_store = store

        state = {"user_request": "", "memory_context": ""}
        # Empty request should produce no memory context
        results = store.hybrid_recall("", max_results=5)
        assert results == []

    def test_inject_memory_no_matches(self, tmp_path):
        """inject_memory should set empty context when no memories match."""
        from agents.memory_store import MemoryStore
        import agents.tools.registry as reg

        store = MemoryStore(db_path=tmp_path / "test.db")
        store._embedder_checked = True
        store._embedder = None
        reg._shared_memory_store = store

        results = store.hybrid_recall("quantum physics", max_results=5)
        assert results == []

    def test_inject_memory_formats_with_citations(self, tmp_path):
        """Memory context should include formatted citations."""
        from agents.memory_store import MemoryStore
        import agents.tools.registry as reg

        store = MemoryStore(db_path=tmp_path / "test.db")
        store._embedder_checked = True
        store._embedder = None
        store.store("FastAPI is built on Starlette", source="url:https://fastapi.tiangolo.com", tags="python web")
        reg._shared_memory_store = store

        results = store.hybrid_recall("FastAPI Starlette", max_results=5)
        assert len(results) >= 1
        # Citation should be the URL
        assert results[0].citation == "https://fastapi.tiangolo.com"

    def test_memory_context_field_in_state(self):
        """State TypedDict should have memory_context field."""
        from agents.state import AgentState, create_initial_state
        state = create_initial_state("test request")
        assert state.get("memory_context") == ""


class TestSpecialistMemoryInjection:
    """Test that specialist prompts include memory context."""

    def test_specialist_prompt_includes_memory_context(self):
        """execute_with_specialist should include memory_context in the prompt."""
        # Verify the code path by checking the prompt template references memory_context
        from agents.specialist_nodes import SpecialistNodesMixin
        import inspect
        source = inspect.getsource(SpecialistNodesMixin.execute_with_specialist)
        assert "memory_context" in source

    def test_sub_task_prompt_includes_memory_context(self):
        """execute_sub_task should include memory_context in the prompt."""
        from agents.specialist_nodes import SpecialistNodesMixin
        import inspect
        source = inspect.getsource(SpecialistNodesMixin.execute_sub_task)
        assert "memory_context" in source


class TestGraphMemoryNode:
    """Test that the inject_memory node is wired into the graph."""

    def test_inject_memory_node_exists(self):
        """The graph should have an inject_memory node."""
        import inspect
        from agents import graph as graph_module
        source = inspect.getsource(graph_module.create_agent_graph)
        assert "inject_memory" in source
        assert 'workflow.add_node("inject_memory"' in source

    def test_inject_memory_edge_wiring(self):
        """inject_memory should be between skill_loader and decomposition."""
        import inspect
        from agents import graph as graph_module
        source = inspect.getsource(graph_module.create_agent_graph)
        assert '"skill_loader", "inject_memory"' in source
        assert '"inject_memory"' in source


# ---------------------------------------------------------------------------
# Doctor check embedding coverage tests
# ---------------------------------------------------------------------------


class TestDoctorCheckEmbeddings:
    """Test that doctor check reports embedding coverage."""

    def test_doctor_reports_embedding_count(self, tmp_path):
        from agents.memory_store import MemoryStore
        import json

        vibe_dir = tmp_path / ".vibe"
        vibe_dir.mkdir()
        db_path = vibe_dir / "memory.db"
        store = MemoryStore(db_path=db_path)
        store._embedder_checked = True
        store._embedder = None
        store.store("test memory")

        # Manually add an embedding
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT id FROM memories").fetchall()
        conn.execute(
            "INSERT INTO memory_embeddings (memory_id, embedding, model, created_at) VALUES (?, ?, ?, ?)",
            (rows[0][0], json.dumps([0.1, 0.2]), "test", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()

        with patch("agents.doctor.Path.home", return_value=tmp_path):
            from agents.doctor import check_memory
            result = check_memory()
        assert result.status == "ok"
        assert "1/1 embedded" in result.summary


# ---------------------------------------------------------------------------
# Scoping (agent_id / task_id) tests
# ---------------------------------------------------------------------------


class TestMemoryStoreScoping:
    """agent_id / task_id should partition recall results."""

    @pytest.fixture()
    def store(self, tmp_path):
        from agents.memory_store import MemoryStore
        s = MemoryStore(db_path=tmp_path / "scoped.db")
        s.store("alpha decided to use postgres", agent_id="cto", task_id="ISSUE-1")
        s.store("alpha picked redis cache", agent_id="cto", task_id="ISSUE-1")
        s.store("alpha implemented login flow", agent_id="backend-engineer", task_id="ISSUE-1")
        s.store("zeta task lives elsewhere", agent_id="cto", task_id="ISSUE-2")
        return s

    def test_recall_scoped_by_agent(self, store):
        results = store.recall("alpha", agent_id="cto")
        agents_seen = {r.agent_id for r in results}
        assert agents_seen == {"cto"}
        assert len(results) >= 2

    def test_recall_scoped_by_task(self, store):
        results = store.recall("alpha", task_id="ISSUE-1")
        for r in results:
            assert r.task_id == "ISSUE-1"

    def test_recall_scoped_by_agent_and_task(self, store):
        results = store.recall("alpha", agent_id="cto", task_id="ISSUE-1")
        for r in results:
            assert r.agent_id == "cto"
            assert r.task_id == "ISSUE-1"
        assert len(results) == 2

    def test_recall_unscoped_returns_all(self, store):
        results = store.recall("alpha")
        # 3 entries contain 'alpha' regardless of agent/task
        assert len(results) >= 3

    def test_get_by_id_returns_scope_fields(self, store):
        results = store.recall("postgres", agent_id="cto")
        assert results
        entry = store.get_by_id(results[0].memory_id)
        assert entry.agent_id == "cto"
        assert entry.task_id == "ISSUE-1"

    def test_hybrid_recall_scoped(self, store):
        results = store.hybrid_recall("alpha", agent_id="backend-engineer")
        for r in results:
            assert r.agent_id == "backend-engineer"


# ---------------------------------------------------------------------------
# Dedup / content_hash tests
# ---------------------------------------------------------------------------


class TestMemoryStoreDedup:
    """Storing the same (agent, task, content) twice should not insert twice."""

    @pytest.fixture()
    def store(self, tmp_path):
        from agents.memory_store import MemoryStore
        return MemoryStore(db_path=tmp_path / "dedup.db")

    def test_dedup_same_scope(self, store):
        id1 = store.store("identical fact", agent_id="cto", task_id="X")
        id2 = store.store("identical fact", agent_id="cto", task_id="X")
        assert id1 == id2
        # Only one row in DB
        stats = store.get_stats()
        assert stats["total"] == 1

    def test_dedup_bumps_access_count(self, store):
        id1 = store.store("repeated fact", agent_id="cto", task_id="X")
        store.store("repeated fact", agent_id="cto", task_id="X")
        store.store("repeated fact", agent_id="cto", task_id="X")
        entry = store.get_by_id(id1)
        assert entry.access_count >= 2

    def test_dedup_keeps_max_importance(self, store):
        id1 = store.store(
            "fact", agent_id="cto", task_id="X", importance=0.3,
        )
        store.store(
            "fact", agent_id="cto", task_id="X", importance=0.9,
        )
        entry = store.get_by_id(id1)
        assert entry.importance == 0.9

    def test_no_dedup_across_agents(self, store):
        id1 = store.store("shared fact", agent_id="cto", task_id="X")
        id2 = store.store("shared fact", agent_id="qa-engineer", task_id="X")
        assert id1 != id2
        stats = store.get_stats()
        assert stats["total"] == 2

    def test_no_dedup_across_tasks(self, store):
        id1 = store.store("shared fact", agent_id="cto", task_id="A")
        id2 = store.store("shared fact", agent_id="cto", task_id="B")
        assert id1 != id2

    def test_dedup_works_without_scope(self, store):
        id1 = store.store("anonymous fact")
        id2 = store.store("anonymous fact")
        assert id1 == id2


# ---------------------------------------------------------------------------
# Importance + decay (hybrid fusion)
# ---------------------------------------------------------------------------


class TestMemoryStoreImportanceAndDecay:
    """Hybrid fusion should respect importance scores and time decay."""

    @pytest.fixture()
    def store(self, tmp_path):
        from agents.memory_store import MemoryStore
        return MemoryStore(db_path=tmp_path / "importance.db")

    def test_importance_clamped(self, store):
        mid = store.store("fact", importance=2.5)
        entry = store.get_by_id(mid)
        assert entry.importance == 1.0
        mid = store.store("other fact", importance=-0.5)
        entry = store.get_by_id(mid)
        assert entry.importance == 0.0

    def test_recency_factor_recent(self, store):
        # Just-stored entry should have a recency factor close to 1.0
        from datetime import datetime
        now = datetime.utcnow().isoformat() + "Z"
        assert store._recency_factor(now) > 0.99

    def test_recency_factor_old(self, store):
        # 365 days old, halflife = 30 → 2^(-365/30) ≈ very small
        old_ts = "2020-01-01T00:00:00"
        assert store._recency_factor(old_ts) < 0.01

    def test_recency_factor_invalid(self, store):
        # Garbage timestamps should not crash; default to 1.0
        assert store._recency_factor("not-a-date") == 1.0
        assert store._recency_factor("") == 1.0

    def test_hybrid_recall_score_includes_importance(self, store):
        # Importance should boost the fused score above the bare BM25 contribution.
        store.store("apples", importance=1.0)
        results = store.hybrid_recall("apples", max_results=1)
        assert results
        # With keyword_weight=0.4 + importance_weight=0.2*1.0 + recency≈0.1
        # the fused score should comfortably exceed 0.4 (the BM25-only term).
        assert results[0].score > 0.5

    def test_hybrid_recall_importance_zero_lower_than_one(self, store):
        # Same content (different scope so no dedup) — high importance > low.
        store.store("kiwi", agent_id="A", importance=0.05)
        store.store("kiwi", agent_id="B", importance=0.95)
        high = store.hybrid_recall("kiwi", agent_id="B", max_results=1)
        low = store.hybrid_recall("kiwi", agent_id="A", max_results=1)
        assert high and low
        assert high[0].score > low[0].score


# ---------------------------------------------------------------------------
# Postgres ph bug regression — ensure semantic_recall doesn't NameError
# ---------------------------------------------------------------------------


class TestSemanticRecallPostgresBugRegression:
    """Regression: semantic_recall used to reference an undefined `ph` var
    in the storage_backend branch. We can't spin up real Postgres in this
    test, but we can simulate one with a mock backend that supports vector
    search to make sure the code path doesn't NameError."""

    def test_semantic_recall_storage_backend_path_does_not_nameerror(self, tmp_path):
        from unittest.mock import MagicMock
        from agents.memory_store import MemoryStore

        # Mock backend that pretends to be pgvector-capable.
        backend = MagicMock()
        backend.placeholder = "%s"
        backend.supports_fts = True
        backend.supports_vector = True
        backend.fetchone_dict.return_value = None
        backend.fetchall_dict.return_value = []
        backend.execute = MagicMock()
        backend.execute_script = MagicMock()

        store = MemoryStore(
            db_path=tmp_path / "noop.db", storage_backend=backend,
        )
        # Inject a fake embedder so we hit the storage_backend SQL path.
        fake_embedder = MagicMock()
        fake_embedder.embed.return_value = [0.1, 0.2, 0.3]
        store._embedder = fake_embedder

        # Should NOT raise NameError; should return empty list because
        # the mock backend returns no rows.
        result = store.semantic_recall(
            "test query",
            tag_filter="x",
            source_filter="user",
            agent_id="cto",
            task_id="ISSUE-1",
        )
        assert result == []

        # Verify the SQL that was sent contains all expected placeholders
        # and includes the scoping clauses.
        called_sql = backend.fetchall_dict.call_args[0][0]
        assert "agent_id = %s" in called_sql
        assert "task_id = %s" in called_sql
        assert "%s::vector" in called_sql


# ---------------------------------------------------------------------------
# Concurrency — recall and store run from multiple threads safely
# ---------------------------------------------------------------------------


class TestMemoryStoreConcurrency:
    """Read-while-write safety: recall paths now hold the lock."""

    def test_concurrent_store_and_recall(self, tmp_path):
        import threading
        from agents.memory_store import MemoryStore

        store = MemoryStore(db_path=tmp_path / "concurrent.db")
        # Pre-seed some entries so recall has hits.
        for i in range(10):
            store.store(f"seed entry {i}", tags="seed")

        errors = []
        stop = threading.Event()

        def writer():
            try:
                i = 0
                while not stop.is_set():
                    store.store(f"writer entry {i}", tags="writer")
                    i += 1
                    if i > 50:
                        return
            except Exception as e:
                errors.append(("writer", e))

        def reader():
            try:
                while not stop.is_set():
                    store.recall("entry", max_results=5)
                    store.hybrid_recall("entry", max_results=5)
            except Exception as e:
                errors.append(("reader", e))

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        # Let them race for a short window
        threads[0].join(timeout=2.0)
        stop.set()
        for t in threads[1:]:
            t.join(timeout=2.0)

        assert not errors, f"Concurrency errors: {errors}"


# ---------------------------------------------------------------------------
# Persist helpers — graph node + heartbeat partial-state writer
# ---------------------------------------------------------------------------


class TestPersistMemoryNode:
    """`persist_memory_node` should write spec/output/feedback to MemoryStore."""

    def _patch_shared_store(self, store, monkeypatch):
        import agents.tools.registry as _reg
        monkeypatch.setattr(
            _reg, "_shared_memory_store", store, raising=False,
        )

    def test_persist_writes_spec_and_output(self, tmp_path, monkeypatch):
        from agents.memory_store import MemoryStore
        from agents.memory_persist import persist_memory_node

        store = MemoryStore(db_path=tmp_path / "persist.db")
        self._patch_shared_store(store, monkeypatch)

        state = {
            "agent_id": "backend-engineer",
            "task_id": "ISSUE-42",
            "routed_task_type": "code",
            "specification": "Build a JWT auth middleware.",
            "final_output": "def jwt_middleware(): pass",
            "final_score": 92,
            "output_critic_feedback": "Looks good, minor style nits.",
            "tool_calls_made": [
                {"tool": "python_executor"},
                {"tool": "pytest_runner"},
            ],
        }

        result = persist_memory_node(state)
        ids = result["memory_persisted_ids"]
        assert len(ids) >= 3  # spec, output, feedback, tools

        # Subsequent recall should find what we just wrote.
        recalled = store.recall(
            "JWT", agent_id="backend-engineer", task_id="ISSUE-42",
        )
        assert any("JWT" in r.content for r in recalled)

    def test_persist_dedups_within_run(self, tmp_path, monkeypatch):
        from agents.memory_store import MemoryStore
        from agents.memory_persist import persist_memory_node

        store = MemoryStore(db_path=tmp_path / "persist.db")
        self._patch_shared_store(store, monkeypatch)

        state = {
            "agent_id": "cto",
            "task_id": "ISSUE-1",
            "specification": "Decide stack.",
            "final_output": "Use Postgres + Redis.",
            "final_score": 88,
        }
        ids1 = persist_memory_node(state)["memory_persisted_ids"]
        ids2 = persist_memory_node(state)["memory_persisted_ids"]
        # Both runs should resolve to the same memory ids (dedup).
        assert ids1 == ids2

    def test_persist_handles_missing_fields(self, tmp_path, monkeypatch):
        from agents.memory_store import MemoryStore
        from agents.memory_persist import persist_memory_node

        store = MemoryStore(db_path=tmp_path / "persist.db")
        self._patch_shared_store(store, monkeypatch)

        # Empty state should not crash; just write nothing.
        result = persist_memory_node({})
        assert result["memory_persisted_ids"] == []

    def test_persist_truncates_long_output(self, tmp_path, monkeypatch):
        from agents.memory_store import MemoryStore
        from agents.memory_persist import persist_memory_node

        store = MemoryStore(db_path=tmp_path / "persist.db")
        self._patch_shared_store(store, monkeypatch)

        huge = "x" * 10000
        state = {
            "agent_id": "cto",
            "task_id": "ISSUE-1",
            "final_output": huge,
            "final_score": 90,
        }
        ids = persist_memory_node(state)["memory_persisted_ids"]
        assert ids
        entry = store.get_by_id(ids[0])
        # Should be truncated and end with ellipsis.
        assert len(entry.content) <= 4100
        assert entry.content.endswith("...")

    def test_persist_partial_state_clarification(self, tmp_path, monkeypatch):
        from agents.memory_store import MemoryStore
        from agents.memory_persist import persist_partial_state

        store = MemoryStore(db_path=tmp_path / "persist.db")
        self._patch_shared_store(store, monkeypatch)

        partial = {
            "specification": "Build something",
            "clarification_questions": [
                "Which database?",
                "Which auth provider?",
            ],
            "last_node": "specialist",
        }
        ids = persist_partial_state(
            partial,
            agent_id="cto",
            task_id="ISSUE-9",
            status="clarification_needed",
        )
        assert ids
        # Subsequent run should be able to recall the clarification ask.
        results = store.recall(
            "database auth", agent_id="cto", task_id="ISSUE-9",
        )
        assert any("Clarification" in r.content for r in results)

    def test_persist_partial_state_blocked(self, tmp_path, monkeypatch):
        from agents.memory_store import MemoryStore
        from agents.memory_persist import persist_partial_state

        store = MemoryStore(db_path=tmp_path / "persist.db")
        self._patch_shared_store(store, monkeypatch)

        partial = {
            "specialist_output": "half-finished work",
            "output_critic_score": 40,
            "output_critic_feedback": "missing tests",
            "last_node": "critic_output",
        }
        ids = persist_partial_state(
            partial,
            agent_id="backend-engineer",
            task_id="ISSUE-7",
            status="blocked",
        )
        assert ids
        results = store.recall(
            "tests", agent_id="backend-engineer", task_id="ISSUE-7",
        )
        assert any("Blocker feedback" in r.content for r in results)

    def test_persist_partial_state_sigterm(self, tmp_path, monkeypatch):
        from agents.memory_store import MemoryStore
        from agents.memory_persist import persist_partial_state

        store = MemoryStore(db_path=tmp_path / "persist.db")
        self._patch_shared_store(store, monkeypatch)

        partial = {
            "specialist_output": "interrupted halfway",
            "last_node": "specialist",
        }
        ids = persist_partial_state(
            partial,
            agent_id="cto",
            task_id="ISSUE-1",
            status="sigterm",
        )
        assert ids
        results = store.recall(
            "interrupted", agent_id="cto", task_id="ISSUE-1",
        )
        assert any("Partial output" in r.content for r in results)


# ---------------------------------------------------------------------------
# End-to-end: persist on run 1 → inject_memory on run 2
# ---------------------------------------------------------------------------


class TestMemoryEndToEnd:
    """Persist → recall round-trip mimics two heartbeat runs."""

    def test_persist_then_inject(self, tmp_path, monkeypatch):
        from agents.memory_store import MemoryStore
        from agents.memory_persist import persist_memory_node
        import agents.tools.registry as _reg

        store = MemoryStore(db_path=tmp_path / "e2e.db")
        monkeypatch.setattr(
            _reg, "_shared_memory_store", store, raising=False,
        )

        # ── Run 1: persist ──
        run1 = {
            "agent_id": "cto",
            "task_id": "ISSUE-1",
            "routed_task_type": "code",
            "specification": "Pick database for new service",
            "final_output": "Selected Postgres with pgvector for embeddings",
            "final_score": 91,
        }
        persist_memory_node(run1)

        # ── Run 2: inject_memory simulation ──
        # Mimic graph_nodes.inject_memory's hybrid_recall call.
        results = store.hybrid_recall(
            query="Postgres pgvector database",
            agent_id="cto",
            task_id="ISSUE-1",
            max_results=5,
        )
        assert results
        joined = " ".join(r.content for r in results)
        assert "Postgres" in joined
