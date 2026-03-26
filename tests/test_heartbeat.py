"""
Tests for Paperclip Heartbeat Execution Mode

Tests the full heartbeat procedure: task selection, workflow execution,
result posting, cost reporting, and error handling.
All external calls (Paperclip API, Vibe workflow) are mocked.
"""

import json
import os
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from agents.config import SystemConfig, PaperclipConfig, WorkflowConfig, ModelConfig
from agents.heartbeat import (
    ClarificationRequest,
    HeartbeatResult,
    _build_user_request,
    _create_client,
    _detect_clarification_resume,
    _estimate_cost_cents,
    _execute_checked_out_task,
    _extract_complexity_hint,
    _format_blocked_comment,
    _format_clarification_comment,
    _format_success_comment,
    _install_sigterm_handler,
    _make_progress_callback,
    _rank_tasks,
    _post_cancelled,
    _post_sigterm_partial,
    _PROGRESS_NODES,
    _restore_sigterm_handler,
    _resolve_task_type,
    _run_workflow,
    _extract_usage,
    _post_failure,
    _SigtermReceived,
    _validate_heartbeat_config,
    run_heartbeat,
)
from agents.cancellation import (
    CancellationToken,
    CancellationPoller,
    WorkflowCancelledError,
)
from agents.paperclip_client import (
    AgentInfo,
    CheckoutResult,
    Comment,
    Issue,
    PaperclipAPIError,
    PaperclipClient,
    PaperclipConflictError,
)


# ── Fixtures ──


@pytest.fixture(autouse=True)
def _paperclip_env(monkeypatch):
    """Ensure PAPERCLIP_AGENT_ID and PAPERCLIP_API_URL are set for all tests."""
    monkeypatch.setenv("PAPERCLIP_AGENT_ID", "agent-1")
    monkeypatch.setenv("PAPERCLIP_API_URL", "http://localhost:3100")



@pytest.fixture
def config():
    """Test config with Paperclip enabled."""
    cfg = SystemConfig()
    cfg.paperclip = PaperclipConfig(
        enabled=True,
        api_url="http://localhost:3100",
        api_key="test-key",
        cost_reporting=True,
        output_format="json",
    )
    cfg.spending.enabled = False
    return cfg


@pytest.fixture
def sample_agent():
    return AgentInfo(
        id="agent-1",
        company_id="company-1",
        name="CodeBot",
        role="engineer",
    )


@pytest.fixture
def sample_issue():
    return Issue(
        id="issue-1",
        title="Implement auth module",
        description="Build JWT authentication for the API",
        status="todo",
        priority="high",
        assignee_agent_id="agent-1",
        goal_id="goal-1",
        ancestors=[{"id": "parent-1", "title": "Auth System"}],
    )


@pytest.fixture
def sample_issue_in_progress():
    return Issue(
        id="issue-2",
        title="Fix bug",
        description="Fix login bug",
        status="in_progress",
        priority="medium",
        assignee_agent_id="agent-1",
    )


@pytest.fixture
def sample_comments():
    return [
        Comment(id="c1", body="Please prioritize this", author_user_id="user-1"),
        Comment(id="c2", body="Started investigation", author_agent_id="agent-1"),
    ]


# ── HeartbeatResult Tests ──


class TestHeartbeatResult:
    def test_to_json(self):
        result = HeartbeatResult(
            status="success",
            issue_id="issue-1",
            summary="Done",
            usage={"input_tokens": 100, "output_tokens": 50},
        )
        data = json.loads(result.to_json())
        assert data["status"] == "success"
        assert data["issue_id"] == "issue-1"
        assert data["usage"]["input_tokens"] == 100

    def test_idle_result(self):
        result = HeartbeatResult(status="idle", summary="No tasks")
        assert result.exit_code == 0


# ── _rank_tasks Tests ──


class TestPickTask:
    def test_prefers_in_progress(self, sample_issue, sample_issue_in_progress):
        result = _rank_tasks([sample_issue, sample_issue_in_progress])
        assert result[0].id == "issue-2"  # in_progress first

    def test_picks_todo_when_no_in_progress(self, sample_issue):
        result = _rank_tasks([sample_issue])
        assert result[0].id == "issue-1"

    def test_respects_paperclip_task_id(self, sample_issue, sample_issue_in_progress, monkeypatch):
        monkeypatch.setenv("PAPERCLIP_TASK_ID", "issue-1")
        result = _rank_tasks([sample_issue, sample_issue_in_progress])
        assert result[0].id == "issue-1"  # forced by env

    def test_returns_empty_when_empty(self):
        result = _rank_tasks([])
        assert result == []

    def test_skips_blocked_by_default(self):
        blocked = Issue(id="b1", title="Blocked", status="blocked")
        result = _rank_tasks([blocked])
        assert result == []

    def test_picks_blocked_when_woken_for_it(self, monkeypatch):
        monkeypatch.setenv("PAPERCLIP_TASK_ID", "b1")
        monkeypatch.setenv("PAPERCLIP_WAKE_REASON", "issue_comment_mentioned")
        blocked = Issue(id="b1", title="Blocked", status="blocked")
        result = _rank_tasks([blocked])
        assert result[0].id == "b1"

    def test_forced_task_id_not_in_assignments(self, sample_issue, monkeypatch):
        monkeypatch.setenv("PAPERCLIP_TASK_ID", "nonexistent")
        result = _rank_tasks([sample_issue])
        # Falls through to normal priority: picks the todo
        assert result[0].id == "issue-1"


# ── _resolve_task_type Tests ──


class TestResolveTaskType:
    def test_from_config(self, config):
        config.paperclip.task_type = "security_audit"
        assert _resolve_task_type(config) == "security_audit"

    def test_from_env(self, config, monkeypatch):
        config.paperclip.task_type = ""
        monkeypatch.setenv("VIBE_TASK_TYPE", "code")
        assert _resolve_task_type(config) == "code"

    def test_empty_when_unset(self, config, monkeypatch):
        config.paperclip.task_type = ""
        monkeypatch.delenv("VIBE_TASK_TYPE", raising=False)
        assert _resolve_task_type(config) == ""

    def test_config_overrides_env(self, config, monkeypatch):
        config.paperclip.task_type = "test_generation"
        monkeypatch.setenv("VIBE_TASK_TYPE", "code")
        assert _resolve_task_type(config) == "test_generation"


# ── _build_user_request Tests ──


class TestBuildUserRequest:
    def test_basic_request(self, sample_issue, sample_comments):
        request = _build_user_request(sample_issue, sample_comments)
        assert "Implement auth module" in request
        assert "Build JWT authentication" in request

    def test_includes_ancestor_chain(self, sample_issue, sample_comments):
        request = _build_user_request(sample_issue, sample_comments)
        assert "Auth System" in request
        assert "Goal chain:" in request

    def test_includes_comments(self, sample_issue, sample_comments):
        request = _build_user_request(sample_issue, sample_comments)
        assert "Please prioritize this" in request
        assert "Started investigation" in request

    def test_no_ancestors(self, sample_comments):
        issue = Issue(id="i1", title="Simple task", description="Do thing")
        request = _build_user_request(issue, sample_comments)
        assert "Goal chain:" not in request
        assert "Simple task" in request

    def test_no_comments(self, sample_issue):
        request = _build_user_request(sample_issue, [])
        assert "Recent discussion" not in request

    def test_truncates_long_comments(self, sample_issue):
        long_comment = Comment(id="c1", body="x" * 500, author_agent_id="a1")
        request = _build_user_request(sample_issue, [long_comment])
        # Comment should be truncated to 200 chars
        assert len(request) < len("x" * 500) + 200


# ── _extract_usage Tests ──


class TestExtractUsage:
    def test_extracts_tokens(self):
        state = {"total_input_tokens": 1500, "total_output_tokens": 300}
        usage = _extract_usage(state)
        assert usage["input_tokens"] == 1500
        assert usage["output_tokens"] == 300

    def test_defaults_to_zero(self):
        usage = _extract_usage({})
        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0


# ── Comment Formatting Tests ──


