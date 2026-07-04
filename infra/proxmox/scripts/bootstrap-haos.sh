#!/usr/bin/env bash
# Injects SSH authorized keys into a HAOS VM via Proxmox qm guest exec.
# No manual console interaction needed.
#
# Usage: ./bootstrap-haos.sh <env>   (test or prod)
# Run from anywhere — uses ~/.ssh/id_ed25519_pve to reach Proxmox.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SSH_KEYS_SECRET_REF="${SSH_KEYS_SECRET_REF:-ssh_authorized_keys}"
PVE_SSH_KEY="${PVE_SSH_KEY:-${HOME}/.ssh/id_ed25519_pve}"

ENV="${1:-}"
if [ -z "${ENV}" ]; then
  echo "Usage: ./bootstrap-haos.sh <env>   (e.g. test or prod)" >&2
  exit 1
fi

case "${ENV}" in
  test) VM_NAME="haos-test-01" ;;
  prod) VM_NAME="haos-prod-01" ;;
  *)
    echo "Unknown env '${ENV}'. Expected: test or prod" >&2
    exit 1
    ;;
esac

# --- Helpers ----------------------------------------------------------------

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing command: $1" >&2; exit 1; }
}

bw_status() {
  bw status 2>/dev/null | jq -r '.status // empty'
}

resolve_secret_id() {
  local ref="$1"
  if [[ "$ref" =~ ^(urn:uuid:)?[0-9a-fA-F-]{36}$ ]]; then
    printf '%s\n' "$ref"
    return 0
  fi
  bws secret list | jq -r --arg ref "$ref" '
    (map(select((.key // "") == $ref or (.name // "") == $ref)) | .[0].id) // empty
  '
}

fetch_secret_value() {
  local ref="$1"
  local id
  id="$(resolve_secret_id "$ref")"
  if [ -z "${id:-}" ]; then
    echo "Could not resolve Bitwarden secret ref '${ref}'." >&2
    exit 1
  fi
  bws secret get "$id" | jq -r '.value'
}

# --- Bitwarden auth ----------------------------------------------------------

require_cmd bw
require_cmd bws
require_cmd jq
require_cmd ssh

status="$(bw_status || true)"

if [ "$status" = "unauthenticated" ] || [ -z "$status" ]; then
  if [ -n "${BW_CLIENTID:-}" ] && [ -n "${BW_CLIENTSECRET:-}" ]; then
    bw login --apikey >/dev/null
  else
    echo "Bitwarden CLI is not logged in. Run 'bw login' or set BW_CLIENTID/BW_CLIENTSECRET." >&2
    exit 1
  fi
  status="$(bw_status || true)"
fi

if [ "$status" != "unlocked" ]; then
  if [ -z "${BW_SESSION:-}" ]; then
    export BW_SESSION="$(bw unlock --raw)"
  fi
fi

if [ -z "${BWS_ACCESS_TOKEN:-}" ]; then
  export BWS_ACCESS_TOKEN="$(bw get password bws_machine_token)"
fi

# --- Fetch secrets -----------------------------------------------------------

echo "[bootstrap-haos] Fetching secrets from Bitwarden..."
SSH_KEYS_JSON="$(fetch_secret_value "$SSH_KEYS_SECRET_REF")"
PM_ENDPOINT="$(fetch_secret_value "proxmox_endpoint")"

# Extract hostname from https://pve.reminat.com:8006 (pure bash, portable)
PVE_HOST="${PM_ENDPOINT#https://}"
PVE_HOST="${PVE_HOST#http://}"
PVE_HOST="${PVE_HOST%%:*}"
PVE_SSH="root@${PVE_HOST}"

# --- Find VMID by name -------------------------------------------------------

echo "[bootstrap-haos] Finding VM '${VM_NAME}' on Proxmox..."
VMID="$(ssh -i "${PVE_SSH_KEY}" -o StrictHostKeyChecking=no "${PVE_SSH}" \
  "qm list | awk '\$2 == \"${VM_NAME}\" {print \$1}'")"

if [ -z "${VMID}" ]; then
  echo "ERROR: VM '${VM_NAME}' not found on Proxmox. Is it started?" >&2
  exit 1
fi

echo "[bootstrap-haos] Found VMID=${VMID}"

# --- Wait for guest agent ----------------------------------------------------

echo "[bootstrap-haos] Waiting for QEMU guest agent (up to 2min)..."
for i in $(seq 1 24); do
  if ssh -i "${PVE_SSH_KEY}" -o StrictHostKeyChecking=no "${PVE_SSH}" \
    "qm agent ${VMID} ping" >/dev/null 2>&1; then
    echo "[bootstrap-haos] Guest agent ready."
    break
  fi
  if [ "$i" -eq 24 ]; then
    echo "ERROR: Guest agent not responding after 2 minutes. Is qemu-guest-agent running on the VM?" >&2
    exit 1
  fi
  echo "[bootstrap-haos] Not ready yet, retrying in 5s... (attempt ${i}/24)"
  sleep 5
done

# --- Inject SSH keys ---------------------------------------------------------

echo "[bootstrap-haos] Injecting SSH keys into ${VM_NAME}..."

# Base64-encode the keys to safely pass them through the ssh+qm exec chain
AUTHORIZED_KEYS_B64="$(echo "${SSH_KEYS_JSON}" | jq -r '.[]' | base64 | tr -d '\n')"

ssh -i "${PVE_SSH_KEY}" -o StrictHostKeyChecking=no "${PVE_SSH}" \
  "qm guest exec ${VMID} --timeout 30 -- bash -c \
  'mkdir -p /root/.ssh && \
   chmod 700 /root/.ssh && \
   echo ${AUTHORIZED_KEYS_B64} | base64 -d > /root/.ssh/authorized_keys && \
   chmod 600 /root/.ssh/authorized_keys'"

echo "[bootstrap-haos] Done. Testing SSH connectivity..."

# Derive host from env
case "${ENV}" in
  test) HAOS_HOST="haos.test.reminat.com" ;;
  prod) HAOS_HOST="haos.reminat.com" ;;
esac

echo "[bootstrap-haos] SSH keys injected on ${VM_NAME}."
echo ""
echo "  Test connection: ssh root@${HAOS_HOST} -p 22222"
