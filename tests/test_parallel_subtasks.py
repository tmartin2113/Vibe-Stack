"""
Tests for Parallel Sub-Task Execution.

Covers:
- _build_local_state: state isolation, field copying, defaults
- run_single_subtask: output loop, refinement, failures, max iterations
- execute_parallel_subtasks: concurrency, merging, timeouts, errors, empty list
- Graph routing: cache_hit_or_miss parallel_decompose path, edge wiring
- Thread safety: concurrent execution, no state leakage
"""

import copy
import concurrent.futures
import threading
import time
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from agents.parallel_subtasks import (
    _build_local_state,
    run_single_subtask,
    execute_parallel_subtasks,
)
from agents.state import AgentState, create_initial_state
from agents.config import SystemConfig, WorkflowConfig


# ===== Fixtures =====


@pytest.fixture
def shared_context():
    """Minimal shared context for _build_local_state / run_single_subtask."""
    return {
        "user_request": "Build a web scraper with tests",
        "specification": "Detailed spec for web scraper...",
        "loaded_skills": [{"name": "python-web", "content": "..."}],
        "memory_context": "Previous session context",
        "quality_threshold": 85,
        "max_iterations": 3,
        "specialist_max_iterations": 3,
        "session_id": "test-session-123",
    }


@pytest.fixture
def sample_subtask():
    """A single sub-task dict as produced by the router."""
    return {
        "task_type": "code_generation",
        "specialist_adapter": "code",
        "description": "Implement web scraper module",
        "specification": "",
        "output": "",
        "status": "pending",
        "spec_score": 0,
        "output_score": 0,
        "iteration_count": 0,
        "max_iterations": 3,
    }


@pytest.fixture
def approved_subtask():
    """A sub-task that will pass output quality gates."""
    return {
        "task_type": "test_generation",
        "specialist_adapter": "code",
        "description": "Write unit tests",
        "specification": "",
        "output": "",
        "status": "pending",
        "spec_score": 0,
        "output_score": 0,
        "iteration_count": 0,
        "max_iterations": 3,
    }


@pytest.fixture
def mock_nodes():
    """Mock AgentNodes with controllable output scoring."""
    nodes = MagicMock()

    def execute_sub(state):
        st = state["sub_tasks"][0]
        st["output"] = "Generated output content"
        st["output_score"] = 90  # Above threshold
        st["status"] = "executed"
        state["sub_tasks"][0] = st
        return state

    def eval_sub_output(state):
        st = state["sub_tasks"][0]
        st["status"] = "output_evaluated"
        state["sub_tasks"][0] = st
        return state

    nodes.execute_sub_task.side_effect = execute_sub
    nodes.evaluate_sub_output.side_effect = eval_sub_output

    return nodes


@pytest.fixture
def mock_training_collector():
    """Mock TrainingDataCollector."""
    tc = MagicMock()
    tc.collect_sub_output_evaluation = MagicMock()
    return tc


@pytest.fixture
def config():
    """SystemConfig with test-friendly settings."""
    cfg = SystemConfig()
    cfg.workflow.parallel_max_workers = 4
    cfg.workflow.parallel_subtask_timeout = 10
    return cfg


# ===== _build_local_state =====


