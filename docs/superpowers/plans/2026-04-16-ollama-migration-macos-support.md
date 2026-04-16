# Ollama Migration + macOS Support — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace vLLM with Ollama as the unified LLM backend on both Linux and macOS, and add full macOS platform support to setup.sh.

**Architecture:** Ollama runs natively on the host (not in Docker) and exposes an OpenAI-compatible API at port 11434. The existing `vibe/backends/vllm.py` works with it unmodified. setup.sh detects the host OS (`uname -s`) and branches into Linux or macOS flows, sharing common steps (secrets, skill sources, Docker image pulls, stack startup, org bootstrap) while skipping platform-specific ones (NVIDIA toolkit, iptables, systemd services on Mac; `pf` firewall on Mac only).

**Tech Stack:** Bash (setup.sh), Docker Compose, Ollama, macOS `pf` firewall, `brew`, `launchctl`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `setup.sh` | Modify | Add OS detection, Ollama install, macOS support, remove vLLM auto-tuning |
| `docker-compose.gpu.yml` | Modify | Remove vllm service, keep opensandbox + comfyui |
| `docker-compose.yml` | Modify | Update vibe service backend defaults to Ollama |
| `Dockerfile` | Modify | Update default VIBE_BACKEND_HOST/PORT |
| `.env.example` | Modify | Add OLLAMA_MODEL, update defaults, document legacy VLLM_* |
| `agents/infra_health.py` | Modify | Update service registry — add ollama, make vllm conditional |

---

## Task 1: OS Detection + Platform Branching in setup.sh

**Files:**
- Modify: `setup.sh:1-47` (header and distro detection)
- Modify: `setup.sh:126` (root check)

- [ ] **Step 1: Add OS detection before distro detection (line 15)**

Insert after the color/logging definitions (after line 18), before distro detection (line 20):

```bash
# ── Platform detection ───────────────────────────────────────
HOST_OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$HOST_OS" in
    linux)  HOST_OS="linux" ;;
    darwin) HOST_OS="darwin" ;;
    *)      error "Unsupported platform: $HOST_OS (only Linux and macOS are supported)" ;;
esac

HOST_ARCH="$(uname -m)"
info "Platform: $HOST_OS ($HOST_ARCH)"
```

- [ ] **Step 2: Guard distro detection with Linux check**

Wrap the existing distro detection block (lines 20-47) in a platform check:

```bash
if [[ "$HOST_OS" == "linux" ]]; then
    # ── Distro detection ──────────────────────────────────────────
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        DISTRO_ID="${ID:-unknown}"
        DISTRO_FAMILY="${ID_LIKE:-$DISTRO_ID}"
    else
        error "Cannot detect distribution — /etc/os-release not found"
    fi

    # Normalize to family
    case "$DISTRO_ID" in
        ubuntu|debian|pop|linuxmint|elementary|zorin)   DISTRO_FAMILY="debian" ;;
        fedora|rhel|centos|rocky|alma|nobara)           DISTRO_FAMILY="fedora" ;;
        arch|manjaro|endeavouros|garuda)                 DISTRO_FAMILY="arch" ;;
        opensuse*|sles)                                  DISTRO_FAMILY="suse" ;;
        *)
            case "$DISTRO_FAMILY" in
                *debian*|*ubuntu*)  DISTRO_FAMILY="debian" ;;
                *fedora*|*rhel*)    DISTRO_FAMILY="fedora" ;;
                *arch*)             DISTRO_FAMILY="arch" ;;
                *suse*)             DISTRO_FAMILY="suse" ;;
                *)                  warn "Unknown distro '$DISTRO_ID' — will attempt Debian-style commands" ; DISTRO_FAMILY="debian" ;;
            esac
            ;;
    esac
    info "Detected distro: $DISTRO_ID (family: $DISTRO_FAMILY)"
else
    DISTRO_FAMILY="darwin"
    DISTRO_ID="macos"
    info "Detected macOS $(sw_vers -productVersion 2>/dev/null || echo 'unknown')"
fi
```

