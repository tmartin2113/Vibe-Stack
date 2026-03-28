"""
Tests targeting uncovered lines in:
- agents/specialist_nodes.py  (execute_with_specialist, execute_sub_task tool loops)
- agents/tools/registry.py    (create_default_tool_registry, create_subprocess_tool_registry,
                                DevToolWrapper edge cases, PythonExecutor._apply_resource_limits,
                                MemoryStoreTool, MemoryRecallTool, ShellExecutor, WebFetchTool)
- agents/api_key_manager.py   (_prompt_user_for_key, _prompt_via_mattermost,
                                _prompt_via_slack, _load_stored_keys failures,
                                _save_key failures, get_error_message with messenger)
"""

import json
import os
import time

os.environ["VIBE_DISABLE_REMOTE_SKILLS"] = "1"

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, PropertyMock, call
from pathlib import Path

import pytest

from agents.state import create_initial_state, AgentState
from agents.nodes import AgentNodes
from agents.adapters import PromptAdapter, AdapterRegistry
from agents.tools.registry import (
    ToolResult,
    ToolCategory,
    ToolRegistry,
    PythonExecutor,
    DevToolWrapper,
    WebFetchTool,
    ShellExecutor,
    MemoryStoreTool,
    MemoryRecallTool,
    _build_allowed_file_dirs,
    _DEFAULT_ALLOWED_FILE_DIRS,
    create_subprocess_tool_registry,
)
from agents.specialist_nodes import (
    SpecialistNodesMixin,
    parse_clarification,
    MAX_TOOL_CALLING_ITERATIONS,
    _CLARIFICATION_INSTRUCTION,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry(responses=None):
    """Create an AdapterRegistry with mock adapters."""
    responses = responses or {}
    registry = AdapterRegistry()
    for name in ["vibe", "critic", "refinement", "code_expert", "general",
                 "test_generator", "security_auditor", "doc_generator",
                 "creative_writer"]:
        resp = responses.get(name, f"Default response from {name}")
        model = MagicMock()
        if isinstance(resp, list):
            model.generate.side_effect = resp
        else:
            model.generate.return_value = resp
        adapter = PromptAdapter(
            name=name,
            system_prompt=f"You are {name}.",
            base_model=model,
        )
        registry.register(adapter)
    return registry


def _make_nodes(responses=None, config=None):
    """Create an AgentNodes instance with mock adapters and a mock tool registry."""
    registry = _make_registry(responses)
    tool_reg = ToolRegistry()
    # Register a mock tool for tool-call tests
    mock_tool = MagicMock()
    mock_tool.name = "test_tool"
    mock_tool.description = "A test tool"
    mock_tool.category = ToolCategory.SPECIALIZED
    mock_tool.enabled = True
    mock_tool.get_schema.return_value = {
        "name": "test_tool",
        "description": "A test tool",
        "parameters": {},
    }
    mock_tool.validate_params.return_value = None
    mock_tool.execute.return_value = ToolResult(success=True, output="tool output")
    tool_reg.register(mock_tool)
    nodes = AgentNodes(registry, tool_reg, config=config)
    return nodes


def _make_state(**overrides):
    """Create a fresh AgentState with sensible defaults, allowing overrides."""
    state = create_initial_state(
        user_request="Write a sorting algorithm",
        max_iterations=3,
        quality_threshold=85,
    )
    state.update(overrides)
    return state


# ===========================================================================
# 1. specialist_nodes.py — execute_with_specialist (lines 194, 199, 229-233,
#    282, 298, 313, 342-420, 424-429)
# ===========================================================================


class TestExecuteWithSpecialist:
    """Tests for the execute_with_specialist tool calling loop."""

    def test_first_iteration_no_tools_no_tool_call(self):
        """Line 282: specialist_name not in tool_enabled_specialists, no tools."""
        nodes = _make_nodes(responses={"creative_writer": "A beautiful poem"})
        state = _make_state(
            specialist_adapter="creative_writer",
            specialist_iteration_count=0,
            routed_task_type="creative",
        )
        result = nodes.execute_with_specialist(state)
        assert result["specialist_output"] == "A beautiful poem"
        assert result["current_output"] == "A beautiful poem"
        assert "creative_writer" in result["adapters_used"]

    def test_skill_content_truncation(self):
        """Line 194: skill content > 3000 chars gets truncated."""
        long_content = "x" * 4000
        nodes = _make_nodes(responses={"vibe": "output with truncated skill"})
        state = _make_state(
            specialist_adapter="vibe",
            specialist_iteration_count=0,
            loaded_skills=[
                {"name": "big_skill", "content": long_content}
            ],
        )
        result = nodes.execute_with_specialist(state)
        assert result["specialist_output"] == "output with truncated skill"

    def test_skill_with_empty_content(self):
        """Line 199: skill with no content shows '(content unavailable)'."""
        nodes = _make_nodes(responses={"vibe": "output with empty skill"})
        state = _make_state(
            specialist_adapter="vibe",
            specialist_iteration_count=0,
            loaded_skills=[
                {"name": "empty_skill", "content": ""}
            ],
        )
        result = nodes.execute_with_specialist(state)
        assert result["specialist_output"] == "output with empty skill"

    def test_refinement_iteration_prompt(self):
        """Lines 229-233: specialist_iteration > 0 builds refinement prompt."""
        nodes = _make_nodes(responses={"vibe": "improved output"})
        state = _make_state(
            specialist_adapter="vibe",
            specialist_iteration_count=1,
            specialist_output="previous attempt",
            output_critic_feedback="needs improvement",
            output_critic_score=60,
        )
        result = nodes.execute_with_specialist(state)
        assert result["specialist_output"] == "improved output"

    def test_skill_generation_config_override(self):
        """Line 298: skill_gen_config overrides base config."""
        nodes = _make_nodes(responses={"vibe": "output"})
        state = _make_state(
            specialist_adapter="vibe",
            specialist_iteration_count=0,
            loaded_skills=[
                {"name": "skill1", "content": "content",
                 "generation_config": {"temperature": 0.1, "max_tokens": 500}}
            ],
        )
        result = nodes.execute_with_specialist(state)
        assert result["specialist_output"] == "output"

    def test_multi_turn_history_on_refinement(self):
        """Line 313: multi_turn_history passed when specialist_iteration > 0."""
        nodes = _make_nodes(responses={"vibe": "refined output"})
        state = _make_state(
            specialist_adapter="vibe",
            specialist_iteration_count=1,
            specialist_output="prev output",
            output_critic_feedback="improve this",
            output_critic_score=50,
            conversation_history=[
                {"output": "attempt 1", "feedback": "do better"},
            ],
        )
        result = nodes.execute_with_specialist(state)
        assert result["specialist_output"] == "refined output"

    def test_tool_call_success_loop(self):
        """Lines 342-404: tool call is parsed, executed, continuation prompt sent."""
        tool_call_output = 'I will run a tool <tool_call name="test_tool">{"key": "val"}</tool_call>'
        final_output = "Final answer after tool"
        nodes = _make_nodes(responses={"vibe": [tool_call_output, final_output]})
        state = _make_state(
            specialist_adapter="vibe",
            specialist_iteration_count=0,
        )
        result = nodes.execute_with_specialist(state)
        assert result["specialist_output"] == final_output
        assert len(result["tool_calls_made"]) == 1
        assert result["tool_calls_made"][0]["tool"] == "test_tool"

    def test_tool_call_failure_result(self):
        """Lines 374-377: tool execution returns failure."""
        tool_call_output = 'Run <tool_call name="test_tool">{"a": "b"}</tool_call>'
        final_output = "Handled failure"

        nodes = _make_nodes(responses={"vibe": [tool_call_output, final_output]})
        # Make the tool return a failure
        tool = nodes.tool_registry.tools["test_tool"]
        tool.execute.return_value = ToolResult(success=False, output="", error="tool failed")

        state = _make_state(specialist_adapter="vibe", specialist_iteration_count=0)
        result = nodes.execute_with_specialist(state)
        assert result["tool_calls_made"][0]["result"]["success"] is False

    def test_tool_call_exception_breaks_loop(self):
        """Lines 406-420: exception in tool calling loop breaks and logs error."""
        tool_call_output = 'Run <tool_call name="test_tool">{"a": "b"}</tool_call>'
        nodes = _make_nodes(responses={"vibe": tool_call_output})

        # Make parse_tool_call raise on first parse after initial generation
        original_parse = nodes.tool_registry.parse_tool_call

        call_count = [0]

        def parse_side_effect(output):
            call_count[0] += 1
            if call_count[0] == 1:
                return original_parse(output)
            return None

        # Make execute_tool raise
        nodes.tool_registry.execute_tool = MagicMock(side_effect=RuntimeError("boom"))

        state = _make_state(specialist_adapter="vibe", specialist_iteration_count=0)
        result = nodes.execute_with_specialist(state)
        # The error entry should be in tool_calls_made
        assert any(t["tool"] == "error" for t in result["tool_calls_made"])

    def test_max_tool_iterations_with_remaining_call(self):
        """Lines 424-429: tool iteration limit reached with remaining unparsed tool call."""
        # Generate responses that always contain tool calls
        tool_output = 'Still calling <tool_call name="test_tool">{"a": "b"}</tool_call>'
        responses = [tool_output] * (MAX_TOOL_CALLING_ITERATIONS + 2)
        nodes = _make_nodes(responses={"vibe": responses})

        state = _make_state(specialist_adapter="vibe", specialist_iteration_count=0)
        result = nodes.execute_with_specialist(state)
        assert "Tool iteration limit reached" in result["specialist_output"]

    def test_skill_tools_enabled_true_overrides(self):
        """skill tools_enabled=True overrides the hardcoded set."""
        nodes = _make_nodes(responses={"creative_writer": "output"})
        state = _make_state(
            specialist_adapter="creative_writer",
            specialist_iteration_count=0,
            loaded_skills=[
                {"name": "sk", "content": "c", "tools_enabled": True}
            ],
        )
        result = nodes.execute_with_specialist(state)
        # creative_writer is NOT in tool_enabled_specialists but skills override
        assert result["specialist_output"] == "output"

    def test_skill_tools_enabled_false_disables(self):
        """skill tools_enabled=False disables tools even for normally-enabled specialist."""
        nodes = _make_nodes(responses={"vibe": "output"})
        state = _make_state(
            specialist_adapter="vibe",
            specialist_iteration_count=0,
            loaded_skills=[
                {"name": "sk", "content": "c", "tools_enabled": False}
            ],
        )
        result = nodes.execute_with_specialist(state)
        # vibe IS in tool_enabled_specialists but skill says False
        assert result["specialist_output"] == "output"

    def test_tool_call_blocked_by_skill_permissions(self):
        """Lines 348-363: tool call blocked by skill security permissions."""
        tool_call_output = 'Run <tool_call name="test_tool">{"a": "b"}</tool_call>'
        final_output = "No more tools"
        nodes = _make_nodes(responses={"vibe": [tool_call_output, final_output]})

        state = _make_state(
            specialist_adapter="vibe",
            specialist_iteration_count=0,
            loaded_skills=[
                {"name": "sk", "content": "c", "allowed_tools": {"other_tool"}}
            ],
        )
        result = nodes.execute_with_specialist(state)
        # The blocked tool call should appear in history with error
        blocked = result["tool_calls_made"][0]
        assert blocked["result"]["success"] is False
        assert "not permitted" in blocked["result"]["error"]

    def test_clarification_skips_tool_loop(self):
        """Lines 320-331: clarification_needed in output skips tool loop."""
        clarification_output = """Here are my questions:
<clarification_needed>
1. What database engine?
2. What is the volume?
</clarification_needed>"""
        nodes = _make_nodes(responses={"vibe": clarification_output})
        state = _make_state(specialist_adapter="vibe", specialist_iteration_count=0)
        result = nodes.execute_with_specialist(state)
        assert result["clarification_needed"] is True
        assert len(result["clarification_questions"]) == 2
        assert result["specialist_output"] == clarification_output


# ===========================================================================
# 2. specialist_nodes.py — execute_sub_task (lines 492-758)
# ===========================================================================


class TestExecuteSubTask:
    """Tests for the execute_sub_task tool calling loop."""

    def _make_sub_task_state(self, **overrides):
        """Create state with sub_tasks for execute_sub_task."""
        sub_task = {
            "specification": "Implement sorting logic",
            "specialist_adapter": "vibe",
            "iteration_count": 0,
            "task_type": "code",
            "status": "pending",
        }
        sub_task.update(overrides.pop("sub_task_overrides", {}))
        defaults = dict(
            sub_tasks=[sub_task],
            current_sub_task_index=0,
        )
        defaults.update(overrides)
        state = _make_state(**defaults)
        return state

    def test_basic_execution(self):
        """Lines 492-758: basic sub-task execution."""
        nodes = _make_nodes(responses={"vibe": "sub-task output"})
        state = self._make_sub_task_state()
        result = nodes.execute_sub_task(state)
        assert result["sub_tasks"][0]["output"] == "sub-task output"
        assert result["sub_tasks"][0]["status"] == "executed"

    def test_index_beyond_sub_tasks_returns_state(self):
        """Line 495-496: current_sub_task_index >= len(sub_tasks) returns early."""
        nodes = _make_nodes()
        state = self._make_sub_task_state(current_sub_task_index=5)
        result = nodes.execute_sub_task(state)
        assert result["sub_tasks"][0]["status"] == "pending"  # unchanged

    def test_sequential_sibling_context(self):
        """Lines 507-526: sequential mode builds sibling output context."""
        nodes = _make_nodes(responses={"vibe": "task 2 output"})
        state = _make_state(
            parallel_execution=False,
            current_sub_task_index=1,
            sub_tasks=[
                {
                    "specification": "task 1",
                    "specialist_adapter": "vibe",
                    "iteration_count": 0,
                    "task_type": "code",
                    "status": "completed",
                    "output": "task 1 output",
                    "output_score": 85,
                },
                {
                    "specification": "task 2",
                    "specialist_adapter": "vibe",
                    "iteration_count": 0,
                    "task_type": "code",
                    "status": "pending",
                },
            ],
        )
        result = nodes.execute_sub_task(state)
        assert result["sub_tasks"][1]["output"] == "task 2 output"

    def test_sequential_sibling_long_output_truncated(self):
        """Line 517-518: sibling outputs > 800 chars are truncated."""
        nodes = _make_nodes(responses={"vibe": "task 2 output"})
        state = _make_state(
            parallel_execution=False,
            current_sub_task_index=1,
            sub_tasks=[
                {
                    "specification": "task 1",
                    "specialist_adapter": "vibe",
                    "iteration_count": 0,
                    "task_type": "code",
                    "status": "completed",
                    "output": "x" * 1000,
                    "output_score": 80,
                },
                {
                    "specification": "task 2",
                    "specialist_adapter": "vibe",
                    "iteration_count": 0,
                    "task_type": "code",
                    "status": "pending",
                },
            ],
        )
        result = nodes.execute_sub_task(state)
        assert result["sub_tasks"][1]["status"] == "executed"

    def test_refinement_iteration_sub_task(self):
        """Lines 570-591: sub-task iteration > 0 builds refinement prompt."""
        nodes = _make_nodes(responses={"vibe": "refined sub-task"})
        state = self._make_sub_task_state(
            sub_task_overrides={
                "iteration_count": 1,
                "output": "previous sub-task output",
                "output_feedback": "needs improvement",
                "output_score": 55,
            },
        )
        result = nodes.execute_sub_task(state)
        assert result["sub_tasks"][0]["output"] == "refined sub-task"

    def test_sub_task_tool_call_success(self):
        """Lines 650-722: sub-task tool calling loop success path."""
        tool_call_output = 'Run <tool_call name="test_tool">{"key": "val"}</tool_call>'
        final_output = "Final sub-task after tool"
        nodes = _make_nodes(responses={"vibe": [tool_call_output, final_output]})
        state = self._make_sub_task_state()
        result = nodes.execute_sub_task(state)
        assert result["sub_tasks"][0]["output"] == final_output
        assert len(result["sub_tasks"][0]["tool_calls"]) == 1

    def test_sub_task_tool_call_exception(self):
        """Lines 724-736: exception in sub-task tool loop breaks loop."""
        tool_call_output = 'Run <tool_call name="test_tool">{"a": "b"}</tool_call>'
        nodes = _make_nodes(responses={"vibe": tool_call_output})
        nodes.tool_registry.execute_tool = MagicMock(side_effect=RuntimeError("boom"))

        state = self._make_sub_task_state()
        result = nodes.execute_sub_task(state)
        tool_calls = result["sub_tasks"][0]["tool_calls"]
        assert any(t["tool"] == "error" for t in tool_calls)

    def test_sub_task_max_tool_iterations(self):
        """Lines 739-744: sub-task tool iteration limit reached."""
        tool_output = 'Call <tool_call name="test_tool">{"a": "b"}</tool_call>'
        responses = [tool_output] * (MAX_TOOL_CALLING_ITERATIONS + 2)
        nodes = _make_nodes(responses={"vibe": responses})

        state = self._make_sub_task_state()
        result = nodes.execute_sub_task(state)
        assert "Tool iteration limit reached" in result["sub_tasks"][0]["output"]

    def test_sub_task_tool_blocked_by_skills(self):
        """Lines 665-681: sub-task tool call blocked by skill permissions."""
        tool_call_output = 'Run <tool_call name="test_tool">{"a": "b"}</tool_call>'
        final_output = "Done"
        nodes = _make_nodes(responses={"vibe": [tool_call_output, final_output]})

        state = self._make_sub_task_state(
            loaded_skills=[
                {"name": "sk", "content": "c", "task_type": "code",
                 "allowed_tools": {"other_tool"}}
            ],
        )
        result = nodes.execute_sub_task(state)
        blocked = result["sub_tasks"][0]["tool_calls"][0]
        assert blocked["result"]["success"] is False
        assert "not permitted" in blocked["result"]["error"]

    def test_sub_task_tool_success_failure_result(self):
        """Lines 687-695: sub-task tool returns FAILED status."""
        tool_call_output = 'Run <tool_call name="test_tool">{"a": "b"}</tool_call>'
        final_output = "Handled failure"
        nodes = _make_nodes(responses={"vibe": [tool_call_output, final_output]})
        tool = nodes.tool_registry.tools["test_tool"]
        tool.execute.return_value = ToolResult(success=False, output="", error="tool broke")

        state = self._make_sub_task_state()
        result = nodes.execute_sub_task(state)
        assert result["sub_tasks"][0]["tool_calls"][0]["result"]["success"] is False

    def test_sub_task_clarification_skips_tools(self):
        """Lines 624-636: sub-task clarification skips tool loop."""
        clarification_output = """<clarification_needed>
1. What framework to use?
</clarification_needed>"""
        nodes = _make_nodes(responses={"vibe": clarification_output})
        state = self._make_sub_task_state()
        result = nodes.execute_sub_task(state)
        assert result["clarification_needed"] is True
        assert result["sub_tasks"][0]["status"] == "clarification_needed"

    def test_sub_task_skill_context_truncation(self):
        """Lines 541-544: skill content > 2000 chars gets truncated for sub-tasks."""
        long_content = "y" * 3000
        nodes = _make_nodes(responses={"vibe": "output"})
        state = self._make_sub_task_state(
            loaded_skills=[
                {"name": "big_skill", "content": long_content, "task_type": "code"}
            ],
        )
        result = nodes.execute_sub_task(state)
        assert result["sub_tasks"][0]["output"] == "output"

    def test_sub_task_no_relevant_skills_falls_back(self):
        """Lines 533-535: no relevant skills falls back to all loaded skills."""
        nodes = _make_nodes(responses={"vibe": "output"})
        state = self._make_sub_task_state(
            loaded_skills=[
                {"name": "sk", "content": "content", "task_type": "different_type"}
            ],
        )
        result = nodes.execute_sub_task(state)
        assert result["sub_tasks"][0]["output"] == "output"

    def test_sub_task_skill_adapter_prompt_override(self):
        """Line 594-595: skill adapter_prompt overrides specialist prompt."""
        nodes = _make_nodes(responses={"vibe": "custom adapter output"})
        state = self._make_sub_task_state(
            loaded_skills=[
                {"name": "sk", "content": "c", "task_type": "code",
                 "adapter_prompt": "You are a custom agent."}
            ],
        )
        result = nodes.execute_sub_task(state)
        assert result["sub_tasks"][0]["output"] == "custom adapter output"

    def test_sub_task_skill_gen_config_override(self):
        """Lines 599-601: skill generation_config overrides base config."""
        nodes = _make_nodes(responses={"vibe": "output"})
        state = self._make_sub_task_state(
            loaded_skills=[
                {"name": "sk", "content": "c", "task_type": "code",
                 "generation_config": {"temperature": 0.9}}
            ],
        )
        result = nodes.execute_sub_task(state)
        assert result["sub_tasks"][0]["output"] == "output"

    def test_sub_task_no_tool_access_for_non_enabled_specialist(self):
        """Lines 603-615: specialist not in tool_enabled_specialists has no tool access."""
        nodes = _make_nodes(responses={"creative_writer": "creative output"})
        state = _make_state(
            sub_tasks=[{
                "specification": "Write creatively",
                "specialist_adapter": "creative_writer",
                "iteration_count": 0,
                "task_type": "creative",
                "status": "pending",
            }],
            current_sub_task_index=0,
        )
        result = nodes.execute_sub_task(state)
        # creative_writer is not in tool_enabled_specialists, no tool_calls key
        assert "tool_calls" not in result["sub_tasks"][0]


# ===========================================================================
# 3. tools/registry.py — create_subprocess_tool_registry (lines 1356-1430)
# ===========================================================================


class TestCreateSubprocessToolRegistry:
    """Tests for create_subprocess_tool_registry factory."""

    def test_basic_creation(self):
        """Lines 1356-1430: create subprocess registry with no env vars."""
        env_clean = {
            k: v for k, v in os.environ.items()
            if k not in ("SEARXNG_URL", "PLAYWRIGHT_WS_URL",
                         "PENPOT_API_URL", "COMFYUI_URL", "GITEA_URL",
                         "MINIO_URL", "BULLETIN_PATH")
        }
        with patch.dict(os.environ, env_clean, clear=True):
            registry = create_subprocess_tool_registry(network_egress=False)

        tool_names = registry.list_tools()
        # Core subprocess tools should be present
        assert "python_executor" in tool_names
        assert "pytest_runner" in tool_names
        assert "bandit" in tool_names
        assert "shell_executor" in tool_names
        # Memory tools
        assert "memory_store" in tool_names
        assert "memory_recall" in tool_names
        # File tools
        assert "file_reader" in tool_names
        assert "file_writer" in tool_names
        # Dev tools (wrapped via DevToolWrapper; names come from inner tool)
        assert "static_code_analyzer" in tool_names
        assert "codebase_search" in tool_names
        assert "git_operations" in tool_names
        assert "data_parser" in tool_names
        # Web fetch should NOT be present
        assert "web_fetch" not in tool_names

    def test_with_network_egress(self):
        """Web fetch tool is registered when network_egress=True."""
        env_clean = {
            k: v for k, v in os.environ.items()
            if k not in ("SEARXNG_URL", "PLAYWRIGHT_WS_URL",
                         "PENPOT_API_URL", "COMFYUI_URL", "GITEA_URL",
                         "MINIO_URL", "BULLETIN_PATH")
        }
        with patch.dict(os.environ, env_clean, clear=True):
            registry = create_subprocess_tool_registry(network_egress=True)
        assert "web_fetch" in registry.list_tools()

    def test_with_searxng_env(self):
        """SEARXNG_URL env var enables web_search tool."""
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("PLAYWRIGHT_WS_URL",
                         "PENPOT_API_URL", "COMFYUI_URL", "GITEA_URL",
                         "MINIO_URL", "BULLETIN_PATH")
        }
        env["SEARXNG_URL"] = "http://searxng:8080"
        with patch.dict(os.environ, env, clear=True):
            registry = create_subprocess_tool_registry()
        assert "web_search" in registry.list_tools()

    def test_with_playwright_env(self):
        """PLAYWRIGHT_WS_URL env var enables web_scrape and browser_automation tools."""
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("SEARXNG_URL",
                         "PENPOT_API_URL", "COMFYUI_URL", "GITEA_URL",
                         "MINIO_URL", "BULLETIN_PATH")
        }
        env["PLAYWRIGHT_WS_URL"] = "ws://pw:1234"
        with patch.dict(os.environ, env, clear=True):
            registry = create_subprocess_tool_registry()
        assert "web_scrape" in registry.list_tools()
        assert "browser_automation" in registry.list_tools()

    def test_with_penpot_env(self):
        """PENPOT_API_URL env var enables design tool."""
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("SEARXNG_URL", "PLAYWRIGHT_WS_URL",
                         "COMFYUI_URL", "GITEA_URL",
                         "MINIO_URL", "BULLETIN_PATH")
        }
        env["PENPOT_API_URL"] = "http://penpot:8080"
        with patch.dict(os.environ, env, clear=True):
            registry = create_subprocess_tool_registry()
        assert "design" in registry.list_tools()

    def test_with_comfyui_env(self):
        """COMFYUI_URL env var enables image_generation tool."""
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("SEARXNG_URL", "PLAYWRIGHT_WS_URL",
                         "PENPOT_API_URL", "GITEA_URL",
                         "MINIO_URL", "BULLETIN_PATH")
        }
        env["COMFYUI_URL"] = "http://comfyui:8080"
        with patch.dict(os.environ, env, clear=True):
            registry = create_subprocess_tool_registry()
        assert "image_generation" in registry.list_tools()

    def test_with_gitea_env(self):
        """GITEA_URL env var enables git_forge tool."""
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("SEARXNG_URL", "PLAYWRIGHT_WS_URL",
                         "PENPOT_API_URL", "COMFYUI_URL",
                         "MINIO_URL", "BULLETIN_PATH")
        }
        env["GITEA_URL"] = "http://gitea:3000"
        with patch.dict(os.environ, env, clear=True):
            registry = create_subprocess_tool_registry()
        assert "git_forge" in registry.list_tools()

    def test_with_minio_env(self):
        """MINIO_URL env var enables artifact_storage tool."""
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("SEARXNG_URL", "PLAYWRIGHT_WS_URL",
                         "PENPOT_API_URL", "COMFYUI_URL", "GITEA_URL",
                         "BULLETIN_PATH")
        }
        env["MINIO_URL"] = "http://minio:9000"
        with patch.dict(os.environ, env, clear=True):
            registry = create_subprocess_tool_registry()
        assert "artifact_storage" in registry.list_tools()

    def test_with_bulletin_env(self):
        """BULLETIN_PATH env var enables bulletin_board tool."""
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("SEARXNG_URL", "PLAYWRIGHT_WS_URL",
                         "PENPOT_API_URL", "COMFYUI_URL", "GITEA_URL",
                         "MINIO_URL")
        }
        env["BULLETIN_PATH"] = "/tmp/bulletin"
        with patch.dict(os.environ, env, clear=True):
            registry = create_subprocess_tool_registry()
        assert "bulletin_board" in registry.list_tools()

    def test_with_custom_allowed_file_dirs(self):
        """allowed_file_dirs parameter is forwarded."""
        env_clean = {
            k: v for k, v in os.environ.items()
            if k not in ("SEARXNG_URL", "PLAYWRIGHT_WS_URL",
                         "PENPOT_API_URL", "COMFYUI_URL", "GITEA_URL",
                         "MINIO_URL", "BULLETIN_PATH")
        }
        with patch.dict(os.environ, env_clean, clear=True):
            registry = create_subprocess_tool_registry(
                allowed_file_dirs=["/tmp", "/custom"]
            )
        assert "file_reader" in registry.list_tools()


