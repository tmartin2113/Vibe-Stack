"""
Tests to improve coverage for smaller modules:

- agents/sandbox/client.py
- agents/aggregator.py
- agents/decision_functions.py
- agents/skill_generator.py
- agents/config.py
- agents/doctor.py
- agents/ws_client.py
- agents/parallel_subtasks.py
"""

import asyncio
import copy
import concurrent.futures
import json
import os
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch, PropertyMock, call

import pytest

from agents.state import AgentState, create_initial_state


# ════════════════════════════════════════════════════════════════════
# sandbox/client.py
# ════════════════════════════════════════════════════════════════════


class TestSandboxCheckSDK:
    """Cover lines 32-38: _check_sdk lazy import with caching."""

    def test_check_sdk_returns_false_when_missing(self):
        """Cover lines 32-37: SDK not installed path."""
        import agents.sandbox.client as client_mod

        # Reset the cached flag
        original = client_mod._opensandbox_available
        client_mod._opensandbox_available = None
        try:
            with patch.dict("sys.modules", {"opensandbox": None}):
                # Force ImportError by making import fail
                import builtins
                real_import = builtins.__import__

                def fake_import(name, *args, **kwargs):
                    if name == "opensandbox":
                        raise ImportError("no opensandbox")
                    return real_import(name, *args, **kwargs)

                with patch("builtins.__import__", side_effect=fake_import):
                    result = client_mod._check_sdk()
                    assert result is False
        finally:
            client_mod._opensandbox_available = original

    def test_check_sdk_caches_result(self):
        """Cover line 32: cached path (already checked)."""
        import agents.sandbox.client as client_mod

        original = client_mod._opensandbox_available
        client_mod._opensandbox_available = True
        try:
            assert client_mod._check_sdk() is True
        finally:
            client_mod._opensandbox_available = original


class TestSandboxPoolManagerWarmPool:
    """Cover lines 146-147: exception during pre-warm."""

    def test_warm_pool_handles_create_failure(self):
        from agents.sandbox.client import SandboxPoolManager
        from agents.sandbox.config import SandboxConfig

        config = SandboxConfig(pool_size=2)
        pool = SandboxPoolManager(config)
        pool._warmed = False

        # Make _create_sandbox always fail
        pool._create_sandbox = MagicMock(side_effect=RuntimeError("create failed"))
        pool._warm_pool()

        assert pool._warmed is True
        assert pool._pool.qsize() == 0  # No containers created


class TestSandboxPoolManagerStop:
    """Cover lines 167-168, 174-175: stop with kill errors and empty queue."""

    def test_stop_handles_kill_exception(self):
        from agents.sandbox.client import SandboxPoolManager, SandboxHandle
        from agents.sandbox.config import SandboxConfig

        config = SandboxConfig()
        pool = SandboxPoolManager(config)
        pool._started = True
        pool._stop_event = threading.Event()

        # Create a fake handle that raises on kill
        mock_sandbox = MagicMock()
        handle = SandboxHandle(sandbox_id="test-1", sandbox=mock_sandbox)
        pool._all_handles = [handle]
        pool._kill_sandbox = MagicMock(side_effect=RuntimeError("kill fail"))

        # Put something in the queue to trigger drain
        pool._pool.put(handle)

        pool.stop()
        assert pool._started is False

    def test_stop_drains_empty_queue(self):
        """Cover lines 174-175: queue.Empty during drain."""
        from agents.sandbox.client import SandboxPoolManager
        from agents.sandbox.config import SandboxConfig

        config = SandboxConfig()
        pool = SandboxPoolManager(config)
        pool._started = True
        pool._stop_event = threading.Event()
        pool._all_handles = []

        pool.stop()
        assert pool._started is False


class TestSandboxExecuteInSandbox:
    """Cover lines 196-235: execute_in_sandbox success and error paths."""

    def test_execute_in_sandbox_success(self):
        from agents.sandbox.client import SandboxPoolManager, SandboxHandle
        from agents.sandbox.config import SandboxConfig
        from agents.tools.registry import ToolResult

        config = SandboxConfig()
        pool = SandboxPoolManager(config)
        pool._warmed = True

        # Create a mock handle
        mock_sandbox = MagicMock()
        mock_result = MagicMock()
        mock_result.exit_code = 0
        mock_result.stdout = "hello world"
        mock_result.stderr = ""

        handle = SandboxHandle(sandbox_id="test-1", sandbox=mock_sandbox)

        pool._acquire = MagicMock(return_value=handle)
        pool._release = MagicMock()
        pool._run_async = MagicMock(return_value=mock_result)

        result = pool.execute_in_sandbox("print('hello')")

        assert result.success is True
        assert result.output == "hello world"
        assert result.metadata["sandboxed"] is True
        assert result.metadata["sandbox_id"] == "test-1"
        pool._release.assert_called_once_with(handle)

    def test_execute_in_sandbox_failure_exit_code(self):
        from agents.sandbox.client import SandboxPoolManager, SandboxHandle
        from agents.sandbox.config import SandboxConfig

        config = SandboxConfig()
        pool = SandboxPoolManager(config)
        pool._warmed = True

        mock_sandbox = MagicMock()
        mock_result = MagicMock()
        mock_result.exit_code = 1
        mock_result.stdout = ""
        mock_result.stderr = "SyntaxError"

        handle = SandboxHandle(sandbox_id="test-2", sandbox=mock_sandbox)

        pool._acquire = MagicMock(return_value=handle)
        pool._release = MagicMock()
        pool._run_async = MagicMock(return_value=mock_result)

        result = pool.execute_in_sandbox("invalid python")
        assert result.success is False
        assert result.error == "SyntaxError"

    def test_execute_in_sandbox_exception(self):
        from agents.sandbox.client import SandboxPoolManager, SandboxHandle
        from agents.sandbox.config import SandboxConfig

        config = SandboxConfig()
        pool = SandboxPoolManager(config)
        pool._warmed = True

        handle = SandboxHandle(sandbox_id="test-3", sandbox=MagicMock())

        pool._acquire = MagicMock(return_value=handle)
        pool._release = MagicMock()
        pool._run_async = MagicMock(side_effect=RuntimeError("boom"))

        result = pool.execute_in_sandbox("code")
        assert result.success is False
        assert "Sandbox execution error" in result.error
        pool._release.assert_called_once_with(handle)


