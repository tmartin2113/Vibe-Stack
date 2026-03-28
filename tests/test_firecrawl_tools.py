"""
Tests for infrastructure service tools (replaced Firecrawl).

Covers:
- WebSearchTool (SearXNG): query validation, result parsing, env gating
- WebScrapeTool (Playwright): URL validation, content extraction, env gating
- Registry wiring: tools registered when env vars set, absent otherwise
- Security: tool names in DEFAULT_ALLOWED_TOOLS / RESTRICTED_TOOLS
"""

import os
import json
import urllib.error
import urllib.request
from unittest.mock import patch, MagicMock

import pytest

from agents.tools.registry import (
    ToolResult,
    ToolCategory,
    create_default_tool_registry,
)
from agents.tools.web_search import WebSearchTool
from agents.tools.web_scrape import WebScrapeTool
from agents.tools.browser_automation import BrowserAutomationTool
from agents.tools.design import DesignTool
from agents.tools.image_generation import ImageGenerationTool
from agents.tools.git_forge import GitForgeTool
from agents.tools.artifact_storage import ArtifactStorageTool


# ============================================================
# WebSearchTool (SearXNG)
# ============================================================


class TestWebSearchTool:
    """Tests for the web_search tool (SearXNG-backed)."""

    def test_name_and_category(self):
        tool = WebSearchTool(base_url="http://searxng:8080")
        assert tool.name == "web_search"
        assert tool.category == ToolCategory.WEB_API

    def test_schema_has_required_query(self):
        tool = WebSearchTool(base_url="http://searxng:8080")
        schema = tool._get_parameters_schema()
        assert "query" in schema["properties"]
        assert "query" in schema["required"]

    def test_schema_has_optional_fields(self):
        tool = WebSearchTool(base_url="http://searxng:8080")
        schema = tool._get_parameters_schema()
        assert "categories" in schema["properties"]
        assert "engines" in schema["properties"]
        assert "limit" in schema["properties"]

    def test_empty_query_rejected(self):
        tool = WebSearchTool(base_url="http://searxng:8080")
        result = tool.execute(query="")
        assert not result.success
        assert "No query" in result.error

    def test_whitespace_query_rejected(self):
        tool = WebSearchTool(base_url="http://searxng:8080")
        result = tool.execute(query="   ")
        assert not result.success
        assert "No query" in result.error

    def test_missing_base_url(self):
        tool = WebSearchTool(base_url="")
        result = tool.execute(query="test query")
        assert not result.success
        assert "SEARXNG_URL" in result.error

    def test_base_url_from_env(self):
        with patch.dict(os.environ, {"SEARXNG_URL": "http://searxng:8080"}):
            tool = WebSearchTool()
            assert tool._base_url == "http://searxng:8080"

    def test_get_schema_full(self):
        tool = WebSearchTool(base_url="http://searxng:8080")
        schema = tool.get_schema()
        assert schema["name"] == "web_search"
        assert "description" in schema
        assert "parameters" in schema


# ============================================================
# WebScrapeTool (Playwright)
# ============================================================


class TestWebScrapeTool:
    """Tests for the web_scrape tool (Playwright-backed)."""

    def test_name_and_category(self):
        tool = WebScrapeTool(ws_url="ws://playwright:3003")
        assert tool.name == "web_scrape"
        assert tool.category == ToolCategory.WEB_API

    def test_schema_has_required_url(self):
        tool = WebScrapeTool(ws_url="ws://playwright:3003")
        schema = tool._get_parameters_schema()
        assert "url" in schema["properties"]
        assert "url" in schema["required"]

    def test_empty_url_rejected(self):
        tool = WebScrapeTool(ws_url="ws://playwright:3003")
        result = tool.execute(url="")
        assert not result.success
        assert "No URL" in result.error

    def test_non_http_url_rejected(self):
        tool = WebScrapeTool(ws_url="ws://playwright:3003")
        result = tool.execute(url="ftp://example.com")
        assert not result.success
        assert "http" in result.error.lower()

    def test_missing_ws_url(self):
        tool = WebScrapeTool(ws_url="")
        result = tool.execute(url="https://example.com")
        assert not result.success
        assert "PLAYWRIGHT_WS_URL" in result.error

    def test_ws_url_from_env(self):
        with patch.dict(os.environ, {"PLAYWRIGHT_WS_URL": "ws://playwright:3003"}):
            tool = WebScrapeTool()
            assert tool._ws_url == "ws://playwright:3003"


# ============================================================
# BrowserAutomationTool (Playwright)
# ============================================================


class TestBrowserAutomationTool:
    """Tests for the browser_automation tool."""

    def test_name_and_category(self):
        tool = BrowserAutomationTool(ws_url="ws://playwright:3003")
        assert tool.name == "browser_automation"
        assert tool.category == ToolCategory.WEB_API

    def test_empty_action_rejected(self):
        tool = BrowserAutomationTool(ws_url="ws://playwright:3003")
        result = tool.execute(action="")
        assert not result.success
        assert "No action" in result.error

    def test_invalid_action_rejected(self):
        tool = BrowserAutomationTool(ws_url="ws://playwright:3003")
        result = tool.execute(action="destroy")
        assert not result.success
        assert "Invalid action" in result.error

    def test_missing_ws_url(self):
        tool = BrowserAutomationTool(ws_url="")
        result = tool.execute(action="navigate", url="https://example.com")
        assert not result.success
        assert "PLAYWRIGHT_WS_URL" in result.error


# ============================================================
# DesignTool (Penpot)
# ============================================================