- [ ] **Step 3: Add darwin case to package manager functions**

Add `darwin)` cases to the three package manager functions:

```bash
pkg_update() {
    case "$DISTRO_FAMILY" in
        debian) apt-get update -qq ;;
        fedora) dnf check-update -q || true ;;
        arch)   pacman -Sy --noconfirm ;;
        suse)   zypper --non-interactive refresh ;;
        darwin) brew update ;;
    esac
}

pkg_install() {
    case "$DISTRO_FAMILY" in
        debian) apt-get install -y --no-install-recommends "$@" ;;
        fedora) dnf install -y "$@" ;;
        arch)   pacman -S --needed --noconfirm "$@" ;;
        suse)   zypper --non-interactive install "$@" ;;
        darwin) brew install "$@" 2>/dev/null || true ;;
    esac
}

pkg_installed() {
    case "$DISTRO_FAMILY" in
        debian) dpkg -s "$1" &>/dev/null ;;
        fedora) rpm -q "$1" &>/dev/null ;;
        arch)   pacman -Qi "$1" &>/dev/null ;;
        suse)   rpm -q "$1" &>/dev/null ;;
        darwin) brew list "$1" &>/dev/null ;;
    esac
}
```

- [ ] **Step 4: Make root check platform-aware (line 126)**

Replace:
```bash
[[ "$EUID" -eq 0 ]] || error "Run as root: sudo ./setup.sh"
```

With:
```bash
if [[ "$HOST_OS" == "linux" ]]; then
    [[ "$EUID" -eq 0 ]] || error "Run as root: sudo ./setup.sh"
fi
```

- [ ] **Step 5: Verify script still parses**

Run: `cd ~/Repos/Vibe-Stack && bash -n setup.sh`
Expected: No syntax errors

- [ ] **Step 6: Commit**

```bash
git add setup.sh
git commit -m "feat: add OS/arch detection and macOS platform support to setup.sh"
```

---

## Task 2: Replace vLLM with Ollama in setup.sh

**Files:**
- Modify: `setup.sh:349-527` (Steps 5 and 5b — NVIDIA toolkit + vLLM tuning)

- [ ] **Step 1: Replace NVIDIA Container Toolkit step (step 5)**

Replace the entire step 5 section (lines 349-382) with a guarded version:

```bash
# ══════════════════════════════════════════════════════════════
# 5. NVIDIA Container Toolkit (Linux only — needed for opensandbox/comfyui)
# ══════════════════════════════════════════════════════════════
HAS_NVIDIA_GPU=false
if [[ "$HOST_OS" == "linux" ]]; then
    step "NVIDIA Container Toolkit"
    if command -v nvidia-smi &>/dev/null; then
        HAS_NVIDIA_GPU=true
        if ! pkg_installed nvidia-container-toolkit; then
            info "Installing NVIDIA Container Toolkit..."
            case "$DISTRO_FAMILY" in
                debian)
                    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
                        | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
                    curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
                        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
                        > /etc/apt/sources.list.d/nvidia-container-toolkit.list
                    ;;
                fedora)
                    curl -s -L https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
                        | tee /etc/yum.repos.d/nvidia-container-toolkit.repo > /dev/null
                    ;;
                arch)
                    info "On Arch, install nvidia-container-toolkit from AUR if not in repos"
                    ;;
                suse)
                    zypper --non-interactive addrepo https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo 2>/dev/null || true
                    ;;
            esac
            pkg_update
            pkg_install nvidia-container-toolkit
            nvidia-ctk runtime configure --runtime=docker
            systemctl restart docker
            success "NVIDIA Container Toolkit installed"
        else
            success "NVIDIA Container Toolkit already present"
        fi
    else
        info "No NVIDIA GPU detected — skipping NVIDIA Container Toolkit"
    fi
else
    step "GPU detection (macOS)"
    info "macOS uses Ollama with Metal acceleration — no NVIDIA toolkit needed"
fi
```

