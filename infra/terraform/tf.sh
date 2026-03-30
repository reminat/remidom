#!/usr/bin/env bash

# -----------------------------------------------------------------------------
# tf.sh, Terraform wrapper for this env
#
# What this script does (high level):
#   - Ensures Bitwarden CLI (`bw`) is logged in and the vault is unlocked
#   - Ensures Bitwarden Secrets Manager CLI (`bws`) has an access token
#   - Fetches Proxmox provider credentials from Bitwarden Secrets Manager
#   - Exports them as TF_VAR_* so Terraform providers can read them
#   - Runs `terraform` with the arguments you provide
#   - After mutating commands (apply/destroy), backs up terraform state to the NAS
#
# Why it exists:
#   - Avoid storing secrets in terraform.tfvars / repo files
#   - Keep the workflow "terraform ..." identical, with secrets injected at runtime
#
# Key assumptions:
#   - You have `bw` and `bws` installed and configured
#   - Bitwarden PM contains an item password named `bws_machine_token`
#   - Bitwarden Secrets Manager contains Proxmox secrets keyed/named:
#       * pm_api_token_secret
#       * pm_api_token_id
#       * pm_endpoint
#   - For modules that declare it, it can also inject:
#       * ssh_authorized_keys_json (JSON array of SSH public keys)
#   - NAS (Synology) is reachable via SSH on BACKUP_PORT and accepts your SSH key
#   - Remote rsync binary is at /usr/bin/rsync (forced because Synology PATH can differ)
# -----------------------------------------------------------------------------

# Fail fast: -e stop on error, -u error on unset var, pipefail catch errors in pipelines
set -euo pipefail

TFWRAP_SHARED_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TFWRAP_ENVS_DIR="${TFWRAP_SHARED_DIR}/envs"

# If the first argument is a path that exists under envs/, cd there and shift it off.
# This allows calling tf.sh from the terraform/ root:
#   ./tf.sh test/pve/docker-host apply
#   ./tf.sh prod/pve/haos plan
if [ $# -ge 1 ] && [ -d "${TFWRAP_ENVS_DIR}/${1}" ]; then
  cd "${TFWRAP_ENVS_DIR}/${1}"
  shift
fi

# Simple timestamped logger (stderr)
log() {
  # Usage: log "message"
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2
}

# Optional debug mode:
#   TFWRAP_DEBUG=1 enables shell tracing (set -x) and Terraform debug logs (TF_LOG=DEBUG)
if [ "${TFWRAP_DEBUG:-}" = "1" ]; then
  set -x
  export TF_LOG=${TF_LOG:-DEBUG}
fi

# If any command fails, print the line number to speed up debugging
trap 'rc=$?; log "ERROR: command failed (exit=$rc) at line $LINENO"; exit $rc' ERR

# Hard dependency check: fail early if a required binary is missing
require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing command: $1" >&2; exit 1; }
}

# Tooling needed by this wrapper
require_cmd bw
require_cmd bws
require_cmd python3
require_cmd terraform
require_cmd rsync

# Per-env config: if a tf.conf exists in the current directory, source it.
# It can define TEMPLATE_SCRIPT to trigger a remote template build before apply.
# Example tf.conf:
#   TEMPLATE_SCRIPT=template-docker.sh
if [ -f tf.conf ]; then
  # shellcheck source=/dev/null
  source tf.conf
fi

# Optional pre-apply step: ensure the Proxmox template exists before Terraform runs.
# Triggered when TEMPLATE_SCRIPT and TEMPLATE_VM_ID are set (via tf.conf) and command is apply.
# - If the template VM already exists on Proxmox: skip.
# - If it doesn't exist: copy the script from the repo and run it on Proxmox.
# Disable entirely with: TFWRAP_SKIP_TEMPLATE_BUILD=1 ./tf.sh <env> apply
if [ "${1:-}" = "apply" ] && [ -n "${TEMPLATE_SCRIPT:-}" ] && [ -n "${TEMPLATE_VM_ID:-}" ] && [ "${TFWRAP_SKIP_TEMPLATE_BUILD:-0}" != "1" ]; then
  PROXMOX_HOST="${PROXMOX_HOST:-pve.reminat.com}"
  PROXMOX_USER="${PROXMOX_USER:-root}"
  PROXMOX_SSH_OPTS="-i ${HOME}/.ssh/id_ed25519_pve -o StrictHostKeyChecking=no"

  log "Checking if Proxmox template VM ${TEMPLATE_VM_ID} exists on ${PROXMOX_HOST}..."

  if ssh ${PROXMOX_SSH_OPTS} "${PROXMOX_USER}@${PROXMOX_HOST}" "qm status ${TEMPLATE_VM_ID}" >/dev/null 2>&1; then
    log "Template VM ${TEMPLATE_VM_ID} already exists, skipping build."
  else
    log "Template VM ${TEMPLATE_VM_ID} not found — building from ${TEMPLATE_SCRIPT}..."
    SCRIPT_SRC="${TFWRAP_SHARED_DIR}/../proxmox/scripts/${TEMPLATE_SCRIPT}"
    if [ ! -f "${SCRIPT_SRC}" ]; then
      echo "ERROR: template script not found: ${SCRIPT_SRC}" >&2
      exit 1
    fi
    log "Copying ${TEMPLATE_SCRIPT} to ${PROXMOX_HOST}:/tmp/..."
    scp ${PROXMOX_SSH_OPTS} "${SCRIPT_SRC}" "${PROXMOX_USER}@${PROXMOX_HOST}:/tmp/${TEMPLATE_SCRIPT}"
    log "Running ${TEMPLATE_SCRIPT} on ${PROXMOX_HOST}..."
    ssh ${PROXMOX_SSH_OPTS} "${PROXMOX_USER}@${PROXMOX_HOST}" "bash /tmp/${TEMPLATE_SCRIPT}"
  fi
