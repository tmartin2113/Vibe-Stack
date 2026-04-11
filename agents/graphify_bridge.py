"""Bridge between Vibe Stack agents and Graphify structural knowledge graphs.

All functions are wrapped in try/except — Graphify is additive and must
never block the existing workflow. If graphify is not installed or the
data volume is unavailable, every function silently returns.
"""

import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

_GRAPHIFY_DATA_PATH = os.environ.get("GRAPHIFY_DATA_PATH", "")

# Maximum chars to inject from GRAPH_REPORT.md (~500-800 tokens)
_MAX_REPORT_CHARS = 2000

# Task types that produce code changes and warrant a graph rebuild
_CODE_TASK_TYPES = frozenset({
    "api-generation", "code-generation", "bug-fix", "refactoring",
    "feature", "backend", "frontend", "fullstack", "devops",
    "security", "testing", "general",
})

# Maximum age (seconds) before a graph is considered stale
_MAX_GRAPH_AGE = 86400  # 24 hours


def _repo_slug(state: Dict[str, Any]) -> str:
    """Derive a repo slug from state for graph directory lookup.

    Checks workspace_path, then falls back to 'vibe-stack'.
    """
    workspace = (state.get("workspace_path") or "").strip()
    if workspace:
        return os.path.basename(workspace).lower().replace(" ", "-")
    return "vibe-stack"


# ── Inject ───────────────────────────────────────────────────────────────────

def graphify_inject(state: Dict[str, Any]) -> str:
    """Inject GRAPH_REPORT.md content into specialist context.

    Called from inject_memory() in graph_nodes.py. Returns formatted text
    to append to memory_context, or empty string if unavailable.
    """
    if not _GRAPHIFY_DATA_PATH:
        return ""

    user_request = state.get("user_request", "")
    if not user_request:
        return ""

    slug = _repo_slug(state)
    report_path = os.path.join(_GRAPHIFY_DATA_PATH, slug, "GRAPH_REPORT.md")

    if not os.path.exists(report_path):
        return ""

    try:
        with open(report_path, "r") as f:
            content = f.read(_MAX_REPORT_CHARS)

        if not content.strip():
            return ""

        return (
            "\n\n## Codebase Structure (Graphify)\n\n"
            + content
        )
    except Exception as e:
        logger.debug("graphify_inject: read failed: %s", e)
        return ""


# ── Ensure ───────────────────────────────────────────────────────────────────

def graphify_ensure(repo_path: str, graph_dir: str) -> None:
    """Ensure a graph exists for the given repo. Build if missing or stale.

    AST-only extraction (tree-sitter), no LLM calls. Fast (~30-60s for
    a typical repo). Called on first inject or during container init.
    """
    if not _GRAPHIFY_DATA_PATH:
        return

    graph_json = os.path.join(graph_dir, "graph.json")

    # Skip if graph exists and is fresh
    if os.path.exists(graph_json):
        import time
        age = time.time() - os.path.getmtime(graph_json)
        if age < _MAX_GRAPH_AGE:
            logger.debug("graphify_ensure: graph is fresh (%.0fs old)", age)
            return

    try:
        import graphify
    except ImportError:
        logger.debug("graphify_ensure: graphify not installed")
        return

    if not os.path.isdir(repo_path):
        logger.debug("graphify_ensure: repo_path %s not found", repo_path)
        return

    try:
        os.makedirs(graph_dir, exist_ok=True)

        # AST-only extraction — no LLM, just tree-sitter
        extraction = graphify.extract(repo_path)
        graph = graphify.build_from_json(extraction)
        graph = graphify.cluster(graph)

        # Export graph.json and report
        graphify.to_json(graph, os.path.join(graph_dir, "graph.json"))

        # Generate GRAPH_REPORT.md
        god = graphify.god_nodes(graph, top_n=10)
        stats = {
            "nodes": graph.number_of_nodes(),
            "edges": graph.number_of_edges(),
            "god_nodes": [{"label": n, "degree": d} for n, d in god],
        }
        report_lines = [
            "# Codebase Knowledge Graph\n",
            f"**Nodes:** {stats['nodes']} | **Edges:** {stats['edges']}\n",
            "\n## God Nodes (most connected)\n",
        ]
        for g in stats["god_nodes"]:
            report_lines.append(f"- {g['label']} (degree {g['degree']})\n")

        with open(os.path.join(graph_dir, "GRAPH_REPORT.md"), "w") as f:
            f.writelines(report_lines)

        logger.info(
            "graphify_ensure: built graph for %s (%d nodes, %d edges)",
            repo_path, stats["nodes"], stats["edges"],
        )
    except Exception as e:
        logger.debug("graphify_ensure: build failed: %s", e)


# ── Rebuild ──────────────────────────────────────────────────────────────────

def graphify_rebuild(state: Dict[str, Any]) -> None:
    """Incrementally rebuild the knowledge graph after a code-producing run.

    Called from persist_memory_wrapper() in graph_nodes.py. Checks if the
    run's task type is code-related, then triggers an incremental rebuild.
    AST-only — no LLM calls, fast.
    """
    if not _GRAPHIFY_DATA_PATH:
        return

    output = state.get("final_output") or state.get("specialist_output") or ""
    if not output:
        return

    task_type = (
        state.get("routed_task_type")
        or state.get("task_type")
        or "general"
    ).lower().replace(" ", "-")

    if task_type not in _CODE_TASK_TYPES:
        return

    slug = _repo_slug(state)
    graph_dir = os.path.join(_GRAPHIFY_DATA_PATH, slug)
    graph_json = os.path.join(graph_dir, "graph.json")

    # Only rebuild if a graph already exists (don't auto-create on every run)
    if not os.path.exists(graph_json):
        return

    try:
        import graphify
    except ImportError:
        logger.debug("graphify_rebuild: graphify not installed")
        return

    try:
        # Determine repo source path
        workspace = (state.get("workspace_path") or "").strip()
        if not workspace or not os.path.isdir(workspace):
            return

        # Incremental: re-extract, rebuild, re-cluster
        extraction = graphify.extract(workspace)
        graph = graphify.build_from_json(extraction)
        graph = graphify.cluster(graph)
        graphify.to_json(graph, graph_json)

        # Regenerate report
        god = graphify.god_nodes(graph, top_n=10)
        report_lines = [
            "# Codebase Knowledge Graph\n",
            f"**Nodes:** {graph.number_of_nodes()} | **Edges:** {graph.number_of_edges()}\n",
            "\n## God Nodes (most connected)\n",
        ]
        for label, degree in god:
            report_lines.append(f"- {label} (degree {degree})\n")

        with open(os.path.join(graph_dir, "GRAPH_REPORT.md"), "w") as f:
            f.writelines(report_lines)

        logger.info(
            "graphify_rebuild: updated %s (%d nodes, %d edges)",
            slug, graph.number_of_nodes(), graph.number_of_edges(),
        )
    except Exception as e:
        logger.debug("graphify_rebuild: failed: %s", e)
