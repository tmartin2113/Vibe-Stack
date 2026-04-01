# Vibe

Multi-agent code generation system with iterative quality refinement. Deployed via Paperclip.

## Architecture

```
User → Paperclip issue → Adapter (TypeScript) → Heartbeat (Python) → Workflow graph → Result → Paperclip comment
```

### Workflow Pipeline (`agents/graph.py`)

Deterministic state machine: **Router → Skill Loader → Spec Builder → Specialist → Critic → (loop or finish)**

- **Router** (`agents/router.py`) — classifies task type via regex/LLM/hybrid from `TaskTypeRegistry`. 12 built-in types + skill-defined custom types.
- **Skill Loader** (`agents/skill_loader.py`) — loads task-type-specific skills from 3-tier registry (community → approved → builtin). Security-gated.
- **Spec Builder** (`agents/nodes.py`) — LLM generates detailed specification. Can emit `clarification_needed` with questions for human.
- **Specialist** (`agents/nodes.py`) — executes code generation with tool access (Python executor, pytest, bandit, file I/O). Single-specialist or multi-specialist (sub-task decomposition). Clarification requests are first routed through simulation before escalating to human.
- **Critic** (`agents/nodes.py`) — scores output 0-100. Below threshold → refinement loop. Above → done.

### Key Subsystems

| System | Files | Purpose |
|--------|-------|---------|
| **LLM Backend** | `agents/llm_backend.py`, `vibe/backends/` | vLLM (default, local), OpenAI, Anthropic. BackendPool for multi-backend failover/load balancing |
| **Backend Pool** | `agents/backend_pool.py` | Multi-backend failover, round-robin, least-loaded strategies. Per-backend circuit breaker (closed→open→half-open) |
| **Storage Layer** | `agents/storage/` | Pluggable storage abstraction: SQLite (local dev) or PostgreSQL (multi-node). Redis for caching + distributed locks |
| **Tool System** | `agents/tools/` | 5 default tools + extended dev/SEO tools. OpenSandbox or subprocess execution. |
| **Skill Security** | `agents/skill_security.py` | Name/path/content validation, AST+regex scanning, runtime tool permission enforcement, SHA-256 integrity |
| **Skill Reinforcement** | `agents/skill_generator.py`, `agents/skill_outcome_store.py`, `agents/skill_cleanup.py` | Closed-loop: outcomes recorded → RAG retrieval → generation → simulation vetting → self-refinement for low scores |
| **Simulation** | `agents/simulation.py` | MiroFish-inspired swarm intelligence: integration prediction (parallel sidecar), clarification short-circuit (stakeholder simulation), offline skill vetting. Hardware-aware VRAM gating |
| **Session Store** | `agents/session_store.py` | SQLite + WAL. Daemon-mode only. TTL-based cleanup. |
| **Messenger Client** | `agents/messenger_client.py` | MattermostClient + SlackClient. Used by daemon and API key prompting. |
| **Resource Discovery** | `agents/resource_discovery.py`, `agents/resource_allocator.py` | CPU/RAM/GPU introspection (startup + real-time). `query_gpu_realtime()` for live VRAM probing. Resource plans for sandbox pool sizing |
| **Sandbox** | `agents/sandbox/` | OpenSandbox Docker containers with GPU passthrough. Toggle: `VIBE_SANDBOX_BACKEND=opensandbox\|subprocess` |
| **LLM Retry** | `agents/llm_retry.py` | Exponential backoff with jitter, Retry-After header support, per-node and workflow timeouts |
| **Task Type Registry** | `agents/task_type_registry.py` | Unified registry of builtin + skill-defined types. Router, orchestrator, and LLM classifier all read from it |
| **Workflow Factory** | `agents/workflow_factory.py` | Cached LLM backend + 28 adapters (17 specialist + 11 simulation) across heartbeat runs. Lazy init on first `run_workflow()` |
| **Heartbeat Hardening** | `agents/heartbeat_progress.py`, `agents/heartbeat_signals.py` | Progress comments at key nodes, graceful SIGTERM with partial result posting |
| **WebSocket Client** | `agents/ws_client.py` | Push-based Paperclip events via WS. Auto-reconnect with backoff. Used by orchestrator POLL and cancellation watcher |
| **MessageStore** | `agents/message_store.py`, `agents/message_types.py` | Message bus with FTS5 + vector semantic search. Typed messages (INFO, DECISION, BLOCKER, HANDOFF, STATUS, QUESTION, COMPLETION) with TTL-based expiry. Pluggable storage backend |
| **MemoryStore** | `agents/memory_store.py` | Long-term agent memory with citations, BM25 + vector search, TTL cleanup. Pluggable storage backend |
| **Shared Embedder** | `agents/embedder.py` | Singleton VLLMEmbedder shared across MessageStore + MemoryStore. Graceful degradation when vLLM unavailable |
| **Spending Tracker** | `agents/spending_tracker.py` | Per-agent LLM cost tracking with configurable budget caps and circuit breaker. Pluggable storage backend |

