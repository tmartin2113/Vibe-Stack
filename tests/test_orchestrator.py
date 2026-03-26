"""
Tests for Orchestrator Bridge Agent — Fan-Out/Fan-In via Paperclip

Tests the 3-phase orchestrator state machine: decompose, poll, aggregate.
All Paperclip API calls and workflow execution are mocked.
"""

import re
from unittest.mock import MagicMock, patch, call

import pytest

from agents.config import SystemConfig, PaperclipConfig, WorkflowConfig
from agents.heartbeat import HeartbeatResult
from agents.orchestrator import (
    OrchestratorPhase,
    _aggregate_with_partial_failures,
    _build_agent_lookup,
    _count_retries,
    _create_aggregation_registry,
    _detect_phase,
    _extract_child_result,
    _extract_strategy,
    _extract_task_type,
    _find_agent_name,
    _match_agent,
    _maybe_retry_child,
    _run_directly,
    run_orchestrator_heartbeat,
)
from agents.paperclip_client import (
    AgentInfo,
    Comment,
    Issue,
    PaperclipAPIError,
    PaperclipClient,
)


# ── Fixtures ──


@pytest.fixture
def config():
    """Test config with orchestrator settings."""
    cfg = SystemConfig()
    cfg.paperclip = PaperclipConfig(
        enabled=True,
        api_url="http://localhost:3100",
        api_key="test-key",
        orchestrator_max_children=5,
        orchestrator_retry_failed=True,
        orchestrator_max_retries=1,
    )
    cfg.spending.enabled = False
    return cfg


@pytest.fixture
def mock_client():
    """Mock PaperclipClient with common defaults."""
    client = MagicMock(spec=PaperclipClient)
    client.agent_id = "orchestrator-1"
    client.company_id = "company-1"
    return client


@pytest.fixture
def parent_issue():
    return Issue(
        id="parent-1",
        title="Build REST API with tests and security audit",
        description="Create a FastAPI REST API with full test coverage and security review",
        status="in_progress",
        assignee_agent_id="orchestrator-1",
        goal_id="goal-1",
    )


@pytest.fixture
def child_issues_done():
    return [
        Issue(id="child-1", title="[code_generation] Build REST API", status="done"),
        Issue(id="child-2", title="[test_generation] Build REST API", status="done"),
        Issue(id="child-3", title="[security_audit] Build REST API", status="done"),
    ]


@pytest.fixture
def child_issues_mixed():
    return [
        Issue(id="child-1", title="[code_generation] Build REST API", status="done"),
        Issue(id="child-2", title="[test_generation] Build REST API", status="in_progress"),
        Issue(id="child-3", title="[security_audit] Build REST API", status="blocked"),
    ]


@pytest.fixture
def sample_agents():
    return [
        AgentInfo(id="orchestrator-1", company_id="c1", name="Orchestrator", role="orchestrator", status="active"),
        AgentInfo(id="code-agent", company_id="c1", name="CodeBot", role="code_engineer", status="active"),
        AgentInfo(id="test-agent", company_id="c1", name="TestBot", role="test_specialist", status="active"),
        AgentInfo(id="security-agent", company_id="c1", name="SecBot", role="security_analyst", status="active"),
        AgentInfo(id="research-agent", company_id="c1", name="ResearchBot", role="research_assistant", status="active"),
    ]


# ── Phase Detection Tests ──


class TestPhaseDetection:
    """Tests for _detect_phase()."""

    def test_no_children_returns_decompose(self):
        assert _detect_phase([]) == OrchestratorPhase.DECOMPOSE

    def test_all_done_returns_aggregate(self, child_issues_done):
        assert _detect_phase(child_issues_done) == OrchestratorPhase.AGGREGATE

    def test_mixed_statuses_returns_poll(self, child_issues_mixed):
        assert _detect_phase(child_issues_mixed) == OrchestratorPhase.POLL

    def test_some_in_progress_returns_poll(self):
        children = [
            Issue(id="c1", title="task", status="done"),
            Issue(id="c2", title="task", status="in_progress"),
        ]
        assert _detect_phase(children) == OrchestratorPhase.POLL

    def test_all_blocked_returns_poll(self):
        children = [
            Issue(id="c1", title="task", status="blocked"),
            Issue(id="c2", title="task", status="blocked"),
        ]
        assert _detect_phase(children) == OrchestratorPhase.POLL


# ── Decompose Tests ──


