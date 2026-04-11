# Resource-Aware Orchestrator & Agent Bootstrap

**Date:** 2026-04-11
**Status:** Proposed
**Scope:** Container bootstrap, credential management, resource-aware scheduling

## Problem

Vibe Stack's 10-agent org exists in Paperclip but cannot execute autonomously because:

1. Agent instruction files are not available inside containers
2. Claude Code credentials do not survive container recreation
3. Agent IDs are not set — the heartbeat process doesn't know which agent it is
4. No concurrency control — on constrained hardware, all agents firing simultaneously would exhaust system RAM

The public repo must work on hardware ranging from 16GB RAM (no GPU) to 128GB+ (multi-GPU) without requiring users to manually tune orchestration.

## Design

### Principle: Same Org, Hardware Determines Throughput

All 10 agents always exist in Paperclip regardless of hardware. The user experience (tagging agents in issues, seeing delegation happen) is identical on a 16GB laptop and a 128GB workstation. The difference is speed: more resources means more agents run concurrently, tasks complete faster.

### Architecture Overview

```
docker compose up vibe
       │
       ▼
┌─────────────────────────────────────────────┐
│  vibe container                             │
│                                             │
│  orchestrator_main.py                       │
│    ├── agent_registry.resolve_all()         │
│    │     → GET /api/companies/{id}/agents   │
│    │     → {role: uuid} map                 │
│    ├── concurrency_budget.calculate()       │
│    │     → probe RAM/CPU → max_slots        │
│    └── scheduler.run()                      │
│          ├── poll Paperclip for pending     │
│          ├── priority queue                 │
│          └── spawn/reap subprocesses        │
│                                             │
│  Subprocesses (up to max_slots):            │
│  ┌──────────────────────────────────┐       │
│  │ python -m agents.main            │       │
│  │   --heartbeat                    │       │
│  │   --agent-id <uuid>              │       │
│  │   --instructions /opt/vibe/...   │       │
│  │                                  │       │
│  │ fetch task → pipeline → post →   │       │
│  │ exit 0                           │       │
│  └──────────────────────────────────┘       │
│                                             │
│  Baked instructions: /opt/vibe/instructions/│
│  Mounted secrets: /home/vibe/.claude (ro)   │
│  Mounted secrets: /run/secrets (ro)         │
└─────────────────────────────────────────────┘
```

---

## Section 1: Container Bootstrap

### Baked Instructions (Build Time)

The Dockerfile copies all agent instruction directories into the image at a well-known path:

```
/opt/vibe/instructions/
├── cto/
│   └── AGENTS.md
├── cto-assistant/
│   └── AGENTS.md
├── backend-engineer/
│   └── AGENTS.md
├── backend-assistant/
│   └── AGENTS.md
├── frontend-engineer/
│   └── AGENTS.md
├── frontend-assistant/
│   └── AGENTS.md
├── devops-engineer/
│   └── AGENTS.md
├── devops-assistant/
│   └── AGENTS.md
├── qa-engineer/
│   └── AGENTS.md
├── qa-assistant/
│   └── AGENTS.md
└── shared/
    └── cto-instructions.md (and other shared docs)
```

Instructions are versioned with the code. A specific image version always has known instructions.

### Mounted Secrets (Run Time)

```yaml
# docker-compose.yml
vibe:
  volumes:
    - ${HOME}/.claude:/home/vibe/.claude:ro
    - ./secrets:/run/secrets:ro
  environment:
    - PAPERCLIP_API_URL=http://server:3100
    - PAPERCLIP_ADMIN_EMAIL=${PAPERCLIP_ADMIN_EMAIL}
    - PAPERCLIP_ADMIN_PASSWORD=${PAPERCLIP_ADMIN_PASSWORD}
    - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
    - VLLM_API_URL=http://host.docker.internal:8000/v1
```

Credentials are read-only mounts. Never baked into the image. The existing `secrets/` directory and SOPS encryption remain unchanged.

### Agent ID Resolution (Startup)

The orchestrator resolves all agent IDs dynamically on boot:

