"""Tests for the V2 BulletinBoardTool (SQLite-backed).

Covers:
- All tool actions: post, read, search, reply, thread, stats
- Backward-compatible read_recent_entries() for context injection
- Parameter validation and error handling
- Paperclip dual-write integration (mocked)
- Env gating (_is_configured)
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_store(tmp_path):
    """Create a fresh MessageStore for testing."""
    from agents.message_store import MessageStore

    return MessageStore(db_path=tmp_path / "test.db")


def _make_tool():
    """Create a BulletinBoardTool instance."""
    from agents.tools.bulletin_board import BulletinBoardTool

    return BulletinBoardTool()


# ── read_recent_entries (backward compat) ────────────────────────


class TestReadRecentEntries:
    """Standalone function used by graph.py inject_memory."""

    def test_returns_empty_when_not_configured(self):
        from agents.tools.bulletin_board import read_recent_entries

        with patch.dict(os.environ, {}, clear=True):
            assert read_recent_entries() == ""

    def test_returns_formatted_messages(self, tmp_path):
        from agents.tools.bulletin_board import read_recent_entries

        store = _make_store(tmp_path)
        store.send(content="Hello world", sender="agent-a", topic="test")

        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = read_recent_entries(limit=5)
                assert "Bulletin Board" in result
                assert "Hello world" in result

    def test_returns_empty_when_no_messages(self, tmp_path):
        from agents.tools.bulletin_board import read_recent_entries

        store = _make_store(tmp_path)
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = read_recent_entries()
                assert result == ""

    def test_handles_exception_gracefully(self, tmp_path):
        from agents.tools.bulletin_board import read_recent_entries

        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "agents.tools.bulletin_board._get_store",
                side_effect=RuntimeError("fail"),
            ):
                result = read_recent_entries()
                assert result == ""


# ── Tool: post action ────────────────────────────────────────────


class TestBulletinBoardPost:
    """Post action on BulletinBoardTool."""

    def test_post_success(self, tmp_path):
        tool = _make_tool()
        store = _make_store(tmp_path)
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="post", message="Hello!")
                assert result.success
                assert "Posted" in result.output
                assert result.metadata["msg_type"] == "info"

    def test_post_with_all_params(self, tmp_path):
        tool = _make_tool()
        store = _make_store(tmp_path)
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(
                    action="post",
                    message="Architecture decision",
                    topic="design",
                    msg_type="decision",
                    recipient="agent-b",
                    ttl_seconds=3600,
                    metadata={"key": "val"},
                )
                assert result.success
                assert result.metadata["msg_type"] == "decision"
                assert result.metadata["topic"] == "design"

    def test_post_no_message(self, tmp_path):
        tool = _make_tool()
        store = _make_store(tmp_path)
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="post")
                assert not result.success
                assert "No message" in result.error

    def test_post_invalid_msg_type_defaults_to_info(self, tmp_path):
        tool = _make_tool()
        store = _make_store(tmp_path)
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(
                    action="post",
                    message="test",
                    msg_type="bogus",
                )
                assert result.success
                assert result.metadata["msg_type"] == "info"

    def test_post_with_paperclip_dual_write(self, tmp_path):
        tool = _make_tool()
        store = _make_store(tmp_path)
        mock_bridge = MagicMock()
        mock_bridge.post_comment.return_value = "comment-42"
        tool._bridge = mock_bridge

        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(
                    action="post",
                    message="dual-write test",
                    issue_id="issue-1",
                )
                assert result.success
                mock_bridge.post_comment.assert_called_once()


# ── Tool: read action ────────────────────────────────────────────


class TestBulletinBoardRead:
    """Read action on BulletinBoardTool."""

    def test_read_empty(self, tmp_path):
        tool = _make_tool()
        store = _make_store(tmp_path)
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="read")
                assert result.success
                assert "No messages" in result.output

    def test_read_returns_messages(self, tmp_path):
        tool = _make_tool()
        store = _make_store(tmp_path)
        store.send(content="Hello", sender="agent-a")
        store.send(content="World", sender="agent-b")

        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="read", limit=10)
                assert result.success
                assert "Hello" in result.output
                assert "World" in result.output

    def test_read_with_type_filter(self, tmp_path):
        from agents.message_types import MessageType

        tool = _make_tool()
        store = _make_store(tmp_path)
        store.send(content="info-msg", sender="a", msg_type=MessageType.INFO)
        store.send(content="blocker-msg", sender="a", msg_type=MessageType.BLOCKER)

        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="read", type_filter="blocker")
                assert result.success
                assert "blocker-msg" in result.output

    def test_read_unread_only(self, tmp_path):
        tool = _make_tool()
        store = _make_store(tmp_path)
        m1 = store.send(content="unread", sender="a", recipient="*")
        m2 = store.send(content="read-msg", sender="a", recipient="*")
        store.mark_read(m2.id, "unknown-agent")

        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="read", unread_only=True)
                assert result.success
                assert "unread" in result.output


# ── Tool: search action ──────────────────────────────────────────


class TestBulletinBoardSearch:
    """Search action on BulletinBoardTool."""

    def test_search_finds_messages(self, tmp_path):
        tool = _make_tool()
        store = _make_store(tmp_path)
        store.send(content="PostgreSQL migration plan", sender="a")
        store.send(content="Redis cache setup", sender="a")

        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="search", query="PostgreSQL")
                assert result.success
                assert "PostgreSQL" in result.output

    def test_search_no_query(self, tmp_path):
        tool = _make_tool()
        store = _make_store(tmp_path)
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="search")
                assert not result.success
                assert "No search query" in result.error

    def test_search_no_results(self, tmp_path):
        tool = _make_tool()
        store = _make_store(tmp_path)
        store.send(content="hello", sender="a")

        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="search", query="xyznonexistent")
                assert result.success
                assert "No messages" in result.output


# ── Tool: reply action ───────────────────────────────────────────


class TestBulletinBoardReply:
    """Reply action on BulletinBoardTool."""

    def test_reply_success(self, tmp_path):
        tool = _make_tool()
        store = _make_store(tmp_path)
        root = store.send(content="question?", sender="a", topic="design")

        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(
                    action="reply",
                    reply_to=root.id,
                    message="answer!",
                )
                assert result.success
                assert result.metadata["parent_id"] == root.id

    def test_reply_no_parent_id(self, tmp_path):
        tool = _make_tool()
        store = _make_store(tmp_path)
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="reply", message="orphan")
                assert not result.success
                assert "No reply_to" in result.error

    def test_reply_no_message(self, tmp_path):
        tool = _make_tool()
        store = _make_store(tmp_path)
        root = store.send(content="q", sender="a")

        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="reply", reply_to=root.id)
                assert not result.success
                assert "No message" in result.error

    def test_reply_nonexistent_parent(self, tmp_path):
        tool = _make_tool()
        store = _make_store(tmp_path)
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(
                    action="reply",
                    reply_to="nonexistent",
                    message="reply",
                )
                assert not result.success
                assert "not found" in result.error


# ── Tool: thread action ──────────────────────────────────────────


class TestBulletinBoardThread:
    """Thread action on BulletinBoardTool."""

    def test_thread_returns_conversation(self, tmp_path):
        tool = _make_tool()
        store = _make_store(tmp_path)
        root = store.send(content="root", sender="a")
        store.reply(root.id, content="reply1", sender="b")
        store.reply(root.id, content="reply2", sender="c")

        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="thread", message_id=root.id)
                assert result.success
                assert result.metadata["thread_length"] == 3

    def test_thread_no_message_id(self, tmp_path):
        tool = _make_tool()
        store = _make_store(tmp_path)
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="thread")
                assert not result.success
                assert "No message_id" in result.error

    def test_thread_nonexistent(self, tmp_path):
        tool = _make_tool()
        store = _make_store(tmp_path)
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="thread", message_id="nonexistent")
                assert result.success
                assert "No thread" in result.output


# ── Tool: stats action ───────────────────────────────────────────


class TestBulletinBoardStats:
    """Stats action on BulletinBoardTool."""

    def test_stats_success(self, tmp_path):
        from agents.message_types import MessageType

        tool = _make_tool()
        store = _make_store(tmp_path)
        store.send(content="a", sender="x", msg_type=MessageType.INFO)
        store.send(content="b", sender="x", msg_type=MessageType.BLOCKER)

        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="stats")
                assert result.success
                assert "Total messages: 2" in result.output
                assert result.metadata["total_messages"] == 2


# ── Error handling ───────────────────────────────────────────────


class TestBulletinBoardErrors:
    """Error handling and edge cases."""

    def test_no_action(self, tmp_path):
        tool = _make_tool()
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            result = tool.execute()
            assert not result.success
            assert "No action" in result.error

    def test_unknown_action(self, tmp_path):
        tool = _make_tool()
        store = _make_store(tmp_path)
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="invalid")
                assert not result.success
                assert "Unknown action" in result.error

    def test_not_configured(self):
        tool = _make_tool()
        with patch.dict(os.environ, {}, clear=True):
            result = tool.execute(action="read")
            assert not result.success
            assert "not configured" in result.error

    def test_store_init_failure(self, tmp_path):
        tool = _make_tool()
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "agents.tools.bulletin_board._get_store",
                side_effect=RuntimeError("db fail"),
            ):
                result = tool.execute(action="read")
                assert not result.success
                assert "Failed to initialize" in result.error

    def test_tool_name_and_category(self):
        from agents.tools.registry import ToolCategory

        tool = _make_tool()
        assert tool.name == "bulletin_board"
        assert tool.category == ToolCategory.SPECIALIZED

    def test_parameters_schema(self):
        tool = _make_tool()
        schema = tool._get_parameters_schema()
        assert schema["type"] == "object"
        assert "action" in schema["properties"]
        assert "msg_type" in schema["properties"]
        assert "reply_to" in schema["properties"]
        assert "action" in schema["required"]