class TestSandboxRunCommand:
    """Cover lines 241-270: run_command success and error."""

    def test_run_command_success(self):
        from agents.sandbox.client import SandboxPoolManager, SandboxHandle
        from agents.sandbox.config import SandboxConfig

        config = SandboxConfig()
        pool = SandboxPoolManager(config)
        pool._warmed = True

        mock_sandbox = MagicMock()
        mock_result = MagicMock()
        mock_result.exit_code = 0
        mock_result.stdout = "output"
        mock_result.stderr = ""

        handle = SandboxHandle(sandbox_id="cmd-1", sandbox=mock_sandbox)
        pool._acquire = MagicMock(return_value=handle)
        pool._release = MagicMock()
        pool._run_async = MagicMock(return_value=mock_result)

        result = pool.run_command("ls -la")
        assert result.success is True
        assert result.output == "output"

    def test_run_command_exception(self):
        from agents.sandbox.client import SandboxPoolManager, SandboxHandle
        from agents.sandbox.config import SandboxConfig

        config = SandboxConfig()
        pool = SandboxPoolManager(config)
        pool._warmed = True

        handle = SandboxHandle(sandbox_id="cmd-2", sandbox=MagicMock())
        pool._acquire = MagicMock(return_value=handle)
        pool._release = MagicMock()
        pool._run_async = MagicMock(side_effect=TimeoutError("timeout"))

        result = pool.run_command("long running cmd")
        assert result.success is False
        assert "Sandbox command error" in result.error
        pool._release.assert_called_once_with(handle)


class TestSandboxCreateSandbox:
    """Cover lines 310-344: _create_sandbox."""

    def test_create_sandbox_cpu(self):
        from agents.sandbox.client import SandboxPoolManager
        from agents.sandbox.config import SandboxConfig

        config = SandboxConfig(gpu_enabled=False)
        pool = SandboxPoolManager(config)

        mock_sandbox = MagicMock()
        mock_sandbox.id = "sb-123"

        # Mock opensandbox imports and Sandbox.create
        mock_sandbox_cls = MagicMock()
        mock_sandbox_cls.create = MagicMock(return_value=mock_sandbox)
        mock_conn_config = MagicMock()

        pool._event_loop = MagicMock()
        pool._event_loop.is_running.return_value = False
        pool._run_async = MagicMock(return_value=mock_sandbox)

        with patch.dict("sys.modules", {
            "opensandbox": MagicMock(Sandbox=mock_sandbox_cls),
            "opensandbox.config": MagicMock(ConnectionConfig=mock_conn_config),
        }):
            handle = pool._create_sandbox()
            assert handle.sandbox_id == "sb-123"
            assert handle.sandbox == mock_sandbox

    def test_create_sandbox_gpu(self):
        from agents.sandbox.client import SandboxPoolManager
        from agents.sandbox.config import SandboxConfig

        config = SandboxConfig(gpu_enabled=True, gpu_device_ids="0,1")
        pool = SandboxPoolManager(config)

        mock_sandbox = MagicMock()
        mock_sandbox.id = "sb-gpu"

        pool._run_async = MagicMock(return_value=mock_sandbox)

        with patch.dict("sys.modules", {
            "opensandbox": MagicMock(),
            "opensandbox.config": MagicMock(),
        }):
            handle = pool._create_sandbox()
            assert handle.sandbox_id == "sb-gpu"


class TestSandboxMaintenanceLoop:
    """Cover lines 367-371: _maintenance_loop and exception handling."""

    def test_maintenance_loop_catches_exception(self):
        from agents.sandbox.client import SandboxPoolManager
        from agents.sandbox.config import SandboxConfig

        config = SandboxConfig()
        pool = SandboxPoolManager(config)
        pool._stop_event = threading.Event()

        pool._recycle_expired = MagicMock(side_effect=RuntimeError("recycle boom"))
        pool._replenish_pool = MagicMock()

        # Run maintenance once then stop
        call_count = [0]
        original_wait = pool._stop_event.wait

        def fake_wait(timeout=None):
            call_count[0] += 1
            if call_count[0] >= 2:
                pool._stop_event.set()
                return True
            return False

        pool._stop_event.wait = fake_wait
        pool._maintenance_loop()

        pool._recycle_expired.assert_called()


class TestSandboxRecycleExpiredQueueEmpty:
    """Cover lines 386-387: queue.Empty in _recycle_expired."""

    def test_recycle_expired_handles_queue_empty(self):
        from agents.sandbox.client import SandboxPoolManager
        from agents.sandbox.config import SandboxConfig

        config = SandboxConfig(sandbox_timeout=1)
        pool = SandboxPoolManager(config)

        # Empty pool should not crash
        pool._recycle_expired()
        assert pool._pool.qsize() == 0


# ════════════════════════════════════════════════════════════════════
# aggregator.py
# ════════════════════════════════════════════════════════════════════


class TestAggregatorNodeNoCompleted:
    """Cover lines 65-67: no completed sub-tasks."""

    def test_no_completed_subtasks(self):
        from agents.aggregator import AggregatorNode

        node = AggregatorNode()
        state = create_initial_state("test request")
        state["sub_tasks"] = [
            {"status": "failed", "output": "fail output", "task_type": "code_generation"},
        ]
        result = node.execute(state)
        assert result["aggregated_output"] == "Error: No sub-tasks completed successfully."
        assert result["final_aggregation_score"] == 0


class TestAggregatorNoAdapterFallback:
    """Cover lines 82-83: no adapter available path."""

    def test_fallback_concatenation_used_when_no_registry(self):
        from agents.aggregator import AggregatorNode

        node = AggregatorNode(adapter_registry=None)
        state = create_initial_state("test request")
        state["sub_tasks"] = [
            {
                "status": "completed",
                "output": "Generated code here",
                "task_type": "code_generation",
                "specialist_adapter": "code",
                "output_score": 90,
                "specification": "spec",
            },
        ]
        result = node.execute(state)
        assert "Generated code here" in result["aggregated_output"]