class TestDecompose:
    """Tests for the DECOMPOSE phase."""

    @patch("agents.router.RouterNode")
    def test_decompose_creates_subtasks(
        self, MockRouter, config, mock_client, parent_issue, sample_agents
    ):
        # Router says decomposition is needed
        mock_router = MockRouter.return_value
        mock_router.execute.return_value = {
            "requires_decomposition": True,
            "sub_tasks": [
                {"task_type": "code_generation", "specification": "Build the API"},
                {"task_type": "test_generation", "specification": "Write tests"},
            ],
            "aggregation_strategy": "merge",
        }
        mock_client.get_children.return_value = []
        mock_client.list_agents.return_value = sample_agents
        mock_client.create_subtask.return_value = Issue(id="new-1", title="subtask")

        result = run_orchestrator_heartbeat(config, mock_client, parent_issue)

        assert result.status == "success"
        assert "2 subtasks" in result.summary
        assert mock_client.create_subtask.call_count == 2
        mock_client.add_comment.assert_called_once()

    @patch("agents.router.RouterNode")
    def test_decompose_assigns_matching_agents(
        self, MockRouter, config, mock_client, parent_issue, sample_agents
    ):
        mock_router = MockRouter.return_value
        mock_router.execute.return_value = {
            "requires_decomposition": True,
            "sub_tasks": [
                {"task_type": "code_generation", "specification": "Code task"},
            ],
            "aggregation_strategy": "merge",
        }
        mock_client.get_children.return_value = []
        mock_client.list_agents.return_value = sample_agents
        mock_client.create_subtask.return_value = Issue(id="new-1", title="sub")

        run_orchestrator_heartbeat(config, mock_client, parent_issue)

        # Should assign to the code agent
        create_call = mock_client.create_subtask.call_args
        assert create_call.kwargs.get("assignee_agent_id") == "code-agent" or \
            create_call[1].get("assignee_agent_id") == "code-agent"

    @patch("agents.orchestrator._run_directly")
    @patch("agents.router.RouterNode")
    def test_decompose_runs_directly_if_not_needed(
        self, MockRouter, mock_run_directly, config, mock_client, parent_issue
    ):
        mock_router = MockRouter.return_value
        mock_router.execute.return_value = {
            "requires_decomposition": False,
        }
        mock_client.get_children.return_value = []
        mock_run_directly.return_value = HeartbeatResult(status="success")

        result = run_orchestrator_heartbeat(config, mock_client, parent_issue)
        mock_run_directly.assert_called_once()

    @patch("agents.router.RouterNode")
    def test_decompose_limits_children(
        self, MockRouter, config, mock_client, parent_issue, sample_agents
    ):
        config.paperclip.orchestrator_max_children = 2
        mock_router = MockRouter.return_value
        mock_router.execute.return_value = {
            "requires_decomposition": True,
            "sub_tasks": [
                {"task_type": "code_generation", "specification": "s1"},
                {"task_type": "test_generation", "specification": "s2"},
                {"task_type": "security_audit", "specification": "s3"},
                {"task_type": "documentation", "specification": "s4"},
            ],
            "aggregation_strategy": "merge",
        }
        mock_client.get_children.return_value = []
        mock_client.list_agents.return_value = sample_agents
        mock_client.create_subtask.return_value = Issue(id="new-1", title="sub")

        run_orchestrator_heartbeat(config, mock_client, parent_issue)

        assert mock_client.create_subtask.call_count == 2

    @patch("agents.router.RouterNode")
    def test_decompose_stores_strategy_in_comment(
        self, MockRouter, config, mock_client, parent_issue, sample_agents
    ):
        mock_router = MockRouter.return_value
        mock_router.execute.return_value = {
            "requires_decomposition": True,
            "sub_tasks": [
                {"task_type": "code_generation", "specification": "s1"},
            ],
            "aggregation_strategy": "report",
        }
        mock_client.get_children.return_value = []
        mock_client.list_agents.return_value = sample_agents
        mock_client.create_subtask.return_value = Issue(id="new-1", title="sub")

        run_orchestrator_heartbeat(config, mock_client, parent_issue)

        comment_body = mock_client.add_comment.call_args[0][1]
        assert "<!-- strategy:report -->" in comment_body

    @patch("agents.router.RouterNode")
    def test_decompose_fails_if_no_subtasks_created(
        self, MockRouter, config, mock_client, parent_issue, sample_agents
    ):
        mock_router = MockRouter.return_value
        mock_router.execute.return_value = {
            "requires_decomposition": True,
            "sub_tasks": [
                {"task_type": "code_generation", "specification": "s1"},
            ],
            "aggregation_strategy": "merge",
        }
        mock_client.get_children.return_value = []
        mock_client.list_agents.return_value = sample_agents
        mock_client.create_subtask.side_effect = PaperclipAPIError(500, "server error")

        result = run_orchestrator_heartbeat(config, mock_client, parent_issue)
        assert result.status == "failed"

    def test_decompose_fails_on_children_fetch_error(
        self, config, mock_client, parent_issue
    ):
        mock_client.get_children.side_effect = PaperclipAPIError(500, "error")

        result = run_orchestrator_heartbeat(config, mock_client, parent_issue)
        assert result.status == "failed"
        assert result.exit_code == 1

    @patch("agents.router.RouterNode")
    def test_decompose_fails_on_agent_discovery_error(
        self, MockRouter, config, mock_client, parent_issue
    ):
        mock_router = MockRouter.return_value
        mock_router.execute.return_value = {
            "requires_decomposition": True,
            "sub_tasks": [{"task_type": "code_generation", "specification": "s1"}],
            "aggregation_strategy": "merge",
        }
        mock_client.get_children.return_value = []
        mock_client.list_agents.side_effect = PaperclipAPIError(500, "error")

        result = run_orchestrator_heartbeat(config, mock_client, parent_issue)
        assert result.status == "failed"

    @patch("agents.router.RouterNode")
    def test_decompose_handles_empty_subtask_list(
        self, MockRouter, config, mock_client, parent_issue
    ):
        mock_router = MockRouter.return_value
        mock_router.execute.return_value = {
            "requires_decomposition": True,
            "sub_tasks": [],
            "aggregation_strategy": "merge",
        }
        mock_client.get_children.return_value = []

        with patch("agents.orchestrator._run_directly") as mock_direct:
            mock_direct.return_value = HeartbeatResult(status="success")
            run_orchestrator_heartbeat(config, mock_client, parent_issue)
            mock_direct.assert_called_once()


# ── Agent Matching Tests ──


class TestAgentMatching:
    """Tests for agent discovery and task_type → role matching."""

    def test_build_lookup_matches_keywords(self, sample_agents):
        lookup = _build_agent_lookup(sample_agents, "orchestrator-1")
        assert "code_generation" in lookup
        assert lookup["code_generation"].id == "code-agent"
        assert "test_generation" in lookup
        assert lookup["test_generation"].id == "test-agent"
        assert "security_audit" in lookup
        assert lookup["security_audit"].id == "security-agent"

    def test_build_lookup_excludes_self(self, sample_agents):
        lookup = _build_agent_lookup(sample_agents, "orchestrator-1")
        for agent in lookup.values():
            assert agent.id != "orchestrator-1"

    def test_build_lookup_excludes_inactive(self):
        agents = [
            AgentInfo(id="a1", company_id="c1", name="Bot", role="code_engineer", status="inactive"),
        ]
        lookup = _build_agent_lookup(agents, "other")
        assert len(lookup) == 0

    def test_match_agent_returns_id(self, sample_agents):
        lookup = _build_agent_lookup(sample_agents, "orchestrator-1")
        assert _match_agent("code_generation", lookup) == "code-agent"

    def test_match_agent_returns_none_for_unknown(self):
        lookup: dict = {}
        assert _match_agent("unknown_type", lookup) is None

    def test_find_agent_name_by_id(self, sample_agents):
        assert _find_agent_name(sample_agents, "code-agent") == "CodeBot"

    def test_find_agent_name_unassigned(self, sample_agents):
        assert _find_agent_name(sample_agents, None) == "unassigned"

    def test_find_agent_name_unknown_id(self, sample_agents):
        name = _find_agent_name(sample_agents, "unknown-id")
        assert name == "unknown-"  # First 8 chars


# ── Poll Tests ──


