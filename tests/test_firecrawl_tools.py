"""
Tests for Firecrawl tool integration.

Covers:
- FirecrawlScrapeTool: URL validation, API key checks, import errors,
  scrape success/failure, metadata extraction, format options
- FirecrawlCrawlTool: URL validation, API key checks, limit clamping,
  page combination, include/exclude patterns
- FirecrawlSearchTool: query validation, result parsing, limit clamping
- Registry wiring: tools registered when egress + key set, absent otherwise
- Security: tool names in RESTRICTED_TOOLS, ALL_KNOWN_TOOLS
- Doctor check: check_firecrawl() outcomes
- SandboxConfig: firecrawl_api_key field and env override
"""

import importlib
import os
import sys
import types
from unittest.mock import patch, MagicMock

import pytest

from agents.tools.registry import (
    ToolResult,
    ToolCategory,
    FirecrawlScrapeTool,
    FirecrawlCrawlTool,
    FirecrawlSearchTool,
    create_default_tool_registry,
)


# ── Helpers for mocking the firecrawl package ──────────────────────────


@pytest.fixture()
def mock_firecrawl_module():
    """Install a fake ``firecrawl`` package in sys.modules so that
    ``from firecrawl import FirecrawlApp`` succeeds inside tool code.

    Yields the mock FirecrawlApp *class* — tests configure its
    return_value (the app instance) before calling tool.execute().
    """
    mock_cls = MagicMock(name="FirecrawlApp")
    mod = types.ModuleType("firecrawl")
    mod.FirecrawlApp = mock_cls  # type: ignore[attr-defined]

    saved = sys.modules.get("firecrawl")
    sys.modules["firecrawl"] = mod
    try:
        yield mock_cls
    finally:
        if saved is None:
            sys.modules.pop("firecrawl", None)
        else:
            sys.modules["firecrawl"] = saved


# ============================================================
# FirecrawlScrapeTool
# ============================================================


