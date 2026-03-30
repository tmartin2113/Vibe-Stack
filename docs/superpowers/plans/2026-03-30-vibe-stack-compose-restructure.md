# Vibe Stack Compose Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure Vibe Stack into a reproducible, open-source repo where `git clone && ./setup.sh && docker compose up -d` gives anyone a working multi-agent platform.

**Architecture:** Replace the gitignored monolithic override with three tracked, purpose-based compose files (core, infra, gpu). External services pull pre-built GHCR images. Only the vibe agent builds locally. `setup.sh` auto-detects hardware and writes `COMPOSE_FILE` to `.env` so `docker compose up -d` always does the right thing.

**Tech Stack:** Docker Compose, Bash, GHCR container images

**Spec:** `docs/superpowers/specs/2026-03-30-vibe-stack-public-repo-design.md`

**Important:** The GHCR images for paperclip-server, deerflow-langgraph, and deerflow-gateway do not exist yet. This plan uses image references that will be published by the paperclip fork CI pipeline (separate plan). Until those images are pushed, use the `docker-compose.override.yml` pattern to build locally.

---

### Task 1: Create `docker-compose.yml` (Core Services)

**Files:**
- Rewrite: `docker-compose.yml`

This replaces the current base compose (which has vibe, vllm, opensandbox, tailscale) with the new core: paperclip server (GHCR), deerflow-langgraph (GHCR), deerflow-gateway (GHCR), vibe agent (local build), tailscale.

- [ ] **Step 1: Back up the current file**

```bash
cp docker-compose.yml docker-compose.yml.bak
```

- [ ] **Step 2: Write the new `docker-compose.yml`**

Replace the entire contents of `docker-compose.yml` with:

