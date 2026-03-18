"""
Tests for Paperclip REST API Client

Tests all 12 API methods, error handling, retry logic, and auth.
All HTTP calls are mocked — no real Paperclip server needed.
"""

import json
import os
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import requests

from agents.paperclip_client import (
    PaperclipClient,
    PaperclipAPIError,
    PaperclipAuthError,
    PaperclipConflictError,
    PaperclipNotFoundError,
    AgentInfo,
    CheckoutResult,
    Comment,
    DashboardSummary,
    Issue,
    _compute_delay,
    _extract_retry_after_header,
    _parse_agent_info,
    _parse_comment,
    _parse_issue,
)


# ── Fixtures ──


@pytest.fixture
def env_vars(monkeypatch):
    """Set required Paperclip environment variables."""
    monkeypatch.setenv("PAPERCLIP_API_URL", "http://localhost:3100")
    monkeypatch.setenv("PAPERCLIP_API_KEY", "pcp_test_key_123")
    monkeypatch.setenv("PAPERCLIP_AGENT_ID", "agent-uuid-1")
    monkeypatch.setenv("PAPERCLIP_COMPANY_ID", "company-uuid-1")
    monkeypatch.setenv("PAPERCLIP_RUN_ID", "run-uuid-1")


@pytest.fixture
def client(env_vars):
    """Create a PaperclipClient with test env vars."""
    return PaperclipClient(max_retries=0)


@pytest.fixture
def retry_client(env_vars):
    """Create a PaperclipClient with retry enabled (for retry tests)."""
    return PaperclipClient(max_retries=2, base_delay=0.01)