class TestPoll:
    """Tests for the POLL phase."""

    def test_poll_returns_idle_when_pending(
        self, config, mock_client, parent_issue
    ):
        children = [
            Issue(id="c1", title="[code] task", status="done"),
            Issue(id="c2", title="[test] task", status="in_progress"),
        ]
        mock_client.get_children.return_value = children

        result = run_orchestrator_heartbeat(config, mock_client, parent_issue)
        assert result.status == "idle"
        assert "1/2" in result.summary

    def test_poll_retries_blocked_child(
        self, config, mock_client, parent_issue
    ):
        children = [
            Issue(id="c1", title="[code] task", status="done"),
            Issue(id="c2", title="[test] task", status="blocked"),
        ]
        mock_client.get_children.return_value = children
        mock_client.get_comments.return_value = []  # No retry markers yet

        result = run_orchestrator_heartbeat(config, mock_client, parent_issue)

        # Should have retried the blocked child
        mock_client.update_issue.assert_any_call("c2", status="todo")
        assert result.status == "idle"  # Still waiting after retry

    def test_poll_blocks_parent_after_max_retries_all_failed(
        self, config, mock_client, parent_issue
    ):
        """When ALL children are blocked and exhausted, parent is blocked."""
        children = [
            Issue(id="c1", title="[code] task", status="blocked"),
            Issue(id="c2", title="[test] task", status="blocked"),
        ]
        mock_client.get_children.return_value = children
        # Both already retried once
        mock_client.get_comments.return_value = [
            Comment(id="cm1", body="<!-- retry:1 --> Orchestrator auto-retry"),
        ]

        result = run_orchestrator_heartbeat(config, mock_client, parent_issue)

        assert result.status == "blocked"
        assert "2 subtask" in result.summary

    def test_poll_skips_retry_when_disabled(
        self, config, mock_client, parent_issue
    ):
        config.paperclip.orchestrator_retry_failed = False
        children = [
            Issue(id="c1", title="[code] task", status="blocked"),
        ]
        mock_client.get_children.return_value = children

        result = run_orchestrator_heartbeat(config, mock_client, parent_issue)

        assert result.status == "blocked"
        # Should not have tried to update the child
        mock_client.update_issue.assert_called_once()  # Only parent blocked

    def test_poll_returns_success_when_all_done(
        self, config, mock_client, parent_issue, child_issues_done
    ):
        # First call to get_children returns all done, which triggers AGGREGATE
        # But AGGREGATE needs comments, so this tests the path via poll
        mock_client.get_children.return_value = child_issues_done
        mock_client.get_comments.return_value = [
            Comment(id="cm1", body="## Completed (score: 85/100)\n\nSome output"),
        ]

        result = run_orchestrator_heartbeat(config, mock_client, parent_issue)
        # Phase detected as AGGREGATE since all children are done
        assert result.status in ("success", "blocked")

    def test_poll_handles_multiple_blocked_children(
        self, config, mock_client, parent_issue
    ):
        children = [
            Issue(id="c1", title="[code] task", status="blocked"),
            Issue(id="c2", title="[test] task", status="blocked"),
        ]
        mock_client.get_children.return_value = children
        # Both already retried
        mock_client.get_comments.return_value = [
            Comment(id="cm1", body="<!-- retry:1 --> retried"),
        ]

        result = run_orchestrator_heartbeat(config, mock_client, parent_issue)
        assert result.status == "blocked"
        assert "2 subtask" in result.summary


# ── Aggregate Tests ──


class TestAggregate:
    """Tests for the AGGREGATE phase."""

    def test_aggregate_combines_child_outputs(
        self, config, mock_client, parent_issue, child_issues_done
    ):
        mock_client.get_children.return_value = child_issues_done
        # Each child has a result comment
        mock_client.get_comments.side_effect = [
            # Strategy comment on parent
            [Comment(id="plan", body="<!-- strategy:merge --> Plan")],
            # Child 1 result
            [Comment(id="r1", body="## Completed (score: 90/100)\n\nAPI code here")],
            # Child 2 result
            [Comment(id="r2", body="## Completed (score: 85/100)\n\nTest suite here")],
            # Child 3 result
            [Comment(id="r3", body="## Completed (score: 80/100)\n\nSecurity report here")],
        ]

        result = run_orchestrator_heartbeat(config, mock_client, parent_issue)

        assert result.status == "success"
        # Should have updated parent to done
        mock_client.update_issue.assert_called_once()
        update_call = mock_client.update_issue.call_args
        assert update_call[1].get("status") == "done" or update_call[0][1] == "done"

    def test_aggregate_uses_fallback_when_no_adapter(
        self, config, mock_client, parent_issue, child_issues_done
    ):
        mock_client.get_children.return_value = child_issues_done
        mock_client.get_comments.side_effect = [
            [Comment(id="plan", body="<!-- strategy:report --> Plan")],
            [Comment(id="r1", body="## Completed (score: 90/100)\n\nCode output")],
            [Comment(id="r2", body="## Completed (score: 85/100)\n\nTest output")],
            [Comment(id="r3", body="## Completed (score: 80/100)\n\nSecurity output")],
        ]

        result = run_orchestrator_heartbeat(config, mock_client, parent_issue)

        assert result.status == "success"
        # Check that the combined result was posted
        comment_body = mock_client.update_issue.call_args[1].get("comment", "")
        assert "Combined Result" in comment_body

    def test_aggregate_blocks_when_no_outputs(
        self, config, mock_client, parent_issue, child_issues_done
    ):
        mock_client.get_children.return_value = child_issues_done
        # No result comments found
        mock_client.get_comments.side_effect = [
            [Comment(id="plan", body="<!-- strategy:merge --> Plan")],
            [],
            [],
            [],
        ]

        result = run_orchestrator_heartbeat(config, mock_client, parent_issue)

        assert result.status == "blocked"
        assert "No child outputs" in result.summary

    def test_aggregate_handles_partial_outputs(
        self, config, mock_client, parent_issue, child_issues_done
    ):
        mock_client.get_children.return_value = child_issues_done
        mock_client.get_comments.side_effect = [
            [Comment(id="plan", body="<!-- strategy:merge --> Plan")],
            [Comment(id="r1", body="## Completed (score: 90/100)\n\nCode output")],
            [],  # No result for child 2
            [Comment(id="r3", body="## Completed (score: 80/100)\n\nSecurity output")],
        ]

        result = run_orchestrator_heartbeat(config, mock_client, parent_issue)

        assert result.status == "success"
        # Should aggregate what's available (2 out of 3)

    def test_aggregate_fails_on_update_error(
        self, config, mock_client, parent_issue, child_issues_done
    ):
        mock_client.get_children.return_value = child_issues_done
        mock_client.get_comments.side_effect = [
            [Comment(id="plan", body="<!-- strategy:merge --> Plan")],
            [Comment(id="r1", body="## Completed (score: 90/100)\n\nOutput")],
            [Comment(id="r2", body="## Completed (score: 85/100)\n\nOutput")],
            [Comment(id="r3", body="## Completed (score: 80/100)\n\nOutput")],
        ]
        mock_client.update_issue.side_effect = PaperclipAPIError(500, "error")

        result = run_orchestrator_heartbeat(config, mock_client, parent_issue)
        assert result.status == "failed"


# ── Helper Tests ──


class TestHelpers:
    """Tests for utility functions."""

    def test_extract_task_type_from_title(self):
        assert _extract_task_type("[code_generation] Build API") == "code_generation"
        assert _extract_task_type("[test_generation] Build API") == "test_generation"
        assert _extract_task_type("[security_audit] Build API") == "security_audit"

    def test_extract_task_type_no_bracket(self):
        assert _extract_task_type("Build API") == "general"

    def test_extract_child_result_with_score(self, mock_client):
        child = Issue(id="c1", title="task", status="done")
        mock_client.get_comments.return_value = [
            Comment(id="cm1", body="some log"),
            Comment(id="cm2", body="## Completed (score: 92/100)\n\nThe actual output content"),
        ]

        output, score = _extract_child_result(mock_client, child)
        assert score == 92
        assert "actual output content" in output

    def test_extract_child_result_no_match_logs_warning(self, mock_client, caplog):
        """Missing result comment should log a warning."""
        child = Issue(id="c1", title="[code] task", status="done")
        mock_client.get_comments.return_value = [
            Comment(id="cm1", body="random comment"),
        ]

        import logging
        with caplog.at_level(logging.WARNING):
            output, score = _extract_child_result(mock_client, child)

        assert output == ""
        assert score == 0
        assert "no result comment" in caplog.text.lower()

    def test_extract_child_result_api_error(self, mock_client):
        child = Issue(id="c1", title="task", status="done")
        mock_client.get_comments.side_effect = PaperclipAPIError(500, "error")

        output, score = _extract_child_result(mock_client, child)
        assert output == ""
        assert score == 0

    def test_extract_strategy_from_comment(self, mock_client):
        issue = Issue(id="i1", title="test")
        mock_client.get_comments.return_value = [
            Comment(id="cm1", body="<!-- strategy:report --> Plan"),
        ]

        assert _extract_strategy(mock_client, issue) == "report"

    def test_extract_strategy_defaults_to_merge(self, mock_client):
        issue = Issue(id="i1", title="test")
        mock_client.get_comments.return_value = []

        assert _extract_strategy(mock_client, issue) == "merge"

    def test_extract_strategy_handles_api_error(self, mock_client):
        issue = Issue(id="i1", title="test")
        mock_client.get_comments.side_effect = PaperclipAPIError(500, "error")

        assert _extract_strategy(mock_client, issue) == "merge"

    def test_count_retries_none(self):
        assert _count_retries([]) == 0

    def test_count_retries_one(self):
        comments = [Comment(id="c1", body="<!-- retry:1 --> retried")]
        assert _count_retries(comments) == 1

    def test_count_retries_multiple(self):
        comments = [
            Comment(id="c1", body="<!-- retry:1 --> first"),
            Comment(id="c2", body="<!-- retry:2 --> second"),
        ]
        assert _count_retries(comments) == 2

    def test_count_retries_ignores_non_retry_comments(self):
        comments = [
            Comment(id="c1", body="normal comment"),
            Comment(id="c2", body="<!-- retry:1 --> retried"),
        ]
        assert _count_retries(comments) == 1