```yaml
# Vibe Stack — Core Services
# Always required. Cloud adapters work with just these 5 services.
#
# Usage:
#   docker compose up -d
#
# See also:
#   docker-compose.infra.yml  — dev environment (gitea, minio, penpot, searxng, playwright)
#   docker-compose.gpu.yml    — local GPU inference (vllm, opensandbox, comfyui)

services:

  # ── Paperclip Control Plane ──────────────────────────────────
  server:
    image: ghcr.io/tmartin2113/paperclip-server:${PAPERCLIP_VERSION:-latest}
    restart: unless-stopped
    env_file: .env
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      - DATABASE_URL=
      - BETTER_AUTH_TRUSTED_ORIGINS=http://localhost:3100,https://${TAILSCALE_HOSTNAME:-localhost}
      - SEARXNG_URL=http://searxng:8080
      - GITEA_URL=http://gitea:3000
      - GITEA_TOKEN=${GITEA_TOKEN:-}
      - PLAYWRIGHT_WS_URL=ws://playwright:3003
      - MINIO_URL=http://minio:9000
      - MINIO_ACCESS_KEY=${MINIO_ROOT_USER:-vibe}
      - MINIO_SECRET_KEY=${MINIO_ROOT_PASSWORD:-changeme123}
    ports:
      - "3100:3100"
    healthcheck:
      test: ["CMD", "node", "-e", "fetch('http://localhost:3100/api/health').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s
    volumes:
      - paperclip-data:/paperclip
    networks:
      - default

  # ── DeerFlow LangGraph Server ────────────────────────────────
  deerflow-langgraph:
    image: ghcr.io/tmartin2113/deerflow-langgraph:${PAPERCLIP_VERSION:-latest}
    restart: unless-stopped
    env_file: .env
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      - SEARXNG_URL=http://searxng:8080
      - GITEA_URL=http://gitea:3000
      - GITEA_TOKEN=${GITEA_TOKEN:-}
      - PLAYWRIGHT_WS_URL=ws://playwright:3003
      - MINIO_URL=http://minio:9000
      - MINIO_ACCESS_KEY=${MINIO_ROOT_USER:-vibe}
      - MINIO_SECRET_KEY=${MINIO_ROOT_PASSWORD:-changeme123}
    networks:
      - default

  # ── DeerFlow Gateway ─────────────────────────────────────────
  deerflow-gateway:
    image: ghcr.io/tmartin2113/deerflow-gateway:${PAPERCLIP_VERSION:-latest}
    restart: unless-stopped
    env_file: .env
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      - SEARXNG_URL=http://searxng:8080
      - GITEA_URL=http://gitea:3000
      - GITEA_TOKEN=${GITEA_TOKEN:-}
      - PLAYWRIGHT_WS_URL=ws://playwright:3003
      - MINIO_URL=http://minio:9000
      - MINIO_ACCESS_KEY=${MINIO_ROOT_USER:-vibe}
      - MINIO_SECRET_KEY=${MINIO_ROOT_PASSWORD:-changeme123}
    networks:
      - default

  # ── Vibe Agent ───────────────────────────────────────────────
  vibe:
    build: .
    env_file: .env
    restart: on-failure
    group_add:
      - "125"
    depends_on:
      server:
        condition: service_healthy
    environment:
      - VIBE_BACKEND_HOST=${VIBE_BACKEND_HOST:-vllm}
      - VIBE_BACKEND_PORT=${VIBE_BACKEND_PORT:-8000}
      - VIBE_SANDBOX_URL=${VIBE_SANDBOX_URL:-http://opensandbox:8080}
      - VIBE_HEALTH_PORT=8080
      - BULLETIN_PATH=/shared/bulletin/BULLETIN.md
      - MESSAGE_STORE_PATH=/shared/bulletin/messages.db
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
      - LOG_FORMAT=json
      - SEARXNG_URL=http://searxng:8080
      - PLAYWRIGHT_WS_URL=ws://playwright:3003
      - PENPOT_API_URL=http://penpot-backend:6060
      - COMFYUI_URL=http://comfyui:8188
      - GITEA_URL=http://gitea:3000
      - MINIO_URL=http://minio:9000
    volumes:
      - vibe-data:/home/vibe/.vibe
      - bulletin-data:/shared/bulletin
      - /var/run/docker.sock:/var/run/docker.sock
    networks:
      - default
    deploy:
      resources:
        limits:
          cpus: "2.0"
          memory: ${VIBE_AGENT_MEMORY_LIMIT:-2G}
        reservations:
          memory: 512M

  # ── Tailscale Remote Access ──────────────────────────────────
  tailscale:
    image: tailscale/tailscale:latest
    hostname: ${TS_HOSTNAME:-vibe}
    restart: unless-stopped
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      - TS_AUTHKEY=${TS_AUTHKEY:-}
      - TS_STATE_DIR=/var/lib/tailscale
      - TS_SERVE_CONFIG=/config/serve-config.json
      - TS_USERSPACE=true
    volumes:
      - tailscale-state:/var/lib/tailscale
      - ./tailscale/serve-config.json:/config/serve-config.json:ro

volumes:
  paperclip-data:
  vibe-data:
  bulletin-data:
  tailscale-state:

networks:
  default:
    driver: bridge
```

- [ ] **Step 3: Validate the compose file**

```bash
docker compose -f docker-compose.yml config --quiet
```

Expected: no output (success). If there are errors, fix syntax issues.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml
git commit -m "refactor: rewrite docker-compose.yml as core services with GHCR images

Replace base compose (vibe, vllm, opensandbox, tailscale) with new core:
paperclip server, deerflow-langgraph, deerflow-gateway (all GHCR images),
vibe agent (local build), and tailscale. GPU services move to
docker-compose.gpu.yml."
```

---

### Task 2: Create `docker-compose.infra.yml` (Infrastructure Services)

**Files:**
- Create: `docker-compose.infra.yml`

Extract all infrastructure services from the current `docker-compose.override.yml` lines 139-299 into a tracked file.

- [ ] **Step 1: Create `docker-compose.infra.yml`**

```yaml
# Vibe Stack — Infrastructure Services
# Full dev environment: search, browser automation, git, object storage, design tool.
#
# Usage:
#   docker compose -f docker-compose.yml -f docker-compose.infra.yml up -d