# ===========================================================================
# 4. tools/registry.py — PythonExecutor._apply_resource_limits (lines 189-213)
# ===========================================================================


class TestPythonExecutorResourceLimits:
    """Test _apply_resource_limits static method."""

    def test_apply_resource_limits_unix(self):
        """Lines 189-213: resource limits are set on Unix."""
        mock_resource = MagicMock()
        mock_resource.RLIMIT_CPU = 0
        mock_resource.RLIMIT_AS = 5
        mock_resource.RLIMIT_FSIZE = 6
        mock_resource.RLIMIT_NPROC = 7
        mock_resource.setrlimit = MagicMock()

        import sys
        original = sys.modules.get("resource")
        sys.modules["resource"] = mock_resource
        try:
            PythonExecutor._apply_resource_limits()
            assert mock_resource.setrlimit.call_count == 4
        finally:
            if original is not None:
                sys.modules["resource"] = original
            else:
                sys.modules.pop("resource", None)

    def test_apply_resource_limits_import_error(self):
        """Lines 211-213: ImportError is silently caught (non-Unix)."""
        import sys
        original = sys.modules.get("resource")
        # Force ImportError by setting to None
        sys.modules["resource"] = None  # type: ignore
        try:
            # Should not raise
            PythonExecutor._apply_resource_limits()
        finally:
            if original is not None:
                sys.modules["resource"] = original
            else:
                sys.modules.pop("resource", None)