class TestAggregatorGetAdapter:
    """Cover lines 112, 119-132: _get_aggregation_adapter fallback chain."""

    def test_get_adapter_returns_none_when_no_registry(self):
        from agents.aggregator import AggregatorNode

        node = AggregatorNode(adapter_registry=None)
        assert node._get_aggregation_adapter() is None

    def test_get_adapter_falls_back_to_other_adapter(self):
        from agents.aggregator import AggregatorNode
        from agents.adapters import AdapterRegistry

        registry = AdapterRegistry()
        # vibe not registered, but register another adapter
        mock_adapter = MagicMock()
        mock_adapter.name = "other"
        registry.register(mock_adapter)

        node = AggregatorNode(adapter_registry=registry)
        # vibe will raise or return None
        result = node._get_aggregation_adapter()
        assert result is not None

    def test_get_adapter_returns_none_when_all_fail(self):
        from agents.aggregator import AggregatorNode

        registry = MagicMock()
        registry.get.side_effect = KeyError("not found")
        registry.list_adapters.return_value = ["broken"]

        node = AggregatorNode(adapter_registry=registry)
        result = node._get_aggregation_adapter()
        assert result is None


class TestAggregatorLLMAggregate:
    """Cover lines 176, 180: sequential and unknown strategy branches."""

    def _make_node_with_adapter(self):
        from agents.aggregator import AggregatorNode

        mock_adapter = MagicMock()
        mock_adapter.generate.return_value = "This is a properly aggregated response with enough content to pass."

        registry = MagicMock()
        registry.get.return_value = mock_adapter

        node = AggregatorNode(adapter_registry=registry)
        return node, mock_adapter

    def test_sequential_strategy_llm(self):
        node, adapter = self._make_node_with_adapter()
        state = create_initial_state("test request")
        state["aggregation_strategy"] = "sequential"
        state["sub_tasks"] = [
            {
                "status": "completed",
                "output": "Step 1 output",
                "task_type": "code_generation",
                "specialist_adapter": "code",
                "output_score": 88,
                "specification": "spec",
            },
        ]
        result = node.execute(state)
        assert result["aggregated_output"] != ""

    def test_unknown_strategy_defaults_to_merge(self):
        node, adapter = self._make_node_with_adapter()
        state = create_initial_state("test request")
        state["aggregation_strategy"] = "custom_unknown"
        state["sub_tasks"] = [
            {
                "status": "completed",
                "output": "Output here",
                "task_type": "code_generation",
                "specialist_adapter": "code",
                "output_score": 88,
                "specification": "spec",
            },
        ]
        result = node.execute(state)
        assert result["aggregated_output"] != ""


class TestAggregatorLLMInsufficient:
    """Cover line 212: LLM returns trivial output, falls back."""

    def test_llm_returns_trivial_output(self):
        from agents.aggregator import AggregatorNode

        mock_adapter = MagicMock()
        mock_adapter.generate.return_value = "OK"  # Too short (<=10 chars)

        raw_sections = [
            {
                "task_type": "code_generation",
                "title": "Generated Code",
                "specialist": "code",
                "output": "def hello(): pass",
                "score": 85,
                "specification": "spec",
            }
        ]
        node = AggregatorNode()
        result = node._llm_aggregate(
            mock_adapter, raw_sections, "request", "spec", "merge"
        )
        # Should fall back to _fallback_aggregate
        assert "def hello(): pass" in result


class TestAggregatorFallbackStrategies:
    """Cover lines 304, 308, 337-346, 376-379: fallback strategies."""

    def _sections(self):
        return [
            {
                "task_type": "code_generation",
                "title": "Generated Code",
                "specialist": "code",
                "output": "def foo(): pass",
                "score": 90,
                "specification": "spec",
            },
            {
                "task_type": "test_generation",
                "title": "Test Suite",
                "specialist": "test",
                "output": "def test_foo(): assert True",
                "score": 80,
                "specification": "spec",
            },
        ]

    def test_fallback_sequential(self):
        from agents.aggregator import AggregatorNode

        node = AggregatorNode()
        result = node._fallback_aggregate(self._sections(), "my request", "sequential")
        assert "Step 1" in result
        assert "Step 2" in result
        assert "Specialist:" in result

    def test_fallback_report(self):
        from agents.aggregator import AggregatorNode

        node = AggregatorNode()
        result = node._fallback_aggregate(self._sections(), "my request", "report")
        assert "Comprehensive Analysis Report" in result
        assert "Executive Summary" in result

    def test_fallback_unknown_defaults_to_merge(self):
        from agents.aggregator import AggregatorNode

        node = AggregatorNode()
        result = node._fallback_aggregate(self._sections(), "my request", "unknown_strat")
        assert "Result for: my request" in result

    def test_fallback_report_low_score(self):
        from agents.aggregator import AggregatorNode

        node = AggregatorNode()
        sections = [
            {
                "task_type": "code_generation",
                "title": "Generated Code",
                "specialist": "code",
                "output": "partial",
                "score": 50,
                "specification": "spec",
            },
        ]
        result = node._fallback_report(sections, "my request")
        assert "review recommended" in result

    def test_fallback_report_mid_score(self):
        from agents.aggregator import AggregatorNode

        node = AggregatorNode()
        sections = [
            {
                "task_type": "code_generation",
                "title": "Generated Code",
                "specialist": "code",
                "output": "ok output",
                "score": 75,
                "specification": "spec",
            },
        ]
        result = node._fallback_report(sections, "my request")
        assert "some areas may need attention" in result


class TestAggregatorConvenience:
    """Cover lines 415-416: aggregate_outputs convenience function."""

    def test_aggregate_outputs(self):
        from agents.aggregator import aggregate_outputs

        state = create_initial_state("test request")
        state["sub_tasks"] = [
            {
                "status": "completed",
                "output": "Result",
                "task_type": "code_generation",
                "specialist_adapter": "code",
                "output_score": 88,
                "specification": "spec",
            },
        ]
        result = aggregate_outputs(state, adapter_registry=None)
        assert "aggregated_output" in result


class TestAggregatorLLMException:
    """Cover line 245: LLM aggregation fails with exception."""

    def test_llm_aggregate_exception_falls_back(self):
        from agents.aggregator import AggregatorNode

        mock_adapter = MagicMock()
        mock_adapter.generate.side_effect = RuntimeError("LLM down")

        raw_sections = [
            {
                "task_type": "code_generation",
                "title": "Generated Code",
                "specialist": "code",
                "output": "def bar(): pass",
                "score": 85,
                "specification": "spec",
            }
        ]
        node = AggregatorNode()
        result = node._llm_aggregate(
            mock_adapter, raw_sections, "request", "spec", "merge"
        )
        assert "def bar(): pass" in result