services:

  # ── Search ───────────────────────────────────────────────────
  searxng:
    image: searxng/searxng:latest
    restart: unless-stopped
    ports:
      - "8888:8080"
    volumes:
      - ./searxng/settings.yml:/etc/searxng/settings.yml:ro
    environment:
      - SEARXNG_SECRET=${SEARXNG_SECRET:-changeme}
    healthcheck:
      test: ["CMD", "wget", "--spider", "-q", "http://localhost:8080/healthz"]
      interval: 10s
      timeout: 5s
      retries: 3

  # ── Browser Automation ───────────────────────────────────────
  playwright:
    image: mcr.microsoft.com/playwright:v1.52.0-noble
    restart: unless-stopped
    ports:
      - "3003:3003"
    command: ["npx", "playwright", "run-server", "--port", "3003", "--host", "0.0.0.0"]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3003/json"]
      interval: 10s
      timeout: 5s
      retries: 3

  # ── Git Server ───────────────────────────────────────────────
  gitea:
    image: gitea/gitea:latest
    restart: unless-stopped
    ports:
      - "3000:3000"
      - "2223:22"
    environment:
      - GITEA__database__DB_TYPE=sqlite3
      - GITEA__database__PATH=/data/gitea/gitea.db
      - GITEA__server__ROOT_URL=http://localhost:3000
      - GITEA__server__HTTP_PORT=3000
      - GITEA__server__DOMAIN=localhost
      - GITEA__security__INSTALL_LOCK=true
      - GITEA__service__DISABLE_REGISTRATION=false
      - GITEA__service__REQUIRE_SIGNIN_VIEW=false
    volumes:
      - gitea-data:/data
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3000/api/v1/version"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s

  # ── Object Storage ──────────────────────────────────────────
  minio:
    image: minio/minio:latest
    restart: unless-stopped
    ports:
      - "9000:9000"
      - "9002:9002"
    command: server /data --console-address ":9002"
    environment:
      - MINIO_ROOT_USER=${MINIO_ROOT_USER:-vibe}
      - MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD:-changeme123}
    volumes:
      - minio-data:/data
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 10s
      timeout: 5s
      retries: 3

  minio-init:
    image: minio/mc:latest
    depends_on:
      minio:
        condition: service_healthy
    restart: "no"
    entrypoint: >
      /bin/sh -c "
      mc alias set vibe http://minio:9000 $${MINIO_ROOT_USER:-vibe} $${MINIO_ROOT_PASSWORD:-changeme123} &&
      mc mb --ignore-existing vibe/vibe-artifacts &&
      echo 'Bucket vibe-artifacts ready'
      "

  # ── Design Tool ─────────────────────────────────────────────
  penpot-frontend:
    image: penpotapp/frontend:latest
    restart: unless-stopped
    ports:
      - "9001:80"
    depends_on:
      - penpot-backend
      - penpot-exporter
    environment:
      - PENPOT_FLAGS=enable-registration enable-login-with-password disable-email-verification
    volumes:
      - penpot-assets:/opt/data/assets

  penpot-backend:
    image: penpotapp/backend:latest
    restart: unless-stopped
    depends_on:
      - penpot-postgres
      - penpot-redis
    environment:
      - PENPOT_FLAGS=enable-registration enable-login-with-password disable-email-verification disable-secure-session-cookies
      - PENPOT_SECRET_KEY=${PENPOT_SECRET_KEY:-changeme}
      - PENPOT_DATABASE_URI=postgresql://penpot-postgres/penpot
      - PENPOT_DATABASE_USERNAME=penpot
      - PENPOT_DATABASE_PASSWORD=penpot
      - PENPOT_REDIS_URI=redis://penpot-redis/0
      - PENPOT_ASSETS_STORAGE_BACKEND=assets-fs
      - PENPOT_STORAGE_ASSETS_FS_DIRECTORY=/opt/data/assets
      - PENPOT_TELEMETRY_ENABLED=false
      - PENPOT_PUBLIC_URI=http://localhost:9001
    volumes:
      - penpot-assets:/opt/data/assets

  penpot-exporter:
    image: penpotapp/exporter:latest
    restart: unless-stopped
    depends_on:
      - penpot-backend
    environment:
      - PENPOT_PUBLIC_URI=http://penpot-frontend
      - PENPOT_REDIS_URI=redis://penpot-redis/0
      - PENPOT_SECRET_KEY=${PENPOT_SECRET_KEY:-changeme}

  penpot-postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      - POSTGRES_DB=penpot
      - POSTGRES_USER=penpot
      - POSTGRES_PASSWORD=penpot
    volumes:
      - penpot-postgres:/var/lib/postgresql/data

  penpot-redis:
    image: redis:7-alpine
    restart: unless-stopped

  # ── SSH Relay ────────────────────────────────────────────────
  ssh-relay:
    build:
      context: ./ssh-relay
      dockerfile: Dockerfile

  # ── Dev Runner ───────────────────────────────────────────────
  dev-runner:
    build:
      context: ./dev-runner
      dockerfile: Dockerfile