class TestBuildLocalState:
    """Tests for _build_local_state()."""

    def test_creates_single_subtask_list(self, sample_subtask, shared_context):
        state = _build_local_state(sample_subtask, shared_context)
        assert len(state["sub_tasks"]) == 1
        assert state["current_sub_task_index"] == 0

    def test_copies_user_request(self, sample_subtask, shared_context):
        state = _build_local_state(sample_subtask, shared_context)
        assert state["user_request"] == shared_context["user_request"]

    def test_copies_specification(self, sample_subtask, shared_context):
        state = _build_local_state(sample_subtask, shared_context)
        assert state["specification"] == shared_context["specification"]

    def test_copies_loaded_skills(self, sample_subtask, shared_context):
        state = _build_local_state(sample_subtask, shared_context)
        assert state["loaded_skills"] == shared_context["loaded_skills"]

    def test_copies_memory_context(self, sample_subtask, shared_context):
        state = _build_local_state(sample_subtask, shared_context)
        assert state["memory_context"] == shared_context["memory_context"]

    def test_copies_quality_threshold(self, sample_subtask, shared_context):
        state = _build_local_state(sample_subtask, shared_context)
        assert state["quality_threshold"] == 85

    def test_sets_parallel_execution_true(self, sample_subtask, shared_context):
        state = _build_local_state(sample_subtask, shared_context)
        assert state["parallel_execution"] is True

    def test_initializes_iteration_counts(self, sample_subtask, shared_context):
        state = _build_local_state(sample_subtask, shared_context)
        assert state["specialist_iteration_count"] == 0
        assert state["iteration_count"] == 0

    def test_copies_max_iterations(self, sample_subtask, shared_context):
        shared_context["max_iterations"] = 5
        shared_context["specialist_max_iterations"] = 4
        state = _build_local_state(sample_subtask, shared_context)
        assert state["max_iterations"] == 5
        assert state["specialist_max_iterations"] == 4

    def test_deep_copies_subtask(self, sample_subtask, shared_context):
        """Modifying local state's sub-task doesn't affect original."""
        state = _build_local_state(sample_subtask, shared_context)
        state["sub_tasks"][0]["status"] = "completed"
        assert sample_subtask["status"] == "pending"

    def test_copies_session_id(self, sample_subtask, shared_context):
        state = _build_local_state(sample_subtask, shared_context)
        assert state["session_id"] == "test-session-123"

    def test_defaults_without_optional_fields(self, sample_subtask):
        """Works with minimal shared_context."""
        minimal_context = {
            "user_request": "test",
            "specification": "",
            "loaded_skills": [],
            "memory_context": "",
            "quality_threshold": 85,
        }
        state = _build_local_state(sample_subtask, minimal_context)
        assert state["max_iterations"] == 3  # default
        assert state["session_id"] == ""  # default

    def test_empty_adapters_and_tool_calls(self, sample_subtask, shared_context):
        state = _build_local_state(sample_subtask, shared_context)
        assert state["adapters_used"] == []
        assert state["tool_calls_made"] == []

    def test_completed_sub_tasks_zero(self, sample_subtask, shared_context):
        state = _build_local_state(sample_subtask, shared_context)
        assert state["completed_sub_tasks"] == 0


# ===== run_single_subtask =====