## Simulation (`agents/simulation.py`)

MiroFish-inspired swarm intelligence prediction engine. Uses lightweight persona-based simulations to predict integration conflicts, resolve clarification ambiguity, and vet skills. All simulation calls reuse the already-loaded LLM via PromptAdapter instances — zero additional model loading.

### Integration Points

1. **Parallel sidecar** — runs alongside multi-specialist sub-tasks, feeding conflict predictions to the aggregator. Auto-disabled on <=24GB VRAM (would compete with specialists for KV cache).
2. **Clarification short-circuit** — when a specialist emits `clarification_needed`, simulates stakeholder personas (product owner, end user, domain expert) to resolve ambiguity without human round-trip. Always enabled regardless of VRAM (GPU is idle during clarification).
3. **Skill vetting** — offline simulation against synthetic task populations before first use. Triggers immediate refinement if score is below threshold.

### Hardware-Aware VRAM Gating

```
assess_simulation_budget(mode="sidecar"|"clarification")
  sidecar:        real-time nvidia-smi probe → disabled below 6GB free
  clarification:  always enabled (GPU idle, no KV contention)
```

Configurable via `VIBE_SIM_ENABLED`, `VIBE_SIM_MIN_FREE_VRAM_MB`, `VIBE_SIM_MAX_PERSONA_ROUNDS`, `VIBE_SIM_MAX_TOKENS`.

## Storage Abstraction (`agents/storage/`)

Pluggable storage layer enabling multi-node deployment. All four persistent stores accept an optional `storage_backend` parameter — when provided, SQL is routed through the backend instead of built-in SQLite.

```
VIBE_STORAGE_BACKEND=sqlite    → local dev (default, zero infra)
VIBE_STORAGE_BACKEND=postgres  → production multi-node (PostgreSQL + pgvector)
VIBE_CACHE_BACKEND=memory      → local dev (default, in-process dict)
VIBE_CACHE_BACKEND=redis       → production multi-node (Redis for cache + distributed locks)
```

### Interfaces

- **StorageBackend** (`storage/base.py`) — SQL-like persistent storage (execute, fetchone, fetchall, execute_script, transaction, placeholder dialect)
- **CacheBackend** (`storage/redis_backend.py`) — Key-value with TTL (get, set, delete, incr, get_json, set_json)
- **DistributedLock** (`storage/redis_backend.py`) — Mutual exclusion across nodes (Redis SET NX EX or threading.Lock fallback)

### Implementations

| Interface | Local Dev | Production |
|-----------|-----------|------------|
| StorageBackend | `SQLiteBackend` (WAL, per-call connections) | `PostgresBackend` (connection pool, pgvector) |
| CacheBackend | `MemoryCacheBackend` (in-process dict) | `RedisCacheBackend` (namespaced, TTL) |
| DistributedLock | `LocalLock` (threading.Lock) | `RedisDistributedLock` (SET NX EX + Lua release) |

### Wired Stores

| Store | Backend Param | Tests |
|-------|---------------|-------|
| `artifact_store.py` | `storage_backend` | 64 pass |
| `message_store.py` | `storage_backend` | 107 pass |
| `memory_store.py` | `storage_backend` | 139 pass |
| `spending_tracker.py` | `storage_backend` | 27 pass |

## Backend Pool (`agents/backend_pool.py`)

Multi-backend load balancing and failover for LLM inference.

- **Strategies**: `failover` (primary first), `round_robin`, `least_loaded`
- **Circuit breaker**: per-backend, 3-state (CLOSED → OPEN → HALF_OPEN → CLOSED). Opens after N consecutive failures, probes after recovery timeout
- **Thread-safe**: per-entry locks, atomic inflight counters
- **Drop-in**: same `generate()` + `health_check()` interface as `LLMBackend`

Configuration via `VIBE_FALLBACK_URLS` (comma-separated host:port), `VIBE_BACKEND_POOL_STRATEGY`, `VIBE_FALLBACK_BACKEND_TYPE`.

## Cloud Backends (`vibe/backends/`)

| Backend | File | API | Auth |
|---------|------|-----|------|
| **vLLM** | `vllm.py` | OpenAI-compatible `/v1/chat/completions` | None (local) |
| **OpenAI** | `openai_backend.py` | OpenAI API | `OPENAI_API_KEY` |
| **Anthropic** | `anthropic_backend.py` | Anthropic Messages API | `ANTHROPIC_API_KEY` |