# ════════════════════════════════════════════════════════════════════
# decision_functions.py
# ════════════════════════════════════════════════════════════════════


class TestShouldApproveSubSpecification:
    """Cover lines 116-154: sub-specification approval branches."""

    def test_index_out_of_range(self):
        """Cover lines 116-124: current_index >= len(sub_tasks)."""
        from agents.decision_functions import should_approve_sub_specification

        state = create_initial_state("test")
        state["sub_tasks"] = []
        state["current_sub_task_index"] = 0
        assert should_approve_sub_specification(state) == "fail"

    def test_approved(self):
        """Cover lines 138-140: spec_score >= threshold."""
        from agents.decision_functions import should_approve_sub_specification

        state = create_initial_state("test")
        state["sub_tasks"] = [
            {"spec_score": 90, "iteration_count": 0, "max_iterations": 3},
        ]
        state["current_sub_task_index"] = 0
        state["quality_threshold"] = 85
        assert should_approve_sub_specification(state) == "approved"

    def test_max_iterations(self):
        """Cover lines 143-145: max iterations reached."""
        from agents.decision_functions import should_approve_sub_specification

        state = create_initial_state("test")
        state["sub_tasks"] = [
            {"spec_score": 70, "iteration_count": 2, "max_iterations": 3},
        ]
        state["current_sub_task_index"] = 0
        state["quality_threshold"] = 85
        assert should_approve_sub_specification(state) == "fail"

    def test_score_too_low(self):
        """Cover lines 148-150: spec_score < 60."""
        from agents.decision_functions import should_approve_sub_specification

        state = create_initial_state("test")
        state["sub_tasks"] = [
            {"spec_score": 40, "iteration_count": 0, "max_iterations": 3},
        ]
        state["current_sub_task_index"] = 0
        state["quality_threshold"] = 85
        assert should_approve_sub_specification(state) == "fail"

    def test_refinable(self):
        """Cover lines 153-154: refinable range."""
        from agents.decision_functions import should_approve_sub_specification

        state = create_initial_state("test")
        state["sub_tasks"] = [
            {"spec_score": 70, "iteration_count": 0, "max_iterations": 3},
        ]
        state["current_sub_task_index"] = 0
        state["quality_threshold"] = 85
        assert should_approve_sub_specification(state) == "refine_sub_spec"


class TestShouldApproveSubOutput:
    """Cover lines 170-174: sub-output index out of range."""

    def test_index_out_of_range(self):
        from agents.decision_functions import should_approve_sub_output

        state = create_initial_state("test")
        state["sub_tasks"] = []
        state["current_sub_task_index"] = 5
        assert should_approve_sub_output(state) == "fail"


class TestShouldApproveOutputEdgeCases:
    """Cover lines 86-89: output score ranges in should_approve_output."""

    def test_output_score_below_60_fails(self):
        from agents.decision_functions import should_approve_output

        state = create_initial_state("test")
        state["output_critic_score"] = 40
        state["specialist_iteration_count"] = 0
        state["specialist_max_iterations"] = 3
        state["quality_threshold"] = 85
        assert should_approve_output(state) == "fail"


class TestHasMoreSubtasksEmptyList:
    """Cover line 255-256: empty sub-tasks list edge case."""

    def test_empty_sub_tasks(self):
        from agents.decision_functions import has_more_subtasks

        state = create_initial_state("test")
        state["sub_tasks"] = []
        assert has_more_subtasks(state) == "done"


# ════════════════════════════════════════════════════════════════════
# skill_generator.py
# ════════════════════════════════════════════════════════════════════


class TestSkillGeneratorLoadTemplate:
    """Cover lines 92-97: _load_skill_template with missing/unreadable file."""

    def test_template_missing(self):
        from agents.skill_generator import SkillGeneratorNode

        with patch.object(SkillGeneratorNode, "_TEMPLATE_PATH", Path("/nonexistent/SKILL.md")):
            result = SkillGeneratorNode._load_skill_template()
            assert result is None

    def test_template_read_error(self):
        from agents.skill_generator import SkillGeneratorNode

        mock_path = MagicMock(spec=Path)
        mock_path.is_file.return_value = True
        mock_path.read_text.side_effect = OSError("permission denied")

        with patch.object(SkillGeneratorNode, "_TEMPLATE_PATH", mock_path):
            result = SkillGeneratorNode._load_skill_template()
            assert result is None


class TestSkillGeneratorExecute:
    """Cover lines 122-161: execute with ephemeral skills."""

    def test_execute_generates_ephemeral_skills(self):
        from agents.skill_generator import SkillGeneratorNode

        mock_registry = MagicMock()
        mock_registry.temp_dir = Path("/tmp/test_skills")
        mock_registry.index = {
            "tiers": {"temp": {"skills": {}}, "local": {"skills": {}}, "official": {"skills": {}}}
        }

        node = SkillGeneratorNode(skill_registry=mock_registry)
        node._generate_skill = MagicMock(return_value=("ephemeral-test-gen", Path("/tmp/skill")))

        state = create_initial_state("test request")
        state["specification"] = "Write tests"
        state["discovered_skills"] = [
            {"tier": "ephemeral", "task_type": "test_generation"},
        ]
        result = node.execute(state)

        assert "ephemeral-test-gen" in result["skills_in_use"]
        assert result["debug_info"]["generated_skills"]["count"] == 1

    def test_execute_no_ephemeral_skills(self):
        from agents.skill_generator import SkillGeneratorNode

        mock_registry = MagicMock()
        node = SkillGeneratorNode(skill_registry=mock_registry)

        state = create_initial_state("test request")
        state["discovered_skills"] = [
            {"tier": "official", "task_type": "code_generation"},
        ]
        result = node.execute(state)
        # Should return state unchanged
        assert result["discovered_skills"] == [{"tier": "official", "task_type": "code_generation"}]