volumes:
  gitea-data:
  minio-data:
  penpot-assets:
  penpot-postgres:
```

- [ ] **Step 2: Validate the compose file**

```bash
docker compose -f docker-compose.yml -f docker-compose.infra.yml config --quiet
```

Expected: no output (success).

- [ ] **Step 3: Commit**

```bash
git add docker-compose.infra.yml
git commit -m "feat: add docker-compose.infra.yml with infrastructure services

Extract searxng, playwright, gitea, minio, penpot, ssh-relay, and
dev-runner from the gitignored override into a tracked compose file."
```

---

### Task 3: Create `docker-compose.gpu.yml` (GPU Services)

**Files:**
- Create: `docker-compose.gpu.yml`

Extract vllm, opensandbox, comfyui into a GPU-only compose file.

- [ ] **Step 1: Create `docker-compose.gpu.yml`**

```yaml
# Vibe Stack — GPU Services
# Local inference via vLLM, sandboxed code execution, image generation.
# Requires an NVIDIA GPU with the nvidia-container-toolkit installed.
#
# Usage:
#   docker compose -f docker-compose.yml -f docker-compose.infra.yml -f docker-compose.gpu.yml up -d

services:

  # ── Local LLM Inference ─────────────────────────────────────
  vllm:
    image: vllm/vllm-openai:latest
    restart: unless-stopped
    ports:
      - "8000:8000"
    command:
      - "${VLLM_MODEL:-Qwen/Qwen3.5-9B}"
      - "--host"
      - "0.0.0.0"
      - "--port"
      - "8000"
      - "--max-model-len"
      - "${VLLM_MAX_MODEL_LEN:-12288}"
      - "--gpu-memory-utilization"
      - "${VLLM_GPU_MEM_UTIL:-0.92}"
      - "--enable-prefix-caching"
      - "--enable-chunked-prefill"
      - "--max-num-seqs"
      - "${VLLM_MAX_NUM_SEQS:-4}"
      - "--trust-remote-code"
      - "--enable-auto-tool-choice"
      - "--tool-call-parser"
      - "${VLLM_TOOL_CALL_PARSER:-hermes}"
      - "--reasoning-parser"
      - "${VLLM_REASONING_PARSER:-qwen3}"
    volumes:
      - vllm-models:/root/.cache/huggingface
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 60s
    deploy:
      resources:
        limits:
          memory: ${VIBE_VLLM_MEMORY_LIMIT:-24G}
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]

  # ── Sandboxed Code Execution ─────────────────────────────────
  opensandbox:
    image: opensandbox/server:v0.1.7
    restart: unless-stopped
    command: ["--config", "/root/.sandbox.toml"]
    ports:
      - "9090:8080"
    volumes:
      - ./sandbox/sandbox.toml:/root/.sandbox.toml:ro
      - /var/run/docker.sock:/var/run/docker.sock
      - ./:/home/user/Vibe:ro
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/docs')"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 15s
    deploy:
      resources:
        limits:
          memory: ${VIBE_SANDBOX_MEMORY_LIMIT:-4G}
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]

  # ── Image Generation ─────────────────────────────────────────
  comfyui:
    image: ghcr.io/ai-dock/comfyui:latest
    restart: unless-stopped
    profiles:
      - gpu-comfyui
    ports:
      - "8188:8188"
    environment:
      - COMFYUI_PORT=8188
    volumes:
      - comfyui-data:/workspace
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]

