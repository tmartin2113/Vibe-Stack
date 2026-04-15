# VIB-58: Infrastructure Health Checks — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add unified health check endpoints to all infrastructure services with a normalized JSON format, aggregate endpoint, Prometheus scraping, and a UI page under Paperclip's Company section.

**Architecture:** A new `agents/infra_health.py` module probes each infrastructure service over HTTP/TCP, normalizes responses to a standard JSON schema, and exposes aggregate endpoints via the existing health server in `agents/metrics.py`. The Paperclip server proxies the aggregate endpoint, and a new React page renders the status grid under the Company section.

**Tech Stack:** Python 3 (stdlib http, asyncio), Express.js (proxy route), React 19 + TanStack Query + Tailwind CSS (UI), Prometheus blackbox exporter (monitoring)

---

## File Structure

### Vibe Stack (`~/Repos/Vibe-Stack/`)

| File | Action | Responsibility |
|------|--------|----------------|
| `agents/infra_health.py` | Create | Service registry, HTTP/TCP probes, response normalization, `check_all()` / `check_service()` |
| `agents/metrics.py` | Modify (lines 219-229) | Add `/api/infrastructure/health` and `/api/infrastructure/health/*` routes to `_HealthHandler.do_GET` |
| `docker-compose.infra.yml` | Modify (lines 125-170) | Add healthcheck directives to penpot-backend, penpot-frontend, penpot-postgres, penpot-redis |
| `monitoring/prometheus/prometheus.yml` | Modify (lines 34-38) | Add infrastructure service targets to health-probes job |
| `tests/test_infra_health.py` | Create | Tests for probes, normalization, aggregation, timeouts |

### Paperclip (`~/Repos/paperclip/`)

| File | Action | Responsibility |
|------|--------|----------------|
| `server/src/routes/infrastructure-health.ts` | Create | Proxy route forwarding to Vibe agent |
| `server/src/app.ts` | Modify (line 152) | Import and mount the new route |
| `ui/src/api/infrastructure-health.ts` | Create | API client + TypeScript types |
| `ui/src/pages/InfrastructureHealth.tsx` | Create | Health dashboard page component |
| `ui/src/App.tsx` | Modify (line 129) | Add route for the new page |
| `ui/src/components/Sidebar.tsx` | Modify (line 117) | Add "Infrastructure" nav item under Company |

---

## Task 1: Infrastructure Health Probes (`agents/infra_health.py`)

**Files:**
- Create: `agents/infra_health.py`
- Test: `tests/test_infra_health.py`

- [ ] **Step 1: Write failing tests for single-service probing**

Create `tests/test_infra_health.py`:

```python
"""Tests for infrastructure health check probes and normalization."""

import json
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from unittest.mock import patch

import pytest

from agents.infra_health import (
    SERVICE_REGISTRY,
    probe_service,
    check_all,
    check_service,
    normalize_response,
)


class _MockHealthHandler(BaseHTTPRequestHandler):
    """Configurable mock service for testing probes."""
    response_code = 200
    response_body = '{"status": "ok"}'
    response_delay = 0

    def do_GET(self):
        if self.response_delay:
            time.sleep(self.response_delay)
        self.send_response(self.response_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.response_body.encode())

    def log_message(self, *args):
        pass


@pytest.fixture()
def mock_server():
    """Start a mock HTTP server and return its (host, port)."""
    server = HTTPServer(("127.0.0.1", 0), _MockHealthHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "127.0.0.1", port
    server.shutdown()


class TestNormalizeResponse:
    """Test response normalization to standard format."""

    def test_json_with_status_field(self):
        raw = {"status": "ok", "models_loaded": True}
        result = normalize_response("paddleocr", raw, 200)
        assert result["status"] == "ok"
        assert result["service"] == "paddleocr"
        assert result["checks"]["models_loaded"] is True

    def test_json_with_version_field(self):
        raw = {"version": "1.21.0"}
        result = normalize_response("gitea", raw, 200)
        assert result["status"] == "ok"
        assert result["version"] == "1.21.0"

    def test_http_200_no_status_field(self):
        raw = {"some": "data"}
        result = normalize_response("searxng", raw, 200)
        assert result["status"] == "ok"

    def test_http_503_maps_to_error(self):
        raw = {"error": "overloaded"}
        result = normalize_response("mirofish", raw, 503)
        assert result["status"] == "error"

    def test_non_json_response(self):
        result = normalize_response("searxng", None, 200)
        assert result["status"] == "ok"

    def test_non_json_error_response(self):
        result = normalize_response("searxng", None, 500)
        assert result["status"] == "error"


class TestProbeService:
    """Test HTTP probe against a live mock server."""

    def test_healthy_service(self, mock_server):
        host, port = mock_server
        _MockHealthHandler.response_code = 200
        _MockHealthHandler.response_body = '{"status": "ok", "version": "1.0"}'
        _MockHealthHandler.response_delay = 0

        result = probe_service("test-svc", f"http://{host}:{port}/health", timeout=2)
        assert result["status"] == "ok"
        assert result["service"] == "test-svc"

    def test_unhealthy_service(self, mock_server):
        host, port = mock_server
        _MockHealthHandler.response_code = 503
        _MockHealthHandler.response_body = '{"error": "down"}'
        _MockHealthHandler.response_delay = 0

        result = probe_service("test-svc", f"http://{host}:{port}/health", timeout=2)
        assert result["status"] == "error"

    def test_unreachable_service(self):
        result = probe_service("ghost", "http://127.0.0.1:1/health", timeout=1)
        assert result["status"] == "error"
        assert "error" in result

    def test_timeout_returns_error(self, mock_server):
        host, port = mock_server
        _MockHealthHandler.response_delay = 5
        _MockHealthHandler.response_code = 200
        _MockHealthHandler.response_body = '{"status": "ok"}'

        result = probe_service("slow-svc", f"http://{host}:{port}/health", timeout=0.5)
        assert result["status"] == "error"
        _MockHealthHandler.response_delay = 0  # reset


class TestCheckAll:
    """Test aggregate health check."""

    def test_all_ok(self, mock_server):
        host, port = mock_server
        _MockHealthHandler.response_code = 200
        _MockHealthHandler.response_body = '{"status": "ok"}'
        _MockHealthHandler.response_delay = 0

        registry = {
            "svc-a": {"url": f"http://{host}:{port}/health"},
            "svc-b": {"url": f"http://{host}:{port}/health"},
        }
        with patch("agents.infra_health.SERVICE_REGISTRY", registry):
            result = check_all(timeout=2)
        assert result["status"] == "ok"
        assert result["summary"]["total"] == 2
        assert result["summary"]["ok"] == 2
        assert result["summary"]["error"] == 0

    def test_mixed_status(self, mock_server):
        host, port = mock_server
        _MockHealthHandler.response_code = 200
        _MockHealthHandler.response_body = '{"status": "ok"}'
        _MockHealthHandler.response_delay = 0

        registry = {
            "good": {"url": f"http://{host}:{port}/health"},
            "dead": {"url": "http://127.0.0.1:1/health"},
        }
        with patch("agents.infra_health.SERVICE_REGISTRY", registry):
            result = check_all(timeout=1)
        assert result["status"] == "error"
        assert result["summary"]["ok"] == 1
        assert result["summary"]["error"] == 1

    def test_includes_timestamp(self, mock_server):
        host, port = mock_server
        _MockHealthHandler.response_code = 200
        _MockHealthHandler.response_body = '{"status": "ok"}'
        _MockHealthHandler.response_delay = 0

        registry = {"svc": {"url": f"http://{host}:{port}/health"}}
        with patch("agents.infra_health.SERVICE_REGISTRY", registry):
            result = check_all(timeout=2)
        assert "timestamp" in result


class TestCheckService:
    """Test single-service lookup."""

    def test_known_service(self, mock_server):
        host, port = mock_server
        _MockHealthHandler.response_code = 200
        _MockHealthHandler.response_body = '{"status": "ok"}'
        _MockHealthHandler.response_delay = 0

        registry = {"my-svc": {"url": f"http://{host}:{port}/health"}}
        with patch("agents.infra_health.SERVICE_REGISTRY", registry):
            result = check_service("my-svc", timeout=2)
        assert result["status"] == "ok"

    def test_unknown_service(self):
        result = check_service("nonexistent", timeout=1)
        assert result["status"] == "error"
        assert "not found" in result["error"].lower()


class TestServiceRegistry:
    """Test that the registry has expected entries."""

    def test_expected_services_present(self):
        expected = {"mirofish", "paddleocr", "searxng", "playwright", "minio", "gitea"}
        assert expected.issubset(set(SERVICE_REGISTRY.keys()))

    def test_each_entry_has_url(self):
        for name, entry in SERVICE_REGISTRY.items():
            assert "url" in entry, f"{name} missing 'url'"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/test_infra_health.py -x --no-header -q`
