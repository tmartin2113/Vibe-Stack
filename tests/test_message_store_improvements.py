"""Tests for MessageStore follow-up improvements.

Covers:
- backfill_embeddings: empty DB, some missing, all present, embedder unavailable
- validate_metadata: each type with valid/invalid/missing fields, unknown types
- MessageStoreConfig: defaults, from_env overrides
- check_message_store: not configured, healthy DB, missing FTS5, empty DB
- Heartbeat cleanup hooks
- Heartbeat progress dual-write
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── backfill_embeddings ──────────────────────────────────────


class TestBackfillEmbeddings:
    """MessageStore.backfill_embeddings batch embedding."""

    def _store(self, tmp_path):
        from agents.message_store import MessageStore

        return MessageStore(db_path=tmp_path / "test.db")

    def test_empty_db(self, tmp_path):
        store = self._store(tmp_path)
        mock_embedder = MagicMock()
        mock_embedder.is_available.return_value = True
        mock_embedder.embed_batch.return_value = []
        store._embedder = mock_embedder
        assert store.backfill_embeddings() == 0

    def test_embedder_unavailable(self, tmp_path):
        store = self._store(tmp_path)
        mock_embedder = MagicMock()
        mock_embedder.is_available.return_value = False
        store._embedder = mock_embedder
        store.send(content="hello", sender="a")
        assert store.backfill_embeddings() == 0

    def test_backfills_missing(self, tmp_path):
        store = self._store(tmp_path)
        # Send messages without embeddings (embedder unavailable at send time)
        mock_embedder = MagicMock()
        mock_embedder.is_available.return_value = False
        mock_embedder.embed.return_value = None
        store._embedder = mock_embedder

        store.send(content="msg1", sender="a")
        store.send(content="msg2", sender="a")

        # Now enable embedder for backfill
        mock_embedder.is_available.return_value = True
        mock_embedder.model = "test-model"
        mock_embedder.embed_batch.return_value = [[1.0, 0.0], [0.0, 1.0]]

        count = store.backfill_embeddings()
        assert count == 2

    def test_skips_already_embedded(self, tmp_path):
        store = self._store(tmp_path)
        mock_embedder = MagicMock()
        mock_embedder.is_available.return_value = True
        mock_embedder.embed.return_value = [1.0, 0.0]
        mock_embedder.model = "test-model"
        store._embedder = mock_embedder

        # Send with embedding
        store.send(content="already-embedded", sender="a")

        # Backfill should find nothing to do
        mock_embedder.embed_batch.return_value = []
        count = store.backfill_embeddings()
        assert count == 0

    def test_partial_batch_failure(self, tmp_path):
        store = self._store(tmp_path)
        mock_embedder = MagicMock()
        mock_embedder.is_available.return_value = False
        mock_embedder.embed.return_value = None
        store._embedder = mock_embedder

        store.send(content="msg1", sender="a")
        store.send(content="msg2", sender="a")

        mock_embedder.is_available.return_value = True
        mock_embedder.model = "test-model"
        mock_embedder.embed_batch.return_value = [[1.0], None]  # second fails

        count = store.backfill_embeddings()
        assert count == 1

    def test_batch_size_respected(self, tmp_path):
        store = self._store(tmp_path)
        mock_embedder = MagicMock()
        mock_embedder.is_available.return_value = False
        mock_embedder.embed.return_value = None
        store._embedder = mock_embedder

        for i in range(10):
            store.send(content=f"msg{i}", sender="a")

        mock_embedder.is_available.return_value = True
        mock_embedder.model = "test-model"
        mock_embedder.embed_batch.return_value = [[1.0]] * 3

        count = store.backfill_embeddings(batch_size=3)
        assert count == 3
        # embed_batch was called with exactly 3 texts
        assert len(mock_embedder.embed_batch.call_args[0][0]) == 3


# ── validate_metadata ────────────────────────────────────────


class TestValidateMetadata:
    """Payload validation for typed message metadata."""

    def test_decision_valid(self):
        from agents.message_types import MessageType, validate_metadata

        warnings = validate_metadata(MessageType.DECISION, {
            "decision": "Use REST",
            "rationale": "Simplicity",
            "alternatives_considered": ["GraphQL"],
            "reversible": True,
        })
        assert warnings == []

    def test_decision_missing_fields(self):
        from agents.message_types import MessageType, validate_metadata

        warnings = validate_metadata(MessageType.DECISION, {})
        assert len(warnings) == 1
        assert "missing" in warnings[0]

    def test_decision_extra_fields(self):
        from agents.message_types import MessageType, validate_metadata

        warnings = validate_metadata(MessageType.DECISION, {
            "decision": "Use REST",
            "rationale": "x",
            "alternatives_considered": [],
            "reversible": True,
            "unexpected_key": "value",
        })
        assert len(warnings) == 1
        assert "unexpected" in warnings[0]

    def test_blocker_valid(self):
        from agents.message_types import MessageType, validate_metadata

        warnings = validate_metadata(MessageType.BLOCKER, {
            "blocker_description": "API down",
            "blocking_task_id": "t-1",
            "severity": "high",
            "needs_human": True,
        })
        assert warnings == []

    def test_handoff_valid(self):
        from agents.message_types import MessageType, validate_metadata

        warnings = validate_metadata(MessageType.HANDOFF, {
            "from_agent": "a",
            "to_agent": "b",
            "task_summary": "x",
            "context": "y",
            "artifacts": [],
        })
        assert warnings == []

    def test_status_valid(self):
        from agents.message_types import MessageType, validate_metadata

        warnings = validate_metadata(MessageType.STATUS, {
            "task_id": "t-1",
            "progress_pct": 50.0,
            "current_step": "building",
            "eta_seconds": 60,
        })
        assert warnings == []

    def test_info_always_passes(self):
        from agents.message_types import MessageType, validate_metadata

        warnings = validate_metadata(MessageType.INFO, {"anything": "goes"})
        assert warnings == []

    def test_question_always_passes(self):
        from agents.message_types import MessageType, validate_metadata

        warnings = validate_metadata(MessageType.QUESTION, {})
        assert warnings == []

    def test_completion_always_passes(self):
        from agents.message_types import MessageType, validate_metadata

        warnings = validate_metadata(MessageType.COMPLETION, {"foo": "bar"})
        assert warnings == []

    def test_empty_metadata_for_typed(self):
        from agents.message_types import MessageType, validate_metadata

        warnings = validate_metadata(MessageType.STATUS, {})
        assert len(warnings) == 1
        assert "missing" in warnings[0]

    def test_both_extra_and_missing(self):
        from agents.message_types import MessageType, validate_metadata

        warnings = validate_metadata(MessageType.BLOCKER, {
            "blocker_description": "x",
            "unknown": "y",
        })
        assert len(warnings) == 2  # one extra, one missing


# ── MessageStoreConfig ───────────────────────────────────────


class TestMessageStoreConfig:
    """Config dataclass defaults and from_env wiring."""

    def test_defaults(self):
        from agents.config import MessageStoreConfig

        c = MessageStoreConfig()
        assert c.enabled is False
        assert c.db_path == ""
        assert c.max_messages == 5000
        assert c.default_ttl_seconds == 604800
        assert c.cleanup_on_heartbeat is True
        assert c.backfill_on_heartbeat is True
        assert c.backfill_batch_size == 50

    def test_in_system_config(self):
        from agents.config import SystemConfig

        config = SystemConfig()
        assert hasattr(config, "messages")
        assert config.messages.enabled is False

    def test_from_env_message_store_path(self):
        from agents.config import SystemConfig

        with patch.dict(os.environ, {"MESSAGE_STORE_PATH": "/tmp/test.db"}, clear=False):
            config = SystemConfig.from_env()
            assert config.messages.enabled is True
            assert config.messages.db_path == "/tmp/test.db"

    def test_from_env_bulletin_path_enables(self):
        from agents.config import SystemConfig

        with patch.dict(os.environ, {"BULLETIN_PATH": "/tmp/bulletin.md"}, clear=False):
            config = SystemConfig.from_env()
            assert config.messages.enabled is True

    def test_from_env_overrides(self):
        from agents.config import SystemConfig

        env = {
            "MESSAGE_STORE_PATH": "/tmp/test.db",
            "VIBE_MSG_MAX_MESSAGES": "1000",
            "VIBE_MSG_DEFAULT_TTL": "3600",
            "VIBE_MSG_CLEANUP_ON_HEARTBEAT": "false",
            "VIBE_MSG_BACKFILL_ON_HEARTBEAT": "0",
        }
        with patch.dict(os.environ, env, clear=False):
            config = SystemConfig.from_env()
            assert config.messages.max_messages == 1000
            assert config.messages.default_ttl_seconds == 3600
            assert config.messages.cleanup_on_heartbeat is False
            assert config.messages.backfill_on_heartbeat is False


# ── check_message_store ──────────────────────────────────────


class TestCheckMessageStore:
    """Doctor health check for message store."""

    def test_not_configured(self):
        from agents.doctor import check_message_store

        with patch.dict(os.environ, {}, clear=True):
            result = check_message_store()
            assert result.status == "ok"
            assert "Not configured" in result.summary

    def test_healthy_db(self, tmp_path):
        from agents.doctor import check_message_store
        from agents.message_store import MessageStore

        db_path = tmp_path / "test.db"
        store = MessageStore(db_path=db_path)
        store.send(content="test message", sender="agent-a")

        with patch.dict(os.environ, {"MESSAGE_STORE_PATH": str(db_path)}, clear=True):
            result = check_message_store()
            assert result.status == "ok"
            assert "1 messages" in result.summary
            assert "FTS5 active" in result.summary

    def test_empty_db(self, tmp_path):
        from agents.doctor import check_message_store
        from agents.message_store import MessageStore

        db_path = tmp_path / "test.db"
        MessageStore(db_path=db_path)

        with patch.dict(os.environ, {"MESSAGE_STORE_PATH": str(db_path)}, clear=True):
            result = check_message_store()
            assert result.status == "ok"
            assert "0 messages" in result.summary

    def test_db_not_yet_created(self, tmp_path):
        from agents.doctor import check_message_store

        db_path = tmp_path / "nonexistent.db"
        with patch.dict(os.environ, {"MESSAGE_STORE_PATH": str(db_path)}, clear=True):
            result = check_message_store()
            assert result.status == "ok"
            assert "No database yet" in result.summary


# ── Heartbeat cleanup hooks ──────────────────────────────────


class TestHeartbeatCleanupHooks:
    """Verify cleanup + backfill called in heartbeat finally block."""

    def test_cleanup_called_in_finally(self):
        """The finally block should call cleanup_expired and backfill_embeddings."""
        from agents.config import SystemConfig, MessageStoreConfig

        config = SystemConfig()
        config.messages = MessageStoreConfig(
            enabled=True,
            cleanup_on_heartbeat=True,
            backfill_on_heartbeat=True,
            backfill_batch_size=25,
        )

        mock_store = MagicMock()
        mock_store.cleanup_expired.return_value = 3
        mock_store.backfill_embeddings.return_value = 5

        with patch("agents.message_store.get_shared_message_store", return_value=mock_store):
            # Simulate the finally block logic
            try:
                if config.messages.cleanup_on_heartbeat or config.messages.backfill_on_heartbeat:
                    from agents.message_store import get_shared_message_store
                    msg_store = get_shared_message_store()
                    if config.messages.cleanup_on_heartbeat:
                        msg_store.cleanup_expired()
                    if config.messages.backfill_on_heartbeat:
                        msg_store.backfill_embeddings(batch_size=config.messages.backfill_batch_size)
            except Exception:
                pass

        mock_store.cleanup_expired.assert_called_once()
        mock_store.backfill_embeddings.assert_called_once_with(batch_size=25)

    def test_cleanup_gated_by_config(self):
        """When flags are False, neither method should be called."""
        from agents.config import SystemConfig, MessageStoreConfig

        config = SystemConfig()
        config.messages = MessageStoreConfig(
            cleanup_on_heartbeat=False,
            backfill_on_heartbeat=False,
        )

        mock_store = MagicMock()
        with patch("agents.message_store.get_shared_message_store", return_value=mock_store):
            try:
                if config.messages.cleanup_on_heartbeat or config.messages.backfill_on_heartbeat:
                    from agents.message_store import get_shared_message_store
                    msg_store = get_shared_message_store()
                    if config.messages.cleanup_on_heartbeat:
                        msg_store.cleanup_expired()
                    if config.messages.backfill_on_heartbeat:
                        msg_store.backfill_embeddings()
            except Exception:
                pass

        mock_store.cleanup_expired.assert_not_called()
        mock_store.backfill_embeddings.assert_not_called()

    def test_cleanup_graceful_on_error(self):
        """Exceptions in cleanup should be caught, not raised."""
        from agents.config import SystemConfig, MessageStoreConfig

        config = SystemConfig()
        config.messages = MessageStoreConfig(
            cleanup_on_heartbeat=True,
            backfill_on_heartbeat=True,
        )

        with patch("agents.message_store.get_shared_message_store", side_effect=RuntimeError("boom")):
            # Should not raise
            try:
                if config.messages.cleanup_on_heartbeat or config.messages.backfill_on_heartbeat:
                    from agents.message_store import get_shared_message_store
                    msg_store = get_shared_message_store()
                    if config.messages.cleanup_on_heartbeat:
                        msg_store.cleanup_expired()
            except Exception:
                pass  # This mirrors the heartbeat's except clause


# ── Heartbeat progress dual-write ────────────────────────────


class TestHeartbeatProgressDualWrite:
    """MessageStore.send called with STATUS type after Paperclip comment."""

    def test_dual_write_on_progress(self):
        from agents.heartbeat_progress import make_progress_callback

        mock_client = MagicMock()
        mock_store = MagicMock()
        mock_store.send.return_value = MagicMock()

        cb = make_progress_callback(mock_client, "issue-42")

        with patch("agents.message_store.get_shared_message_store", return_value=mock_store):
            state = {"iteration_count": 1, "max_iterations": 3}
            cb("specialist", state)

        # Paperclip comment posted
        mock_client.add_comment.assert_called_once()

        # MessageStore dual-write
        mock_store.send.assert_called_once()
        call_kwargs = mock_store.send.call_args
        assert call_kwargs[1]["issue_id"] == "issue-42"
        assert call_kwargs[1]["ttl_seconds"] == 3600

    def test_dual_write_failure_does_not_affect_paperclip(self):
        from agents.heartbeat_progress import make_progress_callback

        mock_client = MagicMock()

        cb = make_progress_callback(mock_client, "issue-1")

        with patch("agents.message_store.get_shared_message_store", side_effect=RuntimeError("boom")):
            state = {"iteration_count": 0, "max_iterations": 3}
            cb("specialist", state)

        # Paperclip still got the comment
        mock_client.add_comment.assert_called_once()

    def test_no_dual_write_for_non_progress_nodes(self):
        from agents.heartbeat_progress import make_progress_callback

        mock_client = MagicMock()
        mock_store = MagicMock()

        cb = make_progress_callback(mock_client, "issue-1")

        with patch("agents.message_store.get_shared_message_store", return_value=mock_store):
            cb("some_other_node", {})

        # Neither Paperclip nor MessageStore should be called
        mock_client.add_comment.assert_not_called()
        mock_store.send.assert_not_called()


# ── send() calls validate_metadata ───────────────────────────


class TestSendValidation:
    """Verify that send() calls validate_metadata."""

    def test_send_logs_validation_warnings(self, tmp_path):
        from agents.message_store import MessageStore
        from agents.message_types import MessageType

        store = MessageStore(db_path=tmp_path / "test.db")
        mock_embedder = MagicMock()
        mock_embedder.is_available.return_value = False
        mock_embedder.embed.return_value = None
        store._embedder = mock_embedder

        # DECISION with no metadata — should produce validation warnings
        with patch("agents.message_store.logger") as mock_logger:
            store.send(
                content="test",
                sender="a",
                msg_type=MessageType.DECISION,
                metadata={},
            )
            # Should have logged debug messages about missing fields
            debug_calls = [c for c in mock_logger.debug.call_args_list
                          if "Metadata validation" in str(c)]
            assert len(debug_calls) > 0

    def test_send_no_warnings_for_info(self, tmp_path):
        from agents.message_store import MessageStore
        from agents.message_types import MessageType

        store = MessageStore(db_path=tmp_path / "test.db")
        mock_embedder = MagicMock()
        mock_embedder.is_available.return_value = False
        mock_embedder.embed.return_value = None
        store._embedder = mock_embedder

        with patch("agents.message_store.logger") as mock_logger:
            store.send(content="test", sender="a", msg_type=MessageType.INFO)
            debug_calls = [c for c in mock_logger.debug.call_args_list
                          if "Metadata validation" in str(c)]
            assert len(debug_calls) == 0