volumes:
  vllm-models:
  comfyui-data:
```

- [ ] **Step 2: Validate all three compose files together**

```bash
docker compose -f docker-compose.yml -f docker-compose.infra.yml -f docker-compose.gpu.yml config --quiet
```

Expected: no output (success).

- [ ] **Step 3: Commit**

```bash
git add docker-compose.gpu.yml
git commit -m "feat: add docker-compose.gpu.yml with vllm, opensandbox, comfyui

GPU tier for local inference, sandboxed execution, and image generation.
Requires NVIDIA GPU with nvidia-container-toolkit."
```

---

### Task 4: Update `.env.example`

**Files:**
- Modify: `.env.example`

Add `COMPOSE_FILE`, `PAPERCLIP_VERSION`, and reorganize for the new compose structure. Remove `PAPERCLIP_SOURCE_DIR` from required vars (now optional, dev-only).

- [ ] **Step 1: Add new variables to the top of `.env.example`**

Add these lines after the header comment, before the existing Host & Network section:

```bash
# ── Compose Configuration ─────────────────────────────────────
# Set by setup.sh based on detected hardware. Controls which services start.
# Manually override to change your stack profile.
#
# Full stack (GPU):   docker-compose.yml:docker-compose.infra.yml:docker-compose.gpu.yml
# Cloud only:         docker-compose.yml:docker-compose.infra.yml
# Minimal:            docker-compose.yml
COMPOSE_FILE=docker-compose.yml:docker-compose.infra.yml

# ── Image Versions ────────────────────────────────────────────
# Pin to a specific release or use 'latest' for bleeding edge.
PAPERCLIP_VERSION=latest

# ── Local Development (optional) ──────────────────────────────
# Set PAPERCLIP_SOURCE_DIR to build server/deerflow from local source
# instead of pulling GHCR images. Use with docker-compose.override.yml.
# PAPERCLIP_SOURCE_DIR=/path/to/paperclip
```

- [ ] **Step 2: Validate `.env.example` is parseable**

```bash
bash -c 'set -a; source .env.example; set +a; echo "COMPOSE_FILE=$COMPOSE_FILE"'
```

Expected: `COMPOSE_FILE=docker-compose.yml:docker-compose.infra.yml`

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "feat: add COMPOSE_FILE and PAPERCLIP_VERSION to .env.example

setup.sh sets COMPOSE_FILE based on detected hardware so docker compose
up -d always starts the right services."
```

---

### Task 5: Update `docker-compose.override.yml.example`

**Files:**
- Rewrite: `docker-compose.override.yml.example`

This is now purely for local development overrides — mounting source code from a local paperclip checkout.

- [ ] **Step 1: Rewrite `docker-compose.override.yml.example`**

