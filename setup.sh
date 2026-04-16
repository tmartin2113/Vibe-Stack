#!/usr/bin/env bash
# setup.sh — Vibe Stack 2.0 (Paperclip + DeerFlow Agent Network)
# One-shot first-time deployment. Run as root (Linux) or normally (macOS).
# Supports: Ubuntu/Debian, Fedora/RHEL/CentOS/Rocky, Arch/Manjaro, openSUSE, macOS
# Idempotent — safe to re-run.

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
info()    { printf "${BLUE}[INFO]${NC}   %s\n" "$*"; }
success() { printf "${GREEN}[OK]${NC}     %s\n" "$*"; }
warn()    { printf "${YELLOW}[WARN]${NC}   %s\n" "$*"; }
error()   { printf "${RED}[ERROR]${NC}  %s\n" "$*" >&2; exit 1; }
err()     { printf "${RED}[ERROR]${NC}  %s\n" "$*" >&2; }

TOTAL_STEPS=24
CURRENT_STEP=0
step() { CURRENT_STEP=$((CURRENT_STEP + 1)); printf "\n${BOLD}[Step %d/%d]${NC} ${BLUE}%s${NC}\n" "$CURRENT_STEP" "$TOTAL_STEPS" "$*"; }

# ── Platform detection ───────────────────────────────────────
HOST_OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$HOST_OS" in
    linux)  HOST_OS="linux" ;;
    darwin) HOST_OS="darwin" ;;
    *)      error "Unsupported platform: $HOST_OS (only Linux and macOS are supported)" ;;
esac

HOST_ARCH="$(uname -m)"
info "Platform: $HOST_OS ($HOST_ARCH)"

if [[ "$HOST_OS" == "darwin" ]]; then
    TOTAL_STEPS=16  # macOS skips 8 Linux-only steps
fi

# ── Distro detection ──────────────────────────────────────────
if [[ "$HOST_OS" == "linux" ]]; then
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
            # Check ID_LIKE for derivatives
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

# ── Package manager abstraction ────────────────────────────────
pkg_update() {
    case "$DISTRO_FAMILY" in
        debian) apt-get update -qq ;;
        fedora) dnf check-update -q || true ;;  # dnf returns 100 when updates available
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

# wait_healthy TIMEOUT_SECS service [service...]
wait_healthy() {
    local timeout="${1:?usage: wait_healthy TIMEOUT svc...}"; shift
    local deadline=$(( SECONDS + timeout ))
    local services=("$@")

    while (( SECONDS < deadline )); do
        local all_healthy=true
        for svc in "${services[@]}"; do
            local cid
            cid=$(docker compose ps -q "$svc" 2>/dev/null)
            if [[ -z "$cid" ]]; then
                warn "  $svc — container not found"
                all_healthy=false; continue
            fi
            local status
            status=$(docker inspect --format='{{.State.Health.Status}}' "$cid" 2>/dev/null || echo "unknown")
            case "$status" in
                healthy)  ;;
                unhealthy)
                    # During startup, unhealthy may just mean "still loading"
                    # (e.g. model download or initialization). Keep waiting until timeout.
                    all_healthy=false ;;
                *)  all_healthy=false ;;
            esac
        done
        if $all_healthy; then
            for svc in "${services[@]}"; do success "  $svc — healthy"; done
            return 0
        fi
        sleep 2
    done

    for svc in "${services[@]}"; do
        local cid status
        cid=$(docker compose ps -q "$svc" 2>/dev/null)
        if [[ -z "$cid" ]]; then
            err "$svc — container never started"
            continue
        fi
        status=$(docker inspect --format='{{.State.Health.Status}}' "$cid" 2>/dev/null || echo "unknown")
        if [[ "$status" != "healthy" ]]; then
            err "$svc — timed out (status: $status). Last 20 log lines:"
            docker logs --tail 20 "$cid" 2>&1 | sed 's/^/         /'
        fi
    done
    error "Timed out after ${timeout}s waiting for: ${services[*]}"
}

if [[ "$HOST_OS" == "linux" ]]; then
    [[ "$EUID" -eq 0 ]] || error "Run as root: sudo ./setup.sh"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ══════════════════════════════════════════════════════════════
# 0. Load .env
# ══════════════════════════════════════════════════════════════
step "Loading .env"
if [[ ! -f ".env" ]]; then
    cp .env.example .env
    warn ".env not found — created from .env.example"
fi
set -a; source .env; set +a

# Auto-detect Tailscale if not set in .env
if [[ -z "${TAILSCALE_IP:-}" ]]; then
    TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || true)
    [[ -z "$TAILSCALE_IP" ]] && error "Tailscale not running — install and run 'tailscale up' first"
fi

if [[ -z "${TAILSCALE_HOSTNAME:-}" ]] || [[ "$TAILSCALE_HOSTNAME" == your-pc-name* ]]; then
    CURRENT_TS_NAME=$(tailscale status --json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null || echo "")
    TS_TAILNET=$(echo "$CURRENT_TS_NAME" | sed 's/^[^.]*\.//')

    printf "\n${YELLOW}Choose a short hostname for this server on Tailscale:${NC}\n"
    printf "  Current: ${BLUE}${CURRENT_TS_NAME}${NC}\n"
    printf "  Enter a short name (e.g. 'vibe') or press Enter to keep current: "
    read -r CUSTOM_HOSTNAME
    if [[ -n "$CUSTOM_HOSTNAME" ]]; then
        tailscale set --hostname="$CUSTOM_HOSTNAME"
        TAILSCALE_HOSTNAME="${CUSTOM_HOSTNAME}.${TS_TAILNET}"
        info "Hostname set to $TAILSCALE_HOSTNAME"
    else
        TAILSCALE_HOSTNAME="$CURRENT_TS_NAME"
    fi
