# DeerFlow Gateway Replacement — Design Spec (Deferred)

> Replace the `langgraph-cli` dependency with a native gateway that serves the LangGraph Platform API directly. **Deferred** — this is the largest remaining upstream change and needs dedicated focus.

## Status: DEFERRED

This spec documents the scope and approach for when we're ready to tackle it. Not scheduled for immediate implementation.

## Context

Upstream PR #1403 (bytedance/deer-flow) eliminated the `langgraph-cli` and `langgraph-api` dependencies by having the FastAPI Gateway embed the agent runtime directly. This reduces the DeerFlow stack from 2 processes (langgraph-cli + gateway) to 1 (gateway only).

**Current fork architecture:**
- `deerflow-langgraph` container: runs `langgraph dev` on port 2024 (agent runtime)
- `deerflow-gateway` container: runs FastAPI on port 8001 (REST API for models, skills, memory, etc.)
- Paperclip adapter talks to port 2024 for agent execution, port 8001 for health checks

**Target architecture:**
- Single `deerflow-gateway` container: runs FastAPI on port 8001 serving both REST API and LangGraph Platform API
- Paperclip adapter talks to port 8001 for everything
- `deerflow-langgraph` container eliminated

## Scope

### New code (~35 files)

**Runtime package** (`deerflow/runtime/`):
- `runs/manager.py` — in-memory run registry
- `runs/worker.py` — asyncio.Task per run, publishes SSE events via `graph.astream()`
- `runs/schemas.py` — RunStatus, DisconnectMode, CancelAction enums
- `stream_bridge/` — in-memory async pub/sub decoupling agent execution from SSE endpoints
- `store/` — thread metadata Store (SQLite/memory)
- `serialization.py` — LangChain object serialization

**Gateway additions:**
- `gateway/deps.py` — runtime bootstrap context manager
- `gateway/services.py` — run lifecycle service layer
- `gateway/routers/assistants_compat.py` — stub for `useStream` hook
- `gateway/routers/thread_runs.py` — thread runs CRUD + SSE streaming
- `gateway/routers/runs.py` — stateless runs

**Gateway modifications:**
- `gateway/app.py` — lifespan wraps in `langgraph_runtime()`, new routers
- `gateway/routers/threads.py` — expanded from stub to full Store-backed CRUD

### Dependencies removed
- `langgraph-api` (currently `>=0.7.0,<0.8.0`)
- `langgraph-cli` (currently `>=0.4.14`)
- `langgraph-runtime-inmem` (currently `>=0.22.1`)
- Keep: `langgraph-sdk>=0.1.51` (channels/client still use it)

### Infrastructure changes
- Docker compose: remove `deerflow-langgraph` service
- Nginx: route `/api/langgraph/*` to gateway instead of langgraph process
- Paperclip adapter: update `deerflowUrl` default from `:2024` to `:8001`
- Vibe-Stack compose: remove langgraph service, update volume mounts

## Risks

1. **In-memory run state** — process restart loses active runs
2. **Import path translation** — 35 files use `app.gateway.*`, need `deerflow.gateway.*`
3. **No `enqueue` multitask strategy** — returns 400 for concurrent same-thread runs
4. **Channels rewiring** — currently use langgraph-sdk HTTP client to port 2024
5. **LangGraph version compatibility** — `context` vs `configurable` split in >= 0.6.0
6. **Testing surface** — need integration tests for SSE streaming, run lifecycle, thread CRUD

## Estimated effort

5-7 sessions of focused work. Should be done on a feature branch with staging validation before merging to master.

## Prerequisites

- ✅ Namespace migration (done)
- ✅ Upstream fix batch (done)
- Memory and sandbox improvements (in progress — do these first)