# ===========================================================================
# 5. tools/registry.py — PythonExecutor.execute exception paths (lines 263-264)
# ===========================================================================


class TestPythonExecutorExceptionPath:
    """Test PythonExecutor.execute generic exception path."""

    def test_execute_generic_exception(self):
        """Lines 263-264: generic exception returns ToolResult with error."""
        executor = PythonExecutor()
        with patch("agents.tools.registry.subprocess.run", side_effect=OSError("no python")):
            result = executor.execute("print('hello')")
        assert result.success is False
        assert "Execution error" in result.error


# ===========================================================================
# 6. tools/registry.py — PytestRunner exception paths (lines 332-363)
# ===========================================================================


class TestPytestRunnerExceptionPaths:
    """Test PytestRunner timeout and exception paths."""

    def test_timeout_expired(self, tmp_path):
        """Lines 356-361: TimeoutExpired returns proper error."""
        from agents.tools.registry import PytestRunner
        import subprocess as sp

        # Create an actual test file so the existence check passes
        test_file = tmp_path / "test_dummy.py"
        test_file.write_text("def test_pass(): pass")

        runner = PytestRunner(allowed_dirs=[tmp_path])
        with patch("agents.tools.registry.subprocess.run",
                   side_effect=sp.TimeoutExpired(cmd="pytest", timeout=60)):
            result = runner.execute(test_file=str(test_file))
        assert result.success is False
        assert "timed out" in result.error

    def test_generic_exception(self, tmp_path):
        """Lines 362-367: generic exception returns error."""
        from agents.tools.registry import PytestRunner

        test_file = tmp_path / "test_dummy.py"
        test_file.write_text("def test_pass(): pass")

        runner = PytestRunner(allowed_dirs=[tmp_path])
        with patch("agents.tools.registry.subprocess.run",
                   side_effect=RuntimeError("pytest not found")):
            result = runner.execute(test_file=str(test_file))
        assert result.success is False
        assert "Error running pytest" in result.error


