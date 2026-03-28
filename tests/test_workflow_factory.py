"""
Tests for WorkflowFactory — cached backend/adapter setup.
"""

from unittest.mock import MagicMock, patch

import pytest

from agents.config import SystemConfig
from agents.workflow_factory import WorkflowFactory, _ADAPTER_DEFS


class TestWorkflowFactoryInit:
    """Tests for lazy initialisation."""

    def test_not_initialised_on_construction(self):
        """Factory should not initialise backend on construction."""
        config = SystemConfig()
        factory = WorkflowFactory(config)
        assert factory._initialised is False
        assert factory._base_model is None
        assert factory._adapter_registry is None

    @patch("agents.workflow_factory.create_agent_graph")
    @patch("agents.workflow_factory.create_backend_from_config")
    def test_initialised_on_first_run(self, mock_backend, mock_graph):
        """Factory should initialise on first run_workflow call."""
        mock_graph.return_value = MagicMock(stream=MagicMock(return_value=iter([])))
        config = SystemConfig()
        factory = WorkflowFactory(config)

        factory.run_workflow("test request", "code_generation")

        assert factory._initialised is True
        mock_backend.assert_called_once_with(config)

    @patch("agents.workflow_factory.create_agent_graph")
    @patch("agents.workflow_factory.create_backend_from_config")
    def test_backend_reused_across_calls(self, mock_backend, mock_graph):
        """Second run_workflow should NOT re-create the backend."""
        mock_graph.return_value = MagicMock(stream=MagicMock(return_value=iter([])))
        config = SystemConfig()
        factory = WorkflowFactory(config)

        factory.run_workflow("request 1", "code_generation")
        factory.run_workflow("request 2", "code_generation")

        # Backend created only once
        mock_backend.assert_called_once()
        # Graph created twice (per-run state)
        assert mock_graph.call_count == 2


class TestWorkflowFactoryAdapters:
    """Tests for adapter registration."""

    def test_adapter_defs_has_17_entries(self):
        """All 17 adapters should be defined."""
        assert len(_ADAPTER_DEFS) == 17

    @patch("agents.workflow_factory.create_agent_graph")
    @patch("agents.workflow_factory.create_backend_from_config")
    def test_all_adapters_registered(self, mock_backend, mock_graph):
        """All adapters in _ADAPTER_DEFS should be registered."""
        mock_graph.return_value = MagicMock(stream=MagicMock(return_value=iter([])))
        config = SystemConfig()
        factory = WorkflowFactory(config)

        factory.run_workflow("test", "code_generation")

        registered = factory._adapter_registry.list_adapters()
        for name, _ in _ADAPTER_DEFS:
            assert name in registered, f"Adapter '{name}' not registered"


class TestWorkflowFactoryClarification:
    """Tests for clarification resume via factory."""

    @patch("agents.workflow_factory.create_agent_graph")
    @patch("agents.workflow_factory.create_backend_from_config")
    def test_clarification_reply_clears_flags(self, mock_backend, mock_graph):
        """clarification_reply should clear clarification flags."""
        mock_compiled = MagicMock()
        mock_compiled.stream.return_value = iter([])
        mock_graph.return_value = mock_compiled

        config = SystemConfig()
        factory = WorkflowFactory(config)
        factory.run_workflow(
            "Task: Build auth\n[Clarification]: Use JWT",
            "code_generation",
            clarification_reply="Use JWT",
        )

        initial_state = mock_compiled.stream.call_args[0][0]
        assert initial_state["clarification_needed"] is False
        assert initial_state["clarification_questions"] == []

    @patch("agents.workflow_factory.create_agent_graph")
    @patch("agents.workflow_factory.create_backend_from_config")
    def test_no_clarification_leaves_spec_empty(self, mock_backend, mock_graph):
        """Without clarification_reply, specification stays empty."""
        mock_compiled = MagicMock()
        mock_compiled.stream.return_value = iter([])
        mock_graph.return_value = mock_compiled

        config = SystemConfig()
        factory = WorkflowFactory(config)
        factory.run_workflow("Build auth module", "code_generation")

        initial_state = mock_compiled.stream.call_args[0][0]
        assert initial_state.get("specification", "") == ""


class TestWorkflowFactoryProgress:
    """Tests for progress callback wiring."""

    @patch("agents.workflow_factory.create_agent_graph")
    @patch("agents.workflow_factory.create_backend_from_config")
    def test_progress_callback_fired(self, mock_backend, mock_graph):
        """Progress callback should fire for yielded node updates."""
        mock_compiled = MagicMock()
        # Simulate graph yielding {node_name: state}
        fake_state = {"iteration_count": 0, "max_iterations": 3}
        mock_compiled.stream.return_value = iter([
            {"specialist": fake_state},
        ])
        mock_graph.return_value = mock_compiled

        callback = MagicMock()
        config = SystemConfig()
        factory = WorkflowFactory(config)
        factory.run_workflow(
            "test", "code_generation",
            progress_callback=callback,
        )

        callback.assert_called_once_with("specialist", fake_state)

    @patch("agents.workflow_factory.create_agent_graph")
    @patch("agents.workflow_factory.create_backend_from_config")
    def test_partial_state_updated(self, mock_backend, mock_graph):
        """partial_state dict should be updated with latest state."""
        fake_state = {"specialist_output": "hello", "last_node": "specialist"}
        mock_compiled = MagicMock()
        mock_compiled.stream.return_value = iter([
            {"specialist": fake_state},
        ])
        mock_graph.return_value = mock_compiled

        partial: dict = {}
        config = SystemConfig()
        factory = WorkflowFactory(config)
        factory.run_workflow(
            "test", "code_generation",
            partial_state=partial,
        )

        assert partial.get("specialist_output") == "hello"
        assert partial.get("last_node") == "specialist"
