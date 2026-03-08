#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MQTT_SECRET_REF="${MQTT_SECRET_REF:-MQTT_PASSWORD}"
Z2M_NETWORK_KEY_SECRET_REF="${Z2M_NETWORK_KEY_SECRET_REF:-Z2M_NETWORK_KEY_JSON}"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing command: $1" >&2; exit 1; }
}

require_cmd bw
require_cmd bws
require_cmd jq
require_cmd ansible-playbook

bw_status() {
  bw status 2>/dev/null | jq -r '.status // empty'
}

status="$(bw_status || true)"

# Ensure Bitwarden CLI is logged in.
if [ "$status" = "unauthenticated" ] || [ -z "$status" ]; then
  if [ -n "${BW_CLIENTID:-}" ] && [ -n "${BW_CLIENTSECRET:-}" ]; then
    bw login --apikey >/dev/null
  else
    echo "Bitwarden CLI is not logged in. Run 'bw login' or set BW_CLIENTID/BW_CLIENTSECRET." >&2
    exit 1
  fi
  status="$(bw_status || true)"
fi

# Ensure vault is unlocked.
if [ "$status" != "unlocked" ]; then
  if [ -z "${BW_SESSION:-}" ]; then
    export BW_SESSION="$(bw unlock --raw)"
  fi
fi

# Ensure bws has an access token, same pattern as infra/terraform/tf.sh.
if [ -z "${BWS_ACCESS_TOKEN:-}" ]; then
  export BWS_ACCESS_TOKEN="$(bw get password bws_machine_token)"
fi

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
    echo "Could not resolve Bitwarden secret ref '${ref}' (expected key/name or UUID)." >&2
    exit 1
  fi
  bws secret get "$id" | jq -r '.value'
}

export MQTT_PASSWORD="$(fetch_secret_value "$MQTT_SECRET_REF")"
export Z2M_NETWORK_KEY="$(fetch_secret_value "$Z2M_NETWORK_KEY_SECRET_REF")"

cd "$SCRIPT_DIR"
ansible-playbook playbooks/docker-host.yml