Expected: `ModuleNotFoundError: No module named 'agents.infra_health'`

- [ ] **Step 3: Implement `agents/infra_health.py`**

```python
"""
Infrastructure health check probes and normalization.

Queries each infrastructure service's health endpoint, normalizes responses
to a standard JSON schema, and provides aggregate health status.

Usage:
    from agents.infra_health import check_all, check_service

    aggregate = check_all(timeout=3)
    single = check_service("mirofish", timeout=3)
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Service registry: name -> {url, [label]}
# URLs use Docker service names (resolvable inside the compose network).
SERVICE_REGISTRY: Dict[str, Dict[str, str]] = {
    "mirofish": {
        "url": "http://mirofish:5001/health",
        "label": "MiroFish",
    },
    "paddleocr": {
        "url": "http://paddleocr:8868/health",
        "label": "PaddleOCR",
    },
    "searxng": {
        "url": "http://searxng:8080/healthz",
        "label": "SearXNG",
    },
    "playwright": {
        "url": "http://playwright:3003/json",
        "label": "Playwright",
    },
    "penpot-backend": {
        "url": "http://penpot-backend:6060/",
        "label": "Penpot Backend",
    },
    "penpot-frontend": {
        "url": "http://penpot-frontend:80/",
        "label": "Penpot Frontend",
    },
    "minio": {
        "url": "http://minio:9000/minio/health/live",
        "label": "MinIO",
    },
    "gitea": {
        "url": "http://gitea:3000/api/v1/version",
        "label": "Gitea",
    },
    "vllm": {
        "url": "http://vllm:8000/health",
        "label": "vLLM",
    },
    "prometheus": {
        "url": "http://prometheus:9090/-/healthy",
        "label": "Prometheus",
    },
    "grafana": {
        "url": "http://grafana:3000/api/health",
        "label": "Grafana",
    },
}


def normalize_response(
    service_name: str,
    raw: Optional[Dict[str, Any]],
    http_status: int,
) -> Dict[str, Any]:
    """Normalize a raw service response into the standard health format."""
    result: Dict[str, Any] = {
        "service": service_name,
        "version": None,
        "uptime_seconds": None,
        "checks": {},
    }

    # Determine status from HTTP code
    if 200 <= http_status < 300:
        result["status"] = "ok"
    elif 400 <= http_status < 500:
        result["status"] = "error"
    else:
        result["status"] = "error"

    if raw is None:
        return result

    # Extract known fields
    if "status" in raw:
        val = raw["status"]
        if val in ("ok", "degraded", "error"):
            result["status"] = val
        elif val in ("unhealthy", "down"):
            result["status"] = "error"

    if "version" in raw:
        result["version"] = str(raw["version"])

    if "uptime_seconds" in raw:
        result["uptime_seconds"] = raw["uptime_seconds"]
    elif "uptime" in raw:
        result["uptime_seconds"] = raw["uptime"]

    # Everything else goes into checks
    skip_keys = {"status", "version", "uptime_seconds", "uptime", "service"}
    for key, value in raw.items():
        if key not in skip_keys:
            result["checks"][key] = value

    # Override status on HTTP error even if body says ok
    if http_status >= 400:
        result["status"] = "error"

    return result


def probe_service(
    name: str,
    url: str,
    timeout: float = 3,
) -> Dict[str, Any]:
    """Probe a single service and return a normalized health result."""
    label = SERVICE_REGISTRY.get(name, {}).get("label", name)
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            http_status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
            try:
                raw = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                raw = None
            result = normalize_response(name, raw, http_status)
            result["label"] = label
            return result
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
            raw = json.loads(body)
        except Exception:
            raw = None
        result = normalize_response(name, raw, e.code)
        result["label"] = label
        return result
    except Exception as e:
        return {
            "status": "error",
            "service": name,
            "label": label,
            "version": None,
            "uptime_seconds": None,
            "checks": {},
            "error": str(e),
        }


def check_all(timeout: float = 3) -> Dict[str, Any]:
    """Probe all registered services concurrently and return aggregate status."""
    services: Dict[str, Dict[str, Any]] = {}
    summary = {"total": 0, "ok": 0, "degraded": 0, "error": 0}

    with ThreadPoolExecutor(max_workers=len(SERVICE_REGISTRY) or 1) as pool:
        futures = {
            pool.submit(probe_service, name, entry["url"], timeout): name
            for name, entry in SERVICE_REGISTRY.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            result = future.result()
            services[name] = result
            summary["total"] += 1
            status = result.get("status", "error")
            if status in summary:
                summary[status] += 1
            else:
                summary["error"] += 1

    # Determine overall status
    if summary["error"] > 0:
        overall = "error"
    elif summary["degraded"] > 0:
        overall = "degraded"
    else:
        overall = "ok"

    return {
        "status": overall,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": services,
        "summary": summary,
    }


def check_service(name: str, timeout: float = 3) -> Dict[str, Any]:
    """Probe a single service by name."""
    entry = SERVICE_REGISTRY.get(name)
    if entry is None:
        return {
            "status": "error",
            "service": name,
            "error": f"Service '{name}' not found in registry",
        }
    return probe_service(name, entry["url"], timeout)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/test_infra_health.py -x --no-header -q`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack
git add agents/infra_health.py tests/test_infra_health.py
git commit -m "feat(VIB-58): add infrastructure health probe module with tests"
```

---

## Task 2: Wire Aggregate Endpoints into Health Server

**Files:**
- Modify: `agents/metrics.py:219-229` (add routes to `do_GET`)
- Modify: `agents/infra_health.py` (already created)
- Test: `tests/test_infra_health.py` (add integration tests)

- [ ] **Step 1: Add failing test for the new endpoints**

Append to `tests/test_infra_health.py`:

```python
class TestHealthServerIntegration:
    """Test /api/infrastructure/health routes on the health server."""

    @pytest.fixture(autouse=True, scope="class")
    def _start_server(self, request):
        import urllib.request as _ur
        import urllib.error as _ue
        from agents.metrics import start_health_server
        port = 18235
        request.cls.port = port
        server = start_health_server(port=port)
        for _ in range(20):
            try:
                _ur.urlopen(f"http://localhost:{port}/healthz", timeout=0.5)
                break
            except Exception:
                time.sleep(0.05)
        request.cls.server = server
        yield
        if server:
            server.shutdown()
            import socket
            for _ in range(20):
                try:
                    with socket.create_connection(("localhost", port), timeout=0.1):
                        pass
                    time.sleep(0.05)
                except OSError:
                    break

    def _get(self, path: str) -> tuple:
        url = f"http://localhost:{self.port}{path}"
        try:
            resp = urllib.request.urlopen(url, timeout=5)
            return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_aggregate_endpoint_returns_json(self):
        status, data = self._get("/api/infrastructure/health")
        assert status == 200
        assert "status" in data
        assert "services" in data
        assert "summary" in data

    def test_single_service_endpoint(self):
        # Use a service from the registry — will likely fail in test env
        # but should still return valid JSON
        status, data = self._get("/api/infrastructure/health/mirofish")
        assert status == 200
        assert data["service"] == "mirofish"

    def test_unknown_service_returns_error(self):
        status, data = self._get("/api/infrastructure/health/nonexistent")
        assert status == 200
        assert data["status"] == "error"
        assert "not found" in data.get("error", "").lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/test_infra_health.py::TestHealthServerIntegration -x --no-header -q`
Expected: FAIL — 404 from health server (route not implemented yet)

- [ ] **Step 3: Add routes to `agents/metrics.py`**

In `agents/metrics.py`, modify `_HealthHandler.do_GET` (line 219) to add the new routes. Replace the existing `do_GET` method:

```python
    def do_GET(self):
        if self.path == "/healthz":
            self._handle_healthz()
        elif self.path == "/readyz":
            self._handle_readyz()
        elif self.path == "/metrics":
            self._handle_metrics()
        elif self.path == "/status":
            self._handle_status()
        elif self.path == "/api/infrastructure/health":
            self._handle_infra_health_all()
        elif self.path.startswith("/api/infrastructure/health/"):
            service_name = self.path.split("/api/infrastructure/health/", 1)[1]
            self._handle_infra_health_single(service_name)
        else:
            self.send_error(404)

    def _handle_infra_health_all(self):
        """Aggregate health of all infrastructure services."""
        from agents.infra_health import check_all
        result = check_all(timeout=3)
        body = json.dumps(result, default=str)
        self._respond(200, body, "application/json")

    def _handle_infra_health_single(self, service_name: str):
        """Health check for a single infrastructure service."""
        from agents.infra_health import check_service
        result = check_service(service_name, timeout=3)
        body = json.dumps(result, default=str)
        self._respond(200, body, "application/json")
