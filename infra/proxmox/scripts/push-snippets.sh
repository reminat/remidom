#!/usr/bin/env bash
set -euo pipefail

PROXMOX_HOST="${PROXMOX_HOST:-pve.reminat.com}"
PROXMOX_USER="${PROXMOX_USER:-root}"
SNIPPETS_DIR="/var/lib/vz/snippets"

echo "Pushing cloud-init snippets to Proxmox..."
echo "Target: ${PROXMOX_USER}@${PROXMOX_HOST}:${SNIPPETS_DIR}"

ssh "${PROXMOX_USER}@${PROXMOX_HOST}" "mkdir -p ${SNIPPETS_DIR}"

scp infra/proxmox/snippets/*.yaml \
  "${PROXMOX_USER}@${PROXMOX_HOST}:${SNIPPETS_DIR}/"

echo "Done."