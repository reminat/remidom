#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Optional pre-apply step: rebuild/update the Proxmox template before cloning.
# Disable with: TFWRAP_SKIP_TEMPLATE_BUILD=1 ./tf.sh apply
if [ "${1:-}" = "apply" ] && [ "${TFWRAP_SKIP_TEMPLATE_BUILD:-0}" != "1" ]; then
  PROXMOX_HOST="${PROXMOX_HOST:-pve.home.arpa}"
  PROXMOX_USER="${PROXMOX_USER:-root}"
  TEMPLATE_SCRIPT="${TEMPLATE_SCRIPT:-template-docker.sh}"

  echo "[tf.sh] Running remote template build: ${PROXMOX_USER}@${PROXMOX_HOST}:~/${TEMPLATE_SCRIPT}"
  ssh -i "${HOME}/.ssh/id_ed25519_pve" "${PROXMOX_USER}@${PROXMOX_HOST}" "bash ~/${TEMPLATE_SCRIPT}"
fi

exec "${SCRIPT_DIR}/../../../../tf.sh" "$@"
