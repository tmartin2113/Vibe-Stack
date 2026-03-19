"""Tests for the MessageStore — SQLite + FTS5 + embeddings message storage.

Covers:
- MessageStore: CRUD, FTS5 search, FIFO eviction, stats, expiry
- Threading / reply chains
- Read tracking (mark_read, get_unread)
- Semantic search (mocked embedder)
- Hybrid search (merged BM25 + vector)
- relevant_messages smart retrieval
- v1 BULLETIN.md migration
- PaperclipBridge dual-write
- Singleton pattern
"""

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── MessageStore Init ────────────────────────────────────────────


class TestMessageStoreInit:
    """Database initialization and schema creation."""

    def test_creates_db_file(self, tmp_path):
        from agents.message_store import MessageStore

        db = tmp_path / "test.db"
        store = MessageStore(db_path=db)
        assert db.exists()

    def test_creates_tables(self, tmp_path):
        from agents.message_store import MessageStore

        db = tmp_path / "test.db"
        MessageStore(db_path=db)
        conn = sqlite3.connect(str(db))
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "messages" in tables
        assert "message_embeddings" in tables

    def test_creates_fts_table(self, tmp_path):
        from agents.message_store import MessageStore

        db = tmp_path / "test.db"
        MessageStore(db_path=db)
        conn = sqlite3.connect(str(db))
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "messages_fts" in tables

    def test_wal_mode_enabled(self, tmp_path):
        from agents.message_store import MessageStore

        db = tmp_path / "test.db"
        MessageStore(db_path=db)
        conn = sqlite3.connect(str(db))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"

    def test_idempotent_init(self, tmp_path):
        from agents.message_store import MessageStore

        db = tmp_path / "test.db"
        MessageStore(db_path=db)
        MessageStore(db_path=db)  # Should not raise


# ── Send / Read ──────────────────────────────────────────────────


