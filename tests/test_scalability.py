"""
Tests for the Paperclip Bridge (Slack/Mattermost ↔ Paperclip).

Covers:
- TTL-based dedup cache (eviction, expiry, hard cap, thread safety)
- Request extraction from mentions
- Response formatting and chunking
- Issue creation from mentions
- Issue completion polling
- Bridge lifecycle
"""

import os
import threading
import time
from datetime import datetime, timedelta
from queue import Queue, Full
from unittest.mock import MagicMock, patch

import pytest

os.environ["GENESIA_DISABLE_REMOTE_SKILLS"] = "1"


# ===== TTL-Based Dedup Cache Tests =====


class TestDedupCache:
    """Test the TTL-based message deduplication cache."""

    def _make_bridge(self):
        """Create a PaperclipBridge with mocked dependencies."""
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            bridge = PaperclipBridge.__new__(PaperclipBridge)
            bridge.processed_messages = {}
            bridge.message_lock = threading.Lock()
            return bridge

    def test_mark_and_check(self):
        bridge = self._make_bridge()
        bridge._mark_message_processed("msg1")
        assert bridge._is_message_processed("msg1")

    def test_unprocessed_returns_false(self):
        bridge = self._make_bridge()
        assert not bridge._is_message_processed("unknown")

    @patch("agents.daemon.DEDUP_TTL_SECONDS", 0)
    def test_ttl_expiry(self):
        bridge = self._make_bridge()
        bridge._mark_message_processed("msg_old")
        time.sleep(0.01)
        assert not bridge._is_message_processed("msg_old")

    @patch("agents.daemon.DEDUP_MAX_SIZE", 2)
    def test_hard_cap_eviction(self):
        bridge = self._make_bridge()
        bridge._mark_message_processed("a")
        bridge._mark_message_processed("b")
        bridge._mark_message_processed("c")
        assert len(bridge.processed_messages) <= 2

    def test_thread_safety(self):
        bridge = self._make_bridge()
        errors = []

        def writer(prefix):
            try:
                for i in range(50):
                    bridge._mark_message_processed(f"{prefix}_{i}")
                    bridge._is_message_processed(f"{prefix}_{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(f"t{t}",)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors


# ===== Request Extraction =====


class TestRequestExtraction:
    """Test extracting request text from mention messages."""

    def _make_bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            bridge = PaperclipBridge.__new__(PaperclipBridge)
            return bridge

    def test_strips_mattermost_mention(self):
        bridge = self._make_bridge()
        msg = {"text": "@genesia-bot Fix the login bug"}
        result = bridge._extract_request_from_message(msg)
        assert result == "Fix the login bug"

    def test_strips_slack_mention(self):
        bridge = self._make_bridge()
        msg = {"text": "<@U12345> Write a function to parse JSON"}
        result = bridge._extract_request_from_message(msg)
        assert result == "Write a function to parse JSON"

    def test_rejects_empty(self):
        bridge = self._make_bridge()
        assert bridge._extract_request_from_message({"text": ""}) is None

    def test_rejects_too_short(self):
        bridge = self._make_bridge()
        msg = {"text": "@bot hi"}
        result = bridge._extract_request_from_message(msg)
        assert result is None  # "hi" is < 5 chars after stripping mention


# ===== Response Formatting =====


class TestResponseFormatting:
    """Test response chunking for chat platforms."""

    def _make_bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            bridge = PaperclipBridge.__new__(PaperclipBridge)
            return bridge

    def test_short_response_single_chunk(self):
        bridge = self._make_bridge()
        chunks = bridge._format_response_for_chat("Hello", max_length=100)
        assert len(chunks) == 1
        assert chunks[0] == "Hello"

    def test_long_response_split(self):
        bridge = self._make_bridge()
        text = "\n".join([f"Line {i}" for i in range(100)])
        chunks = bridge._format_response_for_chat(text, max_length=200)
        assert len(chunks) > 1

    def test_continuation_markers(self):
        bridge = self._make_bridge()
        text = "\n".join([f"Line {i}" for i in range(100)])
        chunks = bridge._format_response_for_chat(text, max_length=200)
        assert "Continued" in chunks[0]


# ===== Issue Creation =====


class TestIssueCreation:
    """Test creating Paperclip issues from mentions."""

    def _make_bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            bridge = PaperclipBridge.__new__(PaperclipBridge)
            bridge.paperclip_client = MagicMock()
            return bridge

    def test_creates_issue_with_title(self):
        bridge = self._make_bridge()
        issue = MagicMock()
        issue.id = "ISSUE-1"
        bridge.paperclip_client.create_issue.return_value = issue

        mention = {
            "platform": "slack",
            "user_id": "U123",
            "channel_id": "C456",
        }

        result = bridge._create_issue_from_mention(mention, "Fix the login bug")

        assert result == "ISSUE-1"
        call_args = bridge.paperclip_client.create_issue.call_args
        assert "Fix the login bug" in call_args[1]["title"]

    def test_includes_source_metadata(self):
        bridge = self._make_bridge()
        issue = MagicMock()
        issue.id = "ISSUE-2"
        bridge.paperclip_client.create_issue.return_value = issue

        mention = {
            "platform": "mattermost",
            "user_id": "user42",
            "channel_id": "ch789",
        }

        bridge._create_issue_from_mention(mention, "Test request")

        call_args = bridge.paperclip_client.create_issue.call_args
        desc = call_args[1]["description"]
        assert "source:bridge" in desc
        assert "platform:mattermost" in desc

    def test_returns_none_on_api_error(self):
        from agents.paperclip_client import PaperclipAPIError
        bridge = self._make_bridge()
        bridge.paperclip_client.create_issue.side_effect = PaperclipAPIError("fail", 500)

        result = bridge._create_issue_from_mention(
            {"platform": "slack", "user_id": "U1", "channel_id": "C1"},
            "test",
        )
        assert result is None

    def test_returns_none_without_client(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            bridge = PaperclipBridge.__new__(PaperclipBridge)
            bridge.paperclip_client = None

            result = bridge._create_issue_from_mention(
                {"platform": "slack", "user_id": "U1", "channel_id": "C1"},
                "test",
            )
            assert result is None


# ===== Issue Completion Polling =====


class TestCompletionPolling:
    """Test polling Paperclip for issue completion."""

    def _make_bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            bridge = PaperclipBridge.__new__(PaperclipBridge)
            bridge.paperclip_client = MagicMock()
            bridge.shutdown_event = threading.Event()
            return bridge

    @patch("agents.daemon.COMPLETION_POLL_INTERVAL", 0)
    @patch("agents.daemon.COMPLETION_TIMEOUT", 5)
    def test_returns_on_done(self):
        bridge = self._make_bridge()
        issue = MagicMock()
        issue.status = "done"
        bridge.paperclip_client.get_issue.return_value = issue

        comment = MagicMock()
        comment.body = "## Completed (score: 90/100)\n\nResult here"
        bridge.paperclip_client.get_comments.return_value = [comment]

        result = bridge._poll_issue_completion("ISSUE-1")

        assert result is not None
        assert result["status"] == "done"
        assert "Completed" in result["output"]

    @patch("agents.daemon.COMPLETION_POLL_INTERVAL", 0)
    @patch("agents.daemon.COMPLETION_TIMEOUT", 5)
    def test_returns_on_blocked(self):
        bridge = self._make_bridge()
        issue = MagicMock()
        issue.status = "blocked"
        bridge.paperclip_client.get_issue.return_value = issue
        bridge.paperclip_client.get_comments.return_value = []

        result = bridge._poll_issue_completion("ISSUE-2")

        assert result is not None
        assert result["status"] == "blocked"

    @patch("agents.daemon.COMPLETION_POLL_INTERVAL", 0)
    @patch("agents.daemon.COMPLETION_TIMEOUT", 0)
    def test_timeout(self):
        bridge = self._make_bridge()
        issue = MagicMock()
        issue.status = "in_progress"
        bridge.paperclip_client.get_issue.return_value = issue

        result = bridge._poll_issue_completion("ISSUE-3")

        assert result is not None
        assert result["status"] == "timeout"

    @patch("agents.daemon.COMPLETION_POLL_INTERVAL", 0)
    @patch("agents.daemon.COMPLETION_TIMEOUT", 5)
    def test_returns_none_on_shutdown(self):
        bridge = self._make_bridge()
        bridge.shutdown_event.set()

        result = bridge._poll_issue_completion("ISSUE-4")
        assert result is None


# ===== Bridge Status =====


class TestBridgeStatus:
    """Test bridge status reporting."""

    def _make_bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            bridge = PaperclipBridge.__new__(PaperclipBridge)
            bridge.running = True
            bridge.metrics = {
                "start_time": datetime.now(),
                "requests_created": 5,
                "requests_completed": 3,
                "requests_failed": 1,
            }
            bridge.inflight = {"ISSUE-1": {}}
            bridge.inflight_lock = threading.Lock()
            bridge.request_queue = Queue()
            bridge.mattermost_client = None
            bridge.slack_client = MagicMock()
            bridge.mattermost_bot_username = None
            bridge.slack_bot_user_id = "U123"
            return bridge

    def test_status_fields(self):
        bridge = self._make_bridge()
        status = bridge.status()

        assert status["running"] is True
        assert status["inflight_issues"] == 1
        assert status["metrics"]["requests_created"] == 5
        assert status["messengers"]["slack"]["enabled"] is True
        assert status["messengers"]["mattermost"]["enabled"] is False

    def test_readiness(self):
        bridge = self._make_bridge()
        bridge.paperclip_client = MagicMock()
        assert bridge._is_ready() is True

    def test_not_ready_without_paperclip(self):
        bridge = self._make_bridge()
        bridge.paperclip_client = None
        assert bridge._is_ready() is False