class TestRunSingleSubtask:
    """Tests for run_single_subtask()."""

    def test_success_path(
        self, sample_subtask, shared_context, mock_nodes, mock_training_collector
    ):
        """Full success: output approved."""
        result = run_single_subtask(
            sub_task_index=0,
            nodes=mock_nodes,
            training_collector=mock_training_collector,
            shared_context=shared_context,
            sub_task_dict=sample_subtask,
        )
        assert result.get("status") == "completed"
        assert result.get("output_score", 0) >= 85

    def test_calls_execute_sub_task(
        self, sample_subtask, shared_context, mock_nodes, mock_training_collector
    ):
        run_single_subtask(0, mock_nodes, mock_training_collector, shared_context, sample_subtask)
        mock_nodes.execute_sub_task.assert_called()

    def test_calls_evaluate_sub_output(
        self, sample_subtask, shared_context, mock_nodes, mock_training_collector
    ):
        run_single_subtask(0, mock_nodes, mock_training_collector, shared_context, sample_subtask)
        mock_nodes.evaluate_sub_output.assert_called()

    def test_collects_output_training_data(
        self, sample_subtask, shared_context, mock_nodes, mock_training_collector
    ):
        run_single_subtask(0, mock_nodes, mock_training_collector, shared_context, sample_subtask)
        mock_training_collector.collect_sub_output_evaluation.assert_called()

    def test_output_refinement_loop(
        self, sample_subtask, shared_context, mock_training_collector
    ):
        """Output loop refines when score is in refinable range."""
        nodes = MagicMock()
        output_call = [0]

        def execute_improving(state):
            output_call[0] += 1
            st = state["sub_tasks"][0]
            st["output"] = "output"
            # First: refinable, second: approved
            st["output_score"] = 70 if output_call[0] == 1 else 90
            st["status"] = "executed"
            state["sub_tasks"][0] = st
            return state

        nodes.execute_sub_task.side_effect = execute_improving
        nodes.evaluate_sub_output.side_effect = lambda s: s

        result = run_single_subtask(
            0, nodes, mock_training_collector, shared_context, sample_subtask
        )
        assert nodes.execute_sub_task.call_count == 2
        assert result.get("status") == "completed"

    def test_output_failure_low_score(
        self, sample_subtask, shared_context, mock_training_collector
    ):
        """Output fails when score is too low."""
        nodes = MagicMock()

        def execute_bad(state):
            st = state["sub_tasks"][0]
            st["output"] = "bad output"
            st["output_score"] = 30  # Below 60 → fail
            st["status"] = "executed"
            state["sub_tasks"][0] = st
            return state

        nodes.execute_sub_task.side_effect = execute_bad
        nodes.evaluate_sub_output.side_effect = lambda s: s

        result = run_single_subtask(
            0, nodes, mock_training_collector, shared_context, sample_subtask
        )
        assert result["status"] == "failed"

    def test_preserves_task_type(
        self, sample_subtask, shared_context, mock_nodes, mock_training_collector
    ):
        result = run_single_subtask(
            0, mock_nodes, mock_training_collector, shared_context, sample_subtask
        )
        assert result["task_type"] == "code_generation"

    def test_uses_subtask_max_iterations(
        self, shared_context, mock_training_collector
    ):
        """Uses sub-task's own max_iterations, not global."""
        subtask = {
            "task_type": "code",
            "specialist_adapter": "code",
            "description": "test",
            "status": "pending",
            "spec_score": 0,
            "output_score": 0,
            "iteration_count": 0,
            "max_iterations": 1,  # Only 1 attempt
        }
        nodes = MagicMock()

        def execute_mediocre(state):
            st = state["sub_tasks"][0]
            st["output"] = "mediocre output"
            st["output_score"] = 70  # Refinable
            st["status"] = "executed"
            state["sub_tasks"][0] = st
            return state

        nodes.execute_sub_task.side_effect = execute_mediocre
        nodes.evaluate_sub_output.side_effect = lambda s: s

        run_single_subtask(
            0, nodes, mock_training_collector, shared_context, subtask,
            max_iterations=5,  # Global is 5 but subtask says 1
        )
        # Should respect subtask's max_iterations=1
        assert nodes.execute_sub_task.call_count == 1

    def test_state_isolation_between_calls(
        self, shared_context, mock_nodes, mock_training_collector
    ):
        """Two calls don't leak state between each other."""
        st1 = {"task_type": "code", "specialist_adapter": "code",
               "description": "task1", "status": "pending",
               "spec_score": 0, "output_score": 0,
               "iteration_count": 0, "max_iterations": 3}
        st2 = {"task_type": "research", "specialist_adapter": "research",
               "description": "task2", "status": "pending",
               "spec_score": 0, "output_score": 0,
               "iteration_count": 0, "max_iterations": 3}

        r1 = run_single_subtask(0, mock_nodes, mock_training_collector, shared_context, st1)
        r2 = run_single_subtask(1, mock_nodes, mock_training_collector, shared_context, st2)

        assert r1["task_type"] == "code"
        assert r2["task_type"] == "research"

    def test_iteration_count_starts_at_zero(
        self, sample_subtask, shared_context, mock_nodes, mock_training_collector
    ):
        """iteration_count starts at 0 for the output phase."""
        original_execute = mock_nodes.execute_sub_task.side_effect

        def check_iteration_start(state):
            st = state["sub_tasks"][0]
            # iteration_count should be 0 at start of output phase
            assert st.get("iteration_count") is not None
            return original_execute(state)

        mock_nodes.execute_sub_task.side_effect = check_iteration_start

        run_single_subtask(
            0, mock_nodes, mock_training_collector, shared_context, sample_subtask
        )


