"""Graphify rebuild tool — allows agents to explicitly rebuild a codebase knowledge graph."""

import logging
import os
from typing import Any, Dict

from .base import Tool, ToolResult, ToolCategory

logger = logging.getLogger(__name__)


class GraphifyRebuildTool(Tool):
    """Rebuild the structural knowledge graph for a repository.

    Uses tree-sitter AST parsing (no LLM) to extract code structure,
    build a NetworkX graph with community clustering, and generate
    a GRAPH_REPORT.md with god nodes and structural analysis.
    """

    name = "graphify_rebuild"
    description = (
        "Rebuild the codebase knowledge graph for a repository. "
        "Extracts code structure via AST parsing, identifies god nodes, "
        "community clusters, and structural relationships. "
        "Use after major code changes or to graph a new repository."
    )
    category = ToolCategory.SPECIALIZED

    def __init__(self):
        super().__init__(
            name=self.name,
            description=self.description,
            category=self.category,
        )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Absolute path to the repository to graph.",
                },
                "full": {
                    "type": "boolean",
                    "description": (
                        "If true, delete existing graph and rebuild from scratch. "
                        "Default: false (incremental)."
                    ),
                },
            },
            "required": ["repo_path"],
        }

    def execute(self, **kwargs) -> ToolResult:
        repo_path = kwargs.get("repo_path", "")
        full = kwargs.get("full", False)

        from ..graphify_bridge import _GRAPHIFY_DATA_PATH, graphify_ensure

        if not _GRAPHIFY_DATA_PATH:
            return ToolResult(
                success=False,
                output="Graphify is not configured (GRAPHIFY_DATA_PATH unavailable).",
            )

        if not repo_path:
            return ToolResult(
                success=False,
                output="repo_path is required.",
            )

        if not os.path.isdir(repo_path):
            return ToolResult(
                success=False,
                output=f"Repository path not found: {repo_path}",
            )

        slug = os.path.basename(repo_path).lower().replace(" ", "-")
        graph_dir = os.path.join(_GRAPHIFY_DATA_PATH, slug)

        # If full rebuild requested, remove existing graph first
        if full:
            graph_json = os.path.join(graph_dir, "graph.json")
            if os.path.exists(graph_json):
                os.remove(graph_json)

        try:
            graphify_ensure(repo_path, graph_dir)

            graph_json = os.path.join(graph_dir, "graph.json")
            if os.path.exists(graph_json):
                report_path = os.path.join(graph_dir, "GRAPH_REPORT.md")
                report = ""
                if os.path.exists(report_path):
                    with open(report_path, "r") as f:
                        report = f.read(1000)
                return ToolResult(
                    success=True,
                    output=f"Graph rebuilt for {slug}.\n\n{report}",
                )
            else:
                return ToolResult(
                    success=False,
                    output="Graph build completed but no graph.json was produced. Is graphify installed?",
                )
        except Exception as e:
            return ToolResult(
                success=False,
                output=f"Graph rebuild failed: {e}",
            )