class TestFormatComments:
    def test_success_comment(self):
        comment = _format_success_comment("Here is the code", 92)
        assert "Completed" in comment
        assert "92/100" in comment
        assert "Here is the code" in comment

    def test_blocked_comment(self):
        comment = _format_blocked_comment("Partial output", 60, 85)
        assert "Blocked" in comment
        assert "60/100" in comment
        assert "threshold: 85" in comment
        assert "Partial output" in comment

    def test_success_truncates_long_output(self):
        comment = _format_success_comment("x" * 5000, 90)
        assert len(comment) < 4000

    def test_blocked_truncates_long_output(self):
        comment = _format_blocked_comment("x" * 5000, 60, 85)
        assert len(comment) < 3000


# ── _create_client Tests ──


class TestCreateClient:
    def test_uses_config_overrides(self, config, monkeypatch):
        monkeypatch.setenv("PAPERCLIP_API_URL", "http://default:3100")
        monkeypatch.setenv("PAPERCLIP_API_KEY", "default-key")
        monkeypatch.setenv("PAPERCLIP_AGENT_ID", "agent-1")
        client = _create_client(config)
        assert client.api_url == "http://localhost:3100"
        assert client.api_key == "test-key"

    def test_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("PAPERCLIP_API_URL", "http://env:3100")
        monkeypatch.setenv("PAPERCLIP_API_KEY", "env-key")
        monkeypatch.setenv("PAPERCLIP_AGENT_ID", "agent-1")
        cfg = SystemConfig()
        client = _create_client(cfg)
        assert client.api_url == "http://env:3100"

    def test_raises_when_no_url(self, monkeypatch):
        monkeypatch.delenv("PAPERCLIP_API_URL", raising=False)
        cfg = SystemConfig()
        with pytest.raises(ValueError, match="PAPERCLIP_API_URL"):
            _create_client(cfg)

    def test_raises_when_no_agent_id(self, monkeypatch):
        """PAPERCLIP_AGENT_ID must be set for self-comment filtering."""
        monkeypatch.setenv("PAPERCLIP_API_URL", "http://localhost:3100")
        monkeypatch.setenv("PAPERCLIP_API_KEY", "test-key")
        monkeypatch.delenv("PAPERCLIP_AGENT_ID", raising=False)
        cfg = SystemConfig()
        with pytest.raises(ValueError, match="PAPERCLIP_AGENT_ID"):
            _create_client(cfg)

    def test_succeeds_with_agent_id(self, monkeypatch):
        """Should not raise when PAPERCLIP_AGENT_ID is set."""
        monkeypatch.setenv("PAPERCLIP_API_URL", "http://localhost:3100")
        monkeypatch.setenv("PAPERCLIP_API_KEY", "test-key")
        monkeypatch.setenv("PAPERCLIP_AGENT_ID", "agent-123")
        cfg = SystemConfig()
        client = _create_client(cfg)
        assert client.agent_id == "agent-123"


# ── _post_failure Tests ──


class TestPostFailure:
    def test_posts_blocked_with_error(self):
        client = MagicMock(spec=PaperclipClient)
        _post_failure(client, "issue-1", "Something went wrong")
        client.update_issue.assert_called_once()
        call_args = client.update_issue.call_args
        assert call_args[0][0] == "issue-1"
        assert call_args[1]["status"] == "blocked"
        assert "Something went wrong" in call_args[1]["comment"]

    def test_handles_api_error_gracefully(self):
        client = MagicMock(spec=PaperclipClient)
        client.update_issue.side_effect = PaperclipAPIError(500, "Server error")
        # Should not raise
        _post_failure(client, "issue-1", "Error")


# ── Server Readiness Probe Tests ──


class TestWaitForServer:
    @patch("agents.heartbeat.PaperclipClient")
    def test_returns_true_immediately_when_healthy(self, MockClient, config):
        MockClient.return_value.health_check.return_value = True
        from agents.heartbeat import _wait_for_server
        assert _wait_for_server(config, max_wait=5) is True
        MockClient.return_value.health_check.assert_called_once()

    @patch("agents.heartbeat.time.sleep")
    @patch("agents.heartbeat.PaperclipClient")
    def test_retries_then_succeeds(self, MockClient, mock_sleep, config):
        MockClient.return_value.health_check.side_effect = [False, False, True]
        from agents.heartbeat import _wait_for_server
        assert _wait_for_server(config, max_wait=60, initial_delay=0.1) is True
        assert MockClient.return_value.health_check.call_count == 3

    @patch("agents.heartbeat.time.sleep")
    @patch("agents.heartbeat.time.monotonic")
    @patch("agents.heartbeat.PaperclipClient")
    def test_returns_false_after_timeout(self, MockClient, mock_mono, mock_sleep, config):
        MockClient.return_value.health_check.return_value = False
        # Simulate time progressing past deadline
        mock_mono.side_effect = [0.0, 0.0, 1.0, 3.0, 7.0, 15.0, 31.0, 121.0]
        from agents.heartbeat import _wait_for_server
        assert _wait_for_server(config, max_wait=120, initial_delay=1.0) is False

    @patch("agents.heartbeat.PaperclipClient")
    def test_skips_probe_when_api_url_missing(self, MockClient, config):
        MockClient.side_effect = ValueError("PAPERCLIP_API_URL not set")
        from agents.heartbeat import _wait_for_server
        # When client can't be created, returns True to let run_heartbeat
        # report the real validation error
        assert _wait_for_server(config) is True

    @patch("agents.heartbeat._wait_for_server")
    @patch("agents.heartbeat.PaperclipClient")
    def test_run_heartbeat_fails_gracefully_on_probe_timeout(self, MockClient, mock_wait, config):
        mock_wait.return_value = False
        result = run_heartbeat(config)
        assert result.status == "failed"
        assert result.exit_code == 0  # exit 0 — not a crash
        assert "not reachable" in result.summary

    @patch("agents.heartbeat._wait_for_server")
    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_run_heartbeat_proceeds_after_probe_success(self, MockClient, mock_workflow, mock_wait, config):
        mock_wait.return_value = True
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = []  # idle
        result = run_heartbeat(config)
        assert result.status == "idle"


# ── Full Heartbeat Integration Tests ──


class TestRunHeartbeat:
    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_successful_heartbeat(self, MockClient, mock_workflow, config):
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(
            id="i1", title="Task", description="Do something",
        )
        client.get_comments.return_value = []

        mock_workflow.return_value = {
            "final_output": "Done successfully",
            "final_score": 90,
            "critic_score": 90,
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
        }

        result = run_heartbeat(config)
        assert result.status == "success"
        assert result.issue_id == "i1"
        assert result.exit_code == 0
        # 2 calls: in_progress + result posting
        assert client.update_issue.call_count == 2
        client.report_cost.assert_called_once()

    @patch("agents.heartbeat.PaperclipClient")
    def test_no_assignments_returns_idle(self, MockClient, config):
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = []

        result = run_heartbeat(config)
        assert result.status == "idle"
        assert result.exit_code == 0

    @patch("agents.heartbeat.PaperclipClient")
    def test_checkout_conflict_returns_idle(self, MockClient, config):
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=False, conflict_owner="other")

        result = run_heartbeat(config)
        assert result.status == "idle"
        # conflict causes fallthrough to next task; with no more tasks, returns generic idle
        assert result.summary is not None

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_workflow_failure_posts_blocked(self, MockClient, mock_workflow, config):
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(id="i1", title="Task")
        client.get_comments.return_value = []

        mock_workflow.side_effect = RuntimeError("LLM backend crashed")

        result = run_heartbeat(config)
        assert result.status == "failed"
        assert result.exit_code == 1
        # Should have posted failure comment (in_progress + blocked)
        assert client.update_issue.call_count == 2
        # Last call should set blocked
        call_args = client.update_issue.call_args
        assert call_args[1]["status"] == "blocked"

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_low_score_posts_blocked(self, MockClient, mock_workflow, config):
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(id="i1", title="Task")
        client.get_comments.return_value = []

        mock_workflow.return_value = {
            "final_output": "Partial work",
            "final_score": 50,
            "critic_score": 50,
        }

        result = run_heartbeat(config)
        assert result.status == "blocked"
        assert result.exit_code == 1
        # 2 calls: in_progress + blocked result
        assert client.update_issue.call_count == 2
        call_args = client.update_issue.call_args
        assert call_args[1]["status"] == "blocked"

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_cost_reporting_disabled(self, MockClient, mock_workflow, config):
        config.paperclip.cost_reporting = False
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(id="i1", title="Task")
        client.get_comments.return_value = []
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}

        run_heartbeat(config)
        client.report_cost.assert_not_called()

    @patch("agents.heartbeat.PaperclipClient")
    def test_identity_failure(self, MockClient, config):
        client = MockClient.return_value
        client.get_identity.side_effect = PaperclipAPIError(401, "Unauthorized")

        result = run_heartbeat(config)
        assert result.status == "failed"
        assert result.exit_code == 1

    @patch("agents.heartbeat.PaperclipClient")
    def test_connection_failure(self, MockClient, config):
        MockClient.side_effect = ValueError("PAPERCLIP_API_URL not set")

        result = run_heartbeat(config)
        assert result.status == "failed"
        assert result.exit_code == 1

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_cost_reporting_failure_non_fatal(self, MockClient, mock_workflow, config):
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(id="i1", title="Task")
        client.get_comments.return_value = []
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}
        client.report_cost.side_effect = PaperclipAPIError(500, "Cost API down")

        # Should succeed despite cost reporting failure
        result = run_heartbeat(config)
        assert result.status == "success"

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_result_posting_failure_non_fatal(self, MockClient, mock_workflow, config):
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(id="i1", title="Task")
        client.get_comments.return_value = []
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}
        client.update_issue.side_effect = PaperclipAPIError(500, "API error")

        # Should still return success status
        result = run_heartbeat(config)
        assert result.status == "success"