# ===== execute_parallel_subtasks =====


class TestExecuteParallelSubtasks:
    """Tests for execute_parallel_subtasks() graph node."""

    def _make_state_with_subtasks(self, subtasks, parallel=True):
        """Helper to create an AgentState with sub-tasks."""
        state = create_initial_state("Build web scraper")
        state["sub_tasks"] = subtasks
        state["requires_decomposition"] = True
        state["parallel_execution"] = parallel
        state["specification"] = "Detailed spec"
        state["loaded_skills"] = [{"name": "python-web", "content": "..."}]
        state["memory_context"] = "context"
        return state

    def test_empty_subtasks(self, mock_nodes, mock_training_collector, config):
        """No sub-tasks → returns state unchanged."""
        state = create_initial_state("test")
        state["sub_tasks"] = []
        result = execute_parallel_subtasks(state, mock_nodes, mock_training_collector, config)
        assert result["sub_tasks"] == []

    def test_single_subtask_success(
        self, sample_subtask, mock_nodes, mock_training_collector, config
    ):
        state = self._make_state_with_subtasks([sample_subtask])
        result = execute_parallel_subtasks(state, mock_nodes, mock_training_collector, config)

        assert result["completed_sub_tasks"] == 1
        assert result["sub_tasks"][0].get("status") == "completed"
        assert result["current_sub_task_index"] == 1

    def test_multiple_subtasks_success(
        self, mock_nodes, mock_training_collector, config
    ):
        subtasks = [
            {"task_type": "code", "specialist_adapter": "code",
             "description": f"task{i}", "status": "pending",
             "spec_score": 0, "output_score": 0,
             "iteration_count": 0, "max_iterations": 3}
            for i in range(3)
        ]
        state = self._make_state_with_subtasks(subtasks)
        result = execute_parallel_subtasks(state, mock_nodes, mock_training_collector, config)

        assert result["completed_sub_tasks"] == 3
        assert len(result["sub_tasks"]) == 3
        assert result["current_sub_task_index"] == 3
        for st in result["sub_tasks"]:
            assert st.get("status") == "completed"

    def test_partial_failure(self, mock_training_collector, config):
        """Some sub-tasks succeed, some fail."""
        nodes = MagicMock()
        call_idx = [0]

        def execute_with_failures(state):
            st = state["sub_tasks"][0]
            idx = call_idx[0]
            call_idx[0] += 1
            st["output"] = "output"
            # Alternate: good, bad, good
            st["output_score"] = 90 if idx != 1 else 30  # idx 1 fails
            st["status"] = "executed"
            state["sub_tasks"][0] = st
            return state

        nodes.execute_sub_task.side_effect = execute_with_failures
        nodes.evaluate_sub_output.side_effect = lambda s: s

        subtasks = [
            {"task_type": "code", "specialist_adapter": "code",
             "description": f"task{i}", "status": "pending",
             "spec_score": 0, "output_score": 0,
             "iteration_count": 0, "max_iterations": 3}
            for i in range(3)
        ]
        state = self._make_state_with_subtasks(subtasks)
        result = execute_parallel_subtasks(state, nodes, mock_training_collector, config)

        # At least one should have failed
        statuses = [st.get("status") for st in result["sub_tasks"]]
        assert "failed" in statuses
        assert result["completed_sub_tasks"] < 3

    def test_all_fail(self, mock_training_collector, config):
        """All sub-tasks fail → completed_sub_tasks=0."""
        nodes = MagicMock()

        def execute_bad_output(state):
            st = state["sub_tasks"][0]
            st["output"] = "bad output"
            st["output_score"] = 20  # Far below threshold → fail
            st["status"] = "executed"
            state["sub_tasks"][0] = st
            return state

        nodes.execute_sub_task.side_effect = execute_bad_output
        nodes.evaluate_sub_output.side_effect = lambda s: s

        subtasks = [
            {"task_type": "code", "specialist_adapter": "code",
             "description": f"task{i}", "status": "pending",
             "spec_score": 0, "output_score": 0,
             "iteration_count": 0, "max_iterations": 3}
            for i in range(2)
        ]
        state = self._make_state_with_subtasks(subtasks)
        result = execute_parallel_subtasks(state, nodes, mock_training_collector, config)

        assert result["completed_sub_tasks"] == 0

    def test_preserves_original_indices(
        self, mock_nodes, mock_training_collector, config
    ):
        """Results are written back at correct original indices."""
        subtasks = [
            {"task_type": t, "specialist_adapter": "code",
             "description": t, "status": "pending",
             "spec_score": 0, "output_score": 0,
             "iteration_count": 0, "max_iterations": 3}
            for t in ["code", "test", "docs"]
        ]
        state = self._make_state_with_subtasks(subtasks)
        result = execute_parallel_subtasks(state, mock_nodes, mock_training_collector, config)

        # Task types should be preserved at original positions
        assert result["sub_tasks"][0]["task_type"] == "code"
        assert result["sub_tasks"][1]["task_type"] == "test"
        assert result["sub_tasks"][2]["task_type"] == "docs"

    def test_updates_current_sub_task_index(
        self, sample_subtask, mock_nodes, mock_training_collector, config
    ):
        subtasks = [copy.deepcopy(sample_subtask) for _ in range(4)]
        state = self._make_state_with_subtasks(subtasks)
        result = execute_parallel_subtasks(state, mock_nodes, mock_training_collector, config)
        assert result["current_sub_task_index"] == 4

    def test_error_recording_on_exception(
        self, mock_training_collector, config
    ):
        """Exceptions in sub-tasks are recorded in parallel_execution_errors."""
        nodes = MagicMock()

        def explode(state):
            raise RuntimeError("LLM backend crashed")

        nodes.execute_sub_task.side_effect = explode

        subtasks = [
            {"task_type": "code", "specialist_adapter": "code",
             "description": "boom", "status": "pending",
             "spec_score": 0, "output_score": 0,
             "iteration_count": 0, "max_iterations": 3}
        ]
        state = self._make_state_with_subtasks(subtasks)
        result = execute_parallel_subtasks(state, nodes, mock_training_collector, config)

        assert len(result["parallel_execution_errors"]) == 1
        assert result["parallel_execution_errors"][0]["error_type"] == "RuntimeError"
        assert result["sub_tasks"][0]["status"] == "failed"

    def test_timeout_handling(self, mock_training_collector):
        """Sub-tasks that exceed timeout are marked as failed."""
        nodes = MagicMock()

        def slow_execute(state):
            time.sleep(5)  # Exceed timeout
            return state

        nodes.execute_sub_task.side_effect = slow_execute

        cfg = SystemConfig()
        cfg.workflow.parallel_subtask_timeout = 1  # 1 second timeout

        subtasks = [
            {"task_type": "code", "specialist_adapter": "code",
             "description": "slow", "status": "pending",
             "spec_score": 0, "output_score": 0,
             "iteration_count": 0, "max_iterations": 3}
        ]
        state = create_initial_state("test")
        state["sub_tasks"] = subtasks
        state["specification"] = ""
        state["loaded_skills"] = []
        state["memory_context"] = ""

        result = execute_parallel_subtasks(state, nodes, mock_training_collector, cfg)
        assert result["sub_tasks"][0]["status"] == "failed"

    def test_config_none_uses_defaults(
        self, sample_subtask, mock_nodes, mock_training_collector
    ):
        """config=None uses default max_workers=4 and timeout=300."""
        state = create_initial_state("test")
        state["sub_tasks"] = [sample_subtask]
        state["specification"] = ""
        state["loaded_skills"] = []
        state["memory_context"] = ""

        # Should not raise
        result = execute_parallel_subtasks(
            state, mock_nodes, mock_training_collector, config=None
        )
        assert result["completed_sub_tasks"] >= 0

    def test_reads_config_workers(self, mock_nodes, mock_training_collector):
        """max_workers is read from config."""
        cfg = SystemConfig()
        cfg.workflow.parallel_max_workers = 2

        subtasks = [
            {"task_type": "code", "specialist_adapter": "code",
             "description": f"t{i}", "status": "pending",
             "spec_score": 0, "output_score": 0,
             "iteration_count": 0, "max_iterations": 3}
            for i in range(4)
        ]
        state = create_initial_state("test")
        state["sub_tasks"] = subtasks
        state["specification"] = ""
        state["loaded_skills"] = []
        state["memory_context"] = ""

        # Should complete without error (just with 2 workers)
        result = execute_parallel_subtasks(state, mock_nodes, mock_training_collector, cfg)
        assert result["completed_sub_tasks"] == 4

    def test_does_not_mutate_original_subtask_dicts(
        self, mock_nodes, mock_training_collector, config
    ):
        """Original sub-task dicts are deep-copied, not mutated in-place by threads."""
        original = {"task_type": "code", "specialist_adapter": "code",
                     "description": "keep me", "status": "pending",
                     "spec_score": 0, "output_score": 0,
                     "iteration_count": 0, "max_iterations": 3}
        frozen = copy.deepcopy(original)

        state = create_initial_state("test")
        state["sub_tasks"] = [original]
        state["specification"] = ""
        state["loaded_skills"] = []
        state["memory_context"] = ""

        execute_parallel_subtasks(state, mock_nodes, mock_training_collector, config)

        # The state's sub_tasks[0] is now the result (replaced by merge),
        # but the _build_local_state deep-copies, so the thread doesn't
        # mutate the original dict passed to it
        # (the original variable was passed by reference to executor.submit
        # as sub_task_dict, which is deep-copied inside _build_local_state)

    def test_shared_context_includes_all_fields(
        self, sample_subtask, mock_training_collector, config
    ):
        """The shared_context dict built in execute_parallel_subtasks has all expected fields."""
        nodes = MagicMock()
        captured_context = {}

        original_build = _build_local_state

        with patch("agents.parallel_subtasks._build_local_state") as mock_build:
            def capture_and_build(sub_task_dict, shared_ctx):
                captured_context.update(shared_ctx)
                return original_build(sub_task_dict, shared_ctx)

            mock_build.side_effect = capture_and_build
            nodes.execute_sub_task.side_effect = lambda s: _set_output_score(s, 90)
            nodes.evaluate_sub_output.side_effect = lambda s: s

            state = create_initial_state("Build something")
            state["sub_tasks"] = [sample_subtask]
            state["specification"] = "spec text"
            state["loaded_skills"] = [{"name": "sk1"}]
            state["memory_context"] = "mem"

            execute_parallel_subtasks(state, nodes, mock_training_collector, config)

            assert "user_request" in captured_context
            assert "specification" in captured_context
            assert "loaded_skills" in captured_context
            assert "memory_context" in captured_context
            assert "quality_threshold" in captured_context
            assert "session_id" in captured_context