- [ ] **Step 2: Replace vLLM auto-tuning with Ollama setup (step 5b)**

Replace the entire step 5b section (lines 384-527) with Ollama setup:

```bash
# ══════════════════════════════════════════════════════════════
# 5b. Ollama — LLM backend (replaces vLLM)
# ══════════════════════════════════════════════════════════════
# Ollama runs natively on the host (not in Docker) and provides an
# OpenAI-compatible API at port 11434. The vibe agent's backend code
# works with it unmodified. Ollama uses CUDA on Linux (if available)
# and Metal on macOS (Apple Silicon).

step "Ollama LLM backend"

OLLAMA_SKIP=false

# Install Ollama if not present
if ! command -v ollama &>/dev/null; then
    info "Installing Ollama..."
    if [[ "$HOST_OS" == "darwin" ]]; then
        brew install ollama
    else
        curl -fsSL https://ollama.com/install.sh | sh
    fi
    success "Ollama installed"
else
    success "Ollama already present ($(ollama --version 2>/dev/null || echo 'unknown'))"
fi

# Start Ollama service
if [[ "$HOST_OS" == "darwin" ]]; then
    if ! pgrep -q ollama; then
        brew services start ollama 2>/dev/null || ollama serve &>/dev/null &
        sleep 2
    fi
    success "Ollama service running"
else
    systemctl enable --now ollama 2>/dev/null || true
    success "Ollama service enabled"
fi

# Detect available memory for model selection
AVAILABLE_MEM_MB=0
if [[ "$HOST_OS" == "darwin" ]]; then
    # macOS: unified memory via sysctl
    TOTAL_BYTES=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
    AVAILABLE_MEM_MB=$(( TOTAL_BYTES / 1048576 ))
elif [[ "$HAS_NVIDIA_GPU" == "true" ]]; then
    # Linux with NVIDIA GPU: use VRAM
    AVAILABLE_MEM_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
else
    # Linux without GPU: use system RAM
    AVAILABLE_MEM_MB=$(free -m 2>/dev/null | awk '/^Mem:/{print $2}')
fi

AVAILABLE_MEM_GB=$(( AVAILABLE_MEM_MB / 1024 ))
info "Available memory for LLM: ${AVAILABLE_MEM_MB} MiB (~${AVAILABLE_MEM_GB} GB)"

# Model selection by memory tier
if (( AVAILABLE_MEM_MB >= 40960 )); then
    OLLAMA_MODEL="qwen3.5:27b"
    info "Tier: >= 40 GB — full 27B model"
elif (( AVAILABLE_MEM_MB >= 20480 )); then
    OLLAMA_MODEL="qwen3.5:9b"
    info "Tier: >= 20 GB — 9B model (sweet spot)"
elif (( AVAILABLE_MEM_MB >= 12288 )); then
    OLLAMA_MODEL="qwen3.5:9b"
    info "Tier: 12-19 GB — 9B model (reduced context)"
elif (( AVAILABLE_MEM_MB >= 8192 )); then
    OLLAMA_MODEL="qwen3.5:4b"
    info "Tier: 8-11 GB — 4B model"
else
    warn "Available memory (${AVAILABLE_MEM_MB} MiB) too low for local LLM"
    warn "Configure OPENAI_API_KEY or ANTHROPIC_API_KEY in .env for cloud inference"
    OLLAMA_SKIP=true
fi

if [[ "$OLLAMA_SKIP" == "false" ]]; then
    success "Selected model: ${OLLAMA_MODEL}"

    # Pre-pull the model
    if ollama list 2>/dev/null | grep -q "${OLLAMA_MODEL%%:*}"; then
        success "Model ${OLLAMA_MODEL} already pulled"
    else
        info "Pulling ${OLLAMA_MODEL} (this may take a few minutes on first run)..."
        ollama pull "$OLLAMA_MODEL"
        success "Model ${OLLAMA_MODEL} pulled"
    fi

    _update_env_var "OLLAMA_MODEL" "${OLLAMA_MODEL}"
    _update_env_var "VIBE_BACKEND_HOST" "host.docker.internal"
    _update_env_var "VIBE_BACKEND_PORT" "11434"

    # MiroFish follows the same model
    if [ -z "${MIROFISH_LLM_MODEL:-}" ]; then
        _update_env_var "MIROFISH_LLM_MODEL" "${OLLAMA_MODEL}"
        _update_env_var "MIROFISH_LLM_API_URL" "http://host.docker.internal:11434/v1"
        success "MIROFISH_LLM_MODEL=${OLLAMA_MODEL} (follows OLLAMA_MODEL)"
    fi
else
    warn "No local LLM configured — agents will need cloud API keys"
fi

# ── Set COMPOSE_FILE based on GPU availability ─────────────────
# GPU compose is only needed for opensandbox/comfyui (not for LLM inference).
if [[ "$HAS_NVIDIA_GPU" == "true" ]]; then
    COMPOSE_FILE="docker-compose.yml:docker-compose.infra.yml:docker-compose.gpu.yml"
    _update_env_var "COMPOSE_FILE" "$COMPOSE_FILE"
    export COMPOSE_FILE
    info "Compose profile: full stack (core + infra + gpu sandbox)"
else
    COMPOSE_FILE="docker-compose.yml:docker-compose.infra.yml"
    _update_env_var "COMPOSE_FILE" "$COMPOSE_FILE"
    export COMPOSE_FILE
    info "Compose profile: standard (core + infra, no GPU sandbox)"
fi
```

