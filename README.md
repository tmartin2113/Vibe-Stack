# Vibe Stack

A self-hosted autonomous agent network for software development — deploy a team of AI agents that plan, code, test, and ship, with humans approving every commit.

- **Multi-model agents** — Claude Opus/Sonnet for coding, Qwen 3.5 via vLLM for research and planning
- **Approval-gated commits** — agents build in staging; humans review and approve before anything ships
- **Network isolation** — four Docker networks with iptables egress filtering; agents can't phone home
- **Skill ecosystem** — extensible skills from Anthropic, Vercel, VoltAgent, and OpenClaw catalogs
- **One-command setup** — `sudo ./setup.sh` handles Docker, Caddy, secrets, firewall, and systemd services
- **Staging previews** — 20 isolated preview ports (8100-8119) behind Tailscale TLS with strict CSP
- **Self-hosted search** — SearXNG for web research without third-party API keys
- **Inter-agent memory** — shared fact store with TF-IDF relevance scoring; agents learn from each other across sessions
- **Workflow automation** — n8n for orchestrating pipelines, notifications, and integrations

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Host (Ubuntu + Tailscale + Caddy)                              │
│                                                                 │
│  ┌──────────────┐   ┌──────────────────────────────────────┐    │
│  │ vLLM (GPU)   │   │  Docker Compose                      │    │
│  │ :8000        │   │                                      │    │
│  │ Qwen 3.5-9B  │◄──┤  ┌────────┐  ┌──────────────────┐   │    │
│  └──────────────┘   │  │   db   │  │  deerflow-langgraph│  │    │
│                     │  │ Postgres│  │  Agent Runtime     │  │    │
│  ┌──────────────┐   │  └────────┘  └──────────────────┘   │    │
│  │ Caddy        │   │  ┌────────┐  ┌──────────────────┐   │    │
│  │ :443  → :3100│──►│  │ server │  │  deerflow-gateway │  │    │
│  │ :5678 → :5678│   │  │Paperclip│  │  REST API         │  │    │
│  │ :8100-8119   │   │  └────────┘  └──────────────────┘   │    │
│  └──────────────┘   │  ┌────────┐  ┌─────────┐ ┌───────┐ │    │
│                     │  │  n8n   │  │ searxng  │ │ssh-   │ │    │
│  ┌──────────────┐   │  │Workflows│  │ Search  │ │relay  │ │    │
│  │ Tailscale    │   │  └────────┘  └─────────┘ └───────┘ │    │
│  │ Mesh VPN     │   │  ┌────────────┐  ┌──────────────┐   │    │
│  └──────────────┘   │  │ dev-runner  │  │  watchtower  │   │    │
│                     │  │ Staging Apps│  │  Auto-update  │   │    │
│                     │  └────────────┘  └──────────────┘   │    │
│                     └──────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

### Services

| Service | Purpose | Port | Network(s) |
|---------|---------|------|------------|
| **db** | PostgreSQL 17 for Paperclip | internal | agent-core-net |
| **server** | Paperclip control plane + UI | 3100 | agent-core-net, internet-access, host-access |
| **deerflow-langgraph** | DeerFlow agent runtime (LangGraph) | 2024 | agent-core-net, internet-access, host-access, git-relay-net |
| **deerflow-gateway** | DeerFlow REST gateway | 8001 | agent-core-net, internet-access, host-access |
| **postgres-n8n** | PostgreSQL 16 for n8n | internal | agent-core-net |
| **n8n** | Workflow automation | 5678 | agent-core-net, internet-access |
| **searxng** | Self-hosted web search | 8080 | agent-core-net, internet-access |
| **ssh-relay** | TCP forwarder for git SSH to GitHub | 22 | git-relay-net, internet-access |
| **dev-runner** | Staging app host for human review | 8100-8119, 9000 | agent-core-net, host-access |
| **watchtower** | Auto-update running containers (daily) | — | — |

### Networks

```
┌─────────────────────────────────────────────────────────────┐
│ agent-core-net (192.168.90.0/24)          INTERNAL ONLY     │
│ All services communicate here. No host/internet access.     │
├─────────────────────────────────────────────────────────────┤
│ internet-access (192.168.91.0/24)         RESTRICTED EGRESS │
│ Outbound TCP 443/80, DNS, SSH only. Everything else dropped.│
├─────────────────────────────────────────────────────────────┤
│ host-access (192.168.92.0/24)             HOST SERVICES     │
│ Reaches vLLM on host via host.docker.internal. Same egress. │
├─────────────────────────────────────────────────────────────┤
│ git-relay-net                             ISOLATED GIT      │
│ Only deerflow-langgraph ↔ ssh-relay. No other connectivity. │
└─────────────────────────────────────────────────────────────┘
```

