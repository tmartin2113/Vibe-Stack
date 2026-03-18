"""
Tests for WebSocket Client and WS-driven integrations.

Tests:
- PaperclipWSClient: URL building, subscribe/unsubscribe, dispatch, reconnect
- WSCancellationWatcher: hybrid push + fallback polling
- Orchestrator WS-driven POLL blocking
"""

import json
import threading
import time
from unittest.mock import MagicMock, patch, call

import pytest

from agents.ws_client import PaperclipWSClient
from agents.cancellation import (
    CancellationToken,
    CancellationPoller,
    WSCancellationWatcher,
    start_cancellation_poller,
)
from agents.config import SystemConfig, PaperclipConfig
from agents.heartbeat import HeartbeatResult
from agents.paperclip_client import Issue, PaperclipAPIError


# ════════════════════════════════════════════════════════════════
# PaperclipWSClient
# ════════════════════════════════════════════════════════════════


class TestWSClientURLBuilding:
    """Test WebSocket URL construction from HTTP API URL."""

    def test_http_to_ws(self):
        ws = PaperclipWSClient("http://localhost:3100", "comp-1", "key-1")
        assert ws._build_ws_url() == "ws://localhost:3100/api/companies/comp-1/events/ws"

    def test_https_to_wss(self):
        ws = PaperclipWSClient("https://api.example.com", "comp-2", "key-2")
        assert ws._build_ws_url() == "wss://api.example.com/api/companies/comp-2/events/ws"

    def test_trailing_slash_stripped(self):
        ws = PaperclipWSClient("http://localhost:3100/", "comp-1", "key-1")
        assert ws._build_ws_url() == "ws://localhost:3100/api/companies/comp-1/events/ws"


class TestWSClientSubscription:
    """Test subscribe/unsubscribe and dispatch without a real WS connection."""

    def _make_client(self):
        return PaperclipWSClient("http://localhost:3100", "comp-1", "key-1")

    def test_subscribe_and_dispatch(self):
        ws = self._make_client()
        received = []

        unsub = ws.subscribe(
            filter_fn=lambda e: e.get("type") == "test",
            handler_fn=lambda e: received.append(e),
        )

        # Simulate dispatch
        ws._dispatch(json.dumps({"type": "test", "payload": {"x": 1}}))
        ws._dispatch(json.dumps({"type": "other", "payload": {"x": 2}}))
        ws._dispatch(json.dumps({"type": "test", "payload": {"x": 3}}))

        assert len(received) == 2
        assert received[0]["payload"]["x"] == 1
        assert received[1]["payload"]["x"] == 3

        unsub()

    def test_unsubscribe_stops_delivery(self):
        ws = self._make_client()
        received = []

        unsub = ws.subscribe(
            filter_fn=lambda e: True,
            handler_fn=lambda e: received.append(e),
        )

        ws._dispatch(json.dumps({"type": "a"}))
        assert len(received) == 1

        unsub()
        ws._dispatch(json.dumps({"type": "b"}))
        assert len(received) == 1  # No new delivery

    def test_multiple_subscribers(self):
        ws = self._make_client()
        a_events = []
        b_events = []

        ws.subscribe(
            filter_fn=lambda e: e.get("type") == "a",
            handler_fn=lambda e: a_events.append(e),
        )
        ws.subscribe(
            filter_fn=lambda e: e.get("type") == "b",
            handler_fn=lambda e: b_events.append(e),
        )

        ws._dispatch(json.dumps({"type": "a"}))
        ws._dispatch(json.dumps({"type": "b"}))
        ws._dispatch(json.dumps({"type": "a"}))

        assert len(a_events) == 2
        assert len(b_events) == 1

    def test_invalid_json_ignored(self):
        ws = self._make_client()
        received = []

        ws.subscribe(
            filter_fn=lambda e: True,
            handler_fn=lambda e: received.append(e),
        )

        ws._dispatch("not json at all")
        ws._dispatch(b"\xff\xfe")
        assert len(received) == 0

    def test_handler_exception_doesnt_break_dispatch(self):
        ws = self._make_client()
        good_events = []

        def bad_handler(e):
            raise RuntimeError("boom")

        ws.subscribe(filter_fn=lambda e: True, handler_fn=bad_handler)
        ws.subscribe(filter_fn=lambda e: True, handler_fn=lambda e: good_events.append(e))

        ws._dispatch(json.dumps({"type": "x"}))
        assert len(good_events) == 1

    def test_double_unsubscribe_safe(self):
        ws = self._make_client()
        unsub = ws.subscribe(filter_fn=lambda e: True, handler_fn=lambda e: None)
        unsub()
        unsub()  # Should not raise

    def test_is_connected_defaults_false(self):
        ws = self._make_client()
        assert ws.is_connected is False

    def test_dispatch_bytes_message(self):
        ws = self._make_client()
        received = []
        ws.subscribe(filter_fn=lambda e: True, handler_fn=lambda e: received.append(e))
        ws._dispatch(json.dumps({"type": "t"}).encode("utf-8"))
        assert len(received) == 1