fi

: "${WORKSPACE_PATH:=/srv/sftp/workspace/files}"
[[ -z "${GIT_USER:-}" || "$GIT_USER" == "your-github-username" ]] && error "GIT_USER not set — edit .env with your GitHub username"
[[ -z "${GHCR_ORG:-}" || "$GHCR_ORG" == "your-github-username" ]] && error "GHCR_ORG not set — edit .env with the GitHub org/user that hosts the GHCR images"

# Persist auto-detected values to .env
_update_env_var() {
    local key="$1" val="$2" file=".env"
    if grep -q "^${key}=" "$file" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$file"
    else
        echo "${key}=${val}" >> "$file"
    fi
}

touch .env
_update_env_var "TAILSCALE_HOSTNAME" "${TAILSCALE_HOSTNAME}"
_update_env_var "TAILSCALE_IP" "${TAILSCALE_IP}"
_update_env_var "WORKSPACE_PATH" "${WORKSPACE_PATH}"
_update_env_var "GIT_USER" "${GIT_USER}"

# Infrastructure service URLs — these enable agent tools at runtime.
# Uses Docker DNS names (services on the compose default network).
_update_env_var "SEARXNG_URL" "http://searxng:8080"
_update_env_var "PLAYWRIGHT_WS_URL" "ws://playwright:3003"
_update_env_var "PENPOT_API_URL" "http://penpot-backend:3000"
_update_env_var "GITEA_URL" "http://gitea:3000"
_update_env_var "MINIO_URL" "http://minio:9000"
_update_env_var "MIROFISH_URL" "http://mirofish:5001"
_update_env_var "PADDLEOCR_URL" "http://paddleocr:8868"

# Skill source repos — agents scan these for domain-specific skills.
# Colon-separated paths. The workspace scanner finds skills/ and .claude/skills/ in each.
_update_env_var "VIBE_SKILL_REPOS" "${SCRIPT_DIR}/skill-sources/anthropics-skills:${SCRIPT_DIR}/skill-sources/impeccable:${SCRIPT_DIR}/skill-sources/obra-superpowers:${SCRIPT_DIR}/skill-sources/vercel-agent-skills"

success ".env loaded (Tailscale: $TAILSCALE_HOSTNAME / $TAILSCALE_IP)"

# PAPERCLIP_SOURCE_DIR only needed for local dev builds (docker-compose.override.yml)
if [[ -n "${PAPERCLIP_SOURCE_DIR:-}" ]]; then
    if [[ ! -f "$PAPERCLIP_SOURCE_DIR/Dockerfile" ]]; then
        warn "PAPERCLIP_SOURCE_DIR set to $PAPERCLIP_SOURCE_DIR but no Dockerfile found — local builds will fail"
    else
        _update_env_var "PAPERCLIP_SOURCE_DIR" "${PAPERCLIP_SOURCE_DIR}"
        success "Paperclip source: $PAPERCLIP_SOURCE_DIR (for local builds)"
    fi
fi

# ══════════════════════════════════════════════════════════════
# 1. Port conflict check
# ══════════════════════════════════════════════════════════════
step "Checking for port conflicts"
REQUIRED_PORTS="2222 3100 5678 8000 8100 8101 8102 8103 8104 8105 8106 8107 8108 8109 8110 8111 8112 8113 8114 8115 8116 8117 8118 8119 9000"
CONFLICTS=""
for port in $REQUIRED_PORTS; do
    pid=$(ss -tlnp "sport = :$port" 2>/dev/null | awk 'NR>1 {print $6}' | grep -oP 'pid=\K\d+' | head -1 || true)
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

# ══════════════════════════════════════════════════════════════
# 2. Detect host versions
# ══════════════════════════════════════════════════════════════
step "Detecting host versions"
HOST_PYTHON_VERSION=$(python3 --version 2>&1 | grep -oP '\d+\.\d+' | head -1 || echo "3.12")
info "$DISTRO_ID ${VERSION_ID:-unknown} / Python $HOST_PYTHON_VERSION"
success "Host versions detected"

# ══════════════════════════════════════════════════════════════
# 3. System prerequisites
# ══════════════════════════════════════════════════════════════
step "Installing system prerequisites"
pkg_update

# Common packages (names vary slightly by distro)
case "$DISTRO_FAMILY" in
    debian)
        pkg_install \
            apt-transport-https ca-certificates curl gnupg lsb-release \
            inotify-tools git git-lfs acl \
            iptables-persistent netfilter-persistent \
            fail2ban auditd audispd-plugins \
            unattended-upgrades jq wget logwatch \
            dnsutils python3-pip python3-venv \
            nodejs npm
        ;;
    fedora)
        pkg_install \
            ca-certificates curl gnupg2 \
            inotify-tools git git-lfs acl \
            iptables-services \
            fail2ban audit \
            dnf-automatic jq wget logwatch \
            bind-utils python3-pip python3 \
            nodejs npm
        ;;
    arch)
        pkg_install \
            ca-certificates curl gnupg \
            inotify-tools git git-lfs acl \
            iptables \
            fail2ban audit \
            jq wget \
            bind python-pip python \
            nodejs npm
        ;;
    suse)
        pkg_install \
            ca-certificates curl gpg2 \
            inotify-tools git git-lfs acl \
            iptables \
            fail2ban audit \
            jq wget \
            bind-utils python3-pip python3 \
            nodejs npm
        ;;
