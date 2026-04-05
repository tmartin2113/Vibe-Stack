# Architecture

Deep technical reference for Vibe Stack internals. For getting started, see the [README](../README.md).

## Agent Workflow Engine

Each agent runs a deterministic state machine: **Router -> Skill Loader -> Spec Builder -> Specialist -> Critic -> (loop or finish)**

### Pipeline Nodes (`agents/graph.py`)

| Node | File | Purpose |
|------|------|---------|
| **Router** | `agents/router.py` | Classifies task type via regex/LLM/hybrid from `TaskTypeRegistry`. 12 built-in types + skill-defined custom types |
| **Skill Loader** | `agents/skill_loader.py` | Loads task-type-specific skills from 3-tier registry (community -> approved -> builtin). Security-gated |
| **Spec Builder** | `agents/nodes.py` | LLM generates detailed specification. Can emit `clarification_needed` with questions for human |
| **Specialist** | `agents/nodes.py` | Executes code generation with tool access. Single or multi-specialist (sub-task decomposition). Clarification requests escalate to human |
| **Critic** | `agents/nodes.py` | Scores output 0-100. Below threshold -> refinement loop. Above -> done |

### 13 Built-in Task Types

Hybrid routing uses regex + semantic matching to select the right specialist adapter. The `TaskTypeRegistry` (`agents/task_type_registry.py`) manages both builtin and skill-defined types.

### Critic-Driven Refinement

Quality threshold: **85/100**. Below this score, the Specialist receives critic feedback and iterates. This loop continues until the score passes or the iteration limit is reached.

## Role-Based Tool Filtering

Tools are filtered per-agent via `ROLE_TOOL_SETS` in `agents/tools/registry.py`. Each role sees only the tools relevant to their work.

### Tool Registry

| Tool | Available To | Service | Purpose |
|------|-------------|---------|---------|
| QuickLookup | senior engineers | SearXNG | Rate-limited web search (1-2 calls/session) |
| WebSearch | assistants | SearXNG | Unrestricted privacy-respecting web search |
| WebScrape | assistants | Playwright | Headless browser scraping |
| BrowserAutomation | frontend, QA, UX, security | Playwright | Browser interaction and E2E testing |
| Design | frontend, UX | Penpot | Design tool integration |
| GitForge | all | Gitea | Git hosting operations |
| ArtifactStorage | all | MinIO | S3-compatible object storage |
| MiroFishSimulation | CTO, backend, QA | MiroFish | Multi-agent simulation for risk assessment |
| OCRTool | all | PaddleOCR | Text extraction from images and PDFs |
| FileReader | all | built-in | Targeted file reads with `start_line`/`end_line`, auto-capped at 200 lines |
| MemoryStore / MemoryRecall | all | built-in | Persistent long-term memory with citations |

### Research Delegation

Senior engineers get `QuickLookup` (rate-limited to 1-2 calls/session via `QuickLookupTool` in `agents/tools/quick_lookup.py`). When the limit is reached, the tool returns an error directing the engineer to delegate further research to their DeerFlow assistant. CTO gets a higher limit configurable via `VIBE_CTO_LOOKUP_LIMIT` (default: 2).

Assistants retain unrestricted `WebSearch` and `WebScrape` access.

### FileReader Targeting

`FileReader` (`agents/tools/file_tools.py`) supports `start_line`/`end_line` for targeted reads. Files exceeding `VIBE_FILE_READ_LINE_CAP` (default: 200) lines are auto-capped with a warning. Redundant re-reads within a session are flagged.

## Agent Instructions

Two-tier instruction system:

1. **`agent-instructions/`** -- shared base instructions plus role-specific guides:
   - `base-instructions.md` -- tech stack, code standards, git workflow, Paperclip coordination
   - `cto-instructions.md`, `engineer-instructions.md`, `qa-instructions.md`, `devops-instructions.md`, `ux-instructions.md`, `security-instructions.md`, `pm-instructions.md`

