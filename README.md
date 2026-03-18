# Vibe

Production-grade multi-agent system purpose-built for [Paperclip](https://paperclip.dev). Each agent handles one task — code, tests, security, research — and Paperclip handles the rest: scheduling, retries, multi-agent coordination, and human-in-the-loop.

```
Paperclip (control plane)
     │
     ├── heartbeat ──→ Vibe Agent (code)          ──→ generates code
     ├── heartbeat ──→ Vibe Agent (test)          ──→ writes tests
     ├── heartbeat ──→ Vibe Agent (security)      ──→ security audit
     ├── heartbeat ──→ Vibe Agent (research)      ──→ research & analysis
     └── heartbeat ──→ Vibe Agent (orchestrator)  ──→ decomposes complex tasks
                              │                           across specialists,
                              ├── DECOMPOSE                aggregates results
                              ├── POLL
                              └── AGGREGATE
```

### Why Vibe + Paperclip

Vibe is designed around Paperclip's primitives — issues, comments, statuses, and heartbeats. Instead of reinventing orchestration, it delegates entirely:

- **Paperclip assigns work** — Vibe never polls for tasks. Each container invocation runs one heartbeat, processes one task, and exits.
- **Paperclip owns lifecycle** — no PID files, no supervisor, no restart logic. Paperclip spawns containers on demand and kills them when done.
- **Issues are the API** — results, clarification questions, progress updates, and partial outputs on SIGTERM all flow through Paperclip issue comments.
- **Stateless by design** — all agent state lives in the Paperclip issue tree. A killed container can be retried with zero data loss because the issue still holds the context.

This means Vibe stays small (workflow engine + LLM glue) while Paperclip handles the hard parts of production orchestration.

## What's Inside

### Multi-Agent Workflow

Each agent runs the same workflow engine. Paperclip assigns tasks; the agent handles everything else:

- **Intent classification** — conversational, research, planning, or task execution
- **Unified task type registry** — 12 built-in types + skill-defined custom types in a single registry. Skills with regex patterns get ~1ms classification without LLM calls
- **Hybrid routing** — regex + semantic matching to select specialists, reading from the unified registry
- **Critic-driven refinement** — iterative quality improvement (threshold: 85/100)
- **Task decomposition** — breaks complex requests into specialist subtasks using the full type vocabulary (built-in + skill-defined)
- **Skill system** — 3-tier architecture with multi-source ingestion, reinforcement learning, and security hardening
- **Docker sandboxing** — OpenSandbox integration with GPU passthrough
- **LLM retry + timeouts** — exponential backoff with jitter, per-node and workflow-level timeouts

### Production Hardening

Built for real workloads where agents run unattended:

- **Progress updates** — posts Paperclip comments at key workflow nodes ("Specialist iteration 2/3, score: 72") so users see live status instead of silence
- **Clarification resume** — when a human answers a clarification question, the workflow skips spec-building and jumps straight to routing/execution. No wasted compute.
- **Graceful SIGTERM** — when Paperclip kills the container, the agent catches the signal, posts partial results (output, score, last step) to the issue, and sets it to blocked for retry
- **Startup validation** — validates config (model name, `PAPERCLIP_API_URL`, `PAPERCLIP_AGENT_ID`) before any API calls. Fails fast with a clear message instead of crashing mid-workflow
- **Lazy sandbox init** — defers Docker container pre-warming to first tool execution, cutting 10-30s off the startup critical path
- **Cached factory** — `WorkflowFactory` reuses the LLM backend and 16 adapter instances across workflow runs instead of recreating them per invocation

### Orchestrator Agent

The orchestrator coordinates multi-agent work via Paperclip's issue hierarchy:

1. **DECOMPOSE** — Analyzes a complex parent issue, creates child subtasks, assigns each to a specialist agent
2. **POLL** — Monitors child progress. Auto-retries blocked children (configurable). Blocks parent for human review if retries exhausted
3. **AGGREGATE** — Collects completed child outputs, combines via AggregatorNode (merge/sequential/report strategies), posts unified result

All orchestration state is derived from Paperclip issue state — no external storage needed.

## Deployment

Vibe agents run as Paperclip process adapters. Paperclip handles scheduling, task assignment, and multi-agent coordination. Each agent specializes in a task type and runs in heartbeat mode.

### Docker Compose (Recommended)

```bash
docker compose -f docker/docker-compose.paperclip.yml up
```

Starts: Paperclip server + Ollama (shared LLM) + Vibe agents. GPU passthrough via NVIDIA Container Toolkit.

### Single Agent (Manual)

```bash
export PAPERCLIP_API_URL="http://localhost:3100"
export PAPERCLIP_API_KEY="your-key"
export VIBE_TASK_TYPE="code"

python -m agents.main --heartbeat
```

### Health Check

```bash
python -m agents.doctor
```

Checks hardware, LLM backends, sandbox availability, and GPU status.

## Architecture

### Agent Workflow

```
Heartbeat Trigger (from Paperclip)
     |
     v
[Validate Config] ──→ fail-fast if model/URL/agent ID missing
     |
     v
[Fetch Assignments] ──→ [Checkout Issue] ──→ [Build Context]
     |
     v
[Install SIGTERM Handler] ──→ captures partial state on container kill
     |
     v
[Clarification Resume?] ──yes──→ inject spec, skip spec-building
     |
     no
     |
     v
[Orchestrator?] ──yes──→ run_orchestrator_heartbeat()
     |
     no
     |
     v
+-----------------------+
| Intent Classifier     |  Conversational / Research / Planning / Task
+-----------------------+
     |
     +--- Conversational ---> Research Mode (Web Search) / Planning Mode
     |
     +--- Task Execution ---> Router (Hybrid: Regex + Semantic)
                                    |
                    +---------------+---------------+
                    |                               |
              Single Specialist              Multi-Specialist
                    |                               |
              Skill Discovery               Decomposition
                    |                               |
              Specialist --→ Generate      Specialist 1 ... N
                    |        (progress →         |
              Critic ──→ Score  Paperclip)   Aggregator
                    |                               |
              Refinement (if < 85)           Final Output
                    |
              Final Output
     |
     v
[Post Results to Paperclip] ──→ [Report Costs] ──→ Exit
```

### Skill System

Skills are how agents acquire domain knowledge. Three-tier architecture with multi-source ingestion:

**Skill Sources** (3 vetted repositories):

| Source | Repository | Trust Level | Content |
|--------|-----------|-------------|---------|
| Anthropics | `anthropics/skills` | High | Official skill collection |
| Superpowers | `obra/superpowers` | Standard | 14 methodology skills: TDD, debugging, planning |
| Vercel | `vercel-labs/agent-skills` | Standard | React best practices, web design |

**Skill Tiers**:
- **Official** — downloaded from vetted sources, cached locally with integrity hashing
- **Local** — promoted from temp after consistent high scores (avg >= 85 over 3+ uses)
- **Temp** — ephemeral, LLM-generated for unknown task types with reinforcement learning

**Security**: SHA-256 integrity hashing (TOFU), regex + AST content scanning, runtime tool permission enforcement, bundled script blocking on critical findings.

**Reinforcement**: Outcome store records (skill, score, feedback) per workflow. Skills scoring < 70 trigger self-refinement with critic feedback. RAG retrieval feeds generation with top positive + negative examples.

### Sandboxed Execution

Code execution runs in Docker containers via OpenSandbox when available:

- **PythonExecutor** — sandboxed Python with timeout (30s)
- **PytestRunner** — sandboxed pytest with coverage (60s)
- **BanditScanner** — sandboxed security scanning

GPU passthrough supported (NVIDIA). Docker and OpenSandbox are required — all code execution is containerized.

### LLM Resilience

- **Retry**: Exponential backoff with full jitter on transient failures (429/500/502/503/504). Respects `Retry-After` headers. Default: 3 retries, 1s base delay.
- **Node timeout**: Per-node execution cap (default: 120s). Prevents hung LLM calls from blocking workers.
- **Workflow timeout**: Total workflow budget (default: 600s). Ensures agents are never blocked indefinitely.

## Configuration

### Environment Variables

```bash
# Paperclip connection (injected by adapter, or set manually)
export PAPERCLIP_API_URL="http://localhost:3100"
export PAPERCLIP_API_KEY="your-key"

# Agent identity
export VIBE_TASK_TYPE="code"            # code, test_generation, security_audit, research, orchestrator

# LLM backend (inside containers)
export VIBE_BACKEND=ollama              # ollama, llamacpp, vllm, openai, anthropic, google
export VIBE_BACKEND_PORT=11434
export VIBE_MODEL=qwen3.5:7b

# Cloud API keys (optional, for cloud backends)
export OPENAI_API_KEY="sk-..."
export ANTHROPIC_API_KEY="sk-ant-..."
export GOOGLE_API_KEY="AI..."

# Sandbox
export VIBE_SANDBOX_URL=http://opensandbox:8080

# Quality control
export VIBE_QUALITY_THRESHOLD=85        # Score needed to pass (default: 85)
export VIBE_MAX_ITERATIONS=3            # Max refinement loops (default: 3)

# Logging
export LOG_LEVEL=INFO
export LOG_TO_FILE=true
```

### Agent Task Types

| Task Type | Role | What It Does |
|-----------|------|-------------|
| `code` | Software engineer | General code generation |
| `test_generation` | QA engineer | Unit/integration tests |
| `security_audit` | Security engineer | Vulnerability assessment |
| `research` | Research analyst | Research and analysis |
| `orchestrator` | Coordinator | Decomposes tasks, coordinates specialists, aggregates results |

## Project Structure

```
Vibe/
|
+-- Paperclip Adapter (TypeScript)
|   +-- paperclip-adapter/
|       +-- src/server/execute.ts       # Process adapter: spawns heartbeat
|       +-- src/server/parse.ts         # Stdout JSON parser
|       +-- src/server/slack-notifier.ts # Clarification DM sender
|       +-- src/server/slack-reply-poller.ts # Reply polling
|       +-- src/shared/config.ts        # Adapter config schema
|
+-- Multi-Agent System (Python)
|   +-- agents/
|   |   +-- main.py                     # Entry point (--heartbeat flag)
|   |   +-- heartbeat.py                # Paperclip heartbeat lifecycle
|   |   +-- heartbeat_progress.py       # Progress updates to Paperclip
|   |   +-- heartbeat_signals.py        # Graceful SIGTERM handling
|   |   +-- workflow_factory.py         # Cached LLM backend + adapter setup
|   |   +-- orchestrator.py             # Fan-out/fan-in orchestrator
|   |   +-- paperclip_client.py         # Paperclip REST API client
|   |   +-- graph.py                    # Workflow state machine
|   |   +-- nodes.py                    # Node implementations
|   |   +-- state.py                    # AgentState definition
|   |   +-- config.py                   # System configuration
|   |   +-- intent_classifier.py        # Intent detection (4 intents)
|   |   +-- task_type_registry.py        # Unified builtin + skill task types
|   |   +-- router.py                   # Hybrid regex + semantic routing
|   |   +-- adapters.py                 # Prompt-based adapters
|   |   +-- aggregator.py              # Multi-specialist output aggregation
|   |   +-- llm_backend.py             # Unified backend wrapper
|   |   +-- llm_retry.py              # Retry utility (backoff + jitter)
|   |   +-- doctor.py                   # Health checks
|   |   +-- resource_discovery.py       # Hardware auto-discovery
|   |   +-- resource_allocator.py       # Resource planning
|   |   +-- tools/                      # Tool registry + implementations
|   |   +-- sandbox/                    # OpenSandbox Docker integration
|   |   +-- skill_registry.py          # 3-tier registry, multi-source ingestion
|   |   +-- skill_loader.py            # Progressive disclosure, script execution
|   |   +-- skill_generator.py         # LLM-driven generation with RAG
|   |   +-- skill_security.py          # Security (regex + AST + TOFU)
|   |   +-- skill_cleanup.py           # Usage tracking, auto-promotion
|   |   +-- skill_outcome_store.py     # JSONL-backed reinforcement memory
|   +-- vibe/
|       +-- backends/                   # LLM backend implementations (vLLM, base)
|
+-- Infrastructure
|   +-- docker/
|   |   +-- docker-compose.paperclip.yml  # Full Paperclip stack
|   |   +-- Dockerfile.vibe-agent      # Agent container image
|   +-- docker-compose.yml                # Standalone stack
|   +-- sandbox/                          # OpenSandbox config + GPU Dockerfile
|
+-- Tests
    +-- tests/                           # ~1772 tests
```

## Testing

```bash
# All tests
python -m pytest tests/ -v

# Paperclip integration
python -m pytest tests/test_paperclip_client.py -v    # Paperclip client (60 tests)
python -m pytest tests/test_heartbeat.py -v            # Heartbeat mode (142 tests)
python -m pytest tests/test_orchestrator.py -v         # Orchestrator agent (56 tests)
python -m pytest tests/test_workflow_factory.py -v     # Cached factory (9 tests)
python -m pytest tests/test_sandbox_lazy.py -v         # Lazy sandbox init (4 tests)
python -m pytest tests/test_task_type_registry.py -v   # Task type registry (22 tests)

# Core systems
python -m pytest tests/test_skill_security.py -v       # Security hardening (135 tests)
python -m pytest tests/test_skill_reinforcement.py -v  # Reinforcement pipeline (49 tests)
python -m pytest tests/test_skill_registry.py -v       # Skill registry (48 tests)
python -m pytest tests/test_routing_layer.py -v        # Routing layer (62 tests)
python -m pytest tests/test_tool_system.py -v          # Tool system (100 tests)
python -m pytest tests/test_sandbox_integration.py -v  # Sandbox integration (50 tests)
python -m pytest tests/test_retry_and_timeout.py -v    # LLM retry + timeouts (73 tests)
python -m pytest tests/test_llm_backends.py -v         # LLM backends (72 tests)
python -m pytest tests/test_messenger_client.py -v     # Messenger clients (75 tests)
python -m pytest tests/test_resource_discovery.py -v   # Hardware discovery (22 tests)
python -m pytest tests/test_resource_allocator.py -v   # Resource allocation (30 tests)
python -m pytest tests/test_session_store.py -v        # Session persistence (38 tests)
python -m pytest tests/test_api_key_manager.py -v      # API key management (39 tests)

# TypeScript adapter tests
cd paperclip-adapter && node --import tsx --test src/server/*.test.ts
```

## System Requirements

**Recommended (Docker Compose stack):**
- Docker with NVIDIA Container Toolkit (for GPU passthrough)
- 8GB+ RAM
- NVIDIA GPU with 4GB+ VRAM (optional, for GPU inference)
- Python 3.14+

**Cloud backends:** No GPU required — just an API key.

## Troubleshooting

**Health check:**
```bash
python -m agents.doctor  # Checks hardware, backends, sandbox, GPU
```

**Router selecting wrong specialist:**
- Run with `LOG_LEVEL=DEBUG` to see routing decisions
- Check specialist keywords in `agents/router.py`

**Heartbeat not picking up tasks:**
- Verify `PAPERCLIP_API_URL` and `PAPERCLIP_API_KEY` are set
- Check agent is registered and active in Paperclip dashboard
- Check `VIBE_TASK_TYPE` matches the agent's role

## Tech Stack

- **Orchestration**: Paperclip (scheduling + task assignment) + custom state machine graph (workflow execution)
- **Adapter**: TypeScript process adapter (`paperclip-adapter/`)
- **LLM Backends**: Ollama (default), llama.cpp, vLLM, OpenAI, Anthropic, Google
- **Routing**: Regex + LLM hybrid matching
- **Adapters**: Prompt-based adapters with task-specific generation configs
- **Skills**: 3-tier registry with 3 vetted sources, reinforcement learning, security hardening
- **Sandboxing**: OpenSandbox (Docker) with GPU passthrough, subprocess fallback
- **Tools**: Dev tools + SEO tools with runtime permission enforcement
- **Persistence**: SQLite (sessions), JSONL (skill outcomes)
- **Language**: Python 3.14+ (agents), TypeScript (adapter)
- **Model**: Qwen 3.5 7B (default)
- **Testing**: ~1772 tests (Python + TypeScript)

## License

MIT License - see LICENSE file for details.

## Acknowledgments

- [Paperclip](https://paperclip.dev) - Agent orchestration platform
- [Qwen Team](https://github.com/QwenLM/Qwen) - Open-source models
- [OpenSandbox](https://github.com/nichochar/open-sandbox) - Docker sandboxing
- [Anthropic Skills](https://github.com/anthropics/skills) - Official skill collection
- [Obra Superpowers](https://github.com/obra/superpowers) - Methodology skills
- [Vercel Agent Skills](https://github.com/vercel-labs/agent-skills) - Web development skills