fi

# Backup configuration
# These values are hardcoded for this env. Change here if your NAS/user/port/path changes.
# Destination path is on the NAS (Synology): it must exist or be creatable.
BACKUP_HOST="192.168.1.200"
BACKUP_USER="remi"
BACKUP_PORT="4022"
BACKUP_BASE_DIR="/volume1/TimeMachine/terraform-state-backups"

# Build a stable backup scope from the Terraform working directory.
# Example:
#   .../infra/terraform/envs/prod/pve/docker-host -> prod_pve_docker-host
derive_backup_scope() {
  local cwd rel
  cwd="$(pwd -P)"

  if [[ "$cwd" == "${TFWRAP_ENVS_DIR}/"* ]]; then
    rel="${cwd#${TFWRAP_ENVS_DIR}/}"
  else
    rel="$(basename "$cwd")"
  fi

  # Keep only filesystem-safe chars and normalize path separators/spaces.
  printf '%s' "$rel" | tr '/ ' '__' | tr -cs 'A-Za-z0-9._-' '_'
}

# Read Bitwarden vault status as a single word:
#   unauthenticated | locked | unlocked
bw_status() {
  bw status 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status",""))'
}

# Snapshot the current Bitwarden status once, then transition through login/unlock if needed
status="$(bw_status || true)"
log "Bitwarden status: ${status:-<empty>}"

# Step 0: ensure `bw` is logged in
# If not logged in, we attempt a non-interactive login via API key (BW_CLIENTID/BW_CLIENTSECRET)
if [ "$status" = "unauthenticated" ] || [ -z "$status" ]; then
  # Try non-interactive login via API key if available
  if [ -n "${BW_CLIENTID:-}" ] && [ -n "${BW_CLIENTSECRET:-}" ]; then
    log "bw unauthenticated, attempting login via API key (BW_CLIENTID/BW_CLIENTSECRET)"
    bw login --apikey >/dev/null
  else
    log "bw unauthenticated and no API key env vars set"
    echo "Bitwarden CLI is not logged in. Do one of:" >&2
    echo "  1) Run: bw login" >&2
    echo "  2) Or set BW_CLIENTID and BW_CLIENTSECRET, then rerun (it will do: bw login --apikey)" >&2
    exit 1
  fi
  status="$(bw_status)"
  log "Bitwarden status after login attempt: ${status:-<empty>}"
fi

# Step 1: ensure the vault is unlocked
# If BW_SESSION is not set, `bw unlock --raw` will prompt once and then we export BW_SESSION
if [ "$status" != "unlocked" ]; then
  if [ -n "${BW_SESSION:-}" ]; then
    log "BW_SESSION already set in environment, re-checking status"
  else
    log "Vault locked, running: bw unlock --raw"
    export BW_SESSION="$(bw unlock --raw)"
  fi
  status="$(bw_status)"
  log "Bitwarden status after unlock check: ${status:-<empty>}"
else
  log "Vault already unlocked"
fi

if [ "$status" != "unlocked" ]; then
  echo "Failed to unlock Bitwarden vault." >&2
  exit 1
fi

# Step 2: ensure `bws` can talk to Secrets Manager
# We load BWS_ACCESS_TOKEN from Bitwarden Password Manager item password: `bws_machine_token`
if [ -z "${BWS_ACCESS_TOKEN:-}" ]; then
  log "BWS_ACCESS_TOKEN not set, fetching from Bitwarden Password Manager item: bws_machine_token"
  export BWS_ACCESS_TOKEN="$(bw get password bws_machine_token)"
  log "BWS_ACCESS_TOKEN loaded"
fi

# Step 3: fetch Proxmox credentials from Bitwarden Secrets Manager
# `bws secret get` expects a secret UUID. For convenience we keep human-friendly refs
# (key or name) below and resolve them to UUIDs using `bws secret list`.
PM_TOKEN_SECRET_REF="pm_api_token_secret"
PM_TOKEN_ID_REF="pm_api_token_id"
PM_ENDPOINT_REF="pm_endpoint"
SSH_AUTHORIZED_KEYS_REF="ssh_authorized_keys_json"