```

Also add to the `start_health_server` log message (line 391):

```python
        logger.info(f"Health server started on port {port} "
                     f"(endpoints: /healthz, /readyz, /metrics, /status, "
                     f"/api/infrastructure/health)")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/test_infra_health.py -x --no-header -q`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack
git add agents/metrics.py tests/test_infra_health.py
git commit -m "feat(VIB-58): wire infrastructure health aggregate endpoints into health server"
```

---

## Task 3: Docker Healthchecks for Penpot Services

**Files:**
- Modify: `docker-compose.infra.yml:125-170` (penpot-backend, penpot-frontend, penpot-postgres, penpot-redis)

- [ ] **Step 1: Add healthcheck to penpot-frontend (lines 111-122)**

After the `volumes` line of `penpot-frontend` (line 122), add:

```yaml
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:80/"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
```

- [ ] **Step 2: Add healthcheck to penpot-backend (lines 125-143)**

After the `volumes` line of `penpot-backend` (line 143), add:

```yaml
    healthcheck:
      test: ["CMD", "curl", "-sf", "-o", "/dev/null", "http://localhost:6060/"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
```

Note: Penpot backend doesn't expose a dedicated health endpoint. We probe the root URL — if the JVM is up and serving, the HTTP 200/302 means the service is alive. Using `-sf -o /dev/null` to accept any successful response and suppress output.

- [ ] **Step 3: Add healthcheck to penpot-postgres (lines 156-165)**

After the `volumes` line of `penpot-postgres` (line 165), add:

```yaml
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "penpot"]
      interval: 10s
      timeout: 5s
      retries: 5
```

- [ ] **Step 4: Add healthcheck to penpot-redis (lines 168-169)**

After `restart: unless-stopped` of `penpot-redis` (line 169), add:

```yaml
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 3
```

- [ ] **Step 5: Update penpot-backend depends_on to use health conditions**

Change `penpot-backend`'s `depends_on` (lines 128-130) from:

```yaml
    depends_on:
      - penpot-postgres
      - penpot-redis
```

to:

```yaml
    depends_on:
      penpot-postgres:
        condition: service_healthy
      penpot-redis:
        condition: service_healthy
```

- [ ] **Step 6: Update penpot-frontend depends_on to use health conditions**

Change `penpot-frontend`'s `depends_on` (lines 117-118) from:

```yaml
    depends_on:
      - penpot-backend
      - penpot-exporter
```

to:

```yaml
    depends_on:
      penpot-backend:
        condition: service_healthy
      penpot-exporter:
        condition: service_started
```

- [ ] **Step 7: Validate compose syntax**

Run: `cd ~/Repos/Vibe-Stack && docker compose -f docker-compose.infra.yml config --quiet`
Expected: Exit code 0 (no errors)

- [ ] **Step 8: Commit**

```bash
cd ~/Repos/Vibe-Stack
git add docker-compose.infra.yml
git commit -m "feat(VIB-58): add Docker healthchecks for Penpot services"
```