# ── ClarificationRequest Tests ──


class TestClarificationRequest:
    def test_to_dict(self):
        req = ClarificationRequest(
            questions=["Which DB?", "What auth method?"],
            blocking_node="vibe",
            context_summary="Building auth module",
        )
        d = req.to_dict()
        assert d["questions"] == ["Which DB?", "What auth method?"]
        assert d["blocking_node"] == "vibe"
        assert d["context_summary"] == "Building auth module"

    def test_empty_questions(self):
        req = ClarificationRequest(questions=[])
        d = req.to_dict()
        assert d["questions"] == []
        assert d["blocking_node"] == ""


class TestHeartbeatResultClarification:
    def test_clarification_field_serialized(self):
        result = HeartbeatResult(
            status="clarification_needed",
            issue_id="i1",
            clarification={
                "questions": ["PostgreSQL or SQLite?"],
                "blocking_node": "vibe",
                "context_summary": "DB choice",
            },
        )
        data = json.loads(result.to_json())
        assert data["status"] == "clarification_needed"
        assert data["clarification"]["questions"] == ["PostgreSQL or SQLite?"]

    def test_clarification_none_by_default(self):
        result = HeartbeatResult(status="success")
        data = json.loads(result.to_json())
        assert data["clarification"] is None


# ── _detect_clarification_resume Tests ──


class TestDetectClarificationResume:
    def test_detects_human_reply(self, monkeypatch):
        monkeypatch.setenv("PAPERCLIP_WAKE_REASON", "issue_comment_mentioned")
        monkeypatch.setenv("PAPERCLIP_WAKE_COMMENT_ID", "c-reply")

        issue = Issue(id="i1", title="Task", status="blocked")
        comments = [
            Comment(id="c-old", body="Agent question", author_agent_id="agent-1"),
            Comment(id="c-reply", body="Use PostgreSQL", author_user_id="user-1"),
        ]

        reply = _detect_clarification_resume(issue, comments, "agent-1")
        assert reply == "Use PostgreSQL"

    def test_ignores_self_comment(self, monkeypatch):
        monkeypatch.setenv("PAPERCLIP_WAKE_REASON", "issue_comment_mentioned")
        monkeypatch.setenv("PAPERCLIP_WAKE_COMMENT_ID", "c-self")

        issue = Issue(id="i1", title="Task", status="blocked")
        comments = [
            Comment(id="c-self", body="My own comment", author_agent_id="agent-1"),
        ]

        reply = _detect_clarification_resume(issue, comments, "agent-1")
        assert reply is None

    def test_returns_none_without_wake_reason(self, monkeypatch):
        monkeypatch.delenv("PAPERCLIP_WAKE_REASON", raising=False)
        monkeypatch.delenv("PAPERCLIP_WAKE_COMMENT_ID", raising=False)

        issue = Issue(id="i1", title="Task", status="blocked")
        reply = _detect_clarification_resume(issue, [], "agent-1")
        assert reply is None

    def test_returns_none_for_non_blocked_issue(self, monkeypatch):
        monkeypatch.setenv("PAPERCLIP_WAKE_REASON", "issue_comment_mentioned")
        monkeypatch.setenv("PAPERCLIP_WAKE_COMMENT_ID", "c1")

        issue = Issue(id="i1", title="Task", status="todo")
        comments = [Comment(id="c1", body="Hello", author_user_id="u1")]

        reply = _detect_clarification_resume(issue, comments, "agent-1")
        assert reply is None

    def test_returns_none_when_comment_not_found(self, monkeypatch):
        monkeypatch.setenv("PAPERCLIP_WAKE_REASON", "issue_comment_mentioned")
        monkeypatch.setenv("PAPERCLIP_WAKE_COMMENT_ID", "nonexistent")

        issue = Issue(id="i1", title="Task", status="blocked")
        reply = _detect_clarification_resume(issue, [], "agent-1")
        assert reply is None

    def test_returns_none_for_wrong_wake_reason(self, monkeypatch):
        monkeypatch.setenv("PAPERCLIP_WAKE_REASON", "schedule")
        monkeypatch.setenv("PAPERCLIP_WAKE_COMMENT_ID", "c1")

        issue = Issue(id="i1", title="Task", status="blocked")
        comments = [Comment(id="c1", body="Hello", author_user_id="u1")]

        reply = _detect_clarification_resume(issue, comments, "agent-1")
        assert reply is None

    def test_empty_agent_id_treats_all_as_human(self, monkeypatch):
        """When agent_id is empty, self-filtering can't work.
        All comments are treated as human replies (safe fallback)."""
        monkeypatch.setenv("PAPERCLIP_WAKE_REASON", "issue_comment_mentioned")
        monkeypatch.setenv("PAPERCLIP_WAKE_COMMENT_ID", "c1")

        issue = Issue(id="i1", title="Task", status="blocked")
        # Comment from an agent, but our agent_id is empty
        comments = [Comment(id="c1", body="Agent reply", author_agent_id="other-agent")]

        reply = _detect_clarification_resume(issue, comments, "")
        # With empty agent_id, comparison "" == "other-agent" is False,
        # so this is treated as a human reply (safe — won't match self)
        assert reply == "Agent reply"

    def test_whitespace_only_reply_returns_none(self, monkeypatch):
        """Whitespace-only comments should not be injected as clarification."""
        monkeypatch.setenv("PAPERCLIP_WAKE_REASON", "issue_comment_mentioned")
        monkeypatch.setenv("PAPERCLIP_WAKE_COMMENT_ID", "c1")

        issue = Issue(id="i1", title="Task", status="blocked")
        comments = [Comment(id="c1", body="   \n  ", author_user_id="u1")]

        reply = _detect_clarification_resume(issue, comments, "agent-1")
        assert reply is None

    def test_empty_body_returns_none(self, monkeypatch):
        """Empty comment body should not be injected."""
        monkeypatch.setenv("PAPERCLIP_WAKE_REASON", "issue_comment_mentioned")
        monkeypatch.setenv("PAPERCLIP_WAKE_COMMENT_ID", "c1")

        issue = Issue(id="i1", title="Task", status="blocked")
        comments = [Comment(id="c1", body="", author_user_id="u1")]

        reply = _detect_clarification_resume(issue, comments, "agent-1")
        assert reply is None

    def test_strips_whitespace_from_reply(self, monkeypatch):
        """Reply should be stripped of leading/trailing whitespace."""
        monkeypatch.setenv("PAPERCLIP_WAKE_REASON", "issue_comment_mentioned")
        monkeypatch.setenv("PAPERCLIP_WAKE_COMMENT_ID", "c1")

        issue = Issue(id="i1", title="Task", status="blocked")
        comments = [Comment(id="c1", body="  Use PostgreSQL  \n", author_user_id="u1")]

        reply = _detect_clarification_resume(issue, comments, "agent-1")
        assert reply == "Use PostgreSQL"


