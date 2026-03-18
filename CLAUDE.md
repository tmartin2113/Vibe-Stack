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
- **Specialist** (`agents/nodes.py`) — executes code generation with tool access (Python executor, pytest, bandit, file I/O). Single-specialist or multi-specialist (sub-task decomposition).
- **Critic** (`agents/nodes.py`) — scores output 0-100. Below threshold → refinement loop. Above → done.

### Key Subsystems

| System | Files | Purpose |
|--------|-------|---------|
| **LLM Backend** | `agents/llm_backend.py`, `vibe/backends/` | Ollama (default), vLLM, llama.cpp, OpenAI, Anthropic, Google |
| **Tool System** | `agents/tools/` | 5 default tools + extended dev/SEO tools. OpenSandbox or subprocess execution. |
| **Skill Security** | `agents/skill_security.py` | Name/path/content validation, AST+regex scanning, runtime tool permission enforcement, SHA-256 integrity |
| **Skill Reinforcement** | `agents/skill_generator.py`, `agents/skill_outcome_store.py`, `agents/skill_cleanup.py` | Closed-loop: outcomes recorded → RAG retrieval → generation → self-refinement for low scores |
| **Session Store** | `agents/session_store.py` | SQLite + WAL. Daemon-mode only. TTL-based cleanup. |
| **Messenger Client** | `agents/messenger_client.py` | MattermostClient + SlackClient. Used by daemon and API key prompting. |
| **Resource Discovery** | `agents/resource_discovery.py`, `agents/resource_allocator.py` | CPU/RAM/GPU introspection → resource plans for sandbox pool sizing |
| **Sandbox** | `agents/sandbox/` | OpenSandbox Docker containers with GPU passthrough. Toggle: `VIBE_SANDBOX_BACKEND=opensandbox\|subprocess` |
| **LLM Retry** | `agents/llm_retry.py` | Exponential backoff with jitter, Retry-After header support, per-node and workflow timeouts |
| **Task Type Registry** | `agents/task_type_registry.py` | Unified registry of builtin + skill-defined types. Router, orchestrator, and LLM classifier all read from it |
| **Workflow Factory** | `agents/workflow_factory.py` | Cached LLM backend + 16 adapters across heartbeat runs. Lazy init on first `run_workflow()` |
| **Heartbeat Hardening** | `agents/heartbeat_progress.py`, `agents/heartbeat_signals.py` | Progress comments at key nodes, graceful SIGTERM with partial result posting |
| **WebSocket Client** | `agents/ws_client.py` | Push-based Paperclip events via WS. Auto-reconnect with backoff. Used by orchestrator POLL and cancellation watcher |

## Execution Modes

1. **Heartbeat** (`python -m agents.main --heartbeat`) — Paperclip-driven. Fetches one task, runs workflow, posts result, exits.
2. **Daemon** (`python -m agents.main --daemon`) — Polls Mattermost/Slack for @mentions. Multi-threaded workers. Session persistence.
3. **Interactive** (`python -m agents.main`) — Single-request CLI mode.
4. **Doctor** (`python -m agents.main --doctor`) — Health checks for LLM, sandbox, hardware, connectivity.

## Paperclip Integration

### Adapter (`paperclip-adapter/`)

TypeScript process adapter. Paperclip calls `execute()` which spawns the Python heartbeat as a subprocess.

- `src/server/execute.ts` — main entry point. Env injection, subprocess management, result parsing, Slack bridge.
- `src/server/slack-notifier.ts` — sends clarification DMs to humans. Returns `{channelId, messageTs}` for reply polling.
- `src/server/slack-reply-poller.ts` — polls Slack thread for human replies. Forwards to Paperclip as issue comment.
- `src/server/parse.ts` — extracts JSON result from heartbeat stdout.
- `src/shared/config.ts` — adapter config shape (`VibeAdapterConfig`).

### Clarification Flow (Two-Way Slack Bridge)

```
Spec builder emits clarification_needed
  → heartbeat posts questions to Paperclip (status=blocked)
  → adapter sends Slack DM to human (threaded)
  → adapter polls Slack thread for reply (up to 5 min)
  → reply forwarded to Paperclip as issue comment
  → Paperclip wakes agent with WAKE_REASON=issue_comment_mentioned
  → heartbeat detects resume, injects reply into workflow context
  → workflow re-runs with clarification
```