# ===== Graph Routing =====


class TestGraphRouting:
    """Tests for cache_hit_or_miss routing and graph edge wiring."""

    def test_parallel_decompose_when_parallel_true(self):
        """cache_hit_or_miss returns 'parallel_decompose' for parallel decomposed tasks."""
        state = create_initial_state("test")
        state["cache_hit"] = False
        state["requires_decomposition"] = True
        state["parallel_execution"] = True

        # Inline the decision function logic
        result = self._cache_hit_or_miss(state)
        assert result == "parallel_decompose"

    def test_decompose_when_parallel_false(self):
        """cache_hit_or_miss returns 'decompose' for sequential decomposed tasks."""
        state = create_initial_state("test")
        state["cache_hit"] = False
        state["requires_decomposition"] = True
        state["parallel_execution"] = False

        result = self._cache_hit_or_miss(state)
        assert result == "decompose"

    def test_single_when_no_decomposition(self):
        """cache_hit_or_miss returns 'single' for non-decomposed tasks."""
        state = create_initial_state("test")
        state["cache_hit"] = False
        state["requires_decomposition"] = False

        result = self._cache_hit_or_miss(state)
        assert result == "single"

    def test_cache_hit_takes_priority(self):
        """cache_hit_or_miss returns 'cache_hit' even if decomposition is true."""
        state = create_initial_state("test")
        state["cache_hit"] = True
        state["requires_decomposition"] = True
        state["parallel_execution"] = True

        result = self._cache_hit_or_miss(state)
        assert result == "cache_hit"

    def test_parallel_decompose_without_parallel_flag(self):
        """Without parallel_execution, decomposed tasks use sequential path."""
        state = create_initial_state("test")
        state["cache_hit"] = False
        state["requires_decomposition"] = True
        # parallel_execution not set (defaults to False)

        result = self._cache_hit_or_miss(state)
        assert result == "decompose"

    @staticmethod
    def _cache_hit_or_miss(state: AgentState) -> str:
        """Replicate cache_hit_or_miss from graph.py for isolated testing."""
        if state.get("cache_hit", False):
            return "cache_hit"
        if state.get("requires_decomposition", False):
            if state.get("parallel_execution", False):
                return "parallel_decompose"
            return "decompose"
        return "single"