esac
success "Prerequisites installed"

# ══════════════════════════════════════════════════════════════
# 4. Docker
# ══════════════════════════════════════════════════════════════
step "Docker"
if ! command -v docker &>/dev/null; then
    info "Installing Docker CE..."
    case "$DISTRO_FAMILY" in
        debian)
            curl -fsSL "https://download.docker.com/linux/${DISTRO_ID}/gpg" \
                | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
            echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] \
                https://download.docker.com/linux/${DISTRO_ID} $(lsb_release -cs) stable" \
                > /etc/apt/sources.list.d/docker.list
            pkg_update
            pkg_install docker-ce docker-ce-cli containerd.io docker-compose-plugin
            ;;
        fedora)
            dnf config-manager addrepo --from-repofile="https://download.docker.com/linux/fedora/docker-ce.repo" 2>/dev/null \
                || dnf config-manager --add-repo "https://download.docker.com/linux/fedora/docker-ce.repo" 2>/dev/null || true
            pkg_install docker-ce docker-ce-cli containerd.io docker-compose-plugin
            ;;
        arch)
            pkg_install docker docker-compose docker-buildx
            ;;
        suse)
            zypper --non-interactive addrepo "https://download.docker.com/linux/sles/docker-ce.repo" 2>/dev/null || true
            pkg_install docker-ce docker-ce-cli containerd.io docker-compose-plugin
            ;;
    esac
    systemctl enable --now docker
    success "Docker CE installed"
elif pkg_installed docker-ce 2>/dev/null || command -v docker &>/dev/null; then
    success "Docker already present"
    if ! docker compose version &>/dev/null; then
        info "Installing docker compose plugin..."
        case "$DISTRO_FAMILY" in
            debian) pkg_update; pkg_install docker-compose-plugin 2>/dev/null || pkg_install docker-compose-v2 ;;
            arch)   pkg_install docker-compose ;;
            *)      pkg_install docker-compose-plugin ;;
        esac
        success "Docker compose plugin installed"
    fi
fi

if ! docker buildx version &>/dev/null; then
    info "Installing docker-buildx..."
    case "$DISTRO_FAMILY" in
        arch) pkg_install docker-buildx ;;
        *)    pkg_install docker-buildx-plugin 2>/dev/null || pkg_install docker-buildx 2>/dev/null || warn "docker-buildx not available in repos — install manually" ;;
    esac
fi

if ! docker compose version &>/dev/null; then
    error "Docker Compose plugin not available — 'docker compose version' failed"
fi
success "Docker Compose $(docker compose version --short) available"

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

# ══════════════════════════════════════════════════════════════
# 6. Caddy with rate-limit plugin
# ══════════════════════════════════════════════════════════════
step "Caddy with rate-limit plugin"
if ! command -v caddy &>/dev/null || ! caddy list-modules 2>/dev/null | grep -q "rate_limit"; then
    info "Building Caddy with rate-limit plugin..."

    GO_REQUIRED="1.25"
    GO_INSTALL_VER="1.25.4"
    GO_CURRENT=$(go version 2>/dev/null | grep -oP '\d+\.\d+' | head -1 || echo "0.0")
    if ! printf '%s\n%s\n' "$GO_REQUIRED" "$GO_CURRENT" | sort -V -C; then
        info "Go $GO_CURRENT too old (need >= $GO_REQUIRED) — installing Go $GO_INSTALL_VER..."
        curl -fsSL "https://dl.google.com/go/go${GO_INSTALL_VER}.linux-amd64.tar.gz" -o /tmp/go.tar.gz
        rm -rf /usr/local/go
        tar -C /usr/local -xzf /tmp/go.tar.gz
        rm /tmp/go.tar.gz
        export PATH="/usr/local/go/bin:$PATH"
        success "Go $(go version | grep -oP '\d+\.\d+\.\d+') installed"
    fi

    info "Installing xcaddy via go install..."
    GOBIN=/usr/local/bin go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest
    xcaddy build --with github.com/mholt/caddy-ratelimit --output /usr/local/bin/caddy
    chmod +x /usr/local/bin/caddy
    useradd -r -s /usr/sbin/nologin -d /var/lib/caddy caddy 2>/dev/null || true
    mkdir -p /etc/caddy /var/log/caddy /var/lib/caddy
    chown caddy:caddy /var/log/caddy /var/lib/caddy
    cat > /etc/systemd/system/caddy.service << 'UNIT'
[Unit]
Description=Caddy Web Server
After=network-online.target
Requires=network-online.target

[Service]
Type=notify
User=caddy
Group=caddy
ExecStart=/usr/local/bin/caddy run --environ --config /etc/caddy/Caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --force
TimeoutStopSec=5s
LimitNOFILE=1048576
PrivateTmp=true
ProtectSystem=full
AmbientCapabilities=CAP_NET_BIND_SERVICE
EnvironmentFile=-/etc/caddy/caddy.env
[Install]
WantedBy=multi-user.target
UNIT
    success "Caddy built with rate-limit"
else
    success "Caddy already present"
fi
usermod -aG tailscale caddy 2>/dev/null || true

# ══════════════════════════════════════════════════════════════
# 7. Secrets
# ══════════════════════════════════════════════════════════════
step "Generating secrets"
mkdir -p secrets; chmod 700 secrets

gen_secret() {
    local file="secrets/$1" val="$2"
    [[ -f "$file" ]] && info "Exists: $file" && return
    printf "%s" "$val" > "$file"; chmod 444 "$file"; success "Generated: $file"
}