- [ ] **Step 3: Verify script parses**

Run: `cd ~/Repos/Vibe-Stack && bash -n setup.sh`
Expected: No syntax errors

- [ ] **Step 4: Commit**

```bash
git add setup.sh
git commit -m "feat: replace vLLM with Ollama as unified LLM backend in setup.sh"
```

---

## Task 3: macOS Guards for Linux-only Setup Steps

**Files:**
- Modify: `setup.sh:210-347` (steps 1-4: port check, prerequisites, Docker)
- Modify: `setup.sh:529-960` (steps 6-13: Caddy, secrets, workspace, SSH, security, Docker creds, stack start)
- Modify: `setup.sh:1024-1205` (steps 14-24: iptables, watchdog, auditd, fail2ban, upgrades, bootstrap)

- [ ] **Step 1: Port conflict check — use lsof on macOS**

Replace the port check block (step 1, lines 213-231). The `ss` command doesn't exist on macOS — use `lsof`:

```bash
step "Checking for port conflicts"
REQUIRED_PORTS="3100 8868 9000"
CONFLICTS=""
for port in $REQUIRED_PORTS; do
    if [[ "$HOST_OS" == "darwin" ]]; then
        pid=$(lsof -ti ":$port" 2>/dev/null | head -1 || true)
    else
        pid=$(ss -tlnp "sport = :$port" 2>/dev/null | awk 'NR>1 {print $6}' | grep -oP 'pid=\K\d+' | head -1 || true)
    fi
    if [[ -n "$pid" ]]; then
        pname=$(ps -p "$pid" -o comm= 2>/dev/null || echo "unknown")
        CONFLICTS="${CONFLICTS}\n  Port $port — PID $pid ($pname)"
    fi
done

if [[ -n "$CONFLICTS" ]]; then
    warn "The following ports are already in use:"
    printf "${CONFLICTS}\n"
    printf "\n${YELLOW}Vibe Stack needs these ports. Stop the conflicting processes or press Enter to continue anyway.${NC}\n"
    printf "  Press Ctrl+C to abort, or Enter to continue: "
    read -r
fi
success "Port check complete"
```

- [ ] **Step 2: Prerequisites — add macOS case**

Add a `darwin)` case to the prerequisites section (step 3, around line 248):