class TestDesignTool:
    """Tests for the design tool."""

    def test_name_and_category(self):
        tool = DesignTool(api_url="http://penpot:6060")
        assert tool.name == "design"
        assert tool.category == ToolCategory.EXTERNAL_SERVICE

    def test_empty_action_rejected(self):
        tool = DesignTool(api_url="http://penpot:6060")
        result = tool.execute(action="")
        assert not result.success
        assert "No action" in result.error

    def test_invalid_action_rejected(self):
        tool = DesignTool(api_url="http://penpot:6060")
        result = tool.execute(action="destroy")
        assert not result.success
        assert "Invalid action" in result.error

    def test_missing_api_url(self):
        tool = DesignTool(api_url="")
        result = tool.execute(action="list_projects")
        assert not result.success
        assert "PENPOT_API_URL" in result.error


# ============================================================
# ImageGenerationTool (ComfyUI)
# ============================================================


class TestImageGenerationTool:
    """Tests for the image_generation tool."""

    def test_name_and_category(self):
        tool = ImageGenerationTool(base_url="http://comfyui:8188")
        assert tool.name == "image_generation"
        assert tool.category == ToolCategory.EXTERNAL_SERVICE

    def test_empty_prompt_rejected(self):
        tool = ImageGenerationTool(base_url="http://comfyui:8188")
        result = tool.execute(prompt="")
        assert not result.success
        assert "No prompt" in result.error

    def test_missing_base_url(self):
        tool = ImageGenerationTool(base_url="")
        result = tool.execute(prompt="a cat")
        assert not result.success
        assert "COMFYUI_URL" in result.error


# ============================================================
# GitForgeTool (Gitea)
# ============================================================


