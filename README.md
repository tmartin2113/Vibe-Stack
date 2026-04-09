<p align="center">
  <strong>Vibe Stack</strong>
</p>

<p align="center">
  <em>Your autonomous software engineering team. One command to deploy.</em>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <a href="#testing"><img src="https://img.shields.io/badge/tests-3000%2B%20passing-brightgreen.svg" alt="Tests: 3000+ passing"></a>
  <a href="#system-requirements"><img src="https://img.shields.io/badge/platform-Linux-orange.svg" alt="Platform: Linux"></a>
  <a href="https://paperclip.dev"><img src="https://img.shields.io/badge/orchestration-Paperclip-purple.svg" alt="Orchestration: Paperclip"></a>
</p>

---

Five senior engineers powered by Claude Opus, each paired with a DeerFlow research assistant running on local GPU inference. [Paperclip](https://paperclip.dev) handles orchestration -- scheduling, retries, multi-agent coordination, and human-in-the-loop review.

```
               You (CEO)
                    |
                   CTO --- cto-assistant
              /     |        \        \
      Backend    Frontend     QA     DevOps
        |            |         |       |
      backend-   frontend-   qa-    devops-
      assistant  assistant  asst.  assistant

      -- Claude Opus (API) --   -- Qwen 3.5 9B (local vLLM) --
         5 senior engineers          5 research assistants
```

You create an issue. The CTO decomposes it, dispatches research to free local-GPU assistants, then delegates implementation to Opus-powered engineers. Each engineer wakes with research context already gathered, executes, and posts results. The CTO reviews, requests fixes if needed, and pushes the feature branch.

## Key Features

- **Zero-config setup** -- one command installs, configures, and bootstraps the full 10-agent org
- **Stateless agents** -- all state lives in the Paperclip issue tree; killed containers retry with zero data loss
- **Cost-optimized** -- Opus for deep reasoning, free local vLLM for research; engineers are rate-limited to 1-2 web lookups and must delegate broad research
- **Self-improving** -- agents detect recurring quality issues and propose source code changes to themselves, gated by a 5-stage safety pipeline with human review
- **Cross-run learning** -- each heartbeat persists spec, output, and critic feedback to a per-agent memory store with importance + 30-day decay; future runs recall scoped context via BM25 + semantic hybrid
- **Role-based tool filtering** -- each agent only sees tools relevant to their role
- **3000+ tests** across 52 files covering all major subsystems

---

## Quick Start

### Prerequisites

| Requirement | Notes |
|-------------|-------|
| **Linux** | Ubuntu/Debian, Fedora/RHEL/CentOS, Arch/Manjaro, or openSUSE |
| **Docker Engine 24+** | With Compose v2 |
| **Tailscale** | [Install](https://tailscale.com/download) and run `tailscale up` |
| **NVIDIA GPU** *(optional)* | 8GB+ VRAM with [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) for local inference |

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

<details>
<summary><strong>Local Development</strong></summary>

Build the Paperclip server and DeerFlow from local source instead of GHCR images:

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
# Set PAPERCLIP_SOURCE_DIR in .env to your local paperclip checkout
docker compose up -d --build
```
</details>

---

## How It Works

### Task Flow

```
1. You create an issue in the Paperclip UI
2. CTO wakes -> creates feature branch -> writes ARCHITECTURE.md -> creates subtask pairs
3. Assistants wake -> run pre-flight research (unrestricted web search) -> post findings
4. Engineers wake with context -> implement (rate-limited to 1-2 web lookups) -> post results
5. CTO wakes -> reviews all work -> creates fix subtasks if needed -> pushes feature branch
```

### The Team

<table>
<tr>
<th colspan="3">Senior Engineers (Claude Opus)</th>
</tr>
<tr><td><strong>CTO</strong></td><td>Architect & reviewer</td><td>Task decomposition, code review, branch management</td></tr>
<tr><td><strong>Sr. Backend</strong></td><td>Server-side</td><td>APIs, databases, auth, integrations</td></tr>
<tr><td><strong>Sr. Frontend</strong></td><td>Client-side</td><td>UI components, styling, UX, browser logic</td></tr>
<tr><td><strong>Sr. QA</strong></td><td>Quality & security</td><td>Test plans, security audits, coverage analysis</td></tr>
<tr><td><strong>Sr. DevOps</strong></td><td>Infrastructure</td><td>Docker, CI/CD, deployment, monitoring</td></tr>
</table>

<table>
<tr>
<th colspan="3">Research Assistants (local vLLM -- zero API cost)</th>
</tr>
<tr><td><code>cto-assistant</code></td><td>CTO</td><td>Architecture research, codebase exploration</td></tr>
<tr><td><code>backend-assistant</code></td><td>Sr. Backend</td><td>API docs, library examples, best practices</td></tr>
<tr><td><code>frontend-assistant</code></td><td>Sr. Frontend</td><td>Component libraries, CSS patterns, framework docs</td></tr>
<tr><td><code>qa-assistant</code></td><td>Sr. QA</td><td>Testing strategies, security checklists, coverage tools</td></tr>
<tr><td><code>devops-assistant</code></td><td>Sr. DevOps</td><td>Docker best practices, CI/CD patterns, infra docs</td></tr>
</table>

### Self-Upgrade Pipeline

Agents detect recurring quality issues and propose improvements to their own source code:

```
Signal accumulation -> Threshold (3+ signals) -> 5-gate safety pipeline -> Human review
                                                  |
                                                  |-- Path validation (agents/ only)
                                                  |-- Diff size (<500 lines)
                                                  |-- Full pytest suite
                                                  |-- Bandit security scan
                                                  |-- Critic scoring (>=90)
```

Proposals appear in the Paperclip **Improvements** section with branch name and review instructions.

---

## Architecture

### Workflow Engine

Each agent runs a deterministic state machine with **13 built-in task types**:

```
Router -> Skill Loader -> Spec Builder -> Specialist -> Critic -+
                                             ^                   |
                                             |    score < 85     |
                                             +-------------------+
```

- **Hybrid routing** -- regex + semantic matching selects the right specialist adapter
- **Critic-driven refinement** -- iterative quality improvement (threshold: 85/100)
- **Task decomposition** -- complex requests split into parallel specialist subtasks
- **5000+ skills** -- auto-loaded per task type from 5 curated sources
- **Docker sandboxing** -- OpenSandbox with GPU passthrough

### LLM Backends

| Backend | Type | Auth |
|---------|------|------|
| vLLM *(default)* | Local GPU inference | None |
| OpenAI | Cloud API | `OPENAI_API_KEY` |
| Anthropic | Cloud API | `ANTHROPIC_API_KEY` |

Multi-backend failover with per-backend circuit breakers via `BackendPool`. Transparent to the agent layer.

### Infrastructure Services

| Service | Purpose | Port |
|---------|---------|------|
| **Paperclip** | Control plane + UI | 3100 |
| **vLLM** | Local model inference | 8000 |
| **DeerFlow** | Research assistant backend | 2024, 8001 |
| **SearXNG** | Self-hosted web search | 8888 |
| **Gitea** | Git hosting | 3000 |
| **MinIO** | S3-compatible object storage | 9000 |
| **Penpot** | Design tool | 9001 |
| **Playwright** | Browser automation | 3003 |
| **OpenSandbox** | Code execution sandbox | 9090 |
| **MiroFish** | Multi-agent simulation | 5001 |
| **PaddleOCR** | OCR text/layout extraction | 8868 |
| **Caddy** | TLS reverse proxy | 443 |

> For detailed subsystem documentation, see [docs/architecture.md](docs/architecture.md).

---

## Configuration

See `.env.example` for all configurable values. Key variables:

| Variable | Purpose | Default |
|----------|---------|---------|
| `VLLM_MODEL` | Local inference model | Auto-detected by GPU VRAM |
| `GH_TOKEN` | GitHub PAT for agent git push | -- |
| `VIBE_SANDBOX_BACKEND` | `opensandbox` or `subprocess` | `subprocess` |
| `VIBE_STORAGE_BACKEND` | `sqlite` or `postgres` | `sqlite` |
| `VIBE_CACHE_BACKEND` | `memory` or `redis` | `memory` |
| `LOG_LEVEL` | Logging verbosity | `WARNING` |

Infrastructure service URLs (`SEARXNG_URL`, `MIROFISH_URL`, `PADDLEOCR_URL`, etc.) are auto-configured by `setup.sh`. See [docs/architecture.md](docs/architecture.md#configuration-reference) for the full configuration reference.

---

## Paperclip Skill Management

The Paperclip server container (`vibe-stack-server-1`) loads its adapter skills from `$PAPERCLIP_SKILLS_DIR`, which the published server image sets to `/app/skills`. You can override it via `docker-compose.yml` to bind-mount a host directory for runtime skill swapping — e.g. to drop customized `SKILL.md` files into a volume that outlives container rebuilds.

Two operator helpers live in `scripts/`:

| Script | Purpose |
|---|---|
| `scripts/check-paperclip-skills.sh` | Verify the active skill dir, list loaded skills, and warn about stale npx caches. Safe to run anytime. |
| `scripts/cleanup-paperclip-skill-cache.sh` | Purge stale `/paperclip/.npm/_npx/*` caches left by earlier Paperclip setup steps. Dry-run by default; pass `--apply` to delete. |

Typical first-time cleanup:

```bash
./scripts/check-paperclip-skills.sh                    # inspect current state
./scripts/cleanup-paperclip-skill-cache.sh --apply     # purge ~hundreds of MB of stale cache
./scripts/check-paperclip-skills.sh                    # confirm the warning clears
```

Both scripts honor `SERVER_CONTAINER=<name>` if your container is named differently.

---

## Testing

```bash
# Full suite (~3000 tests across 52 files)
python -m pytest tests/ -x -m "not e2e" --no-header -q
```

<details>
<summary><strong>Run specific subsystems</strong></summary>

```bash
python -m pytest tests/test_heartbeat.py -v          # Heartbeat lifecycle (142 tests)
python -m pytest tests/test_skill_security.py -v     # Security hardening (142 tests)
python -m pytest tests/test_tool_system.py -v        # Tool system (157 tests)
python -m pytest tests/test_mirofish_tool.py -v      # MiroFish simulation (11 tests)
python -m pytest tests/test_ocr_tool.py -v           # OCR tool (21 tests)
python -m pytest tests/test_memory_store.py -v       # Long-term memory (167 tests)
python -m pytest tests/test_message_store.py -v      # Message bus (107 tests)
```
</details>

---

## System Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| RAM | 16 GB | 32 GB+ |
| GPU VRAM | 8 GB (4B model) | 22 GB+ (9B model) |
| CPU | 4 cores | 16 cores |
| Disk | 50 GB | 100 GB+ |
| OS | Any systemd-based Linux | Ubuntu 24.04, Fedora 41+ |

No GPU required for cloud-only mode (Claude/OpenAI backends).

---

## Troubleshooting

```bash
# Health check
python -m agents.doctor

# Service status
docker compose ps

# Agent logs
docker compose logs --tail 30 vibe

# DeerFlow assistant logs
docker compose logs --tail 30 deerflow-langgraph deerflow-gateway

# Re-run setup (idempotent)
sudo ./setup.sh
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.

## Acknowledgments

[Paperclip](https://paperclip.dev) |
[DeerFlow](https://github.com/bytedance/deer-flow) |
[Qwen](https://github.com/QwenLM/Qwen) |
[MiroFish](https://github.com/666ghj/MiroFish) |
[PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) |
[Impeccable](https://github.com/pbakaus/impeccable) |
[OpenSandbox](https://github.com/nichochar/open-sandbox) |
[Anthropic Skills](https://github.com/anthropics/skills) |
[Obra Superpowers](https://github.com/obra/superpowers) |
[Vercel Agent Skills](https://github.com/vercel-labs/agent-skills) |
[VoltAgent OpenClaw](https://github.com/voltagent/awesome-openclaw-skills)