```yaml
# ══════════════════════════════════════════════════════════════
# Developer Override — Local Source Builds
# ══════════════════════════════════════════════════════════════
#
# Copy to docker-compose.override.yml to build server and DeerFlow
# from local source instead of pulling GHCR images.
#
# Required:
#   PAPERCLIP_SOURCE_DIR — path to your local paperclip checkout
#
# Usage:
#   cp docker-compose.override.yml.example docker-compose.override.yml
#   # Edit PAPERCLIP_SOURCE_DIR in .env
#   docker compose up -d --build

services:

  server:
    build:
      context: ${PAPERCLIP_SOURCE_DIR}
      dockerfile: Dockerfile
    # Mount dist directories for hot-reload during development
    volumes:
      - ${PAPERCLIP_SOURCE_DIR}/server/dist:/app/server/dist
      - ${PAPERCLIP_SOURCE_DIR}/packages/db/dist:/app/packages/db/dist
      - ${PAPERCLIP_SOURCE_DIR}/packages/shared/dist:/app/packages/shared/dist
      - ${PAPERCLIP_SOURCE_DIR}/packages/adapters/deerflow/dist:/app/packages/adapters/deerflow/dist
      - ${PAPERCLIP_SOURCE_DIR}/ui/dist:/app/ui/dist
      - ${PAPERCLIP_SOURCE_DIR}/skills:/app/skills

  deerflow-langgraph:
    build:
      context: ${PAPERCLIP_SOURCE_DIR}/deerflow
      dockerfile: backend/Dockerfile
    volumes:
      - ${PAPERCLIP_SOURCE_DIR}/deerflow/backend/:/app/backend/
      - ${PAPERCLIP_SOURCE_DIR}/deerflow/config.yaml:/app/config.yaml:ro
      - ${PAPERCLIP_SOURCE_DIR}/deerflow/skills:/app/skills

  deerflow-gateway:
    build:
      context: ${PAPERCLIP_SOURCE_DIR}/deerflow
      dockerfile: backend/Dockerfile
    volumes:
      - ${PAPERCLIP_SOURCE_DIR}/deerflow/backend/:/app/backend/
      - ${PAPERCLIP_SOURCE_DIR}/deerflow/config.yaml:/app/config.yaml:ro
      - ${PAPERCLIP_SOURCE_DIR}/deerflow/skills:/app/skills
```

- [ ] **Step 2: Commit**

```bash
git add docker-compose.override.yml.example
git commit -m "refactor: simplify override example to dev-only source mounts

Override is now optional — only needed when building server/deerflow
from local paperclip source instead of GHCR images."
```

---

### Task 6: Update `.gitignore`

**Files:**
- Modify: `.gitignore`

Ensure the new compose files are tracked and the backup is ignored.

- [ ] **Step 1: Add backup to `.gitignore`**

Add this line near the existing `docker-compose.override.yml` entry:

```
docker-compose.yml.bak
```

- [ ] **Step 2: Verify the new compose files are NOT gitignored**

```bash
git check-ignore docker-compose.infra.yml docker-compose.gpu.yml
```

Expected: no output (meaning they are NOT ignored, which is correct).

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "chore: add docker-compose.yml.bak to gitignore"
```

---

### Task 7: Update `setup.sh` — COMPOSE_FILE Auto-Configuration

**Files:**
- Modify: `setup.sh`

The key change: after GPU detection (Phase 5b), write the correct `COMPOSE_FILE` to `.env`. This is a surgical addition, not a rewrite of the entire setup script.

- [ ] **Step 1: Find the GPU detection result in `setup.sh`**

The GPU detection is in Phase 5b (around line 241). After model selection, the script persists `VLLM_MODEL` to `.env`. We add `COMPOSE_FILE` right after.

Find the section after GPU/model detection where `VLLM_MODEL` is written to `.env` (around lines 350-363). After that block, add:

```bash
# ── Set COMPOSE_FILE based on GPU availability ─────────────────
if [ -n "$VLLM_MODEL" ]; then
  _update_env_var COMPOSE_FILE "docker-compose.yml:docker-compose.infra.yml:docker-compose.gpu.yml"
  info "Compose profile: full stack (core + infra + gpu)"
else
  _update_env_var COMPOSE_FILE "docker-compose.yml:docker-compose.infra.yml"
  info "Compose profile: cloud only (core + infra, no GPU)"