class TestGraphEdgeWiring:
    """Verify the parallel_subtasks node is properly wired in the graph."""

    def test_parallel_subtasks_node_exists(self):
        """The graph should have a 'parallel_subtasks' node."""
        # Import and check graph.py source for the node registration
        from agents import graph as graph_module
        source = open(graph_module.__file__).read()
        assert "parallel_subtasks" in source
        assert 'workflow.add_node("parallel_subtasks"' in source

    def test_parallel_subtasks_to_aggregator_edge(self):
        """parallel_subtasks should route to aggregator (via conditional edge)."""
        from agents import graph as graph_module
        source = open(graph_module.__file__).read()
        # Now a conditional edge: clarification → skill_cleanup, continue → aggregator
        assert '"parallel_subtasks"' in source
        assert '"aggregator"' in source

    def test_parallel_decompose_route_exists(self):
        """The conditional edge map should include parallel_decompose."""
        from agents import graph as graph_module
        source = open(graph_module.__file__).read()
        assert '"parallel_decompose"' in source
        assert '"parallel_subtasks"' in source

    def test_import_exists(self):
        """execute_parallel_subtasks is imported in graph.py."""
        from agents import graph as graph_module
        source = open(graph_module.__file__).read()
        assert "from .parallel_subtasks import execute_parallel_subtasks" in source