Human can also reply directly on the Paperclip issue (bypasses Slack bridge, same resume path).

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
- `agents/workflow_factory.py` — cached LLM backend + 16 adapter instances across heartbeat runs

### Docker Compose (`docker/docker-compose.paperclip.yml`)

Infrastructure only: Paperclip + vLLM + OpenSandbox. The agent is NOT a long-running service — Paperclip spawns agent containers on-demand via the process adapter when tasks arrive. Each invocation runs one heartbeat and exits. Task type is passed per-invocation via `VIBE_TASK_TYPE`.

## Development

### Running Tests

```bash
# Python tests
python -m pytest tests/ -x

# TypeScript adapter tests (requires tsx)
cd paperclip-adapter && node --import tsx --test src/server/slack-notifier.test.ts
```

### Test Coverage

~1798 tests across 19 test files covering all major subsystems:
- Heartbeat (142), workflow factory (9), lazy sandbox (4), heartbeat metrics (36), task type registry (22)
- Skill reinforcement (49), routing (62), security (135), session store (38)
- API key manager (39), retry/timeout (73), resource discovery (22), allocator (30)
- Sandbox integration (50), tool system (100), LLM backends (72), messenger client (75)
- Paperclip client (60), orchestrator (90), WebSocket client (26)
- Adapter: notifier + reply poller (17)

### Environment Variables

See `.env.example` for all configurable values. Key ones:

| Variable | Purpose | Default |
|----------|---------|---------|
| `VIBE_SANDBOX_BACKEND` | `opensandbox` or `subprocess` | `subprocess` |
| `PAPERCLIP_API_URL` | Paperclip control plane URL | — |
| `PAPERCLIP_AGENT_ID` | Agent identity for self-comment filtering | — |
| `VIBE_SLACK_BOT_TOKEN` | Two-way Slack bridge bot token | — |
| `VIBE_SLACK_REPLY_TIMEOUT` | Seconds to poll for Slack reply (0 = notify only) | `300` |

### Project Structure

```
agents/                    # Main agent pipeline
  graph.py                 # Workflow state machine
  nodes.py                 # All workflow nodes (router, specialist, critic, etc.)
  state.py                 # TypedDict state definition
  config.py                # SystemConfig, WorkflowConfig, GenerationConfig
  heartbeat.py             # Paperclip heartbeat mode
  heartbeat_progress.py    # Progress updates to Paperclip
  heartbeat_signals.py     # Graceful SIGTERM handling
  workflow_factory.py      # Cached LLM backend + adapter setup
  ws_client.py             # WebSocket client for Paperclip push events
  daemon.py                # Mattermost/Slack polling mode
  llm_backend.py           # LLM abstraction (local + cloud)
  llm_retry.py             # Retry with exponential backoff
  adapters.py              # PromptAdapter, AdapterRegistry, all system prompts
  task_type_registry.py    # Unified builtin + skill task type registry
  router.py                # Task-type classification (reads from registry)
  tools/                   # Tool registry + implementations
  sandbox/                 # OpenSandbox Docker integration
  skill_*.py               # Skill lifecycle (registry, loader, generator, security, cleanup)
  messenger_client.py      # Mattermost + Slack REST clients
  paperclip_client.py      # Paperclip REST client
  resource_discovery.py    # Hardware introspection
  resource_allocator.py    # Resource planning
vibe/                   # Library layer (backends, core utilities)
  backends/                # Ollama, vLLM, llama.cpp, OpenAI, Anthropic, Google
paperclip-adapter/         # TypeScript Paperclip adapter
  src/server/              # execute, parse, slack-notifier, slack-reply-poller
tests/                     # ~1772 tests
```

## Design Decisions

- **Prompt-based adapters only** — LoRA infrastructure was removed (never operational). Task specialization is via system prompts + skills.
- **Defense-in-depth for skills** — AST+regex content scanning, runtime tool permission enforcement, SHA-256 integrity, container isolation. Each layer is independent.
- **Local-first LLM** — Ollama is the default and only production-tested backend. Cloud backends are code-complete but untested in production.
- **Paperclip owns orchestration** — No standalone K8s manifests. Paperclip handles scheduling, pod lifecycle, environment injection.
- **Per-agent skill isolation** — Each agent has its own skill volume. No cross-agent skill sharing by design.
- **Training data collection is passive** — `training_collector.py` writes SFT/DPO JSONL files that nothing reads yet. Kept for future fine-tuning.
