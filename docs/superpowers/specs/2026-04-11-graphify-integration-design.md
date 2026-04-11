# Graphify Integration Design

**Date:** 2026-04-11
**Status:** Draft
**Goal:** Add Graphify as shared structural awareness infrastructure for all Vibe Stack agents, following the MemPalace bridge pattern.

---

## Context

Vibe Stack agents currently understand code by reading files one at a time (grep/glob). They lack structural awareness — which classes call which, how modules cluster, where the god nodes are. Graphify (`tmartin2113/graphify`, fork of `safishamsi/graphify`) generates knowledge graphs from codebases via tree-sitter AST parsing (no LLM needed for code), producing typed nodes/edges with community clustering and centrality analysis.

**Mental model:** MemoryStore = what happened before. MemPalace = who knows what. Graphify = how the code is wired together.

Graphify is purely additive — it fills a gap nothing else covers.

---

## Architecture

### Role

Graphify is **shared infrastructure**, not an agent. Every agent benefits automatically through two access patterns:

1. **Passive (injection):** GRAPH_REPORT.md content injected into `memory_context` alongside MemoryStore recalls and MemPalace long-term memory. Gives agents immediate structural orientation (~500-800 tokens).
2. **Active (MCP + tool):** Agents query the graph programmatically via MCP tools (`query_graph`, `get_neighbors`, `god_nodes`, `shortest_path`, etc.) for deep structural questions. Agents can also trigger graph rebuilds explicitly.

### Data Flow

```
Code changes → persist_memory_wrapper → graphify_rebuild() [incremental, AST-only]
                                              ↓
                                     /graphify/{repo}/graph.json
                                     /graphify/{repo}/GRAPH_REPORT.md
                                              ↓
              ┌──────────────────────┬────────┴────────┐
              ↓                      ↓                  ↓
     inject_memory()          MCP server           rebuild_graph tool
     (report → context)    (structural queries)   (explicit full rebuild)
```

---

## Components

### 1. Docker Infrastructure

**Volume:** `graphify-data` — shared across `vibe`, `deerflow-langgraph`, `deerflow-gateway`.

| Service | Mount point |
|---------|-------------|
| vibe | `/graphify` |
| deerflow-langgraph | `/graphify` |
| deerflow-gateway | `/graphify` |

**Directory structure:**
```
/graphify/
  vibe-stack/
    graph.json          # NetworkX node-link JSON
    GRAPH_REPORT.md     # God nodes, communities, gaps (~500-800 tokens)
  paperclip/
    graph.json
    GRAPH_REPORT.md
```

**Dependency:** `graphifyy[mcp,leiden]` added to:
- `pyproject.toml` (agents extras) for the vibe container
- DeerFlow command override (`cd backend && uv pip install graphifyy[mcp,leiden]`)

**Environment variable:** `GRAPHIFY_DATA_PATH=/graphify` in all three services.

### 2. Bridge Module — `agents/graphify_bridge.py`

Mirrors `palace_bridge.py`. All functions wrapped in try/except — Graphify is additive and must never block the workflow. If graphify is not installed or the data volume is unavailable, every function silently returns.

**Functions:**

#### `graphify_inject(state: dict) -> str`
- Called from `inject_memory()` in `graph_nodes.py`
- Reads `GRAPH_REPORT.md` for the target repo from `/graphify/{repo}/`
- Returns formatted text to append to `memory_context`, or empty string
- Repo detection: uses `state["workspace_path"]` or falls back to Vibe Stack's own graph

#### `graphify_rebuild(state: dict) -> None`
- Called from `persist_memory_wrapper()` in `graph_nodes.py`
- Checks if the run produced code changes (looks at `state["final_output"]` for code artifacts, or `state["tool_calls_made"]` for file-write operations)
- If code changed, runs incremental rebuild: `graphify.extract()` + `graphify.build_from_json()` + `graphify.cluster()` on the target repo
- AST-only (tree-sitter), no LLM calls, fast (~seconds for incremental)
- Writes updated `graph.json` and regenerates `GRAPH_REPORT.md`

#### `graphify_ensure(repo_path: str, graph_dir: str) -> None`
- Ensures a graph exists for the given repo
- If `graph.json` doesn't exist or is older than 24 hours, runs a full build
- Called during container startup for pre-built core repos
- Also called by `graphify_inject()` as a fallback if no graph exists yet

### 3. MCP Server Configuration

Added to `deerflow/extensions_config.json`:

```json
{
  "mcpServers": {
    "mempalace": { ... },
    "graphify": {
      "enabled": true,
      "type": "stdio",
      "command": "python3",
      "args": ["-m", "graphify.serve", "/graphify/vibe-stack/graph.json"],
      "env": {},
      "description": "Graphify — structural codebase knowledge graph with community detection and god node analysis"
    }
  }
}
```

**Multi-repo consideration:** The MCP server loads one graph.json at a time. For the initial integration, it points at the Vibe Stack graph (the repo agents modify most). Future enhancement: a thin wrapper that accepts a `repo` parameter and loads the appropriate graph, or multiple MCP entries.