2. **`agents/<role>/AGENTS.md`** -- per-agent operational directives:
   - Output guidelines (terse 3-6 word sentences, no filler)
   - Mandatory DeerFlow delegation rules
   - Tool usage policies

### Output Brevity Enforcement

- Tool call continuations capped at **500 tokens** (was 1500)
- File reads auto-capped at **200 lines** when no range specified
- Redundant re-reads within same session are flagged
- Agents use short 3-6 word sentences, no preamble or filler

## Self-Upgrade System

Agents detect recurring quality issues across heartbeat runs and propose improvements to their own source code.

1. **Signal accumulation** -- low scores, tool failures, iteration exhaustion, and critic feedback patterns are recorded
2. **Threshold trigger** -- after 3+ signals for a task type, a proposal is generated
3. **Safety pipeline** -- 5 gates:
   - Path validation (`agents/` only)
   - Diff size (<500 lines)
   - Full pytest suite
   - Bandit security scan
   - Critic scoring (>=90)
4. **Human review** -- proposals appear in the Paperclip **Improvements** section with branch name and review instructions

## Skill System

### Skill Sources

| Source | Repository | Content |
|--------|-----------|---------|
| Anthropic | `anthropics/skills` | Official skill collection |
| Impeccable | `pbakaus/impeccable` | 21 design quality skills (audit, polish, typeset, etc.) |
| Superpowers | `obra/superpowers` | TDD, debugging, planning methodologies |
| Vercel | `vercel-labs/agent-skills` | React, web design best practices |
| VoltAgent | `voltagent/awesome-openclaw-skills` | OpenClaw community catalog (~5000 skills) |

Skills load and unload automatically per-task via `SkillLoaderNode` and `SkillCleanupNode`. The skill system matches skills to task types -- frontend tasks get design skills, backend tasks don't.

### Skill Security (`agents/skill_security.py`)

Defense-in-depth: AST + regex content scanning, runtime tool permission enforcement, SHA-256 integrity verification, container isolation. Each layer is independent.

### Skill Reinforcement

Closed-loop system: outcomes recorded -> RAG retrieval -> generation -> self-refinement for low scores. Managed by `agents/skill_generator.py`, `agents/skill_outcome_store.py`, and `agents/skill_cleanup.py`.

## LLM Backends

| Backend | File | API | Auth |
|---------|------|-----|------|
| **vLLM** (default) | `vibe/backends/vllm.py` | OpenAI-compatible `/v1/chat/completions` | None (local) |
| **OpenAI** | `vibe/backends/openai_backend.py` | OpenAI API | `OPENAI_API_KEY` |
| **Anthropic** | `vibe/backends/anthropic_backend.py` | Anthropic Messages API | `ANTHROPIC_API_KEY` |

All backends implement `BackendBase` (generate, generate_chat, health_check). Rate limit handling with Retry-After header parsing.

### Backend Pool (`agents/backend_pool.py`)

Multi-backend load balancing and failover:

- **Strategies**: `failover` (primary first), `round_robin`, `least_loaded`
- **Circuit breaker**: per-backend, 3-state (CLOSED -> OPEN -> HALF_OPEN -> CLOSED). Opens after N consecutive failures, probes after recovery timeout
- **Thread-safe**: per-entry locks, atomic inflight counters
- **Drop-in**: same `generate()` + `health_check()` interface as `LLMBackend`

Configuration: `VIBE_FALLBACK_URLS` (comma-separated host:port), `VIBE_BACKEND_POOL_STRATEGY`, `VIBE_FALLBACK_BACKEND_TYPE`.

### LLM Retry (`agents/llm_retry.py`)

Exponential backoff with jitter, Retry-After header support, per-node and workflow timeouts.

## Storage Layer (`agents/storage/`)

Pluggable storage enabling multi-node deployment.