```
GET /api/companies/{companyId}/agents
→ Response: [{id: "uuid-1", name: "CTO", role: "cto"}, ...]
→ Internal map: {"cto": "uuid-1", "backend-engineer": "uuid-2", ...}
```

No hardcoded UUIDs. If `bootstrap-org.cjs` recreates agents (new UUIDs), the orchestrator picks up the new IDs on next restart.

### Optional Override Directory

```
/opt/vibe/overrides/  (mounted volume, empty by default)
```

If a file exists at `/opt/vibe/overrides/<role>/AGENTS.md`, it takes precedence over the baked version. This allows hot-editing instructions in development without rebuilding the image. In production, this directory is empty.

---

## Section 2: Resource-Aware Scheduler

### Concurrency Budget

On startup, the orchestrator probes available resources:

```python
available_ram_gb = total_ram_gb - INFRA_RESERVE_GB  # reserve ~10GB for other containers
slot_cost_gb = 1.5  # estimated peak per subprocess (Python + tools + buffers)
max_slots = max(1, floor(available_ram_gb / slot_cost_gb))
max_slots = min(max_slots, cpu_cores - 2, 10)  # cap at org size, leave CPU headroom
```

| System RAM | Available | Max Concurrent |
|---|---|---|
| 16GB | ~6GB | 2 |
| 32GB | ~18GB | 4 |
| 64GB | ~50GB | 10 (all) |
| 128GB+ | ~110GB | 10 (capped at org size) |

### Priority Queue

Agents have static priorities based on role:

```
Priority 0 (highest): CTO — unblocks downstream work
Priority 1: Senior Engineers (backend, frontend, devops, qa) — produce deliverables
Priority 2: Research Assistants (all 5) — background prep, can wait
```

When a slot opens, the highest-priority agent with a pending task gets it. Within the same priority level, agents are served in the order their tasks were created (FIFO).

### Scheduler Loop

```
Every 30 seconds:
  1. Poll Paperclip: GET /api/companies/{id}/issues?status=open&assignee=*
     → Identify which agents have assigned, unstarted tasks
  2. For each agent with work, enqueue in priority queue (skip if already running or queued)
  3. While slots_available > 0 AND queue not empty:
     - Pop highest priority agent
     - Spawn: python -m agents.main --heartbeat --agent-id <uuid> --instructions <path>
     - Register PID, start timestamp in active pool
  4. Reap finished subprocesses (waitpid WNOHANG), free slots
  5. Kill subprocesses exceeding VIBE_AGENT_TIMEOUT (default 600s)
  6. If system memory pressure > VIBE_MEMORY_PRESSURE_THRESHOLD (default 90%):
     - Pause spawning until pressure drops below 80%
     - Log warning
```

### Subprocess Isolation

Each subprocess is a full heartbeat execution:
- Own PID, own memory space
- Inherits environment from orchestrator (API keys, URLs)
- Receives `--agent-id` and `--instructions` as CLI args
- Standard heartbeat lifecycle: authenticate → fetch task → run pipeline → post result → exit 0
- stdout/stderr captured by orchestrator for logging

### Health & Recovery

- **Crash tracking:** If an agent's subprocess exits non-zero 3 consecutive times, mark as `unhealthy`. Skip for 5 minutes (exponential backoff: 5m → 15m → 60m).
- **Memory pressure:** Monitor `/proc/meminfo` (or `psutil`). Pause spawning at 90%, resume at 80%.
- **Graceful shutdown:** SIGTERM → forward to all children → wait 30s → SIGKILL stragglers → exit.
- **Stale task detection:** If a subprocess finishes but didn't post a result to Paperclip (crash during execution), log the failure for manual review.

---

## Section 3: Module Structure

### New Files

```
agents/orchestrator_main.py    # Entry point for --orchestrator mode
agents/scheduler.py            # Priority queue, slot pool, spawn/reap loop
agents/concurrency_budget.py   # Hardware probe → max_slots calculation
agents/agent_registry.py       # Resolve agent IDs from Paperclip API at startup
```

### Modified Files