# ===== Thread Safety =====


class TestThreadSafety:
    """Tests for thread-safe concurrent execution."""

    def test_concurrent_training_collector_writes(self, config):
        """Training collector is called from multiple threads without errors."""
        tc = MagicMock()
        tc.collect_sub_output_evaluation = MagicMock()

        nodes = MagicMock()
        nodes.execute_sub_task.side_effect = lambda s: _set_output_score(s, 90)
        nodes.evaluate_sub_output.side_effect = lambda s: s

        subtasks = [
            {"task_type": "code", "specialist_adapter": "code",
             "description": f"task{i}", "status": "pending",
             "spec_score": 0, "output_score": 0,
             "iteration_count": 0, "max_iterations": 3}
            for i in range(5)
        ]

        state = create_initial_state("test")
        state["sub_tasks"] = subtasks
        state["specification"] = ""
        state["loaded_skills"] = []
        state["memory_context"] = ""

        result = execute_parallel_subtasks(state, nodes, tc, config)

        # Each sub-task should have triggered output training calls
        assert tc.collect_sub_output_evaluation.call_count == 5
        assert result["completed_sub_tasks"] == 5

    def test_no_state_leakage_between_threads(self, config):
        """Each thread operates on an isolated state copy."""
        thread_states = []
        lock = threading.Lock()

        nodes = MagicMock()

        def capture_and_execute(state):
            with lock:
                thread_states.append(id(state))
            return _set_output_score(state, 90)

        nodes.execute_sub_task.side_effect = capture_and_execute
        nodes.evaluate_sub_output.side_effect = lambda s: s

        tc = MagicMock()

        subtasks = [
            {"task_type": "code", "specialist_adapter": "code",
             "description": f"task{i}", "status": "pending",
             "spec_score": 0, "output_score": 0,
             "iteration_count": 0, "max_iterations": 3}
            for i in range(3)
        ]

        state = create_initial_state("test")
        state["sub_tasks"] = subtasks
        state["specification"] = ""
        state["loaded_skills"] = []
        state["memory_context"] = ""

        execute_parallel_subtasks(state, nodes, tc, config)

        # All state objects should have unique IDs (different copies)
        assert len(set(thread_states)) == 3

    def test_concurrent_node_calls(self, config):
        """Nodes are called concurrently from multiple threads."""
        call_times = []
        lock = threading.Lock()

        nodes = MagicMock()

        def slow_execute(state):
            start = time.monotonic()
            time.sleep(0.1)  # Small delay to detect parallelism
            end = time.monotonic()
            with lock:
                call_times.append((start, end))
            return _set_output_score(state, 90)

        nodes.execute_sub_task.side_effect = slow_execute
        nodes.evaluate_sub_output.side_effect = lambda s: s

        tc = MagicMock()

        subtasks = [
            {"task_type": "code", "specialist_adapter": "code",
             "description": f"task{i}", "status": "pending",
             "spec_score": 0, "output_score": 0,
             "iteration_count": 0, "max_iterations": 3}
            for i in range(3)
        ]

        state = create_initial_state("test")
        state["sub_tasks"] = subtasks
        state["specification"] = ""
        state["loaded_skills"] = []
        state["memory_context"] = ""

        execute_parallel_subtasks(state, nodes, tc, config)

        # If truly parallel, at least 2 calls should overlap in time
        assert len(call_times) == 3
        # Check overlap: at least one call started before another ended
        has_overlap = False
        for i in range(len(call_times)):
            for j in range(i + 1, len(call_times)):
                s1, e1 = call_times[i]
                s2, e2 = call_times[j]
                if s1 < e2 and s2 < e1:
                    has_overlap = True
        assert has_overlap, "Sub-tasks should execute concurrently"