```
VIBE_STORAGE_BACKEND=sqlite    -> local dev (default, zero infra)
VIBE_STORAGE_BACKEND=postgres  -> production multi-node (PostgreSQL + pgvector)
VIBE_CACHE_BACKEND=memory      -> local dev (default, in-process dict)
VIBE_CACHE_BACKEND=redis       -> production multi-node (Redis for cache + distributed locks)
```

### Interfaces

| Interface | Purpose | Local Dev | Production |
|-----------|---------|-----------|------------|
| **StorageBackend** | SQL-like persistent storage | `SQLiteBackend` (WAL) | `PostgresBackend` (connection pool, pgvector) |
| **CacheBackend** | Key-value with TTL | `MemoryCacheBackend` | `RedisCacheBackend` (namespaced) |
| **DistributedLock** | Mutual exclusion across nodes | `LocalLock` (threading.Lock) | `RedisDistributedLock` (SET NX EX + Lua) |

### Wired Stores

| Store | Backend Param | Tests |
|-------|---------------|-------|
| `artifact_store.py` | `storage_backend` | 64 pass |
| `message_store.py` | `storage_backend` | 107 pass |
| `memory_store.py` | `storage_backend` | 139 pass |
| `spending_tracker.py` | `storage_backend` | 27 pass |

## Simulation (MiroFish)

Multi-agent prediction handled by an external MiroFish service, invoked via the `MiroFishSimulation` tool. Agents use it selectively for architecture decisions, deployment risk assessment, and integration conflict detection.

**Complexity-based LLM routing:**
- Simple simulations (<40 agents, <20 iterations) -> local vLLM (free)
- Complex simulations -> cloud API (if configured, else local with warning)

**Infrastructure:** MiroFish service (port 5001) + self-hosted Zep CE (pgvector + Neo4j + Graphiti) for agent memory. Configurable via `MIROFISH_*` and `ZEP_*` environment variables.

## Production Hardening

| Feature | Implementation |
|---------|---------------|
| **Progress updates** | Live status comments on Paperclip issues (`agents/heartbeat_progress.py`) |
| **Graceful SIGTERM** | Posts partial results on container kill, sets issue to blocked for retry (`agents/heartbeat_signals.py`) |
| **JWT auto-auth** | Fresh JWTs from shared secret per heartbeat (no static API keys) |
| **Lazy sandbox init** | Defers container pre-warming to first tool execution |
| **Cached factory** | Reuses LLM backend and 17 adapter instances across heartbeat runs (`agents/workflow_factory.py`) |
| **Spending tracker** | Per-agent cost tracking with configurable budget caps and circuit breaker (`agents/spending_tracker.py`) |
| **Billing exhaustion halt** | Agents halt permanently when Anthropic billing is exhausted |
| **WebSocket push** | Push-based Paperclip events via WS with auto-reconnect (`agents/ws_client.py`) |

## Infrastructure Services

| Service | Purpose | Port |
|---------|---------|------|
| Paperclip | Control plane + UI | 3100 |
| DeerFlow LangGraph | Research assistant backend | 2024 (internal) |
| DeerFlow Gateway | Research assistant API | 8001 (internal) |
| vLLM | Local model inference | 8000 |
| SearXNG | Self-hosted web search | 8888 |
| Gitea | Git hosting | 3000 |
| MinIO | Object storage | 9000 |
| Penpot | Design tool | 9001 |
| Playwright | Browser automation | 3003 |
| OpenSandbox | Code execution sandbox | 9090 |
| MiroFish | Multi-agent simulation engine | 5001 |
| Zep | Agent memory (for MiroFish) | 8000 (internal) |
| Neo4j | Graph database (for Zep) | 7687 (internal) |
| PaddleOCR | OCR text/layout/table extraction | 8868 |
| Caddy | TLS reverse proxy | 443 |

## Execution Modes

| Mode | Command | Purpose |
|------|---------|---------|
| **Heartbeat** | `python -m agents.main --heartbeat` | Paperclip-driven. Fetches one task, runs workflow, posts result, exits |
| **Daemon** | `python -m agents.main --daemon` | Polls Mattermost/Slack for @mentions. Multi-threaded workers. Session persistence |
| **Interactive** | `python -m agents.main` | Single-request CLI mode |
| **Doctor** | `python -m agents.main --doctor` | Health checks for LLM, sandbox, hardware, connectivity |