gen_secret "better_auth_secret.txt"         "$(openssl rand -hex 32)"
gen_secret "agent_jwt_secret.txt"           "$(openssl rand -hex 32)"
gen_secret "paperclip_postgres_password.txt" "$(openssl rand -hex 32)"
gen_secret "searxng_secret.txt"             "$(openssl rand -hex 32)"

# SSH deploy keys for git access (per-repo)
mkdir -p secrets/ssh; chmod 700 secrets/ssh
if [ -z "$(ls -A secrets/ssh/ 2>/dev/null)" ]; then
    warn "secrets/ssh/ is empty — no git push until deploy keys are added"
    warn "-> ssh-keygen -t ed25519 -f secrets/ssh/repo-name -N ''"
    warn "-> Add secrets/ssh/repo-name.pub as a deploy key on GitHub repo"
fi

if [[ ! -f "secrets/github_token.txt" ]] || [[ ! -s "secrets/github_token.txt" ]]; then
    warn "secrets/github_token.txt is missing or empty"
    warn "Claude Code agents need a GitHub token for git operations."
    warn "-> echo 'ghp_your_token_here' > secrets/github_token.txt && chmod 444 secrets/github_token.txt"
fi

success "Secrets ready"

# ══════════════════════════════════════════════════════════════
# 7b. Skill Sources
# ══════════════════════════════════════════════════════════════
step "Setting up skill sources"
mkdir -p skill-sources

clone_if_missing() {
    local url="$1" dest="$2"
    if [[ -d "$dest/.git" ]]; then
        info "  Exists: $dest"
    else
        info "  Cloning $url → $dest"
        if ! GIT_TERMINAL_PROMPT=0 git clone --depth 1 "$url" "$dest" 2>/dev/null; then
            warn "  Failed to clone $url (private or not found) — skipping"
        fi
    fi
}

clone_if_missing "https://github.com/anthropics/skills.git"            "skill-sources/anthropics-skills"
clone_if_missing "https://github.com/obra/superpowers.git"             "skill-sources/obra-superpowers"
clone_if_missing "https://github.com/vercel-labs/agent-skills.git"     "skill-sources/vercel-agent-skills"
clone_if_missing "https://github.com/voltagent/awesome-openclaw-skills.git" "skill-sources/voltagent-skills"
clone_if_missing "https://github.com/openclaw/skills.git"              "skill-sources/openclaw-skills-repo"
clone_if_missing "https://github.com/pbakaus/impeccable.git"           "skill-sources/impeccable"

if [[ -f "skill-sources/openclaw-skills/index.json" ]]; then
    success "OpenClaw skills already fetched — skipping"
else
    info "Fetching OpenClaw skills..."
    node fetch-openclaw-skills.mjs || warn "OpenClaw skill fetch failed (non-fatal)"
fi

success "Skill sources ready"

# ══════════════════════════════════════════════════════════════
# 8. Workspace
# ══════════════════════════════════════════════════════════════
step "Configuring workspace"
info "Path: $WORKSPACE_PATH"
SFTP_ROOT=$(dirname "$WORKSPACE_PATH")
mkdir -p "$WORKSPACE_PATH"
chown root:root "$SFTP_ROOT" 2>/dev/null || true
chmod 755 "$SFTP_ROOT"
id sftp-vibe &>/dev/null || useradd -r -s /usr/sbin/nologin sftp-vibe
chown sftp-vibe:sftp-vibe "$WORKSPACE_PATH"
chmod 775 "$WORKSPACE_PATH"
setfacl -R  -m u:sftp-vibe:rwx "$WORKSPACE_PATH"
setfacl -Rd -m u:sftp-vibe:rwx "$WORKSPACE_PATH"
setfacl -Rd -m u:root:rwx "$WORKSPACE_PATH"

if [[ ! -d "$WORKSPACE_PATH/.git" ]]; then
    git config --global --add safe.directory "$WORKSPACE_PATH"
    git -C "$WORKSPACE_PATH" init
    git -C "$WORKSPACE_PATH" config user.email "watchdog@localhost"
    git -C "$WORKSPACE_PATH" config user.name  "Vibe Watchdog"
    cat > "$WORKSPACE_PATH/CONTEXT.md" << 'CTX'
# Project Context

## Current Project
Name:
Repo:
Description:

## Current Task


## Decisions Made


## Where We Left Off


## Known Issues


## Environment Notes

CTX
    git -C "$WORKSPACE_PATH" add .
    git -C "$WORKSPACE_PATH" commit -m "Initial commit — workspace initialized"
    success "Workspace git initialized with CONTEXT.md"
fi

mkdir -p /home/sftp-vibe/.ssh
touch /home/sftp-vibe/.ssh/authorized_keys
chown -R sftp-vibe:sftp-vibe /home/sftp-vibe/.ssh
chmod 700 /home/sftp-vibe/.ssh; chmod 600 /home/sftp-vibe/.ssh/authorized_keys
warn "Add phone SSH key: echo 'ssh-ed25519 AAAA...' >> /home/sftp-vibe/.ssh/authorized_keys"
success "Workspace configured"

# ══════════════════════════════════════════════════════════════
# 9. SSH
# ══════════════════════════════════════════════════════════════
step "Configuring SSH"
sed -e "s|100\.x\.x\.x|${TAILSCALE_IP}|g" \
    -e "s|/srv/sftp/workspace|${SFTP_ROOT}|g" \
    sshd_config_additions > /tmp/sshd_resolved