### Agent Workflow

```
  Task Created ──► CEO assigns to agent ──► Agent works in staging
       │                                          │
       │                                    ┌─────▼─────┐
       │                                    │ Preview at │
       │                                    │ :8100-8119 │
       │                                    └─────┬─────┘
       │                                          │
       │                                  ┌───────▼───────┐
       │                                  │ Human reviews  │
       │                                  │ in Paperclip   │
       │                                  └───────┬───────┘
       │                                    ┌─────▼─────┐
       │                               ┌────┤  Approve?  ├────┐
       │                               │    └───────────┘    │
       │                          ┌────▼────┐          ┌────▼────┐
       └──────────────────────────│  Commit  │          │  Reset  │
                                  │  & Push  │          │  Stage  │
                                  └─────────┘          └─────────┘
```

---

## Prerequisites

### Hardware

- **CPU**: 8+ cores recommended
- **RAM**: 32 GB+ (vLLM alone needs ~18 GB for Qwen 3.5-9B)
- **GPU**: NVIDIA GPU with CUDA support (for vLLM inference)
- **Storage**: 200 GB+ SSD (model weights, Docker images, workspace)

### Accounts

- **Tailscale** — mesh VPN for secure access (free tier works)
- **GitHub** — GHCR for container images + SSH deploy keys for agent repos

### Operating System

- Ubuntu 22.04+ (setup.sh handles all package installation)
- `setup.sh` installs: Docker CE, NVIDIA Container Toolkit, Caddy, fail2ban, auditd, and all other dependencies

---

## Quick Start

```bash
# 1. Clone and enter the repo
git clone https://github.com/YOUR_ORG/vibe-stack.git
cd vibe-stack

# 2. Copy and configure environment
cp .env.example .env
# Edit .env — set GIT_USER and GHCR_ORG at minimum

# 3. Run setup (installs everything, generates secrets, starts services)
sudo ./setup.sh

# 4. Create your admin account
#    Open https://your-tailscale-hostname in a browser and register

# 5. Authenticate Claude inside the server container
docker compose exec -it server claude login

# 6. Bootstrap the agent organization
#    Set PAPERCLIP_ADMIN_PASSWORD in .env, then:
node bootstrap.js        # Creates company + CEO agent
node bootstrap-org.js    # Creates CTO, DevOps, SWE, QA, UX agents
```

After bootstrap, save the printed Company ID and CEO Agent ID back to your `.env` file.

---

## Configuration

### Environment Variables

| Variable | Description | Required | Default |
|----------|-------------|----------|---------|
| `GIT_USER` | GitHub username (used by workspace watchdog) | Yes | — |
| `GHCR_ORG` | GitHub org/user hosting Docker images | Yes | — |
| `TAILSCALE_HOSTNAME` | Tailscale FQDN (e.g., `vibe.tailnet.ts.net`) | No | auto-detected |
| `TAILSCALE_IP` | Tailscale IPv4 address | No | auto-detected |
| `VLLM_API_URL` | vLLM OpenAI-compatible API endpoint | No | `http://host.docker.internal:8000/v1` |
| `WORKSPACE_PATH` | Agent project workspace directory | No | `/srv/sftp/workspace/files` |
| `PAPERCLIP_ADMIN_PASSWORD` | Admin password (for bootstrap scripts) | For bootstrap | — |
| `PAPERCLIP_COMPANY_ID` | Company UUID (output of bootstrap.js) | For scripts | — |
| `PAPERCLIP_CEO_ID` | CEO agent UUID (output of bootstrap.js) | For scripts | — |
| `PROJECTS_DIR` | Agent working directory | No | `/srv/sftp/workspace/files` |
| `PAPERCLIP_SOURCE_DIR` | Local Paperclip checkout (dev builds only) | No | — |

### Secrets

Generated automatically by `setup.sh` and stored in `secrets/` (chmod 700):

| Secret | Purpose |
|--------|---------|
| `better_auth_secret.txt` | BetterAuth session encryption |
| `agent_jwt_secret.txt` | JWT signing for agent tokens |
| `paperclip_postgres_password.txt` | Paperclip database password |
| `n8n_postgres_user.txt` | n8n database user |
| `n8n_postgres_password.txt` | n8n database password |
| `searxng_secret.txt` | SearXNG encryption key |
| `github_token.txt` | GitHub token for git operations |
| `ssh/` | SSH deploy keys (per-repo) |