```bash
    darwin)
        if ! command -v brew &>/dev/null; then
            error "Homebrew is required on macOS. Install from https://brew.sh"
        fi
        pkg_install \
            jq wget git git-lfs \
            python3 node
        ;;
```

- [ ] **Step 3: Docker — handle Docker Desktop on macOS**

Add a macOS branch to the Docker step (step 4, around line 296):

```bash
if [[ "$HOST_OS" == "darwin" ]]; then
    step "Docker Desktop"
    if ! command -v docker &>/dev/null; then
        error "Docker Desktop is required on macOS. Install from https://docs.docker.com/desktop/install/mac-install/"
    fi
    if ! docker compose version &>/dev/null; then
        error "Docker Compose not available — ensure Docker Desktop is running"
    fi
    success "Docker Desktop $(docker --version | grep -oP '\d+\.\d+\.\d+' | head -1) with Compose $(docker compose version --short)"
else
    # ... existing Linux Docker installation ...
fi
```

- [ ] **Step 4: Caddy — brew on macOS (step 6)**

Wrap the Caddy build section. On macOS, use `brew install caddy` and `brew services`:

```bash
if [[ "$HOST_OS" == "darwin" ]]; then
    step "Caddy"
    if ! command -v caddy &>/dev/null; then
        brew install caddy
    fi
    success "Caddy $(caddy version 2>/dev/null | head -1 || echo 'installed')"
else
    # ... existing Linux Caddy build from source with rate-limit plugin ...
fi
```

- [ ] **Step 5: Workspace — skip SFTP setup on macOS (step 8)**

Wrap the workspace step. On macOS, use a simpler local directory:

```bash
if [[ "$HOST_OS" == "darwin" ]]; then
    step "Configuring workspace"
    WORKSPACE_PATH="${WORKSPACE_PATH:-$HOME/vibe-workspace}"
    mkdir -p "$WORKSPACE_PATH"
    _update_env_var "WORKSPACE_PATH" "${WORKSPACE_PATH}"
    if [[ ! -d "$WORKSPACE_PATH/.git" ]]; then
        git -C "$WORKSPACE_PATH" init
        git -C "$WORKSPACE_PATH" config user.email "watchdog@localhost"
        git -C "$WORKSPACE_PATH" config user.name  "Vibe Watchdog"
        success "Workspace initialized at $WORKSPACE_PATH"
    else
        success "Workspace already initialized"
    fi
else
    # ... existing Linux SFTP workspace setup ...
fi
```

- [ ] **Step 6: SSH/Tailscale SSH — skip custom SSH on macOS (steps 9-10)**

Wrap steps 9 and 10 in Linux guards:

```bash
if [[ "$HOST_OS" == "linux" ]]; then
    step "Configuring SSH"
    # ... existing SSH config ...
    step "Enabling Tailscale SSH"
    tailscale set --ssh=true
    success "Tailscale SSH enabled"
else
    step "Tailscale"
    if ! command -v tailscale &>/dev/null; then
        warn "Install Tailscale from https://tailscale.com/download/mac"
    fi
    success "Tailscale SSH: use Tailscale app preferences on macOS"
fi
```

- [ ] **Step 7: Caddy config — brew services on macOS (step 11)**

On macOS, write a simpler Caddyfile and use `brew services`:

```bash
if [[ "$HOST_OS" == "darwin" ]]; then
    step "Configuring Caddy"
    mkdir -p /usr/local/etc/caddy 2>/dev/null || mkdir -p "$HOME/.config/caddy"
    CADDY_CONFIG_DIR="${HOME}/.config/caddy"
    cp Caddyfile "$CADDY_CONFIG_DIR/Caddyfile"
    # On macOS, Caddy runs via brew services (launchd)
    brew services restart caddy 2>/dev/null || caddy start --config "$CADDY_CONFIG_DIR/Caddyfile" &
    success "Caddy configured"
else
    # ... existing Linux systemd Caddy config ...
fi
```