# ── _build_user_request with Clarification ──


class TestBuildUserRequestClarification:
    def test_injects_clarification_reply(self, sample_issue):
        request = _build_user_request(
            sample_issue, [], clarification_reply="Use PostgreSQL",
        )
        assert "[Clarification from human]: Use PostgreSQL" in request

    def test_clarification_before_discussion(self, sample_issue, sample_comments):
        request = _build_user_request(
            sample_issue, sample_comments, clarification_reply="Use JWT",
        )
        clarif_pos = request.index("[Clarification from human]")
        discussion_pos = request.index("Recent discussion")
        assert clarif_pos < discussion_pos

    def test_no_clarification_by_default(self, sample_issue, sample_comments):
        request = _build_user_request(sample_issue, sample_comments)
        assert "[Clarification from human]" not in request


# ── _format_clarification_comment Tests ──


class TestFormatClarificationComment:
    def test_formats_questions(self):
        comment = _format_clarification_comment(
            ["Which database?", "What auth method?"]
        )
        assert "Clarification Needed" in comment
        assert "1. Which database?" in comment
        assert "2. What auth method?" in comment
        assert "reply" in comment.lower()

    def test_single_question(self):
        comment = _format_clarification_comment(["PostgreSQL or SQLite?"])
        assert "1. PostgreSQL or SQLite?" in comment


# ── Full Heartbeat Clarification Integration Tests ──


class TestRunHeartbeatClarification:
    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_clarification_needed_posts_structured_comment(
        self, MockClient, mock_workflow, config,
    ):
        client = MockClient.return_value
        client.agent_id = "agent-1"
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(id="i1", title="Task")
        client.get_comments.return_value = []

        mock_workflow.return_value = {
            "clarification_needed": True,
            "clarification_questions": ["Which DB engine?", "REST or GraphQL?"],
            "specification": "Build an API with a database backend",
            "last_node": "vibe",
        }

        result = run_heartbeat(config)
        assert result.status == "clarification_needed"
        assert result.exit_code == 0
        assert result.clarification is not None
        assert len(result.clarification["questions"]) == 2
        assert result.clarification["blocking_node"] == "vibe"

        # Should have posted structured comment + set blocked (2 calls: in_progress + blocked)
        assert client.update_issue.call_count == 2
        # Last call is the clarification posting
        call_args = client.update_issue.call_args
        assert call_args[1]["status"] == "blocked"
        assert "Clarification Needed" in call_args[1]["comment"]
        assert "Which DB engine?" in call_args[1]["comment"]

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_clarification_needed_but_no_questions_falls_through(
        self, MockClient, mock_workflow, config,
    ):
        """When clarification_needed=True but questions=[], treat as normal result."""
        client = MockClient.return_value
        client.agent_id = "agent-1"
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(id="i1", title="Task")
        client.get_comments.return_value = []

        mock_workflow.return_value = {
            "clarification_needed": True,
            "clarification_questions": [],
            "final_output": "Partial output",
            "final_score": 50,
        }

        result = run_heartbeat(config)
        # Falls through to normal score-based blocking
        assert result.status == "blocked"

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat._detect_clarification_resume")
    @patch("agents.heartbeat.PaperclipClient")
    def test_resume_from_clarification(
        self, MockClient, mock_detect, mock_workflow, config, monkeypatch,
    ):
        """When human replies to clarification, the reply is injected into context."""
        # Set wake env vars so _pick_task selects the blocked issue
        monkeypatch.setenv("PAPERCLIP_TASK_ID", "i1")
        monkeypatch.setenv("PAPERCLIP_WAKE_REASON", "issue_comment_mentioned")
        monkeypatch.setenv("PAPERCLIP_WAKE_COMMENT_ID", "c1")

        client = MockClient.return_value
        client.agent_id = "agent-1"
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="blocked"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(
            id="i1", title="Task", status="blocked",
        )
        client.get_comments.return_value = [
            Comment(id="c1", body="Use PostgreSQL please", author_user_id="user-1"),
        ]

        mock_detect.return_value = "Use PostgreSQL please"

        mock_workflow.return_value = {
            "final_output": "Built with PostgreSQL",
            "final_score": 92,
        }

        result = run_heartbeat(config)
        assert result.status == "success"

        # Verify the workflow received the clarification in context
        workflow_call_args = mock_workflow.call_args
        user_request = workflow_call_args[0][1]  # second positional arg
        assert "[Clarification from human]: Use PostgreSQL please" in user_request

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_uses_full_issue_status_not_stale_assignment(
        self, MockClient, mock_workflow, config, monkeypatch,
    ):
        """The detection should use full_issue (fresh) not issue (stale from assignments).

        Scenario: assignments returns status='todo' but get_issue returns status='blocked'
        (status changed between the two API calls). Detection should see 'blocked'.
        """
        monkeypatch.setenv("PAPERCLIP_TASK_ID", "i1")
        monkeypatch.setenv("PAPERCLIP_WAKE_REASON", "issue_comment_mentioned")
        monkeypatch.setenv("PAPERCLIP_WAKE_COMMENT_ID", "c1")

        client = MockClient.return_value
        client.agent_id = "agent-1"
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        # Assignment says "blocked" (so _pick_task selects it)
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="blocked"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        # Fresh fetch also says "blocked" — detection should work
        client.get_issue.return_value = Issue(
            id="i1", title="Task", status="blocked",
        )
        client.get_comments.return_value = [
            Comment(id="c1", body="Use Redis", author_user_id="user-1"),
        ]

        mock_workflow.return_value = {
            "final_output": "Built with Redis",
            "final_score": 95,
        }

        result = run_heartbeat(config)
        assert result.status == "success"
        # The clarification should have been detected and injected
        user_request = mock_workflow.call_args[0][1]
        assert "[Clarification from human]: Use Redis" in user_request


# ── Adapter Contract Tests ──
# These verify the HeartbeatResult JSON contains the fields the adapter expects
# for Slack notification (issue_id, clarification with questions).


class TestAdapterSlackContract:
    def test_clarification_result_has_issue_id_for_slack(self):
        """Adapter needs issue_id to build the Slack notification link."""
        result = HeartbeatResult(
            status="clarification_needed",
            issue_id="GEN-42",
            clarification={
                "questions": ["PostgreSQL or SQLite?"],
                "blocking_node": "vibe",
                "context_summary": "DB choice",
            },
        )
        data = json.loads(result.to_json())
        # Adapter reads these to build Slack DM
        assert data["issue_id"] == "GEN-42"
        assert isinstance(data["clarification"]["questions"], list)
        assert len(data["clarification"]["questions"]) > 0

    def test_non_clarification_result_has_no_slack_trigger(self):
        """Adapter should not send Slack DMs for success/blocked/idle."""
        for status in ("success", "blocked", "idle", "failed"):
            result = HeartbeatResult(status=status, issue_id="GEN-1")
            data = json.loads(result.to_json())
            assert data["clarification"] is None


# ── Checkout Release Tests ──


