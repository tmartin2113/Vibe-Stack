"""Tests for MiroFishSimulation tool."""

import os
from unittest.mock import MagicMock, patch

import pytest

from agents.tools.mirofish_tool import MiroFishSimulationTool


class TestComplexityRouting:
    """Test the complexity-based LLM routing logic."""

    def test_simple_simulation_uses_local(self):
        tool = MiroFishSimulationTool()
        config = tool._select_llm_config(agent_count=10, iterations=5)
        assert config["base_url"] == os.getenv(
            "MIROFISH_LLM_API_URL", "http://host.docker.internal:8000/v1"
        )

    def test_high_agent_count_uses_cloud(self):
        with patch.dict(os.environ, {
            "MIROFISH_LLM_CLOUD_API_URL": "https://api.anthropic.com",
            "MIROFISH_LLM_CLOUD_API_KEY": "sk-test",
            "MIROFISH_LLM_CLOUD_MODEL": "claude-sonnet-4-6",
        }):
            tool = MiroFishSimulationTool()
            config = tool._select_llm_config(agent_count=50, iterations=5)
            assert config["base_url"] == "https://api.anthropic.com"
            assert config["model"] == "claude-sonnet-4-6"

    def test_high_iteration_count_uses_cloud(self):
        with patch.dict(os.environ, {
            "MIROFISH_LLM_CLOUD_API_URL": "https://api.openai.com/v1",
            "MIROFISH_LLM_CLOUD_API_KEY": "sk-test",
            "MIROFISH_LLM_CLOUD_MODEL": "gpt-4o",
        }):
            tool = MiroFishSimulationTool()
            config = tool._select_llm_config(agent_count=10, iterations=25)
            assert config["base_url"] == "https://api.openai.com/v1"

    def test_cloud_not_configured_falls_back_to_local(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MIROFISH_LLM_CLOUD_API_URL", None)
            os.environ.pop("MIROFISH_LLM_CLOUD_API_KEY", None)
            tool = MiroFishSimulationTool()
            config = tool._select_llm_config(agent_count=50, iterations=30)
            # Falls back to local even though complex
            assert "host.docker.internal" in config["base_url"] or "localhost" in config["base_url"]
            assert config.get("fallback") is True

    def test_custom_thresholds(self):
        with patch.dict(os.environ, {
            "MIROFISH_COMPLEXITY_AGENT_THRESHOLD": "20",
            "MIROFISH_COMPLEXITY_ITER_THRESHOLD": "10",
            "MIROFISH_LLM_CLOUD_API_URL": "https://api.test.com",
            "MIROFISH_LLM_CLOUD_API_KEY": "sk-test",
            "MIROFISH_LLM_CLOUD_MODEL": "test-model",
        }):
            tool = MiroFishSimulationTool()
            # 25 agents > 20 threshold
            config = tool._select_llm_config(agent_count=25, iterations=5)
            assert config["base_url"] == "https://api.test.com"


class TestToolExecution:
    """Test the tool's execute method."""

    @patch("agents.tools.mirofish_tool.requests.post")
    def test_successful_simulation(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "report": "Simulation complete. No conflicts detected.",
            "agents": 20,
            "iterations": 10,
        }
        mock_post.return_value = mock_response

        tool = MiroFishSimulationTool()
        result = tool.execute(
            seed_material="Build a REST API with user authentication",
            agent_count=20,
            iterations=10,
        )
        assert result.success is True
        assert "No conflicts detected" in result.output

    @patch("agents.tools.mirofish_tool.requests.post")
    def test_simulation_failure(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal server error"
        mock_post.return_value = mock_response

        tool = MiroFishSimulationTool()
        result = tool.execute(
            seed_material="Test scenario",
            agent_count=10,
            iterations=5,
        )
        assert result.success is False
        assert result.error is not None

    @patch("agents.tools.mirofish_tool.requests.post")
    def test_connection_error(self, mock_post):
        mock_post.side_effect = Exception("Connection refused")

        tool = MiroFishSimulationTool()
        result = tool.execute(
            seed_material="Test scenario",
            agent_count=10,
            iterations=5,
        )
        assert result.success is False
        assert "Connection refused" in result.error

    def test_parameter_schema(self):
        tool = MiroFishSimulationTool()
        schema = tool._get_parameters_schema()
        assert "seed_material" in schema["properties"]
        assert "agent_count" in schema["properties"]
        assert "iterations" in schema["properties"]
        assert "question" in schema["properties"]
        assert schema["required"] == ["seed_material"]


class TestToolMetadata:
    """Test tool registration metadata."""

    def test_tool_name(self):
        tool = MiroFishSimulationTool()
        assert tool.name == "MiroFishSimulation"

    def test_tool_category(self):
        from agents.tools.base import ToolCategory
        tool = MiroFishSimulationTool()
        assert tool.category == ToolCategory.EXTERNAL_SERVICE