---

## Security

### Network Isolation

All container egress is filtered by iptables. The `internet-access` and `host-access` networks only allow:
- TCP 443 (HTTPS), TCP 80 (HTTP)
- UDP/TCP 53 (DNS)
- TCP 22 (SSH — for git only)
- Everything else is **dropped**

The `agent-core-net` is fully internal — no host or internet access. The `git-relay-net` isolates git SSH traffic so only `deerflow-langgraph` and `ssh-relay` can communicate.

### Additional Hardening

- **Docker secrets** — credentials stored as files, never as environment variables in plaintext
- **SSH deploy keys** — per-repo read/write access; no personal access tokens
- **Caddy rate limiting** — 500 req/min for Paperclip, 300 req/min for n8n
- **fail2ban** — SSH brute-force protection
- **auditd** — workspace file access logging, Docker socket monitoring, privilege escalation tracking
- **Approval-gated commits** — agents cannot push directly; humans review every change
- **Strict CSP on staging** — `default-src 'self'` prevents data exfiltration from preview apps
- **Security headers** — HSTS, X-Frame-Options, X-Content-Type-Options on all routes

---

## Helper Scripts

| Script | Description | Key Env Vars |
|--------|-------------|--------------|
| `setup.sh` | First-time deployment (idempotent, run as root) | `GIT_USER`, `GHCR_ORG` |
| `teardown.sh` | Reverse setup for clean re-deployment | — |
| `iptables-setup.sh` | Re-apply firewall rules (run after Docker restart) | — |
| `bootstrap.js` | Create company + CEO agent | `PAPERCLIP_ADMIN_PASSWORD` |
| `bootstrap-org.js` | Create full org (CTO, DevOps, SWE, QA, UX) | `PAPERCLIP_ADMIN_PASSWORD`, `PAPERCLIP_COMPANY_ID` |
| `create-task.js` | Create a task and assign to CEO | `PAPERCLIP_ADMIN_PASSWORD`, `PAPERCLIP_CEO_ID` |
| `check-status.mjs` | Show live agent runs and recent issues | `PAPERCLIP_ADMIN_PASSWORD` |
| `add-ux-designer.js` | Add UX Designer agent to existing org | `PAPERCLIP_ADMIN_PASSWORD`, `PAPERCLIP_COMPANY_ID` |
| `test-adapters.js` | Test claude_local and deerflow adapter envs | — |
| `fetch-openclaw-skills.mjs` | Download OpenClaw skill catalog | — |
| `refresh-claude-token.mjs` | Refresh Claude OAuth access token | — |

---

## Inter-Agent Memory

All agents share a persistent memory layer built into the DeerFlow runtime. Facts learned during any agent's session are stored in a shared `memory.json` file and automatically injected into every agent's context on subsequent interactions.

### How It Works

```
  Agent conversation ──► MemoryMiddleware extracts last 3 turns
                                │
                         ┌──────▼──────┐
                         │  Debounced   │  (30s queue, per-thread dedup)
                         │  fact queue  │
                         └──────┬──────┘
                                │
                         ┌──────▼──────┐
                         │ LLM extracts │  Facts with category + confidence
                         │ new facts    │
                         └──────┬──────┘
                                │
                         ┌──────▼──────┐
                         │ memory.json  │  Shared across all agents
                         └──────┬──────┘
                                │
                    ┌───────────▼───────────┐
                    │  Next agent invocation │
                    │  TF-IDF ranks facts    │
                    │  by conversation topic  │
                    │  Injects as <memory>   │
                    └───────────────────────┘
```

### Context-Aware Fact Selection

Facts are ranked using a combined score before injection:

```
score = (TF-IDF similarity to current conversation × 0.6) + (confidence × 0.4)
```

- **Similarity (60%)** — cosine similarity between fact content and the last 3 turns of conversation (user messages + final AI responses, excluding tool calls)
- **Confidence (40%)** — LLM-assigned confidence score (0-1)

This means different facts surface depending on what the agent is currently working on. An agent debugging a Python test gets Python/pytest facts; an agent setting up infrastructure gets Docker/deployment facts.

### Fact Categories

Each extracted fact is tagged with a category:

| Category | Example |
|----------|---------|
| `preference` | "User prefers pytest over unittest" |
| `knowledge` | "Project uses FastAPI with SQLAlchemy" |
| `context` | "Currently refactoring the auth module" |
| `behavior` | "Always run linting before committing" |
| `goal` | "Target: 90% test coverage by end of sprint" |