class TestCheckoutRelease:
    """Tests for Fix: checkout lock is always released via finally block."""

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_checkout_released_on_success(self, MockClient, mock_workflow, config):
        """Checkout must be released even on successful completion."""
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(id="i1", title="Task")
        client.get_comments.return_value = []
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}

        result = run_heartbeat(config)
        assert result.status == "success"
        client.release_issue.assert_called_once_with("i1")

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_checkout_released_on_workflow_crash(self, MockClient, mock_workflow, config):
        """Checkout must be released when workflow raises an exception."""
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(id="i1", title="Task")
        client.get_comments.return_value = []
        mock_workflow.side_effect = RuntimeError("LLM backend crashed")

        result = run_heartbeat(config)
        assert result.status == "failed"
        client.release_issue.assert_called_once_with("i1")

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_checkout_released_on_low_score(self, MockClient, mock_workflow, config):
        """Checkout must be released when output is blocked by quality gate."""
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(id="i1", title="Task")
        client.get_comments.return_value = []
        mock_workflow.return_value = {"final_output": "Partial", "final_score": 50}

        result = run_heartbeat(config)
        assert result.status == "blocked"
        client.release_issue.assert_called_once_with("i1")

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_checkout_released_on_clarification(self, MockClient, mock_workflow, config):
        """Checkout must be released when agent blocks for clarification."""
        client = MockClient.return_value
        client.agent_id = "agent-1"
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(id="i1", title="Task")
        client.get_comments.return_value = []
        mock_workflow.return_value = {
            "clarification_needed": True,
            "clarification_questions": ["Which DB?"],
        }

        result = run_heartbeat(config)
        assert result.status == "clarification_needed"
        client.release_issue.assert_called_once_with("i1")

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_checkout_released_on_context_fetch_failure(self, MockClient, mock_workflow, config):
        """Checkout must be released when fetching issue context fails."""
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.side_effect = PaperclipAPIError(500, "Server error")

        result = run_heartbeat(config)
        assert result.status == "failed"
        client.release_issue.assert_called_once_with("i1")

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_release_failure_does_not_mask_result(self, MockClient, mock_workflow, config):
        """If release_issue fails, the original result should still be returned."""
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(id="i1", title="Task")
        client.get_comments.return_value = []
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}
        client.release_issue.side_effect = PaperclipAPIError(500, "Release failed")

        # Should still return success despite release failure
        result = run_heartbeat(config)
        assert result.status == "success"
        client.release_issue.assert_called_once_with("i1")

    @patch("agents.heartbeat.PaperclipClient")
    def test_no_release_on_checkout_conflict(self, MockClient, config):
        """Should NOT call release_issue when checkout itself failed (no lock held)."""
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=False, conflict_owner="other")

        result = run_heartbeat(config)
        assert result.status == "idle"
        client.release_issue.assert_not_called()


# ── _extract_complexity_hint Tests ──


class TestExtractComplexityHint:
    def test_extracts_fast_tier(self):
        desc = "Some description\n<!-- complexity:fast -->\nMore text"
        assert _extract_complexity_hint(desc) == "fast"

    def test_extracts_standard_tier(self):
        desc = "<!-- complexity:standard -->"
        assert _extract_complexity_hint(desc) == "standard"

    def test_extracts_full_tier(self):
        desc = "Task details <!-- complexity:full --> end"
        assert _extract_complexity_hint(desc) == "full"

    def test_returns_empty_when_no_hint(self):
        assert _extract_complexity_hint("No hint here") == ""

    def test_returns_empty_for_empty_description(self):
        assert _extract_complexity_hint("") == ""

    def test_returns_empty_for_none_description(self):
        assert _extract_complexity_hint(None) == ""

    def test_ignores_malformed_hint(self):
        assert _extract_complexity_hint("<!-- complexity: -->") == ""

    def test_handles_multiline_description(self):
        desc = "Line 1\nLine 2\n<!-- complexity:fast -->\nLine 4"
        assert _extract_complexity_hint(desc) == "fast"


# ── Complexity Tier Workflow Integration Tests ──


class TestComplexityTierWorkflow:
    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_complexity_tier_passed_to_workflow(self, MockClient, mock_workflow, config):
        """When orchestrator embeds complexity hint, it should reach _run_workflow."""
        client = MockClient.return_value
        client.agent_id = "agent-1"
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(
            id="i1", title="Task",
            description="Build it <!-- complexity:fast -->",
        )
        client.get_comments.return_value = []
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}

        run_heartbeat(config)

        # _run_workflow should have been called with complexity_tier="fast"
        call_kwargs = mock_workflow.call_args
        assert call_kwargs[1].get("complexity_tier") == "fast" or \
            (len(call_kwargs[0]) >= 4 and call_kwargs[0][3] == "fast")

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_no_complexity_hint_passes_empty(self, MockClient, mock_workflow, config):
        """Without complexity hint, complexity_tier should be empty string."""
        client = MockClient.return_value
        client.agent_id = "agent-1"
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(
            id="i1", title="Task", description="Plain description",
        )
        client.get_comments.return_value = []
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}

        run_heartbeat(config)

        call_kwargs = mock_workflow.call_args
        assert call_kwargs[1].get("complexity_tier") == "" or \
            (len(call_kwargs[0]) >= 4 and call_kwargs[0][3] == "")


# ── Orchestrator Branch Tests ──


class TestOrchestratorBranch:
    @patch("agents.orchestrator.run_orchestrator_heartbeat")
    @patch("agents.heartbeat.PaperclipClient")
    def test_orchestrator_task_type_routes_to_orchestrator(
        self, MockClient, mock_orch, config, monkeypatch,
    ):
        """When task_type=orchestrator, should call run_orchestrator_heartbeat."""
        monkeypatch.setenv("VIBE_TASK_TYPE", "orchestrator")
        config.paperclip.task_type = "orchestrator"

        client = MockClient.return_value
        client.agent_id = "agent-1"
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(
            id="i1", title="Task", status="todo",
        )
        client.get_comments.return_value = []

        mock_orch.return_value = HeartbeatResult(
            status="success", issue_id="i1", summary="Orchestrated",
        )

        result = run_heartbeat(config)
        mock_orch.assert_called_once()
        assert result.status == "success"

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_non_orchestrator_routes_to_workflow(
        self, MockClient, mock_workflow, config, monkeypatch,
    ):
        """When task_type != orchestrator, should call _run_workflow."""
        monkeypatch.setenv("VIBE_TASK_TYPE", "code_generation")
        config.paperclip.task_type = "code_generation"

        client = MockClient.return_value
        client.agent_id = "agent-1"
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(
            id="i1", title="Task", status="todo",
        )
        client.get_comments.return_value = []
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}

        result = run_heartbeat(config)
        mock_workflow.assert_called_once()
        assert result.status == "success"


# ── Multi-Round Clarification Tests ──


class TestMultiRoundClarification:
    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_clarification_then_resume_then_success(
        self, MockClient, mock_workflow, config, monkeypatch,
    ):
        """Simulates: first run blocks for clarification, second run resumes with reply."""
        client = MockClient.return_value
        client.agent_id = "agent-1"
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )

        # ── First invocation: clarification needed ──
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(
            id="i1", title="Task", status="todo",
        )
        client.get_comments.return_value = []
        mock_workflow.return_value = {
            "clarification_needed": True,
            "clarification_questions": ["PostgreSQL or SQLite?"],
            "last_node": "vibe",
            "specification": "DB choice needed",
        }

        result1 = run_heartbeat(config)
        assert result1.status == "clarification_needed"

        # ── Second invocation: resume with human reply ──
        monkeypatch.setenv("PAPERCLIP_TASK_ID", "i1")
        monkeypatch.setenv("PAPERCLIP_WAKE_REASON", "issue_comment_mentioned")
        monkeypatch.setenv("PAPERCLIP_WAKE_COMMENT_ID", "c-reply")

        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="blocked"),
        ]
        client.get_issue.return_value = Issue(
            id="i1", title="Task", status="blocked",
        )
        client.get_comments.return_value = [
            Comment(id="c-agent", body="## Clarification Needed", author_agent_id="agent-1"),
            Comment(id="c-reply", body="Use PostgreSQL", author_user_id="user-1"),
        ]

        mock_workflow.return_value = {
            "final_output": "Built with PostgreSQL",
            "final_score": 92,
        }

        result2 = run_heartbeat(config)
        assert result2.status == "success"

        # Verify human reply was injected
        user_request = mock_workflow.call_args[0][1]
        assert "[Clarification from human]: Use PostgreSQL" in user_request


# ── Metrics Tests ──