# ── Retry Logic Tests ──


class TestMaybeRetryChild:
    """Tests for the merged _maybe_retry_child()."""

    def test_retries_when_under_limit(self):
        client = MagicMock(spec=PaperclipClient)
        child = Issue(id="c1", title="task", status="blocked")
        client.get_comments.return_value = []  # No retries yet

        result = _maybe_retry_child(client, child, max_retries=1)

        assert result is True
        client.add_comment.assert_called_once()
        client.update_issue.assert_called_once_with("c1", status="todo")
        # Only ONE get_comments call (no double fetch)
        client.get_comments.assert_called_once_with("c1")

    def test_returns_false_when_exhausted(self):
        client = MagicMock(spec=PaperclipClient)
        child = Issue(id="c1", title="task", status="blocked")
        client.get_comments.return_value = [
            Comment(id="cm1", body="<!-- retry:1 --> retried"),
        ]

        result = _maybe_retry_child(client, child, max_retries=1)

        assert result is False
        client.add_comment.assert_not_called()
        client.update_issue.assert_not_called()

    def test_treats_api_error_as_pending(self):
        """Transient API error fetching comments should not permanently fail the child."""
        client = MagicMock(spec=PaperclipClient)
        child = Issue(id="c1", title="task", status="blocked")
        client.get_comments.side_effect = PaperclipAPIError(503, "Service unavailable")

        result = _maybe_retry_child(client, child, max_retries=1)

        assert result is True  # Conservative: treat as still pending
        client.add_comment.assert_not_called()

    def test_status_update_failure_returns_false(self):
        """If update_issue fails, child stays blocked — return False (permanently failed)."""
        client = MagicMock(spec=PaperclipClient)
        child = Issue(id="c1", title="task", status="blocked")
        client.get_comments.return_value = []
        client.update_issue.side_effect = PaperclipAPIError(500, "Server error")

        result = _maybe_retry_child(client, child, max_retries=1)

        assert result is False  # Permanently failed — status never changed
        client.add_comment.assert_not_called()  # Comment not attempted

    def test_comment_failure_after_status_update_returns_true(self):
        """If status update succeeds but comment fails, retry still happened."""
        client = MagicMock(spec=PaperclipClient)
        child = Issue(id="c1", title="task", status="blocked")
        client.get_comments.return_value = []
        client.update_issue.return_value = None  # Success
        client.add_comment.side_effect = PaperclipAPIError(500, "Comment failed")

        result = _maybe_retry_child(client, child, max_retries=1)

        assert result is True  # Retry happened (status changed)
        client.update_issue.assert_called_once_with("c1", status="todo")


# ── Decompose Status Transition Tests ──


class TestDecomposeStatusTransition:
    """Tests for Fix 4: parent set to in_progress after DECOMPOSE."""

    @patch("agents.router.RouterNode")
    def test_decompose_sets_parent_in_progress(
        self, MockRouter, config, mock_client, parent_issue, sample_agents
    ):
        mock_router = MockRouter.return_value
        mock_router.execute.return_value = {
            "requires_decomposition": True,
            "sub_tasks": [
                {"task_type": "code_generation", "specification": "Build API"},
            ],
            "aggregation_strategy": "merge",
        }
        mock_client.get_children.return_value = []
        mock_client.list_agents.return_value = sample_agents
        mock_client.create_subtask.return_value = Issue(id="new-1", title="sub")

        run_orchestrator_heartbeat(config, mock_client, parent_issue)

        # Parent should be set to in_progress
        mock_client.update_issue.assert_called_once_with(
            parent_issue.id, status="in_progress"
        )

    @patch("agents.router.RouterNode")
    def test_decompose_succeeds_even_if_status_update_fails(
        self, MockRouter, config, mock_client, parent_issue, sample_agents
    ):
        mock_router = MockRouter.return_value
        mock_router.execute.return_value = {
            "requires_decomposition": True,
            "sub_tasks": [
                {"task_type": "code_generation", "specification": "Build API"},
            ],
            "aggregation_strategy": "merge",
        }
        mock_client.get_children.return_value = []
        mock_client.list_agents.return_value = sample_agents
        mock_client.create_subtask.return_value = Issue(id="new-1", title="sub")
        mock_client.update_issue.side_effect = PaperclipAPIError(500, "error")

        # Should still succeed despite status update failure
        result = run_orchestrator_heartbeat(config, mock_client, parent_issue)
        assert result.status == "success"


# ── Heartbeat Integration Tests ──