class TestFirecrawlScrapeTool:
    """Tests for the web_scrape tool."""

    def test_name_and_category(self):
        tool = FirecrawlScrapeTool(api_key="fc-test")
        assert tool.name == "web_scrape"
        assert tool.category == ToolCategory.WEB_API

    def test_schema_has_required_url(self):
        tool = FirecrawlScrapeTool(api_key="fc-test")
        schema = tool._get_parameters_schema()
        assert "url" in schema["properties"]
        assert "url" in schema["required"]

    def test_schema_has_optional_fields(self):
        tool = FirecrawlScrapeTool(api_key="fc-test")
        schema = tool._get_parameters_schema()
        assert "formats" in schema["properties"]
        assert "only_main_content" in schema["properties"]
        assert "timeout" in schema["properties"]

    def test_empty_url_rejected(self):
        tool = FirecrawlScrapeTool(api_key="fc-test")
        result = tool.execute(url="")
        assert not result.success
        assert "No URL" in result.error

    def test_whitespace_url_rejected(self):
        tool = FirecrawlScrapeTool(api_key="fc-test")
        result = tool.execute(url="   ")
        assert not result.success
        assert "No URL" in result.error

    def test_non_http_url_rejected(self):
        tool = FirecrawlScrapeTool(api_key="fc-test")
        result = tool.execute(url="ftp://example.com")
        assert not result.success
        assert "http" in result.error.lower()

    def test_missing_api_key(self):
        tool = FirecrawlScrapeTool(api_key="")
        result = tool.execute(url="https://example.com")
        assert not result.success
        assert "FIRECRAWL_API_KEY" in result.error

    def test_api_key_from_env(self):
        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-from-env"}):
            tool = FirecrawlScrapeTool()
            assert tool._api_key == "fc-from-env"

    def test_explicit_key_overrides_env(self):
        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-env"}):
            tool = FirecrawlScrapeTool(api_key="fc-explicit")
            assert tool._api_key == "fc-explicit"

    def test_import_error_handled(self):
        tool = FirecrawlScrapeTool(api_key="fc-test")
        with patch.dict("sys.modules", {"firecrawl": None}):
            result = tool.execute(url="https://example.com")
            assert not result.success
            assert "not installed" in result.error

    def test_scrape_success_markdown(self, mock_firecrawl_module):
        tool = FirecrawlScrapeTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.scrape_url.return_value = {
            "markdown": "# Hello World\n\nSome content here.",
            "metadata": {
                "title": "Hello World",
                "description": "A test page",
                "language": "en",
            },
            "links": ["https://example.com/link1"],
        }
        mock_firecrawl_module.return_value = mock_app

        result = tool.execute(url="https://example.com")

        assert result.success
        assert "Hello World" in result.output
        assert result.metadata["title"] == "Hello World"
        assert result.metadata["link_count"] == 1

    def test_scrape_success_html_fallback(self, mock_firecrawl_module):
        tool = FirecrawlScrapeTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.scrape_url.return_value = {
            "html": "<h1>Hello</h1>",
            "metadata": {},
        }
        mock_firecrawl_module.return_value = mock_app

        result = tool.execute(url="https://example.com", formats=["html"])

        assert result.success
        assert "<h1>Hello</h1>" in result.output

    def test_scrape_passes_params(self, mock_firecrawl_module):
        tool = FirecrawlScrapeTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.scrape_url.return_value = {"markdown": "content"}
        mock_firecrawl_module.return_value = mock_app

        tool.execute(
            url="https://example.com",
            formats=["markdown", "html"],
            only_main_content=False,
            timeout=60,
        )

        call_args = mock_app.scrape_url.call_args
        assert call_args[0][0] == "https://example.com"
        params = call_args[1]["params"]
        assert params["formats"] == ["markdown", "html"]
        assert params["onlyMainContent"] is False
        assert params["timeout"] == 60000  # converted to ms

    def test_scrape_api_error(self, mock_firecrawl_module):
        tool = FirecrawlScrapeTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.scrape_url.side_effect = RuntimeError("rate limited")
        mock_firecrawl_module.return_value = mock_app

        result = tool.execute(url="https://example.com")

        assert not result.success
        assert "rate limited" in result.error

    def test_scrape_non_dict_response(self, mock_firecrawl_module):
        tool = FirecrawlScrapeTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.scrape_url.return_value = "raw string response"
        mock_firecrawl_module.return_value = mock_app

        result = tool.execute(url="https://example.com")

        assert result.success
        assert "raw string response" in result.output

    def test_scrape_empty_metadata(self, mock_firecrawl_module):
        """No metadata key in response — should not crash."""
        tool = FirecrawlScrapeTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.scrape_url.return_value = {"markdown": "content"}
        mock_firecrawl_module.return_value = mock_app

        result = tool.execute(url="https://example.com")

        assert result.success
        assert "title" not in result.metadata

    def test_default_formats(self, mock_firecrawl_module):
        """Default format should be markdown."""
        tool = FirecrawlScrapeTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.scrape_url.return_value = {"markdown": "ok"}
        mock_firecrawl_module.return_value = mock_app

        tool.execute(url="https://example.com")

        params = mock_app.scrape_url.call_args[1]["params"]
        assert params["formats"] == ["markdown"]

    def test_get_schema_full(self):
        tool = FirecrawlScrapeTool(api_key="fc-test")
        schema = tool.get_schema()
        assert schema["name"] == "web_scrape"
        assert "description" in schema
        assert "parameters" in schema


# ============================================================
# FirecrawlCrawlTool
# ============================================================