All backends implement `BackendBase` (generate, generate_chat, health_check). Rate limit handling with Retry-After header parsing. No additional dependencies — uses `requests` only.

## Execution Modes

1. **Heartbeat** (`python -m agents.main --heartbeat`) — Paperclip-driven. Fetches one task, runs workflow, posts result, exits.
2. **Daemon** (`python -m agents.main --daemon`) — Polls Mattermost/Slack for @mentions. Multi-threaded workers. Session persistence.
3. **Interactive** (`python -m agents.main`) — Single-request CLI mode.
4. **Doctor** (`python -m agents.main --doctor`) — Health checks for LLM, sandbox, hardware, connectivity.

## Paperclip Integration

### Adapter

Agents run via the **DeerFlow adapter** (`packages/adapters/deerflow/` in the paperclip fork). Paperclip streams tasks to the DeerFlow LangGraph server over HTTP/SSE; agents run as long-lived LangGraph threads rather than spawned subprocesses.

### Heartbeat (`agents/heartbeat.py`)

0. Validate config — fail-fast on missing model, `PAPERCLIP_API_URL`, `PAPERCLIP_AGENT_ID`
1. Connect to Paperclip API
2. Fetch assignments → pick highest-priority task
3. Checkout (atomic)
4. Build context (issue + ancestors + comments)
5. Detect clarification resume → if yes, pre-populate spec (skip spec-building)
6. Install SIGTERM handler + progress callback
7. Run workflow graph via `WorkflowFactory` (cached backend + adapters)
8. Post results (success/blocked/clarification_needed)
9. Report cost events
10. Release checkout

On SIGTERM: posts partial output/score/last-step to Paperclip, sets issue to blocked for retry.

Supporting modules:
- `agents/heartbeat_progress.py` — progress callback that posts Paperclip comments at key nodes
- `agents/heartbeat_signals.py` — SIGTERM handler, partial result posting
- `agents/workflow_factory.py` — cached LLM backend + 28 adapter instances across heartbeat runs

### Docker Compose

Root-level compose files: `docker-compose.yml` (core), `docker-compose.infra.yml` (infrastructure), `docker-compose.gpu.yml` (GPU services). The agent (`vibe` service) is heartbeat-driven — Paperclip spawns it on-demand, it runs one task, and exits.

## Development

### Running Tests

```bash
# Python tests
python -m pytest tests/ -x -m "not e2e" --no-header -q
```

### Test Coverage

~2970 tests across 48 test files covering all major subsystems:
- Heartbeat (142), workflow factory (9), lazy sandbox (4), heartbeat metrics (36), task type registry (22)
- Skill reinforcement (49), routing (37), security (142), skill registry (48), skill sources (55)
- API key manager (39), retry/timeout (72), resource discovery (22), allocator (31)
- Sandbox integration (47), tool system (157), LLM backends (41), messenger client (75)
- Paperclip client (60), orchestrator (90+19), WebSocket client (26)
- Message store (107), message types (26), memory store (139), embedder (30)
- Spending tracker (27), doctor (45), artifact store (64), bulletin board v2 (30)
- Graph coverage (82), workflow nodes (90), daemon/router (175), misc (103+145)
- Dynamic adapters (40), complexity triage (34), heuristic critic (25)
- Integration (36), observability (43), scalability (23), parallel subtasks (54)
- Simulation (41)

### Environment Variables

See `.env.example` for all configurable values. Key ones:

| Variable | Purpose | Default |
|----------|---------|---------|
| `VIBE_SANDBOX_BACKEND` | `opensandbox` or `subprocess` | `subprocess` |
| `PAPERCLIP_API_URL` | Paperclip control plane URL | — |
| `PAPERCLIP_AGENT_ID` | Agent identity for self-comment filtering | — |
| `MESSAGE_STORE_PATH` | SQLite path for inter-agent messages | — |
| `VIBE_MSG_MAX_MESSAGES` | FIFO eviction cap | `5000` |
| `VIBE_MSG_DEFAULT_TTL` | Default message TTL (seconds) | `604800` |
| `VIBE_MSG_CLEANUP_ON_HEARTBEAT` | Run cleanup in heartbeat finally | `true` |
| `VIBE_MSG_BACKFILL_ON_HEARTBEAT` | Run embedding backfill in heartbeat finally | `true` |
| `VIBE_STORAGE_BACKEND` | Persistent storage: `sqlite` or `postgres` | `sqlite` |
| `VIBE_CACHE_BACKEND` | Cache: `memory` or `redis` | `memory` |
| `VIBE_DATABASE_URL` | PostgreSQL connection string (when storage=postgres) | — |
| `VIBE_REDIS_URL` | Redis connection string (when cache=redis) | — |
| `VIBE_FALLBACK_URLS` | Comma-separated fallback backend host:port pairs | — |
| `VIBE_BACKEND_POOL_STRATEGY` | `failover`, `round_robin`, or `least_loaded` | `failover` |
| `VIBE_SIM_ENABLED` | Enable/disable simulation module | `true` |
| `VIBE_SIM_MIN_FREE_VRAM_MB` | Min free VRAM for sidecar simulation | `6144` |
| `VIBE_SIM_VET_SKILLS` | Enable offline skill vetting via simulation | `true` |
| `OPENAI_API_KEY` | OpenAI API key (for OpenAI backend) | — |
| `ANTHROPIC_API_KEY` | Anthropic API key (for Anthropic backend) | — |