class TestHeartbeatIntegration:
    """Tests for orchestrator detection in heartbeat.py."""

    @patch("agents.heartbeat._create_client")
    @patch("agents.heartbeat.run_heartbeat")
    def test_heartbeat_routes_to_orchestrator(self, mock_heartbeat, mock_create):
        """Verify orchestrator task_type triggers orchestrator path."""
        # This is tested implicitly via the heartbeat code change,
        # but let's verify the import works
        from agents.orchestrator import run_orchestrator_heartbeat
        assert callable(run_orchestrator_heartbeat)

    def test_orchestrator_phase_enum_values(self):
        assert OrchestratorPhase.DECOMPOSE.value == "decompose"
        assert OrchestratorPhase.POLL.value == "poll"
        assert OrchestratorPhase.AGGREGATE.value == "aggregate"

    def test_heartbeat_result_format(self, config, mock_client, parent_issue):
        """Orchestrator returns standard HeartbeatResult."""
        mock_client.get_children.return_value = []
        mock_client.list_agents.return_value = []

        with patch("agents.router.RouterNode") as MockRouter:
            mock_router = MockRouter.return_value
            mock_router.execute.return_value = {"requires_decomposition": False}

            with patch("agents.orchestrator._run_directly") as mock_direct:
                mock_direct.return_value = HeartbeatResult(
                    status="success", issue_id="parent-1", summary="Done"
                )
                result = run_orchestrator_heartbeat(config, mock_client, parent_issue)

        assert isinstance(result, HeartbeatResult)
        assert hasattr(result, "status")
        assert hasattr(result, "issue_id")
        assert hasattr(result, "summary")

    @patch("agents.router.RouterNode")
    def test_full_lifecycle_decompose_to_aggregate(
        self, MockRouter, config, mock_client, parent_issue, sample_agents
    ):
        """Simulate the full 3-phase lifecycle."""

        # Phase 1: DECOMPOSE
        mock_client.get_children.return_value = []
        mock_router = MockRouter.return_value
        mock_router.execute.return_value = {
            "requires_decomposition": True,
            "sub_tasks": [
                {"task_type": "code_generation", "specification": "Build API"},
                {"task_type": "test_generation", "specification": "Write tests"},
            ],
            "aggregation_strategy": "merge",
        }
        mock_client.list_agents.return_value = sample_agents
        mock_client.create_subtask.return_value = Issue(id="new-1", title="sub")

        result1 = run_orchestrator_heartbeat(config, mock_client, parent_issue)
        assert result1.status == "success"
        assert "2 subtasks" in result1.summary

        # Phase 2: POLL (children in progress)
        mock_client.get_children.return_value = [
            Issue(id="c1", title="[code_generation] Build API", status="in_progress"),
            Issue(id="c2", title="[test_generation] Build API", status="todo"),
        ]

        result2 = run_orchestrator_heartbeat(config, mock_client, parent_issue)
        assert result2.status == "idle"

        # Phase 3: AGGREGATE (all done)
        mock_client.get_children.return_value = [
            Issue(id="c1", title="[code_generation] Build API", status="done"),
            Issue(id="c2", title="[test_generation] Build API", status="done"),
        ]
        mock_client.get_comments.side_effect = [
            [Comment(id="plan", body="<!-- strategy:merge --> Plan")],
            [Comment(id="r1", body="## Completed (score: 90/100)\n\nAPI code")],
            [Comment(id="r2", body="## Completed (score: 85/100)\n\nTest suite")],
        ]

        result3 = run_orchestrator_heartbeat(config, mock_client, parent_issue)
        assert result3.status == "success"
        mock_client.update_issue.assert_called()


# ── Aggregation Registry Tests ──


class TestAggregationRegistry:
    """Tests for _create_aggregation_registry LLM path."""

    @patch("agents.llm_backend.create_backend_from_config")
    def test_creates_registry_with_adapter(self, mock_create_backend, config):
        """When backend is available, returns a registry with vibe adapter."""
        mock_backend = MagicMock()
        mock_create_backend.return_value = mock_backend

        registry = _create_aggregation_registry(config)

        assert registry is not None
        adapter = registry.get("vibe")
        assert adapter is not None
        assert adapter.name == "vibe"

    @patch("agents.llm_backend.create_backend_from_config")
    def test_returns_none_on_backend_failure(self, mock_create_backend, config):
        """When backend creation fails, returns None (fallback to concatenation)."""
        mock_create_backend.side_effect = RuntimeError("No vLLM server")

        registry = _create_aggregation_registry(config)

        assert registry is None

    def test_returns_none_gracefully(self, config):
        """Without a running LLM backend, returns None (no crash)."""
        # In test env, create_backend_from_config will fail since no vLLM server
        # The function should catch the exception and return None
        registry = _create_aggregation_registry(config)
        # May be None (no backend) or a registry (if backend available in test env)
        # The important thing is it doesn't crash
        assert registry is None or hasattr(registry, "get")


# ── Retry After Seconds Tests ──


class TestRetryAfterSeconds:
    """Tests for retry_after_seconds hint in POLL idle responses."""

    def test_poll_idle_includes_retry_after(self, config, mock_client, parent_issue):
        """POLL idle response should include retry_after_seconds=30."""
        children = [
            Issue(id="c1", title="[code] task", status="done"),
            Issue(id="c2", title="[test] task", status="in_progress"),
        ]
        mock_client.get_children.return_value = children

        result = run_orchestrator_heartbeat(config, mock_client, parent_issue)

        assert result.status == "idle"
        assert result.retry_after_seconds == 30

    def test_heartbeat_result_serializes_retry_after(self):
        """retry_after_seconds should appear in JSON output."""
        import json
        result = HeartbeatResult(
            status="idle", issue_id="test-1",
            summary="Waiting", retry_after_seconds=30,
        )
        data = json.loads(result.to_json())
        assert data["retry_after_seconds"] == 30

    def test_heartbeat_result_omits_retry_after_when_none(self):
        """retry_after_seconds should be null in JSON when not set."""
        import json
        result = HeartbeatResult(status="success", issue_id="test-1")
        data = json.loads(result.to_json())
        assert data["retry_after_seconds"] is None

    def test_non_idle_responses_have_no_retry_after(
        self, config, mock_client, parent_issue, child_issues_done
    ):
        """Success/failed responses should not set retry_after_seconds."""
        mock_client.get_children.return_value = child_issues_done
        mock_client.get_comments.side_effect = [
            [Comment(id="plan", body="<!-- strategy:merge --> Plan")],
            [Comment(id="r1", body="## Completed (score: 90/100)\n\nCode")],
            [Comment(id="r2", body="## Completed (score: 85/100)\n\nTests")],
            [Comment(id="r3", body="## Completed (score: 80/100)\n\nSecurity")],
        ]

        result = run_orchestrator_heartbeat(config, mock_client, parent_issue)

        assert result.status == "success"
        assert result.retry_after_seconds is None


# ── Partial Failure Aggregation Tests ──


