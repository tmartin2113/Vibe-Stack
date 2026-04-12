"""Tests for agent instruction loading and prompt injection."""

import os
import pytest
from unittest.mock import MagicMock, patch

from agents.state import InputState
from agents.heartbeat import _load_agent_instructions


class TestStateSchema:

    def test_input_state_accepts_agent_instructions(self):
        state: InputState = {
            "user_request": "test",
            "session_id": "s1",
            "agent_instructions": "# CTO\nYou are the CTO.",
        }
        assert state["agent_instructions"] == "# CTO\nYou are the CTO."

    def test_input_state_agent_instructions_optional(self):
        state: InputState = {
            "user_request": "test",
            "session_id": "s1",
        }
        assert state.get("agent_instructions", "") == ""


class TestLoadAgentInstructions:

    def test_loads_file_content(self, tmp_path):
        f = tmp_path / "AGENTS.md"
        f.write_text("# CTO\nYou are the CTO.")
        assert _load_agent_instructions(str(f)) == "# CTO\nYou are the CTO."

    def test_returns_empty_for_missing_file(self):
        assert _load_agent_instructions("/nonexistent/path/AGENTS.md") == ""

    def test_returns_empty_for_empty_path(self):
        assert _load_agent_instructions("") == ""

    def test_returns_empty_for_none(self):
        assert _load_agent_instructions(None) == ""

    def test_strips_whitespace(self, tmp_path):
        f = tmp_path / "AGENTS.md"
        f.write_text("\n\n# CTO\n\n")
        assert _load_agent_instructions(str(f)) == "# CTO"


class TestSpecialistInjection:

    def _make_agent_nodes(self):
        from agents.nodes import AgentNodes
        adapter_registry = MagicMock()
        tool_registry = MagicMock()
        tool_registry.get_all_schemas.return_value = []
        tool_registry.parse_tool_call.return_value = None  # no tool calls in output
        return AgentNodes(adapter_registry=adapter_registry, tool_registry=tool_registry)

    def test_instructions_appended_to_prompt(self):
        nodes = self._make_agent_nodes()
        mock_adapter = MagicMock()
        mock_adapter.generate.return_value = "result"
        nodes.adapters.get_or_create.return_value = mock_adapter

        state = {
            "user_request": "Fix the login bug",
            "routed_task_type": "debugging",
            "routing_confidence": 0.9,
            "loaded_skills": [],
            "specialist_adapter": "debugging_assistant",
            "specialist_iteration_count": 0,
            "agent_instructions": "# Sr. Backend Engineer\nYou own server-side code.",
        }
        nodes.execute_with_specialist(state)

        prompt = mock_adapter.generate.call_args[0][0]
        assert "## Your Role" in prompt
        assert "You own server-side code." in prompt

    def test_no_instructions_no_injection(self):
        nodes = self._make_agent_nodes()
        mock_adapter = MagicMock()
        mock_adapter.generate.return_value = "result"
        nodes.adapters.get_or_create.return_value = mock_adapter

        state = {
            "user_request": "Fix the login bug",
            "routed_task_type": "debugging",
            "routing_confidence": 0.9,
            "loaded_skills": [],
            "specialist_adapter": "debugging_assistant",
            "specialist_iteration_count": 0,
        }
        nodes.execute_with_specialist(state)

        prompt = mock_adapter.generate.call_args[0][0]
        assert "## Your Role" not in prompt