class TestSkillGeneratorRefreshExisting:
    """Cover lines 202-204: _generate_skill refresh existing temp skill."""

    def test_refresh_existing_temp_skill(self):
        from agents.skill_generator import SkillGeneratorNode

        mock_registry = MagicMock()
        mock_registry.temp_dir = Path("/tmp/test_skills")
        mock_registry.index = {
            "tiers": {
                "temp": {
                    "skills": {
                        "ephemeral-test-generation": {
                            "usage_count": 5,
                            "avg_score": 80,
                        }
                    }
                },
                "local": {"skills": {}},
                "official": {"skills": {}},
            }
        }

        node = SkillGeneratorNode(skill_registry=mock_registry)
        node._create_skill_content = MagicMock(return_value="# Test skill content")

        with patch("builtins.open", MagicMock()):
            name, path = node._generate_skill("test_generation", "spec")

        assert name == "ephemeral-test-generation"
        mock_registry.security.store_integrity_hash.assert_called_once()
        mock_registry._save_index.assert_called_once()
        # Should NOT call register_skill (just refresh)
        mock_registry.register_skill.assert_not_called()


class TestSkillGeneratorLLMGeneration:
    """Cover lines 411-415: negative examples in _build_llm_prompt."""

    def test_build_llm_prompt_with_rag(self):
        from agents.skill_generator import SkillGeneratorNode

        mock_outcome_store = MagicMock()
        mock_outcome_store.retrieve_positive_examples.return_value = [
            {"skill_content": "## How It Works\n1. Step one\n## Best Practices", "feedback": "Good", "score": 90},
        ]
        mock_outcome_store.retrieve_negative_examples.return_value = [
            {"feedback": "Bad approach", "score": 30},
        ]

        mock_registry = MagicMock()
        node = SkillGeneratorNode(
            skill_registry=mock_registry,
            outcome_store=mock_outcome_store,
        )

        prompt = node._build_llm_prompt("test_generation", "Write unit tests")
        assert "HIGH-SCORING EXAMPLES" in prompt
        assert "LOW-SCORING EXAMPLES" in prompt
        assert "Score 30/100" in prompt

    def test_build_llm_prompt_no_outcome_store(self):
        from agents.skill_generator import SkillGeneratorNode

        mock_registry = MagicMock()
        node = SkillGeneratorNode(skill_registry=mock_registry, outcome_store=None)

        prompt = node._build_llm_prompt("code_generation", "Generate code")
        assert "code generation" in prompt.lower()


class TestSkillGeneratorValidateLLM:
    """Cover lines 439, 441: _validate_llm_output edge cases."""

    def test_validate_too_short(self):
        from agents.skill_generator import SkillGeneratorNode

        assert SkillGeneratorNode._validate_llm_output("") is False
        assert SkillGeneratorNode._validate_llm_output("short") is False

    def test_validate_missing_how_it_works(self):
        from agents.skill_generator import SkillGeneratorNode

        content = "## Best Practices\n1. Practice one\nLong enough content " * 5
        assert SkillGeneratorNode._validate_llm_output(content) is False

    def test_validate_missing_best_practices(self):
        from agents.skill_generator import SkillGeneratorNode

        content = "## How It Works\n1. Step one\nLong enough content " * 5
        assert SkillGeneratorNode._validate_llm_output(content) is False

    def test_validate_missing_numbered_step(self):
        from agents.skill_generator import SkillGeneratorNode

        content = "## How It Works\n## Best Practices\nJust text without numbered steps " * 5
        assert SkillGeneratorNode._validate_llm_output(content) is False

    def test_validate_valid(self):
        from agents.skill_generator import SkillGeneratorNode

        content = "## How It Works\n1. Step one\n2. Step two\n## Best Practices\n1. Practice one\n" + "x" * 50
        assert SkillGeneratorNode._validate_llm_output(content) is True


class TestSkillGeneratorExtractWorkflow:
    """Cover lines 514, 519, 524, 536: _extract_workflow_excerpt edge cases."""

    def test_empty_content(self):
        from agents.skill_generator import SkillGeneratorNode

        assert SkillGeneratorNode._extract_workflow_excerpt("") == ""

    def test_no_how_it_works_marker(self):
        from agents.skill_generator import SkillGeneratorNode

        assert SkillGeneratorNode._extract_workflow_excerpt("## Other Section\nContent") == ""

    def test_marker_at_end_of_content(self):
        from agents.skill_generator import SkillGeneratorNode

        result = SkillGeneratorNode._extract_workflow_excerpt("## How It Works")
        assert result == ""

    def test_truncates_long_excerpt(self):
        from agents.skill_generator import SkillGeneratorNode

        long_content = "## How It Works\n" + "x" * 600 + "\n## Next Section"
        result = SkillGeneratorNode._extract_workflow_excerpt(long_content)
        assert result.endswith("...")
        assert len(result) <= 504  # 500 + "..."


class TestSkillGeneratorRefinement:
    """Cover draft_refined_content anchor-missing branch."""

    def test_refined_content_no_context_anchor(self):
        from agents.skill_generator import SkillGeneratorNode

        mock_registry = MagicMock()
        mock_registry.temp_dir = Path("/tmp/test_skills")
        mock_registry.index = {
            "tiers": {"temp": {"skills": {}}, "local": {"skills": {}}, "official": {"skills": {}}}
        }

        node = SkillGeneratorNode(skill_registry=mock_registry)
        # Produce content that does NOT have "## Context" anchor
        node._create_skill_content = MagicMock(return_value="# Skill\n## How It Works\nContent here")

        result = node.draft_refined_content(
            task_type="code_generation",
            specification="spec",
            original_content="old content",
            feedback="Needs improvement",
            score=40,
        )
        assert "Refinement Directives" in result


class TestSkillGeneratorAllowedTools:
    """Cover lines 799-803: _allowed_tools_for_task branches."""

    def test_code_tasks(self):
        from agents.skill_generator import SkillGeneratorNode

        assert "Bash" in SkillGeneratorNode._allowed_tools_for_task("code_generation")
        assert "Bash" in SkillGeneratorNode._allowed_tools_for_task("test_generation")

    def test_security_audit(self):
        from agents.skill_generator import SkillGeneratorNode

        result = SkillGeneratorNode._allowed_tools_for_task("security_audit")
        assert "Grep" in result
        assert "Bash" in result

    def test_read_only_tasks(self):
        from agents.skill_generator import SkillGeneratorNode

        result = SkillGeneratorNode._allowed_tools_for_task("documentation")
        assert "Read" in result
        assert "WebFetch" in result