class TestPartialFailureAggregation:
    """Tests for _aggregate_with_partial_failures."""

    @pytest.fixture
    def config(self):
        cfg = SystemConfig()
        cfg.paperclip = PaperclipConfig(
            enabled=True, api_url="http://localhost:3100", api_key="test-key",
            orchestrator_max_children=5, orchestrator_retry_failed=True,
            orchestrator_max_retries=1,
        )
        cfg.spending.enabled = False
        return cfg

    @pytest.fixture
    def mock_client(self):
        client = MagicMock(spec=PaperclipClient)
        client.agent_id = "orchestrator-1"
        client.company_id = "company-1"
        return client

    def test_aggregates_succeeded_children_only(self, config, mock_client):
        """Should aggregate outputs from succeeded children, ignoring failed ones."""
        parent = Issue(id="p1", title="Build API", status="in_progress")
        children = [
            Issue(id="c1", title="[code_generation] Build API", status="done"),
            Issue(id="c2", title="[test_generation] Build API", status="done"),
            Issue(id="c3", title="[security_audit] Build API", status="blocked"),
        ]
        permanently_failed = [children[2]]

        # Strategy comment on parent
        mock_client.get_comments.side_effect = [
            [Comment(id="plan", body="<!-- strategy:merge --> Plan")],
            [Comment(id="r1", body="## Completed (score: 90/100)\n\nCode output")],
            [Comment(id="r2", body="## Completed (score: 85/100)\n\nTest output")],
        ]

        result = _aggregate_with_partial_failures(
            config, mock_client, parent, children, permanently_failed,
        )

        assert result.status == "success"
        assert "2/3" in result.summary
        assert "1 failed" in result.summary

    def test_posts_failure_section_in_comment(self, config, mock_client):
        """Result comment should include failed subtask details."""
        parent = Issue(id="p1", title="Build API", status="in_progress")
        children = [
            Issue(id="c1", title="[code_generation] Build API", status="done"),
            Issue(id="c2", title="[security_audit] Audit API", status="blocked"),
        ]
        permanently_failed = [children[1]]

        mock_client.get_comments.side_effect = [
            [Comment(id="plan", body="<!-- strategy:merge --> Plan")],
            [Comment(id="r1", body="## Completed (score: 90/100)\n\nCode output")],
        ]

        _aggregate_with_partial_failures(
            config, mock_client, parent, children, permanently_failed,
        )

        comment = mock_client.update_issue.call_args[1].get("comment", "")
        assert "Failed Subtasks" in comment
        assert "security_audit" in comment

    def test_blocks_when_all_outputs_empty(self, config, mock_client):
        """If succeeded children have no extractable output, block the parent."""
        parent = Issue(id="p1", title="Build API", status="in_progress")
        children = [
            Issue(id="c1", title="[code] Build API", status="done"),
            Issue(id="c2", title="[test] Test API", status="blocked"),
        ]
        permanently_failed = [children[1]]

        mock_client.get_comments.side_effect = [
            [Comment(id="plan", body="<!-- strategy:merge --> Plan")],
            [],  # No result for c1
        ]

        result = _aggregate_with_partial_failures(
            config, mock_client, parent, children, permanently_failed,
        )

        assert result.status == "blocked"
        assert result.exit_code == 1

    def test_poll_triggers_partial_aggregation(self, config, mock_client):
        """POLL phase with done + permanently failed children should aggregate partial results."""
        parent = Issue(
            id="p1", title="Build API", status="in_progress",
            assignee_agent_id="orchestrator-1",
        )
        children = [
            Issue(id="c1", title="[code_generation] Build API", status="done"),
            Issue(id="c2", title="[test_generation] Write tests", status="blocked"),
        ]
        mock_client.get_children.return_value = children

        # c2 is blocked and already retried (exhausted)
        def get_comments_side_effect(issue_id):
            if issue_id == "c2":
                return [Comment(id="cm1", body="<!-- retry:1 --> retried")]
            if issue_id == "p1":
                return [Comment(id="plan", body="<!-- strategy:merge --> Plan")]
            # c1 result
            return [Comment(id="r1", body="## Completed (score: 88/100)\n\nCode here")]

        mock_client.get_comments.side_effect = get_comments_side_effect

        result = run_orchestrator_heartbeat(config, mock_client, parent)

        assert result.status == "success"
        assert "1/2" in result.summary

    def test_update_issue_failure_returns_failed(self, config, mock_client):
        """If posting partial result fails, return failed status."""
        parent = Issue(id="p1", title="Build API", status="in_progress")
        children = [
            Issue(id="c1", title="[code_generation] Build API", status="done"),
            Issue(id="c2", title="[security_audit] Audit", status="blocked"),
        ]
        permanently_failed = [children[1]]

        mock_client.get_comments.side_effect = [
            [Comment(id="plan", body="<!-- strategy:merge --> Plan")],
            [Comment(id="r1", body="## Completed (score: 90/100)\n\nOutput")],
        ]
        mock_client.update_issue.side_effect = PaperclipAPIError(500, "server error")

        result = _aggregate_with_partial_failures(
            config, mock_client, parent, children, permanently_failed,
        )

        assert result.status == "failed"
        assert result.exit_code == 1


# ── Partial Decomposition Warning Tests ──


class TestPartialDecomposition:
    """Tests for partial decomposition warning when some subtask creations fail."""

    @patch("agents.router.RouterNode")
    def test_partial_creation_warns_and_succeeds(
        self, MockRouter, config, mock_client, parent_issue, sample_agents, caplog
    ):
        """When some subtask creations fail, should warn but still succeed."""
        mock_router = MockRouter.return_value
        mock_router.execute.return_value = {
            "requires_decomposition": True,
            "sub_tasks": [
                {"task_type": "code_generation", "specification": "s1"},
                {"task_type": "test_generation", "specification": "s2"},
                {"task_type": "security_audit", "specification": "s3"},
            ],
            "aggregation_strategy": "merge",
        }
        mock_client.get_children.return_value = []
        mock_client.list_agents.return_value = sample_agents

        # First call succeeds, second fails, third succeeds
        mock_client.create_subtask.side_effect = [
            Issue(id="new-1", title="sub1"),
            PaperclipAPIError(500, "server error"),
            Issue(id="new-3", title="sub3"),
        ]

        import logging
        with caplog.at_level(logging.WARNING):
            result = run_orchestrator_heartbeat(config, mock_client, parent_issue)

        assert result.status == "success"
        assert "Partial decomposition" in caplog.text
        assert "2/3" in caplog.text


# ── Agent Matching Specificity Tests ──


class TestAgentMatchingSpecificity:
    """Tests for keyword specificity sorting in agent matching."""

    def test_code_reviewer_matches_review_not_code(self):
        """An agent with role 'code_reviewer' should match code_review, not code_generation."""
        agents = [
            AgentInfo(id="reviewer-1", company_id="c1", name="Reviewer",
                      role="code_reviewer", status="active"),
        ]
        lookup = _build_agent_lookup(agents, "other")

        # "review" (6 chars) should match before "code" (4 chars) due to sorting
        assert "code_review" in lookup
        assert lookup["code_review"].id == "reviewer-1"

    def test_security_analyst_matches_security_not_sec(self):
        """Agent with 'security' in role should match security_audit."""
        agents = [
            AgentInfo(id="sec-1", company_id="c1", name="SecBot",
                      role="security_specialist", status="active"),
        ]
        lookup = _build_agent_lookup(agents, "other")
        assert "security_audit" in lookup

    def test_database_admin_matches_database_not_data(self):
        """'database' (8 chars) should match before 'data' (4 chars)."""
        agents = [
            AgentInfo(id="db-1", company_id="c1", name="DBBot",
                      role="database_admin", status="active"),
        ]
        lookup = _build_agent_lookup(agents, "other")
        assert "database_operations" in lookup
        # "data" also matches since "database" contains "data",
        # but "database" keyword should have been processed first
        # and first-match wins via `if task_type not in lookup`

    def test_performance_engineer_matches_correctly(self):
        """Longer keywords like 'performance' should match their specific task type."""
        agents = [
            AgentInfo(id="perf-1", company_id="c1", name="PerfBot",
                      role="performance_engineer", status="active"),
        ]
        lookup = _build_agent_lookup(agents, "other")
        assert "performance_optimization" in lookup
        assert lookup["performance_optimization"].id == "perf-1"

    def test_mixed_agents_all_match_correctly(self):
        """Multiple agents with overlapping keywords should each match their best type."""
        agents = [
            AgentInfo(id="code-1", company_id="c1", name="CodeBot",
                      role="code_engineer", status="active"),
            AgentInfo(id="review-1", company_id="c1", name="ReviewBot",
                      role="code_reviewer", status="active"),
        ]
        lookup = _build_agent_lookup(agents, "other")

        # code_engineer should match code_generation
        assert lookup.get("code_generation", MagicMock()).id in ("code-1", "review-1")
        # code_reviewer should match code_review
        assert "code_review" in lookup
        assert lookup["code_review"].id == "review-1"

    def test_title_matching_respects_specificity(self):
        """Title-based matching should also use longest-first keyword order."""
        agents = [
            AgentInfo(id="a1", company_id="c1", name="Bot",
                      role="generic_worker", title="Code Review Specialist",
                      status="active"),
        ]
        lookup = _build_agent_lookup(agents, "other")
        assert "code_review" in lookup