# ===========================================================================
# 7. tools/registry.py — BanditScanner exception paths (lines 456-470)
# ===========================================================================


class TestBanditScannerExceptionPaths:
    """Test BanditScanner FileNotFoundError and generic exception."""

    def test_file_not_found(self):
        """Lines 463-468: FileNotFoundError when bandit not installed."""
        from agents.tools.registry import BanditScanner

        scanner = BanditScanner(allowed_dirs=[Path("/tmp")])
        with patch("agents.tools.registry.subprocess.run",
                   side_effect=FileNotFoundError("bandit")):
            result = scanner.execute(target="/tmp")
        assert result.success is False
        assert "Bandit not installed" in result.error

    def test_generic_exception(self):
        """Lines 469-474: generic exception in BanditScanner."""
        from agents.tools.registry import BanditScanner

        scanner = BanditScanner(allowed_dirs=[Path("/tmp")])
        with patch("agents.tools.registry.subprocess.run",
                   side_effect=RuntimeError("unexpected")):
            result = scanner.execute(target="/tmp")
        assert result.success is False
        assert "Error running Bandit" in result.error


# ===========================================================================
# 8. tools/registry.py — DevToolWrapper edge cases (lines 868-871)
# ===========================================================================


class TestDevToolWrapperEdgeCases:
    """Test DevToolWrapper with non-dict and non-ToolResult returns."""

    def test_dict_return_with_non_string_output(self):
        """Line 868-869: dict result with non-string output gets json-serialized."""
        inner = MagicMock()
        inner.name = "my_tool"
        inner.description = "desc"
        wrapper = DevToolWrapper(inner, ToolCategory.SPECIALIZED)
        inner.execute.return_value = {
            "success": True,
            "output": {"nested": "data"},
            "error": None,
        }
        result = wrapper.execute()
        assert result.success is True
        assert "nested" in result.output

    def test_non_dict_non_toolresult_return(self):
        """Line 871: plain string return gets wrapped in ToolResult."""
        inner = MagicMock()
        inner.name = "my_tool"
        inner.description = "desc"
        wrapper = DevToolWrapper(inner, ToolCategory.SPECIALIZED)
        inner.execute.return_value = "plain text result"
        result = wrapper.execute()
        assert result.success is True
        assert result.output == "plain text result"

    def test_toolresult_passthrough(self):
        """Line 861-862: ToolResult passthrough."""
        inner = MagicMock()
        inner.name = "my_tool"
        inner.description = "desc"
        wrapper = DevToolWrapper(inner, ToolCategory.SPECIALIZED)
        tr = ToolResult(success=True, output="direct")
        inner.execute.return_value = tr
        result = wrapper.execute()
        assert result is tr