class TestSkillGeneratorLLMSectionsWithLLM:
    """Cover lines 260: _generate_sections_with_llm no base model."""

    def test_no_base_model_returns_none(self):
        from agents.skill_generator import SkillGeneratorNode

        mock_registry = MagicMock()
        node = SkillGeneratorNode(skill_registry=mock_registry, base_model=None)
        assert node._generate_sections_with_llm("code", "spec") is None

    def test_llm_exception_returns_none(self):
        from agents.skill_generator import SkillGeneratorNode

        mock_model = MagicMock()
        mock_model.generate.side_effect = RuntimeError("LLM failed")
        mock_registry = MagicMock()
        node = SkillGeneratorNode(
            skill_registry=mock_registry, base_model=mock_model
        )
        assert node._generate_sections_with_llm("code", "spec") is None

    def test_llm_invalid_output_returns_none(self):
        from agents.skill_generator import SkillGeneratorNode

        mock_model = MagicMock()
        mock_model.generate.return_value = "Invalid output without proper sections"
        mock_registry = MagicMock()
        node = SkillGeneratorNode(
            skill_registry=mock_registry, base_model=mock_model
        )
        assert node._generate_sections_with_llm("code", "spec") is None


# ════════════════════════════════════════════════════════════════════
# config.py
# ════════════════════════════════════════════════════════════════════


class TestSystemConfigFromEnv:
    """Cover lines 275, 278-279, 283, 295, 298, 309-325: from_env env var overrides."""

    def test_model_name_override(self):
        """Cover line 275: MODEL_NAME env var."""
        with patch.dict(os.environ, {"MODEL_NAME": "custom-model"}, clear=False):
            from agents.config import SystemConfig
            config = SystemConfig.from_env()
            assert config.model.model_name == "custom-model"

    def test_dev_mode_override(self):
        """Cover lines 278-279: DEV_MODE env var."""
        with patch.dict(os.environ, {"DEV_MODE": "true"}, clear=False):
            from agents.config import SystemConfig
            config = SystemConfig.from_env()
            assert config.dev_mode is True
            assert config.log_level == "DEBUG"

    def test_spending_disabled(self):
        """Cover line 283: VIBE_SPEND_ENABLED=false."""
        with patch.dict(os.environ, {"VIBE_SPEND_ENABLED": "false"}, clear=False):
            from agents.config import SystemConfig
            config = SystemConfig.from_env()
            assert config.spending.enabled is False

    def test_spending_int_overrides(self):
        """Cover lines 295: spending integer overrides."""
        env = {
            "VIBE_SPEND_WINDOW_SECONDS": "7200",
            "VIBE_SPEND_MAX_CENTS": "1000",
        }
        with patch.dict(os.environ, env, clear=False):
            from agents.config import SystemConfig
            config = SystemConfig.from_env()
            assert config.spending.window_seconds == 7200
            assert config.spending.max_cents_per_window == 1000

    def test_spending_db_path_override(self):
        """Cover lines 298: VIBE_SPEND_DB_PATH override."""
        with patch.dict(os.environ, {"VIBE_SPEND_DB_PATH": "/custom/path.db"}, clear=False):
            from agents.config import SystemConfig
            config = SystemConfig.from_env()
            assert config.spending.db_path == "/custom/path.db"

    def test_orchestrator_poll_timeout(self):
        """Cover lines 309-325: orchestrator poll timeout."""
        with patch.dict(os.environ, {"PAPERCLIP_ORCHESTRATOR_POLL_TIMEOUT": "600"}, clear=False):
            from agents.config import SystemConfig
            config = SystemConfig.from_env()
            assert config.paperclip.orchestrator_poll_timeout == 600


class TestSystemConfigValidate:
    """Cover lines 309-325, 332-336: validate method."""

    def test_validate_passes_with_defaults(self):
        from agents.config import SystemConfig
        config = SystemConfig()
        assert config.validate() is True

    def test_validate_fails_empty_model(self):
        from agents.config import SystemConfig
        config = SystemConfig()
        config.model.model_name = ""
        assert config.validate() is False

    def test_validate_fails_mattermost_no_webhook(self):
        from agents.config import SystemConfig
        config = SystemConfig()
        config.mattermost.enabled = True
        config.mattermost.webhook_url = None
        assert config.validate() is False


class TestGetDevConfig:
    """Cover lines 332-336: get_dev_config."""

    def test_dev_config(self):
        from agents.config import get_dev_config
        config = get_dev_config()
        assert config.dev_mode is True
        assert config.log_level == "DEBUG"
        assert config.mattermost.enabled is False


class TestGetProductionConfig:
    """Cover line 341: get_production_config."""

    def test_production_config(self):
        from agents.config import get_production_config
        config = get_production_config()
        assert config is not None


# ════════════════════════════════════════════════════════════════════
# doctor.py
# ════════════════════════════════════════════════════════════════════


class TestDoctorFormatBytes:
    """Cover lines 292-295: _format_bytes edge cases."""

    def test_format_bytes_bytes(self):
        from agents.doctor import _format_bytes
        assert _format_bytes(500) == "500 B"

    def test_format_bytes_kb(self):
        from agents.doctor import _format_bytes
        assert "KB" in _format_bytes(2048)

    def test_format_bytes_mb(self):
        from agents.doctor import _format_bytes
        assert "MB" in _format_bytes(5 * 1024 * 1024)

    def test_format_bytes_gb(self):
        from agents.doctor import _format_bytes
        assert "GB" in _format_bytes(2 * 1024 ** 3)