is_uuid_ref() {
  # Accept either raw UUID or urn:uuid:<uuid>
  # If the ref is already a UUID, we skip listing and matching.
  [[ "$1" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] || [[ "$1" =~ ^urn:uuid:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]
}

resolve_secret_id() {
  local ref="$1"
  if is_uuid_ref "$ref"; then
    echo "$ref"
    return 0
  fi

  # Otherwise, list all secrets once and find a match by key/name (or id/uuid).
  # Note: `bws secret list` must return JSON; we suppress stderr in the python snippet.
  bws secret list | python3 -c 'import sys, json
ref = sys.argv[1]
data = json.load(sys.stdin)
# data is typically a list of secrets
if isinstance(data, dict) and "data" in data:
    data = data["data"]
if not isinstance(data, list):
    raise SystemExit("Unexpected output from bws secret list")
for s in data:
    if not isinstance(s, dict):
        continue
    # Try common fields
    if s.get("key") == ref or s.get("name") == ref:
        sid = s.get("id") or s.get("uuid")
        if sid:
            print(sid)
            raise SystemExit(0)
# Also allow matching on exact ID string if user pasted it without urn prefix
for s in data:
    if isinstance(s, dict) and (s.get("id") == ref or s.get("uuid") == ref):
        sid = s.get("id") or s.get("uuid")
        if sid:
            print(sid)
            raise SystemExit(0)
raise SystemExit(1)
' "$ref" 2>/dev/null || return 1
}

# Fetches the value of a secret given a ref (key/name) or UUID.
# Returns the raw secret value on stdout.
fetch_secret_value() {
  local ref="$1"
  log "Resolving Secrets Manager secret ref '$ref' to an ID"
  local sid
  sid="$(resolve_secret_id "$ref")" || {
    echo "Could not resolve Bitwarden Secrets Manager secret '$ref'." >&2
    echo "Fix: use a real secret UUID (or urn:uuid:...) or ensure a secret exists with key/name '$ref'." >&2
    exit 1
  }

  bws secret get "$sid" | python3 -c 'import sys,json; print(str(json.load(sys.stdin)["value"]).strip())'
}

# Helper to print a masked version of a secret (never print full secrets in logs)
mask() {
  local v="${1:-}"
  if [ -z "$v" ]; then
    echo "<empty>"
  else
    printf "%s... (len=%d)" "${v:0:8}" "${#v}"
  fi
}

# Backup terraform state files to the NAS (timestamped) after apply/destroy
# We keep multiple timestamped copies for safety.
backup_state() {
  local host="$BACKUP_HOST"
  local user="$BACKUP_USER"
  local port="$BACKUP_PORT"
  local scope
  scope="${TFWRAP_BACKUP_SCOPE:-$(derive_backup_scope)}"
  local remote_dir="${BACKUP_BASE_DIR}/${scope}"
  local dst="${user}@${host}:${remote_dir}"
  local ts
  ts="$(date +%Y%m%d-%H%M%S)"

  # Best effort: ensure destination exists. Ignore failure (ACLs/permissions on Synology shares can differ)
  ssh -p "$port" "${user}@${host}" "mkdir -p ${remote_dir}" >/dev/null 2>&1 || true

  # Main state file (always present after first terraform run)
  rsync -a -e "ssh -p ${port}" --rsync-path="/usr/bin/rsync" terraform.tfstate "${dst}/terraform.tfstate.${ts}"

  # Terraform also keeps a local backup file sometimes, back it up too if present
  if [ -f terraform.tfstate.backup ]; then
    rsync -a -e "ssh -p ${port}" --rsync-path="/usr/bin/rsync" terraform.tfstate.backup "${dst}/terraform.tfstate.backup.${ts}"
  fi

  log "State backed up to ${dst} (scope=${scope}, ts=${ts})"
}

export TF_VAR_pm_api_token_secret="$(fetch_secret_value "$PM_TOKEN_SECRET_REF")"
export TF_VAR_pm_api_token_id="$(fetch_secret_value "$PM_TOKEN_ID_REF")"
export TF_VAR_pm_endpoint="$(fetch_secret_value "$PM_ENDPOINT_REF")"

# Optionally inject SSH keys list when the current module declares ssh_authorized_keys.
# Expected secret format: JSON array, e.g. ["ssh-ed25519 AAAA... user@host", "..."].
if [ -f variables.tf ] && grep -q 'variable "ssh_authorized_keys"' variables.tf; then
  export TF_VAR_ssh_authorized_keys="$(fetch_secret_value "$SSH_AUTHORIZED_KEYS_REF")"
  log "TF_VAR_ssh_authorized_keys = $(mask "${TF_VAR_ssh_authorized_keys:-}")"
fi

# Sanity check logs (masked): helps confirm the wrapper injected something without leaking secrets
log "TF_VAR_pm_endpoint = $(mask "${TF_VAR_pm_endpoint:-}")"
log "TF_VAR_pm_api_token_id = $(mask "${TF_VAR_pm_api_token_id:-}")"
log "TF_VAR_pm_api_token_secret = $(mask "${TF_VAR_pm_api_token_secret:-}")"

# Step 4: run terraform with whatever args were provided to this script
log "Running: terraform $*"
terraform "$@"
rc=$?

# Only back up state after mutating commands, avoid noise on plan/validate
case "${1:-}" in
  apply|destroy)
    backup_state
    ;;
esac

exit $rc