# ===========================================================================
# 9. tools/registry.py — FileReader/FileWriter exception paths (lines 621-622,
#    638-639, 694-695, 725-726)
# ===========================================================================


class TestFileToolExceptionPaths:
    """Test FileReader/FileWriter generic exception paths."""

    def test_file_reader_generic_exception(self):
        """Lines 638-639: generic exception in FileReader."""
        from agents.tools.registry import FileReader
        reader = FileReader(allowed_dirs=[Path("/tmp")])
        # Patch _validate_file_path to pass, then make stat() raise
        with patch("agents.tools.registry._validate_file_path",
                   return_value=(True, None)):
            with patch("agents.tools.registry.Path.stat",
                       side_effect=RuntimeError("bad stat")):
                result = reader.execute(file_path="/tmp/test.txt")
        assert result.success is False
        assert "Error reading file" in result.error

    def test_file_writer_generic_exception(self):
        """Lines 725-726: generic exception in FileWriter."""
        from agents.tools.registry import FileWriter
        writer = FileWriter(allowed_dirs=[Path("/tmp")])
        with patch("agents.tools.registry._validate_file_path",
                   return_value=(True, None)):
            with patch.object(Path, "parent", new_callable=PropertyMock,
                              side_effect=RuntimeError("bad write")):
                result = writer.execute(file_path="/tmp/test.txt", content="hello")
        assert result.success is False
        assert "Error writing file" in result.error


# ===========================================================================
# 10. tools/registry.py — MemoryStoreTool / MemoryRecallTool exception paths
#     (lines 1004-1005, 1100-1101)
# ===========================================================================


class TestMemoryToolsExceptionPaths:
    """Test MemoryStoreTool and MemoryRecallTool exception handling."""

    def test_memory_store_exception(self):
        """Lines 1004-1005: exception in MemoryStoreTool.execute."""
        tool = MemoryStoreTool()
        with patch("agents.tools.registry._get_shared_memory_store",
                   side_effect=RuntimeError("db error")):
            result = tool.execute(content="some fact")
        assert result.success is False
        assert "Failed to store memory" in result.error

    def test_memory_recall_exception(self):
        """Lines 1100-1101: exception in MemoryRecallTool.execute."""
        tool = MemoryRecallTool()
        with patch("agents.tools.registry._get_shared_memory_store",
                   side_effect=RuntimeError("db error")):
            result = tool.execute(query="search term")
        assert result.success is False
        assert "Memory recall failed" in result.error


# ===========================================================================
# 11. tools/registry.py — WebFetchTool exception paths (lines 938-940)
# ===========================================================================


class TestWebFetchToolExceptionPaths:
    """Test WebFetchTool generic exception path."""

    def test_generic_exception(self):
        """Lines 939-940: generic exception in WebFetchTool.execute."""
        tool = WebFetchTool()
        with patch("agents.tools.registry.subprocess.run",
                   side_effect=RuntimeError("no network")):
            result = tool.execute(url="http://example.com")
        assert result.success is False
        assert "no network" in result.error


# ===========================================================================
# 12. tools/registry.py — ShellExecutor exception paths (lines 1180-1181)
# ===========================================================================


class TestShellExecutorExceptionPaths:
    """Test ShellExecutor generic exception path."""

    def test_generic_exception(self):
        """Lines 1180-1181: generic exception in ShellExecutor.execute."""
        tool = ShellExecutor()
        with patch("agents.tools.registry.subprocess.run",
                   side_effect=RuntimeError("no shell")):
            result = tool.execute(command="echo hello")
        assert result.success is False
        assert "no shell" in result.error


# ===========================================================================
# 13. tools/registry.py — _build_allowed_file_dirs paths (lines 550-551)
# ===========================================================================


class TestBuildAllowedFileDirs:
    """Test _build_allowed_file_dirs with various inputs."""

    def test_env_var_override(self):
        """VIBE_ALLOWED_FILE_DIRS env var overrides defaults."""
        with patch.dict(os.environ, {"VIBE_ALLOWED_FILE_DIRS": "/foo:/bar"}):
            dirs = _build_allowed_file_dirs()
        assert Path("/foo").resolve() in dirs
        assert Path("/bar").resolve() in dirs

    def test_configured_dirs_take_priority(self):
        """configured_dirs parameter takes priority."""
        dirs = _build_allowed_file_dirs(["/custom1", "/custom2"])
        assert Path("/custom1").resolve() in dirs
        assert Path("/custom2").resolve() in dirs
        assert len(dirs) == 2

    def test_defaults_when_nothing_configured(self):
        """Falls back to _DEFAULT_ALLOWED_FILE_DIRS."""
        env_clean = {k: v for k, v in os.environ.items()
                     if k != "VIBE_ALLOWED_FILE_DIRS"}
        with patch.dict(os.environ, env_clean, clear=True):
            dirs = _build_allowed_file_dirs()
        assert dirs == list(_DEFAULT_ALLOWED_FILE_DIRS)


# ===========================================================================
# 14. api_key_manager.py — _prompt_user_for_key, _prompt_via_mattermost,
#     _prompt_via_slack (lines 39-44, 140-142, 180, 228-368, 384-522)
# ===========================================================================


@pytest.fixture
def manager(tmp_path):
    """APIKeyManager with storage in a temp directory."""
    from agents.api_key_manager import APIKeyManager
    mgr = APIKeyManager.__new__(APIKeyManager)
    mgr.config = None
    mgr.cache = {}
    mgr.storage_path = tmp_path / "api_keys.json"
    return mgr


@pytest.fixture
def manager_with_mattermost_config(tmp_path):
    """APIKeyManager with mattermost config enabled."""
    from agents.api_key_manager import APIKeyManager
    mgr = APIKeyManager.__new__(APIKeyManager)
    mgr.cache = {}
    mgr.storage_path = tmp_path / "api_keys.json"

    # Mock config with mattermost enabled
    config = MagicMock()
    config.mattermost.enabled = True
    config.mattermost.bot_enabled = True
    config.mattermost.bot_token = "fake-token"
    config.mattermost.mattermost_url = "http://mm.example.com"
    mgr.config = config
    return mgr


class TestPromptUserForKey:
    """Test _prompt_user_for_key routing logic."""

    def test_mattermost_route_when_configured(self, manager_with_mattermost_config):
        """Lines 197-199: Mattermost is tried first when configured."""
        mgr = manager_with_mattermost_config
        with patch.object(mgr, "_prompt_via_mattermost", return_value="fake-key") as mock_mm:
            result = mgr._prompt_user_for_key("MY_KEY")
        mock_mm.assert_called_once_with("MY_KEY")
        assert result == "fake-key"

    def test_slack_route_when_configured(self, manager):
        """Lines 202-204: Slack is tried when SLACK env vars are set."""
        with patch.dict(os.environ, {
            "SLACK_BOT_TOKEN": "xoxb-fake",
            "SLACK_USER_ID": "U12345",
        }):
            with patch.object(manager, "_prompt_via_slack", return_value="slack-key") as mock_slack:
                result = manager._prompt_user_for_key("MY_KEY")
        mock_slack.assert_called_once_with("MY_KEY")
        assert result == "slack-key"

    def test_exception_returns_none(self, manager_with_mattermost_config):
        """Lines 210-212: exception in _prompt_user_for_key returns None."""
        mgr = manager_with_mattermost_config
        with patch.object(mgr, "_prompt_via_mattermost", side_effect=RuntimeError("boom")):
            result = mgr._prompt_user_for_key("MY_KEY")
        assert result is None

    def test_get_api_key_calls_prompt_and_returns(self, manager):
        """Line 180: prompted_key is returned from get_api_key."""
        with patch.object(manager, "_prompt_user_for_key", return_value="prompted-key-12345"):
            key = manager.get_api_key("MISSING_KEY", prompt_user=True)
        assert key == "prompted-key-12345"


