"""
Tests for complexity triage integration with the orchestration layer.

Covers:
- Orchestrator triage before decomposition
- Fast-tier decomposition skip
- Tier embedding in child issue descriptions
- Heartbeat complexity_tier extraction and forwarding
- classify_complexity pre-set guard
- run_workflow / stream_workflow triage integration
"""

import os
import re
from unittest.mock import MagicMock, patch, call

import pytest

os.environ["GENESIA_DISABLE_REMOTE_SKILLS"] = "1"


# ===== classify_complexity pre-set guard =====


class TestPreSetGuard:
    """Test that classify_complexity respects pre-set tier."""

    def test_skips_when_tier_preset(self):
        from agents.complexity_triage import classify_complexity
        state = {
            "user_request": "Build a comprehensive production-ready API",
            "intent": "code_generation",
            "quality_threshold": 85,
            "complexity_tier": "fast",
            "effective_quality_threshold": 70,
        }
        result = classify_complexity(state)
        assert result["complexity_tier"] == "fast"
        assert result["effective_quality_threshold"] == 70

    def test_runs_when_tier_empty(self):
        from agents.complexity_triage import classify_complexity
        state = {
            "user_request": "Add a docstring to `foo()`",
            "intent": "code_generation",
            "quality_threshold": 85,
            "complexity_tier": "",
            "effective_quality_threshold": 85,
        }
        result = classify_complexity(state)
        assert result["complexity_tier"] in ("fast", "standard", "full")

    def test_runs_when_tier_missing(self):
        from agents.complexity_triage import classify_complexity
        state = {
            "user_request": "Add a docstring to `foo()`",
            "intent": "code_generation",
            "quality_threshold": 85,
        }
        result = classify_complexity(state)
        assert result["complexity_tier"] in ("fast", "standard", "full")

    def test_preset_full_not_overridden(self):
        from agents.complexity_triage import classify_complexity
        state = {
            "user_request": "Fix typo in `main.py`",
            "intent": "code_generation",
            "quality_threshold": 85,
            "complexity_tier": "full",
            "effective_quality_threshold": 85,
        }
        result = classify_complexity(state)
        # Should stay full even though the request looks fast
        assert result["complexity_tier"] == "full"
        assert result["effective_quality_threshold"] == 85


# ===== Heartbeat _extract_complexity_hint =====


class TestExtractComplexityHint:
    """Test extraction of complexity tier from issue descriptions."""

    def test_extracts_fast(self):
        from agents.heartbeat import _extract_complexity_hint
        assert _extract_complexity_hint("<!-- complexity:fast -->\nSome spec") == "fast"

    def test_extracts_standard(self):
        from agents.heartbeat import _extract_complexity_hint
        assert _extract_complexity_hint("<!-- complexity:standard -->\nSpec") == "standard"

    def test_extracts_full(self):
        from agents.heartbeat import _extract_complexity_hint
        assert _extract_complexity_hint("<!-- complexity:full -->\nSpec") == "full"

    def test_returns_empty_for_no_hint(self):
        from agents.heartbeat import _extract_complexity_hint
        assert _extract_complexity_hint("Just a normal description") == ""

    def test_returns_empty_for_none(self):
        from agents.heartbeat import _extract_complexity_hint
        assert _extract_complexity_hint(None) == ""

    def test_returns_empty_for_empty_string(self):
        from agents.heartbeat import _extract_complexity_hint
        assert _extract_complexity_hint("") == ""


# ===== Heartbeat _run_workflow with complexity_tier =====


class TestRunWorkflowTier:
    """Test _run_workflow pre-seeds tier in initial state."""

    @patch("agents.workflow_factory.create_agent_graph")
    @patch("agents.workflow_factory.create_backend_from_config")
    def test_sets_tier_fast(self, mock_backend, mock_graph):
        from agents.heartbeat import _run_workflow
        from agents.config import SystemConfig

        mock_app = MagicMock()
        mock_app.stream.return_value = []
        mock_graph.return_value = mock_app

        config = SystemConfig()
        result = _run_workflow(config, "test", "", complexity_tier="fast")

        assert result["complexity_tier"] == "fast"
        assert result["effective_quality_threshold"] == 70

    @patch("agents.workflow_factory.create_agent_graph")
    @patch("agents.workflow_factory.create_backend_from_config")
    def test_sets_tier_standard(self, mock_backend, mock_graph):
        from agents.heartbeat import _run_workflow
        from agents.config import SystemConfig

        mock_app = MagicMock()
        mock_app.stream.return_value = []
        mock_graph.return_value = mock_app

        config = SystemConfig()
        result = _run_workflow(config, "test", "", complexity_tier="standard")

        assert result["complexity_tier"] == "standard"
        assert result["effective_quality_threshold"] == 75

    @patch("agents.workflow_factory.create_agent_graph")
    @patch("agents.workflow_factory.create_backend_from_config")
    def test_no_tier_when_empty(self, mock_backend, mock_graph):
        from agents.heartbeat import _run_workflow
        from agents.config import SystemConfig

        mock_app = MagicMock()
        mock_app.stream.return_value = []
        mock_graph.return_value = mock_app

        config = SystemConfig()
        result = _run_workflow(config, "test", "")

        # Should not be pre-set
        assert result["complexity_tier"] == ""
        assert result["effective_quality_threshold"] == 85


