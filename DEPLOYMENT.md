# Deployment Guide

For quick-start instructions and environment variable reference, see [README.md](README.md). This guide covers hardware sizing, production configuration, GPU tuning, monitoring, backup, and troubleshooting.

## System Requirements

### Hardware

**Minimum** (single agent, no GPU inference):
- 8 CPU cores, 32GB RAM, 100GB disk
- Cloud LLM backend (OpenAI, Anthropic) — no GPU required

**Recommended** (local vLLM inference):
- 16+ cores, 64GB RAM, 500GB SSD
- NVIDIA GPU with 24GB+ VRAM (RTX 3090, RTX 4090, A5000, A100)

**GPU VRAM tiers** — `setup.sh` auto-selects the model based on available VRAM:

| VRAM | Model |
|------|-------|
| >=48GB | Qwen/Qwen3.5-27B |
| >=24GB | Qwen/Qwen3.5-27B-GPTQ-Int4 |
| >=12GB | Qwen/Qwen3.5-9B (default) |
| >=8GB  | Qwen/Qwen3.5-4B |

The 9B fp16 model needs ~18GB for weights plus ~4GB of KV cache at `--max-num-seqs 4`. The simulation sidecar needs at least 6GB free to run alongside specialists (auto-disabled below that threshold).

### Software

- Docker 24.0+ with Compose V2 (`docker compose` not `docker-compose`)
- NVIDIA Container Toolkit (for GPU passthrough)
- Git

Verify Container Toolkit: `nvidia-smi` must work inside a container:
```bash
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi
```

## Pre-Deployment Checklist

1. Clone the repo:
   ```bash
   git clone https://github.com/tmartin2113/vibe-stack Vibe-Stack
   cd Vibe-Stack
   ```

2. Copy the env template:
   ```bash
   cp .env.example .env
   ```

3. Run `setup.sh` for automatic configuration (GPU detection, secret generation):
   ```bash
   ./setup.sh
   ```
   Or manually generate the required secrets:
   ```bash
   # Required — auth session signing
   openssl rand -hex 32   # → BETTER_AUTH_SECRET

   # Infrastructure secrets
   openssl rand -hex 32   # → SEARXNG_SECRET
   openssl rand -hex 32   # → PENPOT_SECRET_KEY
   openssl rand -hex 16   # → MINIO_ROOT_PASSWORD
   openssl rand -hex 16   # → GITEA_ADMIN_PASSWORD
   ```

4. Set required values in `.env`:
   ```bash
   PAPERCLIP_ADMIN_EMAIL=admin@example.com
   PAPERCLIP_ADMIN_PASSWORD=<strong-password>
   BETTER_AUTH_SECRET=<generated above>
   BETTER_AUTH_TRUSTED_ORIGINS=http://localhost:3100
   ```

5. Set resource limits to match your hardware:
   ```bash
   VIBE_VLLM_MEMORY_LIMIT=24G      # Match your GPU VRAM
   VIBE_SANDBOX_MEMORY_LIMIT=4G     # 2–8G depending on workload
   VIBE_AGENT_MEMORY_LIMIT=2G       # Per-agent container limit
   ```

6. Bootstrap the org (creates admin user, company, and all agents):
   ```bash
   docker compose up -d
   node bootstrap-all.js
   ```
   Sign in at `http://localhost:3100`.

## Deployment Options

### Option 1: Standalone Stack (single node, local dev)

```bash
docker compose --profile vibe-standalone --profile vllm up -d
```

Services started: `vibe` (agent), `vllm`, `opensandbox`, `tailscale`

- The `vibe-standalone` profile runs a single all-purpose agent.
- The `vllm` profile starts a local vLLM container. Omit it if you have a host vLLM process or are using a cloud backend.

### Option 2: Multi-Agent Production Stack (Paperclip)

```bash
docker compose -f docker/docker-compose.vllm.yml \
               -f docker/docker-compose.production.yml up -d
```

Services started: `paperclip`, `vllm`, `opensandbox`, six specialist agents (`vibe-code`, `vibe-test`, `vibe-security`, `vibe-research`, `vibe-orchestrator`, `vibe-self-upgrade`)

Key differences from Option 1:
- Pre-built images pulled from GHCR (`ghcr.io/tmartin2113/vibe:latest`) instead of local build
- Each agent specializes in one task type and runs in Paperclip heartbeat mode
- Resource limits, restart policies, and JSON log rotation applied via the production overlay
- Each agent has its own named skill volume (no cross-agent skill sharing)