if ! grep -q "# ── BEGIN vibe-stack" /etc/ssh/sshd_config; then
    if ! grep -q "^Port 2222" /etc/ssh/sshd_config; then
        sed -i '1s/^/Port 22\nPort 2222\n/' /etc/ssh/sshd_config
    fi
    printf '\n# ── BEGIN vibe-stack ──\n' >> /etc/ssh/sshd_config
    cat /tmp/sshd_resolved >> /etc/ssh/sshd_config
    printf '# ── END vibe-stack ──\n' >> /etc/ssh/sshd_config
fi

mkdir -p /etc/systemd/system/ssh.socket.d
cat > /etc/systemd/system/ssh.socket.d/override.conf << 'EOF'
[Socket]
ListenStream=
ListenStream=0.0.0.0:22
ListenStream=[::]:22
ListenStream=0.0.0.0:2222
ListenStream=[::]:2222
EOF
systemctl daemon-reload

mkdir -p /run/sshd
sshd -t || error "sshd config invalid"
systemctl restart ssh.socket
systemctl restart ssh
success "SSH configured (port 22 + 2222)"

# ══════════════════════════════════════════════════════════════
# 10. Tailscale SSH
# ══════════════════════════════════════════════════════════════
step "Enabling Tailscale SSH"
tailscale set --ssh=true
success "Tailscale SSH enabled (port 22 — interactive sessions)"

# ══════════════════════════════════════════════════════════════
# 11. Caddy
# ══════════════════════════════════════════════════════════════
step "Configuring Caddy"
useradd -r -s /usr/sbin/nologin -d /var/lib/caddy caddy 2>/dev/null || true
mkdir -p /etc/caddy /var/log/caddy /var/lib/caddy
chown caddy:caddy /var/log/caddy /var/lib/caddy
cp Caddyfile /etc/caddy/Caddyfile

# Generate staging port blocks (8100-8119)
if ! grep -q "TAILSCALE_HOSTNAME.*:8100" /etc/caddy/Caddyfile; then
    info "Generating Caddy staging port blocks..."
    STAGING_BLOCK=""
    for port in $(seq 8100 8119); do
        read -r -d '' BLOCK <<CADDYEOF || true
https://{\$TAILSCALE_HOSTNAME}:${port} {
    import tailscale_tls
    import security_headers
    import staging_csp
    rate_limit {
        zone staging_${port} {
            key    {remote_host}
            events 200
            window 1m
        }
    }
    reverse_proxy 127.0.0.1:${port} {
        transport http {
            response_header_timeout 60s
        }
    }
}
CADDYEOF
        STAGING_BLOCK+="$BLOCK"$'\n\n'
    done
    awk -v block="$STAGING_BLOCK" '{print} /# STAGING_PORTS_START/{printf "%s", block}' \
        /etc/caddy/Caddyfile > /tmp/Caddyfile.tmp \
        && mv /tmp/Caddyfile.tmp /etc/caddy/Caddyfile
else
    info "Caddy staging port blocks already present — skipping"
fi

if [[ ! -f /etc/systemd/system/caddy.service ]]; then
    cat > /etc/systemd/system/caddy.service << 'UNIT'
[Unit]
Description=Caddy Web Server
After=network-online.target
Requires=network-online.target

[Service]
Type=notify
User=caddy
Group=caddy
ExecStart=/usr/local/bin/caddy run --environ --config /etc/caddy/Caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --force
TimeoutStopSec=5s
LimitNOFILE=1048576
PrivateTmp=true
ProtectSystem=full
AmbientCapabilities=CAP_NET_BIND_SERVICE
EnvironmentFile=-/etc/caddy/caddy.env
[Install]
WantedBy=multi-user.target
UNIT
fi

cat > /etc/caddy/caddy.env << EOF
TAILSCALE_HOSTNAME=${TAILSCALE_HOSTNAME}
TAILSCALE_IP=${TAILSCALE_IP}
EOF
chmod 640 /etc/caddy/caddy.env; chown root:caddy /etc/caddy/caddy.env
systemctl daemon-reload
caddy validate --config /etc/caddy/Caddyfile || error "Caddyfile invalid"
systemctl enable --now caddy
systemctl reload caddy 2>/dev/null || systemctl restart caddy
success "Caddy running (self-signed TLS)"

# ══════════════════════════════════════════════════════════════
# 12a. Docker credential helper + GHCR authentication
# ══════════════════════════════════════════════════════════════
step "Docker credential storage + GHCR auth"

# Install credential helper if not present
if ! command -v docker-credential-secretservice &>/dev/null && \
   ! command -v docker-credential-pass &>/dev/null; then
    case "$DISTRO_FAMILY" in
        debian) apt-get install -y --no-install-recommends golang-docker-credential-helpers >/dev/null 2>&1 || true ;;
        fedora) dnf install -y docker-credential-helpers >/dev/null 2>&1 || true ;;
        *)      info "Docker credential helpers not available in repos — skipping" ;;
    esac
fi