class TestFirecrawlCrawlTool:
    """Tests for the web_crawl tool."""

    def test_name_and_category(self):
        tool = FirecrawlCrawlTool(api_key="fc-test")
        assert tool.name == "web_crawl"
        assert tool.category == ToolCategory.WEB_API

    def test_schema_required_fields(self):
        tool = FirecrawlCrawlTool(api_key="fc-test")
        schema = tool._get_parameters_schema()
        assert "url" in schema["required"]
        assert "limit" in schema["properties"]
        assert "max_depth" in schema["properties"]
        assert "include_patterns" in schema["properties"]
        assert "exclude_patterns" in schema["properties"]

    def test_empty_url_rejected(self):
        tool = FirecrawlCrawlTool(api_key="fc-test")
        result = tool.execute(url="")
        assert not result.success

    def test_non_http_url_rejected(self):
        tool = FirecrawlCrawlTool(api_key="fc-test")
        result = tool.execute(url="file:///etc/passwd")
        assert not result.success

    def test_missing_api_key(self):
        tool = FirecrawlCrawlTool(api_key="")
        result = tool.execute(url="https://docs.example.com")
        assert not result.success
        assert "FIRECRAWL_API_KEY" in result.error

    def test_limit_clamped_to_max(self, mock_firecrawl_module):
        """limit > 50 should be clamped to 50."""
        tool = FirecrawlCrawlTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.crawl_url.return_value = {"data": []}
        mock_firecrawl_module.return_value = mock_app

        tool.execute(url="https://example.com", limit=200)

        params = mock_app.crawl_url.call_args[1]["params"]
        assert params["limit"] == 50

    def test_limit_clamped_to_min(self, mock_firecrawl_module):
        """limit < 1 should be clamped to 1."""
        tool = FirecrawlCrawlTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.crawl_url.return_value = {"data": []}
        mock_firecrawl_module.return_value = mock_app

        tool.execute(url="https://example.com", limit=-5)

        params = mock_app.crawl_url.call_args[1]["params"]
        assert params["limit"] == 1

    def test_max_depth_clamped(self, mock_firecrawl_module):
        """max_depth > 5 should be clamped to 5."""
        tool = FirecrawlCrawlTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.crawl_url.return_value = {"data": []}
        mock_firecrawl_module.return_value = mock_app

        tool.execute(url="https://example.com", max_depth=20)

        params = mock_app.crawl_url.call_args[1]["params"]
        assert params["maxDepth"] == 5

    def test_crawl_multiple_pages(self, mock_firecrawl_module):
        tool = FirecrawlCrawlTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.crawl_url.return_value = {
            "data": [
                {
                    "markdown": "# Page 1\nContent 1",
                    "metadata": {"sourceURL": "https://example.com/p1", "title": "Page 1"},
                },
                {
                    "markdown": "# Page 2\nContent 2",
                    "metadata": {"sourceURL": "https://example.com/p2", "title": "Page 2"},
                },
            ]
        }
        mock_firecrawl_module.return_value = mock_app

        result = tool.execute(url="https://example.com")

        assert result.success
        assert "Page 1" in result.output
        assert "Page 2" in result.output
        assert "---" in result.output  # page separator
        assert result.metadata["pages_crawled"] == 2

    def test_crawl_empty_result(self, mock_firecrawl_module):
        tool = FirecrawlCrawlTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.crawl_url.return_value = {"data": []}
        mock_firecrawl_module.return_value = mock_app

        result = tool.execute(url="https://example.com")

        assert result.success
        assert "no pages" in result.output.lower()
        assert result.metadata["pages_found"] == 0

    def test_crawl_with_include_exclude(self, mock_firecrawl_module):
        tool = FirecrawlCrawlTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.crawl_url.return_value = {"data": []}
        mock_firecrawl_module.return_value = mock_app

        tool.execute(
            url="https://example.com",
            include_patterns=["/docs/*"],
            exclude_patterns=["/blog/*"],
        )

        params = mock_app.crawl_url.call_args[1]["params"]
        assert params["includePaths"] == ["/docs/*"]
        assert params["excludePaths"] == ["/blog/*"]

    def test_crawl_no_patterns_omitted(self, mock_firecrawl_module):
        """When no patterns, includePaths/excludePaths should not be in params."""
        tool = FirecrawlCrawlTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.crawl_url.return_value = {"data": []}
        mock_firecrawl_module.return_value = mock_app

        tool.execute(url="https://example.com")

        params = mock_app.crawl_url.call_args[1]["params"]
        assert "includePaths" not in params
        assert "excludePaths" not in params

    def test_crawl_api_error(self, mock_firecrawl_module):
        tool = FirecrawlCrawlTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.crawl_url.side_effect = ConnectionError("timeout")
        mock_firecrawl_module.return_value = mock_app

        result = tool.execute(url="https://example.com")

        assert not result.success
        assert "timeout" in result.error

    def test_crawl_skips_empty_content_pages(self, mock_firecrawl_module):
        tool = FirecrawlCrawlTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.crawl_url.return_value = {
            "data": [
                {"markdown": "Good content", "metadata": {"sourceURL": "https://example.com/ok", "title": "OK"}},
                {"markdown": "", "metadata": {"sourceURL": "https://example.com/empty", "title": "Empty"}},
            ]
        }
        mock_firecrawl_module.return_value = mock_app

        result = tool.execute(url="https://example.com")

        assert result.success
        assert result.metadata["pages_crawled"] == 1
        assert result.metadata["pages_total"] == 2

    def test_crawl_list_response(self, mock_firecrawl_module):
        """Handle response as a plain list (not wrapped in {data: []})."""
        tool = FirecrawlCrawlTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.crawl_url.return_value = [
            {"markdown": "Content", "metadata": {"sourceURL": "https://example.com", "title": "T"}},
        ]
        mock_firecrawl_module.return_value = mock_app

        result = tool.execute(url="https://example.com")

        assert result.success
        assert result.metadata["pages_crawled"] == 1

    def test_import_error_handled(self):
        tool = FirecrawlCrawlTool(api_key="fc-test")
        with patch.dict("sys.modules", {"firecrawl": None}):
            result = tool.execute(url="https://example.com")
            assert not result.success
            assert "not installed" in result.error