class TestPromptViaMattermost:
    """Test _prompt_via_mattermost flow with mocked MattermostClient."""

    def test_successful_key_retrieval(self, manager_with_mattermost_config):
        """Lines 228-344: happy path — user sends valid key, it's saved and returned."""
        mgr = manager_with_mattermost_config
        mock_client = MagicMock()
        mock_client.get_user_by_username.return_value = {"id": "user123"}
        mock_client.send_direct_message.return_value = "post_id_1"
        mock_client.get_direct_channel_id.return_value = "channel_1"
        mock_client._get_bot_user_id.return_value = "bot_user"
        mock_client.delete_message.return_value = True

        # User's response message
        mock_client.get_recent_messages.return_value = [
            {"user_id": "user123", "id": "msg_1",
             "message": "a" * 50}
        ]

        with patch("agents.messenger_client.MattermostClient", return_value=mock_client):
            with patch.dict(os.environ, {"MATTERMOST_USERNAME": "testuser"}):
                result = mgr._prompt_via_mattermost("GENERIC_API_KEY")

        assert result == "a" * 50
        assert mgr.cache["GENERIC_API_KEY"] == "a" * 50

    def test_missing_bot_token(self, manager_with_mattermost_config):
        """Lines 234-236: missing bot_token returns None."""
        mgr = manager_with_mattermost_config
        mgr.config.mattermost.bot_token = ""

        with patch("agents.messenger_client.MattermostClient"):
            with patch.dict(os.environ, {"MATTERMOST_USERNAME": "testuser"}):
                result = mgr._prompt_via_mattermost("MY_KEY")
        assert result is None

    def test_missing_username(self, manager_with_mattermost_config):
        """Lines 240-242: missing username returns None."""
        mgr = manager_with_mattermost_config
        mock_client = MagicMock()

        env_clean = {k: v for k, v in os.environ.items()
                     if k not in ("MATTERMOST_USERNAME", "USER")}
        with patch("agents.messenger_client.MattermostClient", return_value=mock_client):
            with patch.dict(os.environ, env_clean, clear=True):
                result = mgr._prompt_via_mattermost("MY_KEY")
        assert result is None

    def test_user_not_found(self, manager_with_mattermost_config):
        """Lines 252-254: user not found in Mattermost returns None."""
        mgr = manager_with_mattermost_config
        mock_client = MagicMock()
        mock_client.get_user_by_username.return_value = None

        with patch("agents.messenger_client.MattermostClient", return_value=mock_client):
            with patch.dict(os.environ, {"MATTERMOST_USERNAME": "testuser"}):
                result = mgr._prompt_via_mattermost("MY_KEY")
        assert result is None

    def test_invalid_key_format_retries(self, manager_with_mattermost_config):
        """Lines 320-326: invalid key format sends error, continues polling."""
        mgr = manager_with_mattermost_config
        mock_client = MagicMock()
        mock_client.get_user_by_username.return_value = {"id": "user123"}
        mock_client.send_direct_message.return_value = "post_id_1"
        mock_client.get_direct_channel_id.return_value = "channel_1"
        mock_client._get_bot_user_id.return_value = "bot_user"
        mock_client.delete_message.return_value = True

        # First call: returns invalid key (too short), second call: timeout (empty)
        mock_client.get_recent_messages.side_effect = [
            [{"user_id": "user123", "id": "msg_1", "message": "short"}],
            [],  # empty — loop will time out
        ]

        with patch("agents.messenger_client.MattermostClient", return_value=mock_client):
            with patch.dict(os.environ, {"MATTERMOST_USERNAME": "testuser"}):
                # Use short timeout to avoid long test
                with patch("agents.api_key_manager.DEFAULT_PROMPT_TIMEOUT_MINUTES", 0):
                    result = mgr._prompt_via_mattermost("MY_KEY")

        # Should have sent invalid-format feedback
        assert result is None

    def test_deletion_failure_sends_warning(self, manager_with_mattermost_config):
        """Lines 308-317: failed message deletion sends security warning."""
        mgr = manager_with_mattermost_config
        mock_client = MagicMock()
        mock_client.get_user_by_username.return_value = {"id": "user123"}
        mock_client.send_direct_message.return_value = "post_id_1"
        mock_client.get_direct_channel_id.return_value = "channel_1"
        mock_client._get_bot_user_id.return_value = "bot_user"
        mock_client.delete_message.return_value = False  # Deletion fails

        mock_client.get_recent_messages.return_value = [
            {"user_id": "user123", "id": "msg_1",
             "message": "a" * 50}
        ]

        with patch("agents.messenger_client.MattermostClient", return_value=mock_client):
            with patch.dict(os.environ, {"MATTERMOST_USERNAME": "testuser"}):
                result = mgr._prompt_via_mattermost("GENERIC_API_KEY")

        assert result == "a" * 50
        # Should have sent warning about failed deletion
        warning_calls = [
            c for c in mock_client.send_direct_message.call_args_list
            if "SECURITY WARNING" in str(c)
        ]
        assert len(warning_calls) >= 1

    def test_save_key_failure(self, manager_with_mattermost_config):
        """Lines 346-352: save_key failure returns None and notifies user."""
        mgr = manager_with_mattermost_config
        mock_client = MagicMock()
        mock_client.get_user_by_username.return_value = {"id": "user123"}
        mock_client.send_direct_message.return_value = "post_id_1"
        mock_client.get_direct_channel_id.return_value = "channel_1"
        mock_client._get_bot_user_id.return_value = "bot_user"
        mock_client.delete_message.return_value = True

        mock_client.get_recent_messages.return_value = [
            {"user_id": "user123", "id": "msg_1",
             "message": "a" * 50}
        ]

        with patch("agents.messenger_client.MattermostClient", return_value=mock_client):
            with patch.dict(os.environ, {"MATTERMOST_USERNAME": "testuser"}):
                with patch.object(mgr, "_save_key", side_effect=RuntimeError("disk full")):
                    result = mgr._prompt_via_mattermost("GENERIC_API_KEY")

        assert result is None

    def test_outer_exception_returns_none(self, manager_with_mattermost_config):
        """Lines 366-368: outer exception in _prompt_via_mattermost returns None."""
        mgr = manager_with_mattermost_config
        with patch("agents.messenger_client.MattermostClient",
                   side_effect=ImportError("no module")):
            with patch.dict(os.environ, {"MATTERMOST_USERNAME": "testuser"}):
                result = mgr._prompt_via_mattermost("MY_KEY")
        assert result is None

    def test_timeout_sends_message(self, manager_with_mattermost_config):
        """Lines 357-364: timeout sends message to user."""
        mgr = manager_with_mattermost_config
        mock_client = MagicMock()
        mock_client.get_user_by_username.return_value = {"id": "user123"}
        mock_client.send_direct_message.return_value = "post_id_1"
        mock_client.get_direct_channel_id.return_value = "channel_1"
        mock_client._get_bot_user_id.return_value = "bot_user"
        mock_client.get_recent_messages.return_value = []  # no response

        with patch("agents.messenger_client.MattermostClient", return_value=mock_client):
            with patch.dict(os.environ, {"MATTERMOST_USERNAME": "testuser"}):
                with patch("agents.api_key_manager.DEFAULT_PROMPT_TIMEOUT_MINUTES", 0):
                    result = mgr._prompt_via_mattermost("MY_KEY")

        assert result is None
        # Should have sent timeout message
        timeout_calls = [
            c for c in mock_client.send_direct_message.call_args_list
            if "timed out" in str(c)
        ]
        assert len(timeout_calls) >= 1


