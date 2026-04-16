# ARCHITECTURE — End-to-End Infrastructure Validation (VIB-62)

## Goal

Validate that the entire Vibe Stack infrastructure works end-to-end: all Docker services start, pass health checks, can communicate with each other, and the agent pipeline can execute a task from Paperclip assignment through to result posting.

## Tech Stack

- **Runtime:** Python 3.14, Node.js (Paperclip server)
- **Containerization:** Docker Compose (3 compose files: core, infra, gpu)
- **LLM Backend:** Ollama (host-native, port 11434)
- **Testing:** pytest (Python), bash scripts for infrastructure probes
- **CI/CD:** Docker Compose healthchecks, `scripts/health-report.sh`

## Project Structure

Tests and validation scripts live alongside existing code:

```
tests/
  test_infra_e2e.py          # New: end-to-end infra integration tests
scripts/
  health-report.sh           # Existing: health check + Paperclip reporting
  check-agent-health.sh      # Existing: agent health checks
agents/
  doctor.py                  # Existing: --doctor health check mode
```

## Services Under Test

### Core (docker-compose.yml)
| Service | Port | Health Endpoint | Purpose |
|---------|------|-----------------|---------|
| server (Paperclip) | 3100 | `/api/health` | Control plane |
| deerflow-langgraph | 2024 | `/ok` | LangGraph backend |
| deerflow-gateway | 8001 | `/health` | Gateway API |
| vibe | 8080 | `/healthz` | Agent orchestrator |
| tailscale | — | — | VPN overlay |

### Infrastructure (docker-compose.infra.yml)
| Service | Port | Health Endpoint | Purpose |
|---------|------|-----------------|---------|
| searxng | 8888 | `/healthz` | Search engine |
| playwright | 3003 | `/json` | Browser automation |
| gitea | 3000 | `/api/v1/version` | Git hosting |
| minio | 9000 | `mc ready local` | Object storage |
| paddleocr | 8868 | `/health` | OCR service |
| prometheus | 9091 | `/-/healthy` | Metrics |
| grafana | 3333 | `/api/health` | Dashboards |
| mirofish | 5001 | `/health` | Simulation engine |
| zep | — | TCP 8000 | Agent memory |
| neo4j | — | `neo4j status` | Graph DB |

### GPU (docker-compose.gpu.yml)
| Service | Port | Health Endpoint | Purpose |
|---------|------|-----------------|---------|
| opensandbox | 9090 | `/docs` | Code execution sandbox |
| comfyui | 8188 | — | Image generation |

## Validation Layers

### Layer 1: Service Health (DevOps)
- All core containers start and report healthy
- All infra containers start and report healthy
- Health endpoints respond with expected status codes
- Inter-service DNS resolution works (e.g., `vibe` can reach `server`)

### Layer 2: Integration Connectivity (Backend)
- Paperclip API responds and can list agents
- Vibe agent can reach Ollama on host
- DeerFlow LangGraph backend accepts requests
- Storage backends initialize (SQLite default)
- Existing `--doctor` mode passes all checks

### Layer 3: Pipeline E2E (QA)
- Create a test issue via Paperclip API
- Verify heartbeat can fetch and checkout the issue
- Verify workflow graph can execute (at minimum: router + spec builder)
- Verify results post back to Paperclip
- Cleanup: mark test issue as cancelled

## Error Handling

- Tests must be idempotent — clean up any test artifacts
- Use timeouts on all HTTP probes (max 10s per service)
- Log which services are unavailable vs. unhealthy (different failure modes)
- Never fail silently — all checks must produce explicit pass/fail output

## Security Requirements

- No hardcoded secrets in test files — use env vars or `.env`
- Test issues must be clearly marked as `[TEST]` to avoid confusion
- No modification of production data or running services

## Testing Requirements

- **Unit tests:** Not applicable (this is an integration/e2e validation task)
- **Integration tests:** Each service health check is a test case
- **E2E tests:** Full pipeline test from issue creation to result posting

## Build Command

```bash
docker compose build
```

## Test Command

```bash
python -m pytest tests/test_infra_e2e.py -x -v --no-header -q
```

## Cross-Agent Conventions

- All agents work on branch: `feature/infra-test-e2e-validation`
- Test file: `tests/test_infra_e2e.py`
- Health probe helper: reuse patterns from `scripts/health-report.sh` and `agents/doctor.py`
- Mark all test issues with `[INFRA-TEST]` prefix in title