# ============================================================
# FirecrawlSearchTool
# ============================================================


class TestFirecrawlSearchTool:
    """Tests for the web_search tool."""

    def test_name_and_category(self):
        tool = FirecrawlSearchTool(api_key="fc-test")
        assert tool.name == "web_search"
        assert tool.category == ToolCategory.WEB_API

    def test_schema_required_query(self):
        tool = FirecrawlSearchTool(api_key="fc-test")
        schema = tool._get_parameters_schema()
        assert "query" in schema["required"]
        assert "limit" in schema["properties"]

    def test_empty_query_rejected(self):
        tool = FirecrawlSearchTool(api_key="fc-test")
        result = tool.execute(query="")
        assert not result.success
        assert "No query" in result.error

    def test_missing_api_key(self):
        tool = FirecrawlSearchTool(api_key="")
        result = tool.execute(query="test query")
        assert not result.success
        assert "FIRECRAWL_API_KEY" in result.error

    def test_limit_clamped(self, mock_firecrawl_module):
        tool = FirecrawlSearchTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.search.return_value = {"data": []}
        mock_firecrawl_module.return_value = mock_app

        tool.execute(query="test", limit=50)

        params = mock_app.search.call_args[1]["params"]
        assert params["limit"] == 10

    def test_search_with_results(self, mock_firecrawl_module):
        tool = FirecrawlSearchTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.search.return_value = {
            "data": [
                {
                    "markdown": "Result 1 content",
                    "metadata": {"title": "Result 1", "sourceURL": "https://example.com/1"},
                },
                {
                    "markdown": "Result 2 content",
                    "metadata": {"title": "Result 2", "sourceURL": "https://example.com/2"},
                },
            ]
        }
        mock_firecrawl_module.return_value = mock_app

        result = tool.execute(query="python web scraping")

        assert result.success
        assert "Result 1" in result.output
        assert "Result 2" in result.output
        assert result.metadata["results"] == 2

    def test_search_no_results(self, mock_firecrawl_module):
        tool = FirecrawlSearchTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.search.return_value = {"data": []}
        mock_firecrawl_module.return_value = mock_app

        result = tool.execute(query="zzzzzzz nonexistent")

        assert result.success
        assert "No results" in result.output

    def test_search_api_error(self, mock_firecrawl_module):
        tool = FirecrawlSearchTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.search.side_effect = RuntimeError("quota exceeded")
        mock_firecrawl_module.return_value = mock_app

        result = tool.execute(query="test")

        assert not result.success
        assert "quota exceeded" in result.error

    def test_search_list_response(self, mock_firecrawl_module):
        """Handle response as a plain list."""
        tool = FirecrawlSearchTool(api_key="fc-test")
        mock_app = MagicMock()
        mock_app.search.return_value = [
            {"markdown": "Content", "metadata": {"title": "T", "sourceURL": "https://example.com"}},
        ]
        mock_firecrawl_module.return_value = mock_app

        result = tool.execute(query="test")

        assert result.success
        assert result.metadata["results"] == 1

    def test_import_error_handled(self):
        tool = FirecrawlSearchTool(api_key="fc-test")
        with patch.dict("sys.modules", {"firecrawl": None}):
            result = tool.execute(query="test")
            assert not result.success
            assert "not installed" in result.error


# ============================================================
# Registry wiring
# ============================================================


