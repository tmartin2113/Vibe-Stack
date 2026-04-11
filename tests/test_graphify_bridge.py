"""Tests for the Graphify bridge module.

All graphify operations are additive — the bridge must never raise,
and must degrade gracefully when graphify is not installed.
"""

import os
import tempfile
from unittest.mock import patch

import pytest

from agents.graphify_bridge import (
    graphify_ensure,
    graphify_inject,
    graphify_rebuild,
    _repo_slug,
)


class TestRepoSlug:
    def test_from_workspace_path(self):
        assert _repo_slug({"workspace_path": "/home/user/Projects/MyApp"}) == "myapp"

    def test_fallback_to_vibe_stack(self):
        assert _repo_slug({}) == "vibe-stack"

    def test_empty_workspace(self):
        assert _repo_slug({"workspace_path": ""}) == "vibe-stack"


class TestGraphifyInject:
    def test_returns_report_when_exists(self):
        """graphify_inject returns formatted report text when GRAPH_REPORT.md exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = os.path.join(tmpdir, "vibe-stack")
            os.makedirs(repo_dir)
            report_path = os.path.join(repo_dir, "GRAPH_REPORT.md")
            with open(report_path, "w") as f:
                f.write("# Graph Report\n\n## God Nodes\n- main.py (degree 42)\n")

            with patch("agents.graphify_bridge._GRAPHIFY_DATA_PATH", tmpdir):
                result = graphify_inject({"user_request": "fix the auth bug"})

            assert "## Codebase Structure" in result
            assert "God Nodes" in result
            assert "main.py" in result

    def test_graceful_when_no_data_path(self):
        """graphify_inject returns empty string when GRAPHIFY_DATA_PATH is unset."""
        with patch("agents.graphify_bridge._GRAPHIFY_DATA_PATH", ""):
            result = graphify_inject({"user_request": "test"})
        assert result == ""

    def test_graceful_on_empty_state(self):
        """graphify_inject with empty state returns empty string."""
        result = graphify_inject({})
        assert result == ""

    def test_graceful_when_report_missing(self):
        """graphify_inject returns empty string when GRAPH_REPORT.md doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("agents.graphify_bridge._GRAPHIFY_DATA_PATH", tmpdir):
                result = graphify_inject({"user_request": "test"})
            assert result == ""

    def test_truncates_large_reports(self):
        """Reports larger than 2000 chars are truncated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = os.path.join(tmpdir, "vibe-stack")
            os.makedirs(repo_dir)
            report_path = os.path.join(repo_dir, "GRAPH_REPORT.md")
            with open(report_path, "w") as f:
                f.write("x" * 5000)

            with patch("agents.graphify_bridge._GRAPHIFY_DATA_PATH", tmpdir):
                result = graphify_inject({"user_request": "test"})

            # Header + truncated content should be under 2500 chars
            assert len(result) < 2500


class TestGraphifyEnsure:
    def test_graceful_when_graphify_missing(self):
        """graphify_ensure must not raise when graphify is not installed."""
        with patch("agents.graphify_bridge._GRAPHIFY_DATA_PATH", "/tmp/test-graphify"):
            graphify_ensure("/some/repo", "/tmp/test-graphify/repo")

    def test_skips_when_no_data_path(self):
        """graphify_ensure does nothing when GRAPHIFY_DATA_PATH is empty."""
        with patch("agents.graphify_bridge._GRAPHIFY_DATA_PATH", ""):
            graphify_ensure("/some/repo", "/tmp/out")

    def test_skips_when_graph_is_fresh(self):
        """graphify_ensure skips rebuild when graph.json is recent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            graph_path = os.path.join(tmpdir, "graph.json")
            with open(graph_path, "w") as f:
                f.write('{"nodes": [], "links": []}')

            with patch("agents.graphify_bridge._GRAPHIFY_DATA_PATH", "/tmp"):
                graphify_ensure("/some/repo", tmpdir)


class TestGraphifyRebuild:
    def test_graceful_when_graphify_missing(self):
        """graphify_rebuild must not raise when graphify is not installed."""
        result = graphify_rebuild({})
        assert result is None

    def test_graceful_on_empty_state(self):
        """graphify_rebuild with no output does nothing."""
        result = graphify_rebuild({"agent_id": "test"})
        assert result is None

    def test_skips_when_no_data_path(self):
        """graphify_rebuild does nothing when GRAPHIFY_DATA_PATH is empty."""
        with patch("agents.graphify_bridge._GRAPHIFY_DATA_PATH", ""):
            result = graphify_rebuild({
                "final_output": "some code output",
                "routed_task_type": "api-generation",
            })
            assert result is None

    def test_skips_non_code_task_types(self):
        """graphify_rebuild skips non-code task types."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("agents.graphify_bridge._GRAPHIFY_DATA_PATH", tmpdir):
                result = graphify_rebuild({
                    "final_output": "wrote documentation",
                    "routed_task_type": "documentation",
                })
                assert result is None


from agents.tools.graphify_tool import GraphifyRebuildTool


class TestGraphifyRebuildTool:
    def test_instantiates(self):
        """Tool can be created."""
        tool = GraphifyRebuildTool()
        assert tool.name == "graphify_rebuild"
        assert "knowledge graph" in tool.description.lower()

    def test_graceful_when_graphify_missing(self):
        """Tool returns helpful message when graphify not installed."""
        tool = GraphifyRebuildTool()
        with patch("agents.graphify_bridge._GRAPHIFY_DATA_PATH", ""):
            result = tool.execute(repo_path="/some/repo")
        assert result.success is False
        assert "not configured" in result.output.lower() or "unavailable" in result.output.lower()
