"""Tests for agent registry — resolves agent IDs from Paperclip API."""

import pytest
from unittest.mock import MagicMock
from agents.paperclip_client import AgentInfo, PaperclipAPIError
from agents.agent_registry import AgentRegistry


def _agent(role: str, agent_id: str, status: str = "active") -> AgentInfo:
    return AgentInfo(
        id=agent_id,
        company_id="company-1",
        name=role.replace("-", " ").title(),
        role=role,
        title=role,
        status=status,
    )


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.company_id = "company-1"
    return client


class TestAgentRegistry:

    def test_resolve_all_builds_role_map(self, mock_client):
        mock_client.list_agents.return_value = [
            _agent("cto", "uuid-1"),
            _agent("backend-engineer", "uuid-2"),
            _agent("frontend-engineer", "uuid-3"),
        ]
        registry = AgentRegistry(mock_client)
        result = registry.resolve_all()
        assert result == {
            "cto": "uuid-1",
            "backend-engineer": "uuid-2",
            "frontend-engineer": "uuid-3",
        }

    def test_resolve_all_skips_disabled_agents(self, mock_client):
        mock_client.list_agents.return_value = [
            _agent("cto", "uuid-1"),
            _agent("frontend-engineer", "uuid-2"),
        ]
        registry = AgentRegistry(mock_client, disabled_roles=["frontend-engineer"])
        result = registry.resolve_all()
        assert result == {"cto": "uuid-1"}

    def test_resolve_all_skips_inactive_agents(self, mock_client):
        mock_client.list_agents.return_value = [
            _agent("cto", "uuid-1", status="active"),
            _agent("qa-engineer", "uuid-2", status="inactive"),
        ]
        registry = AgentRegistry(mock_client)
        result = registry.resolve_all()
        assert result == {"cto": "uuid-1"}

    def test_resolve_all_empty_org(self, mock_client):
        mock_client.list_agents.return_value = []
        registry = AgentRegistry(mock_client)
        result = registry.resolve_all()
        assert result == {}

    def test_resolve_all_api_error_raises(self, mock_client):
        mock_client.list_agents.side_effect = PaperclipAPIError(500, "Server down")
        registry = AgentRegistry(mock_client)
        with pytest.raises(PaperclipAPIError):
            registry.resolve_all()

    def test_get_agent_id_returns_uuid(self, mock_client):
        mock_client.list_agents.return_value = [
            _agent("cto", "uuid-1"),
        ]
        registry = AgentRegistry(mock_client)
        registry.resolve_all()
        assert registry.get_agent_id("cto") == "uuid-1"

    def test_get_agent_id_unknown_role_returns_none(self, mock_client):
        mock_client.list_agents.return_value = [
            _agent("cto", "uuid-1"),
        ]
        registry = AgentRegistry(mock_client)
        registry.resolve_all()
        assert registry.get_agent_id("unknown-role") is None

    def test_roles_property(self, mock_client):
        mock_client.list_agents.return_value = [
            _agent("cto", "uuid-1"),
            _agent("backend-engineer", "uuid-2"),
        ]
        registry = AgentRegistry(mock_client)
        registry.resolve_all()
        assert set(registry.roles) == {"cto", "backend-engineer"}