class TestMessageSendRead:
    """Basic send and read operations."""

    def _store(self, tmp_path):
        from agents.message_store import MessageStore

        return MessageStore(db_path=tmp_path / "test.db")

    def test_send_returns_message(self, tmp_path):
        from agents.message_types import MessageType

        store = self._store(tmp_path)
        msg = store.send(content="hello", sender="agent-a")
        assert msg.id
        assert msg.sender == "agent-a"
        assert msg.content == "hello"
        assert msg.msg_type == MessageType.INFO

    def test_send_with_all_params(self, tmp_path):
        from agents.message_types import MessageType

        store = self._store(tmp_path)
        msg = store.send(
            content="decision made",
            sender="agent-a",
            recipient="agent-b",
            msg_type=MessageType.DECISION,
            topic="architecture",
            metadata={"key": "val"},
            issue_id="issue-1",
            ttl_seconds=3600,
        )
        assert msg.recipient == "agent-b"
        assert msg.msg_type == MessageType.DECISION
        assert msg.topic == "architecture"
        assert msg.metadata == {"key": "val"}
        assert msg.issue_id == "issue-1"

    def test_read_recent_returns_sent_messages(self, tmp_path):
        store = self._store(tmp_path)
        store.send(content="msg1", sender="a")
        store.send(content="msg2", sender="b")
        msgs = store.read_recent(limit=10)
        assert len(msgs) == 2
        assert msgs[0].content == "msg1"
        assert msgs[1].content == "msg2"

    def test_read_recent_limit(self, tmp_path):
        store = self._store(tmp_path)
        for i in range(10):
            store.send(content=f"msg{i}", sender="a")
        msgs = store.read_recent(limit=3)
        assert len(msgs) == 3

    def test_read_recent_chronological_order(self, tmp_path):
        store = self._store(tmp_path)
        store.send(content="first", sender="a")
        store.send(content="second", sender="a")
        msgs = store.read_recent(limit=10)
        assert msgs[0].content == "first"
        assert msgs[1].content == "second"

    def test_read_recent_filter_by_recipient(self, tmp_path):
        store = self._store(tmp_path)
        store.send(content="for-b", sender="a", recipient="agent-b")
        store.send(content="broadcast", sender="a", recipient="*")
        store.send(content="for-c", sender="a", recipient="agent-c")

        msgs = store.read_recent(limit=10, recipient="agent-b")
        contents = [m.content for m in msgs]
        assert "for-b" in contents
        assert "broadcast" in contents
        assert "for-c" not in contents

    def test_read_recent_filter_by_type(self, tmp_path):
        from agents.message_types import MessageType

        store = self._store(tmp_path)
        store.send(content="info-msg", sender="a", msg_type=MessageType.INFO)
        store.send(content="blocker-msg", sender="a", msg_type=MessageType.BLOCKER)

        msgs = store.read_recent(type_filter=[MessageType.BLOCKER])
        assert len(msgs) == 1
        assert msgs[0].content == "blocker-msg"

    def test_read_recent_filter_by_topic(self, tmp_path):
        store = self._store(tmp_path)
        store.send(content="auth-msg", sender="a", topic="auth")
        store.send(content="db-msg", sender="a", topic="database")

        msgs = store.read_recent(topic="auth")
        assert len(msgs) == 1
        assert msgs[0].content == "auth-msg"

    def test_read_recent_excludes_expired(self, tmp_path):
        from agents.message_types import Message
        from datetime import datetime, timezone

        store = self._store(tmp_path)
        # Insert an already-expired message directly
        past = (datetime.now(timezone.utc)).isoformat()
        store.send(content="will-expire", sender="a", ttl_seconds=1)
        store.send(content="still-valid", sender="a", ttl_seconds=99999)
        # Manually set expires_at to past for the first message
        with store._connect() as conn:
            conn.execute(
                "UPDATE messages SET expires_at = ? WHERE content = ?",
                ("2000-01-01T00:00:00+00:00", "will-expire"),
            )

        msgs = store.read_recent()
        contents = [m.content for m in msgs]
        assert "still-valid" in contents
        assert "will-expire" not in contents

    def test_read_recent_include_expired(self, tmp_path):
        store = self._store(tmp_path)
        store.send(content="expired", sender="a", ttl_seconds=1)
        with store._connect() as conn:
            conn.execute(
                "UPDATE messages SET expires_at = ? WHERE content = ?",
                ("2000-01-01T00:00:00+00:00", "expired"),
            )

        msgs = store.read_recent(include_expired=True)
        assert any(m.content == "expired" for m in msgs)

    def test_get_by_id(self, tmp_path):
        store = self._store(tmp_path)
        sent = store.send(content="find-me", sender="a")
        found = store.get_by_id(sent.id)
        assert found is not None
        assert found.content == "find-me"

    def test_get_by_id_not_found(self, tmp_path):
        store = self._store(tmp_path)
        assert store.get_by_id("nonexistent") is None

    def test_limit_clamped(self, tmp_path):
        store = self._store(tmp_path)
        store.send(content="x", sender="a")
        msgs = store.read_recent(limit=0)
        assert len(msgs) == 1  # clamped to 1


# ── Threading / Replies ──────────────────────────────────────────