# ===== Orchestrator triage before decomposition =====


class TestOrchestratorTriage:
    """Test orchestrator runs triage before decomposition."""

    def _make_issue(self, title="Test", description=""):
        issue = MagicMock()
        issue.id = "ISSUE-1"
        issue.title = title
        issue.description = description
        issue.goal_id = None
        issue.status = "todo"
        return issue

    @patch("agents.orchestrator._run_directly")
    @patch("agents.router.RouterNode")
    def test_fast_skips_decomposition(self, mock_router_cls, mock_run_directly):
        from agents.orchestrator import _decompose_and_delegate
        from agents.config import SystemConfig

        config = SystemConfig()
        config.paperclip.orchestrator_skip_decomposition_for_fast = True
        client = MagicMock()
        issue = self._make_issue(title="Fix typo in `utils.py`")

        mock_run_directly.return_value = MagicMock(status="success")

        _decompose_and_delegate(config, client, issue)

        mock_run_directly.assert_called_once()
        call_kwargs = mock_run_directly.call_args
        assert call_kwargs[1].get("complexity_tier") == "fast" or \
            (len(call_kwargs[0]) > 4 and call_kwargs[0][4] == "fast")

    @patch("agents.orchestrator._run_directly")
    @patch("agents.router.RouterNode")
    def test_fast_respects_config_toggle(self, mock_router_cls, mock_run_directly):
        from agents.orchestrator import _decompose_and_delegate
        from agents.config import SystemConfig

        config = SystemConfig()
        config.paperclip.orchestrator_skip_decomposition_for_fast = False
        client = MagicMock()
        issue = self._make_issue(title="Fix typo in `utils.py`")

        # Router says no decomposition needed
        mock_router = MagicMock()
        mock_router.execute.return_value = {"requires_decomposition": False}
        mock_router_cls.return_value = mock_router

        mock_run_directly.return_value = MagicMock(status="success")

        _decompose_and_delegate(config, client, issue)

        # Router.execute should be called (not skipped)
        mock_router.execute.assert_called_once()

    @patch("agents.orchestrator._run_directly")
    @patch("agents.router.RouterNode")
    def test_full_goes_to_router(self, mock_router_cls, mock_run_directly):
        from agents.orchestrator import _decompose_and_delegate
        from agents.config import SystemConfig

        config = SystemConfig()
        client = MagicMock()
        # Long complex request → full tier
        issue = self._make_issue(
            title="Build a comprehensive production-ready REST API with authentication, "
                  "rate limiting, database migrations, comprehensive test suite, "
                  "Docker deployment, CI/CD pipeline, monitoring, and documentation"
        )

        mock_router = MagicMock()
        mock_router.execute.return_value = {"requires_decomposition": False}
        mock_router_cls.return_value = mock_router

        mock_run_directly.return_value = MagicMock(status="success")

        _decompose_and_delegate(config, client, issue)

        # Should reach router (not skip decomposition)
        mock_router.execute.assert_called_once()

    @patch("agents.router.RouterNode")
    def test_tier_embedded_in_child_description(self, mock_router_cls):
        from agents.orchestrator import _decompose_and_delegate
        from agents.config import SystemConfig

        config = SystemConfig()
        client = MagicMock()
        client.list_agents.return_value = []
        # Standard-tier request that decomposes
        issue = self._make_issue(
            title="Create a REST API endpoint for user management"
        )

        mock_router = MagicMock()
        mock_router.execute.return_value = {
            "requires_decomposition": True,
            "sub_tasks": [
                {"task_type": "code_generation", "specification": "Build the API"},
            ],
            "aggregation_strategy": "merge",
        }
        mock_router_cls.return_value = mock_router

        child_issue = MagicMock()
        child_issue.id = "CHILD-1"
        client.create_subtask.return_value = child_issue

        _decompose_and_delegate(config, client, issue)

        # Check that create_subtask was called with tier in description
        call_args = client.create_subtask.call_args
        description = call_args[1].get("description", call_args[0][1] if len(call_args[0]) > 1 else "")
        assert "<!-- complexity:" in description


# ===== PaperclipConfig field =====


class TestConfigField:
    """Test orchestrator_skip_decomposition_for_fast config field."""

    def test_default_true(self):
        from agents.config import PaperclipConfig
        config = PaperclipConfig()
        assert config.orchestrator_skip_decomposition_for_fast is True

    def test_can_set_false(self):
        from agents.config import PaperclipConfig
        config = PaperclipConfig(orchestrator_skip_decomposition_for_fast=False)
        assert config.orchestrator_skip_decomposition_for_fast is False