class TestPromptViaSlack:
    """Test _prompt_via_slack flow with mocked SlackClient."""

    @pytest.fixture
    def manager_for_slack(self, tmp_path):
        """APIKeyManager set up for Slack testing."""
        from agents.api_key_manager import APIKeyManager
        mgr = APIKeyManager.__new__(APIKeyManager)
        mgr.config = None  # No mattermost config
        mgr.cache = {}
        mgr.storage_path = tmp_path / "api_keys.json"
        return mgr

    def test_successful_key_retrieval(self, manager_for_slack):
        """Lines 384-498: happy path — Slack user sends valid key."""
        mgr = manager_for_slack
        mock_client = MagicMock()
        mock_client.send_direct_message.return_value = ("ts_1", "channel_1")
        mock_client.delete_message.return_value = True
        mock_client.get_conversation_history.return_value = [
            {"text": "a" * 50, "ts": "ts_2"}
        ]

        with patch("agents.messenger_client.SlackClient", return_value=mock_client):
            with patch.dict(os.environ, {
                "SLACK_BOT_TOKEN": "xoxb-fake",
                "SLACK_USER_ID": "U12345",
            }):
                result = mgr._prompt_via_slack("GENERIC_API_KEY")

        assert result == "a" * 50
        assert mgr.cache["GENERIC_API_KEY"] == "a" * 50

    def test_missing_bot_token(self, manager_for_slack):
        """Lines 391-393: missing SLACK_BOT_TOKEN returns None."""
        mgr = manager_for_slack
        env_clean = {k: v for k, v in os.environ.items()
                     if k != "SLACK_BOT_TOKEN"}
        with patch.dict(os.environ, env_clean, clear=True):
            result = mgr._prompt_via_slack("MY_KEY")
        assert result is None

    def test_missing_user_id(self, manager_for_slack):
        """Lines 397-399: missing SLACK_USER_ID returns None."""
        mgr = manager_for_slack
        env = {k: v for k, v in os.environ.items()
               if k != "SLACK_USER_ID"}
        env["SLACK_BOT_TOKEN"] = "xoxb-fake"
        with patch.dict(os.environ, env, clear=True):
            result = mgr._prompt_via_slack("MY_KEY")
        assert result is None

    def test_send_dm_failure(self, manager_for_slack):
        """Lines 429-431: failed send_direct_message returns None."""
        mgr = manager_for_slack
        mock_client = MagicMock()
        mock_client.send_direct_message.return_value = None

        with patch("agents.messenger_client.SlackClient", return_value=mock_client):
            with patch.dict(os.environ, {
                "SLACK_BOT_TOKEN": "xoxb-fake",
                "SLACK_USER_ID": "U12345",
            }):
                result = mgr._prompt_via_slack("MY_KEY")
        assert result is None

    def test_deletion_failure_sends_warning(self, manager_for_slack):
        """Lines 462-469: failed Slack message deletion sends warning."""
        mgr = manager_for_slack
        mock_client = MagicMock()
        mock_client.send_direct_message.return_value = ("ts_1", "channel_1")
        mock_client.delete_message.return_value = False
        mock_client.get_conversation_history.return_value = [
            {"text": "a" * 50, "ts": "ts_2"}
        ]

        with patch("agents.messenger_client.SlackClient", return_value=mock_client):
            with patch.dict(os.environ, {
                "SLACK_BOT_TOKEN": "xoxb-fake",
                "SLACK_USER_ID": "U12345",
            }):
                result = mgr._prompt_via_slack("GENERIC_API_KEY")

        assert result == "a" * 50
        warning_calls = [
            c for c in mock_client.send_direct_message.call_args_list
            if "SECURITY WARNING" in str(c)
        ]
        assert len(warning_calls) >= 1

    def test_invalid_key_continues_polling(self, manager_for_slack):
        """Lines 472-478: invalid key sends error, continues polling."""
        mgr = manager_for_slack
        mock_client = MagicMock()
        mock_client.send_direct_message.return_value = ("ts_1", "channel_1")
        mock_client.delete_message.return_value = True

        # First: invalid key (too short), then empty (timeout)
        mock_client.get_conversation_history.side_effect = [
            [{"text": "short", "ts": "ts_2"}],
            [],
        ]

        with patch("agents.messenger_client.SlackClient", return_value=mock_client):
            with patch.dict(os.environ, {
                "SLACK_BOT_TOKEN": "xoxb-fake",
                "SLACK_USER_ID": "U12345",
            }):
                with patch("agents.api_key_manager.DEFAULT_PROMPT_TIMEOUT_MINUTES", 0):
                    result = mgr._prompt_via_slack("MY_KEY")

        assert result is None

    def test_save_key_failure(self, manager_for_slack):
        """Lines 500-506: save_key failure returns None."""
        mgr = manager_for_slack
        mock_client = MagicMock()
        mock_client.send_direct_message.return_value = ("ts_1", "channel_1")
        mock_client.delete_message.return_value = True
        mock_client.get_conversation_history.return_value = [
            {"text": "a" * 50, "ts": "ts_2"}
        ]

        with patch("agents.messenger_client.SlackClient", return_value=mock_client):
            with patch.dict(os.environ, {
                "SLACK_BOT_TOKEN": "xoxb-fake",
                "SLACK_USER_ID": "U12345",
            }):
                with patch.object(mgr, "_save_key", side_effect=RuntimeError("disk full")):
                    result = mgr._prompt_via_slack("GENERIC_API_KEY")

        assert result is None

    def test_timeout_sends_message(self, manager_for_slack):
        """Lines 511-518: timeout sends message to user."""
        mgr = manager_for_slack
        mock_client = MagicMock()
        mock_client.send_direct_message.return_value = ("ts_1", "channel_1")
        mock_client.get_conversation_history.return_value = []

        with patch("agents.messenger_client.SlackClient", return_value=mock_client):
            with patch.dict(os.environ, {
                "SLACK_BOT_TOKEN": "xoxb-fake",
                "SLACK_USER_ID": "U12345",
            }):
                with patch("agents.api_key_manager.DEFAULT_PROMPT_TIMEOUT_MINUTES", 0):
                    result = mgr._prompt_via_slack("MY_KEY")

        assert result is None
        timeout_calls = [
            c for c in mock_client.send_direct_message.call_args_list
            if "timed out" in str(c)
        ]
        assert len(timeout_calls) >= 1

    def test_outer_exception_returns_none(self, manager_for_slack):
        """Lines 520-522: outer exception in _prompt_via_slack returns None."""
        mgr = manager_for_slack
        with patch("agents.messenger_client.SlackClient",
                   side_effect=ImportError("no module")):
            with patch.dict(os.environ, {
                "SLACK_BOT_TOKEN": "xoxb-fake",
                "SLACK_USER_ID": "U12345",
            }):
                result = mgr._prompt_via_slack("MY_KEY")
        assert result is None

    def test_bot_message_skipped(self, manager_for_slack):
        """Lines 443-444: bot messages in Slack are skipped."""
        mgr = manager_for_slack
        mock_client = MagicMock()
        mock_client.send_direct_message.return_value = ("ts_1", "channel_1")
        mock_client.delete_message.return_value = True
        mock_client.get_conversation_history.side_effect = [
            # First poll: bot message, then user message
            [
                {"text": "bot msg", "bot_id": "B123", "ts": "ts_bot"},
                {"text": "a" * 50, "ts": "ts_user"},
            ],
        ]

        with patch("agents.messenger_client.SlackClient", return_value=mock_client):
            with patch.dict(os.environ, {
                "SLACK_BOT_TOKEN": "xoxb-fake",
                "SLACK_USER_ID": "U12345",
            }):
                result = mgr._prompt_via_slack("GENERIC_API_KEY")

        assert result == "a" * 50

    def test_prompt_ts_skipped(self, manager_for_slack):
        """Lines 447-448: prompt message itself is skipped."""
        mgr = manager_for_slack
        mock_client = MagicMock()
        mock_client.send_direct_message.return_value = ("ts_prompt", "channel_1")
        mock_client.delete_message.return_value = True
        mock_client.get_conversation_history.side_effect = [
            [
                {"text": "the prompt", "ts": "ts_prompt"},  # Should be skipped
                {"text": "a" * 50, "ts": "ts_user"},  # Actual response
            ],
        ]

        with patch("agents.messenger_client.SlackClient", return_value=mock_client):
            with patch.dict(os.environ, {
                "SLACK_BOT_TOKEN": "xoxb-fake",
                "SLACK_USER_ID": "U12345",
            }):
                result = mgr._prompt_via_slack("GENERIC_API_KEY")

        assert result == "a" * 50

    def test_prompt_deletion_failure_logged(self, manager_for_slack):
        """Lines 492-496: prompt deletion failure is logged but key is still returned."""
        mgr = manager_for_slack
        mock_client = MagicMock()
        mock_client.send_direct_message.return_value = ("ts_1", "channel_1")
        # First delete succeeds (key msg), second fails (prompt msg)
        mock_client.delete_message.side_effect = [True, False]
        mock_client.get_conversation_history.return_value = [
            {"text": "a" * 50, "ts": "ts_2"}
        ]

        with patch("agents.messenger_client.SlackClient", return_value=mock_client):
            with patch.dict(os.environ, {
                "SLACK_BOT_TOKEN": "xoxb-fake",
                "SLACK_USER_ID": "U12345",
            }):
                result = mgr._prompt_via_slack("GENERIC_API_KEY")

        assert result == "a" * 50


