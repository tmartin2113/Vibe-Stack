#!/usr/bin/env bash
# secrets-init.sh — One-time setup for SOPS + age secret encryption.
#
# Installs age + sops (if missing), generates an age keypair,
# and configures .sops.yaml with the public key.
#
# After running this:
#   sops -e -i .env          # encrypt .env in place
#   sops -d .env              # decrypt to stdout
#   sops .env                 # edit encrypted file interactively

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
AGE_KEY_DIR="${HOME}/.config/sops/age"
AGE_KEY_FILE="${AGE_KEY_DIR}/keys.txt"

log() { echo "[secrets] $*"; }

# ── Install age ──────────────────────────────────────────────────
if ! command -v age &>/dev/null; then
  log "Installing age..."
  sudo apt-get update -qq && sudo apt-get install -y -qq age
fi
log "age: $(age --version 2>&1 || echo 'installed')"

# ── Install sops ─────────────────────────────────────────────────
if ! command -v sops &>/dev/null; then
  log "Installing sops..."
  SOPS_VERSION="3.9.4"
  SOPS_URL="https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64"
  sudo curl -fsSL "$SOPS_URL" -o /usr/local/bin/sops
  sudo chmod +x /usr/local/bin/sops
fi
log "sops: $(sops --version 2>&1 | head -1)"

# ── Generate age keypair ─────────────────────────────────────────
if [[ -f "$AGE_KEY_FILE" ]]; then
  log "Age key already exists at $AGE_KEY_FILE"
else
  log "Generating age keypair..."
  mkdir -p "$AGE_KEY_DIR"
  age-keygen -o "$AGE_KEY_FILE" 2>&1
  chmod 600 "$AGE_KEY_FILE"
  log "Key saved to $AGE_KEY_FILE (back this up securely!)"
fi

# Extract public key
AGE_PUBLIC_KEY=$(grep -o 'age1[a-z0-9]*' "$AGE_KEY_FILE" | head -1)
log "Public key: $AGE_PUBLIC_KEY"

# ── Update .sops.yaml ───────────────────────────────────────────
SOPS_YAML="${REPO_DIR}/.sops.yaml"
if [[ -f "$SOPS_YAML" ]]; then
  sed -i "s|age: \".*\"|age: \"$AGE_PUBLIC_KEY\"|g" "$SOPS_YAML"
  log "Updated .sops.yaml with public key"
else
  log "ERROR: .sops.yaml not found at $SOPS_YAML"
  exit 1
fi

# ── Summary ──────────────────────────────────────────────────────
log ""
log "Setup complete. Next steps:"
log "  1. Back up your age key: $AGE_KEY_FILE"
log "  2. Encrypt your .env:    cd $REPO_DIR && sops -e -i .env"
log "  3. To decrypt:           sops -d .env"
log "  4. To edit:              sops .env"
log ""
log "  For backup encryption, set in .env:"
log "    BACKUP_AGE_RECIPIENT=$AGE_PUBLIC_KEY"