fi
```

- [ ] **Step 2: Verify `_update_env_var` function exists**

```bash
grep -n '_update_env_var' setup.sh | head -5
```

Expected: function definition around the initialization section (lines 1-130). If it doesn't exist, check for the actual env-writing helper name used in the script and use that instead.

- [ ] **Step 3: Test the setup.sh change parses correctly**

```bash
bash -n setup.sh
```

Expected: no output (valid syntax).

- [ ] **Step 4: Commit**

```bash
git add setup.sh
git commit -m "feat: setup.sh auto-configures COMPOSE_FILE based on GPU detection

After hardware detection, writes COMPOSE_FILE to .env so docker compose
up -d starts the correct service profile automatically."
```

---

### Task 8: Update Phase 13 of `setup.sh` (Staged Startup)

**Files:**
- Modify: `setup.sh`

Phase 13 (around lines 754-802) starts services in a specific order. Update it to use the new compose file structure instead of assuming all services are in one compose file.

- [ ] **Step 1: Read current Phase 13**

Read `setup.sh` lines 754-802 to see the current staged startup.

- [ ] **Step 2: Update the startup sequence**

Replace the Phase 13 section with a sequence that respects the compose file split:

```bash
# ── Phase 13: Start Stack (Staged) ────────────────────────────
info "Starting services..."

# Core services first (server needs to be healthy before agents)
docker compose up -d server
wait_healthy server 120

# DeerFlow services
docker compose up -d deerflow-langgraph deerflow-gateway
info "DeerFlow services started"

# Infrastructure (if included in COMPOSE_FILE)
if echo "$COMPOSE_FILE" | grep -q "infra"; then
  docker compose up -d searxng playwright gitea minio minio-init penpot-frontend penpot-backend penpot-exporter penpot-postgres penpot-redis ssh-relay dev-runner
  wait_healthy gitea 60
  wait_healthy minio 60
  wait_healthy searxng 60
  info "Infrastructure services started"
fi

# GPU services (if included in COMPOSE_FILE)
if echo "$COMPOSE_FILE" | grep -q "gpu"; then
  docker compose up -d vllm opensandbox
  wait_healthy vllm 300
  info "GPU services started"
fi

# Tailscale
docker compose up -d tailscale
info "Tailscale started"

# Vibe agent (depends on server healthy)
docker compose up -d vibe
info "Vibe agent started"

success "All services running!"
```

- [ ] **Step 3: Validate syntax**

```bash
bash -n setup.sh
```

Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add setup.sh
git commit -m "refactor: update setup.sh staged startup for compose file split

Start services in dependency order across the split compose files:
server → deerflow → infra (if enabled) → gpu (if enabled) → tailscale → vibe."
```

---

### Task 9: Remove Hardcoded Agent Replicas

**Files:**
- Verify: `docker-compose.yml` (already handled — no replicas in new file)
- Modify: `.env.example` — remove `PAPERCLIP_AGENT_ID_ENG_*` vars

- [ ] **Step 1: Remove agent ID vars from `.env.example`**

Find and remove these lines from `.env.example`:

```bash
PAPERCLIP_AGENT_ID_ENG_1=
PAPERCLIP_AGENT_ID_ENG_2=
PAPERCLIP_AGENT_ID_ENG_3=
```

If they exist. Check first:

```bash
grep -n 'PAPERCLIP_AGENT_ID_ENG' .env.example
```

- [ ] **Step 2: Commit if changes were made**

```bash
git add .env.example
git commit -m "chore: remove hardcoded agent replica IDs from env example

Agent provisioning is now done through the Paperclip UI, not compose
environment variables."
```

---

### Task 10: Clean Up Legacy Compose Files

**Files:**
- Review: `docker/docker-compose.vllm.yml`, `docker/docker-compose.production.yml`, `docker/docker-compose.paperclip.yml`

These files reference the old structure. They should be updated or deprecated.

- [ ] **Step 1: Add deprecation notice to legacy compose files**

Add a comment to the top of each file in `docker/`:

```yaml
# DEPRECATED: This file predates the compose restructure (2026-03-30).
# Use the root-level compose files instead:
#   docker-compose.yml        — core services
#   docker-compose.infra.yml  — infrastructure
#   docker-compose.gpu.yml    — GPU services
#
# This file is preserved for reference and will be removed in a future release.
```

- [ ] **Step 2: Commit**

```bash
git add docker/
git commit -m "chore: deprecate legacy compose files in docker/ directory

Root-level compose files (core, infra, gpu) replace these. Preserved
for reference until cleanup."
```

---

### Task 11: Update README Quickstart

**Files:**
- Modify: `README.md`

Update the quickstart section to reflect the new setup flow.

- [ ] **Step 1: Read current README quickstart section**

```bash
grep -n 'Quick\|Setup\|Getting Started\|Install' README.md | head -10
```

Find the deployment/quickstart section.

- [ ] **Step 2: Replace the quickstart with the new flow**

Find the deployment section and replace it with:

```markdown
## Quick Start

### Prerequisites

- Linux host (Ubuntu 22.04+ recommended)
- Docker Engine 24+ with Compose v2
- [Tailscale](https://tailscale.com/download) installed and running (`tailscale up`)
- NVIDIA GPU with [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) (optional — enables local inference)

### Install

```bash
git clone https://github.com/tmartin2113/Vibe-Stack.git
cd Vibe-Stack
./setup.sh
docker compose up -d
```

`setup.sh` detects your hardware, generates secrets, and configures the right services:

| Hardware | What You Get |
|----------|-------------|
| NVIDIA GPU (8GB+ VRAM) | Cloud adapters + local inference via vLLM + DeerFlow assistant |
| No GPU | Cloud adapters only (Claude, GPT, Codex) |

After startup, open the Paperclip UI at the URL printed by setup to create your org and agents.

### Compose Profiles

```bash
# Full stack (default with GPU)
docker compose up -d

# Add specific layers manually
docker compose -f docker-compose.yml -f docker-compose.infra.yml up -d
docker compose -f docker-compose.yml -f docker-compose.infra.yml -f docker-compose.gpu.yml up -d
```

### Local Development

To build the server and DeerFlow from local source instead of GHCR images:

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
# Set PAPERCLIP_SOURCE_DIR in .env
docker compose up -d --build
```
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: update README quickstart for new compose structure

Three-command setup: clone, setup.sh, docker compose up. Documents
compose profiles and local development override."
```

---

### Task 12: Delete Backup and Final Validation

**Files:**
- Delete: `docker-compose.yml.bak`

- [ ] **Step 1: Remove the backup**

```bash
rm -f docker-compose.yml.bak
```

- [ ] **Step 2: Validate all compose profiles**

```bash
# Core only
docker compose -f docker-compose.yml config --quiet && echo "core: OK"

# Core + infra
docker compose -f docker-compose.yml -f docker-compose.infra.yml config --quiet && echo "core+infra: OK"

# Full stack
docker compose -f docker-compose.yml -f docker-compose.infra.yml -f docker-compose.gpu.yml config --quiet && echo "full: OK"
```

Expected: all three print OK.

- [ ] **Step 3: Verify COMPOSE_FILE works from .env**

```bash
echo 'COMPOSE_FILE=docker-compose.yml:docker-compose.infra.yml' > /tmp/test-env
docker compose --env-file /tmp/test-env config --quiet && echo "COMPOSE_FILE: OK"
rm /tmp/test-env
```

Expected: `COMPOSE_FILE: OK`

- [ ] **Step 4: Final commit**

```bash
git commit --allow-empty -m "milestone: Vibe Stack compose restructure complete

All service definitions tracked in git. Three composable profiles:
- docker-compose.yml (core: server, deerflow, vibe, tailscale)
- docker-compose.infra.yml (gitea, minio, penpot, searxng, playwright)
- docker-compose.gpu.yml (vllm, opensandbox, comfyui)

setup.sh auto-configures COMPOSE_FILE based on GPU detection.
GHCR images pending paperclip fork CI pipeline."
```
