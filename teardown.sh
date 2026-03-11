#!/usr/bin/env bash
# teardown.sh — Vibe Stack 2.0
# Reverses setup.sh for a clean re-deployment. Idempotent — safe to re-run.
# Does NOT uninstall apt packages (Docker, NVIDIA toolkit, etc.) — only
# removes resources and configs that setup.sh created.

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { printf "${BLUE}[INFO]${NC}   %s\n" "$*"; }
success() { printf "${GREEN}[OK]${NC}     %s\n" "$*"; }
warn()    { printf "${YELLOW}[WARN]${NC}   %s\n" "$*"; }
error()   { printf "${RED}[ERROR]${NC}  %s\n" "$*" >&2; exit 1; }

[[ "$EUID" -eq 0 ]] || error "Run as root: sudo ./teardown.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Load .env for variable references ─────────────────────────
if [[ -f ".env" ]]; then
    set -a; source .env; set +a
fi
WORKSPACE_PATH="${WORKSPACE_PATH:-/srv/sftp/workspace/files}"
SFTP_ROOT=$(dirname "$WORKSPACE_PATH")
TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME:-}"

printf "\n${YELLOW}════════════════════════════════════════════════════════${NC}\n"
printf "${YELLOW}  Vibe Stack 2.0 Teardown${NC}\n"
printf "${YELLOW}════════════════════════════════════════════════════════${NC}\n\n"
printf "${RED}This will destroy all containers, volumes, configs, and data.${NC}\n"
printf "Press Enter to continue or Ctrl+C to abort...\n"
read -r

# ══════════════════════════════════════════════════════════════
# 1. Docker — containers, volumes, networks, images
# ══════════════════════════════════════════════════════════════
info "Stopping and removing Docker stack..."
if docker compose version &>/dev/null; then
    docker compose down -v --rmi local --remove-orphans 2>/dev/null || true
    success "Docker stack removed"
else
    warn "docker compose not found — skipping"
fi

# ══════════════════════════════════════════════════════════════
# 2. Systemd services
# ══════════════════════════════════════════════════════════════
info "Removing systemd services..."
for svc in vibe-iptables workspace-watchdog caddy; do
    if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
        systemctl disable --now "$svc" 2>/dev/null || true
        info "  Disabled $svc"
    fi
done
rm -f /etc/systemd/system/vibe-iptables.service
rm -f /etc/systemd/system/workspace-watchdog.service
rm -f /etc/systemd/system/caddy.service
systemctl daemon-reload
success "Systemd services removed"

# ══════════════════════════════════════════════════════════════
# 3. iptables rules
# ══════════════════════════════════════════════════════════════
info "Removing iptables rules..."
for parent in INPUT DOCKER-USER; do
    nums=$(iptables -L "$parent" --line-numbers -n 2>/dev/null \
        | grep 'vibe-stack' \
        | awk '{print $1}' \
        | sort -rn) || true
    for num in $nums; do
        iptables -D "$parent" "$num" 2>/dev/null || true
    done
done
for chain in VIBE_DOCKER_OUT VIBE_INTERNAL; do
    if iptables -L "$chain" -n &>/dev/null; then
        iptables -F "$chain"
        iptables -X "$chain" 2>/dev/null || true
    fi
done
if command -v netfilter-persistent &>/dev/null; then
    netfilter-persistent save 2>/dev/null || true
fi
success "iptables rules removed"

# ══════════════════════════════════════════════════════════════
# 4. sshd config
# ══════════════════════════════════════════════════════════════
info "Removing sshd config additions..."
SSHD_CFG="/etc/ssh/sshd_config"
sed -i '/^Port 2222$/d' "$SSHD_CFG"
sed -i '1{/^Port 22$/d}' "$SSHD_CFG"
if grep -q "# ── BEGIN vibe-stack" "$SSHD_CFG" 2>/dev/null; then
    sed -i '/# ── BEGIN vibe-stack/,/# ── END vibe-stack/d' "$SSHD_CFG"
    success "sshd config block removed (marked)"
fi
if grep -q "# ── Global hardening" "$SSHD_CFG" 2>/dev/null; then
    sed -i '/# ── Global hardening/,$d' "$SSHD_CFG"
    success "sshd config block removed (unmarked)"
fi
rm -f /etc/systemd/system/ssh.socket.d/override.conf
rmdir /etc/systemd/system/ssh.socket.d 2>/dev/null || true
systemctl daemon-reload
if ! grep -q "sftp-vibe" "$SSHD_CFG" 2>/dev/null; then
    info "  sshd config clean"
else
    warn "sftp-vibe still present in sshd_config — check manually"
fi
if sshd -t 2>/dev/null; then
    systemctl restart ssh.socket 2>/dev/null || true
    systemctl restart ssh 2>/dev/null || true
    success "sshd restarted"
else
    error "sshd config invalid after teardown — check $SSHD_CFG manually!"
fi

# ══════════════════════════════════════════════════════════════
# 5. Config files
# ══════════════════════════════════════════════════════════════
info "Removing config files..."
rm -f /etc/fail2ban/jail.d/vibe-stack.conf
rm -f /etc/fail2ban/filter.d/caddy-paperclip.conf
rm -f /etc/audit/rules.d/vibe-stack.rules
rm -f /etc/apt/apt.conf.d/20auto-upgrades-vibe
rm -f /etc/caddy/Caddyfile /etc/caddy/caddy.env
rm -rf /etc/caddy 2>/dev/null || true
rm -f /usr/local/bin/workspace-watchdog.sh
augenrules --load >/dev/null 2>&1 || true
systemctl restart fail2ban 2>/dev/null || true
success "Config files removed"

# ══════════════════════════════════════════════════════════════
# 6. Users
# ══════════════════════════════════════════════════════════════
info "Removing system users..."
if id sftp-vibe &>/dev/null; then
    userdel -r sftp-vibe 2>/dev/null || userdel sftp-vibe 2>/dev/null || true
    success "  Removed sftp-vibe"
fi
if id caddy &>/dev/null; then
    userdel -r caddy 2>/dev/null || userdel caddy 2>/dev/null || true
    success "  Removed caddy"
fi
success "System users removed"

# ══════════════════════════════════════════════════════════════
# 7. Directories
# ══════════════════════════════════════════════════════════════
info "Removing created directories..."
rm -rf "$SFTP_ROOT" 2>/dev/null || true
rm -rf /var/log/caddy /var/lib/caddy
if [[ -n "$TAILSCALE_HOSTNAME" ]]; then
    rm -f "/var/lib/tailscale/certs/${TAILSCALE_HOSTNAME}.crt"
    rm -f "/var/lib/tailscale/certs/${TAILSCALE_HOSTNAME}.key"
fi
success "Directories cleaned"

# ══════════════════════════════════════════════════════════════
# Done
# ══════════════════════════════════════════════════════════════
printf "\n${GREEN}════════════════════════════════════════════════════════${NC}\n"
printf "${GREEN}  Teardown complete${NC}\n"
printf "${GREEN}════════════════════════════════════════════════════════${NC}\n\n"
printf "The following were NOT removed (safe to leave for re-deployment):\n"
printf "  - apt packages (docker, nvidia-toolkit, fail2ban, etc.)\n"
printf "  - Go compiler (/usr/local/go)\n"
printf "  - Caddy binary (/usr/local/bin/caddy)\n"
printf "  - Docker/NVIDIA apt repositories\n"
printf "\nRe-run ${BLUE}sudo ./setup.sh${NC} for a fresh deployment.\n\n"