# Determine which credential helper to use
# Both secretservice and pass require D-Bus — skip on headless/sudo.
CRED_HELPER=""
if [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
    if command -v docker-credential-secretservice &>/dev/null; then
        CRED_HELPER="secretservice"
    elif command -v docker-credential-pass &>/dev/null; then
        CRED_HELPER="pass"
    fi
fi

# Configure Docker for the deploying user (not root)
DEPLOY_USER="${SUDO_USER:-$USER}"
DEPLOY_HOME=$(eval echo "~$DEPLOY_USER")
DOCKER_CONFIG_DIR="$DEPLOY_HOME/.docker"
DOCKER_CONFIG="$DOCKER_CONFIG_DIR/config.json"

mkdir -p "$DOCKER_CONFIG_DIR"

if [[ -n "$CRED_HELPER" ]]; then
    # Add credsStore if not already configured
    if [[ -f "$DOCKER_CONFIG" ]]; then
        if ! grep -q '"credsStore"' "$DOCKER_CONFIG"; then
            # Merge credsStore into existing config
            python3 -c "
import json, sys
with open('$DOCKER_CONFIG') as f: cfg = json.load(f)
cfg['credsStore'] = '$CRED_HELPER'
cfg.get('auths', {}).pop('ghcr.io', None)  # remove plaintext cred
with open('$DOCKER_CONFIG', 'w') as f: json.dump(cfg, f, indent=2)
" 2>/dev/null || true
        fi
    else
        echo '{"auths":{},"credsStore":"'"$CRED_HELPER"'"}' > "$DOCKER_CONFIG"
    fi
    chown -R "$DEPLOY_USER:$DEPLOY_USER" "$DOCKER_CONFIG_DIR"
    success "Docker credential helper: $CRED_HELPER"
else
    info "No D-Bus session — skipping credential helper (plaintext auth is fine for servers)"
fi

# Credential helpers (secretservice/pass) need D-Bus which isn't available
# under sudo. Docker auto-discovers them on $PATH even without credsStore
# in config, so we temporarily hide them during setup pulls.
SETUP_DOCKER_CONFIG=$(mktemp -d)
echo '{"auths":{}}' > "$SETUP_DOCKER_CONFIG/config.json"
export DOCKER_CONFIG="$SETUP_DOCKER_CONFIG"
for helper in docker-credential-secretservice docker-credential-pass docker-credential-desktop; do
    if command -v "$helper" &>/dev/null; then
        helper_path=$(command -v "$helper")
        mv "$helper_path" "${helper_path}.setup-disabled" 2>/dev/null || true
    fi
done

# Authenticate with GHCR if not already logged in
if ! timeout 15 docker pull "ghcr.io/${GHCR_ORG}/paperclip-server:latest" >/dev/null 2>&1; then
    info "GHCR authentication required for private images"
    if [[ -f "secrets/github_token.txt" ]] && [[ -s "secrets/github_token.txt" ]]; then
        cat secrets/github_token.txt | su "$DEPLOY_USER" -c "docker login ghcr.io -u ${GIT_USER} --password-stdin" 2>&1 \
            | grep -v "WARNING" || true
        success "Authenticated with GHCR via github_token.txt"
    else
        warn "Cannot authenticate with GHCR — secrets/github_token.txt missing"
        warn "Run: echo 'ghp_...' > secrets/github_token.txt"
        warn "Or:  docker login ghcr.io -u ${GIT_USER}"
    fi
else
    success "GHCR authentication already configured"
fi

# ══════════════════════════════════════════════════════════════
# 12b. Pull and build Docker images
# ══════════════════════════════════════════════════════════════
step "Pulling and building Docker images"
# Pull only services that exist in the compose config
AVAILABLE_SERVICES=$(docker compose config --services 2>/dev/null)
for svc in searxng gitea minio penpot-frontend penpot-backend penpot-postgres penpot-redis penpot-exporter playwright; do
    if echo "$AVAILABLE_SERVICES" | grep -qx "$svc"; then
        # Check if image already exists locally; skip pull if so
        IMAGE_NAME=$(docker compose config --format json 2>/dev/null | python3 -c "import sys,json; cfg=json.load(sys.stdin); print(cfg.get('services',{}).get('$svc',{}).get('image',''))" 2>/dev/null || true)
        if [[ -n "$IMAGE_NAME" ]] && docker image inspect "$IMAGE_NAME" &>/dev/null 2>&1; then
            success "  $svc — image already present"
        else
            info "  Pulling $svc..."
            docker compose pull "$svc" || warn "  $svc — pull failed (network issue?), will retry on next run"
        fi
    fi
done
success "Public images pulled"

CUSTOM_SERVICES="ssh-relay dev-runner server deerflow-langgraph deerflow-gateway"
BUILD_NEEDED=""

info "Pulling pre-built images from GHCR..."
for svc in $CUSTOM_SERVICES; do
    info "  Pulling $svc..."
    if ! docker compose pull "$svc" 2>/dev/null; then
        warn "  $svc — pre-built image not available, will build locally"
        BUILD_NEEDED="$BUILD_NEEDED $svc"
    fi
done

if [[ -n "$BUILD_NEEDED" ]]; then
    if [[ -f "docker-compose.override.yml" ]]; then
        info "Building missing images locally (using override):$BUILD_NEEDED"
        for svc in $BUILD_NEEDED; do
            info "  Building $svc..."
            docker compose build "$svc"
        done
    else
        warn "The following images could not be pulled from GHCR:$BUILD_NEEDED"
        warn "To build locally: cp docker-compose.override.yml.example docker-compose.override.yml"
        warn "Then set PAPERCLIP_SOURCE_DIR in .env and run: docker compose build$BUILD_NEEDED"
    fi
fi
success "All images ready"

# ══════════════════════════════════════════════════════════════
# 13. Start stack (staged for reliable startup)
# ══════════════════════════════════════════════════════════════
# COMPOSE_FILE was set in step 5b and exported; docker compose
# reads it automatically so no -f flags are needed.

step "Starting stack"
# Bootstrap embedded PostgreSQL — the server image uses @embedded-postgres which
# needs initdb + role/database creation before the app can start. On a fresh
# volume this must be done manually; subsequent runs skip if PG_VERSION exists.
PG_DATA="/paperclip/instances/default/db"
if ! docker compose run --rm --no-deps --entrypoint "test -f ${PG_DATA}/PG_VERSION" server 2>/dev/null; then
    info "Initializing embedded PostgreSQL..."
    docker compose run --rm --no-deps --entrypoint "sh -c '\
      PG_BIN=\$(find /app/node_modules -path \"*/@embedded-postgres/linux-x64/native/bin\" -type d | head -1) && \
      \$PG_BIN/initdb -D ${PG_DATA} && \
      echo \"CREATE ROLE paperclip WITH LOGIN SUPERUSER;\" | \$PG_BIN/postgres --single -D ${PG_DATA} postgres && \
      echo \"CREATE DATABASE paperclip OWNER paperclip;\" | \$PG_BIN/postgres --single -D ${PG_DATA} postgres'" server
    success "Embedded PostgreSQL initialized (role=paperclip, db=paperclip)"
else
    info "Embedded PostgreSQL already initialized — skipping"
fi

info "Starting Paperclip server..."
docker compose up -d server
info "Waiting for Paperclip to become healthy..."
wait_healthy 120 server

# Only start DeerFlow if images are available
if docker image inspect "$(docker compose config --images 2>/dev/null | grep deerflow-langgraph | head -1)" &>/dev/null 2>&1; then
    info "Starting stack — DeerFlow services..."
    docker compose up -d deerflow-langgraph deerflow-gateway
    info "Waiting for DeerFlow to become healthy..."
    wait_healthy 120 deerflow-langgraph deerflow-gateway
else
    warn "DeerFlow images not available — skipping. Build them with docker compose build deerflow-langgraph deerflow-gateway"
fi

if [[ "$COMPOSE_FILE" == *"infra"* ]]; then
    info "Building PaddleOCR image (first run downloads ~2GB of models)..."
    docker compose build paddleocr

    info "Starting stack — infrastructure services..."
    docker compose up -d searxng playwright gitea minio minio-init \
        penpot-frontend penpot-backend penpot-exporter penpot-postgres penpot-redis \
        zep-db neo4j graphiti zep mirofish paddleocr \
        ssh-relay dev-runner
    info "Waiting for infrastructure services with healthchecks..."
    wait_healthy 60 searxng playwright gitea minio
    info "Waiting for MiroFish + Zep stack (may take longer on first run)..."
    wait_healthy 120 zep-db neo4j graphiti zep mirofish
    info "Waiting for PaddleOCR..."
    wait_healthy 60 paddleocr
fi

if [[ "$COMPOSE_FILE" == *"gpu"* ]]; then
    info "Starting stack — GPU services (opensandbox + comfyui)..."
    docker compose up -d opensandbox
    info "Waiting for GPU services to become healthy..."
    wait_healthy 300 opensandbox
fi

info "Starting stack — Tailscale..."
docker compose up -d tailscale

info "Starting stack — Vibe agent..."
docker compose up -d vibe
success "Stack started"

# ══════════════════════════════════════════════════════════════
# 14. iptables
# ══════════════════════════════════════════════════════════════
step "iptables rules"
chmod +x iptables-setup.sh
./iptables-setup.sh
success "iptables rules applied"

cat > /etc/systemd/system/vibe-iptables.service << EOF
[Unit]
Description=Vibe Stack iptables rules (re-applied after Docker)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStartPre=/bin/sleep 5
ExecStart=/bin/bash "${SCRIPT_DIR}/iptables-setup.sh"
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable vibe-iptables
success "iptables auto-refresh service installed"

# ══════════════════════════════════════════════════════════════
# 15. Watchdog service
# ══════════════════════════════════════════════════════════════
step "Workspace watchdog service"
cp workspace-watchdog.sh /usr/local/bin/workspace-watchdog.sh
chmod +x /usr/local/bin/workspace-watchdog.sh
cat > /etc/systemd/system/workspace-watchdog.service << EOF
[Unit]
Description=Vibe Workspace Git Watchdog
After=network.target docker.service

[Service]
Type=simple
ExecStart=/usr/local/bin/workspace-watchdog.sh
Restart=always
RestartSec=5
Environment=WATCH_DIR=${WORKSPACE_PATH}
Environment=GIT_USER=${GIT_USER}
Environment=DEPLOY_KEYS_DIR=${SCRIPT_DIR}/secrets/ssh
StandardOutput=journal
StandardError=journal
User=root

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now workspace-watchdog
success "Watchdog installed"

# ══════════════════════════════════════════════════════════════
# 16. auditd
# ══════════════════════════════════════════════════════════════
step "auditd rules"
sed "s|/srv/sftp/workspace/files|${WORKSPACE_PATH}|g" \
    auditd-vibe-stack.rules > /etc/audit/rules.d/vibe-stack.rules
augenrules --load >/dev/null 2>&1 || true
success "auditd configured"

# ══════════════════════════════════════════════════════════════
# 17. fail2ban
# ══════════════════════════════════════════════════════════════
step "fail2ban"
cp fail2ban/vibe-stack.conf    /etc/fail2ban/jail.d/vibe-stack.conf
cp fail2ban/caddy-paperclip.conf /etc/fail2ban/filter.d/caddy-paperclip.conf
systemctl enable --now fail2ban && systemctl restart fail2ban
success "fail2ban configured"

# ══════════════════════════════════════════════════════════════
# 18. Unattended upgrades
# ══════════════════════════════════════════════════════════════
step "Unattended security upgrades"
case "$DISTRO_FAMILY" in
    debian)
        cat > /etc/apt/apt.conf.d/20auto-upgrades-vibe << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
        ;;
    fedora)
        systemctl enable --now dnf-automatic-install.timer 2>/dev/null || \
        systemctl enable --now dnf-automatic.timer 2>/dev/null || \
        warn "dnf-automatic not available — configure automatic updates manually"
        ;;
    arch)
        info "Arch does not support unattended upgrades — skip (rolling release)"
        ;;
    suse)
        systemctl enable --now packagekit-background.service 2>/dev/null || \
        warn "Auto-updates not configured — enable manually"
        ;;