class TestGitForgeTool:
    """Tests for the git_forge tool."""

    def test_name_and_category(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        assert tool.name == "git_forge"
        assert tool.category == ToolCategory.EXTERNAL_SERVICE

    def test_empty_action_rejected(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        result = tool.execute(action="")
        assert not result.success
        assert "No action" in result.error

    def test_invalid_action_rejected(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        result = tool.execute(action="destroy")
        assert not result.success
        assert "Invalid action" in result.error

    def test_missing_base_url(self):
        tool = GitForgeTool(base_url="")
        result = tool.execute(action="list_repos")
        assert not result.success
        assert "GITEA_URL" in result.error

    def test_create_repo_requires_name(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        result = tool.execute(action="create_repo")
        assert not result.success
        assert "name required" in result.error


# ============================================================
# ArtifactStorageTool (MinIO)
# ============================================================


class TestArtifactStorageTool:
    """Tests for the artifact_storage tool."""

    def test_name_and_category(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        assert tool.name == "artifact_storage"
        assert tool.category == ToolCategory.EXTERNAL_SERVICE

    def test_empty_action_rejected(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        result = tool.execute(action="")
        assert not result.success
        assert "No action" in result.error

    def test_invalid_action_rejected(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        result = tool.execute(action="destroy")
        assert not result.success
        assert "Invalid action" in result.error

    def test_missing_base_url(self):
        tool = ArtifactStorageTool(base_url="")
        result = tool.execute(action="list")
        assert not result.success
        assert "MINIO_URL" in result.error

    def test_put_requires_key(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        result = tool.execute(action="put")
        assert not result.success
        assert "key required" in result.error

    def test_put_requires_content(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        result = tool.execute(action="put", key="test.txt")
        assert not result.success
        assert "content required" in result.error


# ============================================================
# Registry wiring
# ============================================================


class TestInfraRegistryWiring:
    """Test that infrastructure tools appear in the registry under correct conditions."""

    def _clean_env(self):
        """Remove all infrastructure service env vars."""
        for key in ["SEARXNG_URL", "PLAYWRIGHT_WS_URL",
                     "PENPOT_API_URL", "COMFYUI_URL", "GITEA_URL", "MINIO_URL",
                     "FIRECRAWL_API_KEY"]:
            os.environ.pop(key, None)

    def test_no_tools_when_env_missing(self):
        with patch.dict(os.environ, {}, clear=False):
            self._clean_env()
            registry = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=False)
            assert registry.get("web_search") is None
            assert registry.get("web_scrape") is None
            assert registry.get("browser_automation") is None
            assert registry.get("design") is None
            assert registry.get("image_generation") is None
            assert registry.get("git_forge") is None
            assert registry.get("artifact_storage") is None

    def test_searxng_registered_when_env_set(self):
        with patch.dict(os.environ, {"SEARXNG_URL": "http://searxng:8080"}):
            registry = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=False)
            assert registry.get("web_search") is not None

    def test_web_scrape_registered_when_playwright_set(self):
        with patch.dict(os.environ, {"PLAYWRIGHT_WS_URL": "ws://playwright:3003"}):
            registry = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=False)
            assert registry.get("web_scrape") is not None

    def test_all_tools_registered(self):
        env = {
            "SEARXNG_URL": "http://searxng:8080",
            "PLAYWRIGHT_WS_URL": "ws://playwright:3003",
            "PENPOT_API_URL": "http://penpot:6060",
            "COMFYUI_URL": "http://comfyui:8188",
            "GITEA_URL": "http://gitea:3000",
            "MINIO_URL": "http://minio:9000",
        }
        with patch.dict(os.environ, env):
            registry = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=False)
            for name in ["web_search", "web_scrape", "browser_automation",
                         "design", "image_generation", "git_forge", "artifact_storage"]:
                assert registry.get(name) is not None, f"Expected {name} to be registered"

    def test_tool_count_with_infra(self):
        """Registry should have 7 more tools when all infra env vars are set."""
        with patch.dict(os.environ, {}, clear=False):
            for key in ["SEARXNG_URL", "PLAYWRIGHT_WS_URL",
                         "PENPOT_API_URL", "COMFYUI_URL", "GITEA_URL", "MINIO_URL",
                         "FIRECRAWL_API_KEY"]:
                os.environ.pop(key, None)
            reg_without = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=False)
            count_without = len(reg_without.list_tools())

        env = {
            "SEARXNG_URL": "http://searxng:8080",
            "PLAYWRIGHT_WS_URL": "ws://playwright:3003",
            "PENPOT_API_URL": "http://penpot:6060",
            "COMFYUI_URL": "http://comfyui:8188",
            "GITEA_URL": "http://gitea:3000",
            "MINIO_URL": "http://minio:9000",
        }
        with patch.dict(os.environ, env):
            reg_with = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=False)
            count_with = len(reg_with.list_tools())

        assert count_with == count_without + 7


# ============================================================
# Security integration
# ============================================================


class TestInfraToolSecurity:
    """Verify infrastructure tool names in skill security sets."""

    def test_search_and_scrape_in_default_allowed(self):
        """web_search and web_scrape should be allowed by default for all skills."""
        from agents.skill_security import DEFAULT_ALLOWED_TOOLS
        assert "web_search" in DEFAULT_ALLOWED_TOOLS
        assert "web_scrape" in DEFAULT_ALLOWED_TOOLS

    def test_restricted_tools_set(self):
        """Interactive/write tools should be in RESTRICTED_TOOLS."""
        from agents.skill_security import RESTRICTED_TOOLS
        assert "browser_automation" in RESTRICTED_TOOLS
        assert "design" in RESTRICTED_TOOLS
        assert "image_generation" in RESTRICTED_TOOLS
        assert "git_forge" in RESTRICTED_TOOLS
        assert "artifact_storage" in RESTRICTED_TOOLS

    def test_search_scrape_not_in_restricted(self):
        from agents.skill_security import RESTRICTED_TOOLS
        assert "web_search" not in RESTRICTED_TOOLS
        assert "web_scrape" not in RESTRICTED_TOOLS

    def test_all_infra_tools_in_all_known(self):
        from agents.skill_security import ALL_KNOWN_TOOLS
        for name in ["web_search", "web_scrape", "browser_automation",
                      "design", "image_generation", "git_forge", "artifact_storage"]:
            assert name in ALL_KNOWN_TOOLS, f"Expected {name} in ALL_KNOWN_TOOLS"

    def test_skills_get_search_scrape_by_default(self):
        """Skills without allowed-tools frontmatter get web_search and web_scrape."""
        from agents.skill_security import SkillSecurity
        security = SkillSecurity()
        content = "# Just a skill with no frontmatter"
        allowed = security.parse_allowed_tools(content)
        assert "web_search" in allowed
        assert "web_scrape" in allowed

    def test_bulletin_board_in_default_allowed(self):
        """bulletin_board should be allowed by default for all skills."""
        from agents.skill_security import DEFAULT_ALLOWED_TOOLS
        assert "bulletin_board" in DEFAULT_ALLOWED_TOOLS

    def test_bulletin_board_in_all_known(self):
        from agents.skill_security import ALL_KNOWN_TOOLS
        assert "bulletin_board" in ALL_KNOWN_TOOLS

    def test_bulletin_board_not_in_restricted(self):
        from agents.skill_security import RESTRICTED_TOOLS
        assert "bulletin_board" not in RESTRICTED_TOOLS


# ============================================================
# BulletinBoardTool
# ============================================================


class TestBulletinBoardTool:
    """Tests for the bulletin_board tool (V2 SQLite-backed inter-agent messaging)."""

    def _make_store(self, tmp_path):
        from agents.message_store import MessageStore
        return MessageStore(db_path=tmp_path / "test.db")

    def test_name_and_category(self):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        assert tool.name == "bulletin_board"
        assert tool.category == ToolCategory.SPECIALIZED

    def test_schema_has_required_action(self):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        schema = tool._get_parameters_schema()
        assert "action" in schema["properties"]
        assert "action" in schema["required"]

    def test_schema_has_optional_fields(self):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        schema = tool._get_parameters_schema()
        assert "message" in schema["properties"]
        assert "topic" in schema["properties"]
        assert "limit" in schema["properties"]
        assert "query" in schema["properties"]

    def test_empty_action_rejected(self, tmp_path):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            result = tool.execute(action="")
            assert not result.success
            assert "No action" in result.error

    def test_unknown_action_rejected(self, tmp_path):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        store = self._make_store(tmp_path)
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="delete")
                assert not result.success
                assert "Unknown action" in result.error

    def test_missing_env_var(self):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BULLETIN_PATH", None)
            os.environ.pop("MESSAGE_STORE_PATH", None)
            result = tool.execute(action="read")
            assert not result.success
            assert "not configured" in result.error

    def test_post_success(self, tmp_path):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        store = self._make_store(tmp_path)
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db"), "VIBE_AGENT_NAME": "test-agent"}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="post", message="Hello from tests")
                assert result.success
                assert "Posted" in result.output
                assert result.metadata["sender"] == "test-agent"

    def test_post_with_topic(self, tmp_path):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        store = self._make_store(tmp_path)
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db"), "VIBE_AGENT_NAME": "test-agent"}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="post", message="Use Redis", topic="architecture")
                assert result.success
                assert result.metadata["topic"] == "architecture"

    def test_post_empty_message_rejected(self, tmp_path):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        store = self._make_store(tmp_path)
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="post", message="")
                assert not result.success
                assert "No message" in result.error

    def test_read_empty(self, tmp_path):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        store = self._make_store(tmp_path)
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="read")
                assert result.success
                assert "No messages" in result.output

    def test_read_returns_posted_entries(self, tmp_path):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        store = self._make_store(tmp_path)
        store.send(content="First message", sender="agent-1")
        store.send(content="Second message", sender="agent-1")
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="read", limit=10)
                assert result.success
                assert "First message" in result.output
                assert "Second message" in result.output
                assert result.metadata["count"] == 2

    def test_read_respects_limit(self, tmp_path):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        store = self._make_store(tmp_path)
        for i in range(5):
            store.send(content=f"Message {i}", sender="agent-1")
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="read", limit=2)
                assert result.success
                assert result.metadata["count"] == 2

    def test_search_finds_matching_entries(self, tmp_path):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        store = self._make_store(tmp_path)
        store.send(content="Use Redis for caching", sender="agent-1")
        store.send(content="SQLite for local storage", sender="agent-1")
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="search", query="Redis")
                assert result.success
                assert "Redis" in result.output
                assert result.metadata["count"] >= 1

    def test_search_no_match(self, tmp_path):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        store = self._make_store(tmp_path)
        store.send(content="Hello world", sender="agent-1")
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="search", query="nonexistent")
                assert result.success
                assert "No messages" in result.output

    def test_search_empty_query_rejected(self, tmp_path):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        store = self._make_store(tmp_path)
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="search", query="")
                assert not result.success
                assert "No search query" in result.error

    def test_agent_name_fallback_to_paperclip_id(self, tmp_path):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        store = self._make_store(tmp_path)
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db"), "PAPERCLIP_AGENT_ID": "pc-agent-42"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("VIBE_AGENT_NAME", None)
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="post", message="Test")
                assert result.metadata["sender"] == "pc-agent-42"

    def test_get_schema_full(self):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        schema = tool.get_schema()
        assert schema["name"] == "bulletin_board"
        assert "description" in schema
        assert "parameters" in schema

    def test_read_with_topic_shows_topic(self, tmp_path):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        store = self._make_store(tmp_path)
        store.send(content="Use Redis", sender="agent-1", topic="architecture")
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="read")
                assert result.success
                assert "architecture" in result.output

    def test_read_limit_clamped_low(self, tmp_path):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        store = self._make_store(tmp_path)
        store.send(content="msg", sender="agent-1")
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="read", limit=0)
                assert result.success
                assert result.metadata["count"] == 1

    def test_read_limit_clamped_high(self, tmp_path):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        store = self._make_store(tmp_path)
        store.send(content="msg", sender="agent-1")
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="read", limit=999)
                assert result.success
                assert result.metadata["count"] == 1

    def test_search_by_topic(self, tmp_path):
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        store = self._make_store(tmp_path)
        store.send(content="Use Redis", sender="agent-1", topic="architecture")
        store.send(content="Fix bug", sender="agent-1", topic="bugfix")
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = tool.execute(action="search", query="architecture")
                assert result.success
                assert "Redis" in result.output
                assert result.metadata["count"] >= 1

    def test_read_recent_entries_no_env(self):
        from agents.tools.bulletin_board import read_recent_entries
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BULLETIN_PATH", None)
            os.environ.pop("MESSAGE_STORE_PATH", None)
            assert read_recent_entries() == ""

    def test_read_recent_entries_with_posts(self, tmp_path):
        from agents.tools.bulletin_board import read_recent_entries
        store = self._make_store(tmp_path)
        store.send(content="Hello world", sender="agent-1")
        store.send(content="Topic post", sender="agent-1", topic="test-topic")
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=store):
                result = read_recent_entries(limit=10)
                assert "Bulletin Board" in result
                assert "Hello world" in result

    def test_read_recent_entries_error_handling(self, tmp_path):
        """read_recent_entries returns empty string on error."""
        from agents.tools.bulletin_board import read_recent_entries
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", side_effect=OSError("disk error")):
                assert read_recent_entries() == ""

    def test_post_error_handling(self, tmp_path):
        """Post handles store errors gracefully."""
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        mock_store = MagicMock()
        mock_store.send.side_effect = RuntimeError("db error")
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=mock_store):
                result = tool.execute(action="post", message="test")
                assert not result.success
                assert "Failed to post" in result.error

    def test_search_error_handling(self, tmp_path):
        """Search handles store errors gracefully."""
        from agents.tools.bulletin_board import BulletinBoardTool
        tool = BulletinBoardTool()
        mock_store = MagicMock()
        mock_store.hybrid_search.side_effect = RuntimeError("db error")
        env = {"MESSAGE_STORE_PATH": str(tmp_path / "test.db")}
        with patch.dict(os.environ, env, clear=False):
            with patch("agents.tools.bulletin_board._get_store", return_value=mock_store):
                result = tool.execute(action="search", query="test")
                assert not result.success
                assert "Failed to search" in result.error