class TestDoctorDiskUsageLowSpace:
    """Cover lines 245-251, 254: low disk space warning and no data dirs."""

    def test_low_disk_space(self):
        from agents.doctor import check_disk_usage

        # Mock disk_usage to show low space
        low_usage = MagicMock()
        low_usage.free = 500 * 1024 * 1024  # 500MB (less than 1GB)

        with patch("agents.doctor.shutil.disk_usage", return_value=low_usage), \
             patch("agents.doctor._dir_size", return_value=1024), \
             patch("pathlib.Path.exists", return_value=True):
            result = check_disk_usage()
            assert result.status == "warn"
            assert "Low disk" in result.detail

    def test_disk_usage_oserror(self):
        """Cover line 251: OSError in disk_usage."""
        from agents.doctor import check_disk_usage

        with patch("agents.doctor.shutil.disk_usage", side_effect=OSError("not supported")), \
             patch("pathlib.Path.exists", return_value=False):
            result = check_disk_usage()
            assert result.status == "ok"
            assert "No data directories" in result.summary


class TestDoctorCheckHardware:
    """Cover lines 331, 343-344: check_hardware exception and GPU paths."""

    def test_hardware_exception(self):
        from agents.doctor import check_hardware

        with patch("agents.resource_discovery.discover_system", side_effect=RuntimeError("no hw")):
            result = check_hardware()
            assert result.status == "warn"
            assert "Discovery failed" in result.summary

    def test_hardware_with_warnings(self):
        from agents.doctor import check_hardware

        mock_profile = MagicMock()
        mock_profile.cpu_threads = 4
        mock_profile.total_ram_mb = 8192
        mock_profile.has_gpu = False
        mock_profile.gpu_count = 0
        mock_profile.gpus = []
        mock_profile.cpu_model = "Intel i5"

        mock_plan = MagicMock()
        mock_plan.strategy = "cpu_only"
        mock_plan.warnings = ["Low RAM for large models"]

        with patch("agents.resource_discovery.discover_system", return_value=mock_profile), \
             patch("agents.resource_allocator.compute_resource_plan", return_value=mock_plan):
            result = check_hardware()
            assert result.status == "warn"


class TestDoctorCheckSandbox:
    """Cover lines 357-378: check_sandbox paths."""

    def test_sandbox_no_config(self):
        from agents.doctor import check_sandbox

        config = MagicMock(spec=[])  # No sandbox attribute
        del config.sandbox
        result = check_sandbox(config)
        assert result.status == "fail"

    def test_sandbox_sdk_missing(self):
        from agents.doctor import check_sandbox

        config = MagicMock()
        config.sandbox.server_url = "http://localhost:8080"

        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "opensandbox":
                raise ImportError("no sdk")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            result = check_sandbox(config)
            assert result.status == "fail"
            assert "not installed" in result.summary


class TestDoctorCheckDockerGPU:
    """Cover lines 390, 399-404: check_docker_gpu paths."""

    def test_docker_not_available(self):
        from agents.doctor import check_docker_gpu

        with patch("agents.resource_discovery._run_cmd", return_value=None):
            result = check_docker_gpu()
            assert result.status == "warn"
            assert "Docker not available" in result.summary

    def test_docker_no_nvidia(self):
        from agents.doctor import check_docker_gpu

        with patch("agents.resource_discovery._run_cmd", return_value="Docker version 20.10"):
            result = check_docker_gpu()
            assert result.status == "warn"
            assert "nvidia runtime not detected" in result.summary

    def test_docker_with_nvidia(self):
        from agents.doctor import check_docker_gpu

        with patch("agents.resource_discovery._run_cmd", return_value="Docker version 20.10\nnvidia runtime"):
            result = check_docker_gpu()
            assert result.status == "ok"

    def test_docker_gpu_exception(self):
        from agents.doctor import check_docker_gpu

        with patch("agents.resource_discovery._run_cmd", side_effect=RuntimeError("docker broken")):
            result = check_docker_gpu()
            assert result.status == "warn"


class TestDoctorCheckMemory:
    """Cover lines 469-470: check_memory error path."""

    def test_memory_no_db(self):
        from agents.doctor import check_memory

        with patch("pathlib.Path.exists", return_value=False):
            result = check_memory()
            assert result.status == "ok"
            assert "No database yet" in result.summary

    def test_memory_error(self):
        from agents.doctor import check_memory

        with patch("pathlib.Path.exists", return_value=True), \
             patch("sqlite3.connect", side_effect=sqlite3.OperationalError("locked")):
            result = check_memory()
            assert result.status == "fail"


class TestDoctorDirSize:
    """Cover line 132: _dir_size on non-existent/permission error."""

    def test_dir_size_oserror(self):
        from agents.doctor import _dir_size

        mock_path = MagicMock(spec=Path)
        mock_path.rglob.side_effect = OSError("permission denied")
        assert _dir_size(mock_path) == 0


# ════════════════════════════════════════════════════════════════════
# ws_client.py
# ════════════════════════════════════════════════════════════════════


class TestWSClientIsConnected:
    """Cover line 74: is_connected property."""

    def test_is_connected_false_initially(self):
        from agents.ws_client import PaperclipWSClient

        ws = PaperclipWSClient("http://localhost:3100", "comp-1", "key-1")
        assert ws.is_connected is False


class TestWSClientUnsubscribeIdempotent:
    """Cover lines 119-120: unsubscribe with ValueError."""

    def test_double_unsubscribe(self):
        from agents.ws_client import PaperclipWSClient

        ws = PaperclipWSClient("http://localhost:3100", "comp-1", "key-1")
        unsub = ws.subscribe(
            filter_fn=lambda e: True,
            handler_fn=lambda e: None,
        )
        unsub()
        # Second call should not raise
        unsub()


class TestWSClientRunLoop:
    """Cover lines 139-140: _run_loop exception handling."""

    def test_run_loop_exception(self):
        from agents.ws_client import PaperclipWSClient

        ws = PaperclipWSClient("http://localhost:3100", "comp-1", "key-1")
        ws._stop_event.set()

        # _connect_loop will exit immediately due to stop_event
        ws._run_loop()


class TestWSClientConnectLoopNoWebsockets:
    """Cover lines 149-151: websockets not installed."""

    def test_no_websockets(self):
        from agents.ws_client import PaperclipWSClient

        ws = PaperclipWSClient("http://localhost:3100", "comp-1", "key-1")

        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "websockets":
                raise ImportError("no websockets")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(ws._connect_loop())
            finally:
                loop.close()