# ── Orchestrator Clarification Pass-Through Tests ──


class TestOrchestratorClarification:
    """Tests for clarification reply being passed to orchestrator."""

    @patch("agents.orchestrator._run_directly")
    @patch("agents.router.RouterNode")
    def test_clarification_reply_injected_into_user_request(
        self, MockRouter, mock_run_directly, config, mock_client, parent_issue
    ):
        """Clarification reply should appear in the user_request passed to decomposition."""
        mock_router = MockRouter.return_value
        mock_router.execute.return_value = {"requires_decomposition": False}
        mock_run_directly.return_value = HeartbeatResult(status="success")
        mock_client.get_children.return_value = []

        run_orchestrator_heartbeat(
            config, mock_client, parent_issue,
            clarification_reply="Use PostgreSQL, not SQLite",
        )

        # _run_directly receives the user_request which should include clarification
        call_args = mock_run_directly.call_args
        user_request_arg = call_args[0][3] if len(call_args[0]) > 3 else call_args[1].get("user_request", "")
        assert "Use PostgreSQL, not SQLite" in user_request_arg

    @patch("agents.router.RouterNode")
    def test_clarification_reply_none_by_default(
        self, MockRouter, config, mock_client, parent_issue, sample_agents
    ):
        """Without clarification, user_request has no clarification section."""
        mock_router = MockRouter.return_value
        mock_router.execute.return_value = {
            "requires_decomposition": True,
            "sub_tasks": [{"task_type": "code_generation", "specification": "s1"}],
            "aggregation_strategy": "merge",
        }
        mock_client.get_children.return_value = []
        mock_client.list_agents.return_value = sample_agents
        mock_client.create_subtask.return_value = Issue(id="new-1", title="sub")

        run_orchestrator_heartbeat(config, mock_client, parent_issue)

        # No "[Clarification from human]" in any create_subtask call
        for call_obj in mock_client.create_subtask.call_args_list:
            desc = call_obj[1].get("description", "") or call_obj[0][2] if len(call_obj[0]) > 2 else ""
            assert "Clarification from human" not in str(desc)


# ── Aggregation Registry Health Check Tests ──


class TestAggregationRegistryHealthCheck:
    """Tests for backend health check in _create_aggregation_registry."""

    @patch("agents.llm_backend.create_backend_from_config")
    def test_unhealthy_backend_returns_none(self, mock_create_backend, config):
        """Backend that fails health check should return None."""
        mock_backend = MagicMock()
        mock_backend.health_check.return_value = False
        mock_create_backend.return_value = mock_backend

        registry = _create_aggregation_registry(config)

        assert registry is None
        mock_backend.health_check.assert_called_once()

    @patch("agents.llm_backend.create_backend_from_config")
    def test_healthy_backend_returns_registry(self, mock_create_backend, config):
        """Backend that passes health check should return a registry."""
        mock_backend = MagicMock()
        mock_backend.health_check.return_value = True
        mock_create_backend.return_value = mock_backend

        registry = _create_aggregation_registry(config)

        assert registry is not None
        assert registry.get("vibe") is not None

    @patch("agents.llm_backend.create_backend_from_config")
    def test_health_check_exception_returns_none(self, mock_create_backend, config):
        """Health check that raises should be caught gracefully."""
        mock_backend = MagicMock()
        mock_backend.health_check.side_effect = ConnectionError("refused")
        mock_create_backend.return_value = mock_backend

        registry = _create_aggregation_registry(config)

        assert registry is None


# ── Aggregator LLM Threshold Tests ──


class TestAggregatorLLMThreshold:
    """Tests for the LLM output acceptance threshold in AggregatorNode."""

    def _make_aggregator_with_mock(self, generate_return):
        """Create an AggregatorNode with a mock LLM adapter."""
        from agents.aggregator import AggregatorNode
        from agents.adapters import AdapterRegistry

        mock_adapter = MagicMock()
        mock_adapter.name = "vibe"
        mock_adapter.generate.return_value = generate_return

        registry = MagicMock(spec=AdapterRegistry)
        registry.get.return_value = mock_adapter
        registry.list_adapters.return_value = ["vibe"]

        return AggregatorNode(adapter_registry=registry), mock_adapter

    def _make_state(self):
        return {
            "sub_tasks": [{
                "task_type": "code_generation",
                "specialist_adapter": "code",
                "specification": "Build API",
                "output": "def hello(): pass",
                "output_score": 90,
                "status": "completed",
            }],
            "aggregation_strategy": "merge",
            "user_request": "Build API",
            "specification": "Build API",
            "adapters_used": [],
        }

    def test_accepts_short_but_substantive_response(self):
        """A 20-char response should be accepted (above 10-char floor)."""
        aggregator, adapter = self._make_aggregator_with_mock("Here is the result.")
        state = self._make_state()

        result = aggregator.execute(state)

        # Should use the LLM output, not the fallback
        assert result["aggregated_output"] == "Here is the result."
        adapter.generate.assert_called_once()

    def test_rejects_trivial_response(self):
        """A response like 'OK' or 'Done.' should trigger fallback."""
        aggregator, adapter = self._make_aggregator_with_mock("Done.")
        state = self._make_state()

        result = aggregator.execute(state)

        # Should NOT be "Done." — should be fallback concatenation
        assert result["aggregated_output"] != "Done."
        assert "Build API" in result["aggregated_output"]

    def test_rejects_empty_response(self):
        """Empty/whitespace response should trigger fallback."""
        aggregator, adapter = self._make_aggregator_with_mock("   ")
        state = self._make_state()

        result = aggregator.execute(state)

        assert "Build API" in result["aggregated_output"]

    def test_accepts_medium_length_response(self):
        """A 60-char response that was previously rejected (>50 threshold) is now accepted."""
        response = "The API implementation is complete with all endpoints."
        aggregator, adapter = self._make_aggregator_with_mock(response)
        state = self._make_state()

        result = aggregator.execute(state)

        assert result["aggregated_output"] == response


# ── _run_directly Clarification + Cost Reporting Tests ──