class TestHeartbeatMetrics:
    @patch("agents.heartbeat.metrics")
    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_records_started_metric(self, MockClient, mock_workflow, mock_metrics, config):
        """Heartbeat should record 'started' metric at the beginning."""
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = []

        run_heartbeat(config)

        # Check that 'started' was recorded
        increment_calls = [
            c for c in mock_metrics.increment.call_args_list
            if c[0][0] == "vibe_heartbeat_total"
        ]
        started_calls = [
            c for c in increment_calls
            if c[1].get("labels", {}).get("status") == "started"
        ]
        assert len(started_calls) >= 1

    @patch("agents.heartbeat.metrics")
    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_records_duration_metric(self, MockClient, mock_workflow, mock_metrics, config):
        """Heartbeat should record duration metric."""
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = []

        run_heartbeat(config)

        observe_calls = [
            c for c in mock_metrics.observe.call_args_list
            if c[0][0] == "vibe_heartbeat_duration_seconds"
        ]
        assert len(observe_calls) >= 1

    @patch("agents.heartbeat.metrics")
    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_records_token_metrics_on_success(self, MockClient, mock_workflow, mock_metrics, config):
        """Token usage should be recorded as metrics on successful runs."""
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(id="i1", title="Task")
        client.get_comments.return_value = []
        mock_workflow.return_value = {
            "final_output": "Done",
            "final_score": 90,
            "total_input_tokens": 1500,
            "total_output_tokens": 300,
        }

        run_heartbeat(config)

        token_calls = [
            c for c in mock_metrics.increment.call_args_list
            if c[0][0] == "vibe_heartbeat_tokens_total"
        ]
        assert len(token_calls) >= 2  # input + output

    @patch("agents.heartbeat.metrics")
    @patch("agents.heartbeat.PaperclipClient")
    def test_records_api_error_metric(self, MockClient, mock_metrics, config):
        """API errors should be counted in metrics."""
        client = MockClient.return_value
        client.get_identity.side_effect = PaperclipAPIError(401, "Unauthorized")

        run_heartbeat(config)

        error_calls = [
            c for c in mock_metrics.increment.call_args_list
            if c[0][0] == "vibe_paperclip_api_errors_total"
        ]
        assert len(error_calls) >= 1


# ── Heartbeat Result JSON Contract Tests ──
# Verify the HeartbeatResult JSON matches what parse.ts expects.


class TestHeartbeatResultAdapterContract:
    def test_all_fields_present_in_json(self):
        """The adapter's parseVibeOutput depends on these specific field names."""
        result = HeartbeatResult(
            status="success",
            issue_id="GEN-42",
            summary="Done",
            usage={"input_tokens": 1000, "output_tokens": 500},
            cost_cents=0,
            provider="ollama",
            model="codellama",
            exit_code=0,
            clarification=None,
            retry_after_seconds=None,
        )
        data = json.loads(result.to_json())

        # These field names must match what parse.ts extracts
        assert "status" in data
        assert "issue_id" in data
        assert "summary" in data
        assert "usage" in data
        assert "cost_cents" in data
        assert "provider" in data
        assert "model" in data
        assert "exit_code" in data
        assert "clarification" in data

    def test_usage_field_names_match_adapter(self):
        """parse.ts reads usage.input_tokens and usage.output_tokens."""
        result = HeartbeatResult(
            status="success",
            usage={"input_tokens": 100, "output_tokens": 50},
        )
        data = json.loads(result.to_json())
        assert "input_tokens" in data["usage"]
        assert "output_tokens" in data["usage"]

    def test_clarification_field_names_match_adapter(self):
        """parse.ts reads clarification.questions, blocking_node, context_summary."""
        result = HeartbeatResult(
            status="clarification_needed",
            clarification={
                "questions": ["Q1?"],
                "blocking_node": "vibe",
                "context_summary": "Building API",
            },
        )
        data = json.loads(result.to_json())
        assert "questions" in data["clarification"]
        assert "blocking_node" in data["clarification"]
        assert "context_summary" in data["clarification"]

    def test_result_is_valid_json(self):
        """Must always produce valid JSON for the adapter to parse."""
        for status in ("success", "idle", "blocked", "clarification_needed", "cancelled", "failed"):
            result = HeartbeatResult(status=status, issue_id="test")
            # Should not raise
            data = json.loads(result.to_json())
            assert isinstance(data, dict)


# ── In-Progress Status Tests ──


class TestInProgressStatus:
    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_heartbeat_sets_in_progress_after_checkout(self, MockClient, mock_workflow, config):
        """After checkout, heartbeat should update issue to in_progress."""
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(id="i1", title="Task")
        client.get_comments.return_value = []
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}

        run_heartbeat(config)

        # Find the in_progress call among all update_issue calls
        in_progress_calls = [
            c for c in client.update_issue.call_args_list
            if c[1].get("status") == "in_progress" or
               (len(c[0]) >= 2 and c[0][1] == "in_progress")
        ]
        assert len(in_progress_calls) >= 1, "Should set status to in_progress after checkout"

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_in_progress_failure_is_non_fatal(self, MockClient, mock_workflow, config):
        """If setting in_progress fails, workflow should still run."""
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(id="i1", title="Task")
        client.get_comments.return_value = []
        mock_workflow.return_value = {"final_output": "Done", "final_score": 90}

        # First call (in_progress) fails, subsequent calls succeed
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise PaperclipAPIError(500, "API hiccup")
            return MagicMock()

        client.update_issue.side_effect = side_effect

        result = run_heartbeat(config)
        assert result.status == "success"


# ── Cost Estimation Tests ──


class TestEstimateCostCents:
    def test_local_backends_are_free(self):
        assert _estimate_cost_cents("vllm", "qwen3.5:7b", 1000, 500) == 0
        assert _estimate_cost_cents("ollama", "llama3:8b", 1000, 500) == 0
        assert _estimate_cost_cents("llama.cpp", "model", 1000, 500) == 0
        assert _estimate_cost_cents("llamacpp", "model", 1000, 500) == 0

    def test_zero_tokens_are_free(self):
        assert _estimate_cost_cents("openai", "gpt-4o", 0, 0) == 0

    def test_openai_gpt4o(self):
        # 1M input tokens @ 250 cents + 1M output tokens @ 1000 cents = 1250 cents
        cost = _estimate_cost_cents("openai", "gpt-4o", 1_000_000, 1_000_000)
        assert cost == 1250

    def test_openai_gpt4o_mini(self):
        cost = _estimate_cost_cents("openai", "gpt-4o-mini", 1_000_000, 1_000_000)
        assert cost == 75  # 15 + 60

    def test_anthropic_sonnet(self):
        cost = _estimate_cost_cents("anthropic", "claude-3.5-sonnet-20241022", 1_000_000, 1_000_000)
        assert cost == 1800  # 300 + 1500

    def test_anthropic_haiku(self):
        cost = _estimate_cost_cents("anthropic", "claude-3-haiku-20240307", 1_000_000, 1_000_000)
        assert cost == 150  # 25 + 125

    def test_google_gemini_flash(self):
        cost = _estimate_cost_cents("google", "gemini-1.5-flash", 1_000_000, 1_000_000)
        assert cost == 38  # 8 + 30

    def test_unknown_backend_returns_zero(self):
        assert _estimate_cost_cents("unknown-backend", "model", 1000, 500) == 0

    def test_unknown_model_uses_default(self):
        # Unknown model under openai uses _default rates (250, 1000)
        cost = _estimate_cost_cents("openai", "o1-preview", 1_000_000, 1_000_000)
        assert cost == 1250

    def test_small_usage_rounds_to_minimum(self):
        # Very small usage should still report at least 1 cent
        cost = _estimate_cost_cents("openai", "gpt-4o-mini", 100, 100)
        assert cost == 1  # Minimum 1 cent for any cloud usage

    def test_case_insensitive_backend(self):
        cost = _estimate_cost_cents("OpenAI", "gpt-4o", 1_000_000, 0)
        assert cost == 250

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_heartbeat_reports_computed_cost(self, MockClient, mock_workflow):
        """Heartbeat should report computed cost, not hardcoded 0."""
        cfg = SystemConfig()
        cfg.paperclip = PaperclipConfig(
            enabled=True, api_url="http://localhost:3100",
            api_key="test-key", cost_reporting=True,
        )
        cfg.spending.enabled = False
        cfg.model.backend = "openai"
        cfg.model.model_name = "gpt-4o"

        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(id="i1", title="Task")
        client.get_comments.return_value = []
        mock_workflow.return_value = {
            "final_output": "Done",
            "final_score": 90,
            "total_input_tokens": 10000,
            "total_output_tokens": 5000,
        }

        result = run_heartbeat(cfg)
        assert result.status == "success"
        # Cost should be non-zero for cloud backends with tokens
        assert result.cost_cents > 0

        # report_cost should have been called with non-zero cost
        cost_call = client.report_cost.call_args
        assert cost_call[1]["cost_cents"] > 0