# ════════════════════════════════════════════════════════════════
# WSCancellationWatcher
# ════════════════════════════════════════════════════════════════


class TestWSCancellationWatcher:
    """Test hybrid WS + HTTP fallback cancellation watcher."""

    def test_ws_event_fires_token(self):
        ws_client = MagicMock()
        http_client = MagicMock()
        token = CancellationToken()

        # Capture the subscribe call to get filter/handler
        subscriber_holder = {}

        def fake_subscribe(filter_fn, handler_fn):
            subscriber_holder["filter"] = filter_fn
            subscriber_holder["handler"] = handler_fn
            return MagicMock()

        ws_client.subscribe.side_effect = fake_subscribe

        watcher = WSCancellationWatcher(ws_client, http_client, "issue-1", token)
        watcher.start()

        # Simulate a matching WS event
        event = {
            "type": "issue.status_changed",
            "payload": {"issueId": "issue-1", "status": "cancelled"},
        }
        assert subscriber_holder["filter"](event) is True
        subscriber_holder["handler"](event)

        assert token.is_cancelled is True

    def test_ws_event_wrong_issue_ignored(self):
        ws_client = MagicMock()
        http_client = MagicMock()
        token = CancellationToken()

        subscriber_holder = {}

        def fake_subscribe(filter_fn, handler_fn):
            subscriber_holder["filter"] = filter_fn
            subscriber_holder["handler"] = handler_fn
            return MagicMock()

        ws_client.subscribe.side_effect = fake_subscribe

        watcher = WSCancellationWatcher(ws_client, http_client, "issue-1", token)
        watcher.start()

        # Event for a different issue
        event = {
            "type": "issue.status_changed",
            "payload": {"issueId": "issue-other", "status": "cancelled"},
        }
        assert subscriber_holder["filter"](event) is False

    def test_non_cancelled_status_no_fire(self):
        ws_client = MagicMock()
        http_client = MagicMock()
        token = CancellationToken()

        subscriber_holder = {}

        def fake_subscribe(filter_fn, handler_fn):
            subscriber_holder["filter"] = filter_fn
            subscriber_holder["handler"] = handler_fn
            return MagicMock()

        ws_client.subscribe.side_effect = fake_subscribe

        watcher = WSCancellationWatcher(ws_client, http_client, "issue-1", token)
        watcher.start()

        event = {
            "type": "issue.status_changed",
            "payload": {"issueId": "issue-1", "status": "done"},
        }
        assert subscriber_holder["filter"](event) is True
        subscriber_holder["handler"](event)  # Handler only fires for "cancelled"

        assert token.is_cancelled is False

    def test_stop_unsubscribes_and_stops_poller(self):
        ws_client = MagicMock()
        http_client = MagicMock()
        token = CancellationToken()

        unsub_mock = MagicMock()
        ws_client.subscribe.return_value = unsub_mock

        watcher = WSCancellationWatcher(ws_client, http_client, "issue-1", token)
        watcher.start()
        watcher.stop()

        unsub_mock.assert_called_once()


