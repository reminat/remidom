#!/usr/bin/env bash
set -euo pipefail

log() {
  # Usage: log "message"
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2
}

# Enable verbose tracing with: TFWRAP_DEBUG=1 ./tf.sh plan
if [ "${TFWRAP_DEBUG:-}" = "1" ]; then
  set -x
  export TF_LOG=${TF_LOG:-DEBUG}
fi

trap 'rc=$?; log "ERROR: command failed (exit=$rc) at line $LINENO"; exit $rc' ERR

# Wrapper Terraform: hydrate required creds from Bitwarden (bw + bws) without storing them in terraform.tfvars

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing command: $1" >&2; exit 1; }
}

require_cmd bw
require_cmd bws
require_cmd python3
require_cmd terraform
require_cmd rsync

# Backup configuration
BACKUP_HOST="192.168.1.200"
BACKUP_USER="remi"
BACKUP_PORT="4022"
BACKUP_BASE_DIR="/volume1/TimeMachine/terraform-state-backups"

bw_status() {
  # returns: unauthenticated | locked | unlocked | (empty on error)
  bw status 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status",""))'
}

status="$(bw_status || true)"
log "Bitwarden status: ${status:-<empty>}"

# 0) Ensure logged-in
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

# 1) Ensure vault unlocked (interactive once per shell)
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

# 2) Ensure we have a Secrets Manager access token for bws.
# Store this token in Bitwarden PM as an item password named: bws_machine_token
if [ -z "${BWS_ACCESS_TOKEN:-}" ]; then
  log "BWS_ACCESS_TOKEN not set, fetching from Bitwarden Password Manager item: bws_machine_token"
  export BWS_ACCESS_TOKEN="$(bw get password bws_machine_token)"
  log "BWS_ACCESS_TOKEN loaded"
fi

# 3) Fetch Proxmox credentials from Bitwarden Secrets Manager.
# We keep human-friendly secret key/name refs here and resolve them to UUIDs because `bws secret get` expects a secret ID.
PM_TOKEN_SECRET_REF="pm_api_token_secret"
PM_TOKEN_ID_REF="pm_api_token_id"
PM_ENDPOINT_REF="pm_endpoint"

is_uuid_ref() {
  # Accept either raw UUID or urn:uuid:<uuid>
  [[ "$1" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] || [[ "$1" =~ ^urn:uuid:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]
}

resolve_secret_id() {
  local ref="$1"
  if is_uuid_ref "$ref"; then
    echo "$ref"
    return 0
  fi

  # Try to find the secret by key/name from the list.
  # `bws secret list` should return JSON; if it doesn't, we fail fast.
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

fetch_secret_value() {
  local ref="$1"
  log "Resolving Secrets Manager secret ref '$ref' to an ID"
  local sid
  sid="$(resolve_secret_id "$ref")" || {
    echo "Could not resolve Bitwarden Secrets Manager secret '$ref'." >&2
    echo "Fix: use a real secret UUID (or urn:uuid:...) or ensure a secret exists with key/name '$ref'." >&2
    exit 1
  }
  log "Resolved secret id: $sid"

  log "Fetching secret value via bws secret get"
  bws secret get "$sid" | python3 -c 'import sys,json; print(str(json.load(sys.stdin)["value"]).strip())'
}


mask() {
  local v="${1:-}"
  if [ -z "$v" ]; then
    echo "<empty>"
  else
    printf "%s... (len=%d)" "${v:0:8}" "${#v}"
  fi
}

backup_state() {
  local host="$BACKUP_HOST"
  local user="$BACKUP_USER"
  local port="$BACKUP_PORT"
  local dst="${user}@${host}:${BACKUP_BASE_DIR}"
  local ts
  ts="$(date +%Y%m%d-%H%M%S)"

  # Ensure destination exists (ignore failure if permissions prevent mkdir)
  ssh -p "$port" "${user}@${host}" "mkdir -p ${BACKUP_BASE_DIR}" >/dev/null 2>&1 || true

  # Main state (timestamped)
  rsync -a -e "ssh -p ${port}" --rsync-path="/usr/bin/rsync" terraform.tfstate "${dst}/terraform.tfstate.${ts}"

  # Terraform's backup file if present (timestamped)
  if [ -f terraform.tfstate.backup ]; then
    rsync -a -e "ssh -p ${port}" --rsync-path="/usr/bin/rsync" terraform.tfstate.backup "${dst}/terraform.tfstate.backup.${ts}"
  fi

  log "State backed up to ${dst} (ts=${ts})"
}

export TF_VAR_pm_api_token_secret="$(fetch_secret_value "$PM_TOKEN_SECRET_REF")"
export TF_VAR_pm_api_token_id="$(fetch_secret_value "$PM_TOKEN_ID_REF")"
export TF_VAR_pm_endpoint="$(fetch_secret_value "$PM_ENDPOINT_REF")"

# Debug: show what we are actually exporting to Terraform (masked)
log "TF_VAR_pm_endpoint = $(mask "${TF_VAR_pm_endpoint:-}")"
log "TF_VAR_pm_api_token_id = $(mask "${TF_VAR_pm_api_token_id:-}")"
log "TF_VAR_pm_api_token_secret = $(mask "${TF_VAR_pm_api_token_secret:-}")"

# 4) Run terraform
log "Running: terraform $*"
terraform "$@"
rc=$?

# Backup only after mutating commands
case "${1:-}" in
  apply|destroy)
    backup_state
    ;;
esac

exit $rc