def _mock_response(status_code=200, json_data=None, text="", headers=None):
    """Create a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text or json.dumps(json_data or {})
    resp.content = resp.text.encode() if resp.text else b""
    resp.headers = headers or {}
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
    return resp


SAMPLE_AGENT = {
    "id": "agent-uuid-1",
    "companyId": "company-uuid-1",
    "name": "CodeBot",
    "role": "engineer",
    "title": "Senior Engineer",
    "status": "active",
    "reportsTo": "manager-uuid",
    "budgetMonthlyCents": 10000,
    "spentMonthlyCents": 2500,
    "chainOfCommand": [{"id": "manager-uuid", "name": "CTO"}],
}

SAMPLE_ISSUE = {
    "id": "issue-uuid-1",
    "title": "Implement auth module",
    "description": "Build JWT authentication",
    "status": "todo",
    "priority": "high",
    "assigneeAgentId": "agent-uuid-1",
    "parentId": "parent-uuid",
    "projectId": "project-uuid",
    "goalId": "goal-uuid",
    "identifier": "PAP-42",
    "ancestors": [{"id": "parent-uuid", "title": "Auth System"}],
    "commentsCount": 3,
}

SAMPLE_COMMENT = {
    "id": "comment-uuid-1",
    "body": "Started working on this",
    "authorAgentId": "agent-uuid-1",
    "authorUserId": None,
    "createdAt": "2026-03-07T10:00:00Z",
}


# ── Constructor Tests ──


class TestConstructor:
    def test_reads_env_vars(self, env_vars):
        client = PaperclipClient()
        assert client.api_url == "http://localhost:3100"
        assert client.api_key == "pcp_test_key_123"
        assert client.agent_id == "agent-uuid-1"
        assert client.company_id == "company-uuid-1"
        assert client.run_id == "run-uuid-1"

    def test_explicit_params_override_env(self, env_vars):
        client = PaperclipClient(
            api_url="http://custom:9000",
            api_key="custom_key",
            agent_id="custom-agent",
            company_id="custom-company",
            run_id="custom-run",
        )
        assert client.api_url == "http://custom:9000"
        assert client.api_key == "custom_key"
        assert client.agent_id == "custom-agent"

    def test_strips_trailing_slash(self, env_vars, monkeypatch):
        monkeypatch.setenv("PAPERCLIP_API_URL", "http://localhost:3100/")
        client = PaperclipClient()
        assert client.api_url == "http://localhost:3100"

    def test_missing_api_url_raises(self, monkeypatch):
        monkeypatch.delenv("PAPERCLIP_API_URL", raising=False)
        monkeypatch.setenv("PAPERCLIP_API_KEY", "key")
        with pytest.raises(ValueError, match="PAPERCLIP_API_URL"):
            PaperclipClient()

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.setenv("PAPERCLIP_API_URL", "http://localhost:3100")
        monkeypatch.delenv("PAPERCLIP_API_KEY", raising=False)
        with pytest.raises(ValueError, match="PAPERCLIP_API_KEY"):
            PaperclipClient()


# ── Headers Tests ──


class TestHeaders:
    def test_includes_auth_and_run_id(self, client):
        headers = client._headers()
        assert headers["Authorization"] == "Bearer pcp_test_key_123"
        assert headers["X-Paperclip-Run-Id"] == "run-uuid-1"
        assert headers["Content-Type"] == "application/json"

    def test_omits_run_id_when_empty(self, env_vars, monkeypatch):
        monkeypatch.setenv("PAPERCLIP_RUN_ID", "")
        c = PaperclipClient()
        headers = c._headers()
        assert "X-Paperclip-Run-Id" not in headers


# ── get_identity Tests ──


class TestGetIdentity:
    @patch("agents.paperclip_client.requests.request")
    def test_success(self, mock_req, client):
        mock_req.return_value = _mock_response(200, SAMPLE_AGENT)
        agent = client.get_identity()
        assert isinstance(agent, AgentInfo)
        assert agent.id == "agent-uuid-1"
        assert agent.name == "CodeBot"
        assert agent.role == "engineer"
        assert agent.budget_monthly_cents == 10000
        assert agent.chain_of_command == [{"id": "manager-uuid", "name": "CTO"}]
        mock_req.assert_called_once()
        call_args = mock_req.call_args
        assert call_args[1]["url"] == "http://localhost:3100/api/agents/me"

    @patch("agents.paperclip_client.requests.request")
    def test_auth_error(self, mock_req, client):
        mock_req.return_value = _mock_response(401, text="Unauthorized")
        with pytest.raises(PaperclipAuthError) as exc_info:
            client.get_identity()
        assert exc_info.value.status_code == 401


# ── get_assignments Tests ──


class TestGetAssignments:
    @patch("agents.paperclip_client.requests.request")
    def test_returns_issue_list(self, mock_req, client):
        mock_req.return_value = _mock_response(200, [SAMPLE_ISSUE])
        issues = client.get_assignments()
        assert len(issues) == 1
        assert issues[0].title == "Implement auth module"
        assert issues[0].priority == "high"

    @patch("agents.paperclip_client.requests.request")
    def test_handles_wrapped_response(self, mock_req, client):
        mock_req.return_value = _mock_response(200, {"issues": [SAMPLE_ISSUE]})
        issues = client.get_assignments()
        assert len(issues) == 1

    @patch("agents.paperclip_client.requests.request")
    def test_custom_statuses(self, mock_req, client):
        mock_req.return_value = _mock_response(200, [])
        client.get_assignments(statuses=["in_progress"])
        call_args = mock_req.call_args
        assert "in_progress" in call_args[1]["params"]["status"]

    @patch("agents.paperclip_client.requests.request")
    def test_default_statuses(self, mock_req, client):
        mock_req.return_value = _mock_response(200, [])
        client.get_assignments()
        call_args = mock_req.call_args
        assert "todo,in_progress,blocked" == call_args[1]["params"]["status"]

    @patch("agents.paperclip_client.requests.request")
    def test_empty_assignments(self, mock_req, client):
        mock_req.return_value = _mock_response(200, [])
        issues = client.get_assignments()
        assert issues == []


# ── checkout_issue Tests ──


class TestCheckoutIssue:
    @patch("agents.paperclip_client.requests.request")
    def test_success(self, mock_req, client):
        mock_req.return_value = _mock_response(200, SAMPLE_ISSUE)
        result = client.checkout_issue("issue-uuid-1")
        assert isinstance(result, CheckoutResult)
        assert result.success is True
        assert result.issue is not None
        assert result.issue.id == "issue-uuid-1"

    @patch("agents.paperclip_client.requests.request")
    def test_conflict_returns_failure(self, mock_req, client):
        mock_req.return_value = _mock_response(409, text="Already checked out")
        result = client.checkout_issue("issue-uuid-1")
        assert result.success is False
        assert result.conflict_owner == "issue-uuid-1"

    @patch("agents.paperclip_client.requests.request")
    def test_sends_expected_statuses(self, mock_req, client):
        mock_req.return_value = _mock_response(200, SAMPLE_ISSUE)
        client.checkout_issue("issue-uuid-1", expected_statuses=["todo"])
        call_args = mock_req.call_args
        body = call_args[1]["json"]
        assert body["expectedStatuses"] == ["todo"]
        assert body["agentId"] == "agent-uuid-1"

    @patch("agents.paperclip_client.requests.request")
    def test_empty_response_body(self, mock_req, client):
        resp = _mock_response(200)
        resp.content = b""
        mock_req.return_value = resp
        result = client.checkout_issue("issue-uuid-1")
        assert result.success is True
        assert result.issue is None


# ── get_issue Tests ──


class TestGetIssue:
    @patch("agents.paperclip_client.requests.request")
    def test_success(self, mock_req, client):
        mock_req.return_value = _mock_response(200, SAMPLE_ISSUE)
        issue = client.get_issue("issue-uuid-1")
        assert issue.id == "issue-uuid-1"
        assert issue.identifier == "PAP-42"
        assert len(issue.ancestors) == 1
        assert issue.ancestors[0]["title"] == "Auth System"

    @patch("agents.paperclip_client.requests.request")
    def test_not_found(self, mock_req, client):
        mock_req.return_value = _mock_response(404, text="Not found")
        with pytest.raises(PaperclipNotFoundError):
            client.get_issue("nonexistent")


# ── get_comments Tests ──


class TestGetComments:
    @patch("agents.paperclip_client.requests.request")
    def test_returns_comments(self, mock_req, client):
        mock_req.return_value = _mock_response(200, [SAMPLE_COMMENT])
        comments = client.get_comments("issue-uuid-1")
        assert len(comments) == 1
        assert comments[0].body == "Started working on this"
        assert comments[0].author_agent_id == "agent-uuid-1"

    @patch("agents.paperclip_client.requests.request")
    def test_wrapped_response(self, mock_req, client):
        mock_req.return_value = _mock_response(200, {"comments": [SAMPLE_COMMENT]})
        comments = client.get_comments("issue-uuid-1")
        assert len(comments) == 1

    @patch("agents.paperclip_client.requests.request")
    def test_empty_comments(self, mock_req, client):
        mock_req.return_value = _mock_response(200, [])
        comments = client.get_comments("issue-uuid-1")
        assert comments == []


# ── update_issue Tests ──


class TestUpdateIssue:
    @patch("agents.paperclip_client.requests.request")
    def test_update_status_with_comment(self, mock_req, client):
        mock_req.return_value = _mock_response(200, {**SAMPLE_ISSUE, "status": "done"})
        issue = client.update_issue("issue-uuid-1", status="done", comment="All done")
        assert issue.status == "done"
        call_args = mock_req.call_args
        body = call_args[1]["json"]
        assert body["status"] == "done"
        assert body["comment"] == "All done"

    @patch("agents.paperclip_client.requests.request")
    def test_update_extra_fields(self, mock_req, client):
        mock_req.return_value = _mock_response(200, SAMPLE_ISSUE)
        client.update_issue("issue-uuid-1", priority="critical", title="Updated")
        call_args = mock_req.call_args
        body = call_args[1]["json"]
        assert body["priority"] == "critical"
        assert body["title"] == "Updated"

    @patch("agents.paperclip_client.requests.request")
    def test_update_status_only(self, mock_req, client):
        mock_req.return_value = _mock_response(200, SAMPLE_ISSUE)
        client.update_issue("issue-uuid-1", status="blocked")
        call_args = mock_req.call_args
        body = call_args[1]["json"]
        assert body == {"status": "blocked"}


# ── add_comment Tests ──


class TestAddComment:
    @patch("agents.paperclip_client.requests.request")
    def test_success(self, mock_req, client):
        mock_req.return_value = _mock_response(200, SAMPLE_COMMENT)
        comment = client.add_comment("issue-uuid-1", "Progress update")
        assert isinstance(comment, Comment)
        assert comment.id == "comment-uuid-1"
        call_args = mock_req.call_args
        assert call_args[1]["json"]["body"] == "Progress update"


# ── release_issue Tests ──


class TestReleaseIssue:
    @patch("agents.paperclip_client.requests.request")
    def test_success(self, mock_req, client):
        resp = _mock_response(200)
        resp.content = b""
        mock_req.return_value = resp
        client.release_issue("issue-uuid-1")
        call_args = mock_req.call_args
        assert "/api/issues/issue-uuid-1/release" in call_args[1]["url"]


# ── create_subtask Tests ──


class TestCreateSubtask:
    @patch("agents.paperclip_client.requests.request")
    def test_success(self, mock_req, client):
        mock_req.return_value = _mock_response(200, {**SAMPLE_ISSUE, "id": "subtask-uuid"})
        issue = client.create_subtask(
            title="Write tests",
            description="Unit tests for auth",
            parent_id="issue-uuid-1",
            goal_id="goal-uuid",
            assignee_agent_id="other-agent",
            priority="high",
        )
        assert issue.id == "subtask-uuid"
        call_args = mock_req.call_args
        body = call_args[1]["json"]
        assert body["parentId"] == "issue-uuid-1"
        assert body["goalId"] == "goal-uuid"
        assert body["assigneeAgentId"] == "other-agent"
        assert body["priority"] == "high"

    @patch("agents.paperclip_client.requests.request")
    def test_minimal_subtask(self, mock_req, client):
        mock_req.return_value = _mock_response(200, SAMPLE_ISSUE)
        client.create_subtask("Title", "Desc", "parent-uuid")
        call_args = mock_req.call_args
        body = call_args[1]["json"]
        assert "goalId" not in body
        assert "assigneeAgentId" not in body


# ── report_cost Tests ──


class TestReportCost:
    @patch("agents.paperclip_client.requests.request")
    def test_success(self, mock_req, client):
        resp = _mock_response(200)
        resp.content = b""
        mock_req.return_value = resp
        client.report_cost(
            provider="vllm",
            model="qwen3.5:7b",
            input_tokens=1000,
            output_tokens=500,
            cost_cents=0,
            issue_id="issue-uuid-1",
        )
        call_args = mock_req.call_args
        body = call_args[1]["json"]
        assert body["provider"] == "vllm"
        assert body["model"] == "qwen3.5:7b"
        assert body["inputTokens"] == 1000
        assert body["outputTokens"] == 500
        assert body["agentId"] == "agent-uuid-1"
        assert body["issueId"] == "issue-uuid-1"

    @patch("agents.paperclip_client.requests.request")
    def test_without_issue_id(self, mock_req, client):
        resp = _mock_response(200)
        resp.content = b""
        mock_req.return_value = resp
        client.report_cost("vllm", "qwen3.5:7b", 100, 50, 0)
        call_args = mock_req.call_args
        body = call_args[1]["json"]
        assert "issueId" not in body


# ── get_dashboard Tests ──


class TestGetDashboard:
    @patch("agents.paperclip_client.requests.request")
    def test_success(self, mock_req, client):
        mock_req.return_value = _mock_response(200, {
            "agentCounts": {"active": 3, "paused": 1},
            "issueCounts": {"todo": 5, "in_progress": 2},
            "spend": 5000,
            "budgetUtilization": 0.5,
        })
        dashboard = client.get_dashboard()
        assert isinstance(dashboard, DashboardSummary)
        assert dashboard.agent_counts["active"] == 3
        assert dashboard.issue_counts["todo"] == 5
        assert dashboard.spend == 5000
        assert dashboard.budget_utilization == 0.5


# ── list_agents Tests ──


class TestListAgents:
    @patch("agents.paperclip_client.requests.request")
    def test_returns_agents(self, mock_req, client):
        mock_req.return_value = _mock_response(200, [SAMPLE_AGENT])
        agents = client.list_agents()
        assert len(agents) == 1
        assert agents[0].name == "CodeBot"

    @patch("agents.paperclip_client.requests.request")
    def test_wrapped_response(self, mock_req, client):
        mock_req.return_value = _mock_response(200, {"agents": [SAMPLE_AGENT]})
        agents = client.list_agents()
        assert len(agents) == 1


# ── Retry Tests ──


class TestRetry:
    @patch("agents.paperclip_client.time.sleep")
    @patch("agents.paperclip_client.requests.request")
    def test_retries_on_503(self, mock_req, mock_sleep, retry_client):
        mock_req.side_effect = [
            _mock_response(503, text="Service Unavailable"),
            _mock_response(503, text="Service Unavailable"),
            _mock_response(200, SAMPLE_AGENT),
        ]
        agent = retry_client.get_identity()
        assert agent.name == "CodeBot"
        assert mock_req.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("agents.paperclip_client.time.sleep")
    @patch("agents.paperclip_client.requests.request")
    def test_retries_on_429(self, mock_req, mock_sleep, retry_client):
        mock_req.side_effect = [
            _mock_response(429, text="Rate limited", headers={"Retry-After": "2"}),
            _mock_response(200, SAMPLE_AGENT),
        ]
        agent = retry_client.get_identity()
        assert agent.name == "CodeBot"
        assert mock_req.call_count == 2

    @patch("agents.paperclip_client.time.sleep")
    @patch("agents.paperclip_client.requests.request")
    def test_retries_on_connection_error(self, mock_req, mock_sleep, retry_client):
        mock_req.side_effect = [
            requests.exceptions.ConnectionError("Connection refused"),
            _mock_response(200, SAMPLE_AGENT),
        ]
        agent = retry_client.get_identity()
        assert agent.name == "CodeBot"

    @patch("agents.paperclip_client.time.sleep")
    @patch("agents.paperclip_client.requests.request")
    def test_exhausted_retries_raises(self, mock_req, mock_sleep, retry_client):
        mock_req.side_effect = [
            _mock_response(503, text="Down"),
            _mock_response(503, text="Down"),
            _mock_response(503, text="Down"),
        ]
        with pytest.raises(PaperclipAPIError, match="failed after"):
            retry_client.get_identity()
        assert mock_req.call_count == 3

    @patch("agents.paperclip_client.requests.request")
    def test_no_retry_on_401(self, mock_req, retry_client):
        mock_req.return_value = _mock_response(401, text="Unauthorized")
        with pytest.raises(PaperclipAuthError):
            retry_client.get_identity()
        assert mock_req.call_count == 1

    @patch("agents.paperclip_client.requests.request")
    def test_no_retry_on_404(self, mock_req, retry_client):
        mock_req.return_value = _mock_response(404, text="Not found")
        with pytest.raises(PaperclipNotFoundError):
            retry_client.get_issue("nonexistent")
        assert mock_req.call_count == 1

    @patch("agents.paperclip_client.requests.request")
    def test_no_retry_on_409(self, mock_req, retry_client):
        mock_req.return_value = _mock_response(409, text="Conflict")
        result = retry_client.checkout_issue("issue-uuid-1")
        assert result.success is False
        assert mock_req.call_count == 1

    @patch("agents.paperclip_client.requests.request")
    def test_no_retry_on_400(self, mock_req, retry_client):
        mock_req.return_value = _mock_response(400, text="Bad request")
        with pytest.raises(PaperclipAPIError) as exc_info:
            retry_client.get_identity()
        assert exc_info.value.status_code == 400
        assert mock_req.call_count == 1


# ── Delay Computation Tests ──


class TestComputeDelay:
    def test_exponential_growth(self):
        d0 = _compute_delay(0, 1.0, 30.0, None)
        d1 = _compute_delay(1, 1.0, 30.0, None)
        # With jitter, can't assert exact values, but bounds hold
        assert 0 <= d0 <= 1.0
        assert 0 <= d1 <= 2.0

    def test_max_delay_cap(self):
        delay = _compute_delay(10, 1.0, 5.0, None)
        assert delay <= 5.0

    def test_retry_after_respected(self):
        delay = _compute_delay(0, 0.01, 30.0, 10.0)
        assert delay >= 10.0

    def test_retry_after_capped_by_max(self):
        delay = _compute_delay(0, 0.01, 5.0, 100.0)
        assert delay <= 5.0


# ── Extract Retry-After Tests ──


class TestExtractRetryAfter:
    def test_present(self):
        resp = MagicMock()
        resp.headers = {"Retry-After": "3.5"}
        assert _extract_retry_after_header(resp) == 3.5

    def test_missing(self):
        resp = MagicMock()
        resp.headers = {}
        assert _extract_retry_after_header(resp) is None

    def test_non_numeric(self):
        resp = MagicMock()
        resp.headers = {"Retry-After": "Thu, 01 Dec 2026 16:00:00 GMT"}
        assert _extract_retry_after_header(resp) is None


# ── Parser Tests ──


class TestParsers:
    def test_parse_agent_info_full(self):
        agent = _parse_agent_info(SAMPLE_AGENT)
        assert agent.id == "agent-uuid-1"
        assert agent.title == "Senior Engineer"
        assert agent.reports_to == "manager-uuid"

    def test_parse_agent_info_minimal(self):
        agent = _parse_agent_info({"id": "a", "name": "Bot", "role": "eng"})
        assert agent.id == "a"
        assert agent.company_id == ""
        assert agent.reports_to is None

    def test_parse_issue_full(self):
        issue = _parse_issue(SAMPLE_ISSUE)
        assert issue.id == "issue-uuid-1"
        assert issue.identifier == "PAP-42"
        assert len(issue.ancestors) == 1

    def test_parse_issue_minimal(self):
        issue = _parse_issue({"id": "i1", "title": "Task"})
        assert issue.id == "i1"
        assert issue.description == ""
        assert issue.ancestors == []

    def test_parse_comment_full(self):
        comment = _parse_comment(SAMPLE_COMMENT)
        assert comment.body == "Started working on this"
        assert comment.created_at == "2026-03-07T10:00:00Z"

    def test_parse_comment_minimal(self):
        comment = _parse_comment({"id": "c1", "body": "hi"})
        assert comment.author_agent_id is None
        assert comment.author_user_id is None


# ── Edge Cases ──


class TestEdgeCases:
    @patch("agents.paperclip_client.requests.request")
    def test_empty_response_body(self, mock_req, client):
        resp = _mock_response(200)
        resp.content = b""
        mock_req.return_value = resp
        result = client._request("POST", "/api/test")
        assert result == {}

    @patch("agents.paperclip_client.requests.request")
    def test_data_wrapper_in_list_response(self, mock_req, client):
        mock_req.return_value = _mock_response(200, {"data": [SAMPLE_ISSUE]})
        issues = client.get_assignments()
        assert len(issues) == 1

    def test_frozen_dataclasses(self):
        agent = AgentInfo(id="a", company_id="c", name="Bot", role="eng")
        with pytest.raises(AttributeError):
            agent.name = "Changed"

        issue = Issue(id="i", title="Task")
        with pytest.raises(AttributeError):
            issue.title = "Changed"

    @patch("agents.paperclip_client.requests.request")
    def test_timeout_error(self, mock_req, client):
        mock_req.side_effect = requests.exceptions.Timeout("Timed out")
        with pytest.raises(PaperclipAPIError, match="failed after"):
            client.get_identity()
