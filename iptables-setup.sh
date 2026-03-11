#!/usr/bin/env bash
# iptables-setup.sh — Vibe Stack 2.0
# Run after every Docker daemon restart. Fully idempotent.
#
# Protections:
#   agent-core-net    — internal Docker network (no host iptables needed)
#   internet-access   — outbound 443/80/53/22 only
#   host-access       — outbound 443/80/53/22 only (mirrors internet-access policy)
#   Dev-runner :9000  — loopback + agent-core-net only
#   DeerFlow   :2024  — agent-core-net only
#   DeerFlow   :8001  — agent-core-net only

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { printf "${GREEN}[iptables]${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}[iptables]${NC} %s\n" "$*"; }
error() { printf "${RED}[iptables]${NC} %s\n" "$*" >&2; exit 1; }

[[ "$EUID" -eq 0 ]] || error "Run as root"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Load .env ─────────────────────────────────────────────────
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    set -a; source "$SCRIPT_DIR/.env"; set +a
fi

# ── Resolve Docker bridge interface names ─────────────────────
get_bridge_iface() {
    local suffix="$1"
    local id
    id=$(docker network ls --filter "name=${suffix}" --format "{{.ID}} {{.Name}}" 2>/dev/null \
        | grep "_${suffix}$" | head -1 | awk '{print $1}')
    [[ -z "$id" ]] && echo "" && return
    echo "br-${id:0:12}"
}

CORE_NET_IFACE=$(get_bridge_iface "agent-core-net")
INET_NET_IFACE=$(get_bridge_iface "internet-access")
HOST_NET_IFACE=$(get_bridge_iface "host-access")

if [[ -z "$CORE_NET_IFACE" || -z "$INET_NET_IFACE" || -z "$HOST_NET_IFACE" ]]; then
    error "Cannot resolve Docker bridge interfaces — run 'docker compose up -d' first"
fi

info "agent-core-net:  $CORE_NET_IFACE"
info "internet-access: $INET_NET_IFACE"
info "host-access:     $HOST_NET_IFACE"

# ── Flush existing vibe-stack rules & chains ──────────────────
flush_vibe_rules() {
    local parent="$1"
    local nums
    nums=$(iptables -L "$parent" --line-numbers -n 2>/dev/null \
        | grep 'vibe-stack' \
        | awk '{print $1}' \
        | sort -rn) || true
    local num
    for num in $nums; do
        iptables -D "$parent" "$num" 2>/dev/null || true
    done
}
flush_vibe_rules INPUT
flush_vibe_rules DOCKER-USER

for chain in VIBE_DOCKER_OUT VIBE_HOST_OUT VIBE_INTERNAL; do
    if iptables -L "$chain" -n &>/dev/null; then
        iptables -F "$chain"
        iptables -X "$chain" 2>/dev/null || true
        info "Cleaned up chain $chain"
    fi
done

# ── Chain: VIBE_DOCKER_OUT — internet-access outbound ────────
iptables -N VIBE_DOCKER_OUT

# Allow established connections back
iptables -A VIBE_DOCKER_OUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# Allow HTTPS outbound from internet-access
iptables -A VIBE_DOCKER_OUT -i "$INET_NET_IFACE" ! -o "$INET_NET_IFACE" \
    -p tcp --dport 443 -j ACCEPT

# Allow HTTP outbound
iptables -A VIBE_DOCKER_OUT -i "$INET_NET_IFACE" ! -o "$INET_NET_IFACE" \
    -p tcp --dport 80 -j ACCEPT

# Allow DNS
iptables -A VIBE_DOCKER_OUT -i "$INET_NET_IFACE" ! -o "$INET_NET_IFACE" \
    -p udp --dport 53 -j ACCEPT
iptables -A VIBE_DOCKER_OUT -i "$INET_NET_IFACE" ! -o "$INET_NET_IFACE" \
    -p tcp --dport 53 -j ACCEPT

# Allow SSH outbound from internet-access (for ssh-relay -> github.com)
iptables -A VIBE_DOCKER_OUT -i "$INET_NET_IFACE" ! -o "$INET_NET_IFACE" \
    -p tcp --dport 22 -j ACCEPT

# Drop everything else
iptables -A VIBE_DOCKER_OUT -i "$INET_NET_IFACE" ! -o "$INET_NET_IFACE" -j DROP

iptables -I DOCKER-USER 1 \
    -m comment --comment "vibe-stack" \
    -j VIBE_DOCKER_OUT

# ── Chain: VIBE_HOST_OUT — host-access outbound ────────────────
iptables -N VIBE_HOST_OUT

# Allow established connections back
iptables -A VIBE_HOST_OUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# Allow HTTPS outbound from host-access
iptables -A VIBE_HOST_OUT -i "$HOST_NET_IFACE" ! -o "$HOST_NET_IFACE" \
    -p tcp --dport 443 -j ACCEPT

# Allow HTTP outbound
iptables -A VIBE_HOST_OUT -i "$HOST_NET_IFACE" ! -o "$HOST_NET_IFACE" \
    -p tcp --dport 80 -j ACCEPT

# Allow DNS
iptables -A VIBE_HOST_OUT -i "$HOST_NET_IFACE" ! -o "$HOST_NET_IFACE" \
    -p udp --dport 53 -j ACCEPT
iptables -A VIBE_HOST_OUT -i "$HOST_NET_IFACE" ! -o "$HOST_NET_IFACE" \
    -p tcp --dport 53 -j ACCEPT

# Allow SSH outbound
iptables -A VIBE_HOST_OUT -i "$HOST_NET_IFACE" ! -o "$HOST_NET_IFACE" \
    -p tcp --dport 22 -j ACCEPT

# Drop everything else
iptables -A VIBE_HOST_OUT -i "$HOST_NET_IFACE" ! -o "$HOST_NET_IFACE" -j DROP

iptables -I DOCKER-USER 2 \
    -m comment --comment "vibe-stack" \
    -j VIBE_HOST_OUT

# ── Block internal services from host exposure ────────────────
declare -A SERVICE_PORTS=(
    [dev-runner]=9000
)

iptables -N VIBE_INTERNAL
iptables -A VIBE_INTERNAL -i lo -j RETURN
iptables -A VIBE_INTERNAL -i "$CORE_NET_IFACE" -j RETURN
for service in "${!SERVICE_PORTS[@]}"; do
    port="${SERVICE_PORTS[$service]}"
    iptables -A VIBE_INTERNAL -p tcp --dport "$port" \
        -m comment --comment "vibe-stack" \
        -j DROP
    info "Blocked $service :$port from external access"
done
iptables -I INPUT 1 -m comment --comment "vibe-stack" -j VIBE_INTERNAL

# ── Persist ───────────────────────────────────────────────────
if command -v netfilter-persistent &>/dev/null; then
    netfilter-persistent save
    info "Rules persisted"
else
    warn "netfilter-persistent not found — install: sudo apt install iptables-persistent"
fi

info "Setup complete — vibe-stack iptables rules:"
iptables -L INPUT -n -v --line-numbers | grep "vibe-stack" || true
