#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VIBE_STACK_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SYSTEMD_DIR="/etc/systemd/system"

# Detect user who owns the repo (don't run health checks as root)
VIBE_USER="${SUDO_USER:-$(stat -c '%U' "$VIBE_STACK_DIR")}"

echo "Installing vibe-health systemd units..."
echo "  Vibe Stack dir: $VIBE_STACK_DIR"
echo "  Run-as user:    $VIBE_USER"

# Generate service file with correct paths
sed -e "s|__VIBE_STACK_DIR__|${VIBE_STACK_DIR}|g" \
    -e "s|__VIBE_USER__|${VIBE_USER}|g" \
    "$SCRIPT_DIR/vibe-health.service" > "$SYSTEMD_DIR/vibe-health.service"

cp "$SCRIPT_DIR/vibe-health.timer" "$SYSTEMD_DIR/vibe-health.timer"

systemctl daemon-reload
systemctl enable vibe-health.timer
systemctl start vibe-health.timer

echo "Done. vibe-health.timer is enabled and running."
echo "Check status: systemctl status vibe-health.timer"