class TestBulletinRegistryWiring:
    """Test that bulletin_board appears in the registry when env is set."""

    def test_not_registered_without_env(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BULLETIN_PATH", None)
            os.environ.pop("MESSAGE_STORE_PATH", None)
            registry = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=False)
            assert registry.get("bulletin_board") is None

    def test_registered_when_bulletin_path_set(self):
        with patch.dict(os.environ, {"BULLETIN_PATH": "/shared/bulletin/BULLETIN.md"}):
            registry = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=False)
            assert registry.get("bulletin_board") is not None

    def test_registered_when_message_store_path_set(self):
        with patch.dict(os.environ, {"MESSAGE_STORE_PATH": "/shared/bulletin/messages.db"}):
            registry = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=False)
            assert registry.get("bulletin_board") is not None


# ============================================================
# WebSearchTool execute() — mocked HTTP
# ============================================================


class TestWebSearchExecution:
    """Tests for WebSearchTool.execute() with mocked HTTP responses."""

    def _mock_response(self, body: bytes):
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_successful_search_with_results(self):
        tool = WebSearchTool(base_url="http://searxng:8080")
        payload = {
            "results": [
                {"title": "Python Docs", "url": "http://docs.python.org", "content": "Official docs", "engine": "google"},
                {"title": "PEP 8", "url": "http://pep8.org", "content": "Style guide", "engine": "duckduckgo"},
            ]
        }
        mock_resp = self._mock_response(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = tool.execute(query="python")
            assert result.success
            assert "Python Docs" in result.output
            assert "http://docs.python.org" in result.output
            assert "(google)" in result.output
            assert "PEP 8" in result.output
            assert result.metadata["results"] == 2
            assert result.metadata["query"] == "python"

    def test_empty_results(self):
        tool = WebSearchTool(base_url="http://searxng:8080")
        payload = {"results": []}
        mock_resp = self._mock_response(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = tool.execute(query="obscure query xyz")
            assert result.success
            assert "No results found" in result.output
            assert result.metadata["results"] == 0

    def test_http_error(self):
        tool = WebSearchTool(base_url="http://searxng:8080")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
            result = tool.execute(query="test")
            assert not result.success
            assert "SearXNG search failed" in result.error

    def test_limit_clamped(self):
        tool = WebSearchTool(base_url="http://searxng:8080")
        many_results = [{"title": f"R{i}", "url": f"http://r{i}.com", "content": f"s{i}", "engine": "g"} for i in range(25)]
        payload = {"results": many_results}
        mock_resp = self._mock_response(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = tool.execute(query="test", limit=25)
            assert result.success
            # limit is clamped to 20
            assert result.metadata["results"] == 20

    def test_result_formatting_no_snippet(self):
        tool = WebSearchTool(base_url="http://searxng:8080")
        payload = {"results": [{"title": "NoSnippet", "url": "http://example.com", "content": "", "engine": ""}]}
        mock_resp = self._mock_response(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = tool.execute(query="test")
            assert result.success
            assert "NoSnippet" in result.output


# ============================================================
# WebScrapeTool execute() — mocked subprocess
# ============================================================


import subprocess as _subprocess_mod


class TestWebScrapeExecution:
    """Tests for WebScrapeTool.execute() with mocked Playwright subprocess."""

    def _mock_completed(self, content, metadata=None, returncode=0):
        output = json.dumps({
            "content": content,
            "metadata": metadata or {"url": "http://example.com", "title": "Test", "format": "markdown", "length": len(content), "status": 200},
        })
        return MagicMock(returncode=returncode, stdout=output, stderr="")

    def test_successful_scrape(self):
        tool = WebScrapeTool(ws_url="ws://playwright:3003")
        mock_result = self._mock_completed("# Hello World\nSome text", {
            "url": "http://example.com", "title": "Test", "format": "markdown",
            "length": 23, "status": 200,
        })
        with patch("subprocess.run", return_value=mock_result):
            result = tool.execute(url="http://example.com")
            assert result.success
            assert "Hello World" in result.output
            assert result.metadata["url"] == "http://example.com"

    def test_subprocess_failure(self):
        tool = WebScrapeTool(ws_url="ws://playwright:3003")
        mock_result = MagicMock(returncode=1, stdout="", stderr="Connection refused")
        with patch("subprocess.run", return_value=mock_result):
            result = tool.execute(url="http://example.com")
            assert not result.success
            assert "Connection refused" in result.error

    def test_timeout(self):
        tool = WebScrapeTool(ws_url="ws://playwright:3003")
        with patch("subprocess.run", side_effect=_subprocess_mod.TimeoutExpired("cmd", 30)):
            result = tool.execute(url="http://example.com", timeout=30)
            assert not result.success
            assert "timed out" in result.error

    def test_non_json_stdout(self):
        tool = WebScrapeTool(ws_url="ws://playwright:3003")
        mock_result = MagicMock(returncode=0, stdout="plain text output", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = tool.execute(url="http://example.com")
            assert result.success
            assert "plain text output" in result.output


# ============================================================
# BrowserAutomationTool execute() — mocked subprocess
# ============================================================


import subprocess


class TestBrowserAutomationExecution:
    """Tests for BrowserAutomationTool.execute() with mocked subprocess."""

    def test_successful_json_output(self):
        tool = BrowserAutomationTool(ws_url="ws://playwright:3003")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"result": "Navigated to http://example.com", "metadata": {"action": "navigate", "status": 200}})
        mock_result.stderr = ""
        with patch("subprocess.run", return_value=mock_result):
            result = tool.execute(action="navigate", url="http://example.com")
            assert result.success
            assert "Navigated to http://example.com" in result.output
            assert result.metadata["action"] == "navigate"
            assert result.metadata["status"] == 200

    def test_successful_non_json_output(self):
        tool = BrowserAutomationTool(ws_url="ws://playwright:3003")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Plain text output from browser"
        mock_result.stderr = ""
        with patch("subprocess.run", return_value=mock_result):
            result = tool.execute(action="get_text", selector="body")
            assert result.success
            assert "Plain text output from browser" in result.output
            assert result.metadata["action"] == "get_text"

    def test_failed_execution(self):
        tool = BrowserAutomationTool(ws_url="ws://playwright:3003")
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "Error: element not found"
        with patch("subprocess.run", return_value=mock_result):
            result = tool.execute(action="click", selector="#btn")
            assert not result.success
            assert "element not found" in result.error

    def test_timeout(self):
        tool = BrowserAutomationTool(ws_url="ws://playwright:3003")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="python", timeout=45)):
            result = tool.execute(action="navigate", url="http://slow.example.com")
            assert not result.success
            assert "timed out" in result.error

    def test_generic_exception(self):
        tool = BrowserAutomationTool(ws_url="ws://playwright:3003")
        with patch("subprocess.run", side_effect=OSError("spawn failed")):
            result = tool.execute(action="navigate", url="http://example.com")
            assert not result.success
            assert "Browser automation failed" in result.error


# ============================================================
# DesignTool execute() — mocked HTTP
# ============================================================


class TestDesignExecution:
    """Tests for DesignTool.execute() with mocked HTTP responses."""

    def _mock_response(self, body: bytes):
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_list_projects(self):
        tool = DesignTool(api_url="http://penpot:6060")
        payload = [{"id": "proj-1", "name": "My Project"}, {"id": "proj-2", "name": "Other"}]
        mock_resp = self._mock_response(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = tool.execute(action="list_projects")
            assert result.success
            assert "My Project" in result.output
            assert result.metadata["command"] == "get-projects"

    def test_get_project_with_id(self):
        tool = DesignTool(api_url="http://penpot:6060")
        payload = {"id": "proj-1", "name": "My Project", "files": []}
        mock_resp = self._mock_response(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = tool.execute(action="get_project", project_id="proj-1")
            assert result.success
            assert "proj-1" in result.output
            assert result.metadata["command"] == "get-project"

    def test_get_project_missing_id(self):
        tool = DesignTool(api_url="http://penpot:6060")
        result = tool.execute(action="get_project")
        assert not result.success
        assert "project_id required" in result.error

    def test_create_file_success(self):
        tool = DesignTool(api_url="http://penpot:6060")
        payload = {"id": "file-1", "name": "new-design", "project-id": "proj-1"}
        mock_resp = self._mock_response(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = tool.execute(action="create_file", project_id="proj-1", name="new-design")
            assert result.success
            assert "new-design" in result.output
            assert result.metadata["command"] == "create-file"

    def test_create_file_missing_project_id(self):
        tool = DesignTool(api_url="http://penpot:6060")
        result = tool.execute(action="create_file", name="test")
        assert not result.success
        assert "project_id required" in result.error

    def test_create_file_missing_name(self):
        tool = DesignTool(api_url="http://penpot:6060")
        result = tool.execute(action="create_file", project_id="proj-1")
        assert not result.success
        assert "name required" in result.error

    def test_list_components(self):
        tool = DesignTool(api_url="http://penpot:6060")
        payload = [{"id": "comp-1", "name": "Button"}, {"id": "comp-2", "name": "Card"}]
        mock_resp = self._mock_response(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = tool.execute(action="list_components", file_id="file-1")
            assert result.success
            assert "Button" in result.output
            assert result.metadata["command"] == "get-file-components"

    def test_list_components_missing_file_id(self):
        tool = DesignTool(api_url="http://penpot:6060")
        result = tool.execute(action="list_components")
        assert not result.success
        assert "file_id required" in result.error

    def test_export_asset_missing_params(self):
        tool = DesignTool(api_url="http://penpot:6060")
        result = tool.execute(action="export_asset", file_id="file-1")
        assert not result.success
        assert "file_id and component_id required" in result.error

    def test_export_asset_missing_file_id(self):
        tool = DesignTool(api_url="http://penpot:6060")
        result = tool.execute(action="export_asset", component_id="comp-1")
        assert not result.success
        assert "file_id and component_id required" in result.error

    def test_export_asset_success(self):
        tool = DesignTool(api_url="http://penpot:6060")
        payload = {"data": "svg-content-here"}
        mock_resp = self._mock_response(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = tool.execute(action="export_asset", file_id="file-1", component_id="comp-1", format="svg")
            assert result.success
            assert result.metadata["command"] == "export"

    def test_http_error(self):
        tool = DesignTool(api_url="http://penpot:6060")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
            result = tool.execute(action="list_projects")
            assert not result.success
            assert "Penpot API call failed" in result.error


# ============================================================
# ImageGenerationTool execute() — mocked HTTP
# ============================================================


class TestImageGenerationExecution:
    """Tests for ImageGenerationTool.execute() with mocked HTTP responses."""

    def _mock_response(self, body: bytes):
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_successful_generation(self):
        tool = ImageGenerationTool(base_url="http://comfyui:8188")
        queue_resp = self._mock_response(json.dumps({"prompt_id": "abc-123"}).encode())
        history_resp = self._mock_response(json.dumps({
            "abc-123": {
                "outputs": {
                    "9": {
                        "images": [{"filename": "vibe_00001_.png", "subfolder": "", "type": "output"}]
                    }
                }
            }
        }).encode())

        call_count = [0]
        def mock_urlopen(req, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return queue_resp
            return history_resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            with patch("time.sleep"):
                result = tool.execute(prompt="a beautiful sunset", width=512, height=512, steps=20, seed=42)
                assert result.success
                assert "abc-123" in result.output
                assert result.metadata["prompt_id"] == "abc-123"
                assert "image_url" in result.metadata
                assert "vibe_00001_.png" in result.metadata["image_url"]

    def test_missing_prompt_id_in_queue_response(self):
        tool = ImageGenerationTool(base_url="http://comfyui:8188")
        queue_resp = self._mock_response(json.dumps({"error": "bad workflow"}).encode())
        with patch("urllib.request.urlopen", return_value=queue_resp):
            result = tool.execute(prompt="a cat")
            assert not result.success
            assert "did not return a prompt_id" in result.error

    def test_poll_timeout(self):
        tool = ImageGenerationTool(base_url="http://comfyui:8188")
        queue_resp = self._mock_response(json.dumps({"prompt_id": "timeout-id"}).encode())
        # History always returns empty (prompt never completes)
        empty_history = self._mock_response(json.dumps({}).encode())

        call_count = [0]
        def mock_urlopen(req, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return queue_resp
            return empty_history

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            with patch("time.sleep"):
                with patch("time.monotonic", side_effect=[0, 0, 9999]):
                    result = tool.execute(prompt="a cat", steps=1)
                    assert not result.success
                    assert "timed out" in result.error

    def test_dimension_clamping(self):
        tool = ImageGenerationTool(base_url="http://comfyui:8188")
        queue_resp = self._mock_response(json.dumps({"prompt_id": "clamp-id"}).encode())
        history_resp = self._mock_response(json.dumps({
            "clamp-id": {
                "outputs": {
                    "9": {
                        "images": [{"filename": "out.png", "subfolder": "", "type": "output"}]
                    }
                }
            }
        }).encode())

        call_count = [0]
        def mock_urlopen(req, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return queue_resp
            return history_resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            with patch("time.sleep"):
                # Width too small, height too large
                result = tool.execute(prompt="test", width=10, height=5000, steps=1, seed=42)
                assert result.success
                assert result.metadata["width"] == 64
                assert result.metadata["height"] == 2048
                assert result.metadata["steps"] == 1

    def test_http_error(self):
        tool = ImageGenerationTool(base_url="http://comfyui:8188")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
            result = tool.execute(prompt="a cat")
            assert not result.success
            assert "ComfyUI image generation failed" in result.error

    def test_completed_but_no_images(self):
        tool = ImageGenerationTool(base_url="http://comfyui:8188")
        queue_resp = self._mock_response(json.dumps({"prompt_id": "no-img"}).encode())
        history_resp = self._mock_response(json.dumps({
            "no-img": {
                "outputs": {
                    "9": {"images": []}
                }
            }
        }).encode())

        call_count = [0]
        def mock_urlopen(req, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return queue_resp
            return history_resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            with patch("time.sleep"):
                result = tool.execute(prompt="a cat", steps=1, seed=42)
                assert not result.success
                assert "timed out" in result.error


# ============================================================
# GitForgeTool execute() — mocked HTTP
# ============================================================


class TestGitForgeExecution:
    """Tests for GitForgeTool.execute() with mocked HTTP responses."""

    def _mock_response(self, body: bytes):
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_list_repos(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        payload = {"data": [{"name": "my-repo", "full_name": "user/my-repo"}]}
        mock_resp = self._mock_response(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = tool.execute(action="list_repos")
            assert result.success
            assert "my-repo" in result.output
            assert result.metadata["path"] == "/api/v1/repos/search?limit=50"

    def test_create_repo_success(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        payload = {"id": 1, "name": "new-repo", "full_name": "user/new-repo"}
        mock_resp = self._mock_response(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = tool.execute(action="create_repo", name="new-repo")
            assert result.success
            assert "new-repo" in result.output
            assert result.metadata["method"] == "POST"

    def test_create_repo_missing_name(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        result = tool.execute(action="create_repo")
        assert not result.success
        assert "name required" in result.error

    def test_get_file_missing_params(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        result = tool.execute(action="get_file", owner="user", repo="repo")
        assert not result.success
        assert "owner, repo, and path required" in result.error

    def test_get_file_missing_owner(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        result = tool.execute(action="get_file", repo="repo", path="README.md")
        assert not result.success
        assert "owner, repo, and path required" in result.error

    def test_get_file_success(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        payload = {"name": "README.md", "content": "SGVsbG8=", "encoding": "base64"}
        mock_resp = self._mock_response(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = tool.execute(action="get_file", owner="user", repo="myrepo", path="README.md")
            assert result.success
            assert "README.md" in result.output

    def test_create_file_success(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        payload = {"content": {"name": "hello.py", "path": "hello.py"}}
        mock_resp = self._mock_response(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = tool.execute(
                action="create_file", owner="user", repo="myrepo",
                path="hello.py", content="print('hello')", message="add hello.py"
            )
            assert result.success
            assert result.metadata["method"] == "POST"

    def test_create_file_missing_params(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        result = tool.execute(action="create_file", owner="user", repo="myrepo", path="file.py")
        assert not result.success
        assert "owner, repo, path, and content required" in result.error

    def test_list_branches(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        payload = [{"name": "main"}, {"name": "dev"}]
        mock_resp = self._mock_response(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = tool.execute(action="list_branches", owner="user", repo="myrepo")
            assert result.success
            assert "main" in result.output
            assert "dev" in result.output

    def test_list_branches_missing_params(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        result = tool.execute(action="list_branches", owner="user")
        assert not result.success
        assert "owner and repo required" in result.error

    def test_create_issue(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        payload = {"id": 1, "title": "Bug report", "body": "Something broke"}
        mock_resp = self._mock_response(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = tool.execute(
                action="create_issue", owner="user", repo="myrepo",
                title="Bug report", content="Something broke"
            )
            assert result.success
            assert "Bug report" in result.output
            assert result.metadata["method"] == "POST"

    def test_create_issue_missing_params(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        result = tool.execute(action="create_issue", owner="user", repo="myrepo")
        assert not result.success
        assert "owner, repo, and title required" in result.error

    def test_http_error(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
            result = tool.execute(action="list_repos")
            assert not result.success
            assert "Gitea API call failed" in result.error

    def test_api_token_added_to_requests(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        payload = {"data": []}
        mock_resp = self._mock_response(json.dumps(payload).encode())
        with patch.dict(os.environ, {"GITEA_API_TOKEN": "my-token-123"}):
            with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
                result = tool.execute(action="list_repos")
                assert result.success
                req_obj = mock_urlopen.call_args[0][0]
                assert req_obj.get_header("Authorization") == "token my-token-123"

    def test_list_issues(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        payload = [{"id": 1, "title": "Bug"}, {"id": 2, "title": "Feature"}]
        mock_resp = self._mock_response(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = tool.execute(action="list_issues", owner="user", repo="myrepo")
            assert result.success
            assert "Bug" in result.output

    def test_create_pull_request(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        payload = {"id": 1, "title": "My PR", "base": "main", "head": "feature"}
        mock_resp = self._mock_response(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = tool.execute(
                action="create_pull_request", owner="user", repo="myrepo",
                title="My PR", head="feature", base="main"
            )
            assert result.success
            assert "My PR" in result.output

    def test_create_pull_request_missing_params(self):
        tool = GitForgeTool(base_url="http://gitea:3000")
        result = tool.execute(action="create_pull_request", owner="user", repo="myrepo", title="PR")
        assert not result.success
        assert "owner, repo, title, and head required" in result.error


# ============================================================
# ArtifactStorageTool execute() — mocked HTTP
# ============================================================


class TestArtifactStorageExecution:
    """Tests for ArtifactStorageTool.execute() with mocked HTTP responses."""

    def _mock_response(self, body: bytes):
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_put_success(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        mock_resp = self._mock_response(b"")
        with patch.dict(os.environ, {"MINIO_ROOT_USER": "vibe", "MINIO_ROOT_PASSWORD": "secret"}):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                result = tool.execute(action="put", key="test/file.txt", content="Hello world")
                assert result.success
                assert "11 bytes" in result.output
                assert result.metadata["key"] == "test/file.txt"
                assert result.metadata["bucket"] == "vibe-artifacts"

    def test_put_missing_key(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        result = tool.execute(action="put", content="data")
        assert not result.success
        assert "key required" in result.error

    def test_put_missing_content(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        result = tool.execute(action="put", key="test.txt")
        assert not result.success
        assert "content required" in result.error

    def test_get_text_response(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        mock_resp = self._mock_response(b"File contents here")
        with patch.dict(os.environ, {"MINIO_ROOT_USER": "vibe", "MINIO_ROOT_PASSWORD": "secret"}):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                result = tool.execute(action="get", key="test/file.txt")
                assert result.success
                assert "File contents here" in result.output
                assert result.metadata["size"] == 18

    def test_get_binary_response(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        # Binary data that can't be decoded as UTF-8
        binary_data = bytes([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10])
        mock_resp = self._mock_response(binary_data)
        with patch.dict(os.environ, {"MINIO_ROOT_USER": "vibe", "MINIO_ROOT_PASSWORD": "secret"}):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                result = tool.execute(action="get", key="image.png")
                assert result.success
                # Binary data should be base64-encoded
                import base64
                assert result.output == base64.b64encode(binary_data).decode()

    def test_get_missing_key(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        result = tool.execute(action="get")
        assert not result.success
        assert "key required" in result.error

    def test_list_objects(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        xml_body = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult>
    <Contents><Key>builds/output.zip</Key><Size>1024</Size></Contents>
    <Contents><Key>builds/log.txt</Key><Size>256</Size></Contents>
</ListBucketResult>"""
        mock_resp = self._mock_response(xml_body.encode())
        with patch.dict(os.environ, {"MINIO_ROOT_USER": "vibe", "MINIO_ROOT_PASSWORD": "secret"}):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                result = tool.execute(action="list", prefix="builds/")
                assert result.success
                assert "builds/output.zip" in result.output
                assert "1024 bytes" in result.output
                assert "builds/log.txt" in result.output
                assert result.metadata["count"] == 2

    def test_list_empty(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        xml_body = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult></ListBucketResult>"""
        mock_resp = self._mock_response(xml_body.encode())
        with patch.dict(os.environ, {"MINIO_ROOT_USER": "vibe", "MINIO_ROOT_PASSWORD": "secret"}):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                result = tool.execute(action="list")
                assert result.success
                assert "No objects found" in result.output
                assert result.metadata["count"] == 0

    def test_delete_success(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        mock_resp = self._mock_response(b"")
        with patch.dict(os.environ, {"MINIO_ROOT_USER": "vibe", "MINIO_ROOT_PASSWORD": "secret"}):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                result = tool.execute(action="delete", key="old/file.txt")
                assert result.success
                assert "Deleted" in result.output
                assert "old/file.txt" in result.output

    def test_delete_missing_key(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        result = tool.execute(action="delete")
        assert not result.success
        assert "key required" in result.error

    def test_presign(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        result = tool.execute(action="presign", key="builds/output.zip")
        assert result.success
        assert "http://minio:9000/vibe-artifacts/builds/output.zip" in result.output
        assert result.metadata["url"] == "http://minio:9000/vibe-artifacts/builds/output.zip"
        assert result.metadata["bucket"] == "vibe-artifacts"
        assert result.metadata["key"] == "builds/output.zip"

    def test_presign_missing_key(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        result = tool.execute(action="presign")
        assert not result.success
        assert "key required" in result.error

    def test_presign_custom_bucket(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        result = tool.execute(action="presign", key="data.csv", bucket="custom-bucket")
        assert result.success
        assert "http://minio:9000/custom-bucket/data.csv" in result.output
        assert result.metadata["bucket"] == "custom-bucket"

    def test_http_error(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        with patch.dict(os.environ, {"MINIO_ROOT_USER": "vibe", "MINIO_ROOT_PASSWORD": "secret"}):
            with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
                result = tool.execute(action="get", key="test.txt")
                assert not result.success
                assert "MinIO operation failed" in result.error

    def test_custom_bucket(self):
        tool = ArtifactStorageTool(base_url="http://minio:9000")
        mock_resp = self._mock_response(b"")
        with patch.dict(os.environ, {"MINIO_ROOT_USER": "vibe", "MINIO_ROOT_PASSWORD": "secret"}):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                result = tool.execute(action="put", key="file.txt", content="data", bucket="my-bucket")
                assert result.success
                assert result.metadata["bucket"] == "my-bucket"