class TestStartCancellationPoller:
    """Test the factory function with and without ws_client."""

    def test_without_ws_returns_poller(self):
        client = MagicMock()
        client.get_issue.return_value = Issue(id="i", title="t", status="in_progress")
        token = CancellationToken()

        result = start_cancellation_poller(client, "issue-1", token)
        assert isinstance(result, CancellationPoller)
        result.stop()

    def test_with_ws_returns_watcher(self):
        ws_client = MagicMock()
        ws_client.subscribe.return_value = MagicMock()
        client = MagicMock()
        token = CancellationToken()

        result = start_cancellation_poller(client, "issue-1", token, ws_client=ws_client)
        assert isinstance(result, WSCancellationWatcher)
        result.stop()


# ════════════════════════════════════════════════════════════════
# Orchestrator WS-driven POLL blocking
# ════════════════════════════════════════════════════════════════


class TestOrchestratorWSPoll:
    """Test WS-driven blocking in _poll_children."""

    @pytest.fixture
    def config(self):
        cfg = SystemConfig()
        cfg.paperclip = PaperclipConfig(
            enabled=True,
            api_url="http://localhost:3100",
            api_key="test-key",
            orchestrator_max_children=5,
            orchestrator_retry_failed=True,
            orchestrator_max_retries=1,
            orchestrator_poll_timeout=5,  # Short for tests
        )
        cfg.spending.enabled = False
        return cfg

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.agent_id = "orchestrator-1"
        client.company_id = "company-1"
        return client

    @pytest.fixture
    def parent_issue(self):
        return Issue(
            id="parent-1",
            title="Parent task",
            status="in_progress",
            assignee_agent_id="orchestrator-1",
        )

    def test_poll_without_ws_returns_idle(self, config, mock_client, parent_issue):
        """Without ws_client, _poll_children returns idle with retry hint."""
        from agents.orchestrator import _poll_children

        children = [
            Issue(id="c1", title="Child 1", status="done"),
            Issue(id="c2", title="Child 2", status="in_progress"),
        ]
        result = _poll_children(config, mock_client, parent_issue, children)
        assert result.status == "idle"
        assert result.retry_after_seconds == 30

    def test_poll_with_ws_blocks_until_aggregate(self, config, mock_client, parent_issue):
        """With ws_client connected, blocks until children complete then aggregates."""
        from agents.orchestrator import _poll_children

        ws_client = MagicMock()
        ws_client.is_connected = True

        subscriber_holder = {}

        def fake_subscribe(filter_fn, handler_fn):
            subscriber_holder["filter"] = filter_fn
            subscriber_holder["handler"] = handler_fn
            return MagicMock()

        ws_client.subscribe.side_effect = fake_subscribe

        children_in_progress = [
            Issue(id="c1", title="Child 1", status="done"),
            Issue(id="c2", title="Child 2", status="in_progress"),
        ]
        children_all_done = [
            Issue(id="c1", title="Child 1", status="done"),
            Issue(id="c2", title="Child 2", status="done"),
        ]

        # First get_children returns in_progress, second returns all done
        mock_client.get_children.side_effect = [children_all_done]

        # Mock _aggregate_and_present to avoid full aggregation
        with patch("agents.orchestrator._aggregate_and_present") as mock_agg:
            mock_agg.return_value = HeartbeatResult(
                status="success", issue_id="parent-1", summary="Aggregated",
            )

            # Run _poll_children in a thread so we can trigger the WS event
            result_holder = {}

            def run_poll():
                result_holder["result"] = _poll_children(
                    config, mock_client, parent_issue, children_in_progress,
                    ws_client=ws_client,
                )

            t = threading.Thread(target=run_poll)
            t.start()

            # Give the thread time to subscribe and block
            time.sleep(0.1)

            # Simulate WS event — fires the wake event
            if "handler" in subscriber_holder:
                subscriber_holder["handler"]({"type": "issue.status_changed"})

            t.join(timeout=5.0)
            assert not t.is_alive()

            assert result_holder["result"].status == "success"
            mock_agg.assert_called_once()

    def test_poll_ws_timeout_returns_idle(self, config, mock_client, parent_issue):
        """WS blocking times out → returns None → idle exit."""
        from agents.orchestrator import _poll_children

        config.paperclip.orchestrator_poll_timeout = 1  # 1 second timeout

        ws_client = MagicMock()
        ws_client.is_connected = True
        ws_client.subscribe.return_value = MagicMock()

        # get_children always returns in_progress
        mock_client.get_children.return_value = [
            Issue(id="c1", title="Child 1", status="in_progress"),
        ]

        children = [
            Issue(id="c1", title="Child 1", status="in_progress"),
        ]

        result = _poll_children(
            config, mock_client, parent_issue, children, ws_client=ws_client,
        )
        assert result.status == "idle"
        assert result.retry_after_seconds == 30

    def test_poll_ws_disconnected_falls_through(self, config, mock_client, parent_issue):
        """If ws_client is not connected, falls through to idle immediately."""
        from agents.orchestrator import _poll_children

        ws_client = MagicMock()
        ws_client.is_connected = False

        children = [
            Issue(id="c1", title="Child 1", status="in_progress"),
        ]

        result = _poll_children(
            config, mock_client, parent_issue, children, ws_client=ws_client,
        )
        assert result.status == "idle"
        assert result.retry_after_seconds == 30
        # Should NOT have tried to subscribe
        ws_client.subscribe.assert_not_called()


