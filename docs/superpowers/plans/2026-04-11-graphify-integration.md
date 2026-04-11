# Graphify Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Graphify as shared structural awareness infrastructure for all Vibe Stack agents — passive context injection + active MCP queries + auto-rebuild on code changes.

**Architecture:** Bridge pattern mirroring MemPalace integration. A `graphify_bridge.py` module provides `graphify_inject()`, `graphify_rebuild()`, and `graphify_ensure()`, wired into the existing `graph_nodes.py` hook points. A Graphify MCP server exposes 7 structural query tools to DeerFlow agents. A `rebuild_graph` agent tool allows explicit rebuilds.

**Tech Stack:** graphifyy (PyPI), tree-sitter (AST parsing), NetworkX (graph engine), MCP stdio protocol

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `agents/graphify_bridge.py` | Bridge module: inject, rebuild, ensure functions |
| Create | `agents/tools/graphify_tool.py` | Agent tool: explicit graph rebuild |
| Create | `tests/test_graphify_bridge.py` | Tests for bridge + tool |
| Modify | `agents/graph_nodes.py` | Wire inject + rebuild hooks |
| Modify | `agents/tools/registry.py` | Register graphify_rebuild tool |
| Modify | `deerflow/extensions_config.json` | Add Graphify MCP server entry |
| Modify | `docker-compose.yml` | Volume, mounts, env vars, pip install |
| Modify | `pyproject.toml` | Add graphifyy dependency |

---

### Task 1: Bridge Module — `graphify_inject()`

**Files:**
- Create: `agents/graphify_bridge.py`
- Create: `tests/test_graphify_bridge.py`

- [ ] **Step 1: Write failing tests for graphify_inject**

Create `tests/test_graphify_bridge.py`:

