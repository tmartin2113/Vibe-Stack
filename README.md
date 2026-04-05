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
- **Self-improving** — agents detect recurring quality issues and propose code changes to themselves. Proposals pass a 5-gate safety pipeline (path validation, diff size, pytest, Bandit, critic scoring) and appear in the Paperclip Improvements section for human review.

## Quick Start

### Prerequisites

- Linux host (Ubuntu/Debian, Fedora/RHEL/CentOS, Arch/Manjaro, or openSUSE)
- Docker Engine 24+ with Compose v2
- [Tailscale](https://tailscale.com/download) installed and running (`tailscale up`)
- NVIDIA GPU with [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) (optional, enables local inference)

### Install

```bash
git clone https://github.com/tmartin2113/Vibe-Stack.git
cd Vibe-Stack
sudo ./setup.sh
```

Setup auto-detects your hardware, generates secrets, clones skill sources, builds service images, bootstraps the 10-agent org, and applies security hardening. Progress is displayed as `[Step N/24]`.

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
docker compose -f docker-compose.yml up -d
```

### Local Development

To build the Paperclip server and DeerFlow from local source instead of GHCR images:

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
# Set PAPERCLIP_SOURCE_DIR in .env to your local paperclip checkout
docker compose up -d --build
```

## Org Structure

The bootstrap creates 10 agents automatically — no manual configuration required. Agent roles use specialized identifiers (`cto`, `backend_engineer`, `frontend_engineer`, `qa_engineer`, `devops_engineer`, `research_assistant`) that map to role-specific tool sets.

> **Migrating existing deployments:** If your agents have generic roles (e.g., `engineer`), update them via the Paperclip DB. See migration instructions in `bootstrap-org.cjs` (lines 14-28).

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

### Agent Instructions

Agent behavior is governed by a two-tier instruction system stored in the repo:

- **`agent-instructions/`** — shared base instructions (`base-instructions.md`) plus role-specific guides (`cto-instructions.md`, `engineer-instructions.md`, `qa-instructions.md`, `devops-instructions.md`, `ux-instructions.md`, `security-instructions.md`, `pm-instructions.md`)
- **`agents/<role>/AGENTS.md`** — per-agent operational directives including output guidelines (terse 3-6 word sentences, no filler), mandatory DeerFlow delegation rules, and tool usage policies

All agents enforce strict output brevity: tool calls are capped at 500 tokens per continuation, file reads auto-cap at 200 lines, and redundant re-reads within a session are flagged.

### Task Flow

1. **You** create an issue in the Paperclip UI
2. **CTO** wakes &rarr; creates feature branch &rarr; writes `ARCHITECTURE.md` &rarr; creates research + implementation subtask pairs
3. **Assistants** wake first &rarr; run pre-flight research (unrestricted web search) &rarr; post findings
4. **Engineers** wake with research context already gathered &rarr; implement (rate-limited to 1-2 web lookups) &rarr; post handoff comments
5. **CTO** wakes &rarr; reviews all work &rarr; creates fix subtasks if needed &rarr; pushes feature branch

## Architecture

### Agent Workflow Engine

Each agent runs the same Python workflow engine with 13 built-in task types:

- **Hybrid routing** — regex + semantic matching to select the right specialist adapter
- **Critic-driven refinement** — iterative quality improvement (threshold: 85/100)
- **Task decomposition** — breaks complex requests into parallel specialist subtasks
- **Skill system** — 3-tier architecture with multi-source ingestion, reinforcement learning, and security hardening
- **Docker sandboxing** — OpenSandbox integration with GPU passthrough
- **Role-based tool filtering** — each agent only sees tools relevant to their role via `ROLE_TOOL_SETS`
- **Research delegation** — senior engineers get rate-limited `quick_lookup` (1-2 calls/session) and must delegate broad research to DeerFlow assistants

### Agent Tools

Agents have access to infrastructure tools that are automatically discovered via environment variables. All tools are role-filtered — each agent only sees tools relevant to their role via `ROLE_TOOL_SETS` in `agents/tools/registry.py`.

| Tool | Available To | Service | Purpose |
|------|-------------|---------|---------|
| QuickLookup | senior engineers | SearXNG | Rate-limited web search (1-2 calls/session, forces DeerFlow delegation) |
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

### Self-Upgrade System

Agents detect recurring quality issues across heartbeat runs and propose improvements to their own source code:

1. **Signal accumulation** — low scores, tool failures, iteration exhaustion, and critic feedback patterns are recorded
2. **Threshold trigger** — after 3+ signals for a task type, a proposal is generated
3. **Safety pipeline** — 5 gates: path validation (agents/ only), diff size (<500 lines), full pytest, Bandit security scan, critic scoring (>=90)
4. **Human review** — proposals appear in the Paperclip **Improvements** section with branch name and review instructions

### Production Hardening

- **Progress updates** — live status comments on Paperclip issues
- **Graceful SIGTERM** — posts partial results on container kill, sets issue to blocked for retry
- **JWT auto-auth** — agents generate fresh JWTs from a shared secret per heartbeat (no static API keys)
- **Lazy sandbox init** — defers container pre-warming to first tool execution
- **Cached factory** — reuses LLM backend and adapter instances across heartbeat runs
- **Spending tracker** — per-agent cost tracking with configurable budget caps
- **Billing exhaustion halt** — agents halt permanently when Anthropic billing is exhausted

### Skill Sources

| Source | Repository | Content |
|--------|-----------|---------|
| Anthropic | `anthropics/skills` | Official skill collection |
| Impeccable | `pbakaus/impeccable` | 21 design quality skills (audit, polish, typeset, etc.) |
| Superpowers | `obra/superpowers` | TDD, debugging, planning methodologies |
| Vercel | `vercel-labs/agent-skills` | React, web design best practices |
| VoltAgent | `voltagent/awesome-openclaw-skills` | OpenClaw community catalog (~5000 skills) |

Skills load and unload automatically per-task via the `SkillLoaderNode` and `SkillCleanupNode`. The skill system matches skills to task types, so frontend tasks get design skills and backend tasks don't.

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

## Configuration

See `.env.example` for all configurable values. Key variables:

| Variable | Purpose | Default |
|----------|---------|---------|
| `VLLM_MODEL` | Local inference model | Auto-detected by GPU VRAM |
| `GH_TOKEN` | GitHub PAT for agent git push | — |
| `VIBE_SANDBOX_BACKEND` | `opensandbox` or `subprocess` | `subprocess` |
| `VIBE_STORAGE_BACKEND` | `sqlite` or `postgres` | `sqlite` |
| `VIBE_CACHE_BACKEND` | `memory` or `redis` | `memory` |
| `VIBE_SKILL_REPOS` | Colon-separated skill repo paths | Auto-configured by setup.sh |
| `VIBE_FILE_READ_LINE_CAP` | Max lines returned by FileReader when no range specified | `200` |
| `VIBE_CTO_LOOKUP_LIMIT` | QuickLookup calls per session for CTO | `2` |
| `LOG_LEVEL` | Logging verbosity | `WARNING` |

Infrastructure service URLs (`SEARXNG_URL`, `MIROFISH_URL`, `PADDLEOCR_URL`, etc.) are auto-configured by `setup.sh` using Docker DNS names. See `.env.example` for the full list.

## Testing

```bash
# All tests (~3000 across 52 files)
python -m pytest tests/ -x -m "not e2e" --no-header -q

# Specific subsystems
python -m pytest tests/test_heartbeat.py -v          # Heartbeat lifecycle (142 tests)
python -m pytest tests/test_skill_security.py -v     # Security hardening (142 tests)
python -m pytest tests/test_tool_system.py -v        # Tool system (157 tests)
python -m pytest tests/test_mirofish_tool.py -v      # MiroFish simulation (11 tests)
python -m pytest tests/test_ocr_tool.py -v           # OCR tool (21 tests)
python -m pytest tests/test_memory_store.py -v       # Long-term memory (139 tests)
python -m pytest tests/test_message_store.py -v      # Message bus (107 tests)
```

## System Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| RAM | 16GB | 32GB+ |
| GPU VRAM | 8GB (4B model) | 22GB+ (9B model) |
| CPU | 4 cores | 16 cores |
| Disk | 50GB | 100GB+ (models + Docker images) |
| OS | Any systemd-based Linux | Ubuntu 24.04, Fedora 41+ |

Supported distros: Ubuntu/Debian, Fedora/RHEL/CentOS/Rocky, Arch/Manjaro, openSUSE. No GPU required for cloud-only mode (Claude/OpenAI backends).

## Troubleshooting

```bash
# Health check
python -m agents.doctor

# Check all service status
docker compose ps

# View agent logs
docker compose logs --tail 30 vibe

# Check DeerFlow assistants
docker compose logs --tail 30 deerflow-langgraph deerflow-gateway

# Re-run setup (idempotent)
sudo ./setup.sh
```

## License

MIT License. See [LICENSE](LICENSE) for details.

## Acknowledgments

- [Paperclip](https://paperclip.dev) — agent orchestration platform
- [DeerFlow](https://github.com/bytedance/deer-flow) — research assistant framework
- [Qwen](https://github.com/QwenLM/Qwen) — open-source models for local inference
- [MiroFish](https://github.com/666ghj/MiroFish) — multi-agent simulation engine
- [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) — OCR text extraction
- [Impeccable](https://github.com/pbakaus/impeccable) — design quality skills
- [OpenSandbox](https://github.com/nichochar/open-sandbox) — Docker sandboxing
- [Anthropic Skills](https://github.com/anthropics/skills) — official skill collection
- [Obra Superpowers](https://github.com/obra/superpowers) — methodology skills
- [Vercel Agent Skills](https://github.com/vercel-labs/agent-skills) — web development skills
- [VoltAgent OpenClaw](https://github.com/voltagent/awesome-openclaw-skills) — community skill catalog