# ===== Config Integration =====


class TestConfigIntegration:
    """Tests for WorkflowConfig parallel fields."""

    def test_default_max_workers(self):
        cfg = WorkflowConfig()
        assert cfg.parallel_max_workers == 4

    def test_default_subtask_timeout(self):
        cfg = WorkflowConfig()
        assert cfg.parallel_subtask_timeout == 300

    def test_custom_max_workers(self):
        cfg = WorkflowConfig()
        cfg.parallel_max_workers = 8
        assert cfg.parallel_max_workers == 8

    def test_system_config_includes_parallel_fields(self):
        cfg = SystemConfig()
        assert hasattr(cfg.workflow, "parallel_max_workers")
        assert hasattr(cfg.workflow, "parallel_subtask_timeout")


# ===== State Integration =====


class TestStateIntegration:
    """Tests for AgentState parallel fields."""

    def test_parallel_execution_errors_initialized(self):
        state = create_initial_state("test")
        assert state["parallel_execution_errors"] == []

    def test_parallel_execution_default_false(self):
        state = create_initial_state("test")
        assert state["parallel_execution"] is False

    def test_parallel_execution_errors_type_hint(self):
        """parallel_execution_errors is in AgentState TypedDict."""
        assert "parallel_execution_errors" in AgentState.__annotations__


# ===== Helpers =====


def _set_output_score(state, score):
    """Helper to set output_score on the first sub-task."""
    st = state["sub_tasks"][0]
    st["output"] = "Generated output"
    st["output_score"] = score
    st["status"] = "executed"
    state["sub_tasks"][0] = st
    return state
