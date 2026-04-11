# Vibe Stack Operations Runbook

## Quick Reference

| Action | Command |
|--------|---------|
| Start core services | `make up` |
| Start everything | `make up-all` |
| Check health | `make status` |
| Tail logs | `make logs` or `make logs SVC=vibe` |
| Restart a service | `make restart SVC=deerflow-langgraph` |
| Rebuild a service | `make rebuild SVC=vibe` |
| Run tests | `make test` |
| Open shell | `make shell SVC=deerflow-langgraph` |
| Build DeerFlow image | `make build-deerflow` |

## First-Time Setup

```bash
# 1. Clone and configure
git clone https://github.com/tmartin2113/Vibe-Stack.git
cd Vibe-Stack
cp .env.example .env
./setup.sh  # auto-detects GPU, generates secrets

# 2. Start core services
make up

# 3. (Optional) Start infrastructure
make up-all

# 4. Verify health
make status
# All services should show (healthy)
```

## Deploying Updates

### Update Vibe Agent (code changes in this repo)

```bash
make rebuild SVC=vibe
```

### Update DeerFlow (code changes in Paperclip repo)

```bash
# Option A: Use CI-built image (recommended)
# 1. Push changes to Paperclip master
# 2. CI builds and pushes new image
# 3. Pull and restart
docker compose pull deerflow-langgraph deerflow-gateway
make restart SVC=deerflow-langgraph
make restart SVC=deerflow-gateway

# Option B: Build locally
make build-deerflow
make restart SVC=deerflow-langgraph
make restart SVC=deerflow-gateway
```

### Update Paperclip Server

```bash
docker compose pull server
make restart SVC=server
```

### Update DeerFlow Config (config.yaml or extensions_config.json)

Config files are bind-mounted. Changes require a container restart, not a rebuild:

```bash
# Edit the config
nano deerflow/config.yaml

# Restart to pick up changes
make restart SVC=deerflow-langgraph
make restart SVC=deerflow-gateway
```

### Pin to a Specific Image Version

```bash
# In .env, change:
PAPERCLIP_VERSION=v1.2.3  # or sha-abc123f

# Then restart
make restart SVC=server
make restart SVC=deerflow-langgraph
make restart SVC=deerflow-gateway
```

## Monitoring

### Dashboards

- **Grafana**: http://localhost:3333 (admin / vibe)
  - "Vibe Stack Overview" dashboard: service health, heartbeat runs, workflow duration, token spend, error rate
- **Prometheus**: http://localhost:9091
  - Targets: http://localhost:9091/targets

### Health Probes

Prometheus scrapes these every 30s via blackbox exporter:

| Service | Probe URL | Expected |
|---------|-----------|----------|
| Paperclip Server | http://server:3100/api/health | `{"status":"ok"}` |
| DeerFlow LangGraph | http://deerflow-langgraph:2024/ok | `{"ok":true}` |
| DeerFlow Gateway | http://deerflow-gateway:8001/health | `{"status":"ok"}` |
| Vibe Agent | http://vibe:8080/healthz | `{"status":"ok"}` |

### LangSmith Tracing (optional)

Traces DeerFlow agent decisions (tool calls, reasoning, errors):

```bash
# In .env:
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_your_key_here
LANGSMITH_PROJECT=vibe-stack

# Restart DeerFlow services
make restart SVC=deerflow-langgraph
make restart SVC=deerflow-gateway
```

View traces at https://smith.langchain.com.

## Troubleshooting

### Service won't start

```bash
# Check logs for the specific service
make logs SVC=deerflow-langgraph

# Common causes:
# - Missing env var: "Set MINIO_ROOT_PASSWORD in .env"
# - Port conflict: "address already in use"
# - Image not found: run `docker compose pull`
```

### DeerFlow config errors

```bash
# Verify config loads inside the container
make shell SVC=deerflow-langgraph
cd backend && uv run python -c "from src.config.app_config import AppConfig; c = AppConfig.from_file(); print('OK:', [m.name for m in c.models])"

# Common causes:
# - YAML syntax error in config.yaml
# - Missing env var (config uses $VAR syntax)
# - Wrong module path (must be src.*, not deerflow.*)
```

### Agent heartbeat failures

```bash
# Check vibe agent logs
make logs SVC=vibe

# Check Paperclip server health
curl http://localhost:3100/api/health

# Common causes:
# - Server not healthy (agent depends_on server)
# - PAPERCLIP_API_URL not set
# - Foreign key violations (check server logs)
```

### vLLM not responding

```bash
# Check vLLM status
curl http://localhost:8000/health

# Check GPU usage
nvidia-smi

# Common causes:
# - GPU out of memory (reduce VLLM_GPU_MEMORY_UTILIZATION)
# - Model not downloaded yet (check vllm logs)
# - Wrong VLLM_MODEL or VLLM_API_URL in .env
```

### Prometheus targets showing DOWN

```bash
# Check which targets are down
curl -s http://localhost:9091/api/v1/targets | python3 -c "
import sys, json
for t in json.load(sys.stdin)['data']['activeTargets']:
    if t['health'] != 'up':
        print(f\"{t['labels'].get('instance', t['labels']['job'])}: {t['lastError'][:100]}\")"

# Common causes:
# - Service hasn't started yet (wait for start_period)
# - Service crashed (check `make logs SVC=<name>`)
# - Network issue (services must be on the same Docker network)
```

### Container keeps restarting

```bash
# Check exit code
docker inspect vibe-stack-vibe-1 --format='{{.State.ExitCode}}'

# Note: vibe agent (exit code 0) restarts by design — it runs heartbeat mode
# (start → execute one task → exit → restart). This is normal.

# For other services, check logs:
make logs SVC=<service-name>
```

## Backup & Recovery

### Persistent Volumes

| Volume | Service | Contains |
|--------|---------|----------|
| `paperclip-data` | server | Paperclip database + files |
| `vibe-data` | vibe | Agent state |
| `palace-data` | vibe, deerflow | MemPalace long-term memory |
| `graphify-data` | vibe, deerflow | Codebase knowledge graph |
| `bulletin-data` | vibe | Inter-agent messages |
| `gitea-data` | gitea | Git repositories |
| `minio-data` | minio | Object storage |
| `prometheus-data` | prometheus | Metrics (30-day retention) |
| `grafana-data` | grafana | Dashboard state |

### Backup

```bash
# Stop services first for consistency
make down

# Backup all volumes
for vol in $(docker volume ls -q | grep vibe-stack); do
  docker run --rm -v "$vol":/data -v "$(pwd)/backups":/backup \
    alpine tar czf "/backup/$vol-$(date +%Y%m%d).tar.gz" -C /data .
done

make up-all
```

### Restore

```bash
make down
docker run --rm -v "vibe-stack_paperclip-data":/data -v "$(pwd)/backups":/backup \
  alpine sh -c "cd /data && tar xzf /backup/vibe-stack_paperclip-data-20260411.tar.gz"
make up-all
```

## Resource Requirements

| Profile | CPU | RAM | Disk | GPU |
|---------|-----|-----|------|-----|
| Minimal (core only) | 4 | 8 GB | 20 GB | None |
| Standard (core + infra) | 8 | 16 GB | 40 GB | None |
| Full (all + GPU) | 8+ | 32 GB | 80 GB | 20+ GB VRAM |