class TestRunDirectly:
    """Tests for _run_directly() clarification handling and cost reporting."""

    @pytest.fixture
    def config(self):
        cfg = SystemConfig()
        cfg.paperclip = PaperclipConfig(
            enabled=True, api_url="http://localhost:3100", api_key="test-key",
            cost_reporting=True,
        )
        cfg.spending.enabled = False
        return cfg

    @pytest.fixture
    def mock_client(self):
        client = MagicMock(spec=PaperclipClient)
        client.agent_id = "orchestrator-1"
        return client

    @patch("agents.heartbeat._run_workflow")
    def test_clarification_returned_when_needed(self, mock_run_workflow, config, mock_client):
        """_run_directly should return clarification_needed if workflow requests it."""
        mock_run_workflow.return_value = {
            "clarification_needed": True,
            "clarification_questions": ["Which database?", "REST or GraphQL?"],
            "last_node": "vibe",
            "specification": "Build an API",
        }
        issue = Issue(id="p1", title="Build API", status="in_progress")

        result = _run_directly(config, mock_client, issue, "Build API")

        assert result.status == "clarification_needed"
        assert result.clarification is not None
        assert len(result.clarification["questions"]) == 2
        # Should block the issue in Paperclip
        mock_client.update_issue.assert_called_once()
        update_kwargs = mock_client.update_issue.call_args[1]
        assert update_kwargs.get("status") == "blocked"

    @patch("agents.heartbeat._run_workflow")
    def test_cost_reported_on_success(self, mock_run_workflow, config, mock_client):
        """_run_directly should report costs to Paperclip on success."""
        mock_run_workflow.return_value = {
            "final_output": "API code here",
            "final_score": 90,
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
        }
        issue = Issue(id="p1", title="Build API", status="in_progress")

        result = _run_directly(config, mock_client, issue, "Build API")

        assert result.status == "success"
        mock_client.report_cost.assert_called_once()
        cost_kwargs = mock_client.report_cost.call_args[1]
        assert cost_kwargs["input_tokens"] == 1000
        assert cost_kwargs["output_tokens"] == 500

    @patch("agents.heartbeat._run_workflow")
    def test_cost_not_reported_when_disabled(self, mock_run_workflow, config, mock_client):
        """Cost reporting should be skipped when config disables it."""
        config.paperclip.cost_reporting = False
        mock_run_workflow.return_value = {
            "final_output": "output",
            "final_score": 90,
        }
        issue = Issue(id="p1", title="Build API", status="in_progress")

        _run_directly(config, mock_client, issue, "Build API")

        mock_client.report_cost.assert_not_called()

    @patch("agents.heartbeat._run_workflow")
    def test_provider_and_model_set(self, mock_run_workflow, config, mock_client):
        """HeartbeatResult should include provider and model metadata."""
        mock_run_workflow.return_value = {
            "final_output": "output",
            "final_score": 90,
        }
        issue = Issue(id="p1", title="Build API", status="in_progress")

        result = _run_directly(config, mock_client, issue, "Build API")

        assert result.provider == config.model.backend
        assert result.model == config.model.model_name


# ── AGGREGATE Idempotency Tests ──


class TestAggregateIdempotency:
    """Tests for idempotency guard in AGGREGATE phase."""

    def test_skips_aggregation_if_parent_already_done(self, config, mock_client):
        """If parent issue is already done, aggregation should be skipped."""
        parent = Issue(id="p1", title="Build API", status="done",
                       assignee_agent_id="orchestrator-1")
        children = [
            Issue(id="c1", title="[code] Build API", status="done"),
            Issue(id="c2", title="[test] Test API", status="done"),
        ]
        mock_client.get_children.return_value = children

        result = run_orchestrator_heartbeat(config, mock_client, parent)

        assert result.status == "success"
        assert "idempotent" in result.summary.lower()
        # Should NOT have posted any comments or updates
        mock_client.update_issue.assert_not_called()
        mock_client.add_comment.assert_not_called()

    def test_aggregates_normally_if_parent_in_progress(
        self, config, mock_client, child_issues_done
    ):
        """Normal aggregation should proceed when parent is in_progress."""
        parent = Issue(id="p1", title="Build API", status="in_progress",
                       assignee_agent_id="orchestrator-1")
        mock_client.get_children.return_value = child_issues_done
        mock_client.get_comments.side_effect = [
            [Comment(id="plan", body="<!-- strategy:merge --> Plan")],
            [Comment(id="r1", body="## Completed (score: 90/100)\n\nCode")],
            [Comment(id="r2", body="## Completed (score: 85/100)\n\nTests")],
            [Comment(id="r3", body="## Completed (score: 80/100)\n\nSecurity")],
        ]

        result = run_orchestrator_heartbeat(config, mock_client, parent)

        assert result.status == "success"
        assert "idempotent" not in result.summary.lower()
        mock_client.update_issue.assert_called_once()


# ── Dedup Tests ──


def test_decompose_skips_duplicate_titles(monkeypatch):
    """DECOMPOSE should skip subtasks whose titles match existing children."""
    from agents.orchestrator import _normalize_subtask_title, _filter_duplicate_subtasks
    from agents.paperclip_client import Issue

    existing_children = [
        Issue(id="c1", title="[code_generation] Build API", status="in_progress",
              description="", ancestors=[], goal_id=""),
        Issue(id="c2", title="[test_generation] Build API", status="todo",
              description="", ancestors=[], goal_id=""),
    ]

    proposed = [
        {"task_type": "code_generation", "specification": "build api"},
        {"task_type": "security_audit", "specification": "audit api"},
        {"task_type": "test_generation", "specification": "test api"},  # duplicate
    ]

    filtered = _filter_duplicate_subtasks(proposed, existing_children, "Build API")
    # code_generation matches c1, test_generation matches c2
    assert len(filtered) == 1
    assert filtered[0]["task_type"] == "security_audit"


def test_rebalance_reassigns_from_backlogged_to_idle():
    """_rebalance_children should reassign pending tasks from overloaded agents to idle ones."""
    from agents.orchestrator import _rebalance_children
    from agents.paperclip_client import Issue, AgentInfo

    children = [
        # Agent A: 1 done, 3 pending = backlogged
        Issue(id="c1", title="[code] T1", status="done", description="", ancestors=[], goal_id="", assignee_agent_id="agent-a"),
        Issue(id="c2", title="[code] T2", status="todo", description="", ancestors=[], goal_id="", assignee_agent_id="agent-a"),
        Issue(id="c3", title="[code] T3", status="todo", description="", ancestors=[], goal_id="", assignee_agent_id="agent-a"),
        Issue(id="c4", title="[code] T4", status="todo", description="", ancestors=[], goal_id="", assignee_agent_id="agent-a"),
        # Agent B: 1 done, 0 pending = idle
        Issue(id="c5", title="[test] T5", status="done", description="", ancestors=[], goal_id="", assignee_agent_id="agent-b"),
    ]

    agents = [
        AgentInfo(id="agent-a", name="UX Engineer", company_id="co-1", role="engineer", title="UX Engineer", status="active"),
        AgentInfo(id="agent-b", name="Backend Engineer", company_id="co-1", role="engineer", title="Backend Engineer", status="active"),
    ]

    reassigned = []

    class MockClient:
        def update_issue(self, issue_id, **kwargs):
            reassigned.append((issue_id, kwargs))

        def add_comment(self, issue_id, body):
            pass

    result = _rebalance_children(MockClient(), children, agents)
    assert result >= 1, "Should reassign at least 1 task"
    assert any(r[1].get("assignee_agent_id") == "agent-b" for r in reassigned)