### Project Structure

```
agents/                    # Main agent pipeline
  graph.py                 # Workflow state machine
  nodes.py                 # All workflow nodes (router, specialist, critic, etc.)
  state.py                 # TypedDict state definition
  config.py                # SystemConfig, WorkflowConfig, GenerationConfig, StorageConfig
  heartbeat.py             # Paperclip heartbeat mode
  heartbeat_progress.py    # Progress updates to Paperclip
  heartbeat_signals.py     # Graceful SIGTERM handling
  workflow_factory.py      # Cached LLM backend + adapter setup
  ws_client.py             # WebSocket client for Paperclip push events
  daemon.py                # Mattermost/Slack polling mode
  llm_backend.py           # LLM abstraction (local + cloud + pool)
  llm_retry.py             # Retry with exponential backoff
  backend_pool.py          # Multi-backend failover + circuit breaker
  adapters.py              # PromptAdapter, AdapterRegistry, all system prompts
  simulation.py            # MiroFish-inspired simulation (integration, clarification, vetting)
  task_type_registry.py    # Unified builtin + skill task type registry
  router.py                # Task-type classification (reads from registry)
  tools/                   # Tool registry + implementations
  sandbox/                 # OpenSandbox Docker integration
  storage/                 # Pluggable storage abstraction
    base.py                # StorageBackend, CacheBackend, DistributedLock interfaces
    sqlite.py              # SQLite implementation (default)
    postgres.py            # PostgreSQL implementation (multi-node)
    redis_backend.py       # Redis cache + distributed lock + in-memory fallbacks
    factory.py             # create_storage_backend(), create_cache_backend(), create_lock()
  skill_*.py               # Skill lifecycle (registry, loader, generator, security, cleanup)
  embedder.py              # Shared VLLMEmbedder + cosine_similarity singleton
  message_store.py         # Message bus (FTS5 + vector search, pluggable storage)
  message_types.py         # Message, MessageType, payload dataclasses, validate_metadata
  memory_store.py          # Long-term memory with citations, BM25 + vector search, pluggable storage
  spending_tracker.py      # Per-agent cost tracking with budgets + circuit breaker, pluggable storage
  artifact_store.py        # Result cache with TTL + LRU eviction, pluggable storage
  messenger_client.py      # Mattermost + Slack REST clients
  paperclip_client.py      # Paperclip REST client
  resource_discovery.py    # Hardware introspection (startup + real-time GPU probing)
  resource_allocator.py    # Resource planning + runtime GPU headroom
vibe/                      # Library layer (backends, core utilities)
  backends/                # vLLM, OpenAI, Anthropic
tests/                     # ~2970 tests across 48 files
```

## Design Decisions

- **Prompt-based adapters only** — LoRA infrastructure was removed (never operational). Task specialization is via system prompts + skills.
- **Defense-in-depth for skills** — AST+regex content scanning, runtime tool permission enforcement, SHA-256 integrity, container isolation. Each layer is independent.
- **Local-first LLM** — vLLM is the default and production-tested backend. OpenAI and Anthropic cloud backends are implemented for hybrid/fallback deployment.
- **Paperclip owns orchestration** — No standalone K8s manifests. Paperclip handles scheduling, pod lifecycle, environment injection.
- **Per-agent skill isolation** — Each agent has its own skill volume. No cross-agent skill sharing by design.
- **Simulation is hardware-gated** — MiroFish-style simulation adapts to available VRAM. Sidecar disabled on constrained GPUs (<=24GB); clarification simulation always runs (GPU idle). No wasted inference budget.
- **Storage is pluggable** — SQLite for local dev, PostgreSQL + Redis for production multi-node. All stores accept optional `storage_backend` param. Switch via env var, zero code changes.
- **Backend pool for resilience** — Multi-backend failover with per-backend circuit breakers. Transparent to the adapter layer — specialists don't know which backend served their request.
