# Infrastructure Hardening — Design Spec

**Goal**: Bring Vibe-Stack infrastructure from B to A- by fixing the operational gaps: CI-built images, baked dependencies, health checks, developer Makefile, and image versioning.

**Scope**: Vibe-Stack repo + Paperclip repo. No new services or external dependencies.

---

## 1. Paperclip CI — DeerFlow Image Build Pipeline

New workflow `deerflow-build.yml` in the Paperclip repo.

**Triggers**: Push to `master` (paths: `deerflow/**`), version tags (`v*`), manual dispatch.

**Jobs**:
1. **test** — `cd deerflow/backend && uv run pytest tests/ -v`
2. **scan** — Trivy misconfig scan on `deerflow/backend/Dockerfile`
3. **build** (needs test + scan) — Build `ghcr.io/tmartin2113/paperclip-deerflow`, tag with:
   - `sha-<short-hash>` (every push)
   - `latest` (every push to master)
   - `v1.2.3` (only on version tags)
4. **post-scan** — Trivy image scan (CRITICAL severity)

**Also**: Add Python DeerFlow test step to existing `pr-verify.yml`. Remove the `deerflow` build job from Vibe-Stack's `docker-publish.yml`.

## 2. Dockerfile — Bake Dependencies

Add `mempalace>=3.1.0` and `graphifyy[mcp,leiden]>=0.3.20` to `deerflow/backend/pyproject.toml` dependencies so `uv sync` installs them at build time.

Remove the `uv pip install mempalace... graphifyy...` prefix from both DeerFlow service commands in Vibe-Stack's `docker-compose.yml`. Commands simplify to just `cd backend && uv run ...`.

## 3. Health Checks for DeerFlow Services

Add to `Vibe-Stack/docker-compose.yml`:

- **deerflow-gateway**: `curl -f http://localhost:8001/health` (endpoint exists)
- **deerflow-langgraph**: `curl -f http://localhost:2024/ok` (LangGraph exposes this)
- **vibe**: `curl -f http://localhost:8080/healthz` (implemented in `agents/metrics.py`)

All use `interval: 10s, timeout: 5s, retries: 5, start_period: 30s`.

## 4. Makefile for Vibe-Stack

Targets:

| Category | Target | Command |
|----------|--------|---------|
| Lifecycle | `up` | `docker compose up -d` |
| | `up-all` | All 3 compose files |
| | `down` | `docker compose down` |
| | `restart` | Restart SVC |
| | `rebuild` | `up -d --build` SVC |
| Monitoring | `status` | `docker compose ps` all files |
| | `logs` | Tail logs, optional SVC |
| Development | `test` | pytest |
| | `lint` | ruff check |
| | `shell` | exec bash into SVC |
| Images | `build-deerflow` | Local DeerFlow image build |
| | `build-vibe` | Local Vibe agent image build |

## 5. Image Versioning

- SHA tags (`sha-abc123f`) on every push for traceability
- `latest` on every push for dev convenience
- Semver tags (`v1.0.0`) on git tags for production pins
- Vibe-Stack `.env` pins `PAPERCLIP_VERSION` to semver in production, `latest` for dev
- First tag: `v1.0.0` after CI pipeline is validated

---

## Files Changed

| Action | Repo | File |
|--------|------|------|
| Create | Paperclip | `.github/workflows/deerflow-build.yml` |
| Modify | Paperclip | `.github/workflows/pr-verify.yml` |
| Modify | Paperclip | `deerflow/backend/pyproject.toml` |
| Create | Vibe-Stack | `Makefile` |
| Modify | Vibe-Stack | `docker-compose.yml` |
| Modify | Vibe-Stack | `.github/workflows/docker-publish.yml` |
| Modify | Vibe-Stack | `.env.example` |