# ── Cancellation Tests ──


class TestCancellationToken:
    def test_not_cancelled_initially(self):
        token = CancellationToken()
        assert not token.is_cancelled

    def test_cancel_sets_flag(self):
        token = CancellationToken()
        token.cancel("user requested")
        assert token.is_cancelled
        assert token.reason == "user requested"

    def test_check_raises_when_cancelled(self):
        token = CancellationToken()
        token.cancel()
        with pytest.raises(WorkflowCancelledError):
            token.check()

    def test_check_does_not_raise_when_not_cancelled(self):
        token = CancellationToken()
        token.check()  # Should not raise

    def test_cancel_is_idempotent(self):
        token = CancellationToken()
        token.cancel("first")
        token.cancel("second")
        assert token.is_cancelled
        assert token.reason == "second"


class TestCancellationPoller:
    def test_polls_and_cancels_on_cancelled_status(self):
        """Poller should fire the token when issue becomes cancelled."""
        client = MagicMock(spec=PaperclipClient)
        client.get_issue.return_value = Issue(id="i1", title="Task", status="cancelled")
        token = CancellationToken()

        poller = CancellationPoller(client, "i1", token, interval=0.05)
        poller.start()

        import time
        time.sleep(0.3)
        poller.stop()

        assert token.is_cancelled
        assert "cancelled in Paperclip" in token.reason

    def test_does_not_cancel_for_in_progress(self):
        """Poller should not fire for non-cancelled statuses."""
        client = MagicMock(spec=PaperclipClient)
        client.get_issue.return_value = Issue(id="i1", title="Task", status="in_progress")
        token = CancellationToken()

        poller = CancellationPoller(client, "i1", token, interval=0.05)
        poller.start()

        import time
        time.sleep(0.2)
        poller.stop()

        assert not token.is_cancelled

    def test_stops_after_max_errors(self):
        """Poller should give up after consecutive API errors."""
        client = MagicMock(spec=PaperclipClient)
        client.get_issue.side_effect = PaperclipAPIError(500, "Server error")
        token = CancellationToken()

        poller = CancellationPoller(client, "i1", token, interval=0.05, max_errors=2)
        poller.start()

        import time
        time.sleep(0.5)
        poller.stop()

        assert not token.is_cancelled
        # Should have been called at least 2 times before giving up
        assert client.get_issue.call_count >= 2

    def test_stop_is_idempotent(self):
        """Calling stop() multiple times should not raise."""
        client = MagicMock(spec=PaperclipClient)
        client.get_issue.return_value = Issue(id="i1", title="Task", status="todo")
        token = CancellationToken()

        poller = CancellationPoller(client, "i1", token, interval=0.05)
        poller.start()
        poller.stop()
        poller.stop()  # Should not raise


class TestHeartbeatCancellation:
    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_cancelled_workflow_returns_cancelled_status(self, MockClient, mock_workflow, config):
        """When workflow is cancelled, heartbeat returns cancelled status."""
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(id="i1", title="Task")
        client.get_comments.return_value = []

        mock_workflow.side_effect = WorkflowCancelledError("issue cancelled in Paperclip")

        result = run_heartbeat(config)
        assert result.status == "cancelled"
        assert result.exit_code == 0
        assert "cancelled" in result.summary.lower()

    def test_post_cancelled_adds_comment(self):
        """_post_cancelled should use add_comment (not update_issue)."""
        client = MagicMock(spec=PaperclipClient)
        _post_cancelled(client, "i1")
        client.add_comment.assert_called_once()
        body = client.add_comment.call_args[0][1]
        assert "Cancelled" in body

    def test_post_cancelled_handles_api_error(self):
        """_post_cancelled should not raise on API failure."""
        client = MagicMock(spec=PaperclipClient)
        client.add_comment.side_effect = PaperclipAPIError(500, "Error")
        _post_cancelled(client, "i1")  # Should not raise


# ── Config Validation Tests ──


class TestValidateHeartbeatConfig:
    """Tests for _validate_heartbeat_config."""

    def test_valid_config_returns_empty(self, config):
        """Valid config with all env vars produces no issues."""
        with patch.dict(os.environ, {
            "PAPERCLIP_API_URL": "http://localhost:3100",
            "PAPERCLIP_AGENT_ID": "agent-1",
        }):
            issues = _validate_heartbeat_config(config)
            assert issues == []

    def test_missing_model_name(self, config):
        """Missing model name triggers validation error."""
        config.model.model_name = ""
        with patch.dict(os.environ, {
            "PAPERCLIP_API_URL": "http://localhost:3100",
            "PAPERCLIP_AGENT_ID": "agent-1",
        }):
            issues = _validate_heartbeat_config(config)
            assert any("Model name" in i for i in issues)

    def test_missing_paperclip_api_url(self, config, monkeypatch):
        """Missing PAPERCLIP_API_URL triggers validation error."""
        config.paperclip.api_url = ""
        monkeypatch.delenv("PAPERCLIP_API_URL", raising=False)
        issues = _validate_heartbeat_config(config)
        assert any("PAPERCLIP_API_URL" in i for i in issues)

    def test_missing_paperclip_agent_id(self, config, monkeypatch):
        """Missing PAPERCLIP_AGENT_ID triggers validation error."""
        monkeypatch.delenv("PAPERCLIP_AGENT_ID", raising=False)
        issues = _validate_heartbeat_config(config)
        assert any("PAPERCLIP_AGENT_ID" in i for i in issues)

    def test_config_api_url_override_satisfies(self, config, monkeypatch):
        """Config api_url override satisfies the check even without env var."""
        config.paperclip.api_url = "http://paperclip:3100"
        monkeypatch.delenv("PAPERCLIP_API_URL", raising=False)
        issues = _validate_heartbeat_config(config)
        assert not any("PAPERCLIP_API_URL" in i for i in issues)

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_heartbeat_fails_on_invalid_config(self, MockClient, mock_workflow, config):
        """run_heartbeat returns failed when config validation fails."""
        config.model.model_name = ""
        with patch.dict(os.environ, {
            "PAPERCLIP_API_URL": "http://localhost:3100",
            "PAPERCLIP_AGENT_ID": "agent-1",
        }):
            result = run_heartbeat(config)
            assert result.status == "failed"
            assert "Configuration validation failed" in result.summary
            # Workflow should never be called
            mock_workflow.assert_not_called()


# ── Progress Callback Tests ──


class TestProgressCallback:
    """Tests for _make_progress_callback and _PROGRESS_NODES."""

    def test_progress_nodes_defined(self):
        """Key workflow nodes should be in the progress map."""
        assert "specialist" in _PROGRESS_NODES
        assert "heuristic_critic" in _PROGRESS_NODES

    def test_callback_posts_for_specialist(self):
        """Progress callback posts a comment for specialist node."""
        client = MagicMock(spec=PaperclipClient)
        cb = _make_progress_callback(client, "issue-1")
        state = {"iteration_count": 1, "max_iterations": 3, "critic_score": 0}
        cb("specialist", state)
        client.add_comment.assert_called_once()
        body = client.add_comment.call_args[0][1]
        assert "specialist" in body.lower() or "Specialist" in body
        assert "2/3" in body  # iteration 1+1 / 3

    def test_callback_posts_for_critic_with_score(self):
        """Progress callback includes score for critic node."""
        client = MagicMock(spec=PaperclipClient)
        cb = _make_progress_callback(client, "issue-1")
        state = {"iteration_count": 0, "max_iterations": 3, "critic_score": 72, "heuristic_critic_score": 72}
        cb("heuristic_critic", state)
        client.add_comment.assert_called_once()
        body = client.add_comment.call_args[0][1]
        assert "72" in body

    def test_callback_ignores_non_progress_nodes(self):
        """Progress callback does not post for non-key nodes."""
        client = MagicMock(spec=PaperclipClient)
        cb = _make_progress_callback(client, "issue-1")
        cb("inject_memory", {})
        client.add_comment.assert_not_called()

    def test_callback_handles_api_error(self):
        """Progress callback does not raise on API failure."""
        client = MagicMock(spec=PaperclipClient)
        client.add_comment.side_effect = PaperclipAPIError(500, "Server error")
        cb = _make_progress_callback(client, "issue-1")
        state = {"iteration_count": 0, "max_iterations": 3}
        cb("specialist", state)  # Should not raise

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_heartbeat_passes_progress_callback(self, MockClient, mock_workflow, config):
        """_run_workflow receives a progress_callback argument."""
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(id="i1", title="Task")
        client.get_comments.return_value = []
        mock_workflow.return_value = {
            "final_output": "done",
            "final_score": 90,
            "critic_score": 90,
        }

        result = run_heartbeat(config)
        assert result.status == "success"
        # Verify _run_workflow was called with progress_callback
        call_kwargs = mock_workflow.call_args[1]
        assert "progress_callback" in call_kwargs
        assert callable(call_kwargs["progress_callback"])