```
agents/main.py                 # Add --orchestrator flag → delegates to orchestrator_main
agents/heartbeat.py            # Accept --agent-id and --instructions as CLI args
agents/resource_discovery.py   # Expose get_available_ram_gb(), get_cpu_cores() as public API
Dockerfile                     # Add COPY for instruction directories
docker-compose.yml             # Add volume mounts for secrets, change entrypoint to --orchestrator
```

### Unchanged

- Entire pipeline (Router → Spec → Specialist → Critic)
- `bootstrap-org.cjs` (still creates org once)
- Paperclip UI (user still tags agents, creates issues)
- DeerFlow assistants (still called via LangGraph adapter)
- All 27 tools, skill system, storage layer
- Network architecture, security rules

The scheduler is purely a supervisor layer. It does not change how agents think or work — only when they are allowed to run.

---

## Section 4: Configuration & User Experience

### Zero-Config Defaults

The orchestrator works out of the box with no user tuning:
- Concurrency: auto-detected from hardware
- Agent IDs: resolved from Paperclip API
- Instructions: baked into image at well-known path
- Polling interval: 30s
- Timeout: 600s (10 minutes)

### Optional Overrides (`.env`)

```bash
VIBE_MAX_CONCURRENT_AGENTS=3         # Override auto-detected slot count
VIBE_SCHEDULER_INTERVAL=30           # Poll interval in seconds
VIBE_AGENT_TIMEOUT=600               # Per-heartbeat timeout in seconds
VIBE_DISABLED_AGENTS=frontend-engineer,frontend-assistant  # Skip these roles
VIBE_MEMORY_PRESSURE_THRESHOLD=90    # Pause spawning above this %
VIBE_INFRA_RESERVE_GB=10             # RAM reserved for non-agent containers
```

### Logging

```
[orchestrator] Detected: 31GB RAM, 12 cores → budget: 4 concurrent slots
[orchestrator] Resolved 10 agents from Paperclip
[scheduler] Spawning cto (priority 0) → PID 4521
[scheduler] cto exited (0) in 45s — 3 slots available
[scheduler] Spawning backend-engineer (priority 1) → PID 4533
[scheduler] Spawning frontend-engineer (priority 1) → PID 4534
[scheduler] backend-engineer exceeded timeout (600s) — killed
[scheduler] backend-engineer marked unhealthy (attempt 2/3)
```

### Health Endpoint

The existing `/healthz` endpoint expands to include scheduler state:

```json
{
  "status": "healthy",
  "scheduler": {
    "slots_total": 4,
    "slots_active": 2,
    "slots_available": 2,
    "queue_depth": 3,
    "agents_healthy": 9,
    "agents_unhealthy": 1,
    "memory_pressure_pct": 67
  }
}
```

### First-Install Experience

```bash
git clone https://github.com/tmartin2113/Vibe-Stack.git
cd Vibe-Stack
cp .env.example .env     # Add API keys
./setup.sh               # Builds image (instructions baked), starts stack, bootstraps org

# Open Paperclip UI → create issue → tag @CTO
# Within 30s: orchestrator detects task, spawns CTO subprocess
# CTO architects, creates subtasks for engineers
# Next poll cycle: engineers spawn (within slot budget), begin implementation
```

No tuning required. Hardware determines speed, not capability.

---

## Success Criteria

1. `setup.sh` on a fresh machine results in agents that can autonomously execute tasks
2. On 32GB RAM: CTO delegates to engineers, work completes (staggered if needed)
3. On 16GB RAM: same workflow works, just slower (2 concurrent max)
4. No manual agent ID configuration required
5. Credential rotation (new API key in `.env`) takes effect on next container restart
6. An agent crash does not affect other running agents
7. `/healthz` accurately reports scheduler state

## Out of Scope

- Multi-machine orchestration (future: container-per-agent with Docker Swarm/K8s)
- Dynamic scaling (adding/removing agents at runtime)
- GPU scheduling for vLLM (vLLM manages its own resources independently)
- Changes to the pipeline internals (Router, Specialist, Critic)
- Changes to Paperclip UI or API