class TestWSClientDispatch:
    """Cover lines 165-175, 179, 185: _dispatch edge cases."""

    def test_dispatch_bytes_message(self):
        from agents.ws_client import PaperclipWSClient

        ws = PaperclipWSClient("http://localhost:3100", "comp-1", "key-1")
        received = []
        ws.subscribe(
            filter_fn=lambda e: True,
            handler_fn=lambda e: received.append(e),
        )
        ws._dispatch(b'{"type": "test"}')
        assert len(received) == 1
        assert received[0]["type"] == "test"

    def test_dispatch_invalid_json(self):
        from agents.ws_client import PaperclipWSClient

        ws = PaperclipWSClient("http://localhost:3100", "comp-1", "key-1")
        # Should not raise
        ws._dispatch("not json at all{{{")

    def test_dispatch_handler_exception(self):
        from agents.ws_client import PaperclipWSClient

        ws = PaperclipWSClient("http://localhost:3100", "comp-1", "key-1")

        def bad_handler(e):
            raise RuntimeError("handler boom")

        ws.subscribe(
            filter_fn=lambda e: True,
            handler_fn=bad_handler,
        )
        # Should not raise — errors are caught
        ws._dispatch(json.dumps({"type": "test"}))

    def test_dispatch_unicode_error(self):
        from agents.ws_client import PaperclipWSClient

        ws = PaperclipWSClient("http://localhost:3100", "comp-1", "key-1")
        # Invalid UTF-8 bytes
        ws._dispatch(b'\x80\x81\x82')


# ════════════════════════════════════════════════════════════════════
# parallel_subtasks.py
# ════════════════════════════════════════════════════════════════════


class TestParallelSubtasksRunSingleClarification:
    """Cover lines 110-114: clarification_needed in run_single_subtask."""

    def test_clarification_breaks_loop(self):
        from agents.parallel_subtasks import run_single_subtask

        mock_nodes = MagicMock()

        def execute_sub_task(state):
            state["clarification_needed"] = True
            state["sub_tasks"][0]["status"] = "clarification_needed"
            state["sub_tasks"][0]["output"] = "CLARIFICATION_NEEDED: What is the scope?"
            return state

        mock_nodes.execute_sub_task.side_effect = execute_sub_task
        mock_nodes.evaluate_sub_output = MagicMock()

        shared_context = {
            "user_request": "Build something",
            "specification": "spec",
            "loaded_skills": [],
            "memory_context": "",
            "quality_threshold": 85,
            "max_iterations": 3,
            "specialist_max_iterations": 3,
            "session_id": "test",
        }
        sub_task = {
            "task_type": "code_generation",
            "status": "pending",
            "output": "",
            "output_score": 0,
            "iteration_count": 0,
            "max_iterations": 3,
        }

        result = run_single_subtask(
            sub_task_index=0,
            nodes=mock_nodes,
            shared_context=shared_context,
            sub_task_dict=sub_task,
        )
        # evaluate_sub_output should NOT be called
        mock_nodes.evaluate_sub_output.assert_not_called()


class TestParallelSubtasksExecuteErrors:
    """Cover lines 229-241: timeout errors in execute_parallel_subtasks."""

    def test_future_exception(self):
        from agents.parallel_subtasks import execute_parallel_subtasks

        state = create_initial_state("test")
        state["sub_tasks"] = [
            {
                "task_type": "code_generation",
                "status": "pending",
                "output": "",
                "output_score": 0,
                "iteration_count": 0,
                "max_iterations": 3,
            },
        ]

        mock_nodes = MagicMock()

        # Patch run_single_subtask to raise
        with patch(
            "agents.parallel_subtasks.run_single_subtask",
            side_effect=RuntimeError("thread boom"),
        ):
            result = execute_parallel_subtasks(state, mock_nodes)

        assert len(result["parallel_execution_errors"]) == 1
        assert result["parallel_execution_errors"][0]["error"] == "thread boom"
        assert result["sub_tasks"][0]["status"] == "failed"


class TestParallelSubtasksClarificationPropagation:
    """Cover lines 260-262, 265-266: clarification questions propagation."""

    def test_clarification_questions_propagated(self):
        from agents.parallel_subtasks import execute_parallel_subtasks

        state = create_initial_state("test")
        state["sub_tasks"] = [
            {
                "task_type": "code_generation",
                "status": "pending",
                "output": "",
                "output_score": 0,
                "iteration_count": 0,
                "max_iterations": 3,
            },
        ]

        # Use the proper <clarification_needed> tag format
        clarification_output = (
            "<clarification_needed>\n"
            "1. What API should we use?\n"
            "2. What format?\n"
            "</clarification_needed>"
        )

        def fake_run(*args, **kwargs):
            return {
                "task_type": "code_generation",
                "status": "clarification_needed",
                "output": clarification_output,
                "output_score": 0,
                "iteration_count": 1,
                "max_iterations": 3,
            }

        with patch("agents.parallel_subtasks.run_single_subtask", side_effect=fake_run):
            result = execute_parallel_subtasks(state, MagicMock())

        assert result.get("clarification_needed") is True
        assert len(result.get("clarification_questions", [])) == 2


class TestParallelSubtasksEmptyList:
    """Cover empty sub-tasks list path."""

    def test_empty_subtasks(self):
        from agents.parallel_subtasks import execute_parallel_subtasks

        state = create_initial_state("test")
        state["sub_tasks"] = []
        result = execute_parallel_subtasks(state, MagicMock())
        assert result["sub_tasks"] == []


class TestParallelSubtasksWithConfig:
    """Cover config-reading path."""

    def test_with_config_object(self):
        from agents.parallel_subtasks import execute_parallel_subtasks

        state = create_initial_state("test")
        state["sub_tasks"] = [
            {
                "task_type": "code_generation",
                "status": "pending",
                "output": "",
                "output_score": 0,
                "iteration_count": 0,
                "max_iterations": 3,
            },
        ]

        config = MagicMock()
        config.workflow.parallel_max_workers = 2
        config.workflow.parallel_subtask_timeout = 60

        def fake_run(*args, **kwargs):
            return {
                "task_type": "code_generation",
                "status": "completed",
                "output": "done",
                "output_score": 90,
                "iteration_count": 1,
                "max_iterations": 3,
            }

        with patch("agents.parallel_subtasks.run_single_subtask", side_effect=fake_run):
            result = execute_parallel_subtasks(state, MagicMock(), config=config)

        assert result["completed_sub_tasks"] == 1