---

## Task 4: Prometheus Scrape Config

**Files:**
- Modify: `monitoring/prometheus/prometheus.yml:34-38`

- [ ] **Step 1: Add infrastructure targets to health-probes job**

Replace the `static_configs` block in the `health-probes` job (lines 33-38) with:

```yaml
    static_configs:
      - targets:
          # Core services
          - http://server:3100/api/health
          - http://deerflow-langgraph:2024/ok
          - http://deerflow-gateway:8001/health
          - http://vibe:8080/healthz
          # Infrastructure services
          - http://mirofish:5001/health
          - http://paddleocr:8868/health
          - http://searxng:8080/healthz
          - http://gitea:3000/api/v1/version
          - http://minio:9000/minio/health/live
          - http://prometheus:9090/-/healthy
          - http://grafana:3000/api/health
```

- [ ] **Step 2: Validate YAML syntax**

Run: `cd ~/Repos/Vibe-Stack && python3 -c "import yaml; yaml.safe_load(open('monitoring/prometheus/prometheus.yml'))"; echo "OK"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
cd ~/Repos/Vibe-Stack
git add monitoring/prometheus/prometheus.yml
git commit -m "feat(VIB-58): add infrastructure service targets to Prometheus health probes"
```

---

## Task 5: Paperclip Server Proxy Route

**Files:**
- Create: `server/src/routes/infrastructure-health.ts` (in `~/Repos/paperclip/`)
- Modify: `server/src/app.ts:152` (in `~/Repos/paperclip/`)

- [ ] **Step 1: Create the proxy route**

Create `server/src/routes/infrastructure-health.ts`:

```typescript
import { Router } from "express";

const VIBE_HEALTH_URL = process.env.VIBE_HEALTH_URL ?? "http://vibe:8080";

export function infrastructureHealthRoutes() {
  const router = Router();

  router.get("/", async (_req, res) => {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 10_000);
      const upstream = await fetch(
        `${VIBE_HEALTH_URL}/api/infrastructure/health`,
        { signal: controller.signal, headers: { Accept: "application/json" } },
      );
      clearTimeout(timer);
      const data = await upstream.json();
      res.status(upstream.status).json(data);
    } catch (err) {
      res.status(502).json({
        status: "error",
        error: "Failed to reach infrastructure health endpoint",
      });
    }
  });

  router.get("/:service", async (req, res) => {
    const { service } = req.params;
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 10_000);
      const upstream = await fetch(
        `${VIBE_HEALTH_URL}/api/infrastructure/health/${encodeURIComponent(service)}`,
        { signal: controller.signal, headers: { Accept: "application/json" } },
      );
      clearTimeout(timer);
      const data = await upstream.json();
      res.status(upstream.status).json(data);
    } catch (err) {
      res.status(502).json({
        status: "error",
        error: `Failed to reach health endpoint for ${service}`,
      });
    }
  });

  return router;
}
```

- [ ] **Step 2: Mount the route in `app.ts`**

In `server/src/app.ts`, add the import (after the existing route imports around line 34):

```typescript
import { infrastructureHealthRoutes } from "./routes/infrastructure-health.js";
```

Then mount it right after the health route (after line 152):

```typescript
  api.use("/infrastructure/health", infrastructureHealthRoutes());
```

- [ ] **Step 3: Verify the server builds**

Run: `cd ~/Repos/paperclip && cd server && npx tsc --noEmit`
Expected: No type errors

- [ ] **Step 4: Commit**

```bash
cd ~/Repos/paperclip
git add server/src/routes/infrastructure-health.ts server/src/app.ts
git commit -m "feat(VIB-58): add infrastructure health proxy route to Paperclip server"
```

---

## Task 6: Paperclip Frontend — API Client + Types

**Files:**
- Create: `ui/src/api/infrastructure-health.ts` (in `~/Repos/paperclip/`)

- [ ] **Step 1: Create the API client**

Create `ui/src/api/infrastructure-health.ts`:

```typescript
export type InfraServiceStatus = {
  status: "ok" | "degraded" | "error";
  service: string;
  label?: string;
  version: string | null;
  uptime_seconds: number | null;
  checks: Record<string, unknown>;
  error?: string;
};

export type InfraHealthSummary = {
  total: number;
  ok: number;
  degraded: number;
  error: number;
};

export type InfraHealthResponse = {
  status: "ok" | "degraded" | "error";
  timestamp: string;
  services: Record<string, InfraServiceStatus>;
  summary: InfraHealthSummary;
};

export const infrastructureHealthApi = {
  getAll: async (): Promise<InfraHealthResponse> => {
    const res = await fetch("/api/infrastructure/health", {
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    if (!res.ok) {
      throw new Error(`Infrastructure health check failed (${res.status})`);
    }
    return res.json();
  },
};
```

- [ ] **Step 2: Commit**

```bash
cd ~/Repos/paperclip
git add ui/src/api/infrastructure-health.ts
git commit -m "feat(VIB-58): add infrastructure health API client and types"
```

---

## Task 7: Paperclip Frontend — Infrastructure Health Page

**Files:**
- Create: `ui/src/pages/InfrastructureHealth.tsx` (in `~/Repos/paperclip/`)
- Modify: `ui/src/App.tsx:129`
- Modify: `ui/src/components/Sidebar.tsx:117`

- [ ] **Step 1: Create the page component**

Create `ui/src/pages/InfrastructureHealth.tsx`:

```tsx
import { useQuery } from "@tanstack/react-query";
import { RefreshCw, CheckCircle2, AlertTriangle, XCircle, Clock } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  infrastructureHealthApi,
  type InfraServiceStatus,
} from "../api/infrastructure-health";

const statusConfig = {
  ok: {
    icon: CheckCircle2,
    color: "text-green-600 dark:text-green-400",
    bg: "bg-green-100 dark:bg-green-900/50",
    label: "Healthy",
  },
  degraded: {
    icon: AlertTriangle,
    color: "text-yellow-600 dark:text-yellow-400",
    bg: "bg-yellow-100 dark:bg-yellow-900/50",
    label: "Degraded",
  },
  error: {
    icon: XCircle,
    color: "text-red-600 dark:text-red-400",
    bg: "bg-red-100 dark:bg-red-900/50",
    label: "Error",
  },
} as const;

function formatUptime(seconds: number | null): string {
  if (seconds == null) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function ServiceCard({ service }: { service: InfraServiceStatus }) {
  const config = statusConfig[service.status] ?? statusConfig.error;
  const Icon = config.icon;

  return (
    <Card className="rounded-lg">
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm font-medium">
            {service.label ?? service.service}
          </CardTitle>
          <Icon className={cn("h-4 w-4", config.color)} />
        </div>
      </CardHeader>
      <CardContent>
        <div className="space-y-1.5">
          <div className="flex items-center gap-2">
            <span
              className={cn(
                "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium",
                config.bg,
                config.color,
              )}
            >
              {config.label}
            </span>
            {service.version && (
              <span className="text-xs text-muted-foreground">
                v{service.version}
              </span>
            )}
          </div>
          {service.uptime_seconds != null && (
            <div className="flex items-center gap-1 text-xs text-muted-foreground">
              <Clock className="h-3 w-3" />
              {formatUptime(service.uptime_seconds)}
            </div>
          )}
          {service.error && (
            <p className="text-xs text-red-600 dark:text-red-400 truncate" title={service.error}>
              {service.error}
            </p>
          )}
          {Object.keys(service.checks).length > 0 && (
            <div className="mt-2 space-y-0.5">
              {Object.entries(service.checks).map(([key, value]) => (
                <div key={key} className="flex items-center justify-between text-xs">
                  <span className="text-muted-foreground">{key}</span>
                  <span className={cn(
                    value === "ok" || value === true
                      ? "text-green-600 dark:text-green-400"
                      : "text-muted-foreground"
                  )}>
                    {String(value)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export function InfrastructureHealth() {
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ["infrastructure-health"],
    queryFn: infrastructureHealthApi.getAll,
    refetchInterval: 30_000,
    retry: 1,
  });

  const overallConfig = data
    ? statusConfig[data.status] ?? statusConfig.error
    : null;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Infrastructure</h1>
          <p className="text-sm text-muted-foreground">
            Health status of all infrastructure services
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
        >
          <RefreshCw className={cn("mr-2 h-4 w-4", isFetching && "animate-spin")} />
          Refresh
        </Button>
      </div>

      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-4 dark:border-red-800 dark:bg-red-950">
          <p className="text-sm text-red-700 dark:text-red-300">
            Failed to load infrastructure health. The Vibe agent may be unreachable.
          </p>
        </div>
      )}

      {isLoading && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {Array.from({ length: 8 }).map((_, i) => (
            <Card key={i} className="rounded-lg animate-pulse">
              <CardHeader className="pb-2">
                <div className="h-4 w-24 rounded bg-muted" />
              </CardHeader>
              <CardContent>
                <div className="h-3 w-16 rounded bg-muted" />
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {data && (
        <>
          <div className="flex items-center gap-4 rounded-lg border p-4">
            {overallConfig && (
              <>
                <overallConfig.icon className={cn("h-6 w-6", overallConfig.color)} />
                <div>
                  <p className="font-medium">
                    System {overallConfig.label}
                  </p>
                  <p className="text-sm text-muted-foreground">
                    {data.summary.ok}/{data.summary.total} services healthy
                    {data.summary.degraded > 0 && ` · ${data.summary.degraded} degraded`}
                    {data.summary.error > 0 && ` · ${data.summary.error} errors`}
                  </p>
                </div>
              </>
            )}
            <span className="ml-auto text-xs text-muted-foreground">
              {new Date(data.timestamp).toLocaleTimeString()}
            </span>
          </div>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {Object.values(data.services)
              .sort((a, b) => {
                const order = { error: 0, degraded: 1, ok: 2 };
                return (order[a.status] ?? 1) - (order[b.status] ?? 1);
              })
              .map((service) => (
                <ServiceCard key={service.service} service={service} />
              ))}
          </div>
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Add route in `App.tsx`**

In `ui/src/App.tsx`, add the import at the top (after the other page imports around line 27):

```typescript
import { InfrastructureHealth } from "./pages/InfrastructureHealth";
```

Then in the `boardRoutes()` function, add the route after the `company/settings` route (after line 129):

```tsx
      <Route path="company/infrastructure" element={<InfrastructureHealth />} />