## Key Subsystems Reference

| System | Files | Purpose |
|--------|-------|---------|
| **Workflow Graph** | `agents/graph.py` | Deterministic state machine |
| **Workflow Nodes** | `agents/nodes.py` | All workflow nodes |
| **State** | `agents/state.py` | TypedDict state definition |
| **Config** | `agents/config.py` | SystemConfig, WorkflowConfig, GenerationConfig, StorageConfig |
| **Adapters** | `agents/adapters.py` | PromptAdapter, AdapterRegistry, all system prompts |
| **Heartbeat** | `agents/heartbeat.py` | Paperclip heartbeat mode |
| **Session Store** | `agents/session_store.py` | SQLite + WAL. Daemon-mode only. TTL-based cleanup |
| **Messenger Client** | `agents/messenger_client.py` | MattermostClient + SlackClient |
| **Resource Discovery** | `agents/resource_discovery.py` | CPU/RAM/GPU introspection. `query_gpu_realtime()` for live VRAM probing |
| **Resource Allocator** | `agents/resource_allocator.py` | Resource plans for sandbox pool sizing |
| **Message Store** | `agents/message_store.py` | Message bus with FTS5 + vector semantic search |
| **Memory Store** | `agents/memory_store.py` | Long-term memory with citations, BM25 + vector search |
| **Embedder** | `agents/embedder.py` | Singleton VLLMEmbedder shared across stores |

## Migration Guide

### Upgrading Agent Roles

Deployments created before the role-based tool filtering update may have generic agent roles (e.g., `engineer` instead of `backend_engineer`). This causes all agents to receive unfiltered tool sets.

**To fix:** Update agent roles in the Paperclip database. See `bootstrap-org.cjs` (lines 14-28) for the role mapping and SQL update commands.

Required role values: `cto`, `backend_engineer`, `frontend_engineer`, `qa_engineer`, `devops_engineer`, `research_assistant`.

## Configuration Reference

See `.env.example` for all configurable values. Key variables:

| Variable | Purpose | Default |
|----------|---------|---------|
| `VLLM_MODEL` | Local inference model | Auto-detected by GPU VRAM |
| `GH_TOKEN` | GitHub PAT for agent git push | -- |
| `VIBE_SANDBOX_BACKEND` | `opensandbox` or `subprocess` | `subprocess` |
| `VIBE_STORAGE_BACKEND` | `sqlite` or `postgres` | `sqlite` |
| `VIBE_CACHE_BACKEND` | `memory` or `redis` | `memory` |
| `VIBE_DATABASE_URL` | PostgreSQL connection string | -- |
| `VIBE_REDIS_URL` | Redis connection string | -- |
| `VIBE_SKILL_REPOS` | Colon-separated skill repo paths | Auto-configured |
| `VIBE_FILE_READ_LINE_CAP` | Max lines returned by FileReader | `200` |
| `VIBE_CTO_LOOKUP_LIMIT` | QuickLookup calls per session for CTO | `2` |
| `VIBE_FALLBACK_URLS` | Comma-separated fallback backend host:port pairs | -- |
| `VIBE_BACKEND_POOL_STRATEGY` | `failover`, `round_robin`, or `least_loaded` | `failover` |
| `MIROFISH_URL` | MiroFish simulation service URL | `http://mirofish:5001` |
| `OPENAI_API_KEY` | OpenAI API key | -- |
| `ANTHROPIC_API_KEY` | Anthropic API key | -- |
| `PAPERCLIP_API_URL` | Paperclip control plane URL | -- |
| `PAPERCLIP_AGENT_ID` | Agent identity for self-comment filtering | -- |
| `LOG_LEVEL` | Logging verbosity | `WARNING` |