Required `.env` variables for this option:
```bash
PAPERCLIP_API_KEY=<bearer-token>
PAPERCLIP_COMPANY_ID=<uuid>
PAPERCLIP_AGENT_ID_CODE=<uuid>
PAPERCLIP_AGENT_ID_TEST=<uuid>
PAPERCLIP_AGENT_ID_SECURITY=<uuid>
PAPERCLIP_AGENT_ID_RESEARCH=<uuid>
PAPERCLIP_AGENT_ID_ORCHESTRATOR=<uuid>
PAPERCLIP_AGENT_ID_SELF_UPGRADE=<uuid>
```
Agent UUIDs are assigned by Paperclip after bootstrap. Run `node bootstrap-all.js` once to populate them, then copy the printed UUIDs into `.env`.

### Option 3: Cloud LLM Backend (no GPU)

Set `VIBE_BACKEND` to `openai` or `anthropic` and supply an API key:
```bash
VIBE_BACKEND=openai
OPENAI_API_KEY=sk-...
```
Then start without the `vllm` profile:
```bash
docker compose --profile vibe-standalone up -d
```
No GPU or VRAM required. All GPU-related config (`VIBE_VLLM_MEMORY_LIMIT`, simulation gating) is ignored.

### Option 4: Multi-Backend Pool (failover)

For resilience, configure fallback backends. The pool tries the primary first and fails over on consecutive errors:
```bash
VIBE_FALLBACK_URLS=host2:8000,host3:8000
VIBE_BACKEND_POOL_STRATEGY=failover   # or round_robin, least_loaded
VIBE_FALLBACK_BACKEND_TYPE=vllm
```

### Option 5: Multi-Node Production (PostgreSQL + Redis)

Switch from SQLite to PostgreSQL and Redis for shared state across multiple nodes:
```bash
VIBE_STORAGE_BACKEND=postgres
VIBE_DATABASE_URL=postgresql://user:pass@host:5432/vibe
VIBE_CACHE_BACKEND=redis
VIBE_REDIS_URL=redis://host:6379/0
```
All four stores (`message_store`, `memory_store`, `spending_tracker`, `artifact_store`) route through the configured backend automatically. No code changes required.

## Health Checks

All services expose health endpoints:

| Service | Endpoint | Notes |
|---------|----------|-------|
| Agent | `http://localhost:8080/healthz` | LLM backend, disk, GPU, sandbox |
| vLLM | `http://localhost:8000/health` | Model loaded, GPU status |
| OpenSandbox | `http://localhost:9090/docs` | Docker socket reachable |
| Paperclip | `http://localhost:3100/api/health` | Control plane |

Quick check:
```bash
curl http://localhost:8080/healthz | jq
```

Full diagnostic (checks hardware, backends, sandbox, GPU, all stores):
```bash
python -m agents.doctor
```

## GPU Configuration

### VRAM Budget

| Component | VRAM | Notes |
|-----------|------|-------|
| Model weights (9B fp16) | ~18GB | Loaded once at vLLM startup |
| KV cache (`--max-num-seqs 4`) | ~4GB | Scales with concurrent sequences |
| Simulation sidecar | ~2GB | Only when free VRAM > 6GB |

### Tuning Parameters

**vLLM flags** (set in `docker-compose.yml` command or `docker/docker-compose.vllm.yml`):

```
--gpu-memory-utilization 0.92   # Fraction of VRAM for weights + KV cache
--max-num-seqs 4                # Max concurrent inference sequences
--max-model-len 12288           # Max context window (tokens)
--enable-prefix-caching         # Reuse KV cache across requests with shared prefix
--enable-chunked-prefill        # Better batching for mixed-length inputs
```

**Simulation gating** (`.env`):
```bash
VIBE_SIM_ENABLED=true                # Master switch for simulation module
VIBE_SIM_MIN_FREE_VRAM_MB=6144       # Min free VRAM (MB) for sidecar simulation
VIBE_SIM_MAX_PERSONA_ROUNDS=3        # Max stakeholder persona rounds
VIBE_SIM_MAX_TOKENS=512              # Max tokens per simulation call
```

Simulation behavior by VRAM availability:
- **Free VRAM > 6GB**: sidecar simulation enabled (runs alongside multi-specialist subtasks)
- **Free VRAM <= 6GB**: sidecar disabled; clarification simulation still runs (GPU is idle during clarification)

### Tensor Parallelism (multi-GPU)

For models larger than a single GPU's VRAM, set `VLLM_TP_SIZE` to the number of GPUs:
```bash
VLLM_TP_SIZE=2   # Splits model across 2 GPUs
```