**Tools exposed (7):**
- `query_graph` — keyword/question search with BFS/DFS traversal
- `get_node` — fetch node details by label/ID
- `get_neighbors` — direct connections of a node
- `get_community` — all nodes in a community cluster
- `god_nodes` — top N most-connected nodes
- `graph_stats` — node/edge/community counts, confidence breakdown
- `shortest_path` — path between two concepts

### 4. Rebuild Tool — `agents/tools/graphify_tool.py`

Registered in the tool registry alongside existing tools. Allows agents to explicitly trigger a full graph rebuild.

```python
def rebuild_graph(repo_path: str, full: bool = False) -> str:
    """Rebuild the knowledge graph for a repository.

    Args:
        repo_path: Path to the repository to graph
        full: If True, full rebuild. If False, incremental update only.

    Returns:
        Summary of graph stats after rebuild.
    """
```

Available to all agent roles. Wrapped in try/except for graceful degradation.

### 5. Graph Nodes Integration

Two hook points in `graph_nodes.py`, identical pattern to MemPalace:

**inject_memory() — after palace_inject:**
```python
# Supplement with Graphify structural context
try:
    from .graphify_bridge import graphify_inject
    graph_text = graphify_inject(state)
    if graph_text:
        state["memory_context"] = state.get("memory_context", "") + graph_text
except Exception as e:
    logger.debug(f"Graphify injection skipped: {e}")
```

**persist_memory_wrapper() — after palace_persist:**
```python
# Update knowledge graph if code changed (additive, never blocks)
try:
    from .graphify_bridge import graphify_rebuild
    graphify_rebuild(state)
except Exception as e:
    logger.debug("graphify_rebuild: skipped (%s)", e)
```

### 6. Pre-Built Graphs

Generated at container startup or first run for core repos:

| Repo | Source path | Graph output |
|------|-----------|--------------|
| Vibe Stack | `/workspace/Vibe-Stack` or local agents/ dir | `/graphify/vibe-stack/` |
| Paperclip | `/workspace/paperclip` | `/graphify/paperclip/` |

Extraction is AST-only (tree-sitter), no LLM. Estimated build time: ~30-60 seconds for Vibe Stack (~50 Python files), ~20 seconds for Paperclip server (~30 TypeScript files).

The `graphify_ensure()` function handles this — called once on first `graphify_inject()` invocation, or explicitly during container init.

---

## What This Does NOT Include

- **LLM-based semantic extraction** — AST-only for now. Semantic extraction (docs, images) is expensive and unnecessary for code structure.
- **Multi-graph MCP switching** — Initial version points at one graph. Multi-repo MCP is a future enhancement.
- **Graph diffing in critic loop** — Comparing pre/post graphs to detect structural regressions is valuable but out of scope for v1.
- **Neo4j export** — Graphify supports it, but we don't need another database. NetworkX in-memory is sufficient.
- **Watch mode** — Not needed when rebuild is triggered by the agent pipeline.

---

## Testing

**New test file:** `tests/test_graphify_bridge.py`

Tests mirror `test_palace_bridge.py` structure:
- `test_graphify_inject_returns_report` — returns formatted graph report text
- `test_graphify_inject_graceful_when_missing` — returns empty string when graphify not installed
- `test_graphify_inject_graceful_on_empty_state` — handles missing state keys
- `test_graphify_rebuild_triggers_on_code_changes` — incremental rebuild when code detected
- `test_graphify_rebuild_skips_non_code_runs` — no rebuild for non-code tasks
- `test_graphify_rebuild_graceful_when_missing` — never raises
- `test_graphify_ensure_builds_if_missing` — creates graph if none exists
- `test_graphify_ensure_skips_if_fresh` — doesn't rebuild recent graphs
- `test_rebuild_tool_returns_stats` — tool returns graph statistics

All tests mock `graphify` imports for environments where it's not installed. Existing test suite must pass unchanged.

---

## Verification

1. `docker compose up -d` — verify `graphify-data` volume created
2. `docker exec vibe-stack-deerflow-langgraph-1 python3 -c "import graphify; print('OK')"` — verify install
3. Trigger a heartbeat run — verify `graphify_inject` logs report injection
4. `python -m pytest tests/test_graphify_bridge.py -v` — new tests pass
5. `python -m pytest tests/ -x -m "not e2e" --no-header -q` — existing tests unaffected
6. Check `/graphify/vibe-stack/graph.json` exists after first run

---

## Key Files Summary

| Action | File |
|--------|------|
| Create | `agents/graphify_bridge.py` |
| Create | `agents/tools/graphify_tool.py` |
| Create | `tests/test_graphify_bridge.py` |
| Modify | `deerflow/extensions_config.json` (add graphify MCP entry) |
| Modify | `docker-compose.yml` (volume + mounts + env vars + pip install) |
| Modify | `agents/graph_nodes.py` (2 hook points) |
| Modify | `pyproject.toml` (add graphifyy dependency) |