- [ ] **Step 8: Docker creds — simpler on macOS (step 12a)**

On macOS, Docker Desktop handles credential storage via Keychain:

```bash
if [[ "$HOST_OS" == "darwin" ]]; then
    step "Docker credentials + GHCR auth"
    # Docker Desktop on macOS uses osxkeychain by default
    if ! docker pull "ghcr.io/${GHCR_ORG}/paperclip-server:latest" >/dev/null 2>&1; then
        info "GHCR authentication required"
        if [[ -f "secrets/github_token.txt" ]] && [[ -s "secrets/github_token.txt" ]]; then
            cat secrets/github_token.txt | docker login ghcr.io -u "${GIT_USER}" --password-stdin 2>&1 | grep -v "WARNING" || true
            success "Authenticated with GHCR"
        else
            warn "Cannot authenticate — create secrets/github_token.txt"
        fi
    else
        success "GHCR authentication already configured"
    fi
else
    # ... existing Linux credential helper setup ...
fi
```

- [ ] **Step 9: Guard all Linux-only security steps (steps 14-18)**

Wrap iptables, watchdog, auditd, fail2ban, and unattended-upgrades in a single Linux guard:

```bash
if [[ "$HOST_OS" == "linux" ]]; then
    # Step 14: iptables
    step "iptables rules"
    chmod +x iptables-setup.sh
    ./iptables-setup.sh
    success "iptables rules applied"
    # ... iptables systemd service ...

    # Step 15: Watchdog
    step "Workspace watchdog service"
    # ... existing watchdog systemd ...

    # Step 16: auditd
    step "auditd rules"
    # ... existing auditd ...

    # Step 17: fail2ban
    step "fail2ban"
    # ... existing fail2ban ...

    # Step 18: Unattended upgrades
    step "Unattended security upgrades"
    # ... existing upgrades ...
else
    step "macOS firewall"
    info "Configuring pf firewall for Tailscale-only access..."
    # Create a pf anchor for Vibe Stack
    PF_ANCHOR="/etc/pf.anchors/com.vibe-stack"
    if [[ ! -f "$PF_ANCHOR" ]]; then
        sudo tee "$PF_ANCHOR" > /dev/null << 'PFEOF'
# Vibe Stack: allow Tailscale (utun*) and localhost only on service ports
# Block external access to Docker-published ports
services_ports = "{ 3000, 3003, 3100, 5001, 8868, 9000, 9001 }"
pass in quick on lo0 proto tcp to any port $services_ports
pass in quick on utun0 proto tcp to any port $services_ports
pass in quick on utun1 proto tcp to any port $services_ports
pass in quick on utun2 proto tcp to any port $services_ports
block in quick proto tcp to any port $services_ports
PFEOF
        info "pf anchor written to $PF_ANCHOR"
        # Add anchor to main pf.conf if not present
        if ! grep -q "com.vibe-stack" /etc/pf.conf 2>/dev/null; then
            sudo cp /etc/pf.conf /etc/pf.conf.backup
            echo 'anchor "com.vibe-stack"' | sudo tee -a /etc/pf.conf > /dev/null
            echo 'load anchor "com.vibe-stack" from "/etc/pf.anchors/com.vibe-stack"' | sudo tee -a /etc/pf.conf > /dev/null
        fi
        sudo pfctl -f /etc/pf.conf 2>/dev/null || warn "pf reload failed — may need to enable pf in System Preferences"
        success "pf firewall configured (Tailscale + localhost only)"
    else
        success "pf firewall already configured"
    fi
fi
```

- [ ] **Step 10: Update TOTAL_STEPS based on platform**

After platform detection, adjust the step count:

```bash
if [[ "$HOST_OS" == "darwin" ]]; then
    TOTAL_STEPS=16  # macOS skips 8 Linux-only steps
fi
```

- [ ] **Step 11: Update final output**

In the completion banner at the end, add Ollama info:

```bash
if [[ "${OLLAMA_SKIP:-true}" == "false" ]]; then
    printf "  Ollama model: ${BLUE}${OLLAMA_MODEL}${NC}\n"
    printf "  Ollama API:   ${BLUE}http://localhost:11434${NC}\n"
fi
```

- [ ] **Step 12: Verify script parses**

Run: `cd ~/Repos/Vibe-Stack && bash -n setup.sh`
Expected: No syntax errors

- [ ] **Step 13: Commit**

```bash
git add setup.sh
git commit -m "feat: add macOS guards for Linux-only steps, pf firewall, brew services"
```

---

## Task 4: Update Docker Compose + Dockerfile Defaults

**Files:**
- Modify: `docker-compose.gpu.yml:20-90` (remove vllm service)
- Modify: `docker-compose.yml:171-172` (update vibe backend defaults)
- Modify: `Dockerfile:61-62` (update default env vars)

- [ ] **Step 1: Remove vllm service from docker-compose.gpu.yml**

Remove lines 20-90 (the vllm service definition and all its comments). Keep opensandbox, comfyui, and the `comfyui-data` volume. Remove the `vllm-models` volume.

The updated file header should reflect the change:

```yaml
# ══════════════════════════════════════════════════════════════════════════════
# docker-compose.gpu.yml — GPU Services (Sandbox + Image Generation)
#
# Requires: NVIDIA GPU + nvidia-container-toolkit installed on the host.
# Note: LLM inference is handled by Ollama (runs natively on host, not here).
#
# These services are NOT started automatically. Use -f to merge with core:
#
#   docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
#
# Related compose files:
#   docker-compose.yml       — Core services: server, deerflow, vibe, tailscale
#   docker-compose.infra.yml — Infrastructure: gitea, minio, penpot, searxng, playwright
# ══════════════════════════════════════════════════════════════════════════════

services:

  # ── OpenSandbox (secure code execution sandbox) ────────────────────────────
  opensandbox:
    # ... unchanged ...

  # ── ComfyUI (image generation UI) ──────────────────────────────────────────
  comfyui:
    # ... unchanged ...

volumes:
  comfyui-data:
```

- [ ] **Step 2: Update vibe service backend defaults in docker-compose.yml**

Change line 172:

```yaml
      - VIBE_BACKEND_PORT=${VIBE_BACKEND_PORT:-11434}
```

(VIBE_BACKEND_HOST already defaults to `host.docker.internal` on line 171)

- [ ] **Step 3: Update Dockerfile defaults**

Change lines 61-62:

```dockerfile
ENV VIBE_BACKEND_HOST=host.docker.internal
ENV VIBE_BACKEND_PORT=11434
```

- [ ] **Step 4: Verify compose config**

Run: `cd ~/Repos/Vibe-Stack && docker compose config --quiet`
Expected: No errors

- [ ] **Step 5: Commit**

```bash
git add docker-compose.gpu.yml docker-compose.yml Dockerfile
git commit -m "feat: remove vLLM service, update backend defaults to Ollama (port 11434)"
```

---

## Task 5: Update .env.example

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Update .env.example**

Key changes:
1. Replace the vLLM section header and tier table with Ollama equivalents
2. Add `OLLAMA_MODEL` variable
3. Keep VLLM_* vars commented out as legacy reference
4. Update default `VIBE_BACKEND_PORT` comment
5. Update `MIROFISH_LLM_API_URL` default comment

Replace the LLM Backend and vLLM sections (lines 54-109) with:

```
# ── LLM Backend ──────────────────────────────────────────────
# Ollama runs natively on the host and provides an OpenAI-compatible API.
# The vibe agent connects to it via host.docker.internal.
# VIBE_BACKEND_HOST=host.docker.internal
# VIBE_BACKEND_PORT=11434

# ── Ollama (local inference, runs on host) ───────────────────
# Model served by Ollama. Set by setup.sh based on available memory.
# Tier table (matches setup.sh logic):
#
#   ≥ 40 GB                    → qwen3.5:27b     (full 27B model)
#   ≥ 20 GB (3090, M2 32GB)   → qwen3.5:9b      (sweet spot)
#   12-19 GB                   → qwen3.5:9b      (reduced context)
#    8-11 GB                   → qwen3.5:4b      (minimum viable)
#   <  8 GB                    → disabled, use cloud API
OLLAMA_MODEL=

# ── Legacy vLLM (no longer default — use Ollama instead) ─────
# These variables are only needed if you manually run a vLLM server.
# setup.sh no longer configures vLLM. Set VIBE_BACKEND_PORT=8000
# and configure these if you want to use vLLM instead of Ollama.
# VLLM_MODEL=
# VLLM_API_URL=http://host.docker.internal:8000/v1
# VLLM_TOOL_CALL_PARSER=qwen3_xml
# VLLM_QUANTIZATION=awq
# VLLM_MAX_MODEL_LEN=65536
# VLLM_MAX_NUM_SEQS=4
# VLLM_GPU_MEM_UTIL=0.92
```

Also update the MiroFish section (around line 261) default comment:

```
# MIROFISH_LLM_API_URL=http://host.docker.internal:11434/v1  # Local Ollama (default)
```

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "feat: update .env.example for Ollama migration, document legacy vLLM vars"
```

---

## Task 6: Update Infrastructure Health Registry

**Files:**
- Modify: `agents/infra_health.py:33-70` (SERVICE_REGISTRY)

- [ ] **Step 1: Update SERVICE_REGISTRY**

Replace the `vllm` entry with an `ollama` entry. Since Ollama runs on the host (not in Docker), the probe URL uses `host.docker.internal`:

```python
    "ollama": {
        "url": "http://host.docker.internal:11434/api/tags",
        "label": "Ollama LLM",
    },
```

Remove the `vllm` entry from the registry.

- [ ] **Step 2: Run infra health tests**

Run: `cd ~/Repos/Vibe-Stack && python3 -m pytest tests/test_infra_health.py -x --no-header -q`

Fix any test that asserts `vllm` is in the registry — update to assert `ollama` instead.

- [ ] **Step 3: Commit**

```bash
git add agents/infra_health.py tests/test_infra_health.py
git commit -m "feat: replace vllm with ollama in infrastructure health registry"
```

---

## Task 7: Full Test Suite Verification

- [ ] **Step 1: Run all Python tests**

Run: `cd ~/Repos/Vibe-Stack && python3 -m pytest tests/ -x -m "not e2e" --no-header -q`
Expected: All tests pass (no regressions)

- [ ] **Step 2: Validate all compose files**

Run: `cd ~/Repos/Vibe-Stack && docker compose config --quiet && docker compose -f docker-compose.yml -f docker-compose.infra.yml config --quiet`
Expected: No errors

- [ ] **Step 3: Validate setup.sh syntax**

Run: `cd ~/Repos/Vibe-Stack && bash -n setup.sh`
Expected: No syntax errors

- [ ] **Step 4: Verify Ollama connectivity from vibe container**

Run: `docker exec vibe-stack-vibe-1 python3 -c "import urllib.request; r=urllib.request.urlopen('http://host.docker.internal:11434/api/tags', timeout=3); print(r.status, r.read().decode()[:100])"`
Expected: 200 with JSON listing models

---

## Task 8: Documentation Update

- [ ] **Step 1: Update CLAUDE.md**

In the `CLAUDE.md` LLM Backend section, update the table:

Change the vLLM row to:
```
| **Ollama** | (native host) | OpenAI-compatible `http://host.docker.internal:11434/v1` | CUDA (Linux), Metal (macOS) |
```

Add a note:
```
**LLM Backend:** Ollama runs natively on the host (not in Docker). setup.sh installs it and pulls
the appropriate model based on available memory. The vibe agent connects via `host.docker.internal:11434`.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for Ollama migration"
```