## Monitoring

### Prometheus Metrics

The agent exposes metrics at `http://localhost:8080/metrics` (Prometheus text format). Scrape interval recommendation: 15s.

### Logs

In the production overlay, all services use the `json-file` driver with rotation:
- Max size: 50MB per file (100MB for vLLM), 5 files retained
- View live logs: `docker compose -f docker/docker-compose.vllm.yml -f docker/docker-compose.production.yml logs -f vibe-code`
- Set `LOG_FORMAT=json` and `LOG_LEVEL=WARNING` in `.env` for structured production logs

### Spending Tracker

Per-agent cost tracking with configurable budget caps:
```bash
VIBE_SPENDING_DB=/data/spending.db
VIBE_SPENDING_BUDGET=10.00           # Per-heartbeat budget cap (USD); circuit-breaks workflow on breach
```
View spending data directly: `sqlite3 /data/spending.db "SELECT * FROM spending ORDER BY created_at DESC LIMIT 20;"`

### Slack Alerts

Send agent health alerts to a Slack channel:
```bash
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

## Backup and Recovery

### SQLite Databases

```bash
# Inter-agent message bus
cp /shared/bulletin/messages.db messages_backup.db

# Long-term agent memory
cp ~/.vibe/memory.db memory_backup.db

# Spending tracker
cp /data/spending.db spending_backup.db
```

### Named Volumes

Back up any named volume without stopping services:
```bash
docker run --rm \
  -v vibe-data:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/vibe-data-$(date +%Y%m%d).tar.gz -C /data .
```

Restore:
```bash
docker run --rm \
  -v vibe-data:/data \
  -v $(pwd):/backup \
  alpine tar xzf /backup/vibe-data-20260328.tar.gz -C /data
```

### Model Cache

The `vllm-models` volume holds downloaded HuggingFace model weights (~18GB for 9B). Back up or pre-populate it to avoid re-downloading on a fresh host.

## Troubleshooting

### vLLM won't start

- Check GPU visibility: `nvidia-smi`
- Check VRAM: `nvidia-smi --query-gpu=memory.free --format=csv`
  - The 9B model needs ~22GB total (weights + KV cache at `--gpu-memory-utilization 0.92`)
- If downloading the model on first start, increase `start_period` in the healthcheck (default: 60s in `docker-compose.yml`, 120s in `docker-compose.vllm.yml`)
- Check vLLM logs: `docker compose logs vllm`

### Agent can't reach vLLM

```bash
docker compose exec vibe curl http://vllm:8000/health
```
- Both services must be on the `backend` network
- Check `VIBE_BACKEND_HOST=vllm` and `VIBE_BACKEND_PORT=8000` in the agent's environment

### Sandbox unhealthy

```bash
docker compose exec opensandbox curl http://localhost:8080/docs
```
- OpenSandbox requires `/var/run/docker.sock` mounted — verify the mount exists
- Check socket permissions: the container user needs access to the Docker socket group (`group_add: ["125"]` in `docker-compose.yml` — replace `125` with your host's docker GID: `getent group docker | cut -d: -f3`)

### GPU out of memory mid-workflow

Reduce KV cache pressure in order of impact:
1. Lower `--max-num-seqs` (default 4, try 2)
2. Disable simulation sidecar: `VIBE_SIM_ENABLED=false`
3. Lower `--gpu-memory-utilization` (default 0.92, try 0.85)
4. Reduce `--max-model-len` (default 12288, try 8192)

### Heartbeat not picking up tasks

- Verify `PAPERCLIP_API_URL`, `PAPERCLIP_API_KEY`, and `PAPERCLIP_AGENT_ID` are all set
- Check the agent is registered and active in the Paperclip dashboard
- Confirm `VIBE_TASK_TYPE` matches the task type assigned to this agent in Paperclip
- Run `python -m agents.doctor` to isolate connectivity issues

### Auth requests rejected (browser sign-in)

`BETTER_AUTH_TRUSTED_ORIGINS` must include the exact origin used in the browser:
```bash
BETTER_AUTH_TRUSTED_ORIGINS=http://localhost:3100
# For Tailscale access, also add:
BETTER_AUTH_TRUSTED_ORIGINS=http://localhost:3100,https://vibe.your-tailnet.ts.net
```

### Router selecting wrong specialist

Enable debug routing logs:
```bash
LOG_LEVEL=DEBUG docker compose up vibe
```
Routing decisions are logged at DEBUG level. Check keyword patterns in `agents/router.py` and the task type definitions in `agents/task_type_registry.py`.