class TestMessageThreading:
    """Thread operations: reply and get_thread."""

    def _store(self, tmp_path):
        from agents.message_store import MessageStore

        return MessageStore(db_path=tmp_path / "test.db")

    def test_reply_creates_child(self, tmp_path):
        store = self._store(tmp_path)
        root = store.send(content="question?", sender="a", topic="design")
        reply = store.reply(root.id, content="answer!", sender="b")
        assert reply.parent_id == root.id
        assert reply.topic == "design"
        assert reply.recipient == "a"

    def test_reply_to_nonexistent_raises(self, tmp_path):
        store = self._store(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            store.reply("nonexistent", content="orphan", sender="a")

    def test_get_thread(self, tmp_path):
        store = self._store(tmp_path)
        root = store.send(content="root", sender="a")
        store.reply(root.id, content="reply1", sender="b")
        store.reply(root.id, content="reply2", sender="c")

        thread = store.get_thread(root.id)
        assert len(thread) == 3
        assert thread[0].content == "root"

    def test_get_thread_from_child(self, tmp_path):
        store = self._store(tmp_path)
        root = store.send(content="root", sender="a")
        child = store.reply(root.id, content="child", sender="b")

        thread = store.get_thread(child.id)
        assert len(thread) == 2

    def test_get_thread_nonexistent(self, tmp_path):
        store = self._store(tmp_path)
        assert store.get_thread("nonexistent") == []

    def test_reply_inherits_issue_id(self, tmp_path):
        store = self._store(tmp_path)
        root = store.send(content="q", sender="a", issue_id="issue-42")
        reply = store.reply(root.id, content="a", sender="b")
        assert reply.issue_id == "issue-42"


# ── Read Tracking ────────────────────────────────────────────────


class TestReadTracking:
    """mark_read, mark_many_read, get_unread."""

    def _store(self, tmp_path):
        from agents.message_store import MessageStore

        return MessageStore(db_path=tmp_path / "test.db")

    def test_mark_read(self, tmp_path):
        store = self._store(tmp_path)
        msg = store.send(content="test", sender="a")
        store.mark_read(msg.id, "agent-b")
        found = store.get_by_id(msg.id)
        assert "agent-b" in found.read_by

    def test_mark_read_idempotent(self, tmp_path):
        store = self._store(tmp_path)
        msg = store.send(content="test", sender="a")
        store.mark_read(msg.id, "agent-b")
        store.mark_read(msg.id, "agent-b")
        found = store.get_by_id(msg.id)
        assert found.read_by.count("agent-b") == 1

    def test_mark_read_nonexistent(self, tmp_path):
        store = self._store(tmp_path)
        store.mark_read("nonexistent", "agent-b")  # Should not raise

    def test_mark_many_read(self, tmp_path):
        store = self._store(tmp_path)
        m1 = store.send(content="a", sender="x")
        m2 = store.send(content="b", sender="x")
        store.mark_many_read([m1.id, m2.id], "agent-b")
        assert "agent-b" in store.get_by_id(m1.id).read_by
        assert "agent-b" in store.get_by_id(m2.id).read_by

    def test_get_unread(self, tmp_path):
        store = self._store(tmp_path)
        m1 = store.send(content="unread", sender="a", recipient="agent-b")
        m2 = store.send(content="read", sender="a", recipient="agent-b")
        store.mark_read(m2.id, "agent-b")

        unread = store.get_unread(agent_name="agent-b")
        contents = [m.content for m in unread]
        assert "unread" in contents
        assert "read" not in contents

    def test_get_unread_includes_broadcast(self, tmp_path):
        store = self._store(tmp_path)
        store.send(content="broadcast-msg", sender="a", recipient="*")
        unread = store.get_unread(agent_name="agent-b")
        assert len(unread) == 1

    def test_get_unread_excludes_expired(self, tmp_path):
        store = self._store(tmp_path)
        store.send(content="expired", sender="a", recipient="agent-b", ttl_seconds=1)
        with store._connect() as conn:
            conn.execute(
                "UPDATE messages SET expires_at = ? WHERE content = ?",
                ("2000-01-01T00:00:00+00:00", "expired"),
            )
        unread = store.get_unread(agent_name="agent-b")
        assert len(unread) == 0


# ── FTS5 Search ──────────────────────────────────────────────────


class TestFTSSearch:
    """Full-text search via FTS5 BM25."""

    def _store(self, tmp_path):
        from agents.message_store import MessageStore

        return MessageStore(db_path=tmp_path / "test.db")

    def test_search_finds_content(self, tmp_path):
        store = self._store(tmp_path)
        store.send(content="PostgreSQL migration plan", sender="a")
        store.send(content="Redis cache strategy", sender="a")

        results = store.search("PostgreSQL")
        assert len(results) == 1
        assert "PostgreSQL" in results[0].content

    def test_search_finds_topic(self, tmp_path):
        store = self._store(tmp_path)
        store.send(content="some message", sender="a", topic="database")
        results = store.search("database")
        assert len(results) >= 1

    def test_search_finds_sender(self, tmp_path):
        store = self._store(tmp_path)
        store.send(content="hello", sender="architect-agent")
        results = store.search("architect")
        assert len(results) >= 1

    def test_search_empty_query(self, tmp_path):
        store = self._store(tmp_path)
        store.send(content="hello", sender="a")
        assert store.search("") == []
        assert store.search("   ") == []

    def test_search_no_matches(self, tmp_path):
        store = self._store(tmp_path)
        store.send(content="hello world", sender="a")
        results = store.search("xyznonexistent")
        assert len(results) == 0

    def test_search_scores_positive(self, tmp_path):
        store = self._store(tmp_path)
        store.send(content="important decision about architecture", sender="a")
        results = store.search("architecture")
        assert len(results) == 1
        assert results[0].score > 0

    def test_search_excludes_expired(self, tmp_path):
        store = self._store(tmp_path)
        store.send(content="expired architecture note", sender="a", ttl_seconds=1)
        with store._connect() as conn:
            conn.execute(
                "UPDATE messages SET expires_at = ? WHERE content LIKE '%expired%'",
                ("2000-01-01T00:00:00+00:00",),
            )
        results = store.search("architecture")
        assert len(results) == 0

    def test_search_special_characters(self, tmp_path):
        store = self._store(tmp_path)
        store.send(content="error in foo() function", sender="a")
        results = store.search("foo()")
        assert len(results) >= 1

    def test_search_max_results(self, tmp_path):
        store = self._store(tmp_path)
        for i in range(10):
            store.send(content=f"architecture note {i}", sender="a")
        results = store.search("architecture", max_results=3)
        assert len(results) == 3


# ── Semantic Search ──────────────────────────────────────────────


class TestSemanticSearch:
    """Semantic search with mocked embedder."""

    def _store_with_embedder(self, tmp_path):
        from agents.message_store import MessageStore

        store = MessageStore(db_path=tmp_path / "test.db")
        # Mock the embedder
        mock_embedder = MagicMock()
        mock_embedder.is_available.return_value = True
        store._embedder = mock_embedder
        return store, mock_embedder

    def test_semantic_search_returns_results(self, tmp_path):
        store, embedder = self._store_with_embedder(tmp_path)

        # Pre-store embeddings manually
        embedder.embed.return_value = [1.0, 0.0, 0.0]
        store.send(content="auth module design", sender="a")

        # Query
        embedder.embed.return_value = [0.9, 0.1, 0.0]
        results = store.semantic_search("authentication")
        assert len(results) >= 1

    def test_semantic_search_no_embedder(self, tmp_path):
        from agents.message_store import MessageStore

        store = MessageStore(db_path=tmp_path / "test.db")
        mock_embedder = MagicMock()
        mock_embedder.is_available.return_value = False
        mock_embedder.embed.return_value = None
        store._embedder = mock_embedder

        store.send(content="test", sender="a")
        results = store.semantic_search("test")
        assert results == []


# ── Hybrid Search ────────────────────────────────────────────────


class TestHybridSearch:
    """Merged BM25 + semantic search."""

    def test_hybrid_combines_results(self, tmp_path):
        from agents.message_store import MessageStore

        store = MessageStore(db_path=tmp_path / "test.db")
        mock_embedder = MagicMock()
        mock_embedder.is_available.return_value = True
        mock_embedder.embed.return_value = [1.0, 0.0]
        store._embedder = mock_embedder

        store.send(content="database optimization strategy", sender="a")
        store.send(content="API design patterns", sender="a")

        results = store.hybrid_search("database")
        assert len(results) >= 1

    def test_hybrid_empty_results(self, tmp_path):
        from agents.message_store import MessageStore

        store = MessageStore(db_path=tmp_path / "test.db")
        mock_embedder = MagicMock()
        mock_embedder.is_available.return_value = False
        mock_embedder.embed.return_value = None
        store._embedder = mock_embedder

        results = store.hybrid_search("nonexistent")
        assert results == []


# ── FIFO Eviction ────────────────────────────────────────────────


class TestFIFOEviction:
    """FIFO eviction at MAX_MESSAGES cap."""

    def test_evicts_oldest(self, tmp_path):
        from agents.message_store import MessageStore

        store = MessageStore(db_path=tmp_path / "test.db")
        store.MAX_MESSAGES = 5

        for i in range(7):
            store.send(content=f"msg-{i}", sender="a")

        msgs = store.read_recent(limit=100)
        assert len(msgs) == 5
        # Oldest (msg-0, msg-1) should have been evicted
        contents = [m.content for m in msgs]
        assert "msg-0" not in contents
        assert "msg-1" not in contents
        assert "msg-6" in contents


# ── Expiry Cleanup ───────────────────────────────────────────────


class TestExpiryCleanup:
    """cleanup_expired deletes past-expiry messages."""

    def test_cleanup_removes_expired(self, tmp_path):
        from agents.message_store import MessageStore

        store = MessageStore(db_path=tmp_path / "test.db")
        store.send(content="expired", sender="a", ttl_seconds=1)
        store.send(content="valid", sender="a", ttl_seconds=99999)

        # Backdate the first message's expiry
        with store._connect() as conn:
            conn.execute(
                "UPDATE messages SET expires_at = ? WHERE content = ?",
                ("2000-01-01T00:00:00+00:00", "expired"),
            )

        deleted = store.cleanup_expired()
        assert deleted == 1
        msgs = store.read_recent(include_expired=True)
        assert len(msgs) == 1
        assert msgs[0].content == "valid"

    def test_cleanup_nothing_to_delete(self, tmp_path):
        from agents.message_store import MessageStore

        store = MessageStore(db_path=tmp_path / "test.db")
        store.send(content="valid", sender="a", ttl_seconds=99999)
        assert store.cleanup_expired() == 0


# ── Stats ────────────────────────────────────────────────────────


class TestMessageStoreStats:
    """get_stats returns accurate statistics."""

    def test_stats_empty(self, tmp_path):
        from agents.message_store import MessageStore

        store = MessageStore(db_path=tmp_path / "test.db")
        stats = store.get_stats()
        assert stats["total_messages"] == 0
        assert stats["by_type"] == {}

    def test_stats_with_messages(self, tmp_path):
        from agents.message_store import MessageStore
        from agents.message_types import MessageType

        store = MessageStore(db_path=tmp_path / "test.db")
        store.send(content="a", sender="x", msg_type=MessageType.INFO)
        store.send(content="b", sender="x", msg_type=MessageType.INFO)
        store.send(content="c", sender="x", msg_type=MessageType.BLOCKER)

        stats = store.get_stats()
        assert stats["total_messages"] == 3
        assert stats["by_type"]["info"] == 2
        assert stats["by_type"]["blocker"] == 1

    def test_stats_thread_count(self, tmp_path):
        from agents.message_store import MessageStore

        store = MessageStore(db_path=tmp_path / "test.db")
        root = store.send(content="root", sender="a")
        store.reply(root.id, content="reply", sender="b")
        store.send(content="standalone", sender="a")

        stats = store.get_stats()
        assert stats["thread_count"] == 2  # root thread + standalone


# ── relevant_messages ────────────────────────────────────────────


class TestRelevantMessages:
    """Smart retrieval for context injection."""

    def _store(self, tmp_path):
        from agents.message_store import MessageStore

        store = MessageStore(db_path=tmp_path / "test.db")
        # Disable embedder for these tests
        mock_embedder = MagicMock()
        mock_embedder.is_available.return_value = False
        mock_embedder.embed.return_value = None
        store._embedder = mock_embedder
        return store

    def test_surfaces_unread(self, tmp_path):
        store = self._store(tmp_path)
        store.send(content="for-you", sender="a", recipient="agent-b")
        results = store.relevant_messages(query="", agent_name="agent-b")
        assert len(results) >= 1
        assert any(m.content == "for-you" for m in results)

    def test_surfaces_high_priority(self, tmp_path):
        from agents.message_types import MessageType

        store = self._store(tmp_path)
        store.send(content="blocker-msg", sender="a", msg_type=MessageType.BLOCKER)
        store.send(content="info-msg", sender="a", msg_type=MessageType.INFO)
        results = store.relevant_messages(query="", agent_name="agent-x")
        # Blocker should appear (high priority type)
        assert any(m.content == "blocker-msg" for m in results)

    def test_deduplicates(self, tmp_path):
        from agents.message_types import MessageType

        store = self._store(tmp_path)
        store.send(
            content="unread blocker",
            sender="a",
            recipient="agent-b",
            msg_type=MessageType.BLOCKER,
        )
        results = store.relevant_messages(query="blocker", agent_name="agent-b")
        # Should appear only once despite matching unread + high-priority + search
        ids = [m.id for m in results]
        assert len(ids) == len(set(ids))

    def test_respects_max_results(self, tmp_path):
        store = self._store(tmp_path)
        for i in range(20):
            store.send(content=f"msg-{i}", sender="a", recipient="agent-b")
        results = store.relevant_messages(
            query="msg", agent_name="agent-b", max_results=5
        )
        assert len(results) <= 5

    def test_empty_store(self, tmp_path):
        store = self._store(tmp_path)
        results = store.relevant_messages(query="anything", agent_name="agent-b")
        assert results == []


# ── v1 Migration ─────────────────────────────────────────────────


class TestV1Migration:
    """Auto-import from BULLETIN.md."""

    def _bulletin_content(self):
        return (
            "# Inter-Agent Bulletin Board\n\n"
            "Shared notes between agents.\n\n---\n\n"
            "### [2026-03-19 10:00:00] agent-alpha\n"
            "> Topic: architecture\n\n"
            "Use PostgreSQL for the data layer\n\n---\n\n"
            "### [2026-03-19 11:00:00] agent-beta\n\n"
            "Completed auth module tests\n\n---\n"
        )

    def test_migrates_entries(self, tmp_path):
        from agents.message_store import MessageStore, migrate_v1_bulletin

        bulletin = tmp_path / "BULLETIN.md"
        bulletin.write_text(self._bulletin_content())

        store = MessageStore(db_path=tmp_path / "test.db")
        count = migrate_v1_bulletin(str(bulletin), store)
        assert count == 2

        msgs = store.read_recent(limit=10)
        assert len(msgs) == 2
        assert msgs[0].sender == "agent-alpha"
        assert msgs[0].topic == "architecture"
        assert "PostgreSQL" in msgs[0].content
        assert msgs[1].sender == "agent-beta"

    def test_migration_idempotent(self, tmp_path):
        from agents.message_store import MessageStore, migrate_v1_bulletin

        bulletin = tmp_path / "BULLETIN.md"
        bulletin.write_text(self._bulletin_content())

        store = MessageStore(db_path=tmp_path / "test.db")
        migrate_v1_bulletin(str(bulletin), store)
        count2 = migrate_v1_bulletin(str(bulletin), store)
        assert count2 == 0  # Skipped because store already has data

    def test_migration_no_file(self, tmp_path):
        from agents.message_store import MessageStore, migrate_v1_bulletin

        store = MessageStore(db_path=tmp_path / "test.db")
        count = migrate_v1_bulletin("/nonexistent/BULLETIN.md", store)
        assert count == 0

    def test_migration_empty_file(self, tmp_path):
        from agents.message_store import MessageStore, migrate_v1_bulletin

        bulletin = tmp_path / "BULLETIN.md"
        bulletin.write_text("# Empty Bulletin\n")

        store = MessageStore(db_path=tmp_path / "test.db")
        count = migrate_v1_bulletin(str(bulletin), store)
        assert count == 0

    def test_migrated_entries_never_expire(self, tmp_path):
        from agents.message_store import MessageStore, migrate_v1_bulletin

        bulletin = tmp_path / "BULLETIN.md"
        bulletin.write_text(self._bulletin_content())

        store = MessageStore(db_path=tmp_path / "test.db")
        migrate_v1_bulletin(str(bulletin), store)

        msgs = store.read_recent(limit=10)
        for msg in msgs:
            assert msg.ttl_seconds == 0
            assert msg.expires_at is None


# ── PaperclipBridge ──────────────────────────────────────────────


class TestPaperclipBridge:
    """Dual-write to Paperclip issues."""

    def test_disabled_without_env(self):
        from agents.message_store import PaperclipBridge

        with patch.dict(os.environ, {}, clear=True):
            bridge = PaperclipBridge()
            bridge._enabled = None  # Reset cache
            assert not bridge.is_enabled()

    def test_enabled_with_env(self):
        from agents.message_store import PaperclipBridge

        env = {
            "PAPERCLIP_API_URL": "http://test",
            "PAPERCLIP_AGENT_ID": "agent-1",
        }
        with patch.dict(os.environ, env, clear=True):
            bridge = PaperclipBridge()
            bridge._enabled = None
            assert bridge.is_enabled()

    def test_post_comment_returns_none_when_disabled(self):
        from agents.message_store import PaperclipBridge
        from agents.message_types import Message

        bridge = PaperclipBridge()
        bridge._enabled = False
        msg = Message(sender="a", content="test", issue_id="issue-1")
        assert bridge.post_comment(msg) is None

    def test_post_comment_returns_none_without_issue_id(self):
        from agents.message_store import PaperclipBridge
        from agents.message_types import Message

        bridge = PaperclipBridge()
        bridge._enabled = True
        msg = Message(sender="a", content="test", issue_id=None)
        assert bridge.post_comment(msg) is None

    def test_post_comment_success(self):
        from agents.message_store import PaperclipBridge
        from agents.message_types import Message, MessageType

        bridge = PaperclipBridge()
        bridge._enabled = True

        mock_client = MagicMock()
        mock_comment = MagicMock()
        mock_comment.id = "comment-42"
        mock_client.add_comment.return_value = mock_comment
        bridge._client = mock_client

        msg = Message(
            sender="agent-a",
            content="Architecture decision",
            msg_type=MessageType.DECISION,
            topic="design",
            issue_id="issue-1",
        )
        result = bridge.post_comment(msg)
        assert result == "comment-42"
        mock_client.add_comment.assert_called_once()

    def test_post_comment_failure_logged(self):
        from agents.message_store import PaperclipBridge
        from agents.message_types import Message

        bridge = PaperclipBridge()
        bridge._enabled = True

        mock_client = MagicMock()
        mock_client.add_comment.side_effect = RuntimeError("API down")
        bridge._client = mock_client

        msg = Message(sender="a", content="test", issue_id="issue-1")
        result = bridge.post_comment(msg)
        assert result is None

    def test_format_comment(self):
        from agents.message_store import PaperclipBridge
        from agents.message_types import Message, MessageType

        bridge = PaperclipBridge()
        msg = Message(
            sender="agent-a",
            content="Use REST over GraphQL",
            msg_type=MessageType.DECISION,
            topic="api-design",
            metadata={"key": "val"},
        )
        text = bridge._format_comment(msg)
        assert "DECISION" in text
        assert "agent-a" in text
        assert "api-design" in text
        assert "Use REST over GraphQL" in text
        assert '"key"' in text


# ── Singleton ────────────────────────────────────────────────────


class TestSingleton:
    """get_shared_message_store singleton."""

    def test_returns_same_instance(self, tmp_path):
        import agents.message_store as mod

        # Reset singleton
        mod._shared_message_store = None
        with patch.dict(os.environ, {"MESSAGE_STORE_PATH": str(tmp_path / "s.db")}):
            store1 = mod.get_shared_message_store()
            store2 = mod.get_shared_message_store()
            assert store1 is store2
        mod._shared_message_store = None  # Cleanup

    def test_auto_migrates_v1(self, tmp_path):
        import agents.message_store as mod

        mod._shared_message_store = None

        bulletin = tmp_path / "BULLETIN.md"
        bulletin.write_text(
            "# Bulletin\n\n### [2026-01-01 00:00:00] test-agent\n\nHello\n\n---\n"
        )

        env = {
            "MESSAGE_STORE_PATH": str(tmp_path / "singleton.db"),
            "BULLETIN_PATH": str(bulletin),
        }
        with patch.dict(os.environ, env, clear=False):
            store = mod.get_shared_message_store()
            msgs = store.read_recent()
            assert len(msgs) == 1
            assert msgs[0].sender == "test-agent"
        mod._shared_message_store = None


# ── update_paperclip_comment_id ──────────────────────────────────


class TestUpdatePaperclipCommentId:
    def test_updates_comment_id(self, tmp_path):
        from agents.message_store import MessageStore

        store = MessageStore(db_path=tmp_path / "test.db")
        msg = store.send(content="test", sender="a", issue_id="issue-1")
        store.update_paperclip_comment_id(msg.id, "comment-99")
        found = store.get_by_id(msg.id)
        assert found.paperclip_comment_id == "comment-99"


# ── VLLMEmbedder ─────────────────────────────────────────────────


class TestVLLMEmbedder:
    """VLLMEmbedder graceful degradation."""

    def test_unavailable_returns_none(self):
        from agents.message_store import VLLMEmbedder

        embedder = VLLMEmbedder(vllm_url="http://nonexistent:9999", timeout=1)
        embedder._available = False
        assert embedder.embed("test") is None

    def test_available_cached(self):
        from agents.message_store import VLLMEmbedder

        embedder = VLLMEmbedder()
        embedder._available = True
        assert embedder.is_available() is True

    def test_embed_failure_returns_none(self):
        from agents.message_store import VLLMEmbedder

        embedder = VLLMEmbedder()
        embedder._available = True
        with patch("requests.post", side_effect=ConnectionError("fail")):
            result = embedder.embed("test")
            assert result is None