```

- [ ] **Step 3: Add nav item in `Sidebar.tsx`**

In `ui/src/components/Sidebar.tsx`, add the `Activity` import to the lucide-react import (line 1) — add `Activity as ActivityIcon`:

Wait — `Activity` is already the name of a lucide icon and a page. Use `HeartPulse` instead, which better represents health monitoring.

Add `HeartPulse` to the lucide-react import on line 1:

```typescript
import {
  Inbox,
  CircleDot,
  Target,
  LayoutDashboard,
  DollarSign,
  History,
  Search,
  SquarePen,
  Network,
  Boxes,
  Repeat,
  Settings,
  Eye,
  HeartPulse,
} from "lucide-react";
```

Then in the Company section (after the Activity nav item on line 116, before Settings on line 117), add:

```tsx
          <SidebarNavItem to="/company/infrastructure" label="Infrastructure" icon={HeartPulse} />
```

- [ ] **Step 4: Verify the frontend builds**

Run: `cd ~/Repos/paperclip/ui && npx tsc --noEmit`
Expected: No type errors

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/paperclip
git add ui/src/pages/InfrastructureHealth.tsx ui/src/App.tsx ui/src/components/Sidebar.tsx
git commit -m "feat(VIB-58): add Infrastructure health page under Company section"
```

---

## Task 8: Run Full Test Suite

- [ ] **Step 1: Run Vibe Stack tests**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/test_infra_health.py tests/test_observability.py -x --no-header -q`
Expected: All tests PASS

- [ ] **Step 2: Run Paperclip server type check**

Run: `cd ~/Repos/paperclip/server && npx tsc --noEmit`
Expected: No type errors

- [ ] **Step 3: Run Paperclip UI type check**

Run: `cd ~/Repos/paperclip/ui && npx tsc --noEmit`
Expected: No type errors