class TestFirecrawlRegistryWiring:
    """Test that Firecrawl tools appear in the registry under correct conditions."""

    def test_no_firecrawl_when_key_missing(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FIRECRAWL_API_KEY", None)
            registry = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=False)
            assert registry.get("web_scrape") is None
            assert registry.get("web_crawl") is None
            assert registry.get("web_search") is None

    def test_firecrawl_registered_when_key_set_no_egress(self):
        """Firecrawl tools register even without network_egress."""
        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test123"}):
            registry = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=False)
            assert registry.get("web_scrape") is not None
            assert registry.get("web_crawl") is not None
            assert registry.get("web_search") is not None

    def test_firecrawl_registered_with_egress(self):
        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test123"}):
            registry = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=True)
            assert registry.get("web_scrape") is not None
            assert registry.get("web_crawl") is not None
            assert registry.get("web_search") is not None

    def test_web_fetch_still_registered_with_firecrawl(self):
        """web_fetch should coexist with Firecrawl tools."""
        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test123"}):
            registry = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=True)
            assert registry.get("web_fetch") is not None
            assert registry.get("web_scrape") is not None

    def test_all_tools_in_schemas(self):
        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test123"}):
            registry = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=False)
            names = [s["name"] for s in registry.get_all_schemas()]
            assert "web_scrape" in names
            assert "web_crawl" in names
            assert "web_search" in names

    def test_firecrawl_tools_executable_via_registry(self):
        """Registry.execute_tool should route to Firecrawl tools."""
        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test123"}):
            registry = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=False)

            # Execute with empty URL — should fail gracefully
            result = registry.execute_tool("web_scrape", url="")
            assert not result.success

    def test_tool_count_with_firecrawl(self):
        """Registry should have 3 more tools when Firecrawl is enabled."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FIRECRAWL_API_KEY", None)
            reg_without = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=False)
            count_without = len(reg_without.list_tools())

        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test123"}):
            reg_with = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=False)
            count_with = len(reg_with.list_tools())

        assert count_with == count_without + 3


# ============================================================
# Security integration
# ============================================================


class TestFirecrawlSecurity:
    """Verify Firecrawl tool names in skill security sets."""

    def test_tools_in_default_allowed(self):
        """Firecrawl tools should be allowed by default for all skills."""
        from agents.skill_security import DEFAULT_ALLOWED_TOOLS
        assert "web_scrape" in DEFAULT_ALLOWED_TOOLS
        assert "web_crawl" in DEFAULT_ALLOWED_TOOLS
        assert "web_search" in DEFAULT_ALLOWED_TOOLS

    def test_tools_not_in_restricted(self):
        from agents.skill_security import RESTRICTED_TOOLS
        assert "web_scrape" not in RESTRICTED_TOOLS
        assert "web_crawl" not in RESTRICTED_TOOLS
        assert "web_search" not in RESTRICTED_TOOLS

    def test_tools_in_all_known(self):
        from agents.skill_security import ALL_KNOWN_TOOLS
        assert "web_scrape" in ALL_KNOWN_TOOLS
        assert "web_crawl" in ALL_KNOWN_TOOLS
        assert "web_search" in ALL_KNOWN_TOOLS

    def test_skills_get_firecrawl_by_default(self):
        """Skills without allowed-tools frontmatter get Firecrawl tools."""
        from agents.skill_security import SkillSecurity
        security = SkillSecurity()
        content = "# Just a skill with no frontmatter"
        allowed = security.parse_allowed_tools(content)
        assert "web_scrape" in allowed
        assert "web_crawl" in allowed
        assert "web_search" in allowed


# ============================================================
# Doctor check
# ============================================================


class TestFirecrawlDoctorCheck:
    """Test the check_firecrawl() diagnostic."""

    def test_no_api_key(self):
        from agents.doctor import check_firecrawl
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FIRECRAWL_API_KEY", None)
            result = check_firecrawl()
            assert result.status == "warn"
            assert "not set" in result.summary.lower()

    def test_package_not_installed(self):
        from agents.doctor import check_firecrawl
        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test"}):
            with patch.dict("sys.modules", {"firecrawl": None}):
                result = check_firecrawl()
                assert result.status == "warn"
                assert "not installed" in result.summary.lower()

    def test_api_success(self, mock_firecrawl_module):
        from agents.doctor import check_firecrawl
        mock_app = MagicMock()
        mock_app.scrape_url.return_value = {"markdown": "Example Domain"}
        mock_firecrawl_module.return_value = mock_app

        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test"}):
            result = check_firecrawl()
            assert result.status == "ok"

    def test_api_failure(self, mock_firecrawl_module):
        from agents.doctor import check_firecrawl
        mock_app = MagicMock()
        mock_app.scrape_url.side_effect = RuntimeError("401 Unauthorized")
        mock_firecrawl_module.return_value = mock_app

        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-bad-key"}):
            result = check_firecrawl()
            assert result.status == "fail"

    def test_api_empty_response(self, mock_firecrawl_module):
        from agents.doctor import check_firecrawl
        mock_app = MagicMock()
        mock_app.scrape_url.return_value = {}
        mock_firecrawl_module.return_value = mock_app

        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test"}):
            result = check_firecrawl()
            assert result.status == "warn"


# ============================================================
# SandboxConfig
# ============================================================


class TestSandboxConfigFirecrawl:
    """Test firecrawl_api_key field in SandboxConfig."""

    def test_default_empty(self):
        from agents.sandbox.config import SandboxConfig
        cfg = SandboxConfig()
        assert cfg.firecrawl_api_key == ""

    def test_env_override(self):
        from agents.sandbox.config import SandboxConfig
        cfg = SandboxConfig()
        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-from-env"}):
            cfg.apply_env_overrides()
            assert cfg.firecrawl_api_key == "fc-from-env"
