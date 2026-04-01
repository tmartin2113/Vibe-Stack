# Vibe Stack

Autonomous software engineering company in a box. Five senior engineers powered by Claude Opus, each paired with a DeerFlow research assistant running on local GPU inference. [Paperclip](https://paperclip.dev) handles orchestration — scheduling, retries, multi-agent coordination, and human-in-the-loop review.

```
             You (CEO)
                  |
                 CTO ─── cto-assistant
            /     |        \        \
    Backend    Frontend     QA     DevOps
      |            |         |       |
    backend-   frontend-   qa-    devops-
    assistant  assistant  asst.  assistant

    ── Claude Opus (API) ──   ── Qwen 3.5 9B (local vLLM) ──
       5 senior engineers          5 research assistants
```

## How It Works

You create an issue in the Paperclip UI. The CTO decomposes it into subtasks, assigns research to DeerFlow assistants (free, local GPU), then delegates implementation to senior engineers (Claude Opus). Each engineer wakes with research context already gathered, executes their task, and posts results. The CTO reviews, requests fixes if needed, and pushes the feature branch.

**Key principles:**

- **Paperclip assigns work** — agents never poll. Each heartbeat runs one task and exits.
- **Stateless by design** — all state lives in the Paperclip issue tree. Killed containers retry with zero data loss.
- **Cost-optimized** — senior engineers use Opus for deep reasoning; research assistants run free on local vLLM.
- **Convention-based pairing** — assistants are named `<role>-assistant`. No configuration needed.

## Quick Start

### Prerequisites

- Linux host (Ubuntu 22.04+)
- Docker Engine 24+ with Compose v2
- [Tailscale](https://tailscale.com/download) installed and running (`tailscale up`)
- NVIDIA GPU with [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) (optional, enables local inference)

### Install

```bash
git clone https://github.com/tmartin2113/Vibe-Stack.git
cd Vibe-Stack
sudo ./setup.sh
```

Setup runs 24 steps: system prerequisites, Docker, NVIDIA toolkit, vLLM model selection, Caddy, secrets, skill sources, SSH, Paperclip server, infrastructure services, GPU services, org bootstrap, and security hardening. Progress is displayed as `[Step N/24]`.

| Hardware | What You Get |
|----------|-------------|
| NVIDIA GPU (8GB+ VRAM) | Claude Opus engineers + local vLLM assistants + full infrastructure |
| No GPU | Claude Opus engineers only (cloud-only mode) |

After setup completes, open the Paperclip UI at the URL printed by setup to create your admin account.

### Compose Profiles

```bash
# Full stack (default with GPU)
docker compose up -d

# Core + infrastructure (no GPU services)
docker compose -f docker-compose.yml -f docker-compose.infra.yml up -d

# Core only (minimal)
docker compose up -d
```

### Local Development

To build the Paperclip server and DeerFlow from local source instead of GHCR images:

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
# Set PAPERCLIP_SOURCE_DIR in .env to your local paperclip checkout
docker compose up -d --build
```

## Org Structure

The bootstrap creates 10 agents automatically — no manual configuration required.

### Senior Engineers (Claude Opus)

| Agent | Role | Responsibilities |
|-------|------|-----------------|
| **CTO** | Architect & reviewer | Task decomposition, `ARCHITECTURE.md`, code review, branch management |
| **Sr. Backend** | Server-side | APIs, databases, auth, integrations, DeerFlow/LangGraph Python |
| **Sr. Frontend** | Client-side | UI components, styling, UX implementation, browser logic |
| **Sr. QA** | Quality & security | Test plans, test suites, security audits, coverage analysis |
| **Sr. DevOps** | Infrastructure | Docker, CI/CD, deployment, monitoring, networking |

### DeerFlow Assistants (local vLLM)

Each senior has a paired research assistant that handles pre-flight research and ad-hoc exploration. Assistants run on local GPU (Qwen 3.5 9B) at zero API cost.

| Assistant | Paired With | Focus |
|-----------|------------|-------|
| `cto-assistant` | CTO | Architecture research, codebase exploration |
| `backend-assistant` | Sr. Backend | API docs, library examples, best practices |
| `frontend-assistant` | Sr. Frontend | Component libraries, CSS patterns, framework docs |
| `qa-assistant` | Sr. QA | Testing strategies, security checklists, coverage tools |
| `devops-assistant` | Sr. DevOps | Docker best practices, CI/CD patterns, infra docs |

### Task Flow

1. **You** create an issue in the Paperclip UI
2. **CTO** wakes → creates feature branch → writes `ARCHITECTURE.md` → creates research + implementation subtask pairs
3. **Assistants** wake first → run pre-flight research → post findings
4. **Engineers** wake with context → implement → post handoff comments
5. **CTO** wakes → reviews all work → creates fix subtasks if needed → pushes feature branch

## Architecture

### Agent Workflow Engine

Each agent runs the same Python workflow engine with 13 built-in task types:

- **Hybrid routing** — regex + semantic matching to select the right specialist adapter
- **Critic-driven refinement** — iterative quality improvement (threshold: 85/100)
- **Task decomposition** — breaks complex requests into specialist subtasks
- **Skill system** — 3-tier architecture with multi-source ingestion, reinforcement learning, and security hardening
- **Docker sandboxing** — OpenSandbox integration with GPU passthrough

### Production Hardening

- **Progress updates** — live status comments on Paperclip issues
- **Graceful SIGTERM** — posts partial results on container kill, sets issue to blocked for retry
- **JWT auto-auth** — agents generate fresh JWTs from a shared secret per heartbeat (no static API keys)
- **Lazy sandbox init** — defers container pre-warming to first tool execution
- **Cached factory** — reuses LLM backend and adapter instances across heartbeat runs
- **Spending tracker** — per-agent cost tracking with configurable budget caps

### Skill Sources

| Source | Repository | Content |
|--------|-----------|---------|
| Anthropic | `anthropics/skills` | Official skill collection |
| Superpowers | `obra/superpowers` | TDD, debugging, planning methodologies |
| Vercel | `vercel-labs/agent-skills` | React, web design best practices |
| VoltAgent | `voltagent/awesome-openclaw-skills` | OpenClaw community catalog (~5000 skills) |

### LLM Backends

| Backend | Type | Auth |
|---------|------|------|
| vLLM (default) | Local GPU inference | None |
| OpenAI | Cloud API | `OPENAI_API_KEY` |
| Anthropic | Cloud API | `ANTHROPIC_API_KEY` |

### Infrastructure Services

| Service | Purpose | Port |
|---------|---------|------|
| Paperclip | Control plane + UI | 3100 |
| vLLM | Local model inference | 8000 |
| SearXNG | Self-hosted web search | 8888 |
| Gitea | Git hosting | 3000 |
| MinIO | Object storage | 9000 |
| Penpot | Design tool | 9001 |
| Playwright | Browser automation | 3003 |
| OpenSandbox | Code execution sandbox | 9090 |
| Caddy | TLS reverse proxy | 443 |

## Configuration

See `.env.example` for all configurable values. Key variables:

| Variable | Purpose | Default |
|----------|---------|---------|
| `PAPERCLIP_AGENT_JWT_SECRET` | Shared secret for agent JWT auth | Auto-generated |
| `VLLM_MODEL` | Local inference model | Auto-detected by GPU VRAM |
| `VIBE_SANDBOX_BACKEND` | `opensandbox` or `subprocess` | `subprocess` |
| `VIBE_STORAGE_BACKEND` | `sqlite` or `postgres` | `sqlite` |
| `VIBE_CACHE_BACKEND` | `memory` or `redis` | `memory` |
| `LOG_LEVEL` | Logging verbosity | `WARNING` |

## Testing

```bash
# All tests (~2970 across 48 files)
python -m pytest tests/ -x -m "not e2e" --no-header -q

# Specific subsystems
python -m pytest tests/test_heartbeat.py -v          # Heartbeat lifecycle (142 tests)
python -m pytest tests/test_skill_security.py -v     # Security hardening (142 tests)
python -m pytest tests/test_tool_system.py -v        # Tool system (157 tests)
python -m pytest tests/test_memory_store.py -v       # Long-term memory (139 tests)
python -m pytest tests/test_message_store.py -v      # Message bus (107 tests)
```

## System Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| RAM | 16GB | 32GB+ |
| GPU VRAM | 8GB (4B model) | 22GB+ (9B model) |
| CPU | 4 cores | 16 cores |
| Disk | 50GB | 100GB+ |
| OS | Ubuntu 22.04 | Ubuntu 24.04 |

No GPU required for cloud-only mode (Claude/OpenAI backends).

## Troubleshooting

```bash
# Health check
python -m agents.doctor

# Check agent status
docker compose ps

# View agent logs
docker compose logs --tail 30 vibe

# Re-run setup (idempotent)
sudo ./setup.sh
```

## License

MIT License. See [LICENSE](LICENSE) for details.

## Acknowledgments

- [Paperclip](https://paperclip.dev) — agent orchestration platform
- [Qwen](https://github.com/QwenLM/Qwen) — open-source models
- [OpenSandbox](https://github.com/nichochar/open-sandbox) — Docker sandboxing
- [Anthropic Skills](https://github.com/anthropics/skills) — official skill collection
- [Obra Superpowers](https://github.com/obra/superpowers) — methodology skills
- [Vercel Agent Skills](https://github.com/vercel-labs/agent-skills) — web development skills
- [VoltAgent OpenClaw](https://github.com/voltagent/awesome-openclaw-skills) — community skill catalog