# ===========================================================================
# 15. api_key_manager.py — _load_stored_keys failure (lines 39-44)
# ===========================================================================


class TestLoadStoredKeysFailure:
    """Test _load_stored_keys with corrupted file."""

    def test_corrupted_json(self, tmp_path):
        """Lines 39-44: corrupted JSON in stored keys file."""
        from agents.api_key_manager import APIKeyManager
        storage = tmp_path / "api_keys.json"
        storage.write_text("not valid json{{{")

        mgr = APIKeyManager.__new__(APIKeyManager)
        mgr.config = None
        mgr.cache = {}
        mgr.storage_path = storage
        mgr._load_stored_keys()  # Should not raise
        assert mgr.cache == {}


# ===========================================================================
# 16. api_key_manager.py — _save_key failure propagation (lines 140-142)
# ===========================================================================


class TestSaveKeyFailure:
    """Test _save_key raises on write failure."""

    def test_save_key_raises_on_failure(self, tmp_path):
        """Lines 140-142: _save_key raises if write fails."""
        from unittest.mock import patch as _patch
        from agents.api_key_manager import APIKeyManager
        mgr = APIKeyManager.__new__(APIKeyManager)
        mgr.config = None
        mgr.cache = {}
        mgr.storage_path = tmp_path / "api_keys.json"
        # Force the file write to raise — works regardless of user/root
        with _patch("builtins.open", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                mgr._save_key("KEY", "value")


# ===========================================================================
# 17. api_key_manager.py — get_error_message with mattermost (line 540)
# ===========================================================================


class TestGetErrorMessageWithMessenger:
    """Test get_error_message when mattermost is enabled."""

    def test_error_message_includes_messenger_hint(self, tmp_path):
        """Line 540: error message includes messenger hint."""
        from agents.api_key_manager import APIKeyManager
        mgr = APIKeyManager.__new__(APIKeyManager)
        mgr.cache = {}
        mgr.storage_path = tmp_path / "api_keys.json"

        config = MagicMock()
        config.mattermost.enabled = True
        mgr.config = config

        msg = mgr.get_error_message("MY_KEY")
        assert "messenger" in msg.lower() or "Interactive" in msg


# ===========================================================================
# 18. api_key_manager.py — constructor __init__ (lines 39-44)
# ===========================================================================


class TestAPIKeyManagerInit:
    """Test APIKeyManager __init__ with proper constructor."""

    def test_init_loads_stored_keys(self, tmp_path):
        """Lines 39-44: __init__ calls _load_stored_keys."""
        from agents.api_key_manager import APIKeyManager
        storage = tmp_path / ".vibe" / "api_keys.json"
        storage.parent.mkdir(parents=True)
        storage.write_text(json.dumps({"STORED_KEY": "stored-value-12345"}))

        with patch.object(APIKeyManager, "__init__", lambda self, config=None: None):
            mgr = APIKeyManager.__new__(APIKeyManager)
        mgr.config = None
        mgr.cache = {}
        mgr.storage_path = storage
        mgr._load_stored_keys()
        assert mgr.cache["STORED_KEY"] == "stored-value-12345"


# ===========================================================================
# 19. tools/registry.py — create_default_tool_registry (lines 1258-1341)
# ===========================================================================


class TestCreateDefaultToolRegistry:
    """Test create_default_tool_registry with mocked sandbox imports."""

    def test_basic_creation_with_mocked_sandbox(self):
        """Lines 1258-1341: create default registry with mocked sandbox tools."""
        mock_pool = MagicMock()

        # Mock sandbox tool constructors
        mock_sandbox_tools = MagicMock()
        for name in ["SandboxedPythonExecutor", "SandboxedPytestRunner",
                     "SandboxedBanditScanner", "SandboxedShellExecutor",
                     "SandboxedWebFetchTool"]:
            mock_tool = MagicMock()
            mock_tool.name = name.lower().replace("sandboxed", "").strip("_")
            # Map to expected tool names
            name_map = {
                "SandboxedPythonExecutor": "python_executor",
                "SandboxedPytestRunner": "pytest_runner",
                "SandboxedBanditScanner": "bandit",
                "SandboxedShellExecutor": "shell_executor",
                "SandboxedWebFetchTool": "web_fetch",
            }
            mock_tool.name = name_map.get(name, name.lower())
            mock_tool.enabled = True
            mock_tool.get_schema.return_value = {"name": mock_tool.name, "description": "mock"}
            setattr(mock_sandbox_tools, name, MagicMock(return_value=mock_tool))

        env_clean = {
            k: v for k, v in os.environ.items()
            if k not in ("SEARXNG_URL", "PLAYWRIGHT_WS_URL",
                         "PENPOT_API_URL", "COMFYUI_URL", "GITEA_URL",
                         "MINIO_URL", "BULLETIN_PATH")
        }

        with patch.dict(os.environ, env_clean, clear=True):
            with patch.dict("sys.modules", {
                "agents.sandbox.tools": mock_sandbox_tools,
            }):
                from agents.tools.registry import create_default_tool_registry
                registry = create_default_tool_registry(
                    sandbox_pool=mock_pool,
                    network_egress=False,
                )

        tool_names = registry.list_tools()
        # Memory tools should be present
        assert "memory_store" in tool_names
        assert "memory_recall" in tool_names
        # File tools should be present
        assert "file_reader" in tool_names
        assert "file_writer" in tool_names

    def test_with_network_egress(self):
        """Network egress enables web_fetch through sandbox."""
        mock_pool = MagicMock()

        mock_sandbox_tools = MagicMock()
        for cls_name, tool_name in [
            ("SandboxedPythonExecutor", "python_executor"),
            ("SandboxedPytestRunner", "pytest_runner"),
            ("SandboxedBanditScanner", "bandit"),
            ("SandboxedShellExecutor", "shell_executor"),
            ("SandboxedWebFetchTool", "web_fetch"),
        ]:
            mock_tool = MagicMock()
            mock_tool.name = tool_name
            mock_tool.enabled = True
            mock_tool.get_schema.return_value = {"name": tool_name, "description": "mock"}
            setattr(mock_sandbox_tools, cls_name, MagicMock(return_value=mock_tool))

        env_clean = {
            k: v for k, v in os.environ.items()
            if k not in ("SEARXNG_URL", "PLAYWRIGHT_WS_URL",
                         "PENPOT_API_URL", "COMFYUI_URL", "GITEA_URL",
                         "MINIO_URL", "BULLETIN_PATH")
        }

        with patch.dict(os.environ, env_clean, clear=True):
            with patch.dict("sys.modules", {
                "agents.sandbox.tools": mock_sandbox_tools,
            }):
                from agents.tools.registry import create_default_tool_registry
                registry = create_default_tool_registry(
                    sandbox_pool=mock_pool,
                    network_egress=True,
                )

        assert "web_fetch" in registry.list_tools()


# ===========================================================================
# 20. parse_clarification edge cases
# ===========================================================================


class TestParseClarification:
    """Test parse_clarification edge cases."""

    def test_empty_tag(self):
        """Empty clarification tag returns (False, [])."""
        ok, qs = parse_clarification("<clarification_needed>\n</clarification_needed>")
        assert ok is False
        assert qs == []

    def test_bullets_with_dashes(self):
        """Bullet points with dashes are parsed."""
        text = """<clarification_needed>
- What language?
- What framework?
</clarification_needed>"""
        ok, qs = parse_clarification(text)
        assert ok is True
        assert len(qs) == 2

    def test_no_tag(self):
        """No tag returns (False, [])."""
        ok, qs = parse_clarification("Just a normal response.")
        assert ok is False
        assert qs == []