```python
"""Tests for the Graphify bridge module.

All graphify operations are additive — the bridge must never raise,
and must degrade gracefully when graphify is not installed.
"""

import os
import tempfile
from unittest.mock import patch

import pytest

from agents.graphify_bridge import graphify_inject


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_graphify_bridge.py -v`
Expected: FAIL with `ModuleNotFoundError` or `ImportError` (module doesn't exist yet)

- [ ] **Step 3: Implement graphify_bridge.py with graphify_inject**

Create `agents/graphify_bridge.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_graphify_bridge.py::TestGraphifyInject -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add agents/graphify_bridge.py tests/test_graphify_bridge.py
git commit -m "feat: add graphify_bridge with inject function"
```

---

### Task 2: Bridge Module — `graphify_ensure()` and `graphify_rebuild()`

**Files:**
- Modify: `agents/graphify_bridge.py`
- Modify: `tests/test_graphify_bridge.py`

- [ ] **Step 1: Write failing tests for graphify_ensure and graphify_rebuild**

Append to `tests/test_graphify_bridge.py`:

```python
from unittest.mock import MagicMock, patch
import time

from agents.graphify_bridge import graphify_ensure, graphify_rebuild


class TestGraphifyEnsure:
    def test_graceful_when_graphify_missing(self):
        """graphify_ensure must not raise when graphify is not installed."""
        with patch("agents.graphify_bridge._GRAPHIFY_DATA_PATH", "/tmp/test-graphify"):
            # Should not raise even if graphify is not installed
            graphify_ensure("/some/repo", "/tmp/test-graphify/repo")

    def test_skips_when_no_data_path(self):
        """graphify_ensure does nothing when GRAPHIFY_DATA_PATH is empty."""
        with patch("agents.graphify_bridge._GRAPHIFY_DATA_PATH", ""):
            graphify_ensure("/some/repo", "/tmp/out")  # Should not raise

    def test_skips_when_graph_is_fresh(self):
        """graphify_ensure skips rebuild when graph.json is recent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            graph_path = os.path.join(tmpdir, "graph.json")
            with open(graph_path, "w") as f:
                f.write('{"nodes": [], "links": []}')

            with patch("agents.graphify_bridge._GRAPHIFY_DATA_PATH", "/tmp"):
                # graph.json was just created — should be considered fresh
                graphify_ensure("/some/repo", tmpdir)
                # No exception means it skipped correctly


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_graphify_bridge.py -v -k "Ensure or Rebuild"`
Expected: FAIL with `ImportError` (functions not defined yet)

- [ ] **Step 3: Implement graphify_ensure and graphify_rebuild**

Append to `agents/graphify_bridge.py`:

```python
# Task types that produce code changes and warrant a graph rebuild
_CODE_TASK_TYPES = frozenset({
    "api-generation", "code-generation", "bug-fix", "refactoring",
    "feature", "backend", "frontend", "fullstack", "devops",
    "security", "testing", "general",
})

# Maximum age (seconds) before a graph is considered stale
_MAX_GRAPH_AGE = 86400  # 24 hours


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_graphify_bridge.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add agents/graphify_bridge.py tests/test_graphify_bridge.py
git commit -m "feat: add graphify_ensure and graphify_rebuild to bridge"
```

---

### Task 3: Rebuild Tool — `agents/tools/graphify_tool.py`

**Files:**
- Create: `agents/tools/graphify_tool.py`
- Modify: `agents/tools/registry.py:560-561`
- Modify: `tests/test_graphify_bridge.py`

- [ ] **Step 1: Write failing test for the rebuild tool**

Append to `tests/test_graphify_bridge.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_graphify_bridge.py::TestGraphifyRebuildTool -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement the rebuild tool**

Create `agents/tools/graphify_tool.py`:

```python
"""Graphify rebuild tool — allows agents to explicitly rebuild a codebase knowledge graph."""

import logging
import os

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
                # Read stats from the report
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
```

- [ ] **Step 4: Register the tool in the tool registry**

In `agents/tools/registry.py`, after the `MemoryRecallTool` registration (line ~564), add:

```python
    # --- Structural knowledge graph (env-gated) ---
    if os.environ.get("GRAPHIFY_DATA_PATH"):
        from .graphify_tool import GraphifyRebuildTool
        registry.register(GraphifyRebuildTool())
        logger.info("Tool registry: graphify_rebuild enabled")
```

Also add `"graphify_rebuild"` to every role's tool set in `ROLE_TOOL_SETS` that should have access. Add it to: `cto`, `frontend_engineer`, `backend_engineer`, `qa_engineer`, `devops_engineer`, `security_engineer` — all roles benefit from structural awareness.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_graphify_bridge.py -v`
Expected: All 12 tests PASS

- [ ] **Step 6: Commit**

```bash
git add agents/tools/graphify_tool.py agents/tools/registry.py tests/test_graphify_bridge.py
git commit -m "feat: add graphify_rebuild agent tool"
```

---

### Task 4: Wire into Graph Nodes

**Files:**
- Modify: `agents/graph_nodes.py:174` (after palace_inject block)
- Modify: `agents/graph_nodes.py:368` (after palace_persist block)

- [ ] **Step 1: Add graphify_inject hook to inject_memory**

In `agents/graph_nodes.py`, after the palace_inject try/except block (line 174), before the outer `except Exception` on line 176, add:

```python
            # Supplement with Graphify structural context
            try:
                from .graphify_bridge import graphify_inject
                graph_text = graphify_inject(state)
                if graph_text:
                    state["memory_context"] = state.get("memory_context", "") + graph_text
                    logger.info("Injected graphify structural context into specialist context")
            except Exception as e:
                logger.debug(f"Graphify injection skipped: {e}")
```

- [ ] **Step 2: Add graphify_rebuild hook to persist_memory_wrapper**

In `agents/graph_nodes.py`, after the palace_persist try/except block (line 368), before `return result` on line 370, add:

```python
        # Update knowledge graph if code changed (additive, never blocks)
        try:
            from .graphify_bridge import graphify_rebuild
            graphify_rebuild(state)
        except Exception as e:
            logger.debug("graphify_rebuild: skipped (%s)", e)
```

- [ ] **Step 3: Run existing tests to verify no regression**

Run: `python -m pytest tests/ -x -m "not e2e" --no-header -q --timeout=60 2>&1 | tail -5`
Expected: All existing tests PASS

- [ ] **Step 4: Commit**

```bash
git add agents/graph_nodes.py
git commit -m "feat: wire graphify inject and rebuild into graph nodes"
```

---

### Task 5: MCP Server + Docker Infrastructure

**Files:**
- Modify: `deerflow/extensions_config.json`
- Modify: `docker-compose.yml`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add Graphify MCP server to extensions_config.json**

Replace the contents of `deerflow/extensions_config.json` with:

```json
{
  "mcpServers": {
    "mempalace": {
      "enabled": true,
      "type": "stdio",
      "command": "uv",
      "args": ["run", "python", "-m", "mempalace.mcp_server", "--palace", "/palace/palace"],
      "env": {
        "MEMPALACE_PALACE_PATH": "/palace/palace"
      },
      "description": "MemPalace — structured AI memory with semantic search, knowledge graph, and agent diaries"
    },
    "graphify": {
      "enabled": true,
      "type": "stdio",
      "command": "python3",
      "args": ["-m", "graphify.serve", "/graphify/vibe-stack/graph.json"],
      "env": {},
      "description": "Graphify — structural codebase knowledge graph with community detection and god node analysis"
    }
  },
  "skills": {}
}
```

- [ ] **Step 2: Add graphify-data volume and mounts to docker-compose.yml**

Add `graphify-data:` to the `volumes:` section at the bottom of docker-compose.yml (after `palace-data:`).

Add volume mount `- graphify-data:/graphify` to all three services: `deerflow-langgraph`, `deerflow-gateway`, and `vibe`.

Add environment variable `- GRAPHIFY_DATA_PATH=/graphify` to all three services.

Update DeerFlow command overrides to also install graphify:

For `deerflow-langgraph`:
```yaml
command: >
  sh -c "cd backend && uv pip install mempalace>=3.1.0 'graphifyy[mcp,leiden]' &&
  uv run langgraph dev
  --no-browser --allow-blocking --no-reload --host 0.0.0.0 --port 2024"
```

For `deerflow-gateway`:
```yaml
command: >
  sh -c "cd backend && uv pip install mempalace>=3.1.0 'graphifyy[mcp,leiden]' &&
  uv run uvicorn
  src.gateway.app:app --host 0.0.0.0 --port 8001"
```

- [ ] **Step 3: Add graphifyy dependency to pyproject.toml**

In the `[project.optional-dependencies] agents` list, add:

```
"graphifyy[leiden]>=0.3.20",
```

- [ ] **Step 4: Commit**

```bash
git add deerflow/extensions_config.json docker-compose.yml pyproject.toml
git commit -m "feat: add Graphify infrastructure — volume, MCP server, dependency"
```

---

### Task 6: Deploy and Verify

**Files:** None (operational verification)

- [ ] **Step 1: Restart DeerFlow services**

```bash
cd /home/prime/Repos/Vibe-Stack
docker compose up -d deerflow-langgraph deerflow-gateway
```

Expected: Both containers start, `graphify-data` volume created.

- [ ] **Step 2: Verify graphify installed in DeerFlow**

```bash
docker compose exec deerflow-langgraph python3 -c "import graphify; print('graphify OK')"
```

Expected: `graphify OK`

- [ ] **Step 3: Verify MCP server config loaded**

```bash
docker compose exec deerflow-langgraph cat /app/extensions_config.json | python3 -m json.tool
```

Expected: JSON with both `mempalace` and `graphify` entries.

- [ ] **Step 4: Run new tests**

```bash
python -m pytest tests/test_graphify_bridge.py -v
```

Expected: All tests PASS.

- [ ] **Step 5: Run full test suite**

```bash
python -m pytest tests/ -x -m "not e2e" --no-header -q --timeout=60 2>&1 | tail -10
```

Expected: All existing tests PASS, no regressions.

- [ ] **Step 6: Verify env vars in all containers**

```bash
docker compose exec deerflow-langgraph sh -c 'echo $GRAPHIFY_DATA_PATH'
docker compose exec deerflow-gateway sh -c 'echo $GRAPHIFY_DATA_PATH'
```

Expected: `/graphify` for both.

- [ ] **Step 7: Push to remote**

```bash
git push origin main
```