# ════════════════════════════════════════════════════════════════
# Heartbeat WS integration
# ════════════════════════════════════════════════════════════════


class TestTryConnectWS:
    """Test the _try_connect_ws helper."""

    def _make_client(self):
        client = MagicMock()
        client.api_url = "http://localhost:3100"
        client.company_id = "comp-1"
        client.api_key = "key-1"
        return client

    def test_returns_ws_on_success(self):
        from agents.heartbeat import _try_connect_ws

        mock_ws = MagicMock()
        mock_ws.wait_connected.return_value = True

        with patch("agents.ws_client.PaperclipWSClient", return_value=mock_ws):
            result = _try_connect_ws(self._make_client())
            assert result is mock_ws
            mock_ws.start.assert_called_once()
            mock_ws.wait_connected.assert_called_once()

    def test_returns_none_on_connect_timeout(self):
        from agents.heartbeat import _try_connect_ws

        mock_ws = MagicMock()
        mock_ws.wait_connected.return_value = False

        with patch("agents.ws_client.PaperclipWSClient", return_value=mock_ws):
            result = _try_connect_ws(self._make_client())
            assert result is None
            mock_ws.stop.assert_called_once()

    def test_returns_none_on_exception(self):
        from agents.heartbeat import _try_connect_ws

        with patch("agents.ws_client.PaperclipWSClient", side_effect=RuntimeError("boom")):
            result = _try_connect_ws(self._make_client())
            assert result is None


# ════════════════════════════════════════════════════════════════
# Config
# ════════════════════════════════════════════════════════════════


class TestOrchestratorPollTimeoutConfig:
    """Test orchestrator_poll_timeout configuration."""

    def test_default_value(self):
        cfg = PaperclipConfig()
        assert cfg.orchestrator_poll_timeout == 300

    def test_env_override(self):
        import os
        os.environ["PAPERCLIP_ORCHESTRATOR_POLL_TIMEOUT"] = "60"
        try:
            cfg = SystemConfig.from_env()
            assert cfg.paperclip.orchestrator_poll_timeout == 60
        finally:
            del os.environ["PAPERCLIP_ORCHESTRATOR_POLL_TIMEOUT"]
