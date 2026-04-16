# VIB-58: Unified Infrastructure Health Checks

## Problem

Infrastructure services (MiroFish, PaddleOCR, SearXNG, Playwright, Penpot, MinIO, Gitea) have inconsistent health reporting. Some have HTTP endpoints, some use CLI checks, some have nothing. Prometheus only scrapes core services — infra services are invisible. There's no aggregate view and no UI to see it all at a glance.

## Design

### Standard Response Format

Every service probe normalizes to:

```json
{
  "status": "ok" | "degraded" | "error",
  "service": "mirofish",
  "version": "1.0.0",
  "uptime_seconds": 3600,
  "checks": {
    "http": "ok",
    "gpu": "ok"
  }
}
```

When a service doesn't provide version/uptime, those fields are `null`.

### Component 1: `agents/infra_health.py` (new)

Service registry mapping each service to:
- Name, internal URL, health endpoint path
- Probe function (HTTP GET, TCP socket, or CLI-based)
- Response normalizer (extracts status/version/checks from raw response)

Services and their probe strategies:

| Service | Probe URL | Normalization |
|---------|-----------|---------------|
| MiroFish | `http://mirofish:5001/health` | Map existing JSON to standard format |
| PaddleOCR | `http://paddleocr:8868/health` | Map existing JSON to standard format |
| SearXNG | `http://searxng:8080/healthz` | HTTP 200 = ok, else error |
| Playwright | `http://playwright:3003/json` | HTTP 200 = ok (returns browser list) |
| Penpot Backend | `http://penpot-backend:6060/readyz` | HTTP 200 = ok (new endpoint via reverse proxy or direct) |
| MinIO | `http://minio:9000/minio/health/live` | HTTP 200 = ok |
| Gitea | `http://gitea:3000/api/v1/version` | Extract version from JSON response |
| vLLM | `http://vllm:8000/health` | HTTP 200 = ok |
| Neo4j | `neo4j:7687` via TCP | Socket connect = ok |
| Prometheus | `http://prometheus:9090/-/healthy` | HTTP 200 = ok |
| Grafana | `http://grafana:3000/api/health` | Map existing JSON |

Key behaviors:
- All probes run concurrently with `asyncio.gather` (3s timeout per probe)
- Failed probes return `{"status": "error", "service": "...", "error": "connection refused"}`
- Module exposes `check_all() -> dict` and `check_service(name) -> dict`

### Component 2: Aggregate Endpoints in `agents/metrics.py`

Two new routes on the existing Vibe health server:

- `GET /api/infrastructure/health` — calls `check_all()`, returns:
  ```json
  {
    "status": "ok" | "degraded" | "error",
    "timestamp": "2026-04-15T12:00:00Z",
    "services": { ... per-service results ... },
    "summary": { "total": 11, "ok": 9, "degraded": 1, "error": 1 }
  }
  ```
  Overall status: "ok" if all ok, "degraded" if any degraded, "error" if any error.

- `GET /api/infrastructure/health/{service}` — calls `check_service(name)`, returns single service result.

### Component 3: Docker Healthcheck for Penpot

Add to `docker-compose.infra.yml`:

```yaml
penpot-backend:
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:6060/readyz"]
    interval: 30s
    timeout: 10s
    retries: 3
    start_period: 60s

penpot-frontend:
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:80/"]
    interval: 30s
    timeout: 5s
    retries: 3
    start_period: 30s

penpot-postgres:
  healthcheck:
    test: ["CMD", "pg_isready", "-U", "penpot"]
    interval: 10s
    timeout: 5s
    retries: 5

penpot-redis:
  healthcheck:
    test: ["CMD", "redis-cli", "ping"]
    interval: 10s
    timeout: 5s
    retries: 3
```

Note: Penpot backend may not natively expose `/readyz`. If not, fall back to a TCP check on port 6060. The probe in `infra_health.py` handles this gracefully either way.

### Component 4: Prometheus Scrape Config

Add infrastructure service targets to the existing `health-probes` job in `monitoring/prometheus/prometheus.yml`:

```yaml
- job_name: "health-probes"
  static_configs:
    - targets:
        # Existing core services
        - http://server:3100/api/health
        - http://deerflow-langgraph:2024/ok
        - http://deerflow-gateway:8001/health
        - http://vibe:8080/healthz
        # New infra services
        - http://mirofish:5001/health
        - http://paddleocr:8868/health
        - http://searxng:8080/healthz
        - http://gitea:3000/api/v1/version
        - http://minio:9000/minio/health/live
        - http://prometheus:9090/-/healthy
        - http://grafana:3000/api/health
```

### Component 5: Paperclip UI — Health Check Visual (Company Section)

**Repo:** `~/Repos/paperclip`

**Server-side proxy:** Add a route on the Paperclip server that proxies `GET /api/infrastructure/health` to the Vibe agent (`http://vibe:8080/api/infrastructure/health`). This keeps the frontend calling its own server as usual.

**Frontend changes:**

1. **`ui/src/api/infrastructure-health.ts`** (new) — API client that calls `/api/infrastructure/health` and types the response.

2. **`ui/src/pages/InfrastructureHealth.tsx`** (new) — Page component showing:
   - Overall status banner (green/yellow/red)
   - Grid of service cards, each showing: service name, status badge, version, uptime, sub-checks
   - Auto-refresh every 30s via React Query
   - Uses existing `StatusBadge`, `Card`, and status color utilities

3. **Route + navigation:**
   - Add route in `App.tsx` under the company path: `company/infrastructure`
   - Add "Infrastructure" item in `Sidebar.tsx` under the Company section (between Activity and Settings)

**Design language:** Follows existing patterns — Tailwind CSS, lucide-react icons, radix-ui primitives, dark/light theme support. Reference `DevRestartBanner.tsx` for status display patterns and `status-colors.ts` for color palette.

### Component 6: Tests

**`tests/test_infra_health.py`** (new) in Vibe Stack:
- Test normalization for each service type (mock HTTP responses)
- Test aggregate status logic (all-ok, mixed, all-error)
- Test timeout handling (slow/unreachable services)
- Test single-service lookup (valid name, invalid name)

## Out of Scope

- Modifying individual service images to add endpoints (we normalize on our side)
- Alerting rules (follow-up ticket)
- Historical health data storage

## Files Changed

**Vibe Stack (`~/Repos/Vibe-Stack/`):**
- `agents/infra_health.py` — new
- `agents/metrics.py` — modified (add routes)
- `docker-compose.infra.yml` — modified (Penpot healthchecks)
- `monitoring/prometheus/prometheus.yml` — modified (add targets)
- `tests/test_infra_health.py` — new

**Paperclip (`~/Repos/paperclip/`):**
- `server/routes/infrastructure-health.ts` — new (proxy route, exact path TBD after exploring server structure)
- `ui/src/api/infrastructure-health.ts` — new
- `ui/src/pages/InfrastructureHealth.tsx` — new
- `ui/src/App.tsx` — modified (add route)
- `ui/src/components/Sidebar.tsx` — modified (add nav item)
