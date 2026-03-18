"""
Tests for heartbeat metrics instrumentation.

Verifies that heartbeat runs correctly record Prometheus metrics:
- Heartbeat execution counts by status
- Heartbeat duration histograms
- Token usage counters
- Workflow duration histograms
- Paperclip API call counts and errors
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from agents.config import SystemConfig, PaperclipConfig, ModelConfig
from agents.heartbeat import run_heartbeat, HeartbeatResult
from agents.metrics import MetricsRegistry, metrics
from agents.paperclip_client import (
    AgentInfo,
    CheckoutResult,
    Comment,
    Issue,
    PaperclipAPIError,
    PaperclipClient,
)


# ── Fixtures ──


@pytest.fixture(autouse=True)
def _paperclip_env(monkeypatch):
    """Ensure PAPERCLIP_AGENT_ID and PAPERCLIP_API_URL are set for all tests."""
    monkeypatch.setenv("PAPERCLIP_AGENT_ID", "agent-1")
    monkeypatch.setenv("PAPERCLIP_API_URL", "http://localhost:3100")


@pytest.fixture(autouse=True)
def reset_metrics():
    """Reset the global metrics registry before each test."""
    metrics._counters.clear()
    metrics._gauges.clear()
    metrics._histograms.clear()
    yield


@pytest.fixture
def config():
    cfg = SystemConfig()
    cfg.paperclip = PaperclipConfig(
        enabled=True,
        api_url="http://localhost:3100",
        api_key="test-key",
        cost_reporting=True,
    )
    cfg.spending.enabled = False
    return cfg


def _setup_client_mock(MockClient, assignments=None, checkout_success=True):
    """Helper to configure a fully-mocked PaperclipClient."""
    client = MockClient.return_value
    client.agent_id = "agent-1"
    client.get_identity.return_value = AgentInfo(
        id="agent-1", company_id="c1", name="Bot", role="eng",
    )
    client.get_assignments.return_value = assignments or []
    client.checkout_issue.return_value = CheckoutResult(success=checkout_success)
    client.get_issue.return_value = Issue(id="i1", title="Task", description="Do it")
    client.get_comments.return_value = []
    return client


# ── Heartbeat Counter Tests ──


class TestHeartbeatCounters:
    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_success_increments_counters(self, MockClient, mock_workflow, config):
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}

        run_heartbeat(config)

        assert metrics.get_counter("vibe_heartbeat_total", {"status": "started"}) == 1
        assert metrics.get_counter("vibe_heartbeat_total", {"status": "success"}) == 1

    @patch("agents.heartbeat.PaperclipClient")
    def test_idle_increments_counter(self, MockClient, config):
        _setup_client_mock(MockClient, assignments=[])

        run_heartbeat(config)

        assert metrics.get_counter("vibe_heartbeat_total", {"status": "started"}) == 1
        assert metrics.get_counter("vibe_heartbeat_total", {"status": "idle"}) == 1

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_failed_increments_counter(self, MockClient, mock_workflow, config):
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        mock_workflow.side_effect = RuntimeError("crash")

        run_heartbeat(config)

        assert metrics.get_counter("vibe_heartbeat_total", {"status": "started"}) == 1
        assert metrics.get_counter("vibe_heartbeat_total", {"status": "failed"}) == 1

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_blocked_increments_counter(self, MockClient, mock_workflow, config):
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        mock_workflow.return_value = {"final_output": "Partial", "final_score": 50}

        run_heartbeat(config)

        assert metrics.get_counter("vibe_heartbeat_total", {"status": "blocked"}) == 1

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_clarification_increments_counter(self, MockClient, mock_workflow, config):
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        mock_workflow.return_value = {
            "clarification_needed": True,
            "clarification_questions": ["Which DB?"],
        }

        run_heartbeat(config)

        assert metrics.get_counter("vibe_heartbeat_total", {"status": "clarification_needed"}) == 1

    @patch("agents.heartbeat.PaperclipClient")
    def test_identity_failure_increments_failed(self, MockClient, config):
        client = MockClient.return_value
        client.get_identity.side_effect = PaperclipAPIError(401, "Unauthorized")

        run_heartbeat(config)

        assert metrics.get_counter("vibe_heartbeat_total", {"status": "failed"}) == 1

    @patch("agents.heartbeat.PaperclipClient")
    def test_connection_failure_increments_failed(self, MockClient, config):
        MockClient.side_effect = ValueError("PAPERCLIP_API_URL not set")

        run_heartbeat(config)

        assert metrics.get_counter("vibe_heartbeat_total", {"status": "failed"}) == 1

    @patch("agents.heartbeat.PaperclipClient")
    def test_checkout_conflict_increments_idle(self, MockClient, config):
        _setup_client_mock(
            MockClient,
            [Issue(id="i1", title="Task", status="todo")],
            checkout_success=False,
        )

        run_heartbeat(config)

        assert metrics.get_counter("vibe_heartbeat_total", {"status": "idle"}) == 1

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_multiple_heartbeats_accumulate(self, MockClient, mock_workflow, config):
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}

        run_heartbeat(config)
        run_heartbeat(config)

        assert metrics.get_counter("vibe_heartbeat_total", {"status": "started"}) == 2
        assert metrics.get_counter("vibe_heartbeat_total", {"status": "success"}) == 2


# ── Duration Histogram Tests ──


class TestHeartbeatDuration:
    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_records_heartbeat_duration(self, MockClient, mock_workflow, config):
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}

        run_heartbeat(config)

        # Should have at least one observation in the histogram
        with metrics._lock:
            key = metrics._labels_key({"status": "success"})
            observations = metrics._histograms["vibe_heartbeat_duration_seconds"][key]
        assert len(observations) == 1
        assert observations[0] >= 0  # Non-negative duration

    @patch("agents.heartbeat.PaperclipClient")
    def test_records_duration_on_idle(self, MockClient, config):
        _setup_client_mock(MockClient, assignments=[])

        run_heartbeat(config)

        with metrics._lock:
            key = metrics._labels_key({"status": "idle"})
            observations = metrics._histograms["vibe_heartbeat_duration_seconds"][key]
        assert len(observations) == 1


# ── Token Usage Tests ──


class TestHeartbeatTokens:
    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_records_token_usage(self, MockClient, mock_workflow, config):
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        mock_workflow.return_value = {
            "final_output": "Done",
            "final_score": 90,
            "total_input_tokens": 1500,
            "total_output_tokens": 300,
        }

        run_heartbeat(config)

        assert metrics.get_counter("vibe_heartbeat_tokens_total", {"direction": "input"}) == 1500
        assert metrics.get_counter("vibe_heartbeat_tokens_total", {"direction": "output"}) == 300

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_no_tokens_when_zero(self, MockClient, mock_workflow, config):
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}

        run_heartbeat(config)

        # Zero tokens should not be recorded (guard in _finish)
        assert metrics.get_counter("vibe_heartbeat_tokens_total", {"direction": "input"}) == 0
        assert metrics.get_counter("vibe_heartbeat_tokens_total", {"direction": "output"}) == 0

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_tokens_accumulate_across_heartbeats(self, MockClient, mock_workflow, config):
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        mock_workflow.return_value = {
            "final_output": "Done",
            "final_score": 90,
            "total_input_tokens": 1000,
            "total_output_tokens": 200,
        }

        run_heartbeat(config)
        run_heartbeat(config)

        assert metrics.get_counter("vibe_heartbeat_tokens_total", {"direction": "input"}) == 2000
        assert metrics.get_counter("vibe_heartbeat_tokens_total", {"direction": "output"}) == 400


# ── Workflow Duration Tests ──


class TestWorkflowDuration:
    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_records_workflow_duration(self, MockClient, mock_workflow, config):
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}

        run_heartbeat(config)

        with metrics._lock:
            key = metrics._labels_key({"task_type": "auto"})
            observations = metrics._histograms["vibe_heartbeat_workflow_duration_seconds"][key]
        assert len(observations) == 1
        assert observations[0] >= 0

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_workflow_duration_uses_task_type_label(self, MockClient, mock_workflow, config):
        config.paperclip.task_type = "security_audit"
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}

        run_heartbeat(config)

        with metrics._lock:
            key = metrics._labels_key({"task_type": "security_audit"})
            observations = metrics._histograms["vibe_heartbeat_workflow_duration_seconds"][key]
        assert len(observations) == 1

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_no_workflow_duration_on_crash(self, MockClient, mock_workflow, config):
        """Workflow duration should not be recorded when workflow crashes."""
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        mock_workflow.side_effect = RuntimeError("crash")

        run_heartbeat(config)

        with metrics._lock:
            # No observations should exist for workflow duration
            assert len(metrics._histograms["vibe_heartbeat_workflow_duration_seconds"]) == 0


# ── Paperclip API Metrics Tests ──


class TestPaperclipAPIMetrics:
    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_records_api_calls(self, MockClient, mock_workflow, config):
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}

        run_heartbeat(config)

        assert metrics.get_counter("vibe_paperclip_api_calls_total", {"endpoint": "get_issue"}) == 1
        assert metrics.get_counter("vibe_paperclip_api_calls_total", {"endpoint": "get_comments"}) == 1
        assert metrics.get_counter("vibe_paperclip_api_calls_total", {"endpoint": "update_issue"}) == 1
        assert metrics.get_counter("vibe_paperclip_api_calls_total", {"endpoint": "report_cost"}) == 1

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_records_api_duration(self, MockClient, mock_workflow, config):
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}

        run_heartbeat(config)

        with metrics._lock:
            # Context fetch duration
            ctx_key = metrics._labels_key({"endpoint": "context"})
            assert len(metrics._histograms["vibe_paperclip_api_duration_seconds"][ctx_key]) == 1
            # update_issue duration
            update_key = metrics._labels_key({"endpoint": "update_issue"})
            assert len(metrics._histograms["vibe_paperclip_api_duration_seconds"][update_key]) == 1

    @patch("agents.heartbeat.PaperclipClient")
    def test_identity_error_records_api_error(self, MockClient, config):
        client = MockClient.return_value
        client.get_identity.side_effect = PaperclipAPIError(401, "Unauthorized")

        run_heartbeat(config)

        assert metrics.get_counter("vibe_paperclip_api_errors_total", {"endpoint": "identity"}) == 1

    @patch("agents.heartbeat.PaperclipClient")
    def test_assignments_error_records_api_error(self, MockClient, config):
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.side_effect = PaperclipAPIError(500, "Server error")

        run_heartbeat(config)

        assert metrics.get_counter("vibe_paperclip_api_errors_total", {"endpoint": "assignments"}) == 1

    @patch("agents.heartbeat.PaperclipClient")
    def test_checkout_error_records_api_error(self, MockClient, config):
        client = MockClient.return_value
        client.agent_id = "agent-1"
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.side_effect = PaperclipAPIError(500, "Checkout failed")

        run_heartbeat(config)

        assert metrics.get_counter("vibe_paperclip_api_errors_total", {"endpoint": "checkout"}) == 1

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_context_error_records_api_error(self, MockClient, mock_workflow, config):
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        client = MockClient.return_value
        client.get_issue.side_effect = PaperclipAPIError(500, "Server error")

        run_heartbeat(config)

        assert metrics.get_counter("vibe_paperclip_api_errors_total", {"endpoint": "context"}) == 1

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_update_issue_error_records_api_error(self, MockClient, mock_workflow, config):
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        client = MockClient.return_value
        client.update_issue.side_effect = PaperclipAPIError(500, "Update failed")
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}

        run_heartbeat(config)

        assert metrics.get_counter("vibe_paperclip_api_errors_total", {"endpoint": "update_issue"}) == 1

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_cost_reporting_error_records_api_error(self, MockClient, mock_workflow, config):
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        client = MockClient.return_value
        client.report_cost.side_effect = PaperclipAPIError(500, "Cost API down")
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}

        run_heartbeat(config)

        assert metrics.get_counter("vibe_paperclip_api_errors_total", {"endpoint": "report_cost"}) == 1

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_workflow_error_increments_workflow_error_counter(self, MockClient, mock_workflow, config):
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        mock_workflow.side_effect = RuntimeError("LLM crashed")

        run_heartbeat(config)

        assert metrics.get_counter("vibe_heartbeat_total", {"status": "workflow_error"}) == 1

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_clarification_update_error_records_api_error(self, MockClient, mock_workflow, config):
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        client = MockClient.return_value
        client.update_issue.side_effect = PaperclipAPIError(500, "Update failed")
        mock_workflow.return_value = {
            "clarification_needed": True,
            "clarification_questions": ["Which DB?"],
        }

        run_heartbeat(config)

        assert metrics.get_counter("vibe_paperclip_api_errors_total", {"endpoint": "update_issue"}) == 1


# ── Metric Registration Tests ──


class TestMetricRegistrations:
    """Verify all heartbeat metrics are properly registered with descriptions."""

    def test_heartbeat_total_registered(self):
        assert "vibe_heartbeat_total" in metrics._help
        assert metrics._types["vibe_heartbeat_total"] == "counter"

    def test_heartbeat_duration_registered(self):
        assert "vibe_heartbeat_duration_seconds" in metrics._help
        assert metrics._types["vibe_heartbeat_duration_seconds"] == "histogram"

    def test_heartbeat_tokens_registered(self):
        assert "vibe_heartbeat_tokens_total" in metrics._help
        assert metrics._types["vibe_heartbeat_tokens_total"] == "counter"

    def test_workflow_duration_registered(self):
        assert "vibe_heartbeat_workflow_duration_seconds" in metrics._help
        assert metrics._types["vibe_heartbeat_workflow_duration_seconds"] == "histogram"

    def test_paperclip_api_calls_registered(self):
        assert "vibe_paperclip_api_calls_total" in metrics._help
        assert metrics._types["vibe_paperclip_api_calls_total"] == "counter"

    def test_paperclip_api_errors_registered(self):
        assert "vibe_paperclip_api_errors_total" in metrics._help
        assert metrics._types["vibe_paperclip_api_errors_total"] == "counter"

    def test_paperclip_api_duration_registered(self):
        assert "vibe_paperclip_api_duration_seconds" in metrics._help
        assert metrics._types["vibe_paperclip_api_duration_seconds"] == "histogram"


# ── Prometheus Export Tests ──


class TestPrometheusExport:
    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_heartbeat_metrics_in_prometheus_output(self, MockClient, mock_workflow, config):
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        mock_workflow.return_value = {
            "final_output": "Done",
            "final_score": 90,
            "total_input_tokens": 500,
            "total_output_tokens": 100,
        }

        run_heartbeat(config)

        output = metrics.format_prometheus()
        assert "vibe_heartbeat_total" in output
        assert "vibe_heartbeat_duration_seconds" in output
        assert "vibe_heartbeat_tokens_total" in output
        assert "vibe_paperclip_api_calls_total" in output

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_heartbeat_metrics_in_json_output(self, MockClient, mock_workflow, config):
        _setup_client_mock(MockClient, [Issue(id="i1", title="Task", status="todo")])
        mock_workflow.return_value = {
            "final_output": "Done",
            "final_score": 90,
            "total_input_tokens": 500,
            "total_output_tokens": 100,
        }

        run_heartbeat(config)

        json_output = metrics.format_json()
        # Should contain heartbeat counters
        found_heartbeat = any("vibe_heartbeat" in k for k in json_output)
        assert found_heartbeat