### Configuration

In DeerFlow's `config.yaml`:

```yaml
memory:
  enabled: true
  injection_enabled: true
  max_injection_tokens: 2000
  debounce_seconds: 30
  max_facts: 100
  fact_confidence_threshold: 0.7
  similarity_weight: 0.6
  confidence_weight: 0.4
```

### Memory API

The DeerFlow Gateway exposes memory endpoints:

| Endpoint | Description |
|----------|-------------|
| `GET /api/memory` | View all stored facts and context |
| `POST /api/memory/reload` | Force reload from disk |
| `GET /api/memory/config` | Current memory configuration |
| `GET /api/memory/status` | Config + data combined |

---

## Development Setup

To build images locally from source instead of pulling from GHCR:

```bash
# 1. Copy the override template
cp docker-compose.override.yml.example docker-compose.override.yml

# 2. Set the path to your local Paperclip checkout
export PAPERCLIP_SOURCE_DIR=/path/to/paperclip

# 3. Rebuild and start
docker compose up -d --build
```

### CI Pipeline

The GitHub Actions workflow (`.github/workflows/docker-publish.yml`) runs on push to `main`:

| Job | Timeout | Builds |
|-----|---------|--------|
| **ssh-relay** | 10 min | `./ssh-relay/Dockerfile` → `ghcr.io/{org}/vibe-ssh-relay` |
| **dev-runner** | 15 min | `./dev-runner/Dockerfile` → `ghcr.io/{org}/vibe-dev-runner` |
| **server** | 30 min | `paperclipai/paperclip` → `ghcr.io/{org}/paperclip-server` |
| **deerflow** | 20 min | `bytedance/deer-flow` → `ghcr.io/{org}/paperclip-deerflow` |

All images are tagged with both the Git SHA and `latest`.

---

## Health Monitoring

Run the health checker manually:

```bash
node tools/vibe-health/index.js          # Table output
node tools/vibe-health/index.js --json   # JSON output
node tools/vibe-health/index.js --log /var/log/vibe-health.log  # Append JSONL
```

Checks performed:

| Service | Method | Endpoint |
|---------|--------|----------|
| Paperclip | HTTP | `http://127.0.0.1:3100/api/health` |
| vLLM | HTTP | `http://127.0.0.1:8000/v1/models` |
| DeerFlow LangGraph | Docker exec | `localhost:2024/ok` |
| DeerFlow Gateway | Docker exec | `localhost:8001/health` |
| PostgreSQL | Docker exec | `pg_isready` |
| Docker Containers | Docker ps | All containers running |

### Install as systemd timer

```bash
cd tools/vibe-health
sudo ./install.sh
```

This creates a timer that runs the health check every 5 minutes, logging results to `/var/log/vibe-health.log` in JSONL format. Exit code 0 means all services are up; exit code 1 means at least one service is down.

---

## Troubleshooting

### Port conflicts

`setup.sh` checks for conflicts on startup. If a port is in use:

```bash
sudo lsof -i :3100   # Find the process using the port
sudo kill <PID>       # Free it up
sudo ./setup.sh       # Re-run setup
```

### GHCR image pull failures

Make sure you're authenticated with the GitHub Container Registry:

```bash
echo $GITHUB_TOKEN | docker login ghcr.io -u $GIT_USER --password-stdin
```

Check that your `GHCR_ORG` in `.env` matches the org/user where images are published.

### Claude authentication

If Claude auth expires inside the server container:

```bash
docker compose exec -it server claude login
```

To refresh an OAuth token programmatically:

```bash
node refresh-claude-token.mjs
```

### vLLM / GPU issues

Verify NVIDIA drivers and the container toolkit:

```bash
nvidia-smi                          # Check GPU is visible
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi  # Check Docker GPU access
```

Ensure vLLM is running on the host and listening on port 8000:

```bash
curl http://127.0.0.1:8000/v1/models
```

### iptables rules lost after Docker restart

Docker resets iptables chains when the daemon restarts. Re-apply the firewall rules:

```bash
sudo ./iptables-setup.sh
```

This is also handled by the `vibe-iptables` systemd service, which runs automatically after Docker starts.

---

## Contributing

1. Fork the repo
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes
4. Push to your fork and open a Pull Request

CI auto-publishes Docker images to GHCR on push to `main`, so merged PRs deploy automatically via Watchtower.

---

## License

[MIT](LICENSE) — compatible with upstream dependencies (Paperclip: MIT, DeerFlow: MIT).