# ── SIGTERM Handling Tests ──


class TestSigtermHandling:
    """Tests for SIGTERM graceful shutdown."""

    def test_sigterm_handler_install_and_restore(self):
        """Install and restore SIGTERM handler round-trips correctly."""
        import signal as sig
        original = sig.getsignal(sig.SIGTERM)
        client = MagicMock(spec=PaperclipClient)
        state: dict = {}
        _install_sigterm_handler(client, "i1", state)
        # Handler should be changed
        assert sig.getsignal(sig.SIGTERM) != original
        _restore_sigterm_handler()
        # Should be restored
        assert sig.getsignal(sig.SIGTERM) == original

    def test_sigterm_handler_raises_exception(self):
        """SIGTERM handler raises _SigtermReceived."""
        import signal as sig
        client = MagicMock(spec=PaperclipClient)
        state: dict = {}
        _install_sigterm_handler(client, "i1", state)
        try:
            handler = sig.getsignal(sig.SIGTERM)
            with pytest.raises(_SigtermReceived):
                handler(sig.SIGTERM, None)
        finally:
            _restore_sigterm_handler()

    def test_post_sigterm_partial_with_output(self):
        """_post_sigterm_partial posts partial output and sets blocked."""
        client = MagicMock(spec=PaperclipClient)
        state = {
            "current_output": "partial code here",
            "critic_score": 45,
            "last_node": "specialist",
        }
        _post_sigterm_partial(client, "i1", state)
        client.update_issue.assert_called_once()
        call_kwargs = client.update_issue.call_args
        assert call_kwargs[1]["status"] == "blocked" or call_kwargs[0][1] == "blocked"
        comment = call_kwargs[1].get("comment", "")
        assert "SIGTERM" in comment
        assert "partial code here" in comment
        assert "specialist" in comment

    def test_post_sigterm_partial_empty_state(self):
        """_post_sigterm_partial handles empty state gracefully."""
        client = MagicMock(spec=PaperclipClient)
        _post_sigterm_partial(client, "i1", {})
        client.update_issue.assert_called_once()
        comment = client.update_issue.call_args[1].get("comment", "")
        assert "No output yet" in comment

    def test_post_sigterm_partial_handles_api_error(self):
        """_post_sigterm_partial does not raise on API failure."""
        client = MagicMock(spec=PaperclipClient)
        client.update_issue.side_effect = PaperclipAPIError(500, "Error")
        _post_sigterm_partial(client, "i1", {})  # Should not raise

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_heartbeat_handles_sigterm(self, MockClient, mock_workflow, config):
        """When SIGTERM fires during workflow, heartbeat posts partial and returns blocked."""
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="todo"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(id="i1", title="Task")
        client.get_comments.return_value = []

        mock_workflow.side_effect = _SigtermReceived()

        result = run_heartbeat(config)
        assert result.status == "blocked"
        assert "SIGTERM" in result.summary


# ── Clarification Resume Tests ──


class TestClarificationResume:
    """Tests for clarification resume that skips spec rebuild."""

    @patch("agents.workflow_factory.create_agent_graph")
    @patch("agents.workflow_factory.create_backend_from_config")
    def test_clarification_reply_clears_flags(self, mock_backend, mock_graph, config):
        """When clarification_reply is set, clarification flags are cleared."""
        mock_compiled = MagicMock()
        mock_compiled.stream.return_value = iter([])
        mock_graph.return_value = mock_compiled

        result = _run_workflow(
            config,
            user_request="Task: Build auth\n[Clarification from human]: Use JWT",
            task_type="code_generation",
            clarification_reply="Use JWT",
        )

        # The initial state should have clarification flags cleared
        call_args = mock_compiled.stream.call_args[0][0]
        assert call_args["clarification_needed"] is False
        assert call_args["clarification_questions"] == []

    @patch("agents.workflow_factory.create_agent_graph")
    @patch("agents.workflow_factory.create_backend_from_config")
    def test_no_clarification_reply_normal_flow(self, mock_backend, mock_graph, config):
        """Without clarification_reply, specification is empty (normal flow)."""
        mock_compiled = MagicMock()
        mock_compiled.stream.return_value = iter([])
        mock_graph.return_value = mock_compiled

        result = _run_workflow(
            config,
            user_request="Build auth module",
            task_type="code_generation",
        )

        call_args = mock_compiled.stream.call_args[0][0]
        assert call_args.get("specification", "") == ""

    @patch("agents.heartbeat._run_workflow")
    @patch("agents.heartbeat.PaperclipClient")
    def test_heartbeat_passes_clarification_reply_to_workflow(self, MockClient, mock_workflow, config):
        """When resuming from clarification, clarification_reply is passed to _run_workflow."""
        client = MockClient.return_value
        client.get_identity.return_value = AgentInfo(
            id="agent-1", company_id="c1", name="Bot", role="eng",
        )
        client.get_assignments.return_value = [
            Issue(id="i1", title="Task", status="blocked"),
        ]
        client.checkout_issue.return_value = CheckoutResult(success=True)
        client.get_issue.return_value = Issue(
            id="i1", title="Task", status="blocked",
        )
        # Simulate a human reply comment
        human_comment = MagicMock()
        human_comment.id = "c1"
        human_comment.author_agent_id = None
        human_comment.body = "Use JWT please"
        client.get_comments.return_value = [human_comment]
        client.agent_id = "agent-1"

        mock_workflow.return_value = {
            "final_output": "done",
            "final_score": 90,
            "critic_score": 90,
        }

        with patch.dict(os.environ, {
            "PAPERCLIP_WAKE_REASON": "issue_comment_mentioned",
            "PAPERCLIP_WAKE_COMMENT_ID": "c1",
            "PAPERCLIP_TASK_ID": "i1",
        }):
            result = run_heartbeat(config)

        # Verify clarification_reply was passed
        call_kwargs = mock_workflow.call_args[1]
        assert call_kwargs.get("clarification_reply") == "Use JWT please"


# ── Artifact Cache Maintenance Tests ──


def test_heartbeat_calls_artifact_cache_cleanup(monkeypatch, tmp_path):
    """Heartbeat finally block should clean up expired cache entries."""
    from agents.artifact_store import ArtifactStore

    cleanup_called = False
    evict_called = False

    original_cleanup = ArtifactStore.cleanup_expired
    original_evict = ArtifactStore._evict_if_needed

    def mock_cleanup(self):
        nonlocal cleanup_called
        cleanup_called = True
        return 0

    def mock_evict(self, conn):
        nonlocal evict_called
        evict_called = True
        return 0

    monkeypatch.setattr(ArtifactStore, "cleanup_expired", mock_cleanup)
    monkeypatch.setattr(ArtifactStore, "_evict_if_needed", mock_evict)

    # We need to test that the finally block calls these.
    # Import the function that does the cleanup:
    from agents.heartbeat import _artifact_cache_maintenance
    _artifact_cache_maintenance()

    assert cleanup_called, "cleanup_expired should be called"