esac
success "Unattended upgrades configured"

# ══════════════════════════════════════════════════════════════
# 19. Claude Code login check
# ══════════════════════════════════════════════════════════════
step "Claude Code login check"
CREDS_PATH="/paperclip/.claude/.credentials.json"
if docker compose exec -T server test -f "$CREDS_PATH" 2>/dev/null; then
    success "Claude Code credentials found in container"
else
    printf "\n${YELLOW}Claude Code is not authenticated inside the Paperclip container.${NC}\n"
    printf "  Agents need this to run tasks. Log in now?\n"
    printf "  Press Enter to log in, or Ctrl+C to skip (you can do it later): "
    read -r
    docker compose exec -it server claude login
    if docker compose exec -T server test -f "$CREDS_PATH" 2>/dev/null; then
        success "Claude Code authenticated"
    else
        warn "Claude Code login skipped — run later: docker compose exec -it server claude login"
    fi
fi

# ══════════════════════════════════════════════════════════════
# 24. Bootstrap org (create agents)
# ══════════════════════════════════════════════════════════════
step "Bootstrapping agent org"
AGENT_COUNT=$(docker compose exec -T server sh -c '
  node --input-type=module -e "
    import pg from \"/app/node_modules/.pnpm/postgres@3.4.8/node_modules/postgres/src/index.js\";
    const sql = pg({host:\"/tmp\",port:54329,database:\"paperclip\",username:\"paperclip\"});
    const r = await sql\`SELECT count(*) as c FROM agents\`;
    console.log(r[0].c);
    await sql.end();
  "' 2>/dev/null || echo "0")

if [[ "$AGENT_COUNT" -ge 10 ]]; then
    success "Org already bootstrapped ($AGENT_COUNT agents) — skipping"
else
    info "Creating 10-agent engineering org..."
    docker compose cp bootstrap-org.cjs server:/app/bootstrap-org.cjs
    docker compose exec -T server node /app/bootstrap-org.cjs
    # Copy agent IDs from container .env back to host .env
    docker compose exec -T server grep "^PAPERCLIP_AGENT_ID" /app/.env 2>/dev/null | while read -r line; do
        key="${line%%=*}"
        val="${line#*=}"
        _update_env_var "$key" "$val"
    done
    success "Org bootstrap complete — agent IDs written to .env"
fi

# Restore credential helpers and clean up temporary Docker config
for helper in docker-credential-secretservice docker-credential-pass docker-credential-desktop; do
    disabled=$(command -v "${helper}.setup-disabled" 2>/dev/null || which "${helper}.setup-disabled" 2>/dev/null || true)
    [[ -n "$disabled" ]] && mv "$disabled" "${disabled%.setup-disabled}" 2>/dev/null || true
done
[[ -n "${SETUP_DOCKER_CONFIG:-}" ]] && rm -rf "$SETUP_DOCKER_CONFIG"
unset DOCKER_CONFIG

# ══════════════════════════════════════════════════════════════
# DONE
# ══════════════════════════════════════════════════════════════
printf "\n${GREEN}══════════════════════════════════════════════════════${NC}\n"
printf "${GREEN}  Vibe Stack 2.0 deployment complete!${NC}\n"
printf "${GREEN}══════════════════════════════════════════════════════${NC}\n\n"
printf "  Paperclip:   ${BLUE}https://${TAILSCALE_HOSTNAME}${NC}\n"
if [[ "${OLLAMA_SKIP:-true}" == "false" ]]; then
    printf "  Ollama model: ${BLUE}${OLLAMA_MODEL}${NC}\n"
    printf "  Ollama API:   ${BLUE}http://localhost:11434${NC}\n"
fi
printf "\n"

printf "${YELLOW}Next steps:${NC}\n"
printf "  1. Open ${BLUE}https://${TAILSCALE_HOSTNAME}${NC} and run the onboard wizard:\n"
printf "     docker compose exec -it server pnpm paperclipai onboard\n\n"
printf "  2. Add phone SSH public key (SFTP on port 2222):\n"
printf "     echo 'ssh-ed25519 AAAA...' >> /home/sftp-vibe/.ssh/authorized_keys\n"
printf "     Connect with Termius: host=${TAILSCALE_IP} port=2222 user=sftp-vibe\n\n"
printf "  3. Add SSH deploy keys (per-repo):\n"
printf "     ssh-keygen -t ed25519 -f %s/secrets/ssh/your-repo -N ''\n" "$SCRIPT_DIR"
printf "     Add the .pub key as a deploy key on the GitHub repo\n"
printf "     (Settings -> Deploy Keys -> Add, check 'Allow write access')\n\n"
printf "  4. Add GitHub token for agent git operations:\n"
printf "     echo 'ghp_...' > secrets/github_token.txt && chmod 444 secrets/github_token.txt\n\n"
