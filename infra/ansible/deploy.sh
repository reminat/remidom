#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MQTT_SECRET_REF="${MQTT_SECRET_REF:-mqtt_password}"
Z2M_NETWORK_KEY_SECRET_REF="${Z2M_NETWORK_KEY_SECRET_REF:-}"
CF_DNS_API_TOKEN_SECRET_REF="${CF_DNS_API_TOKEN_SECRET_REF:-cloudflare_dns_api_token}"

# First argument is the target environment (test or prod).
ENV="${1:-}"
if [ -z "${ENV}" ]; then
  echo "Usage: ./deploy.sh <env>   (e.g. test or prod)" >&2
  exit 1
fi
shift

INVENTORY="${SCRIPT_DIR}/inventories/${ENV}/hosts.ini"
if [ ! -f "${INVENTORY}" ]; then
  echo "No inventory found for env '${ENV}': ${INVENTORY}" >&2
  exit 1
fi

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing command: $1" >&2; exit 1; }
}

require_cmd bws
require_cmd jq
require_cmd ansible-playbook

# En CI, BWS_ACCESS_TOKEN est injecté directement via les secrets GitHub.
# bw n'est nécessaire qu'en local pour le récupérer depuis le vault.
if [ -z "${BWS_ACCESS_TOKEN:-}" ]; then
  require_cmd bw

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
Z2M_NETWORK_KEY_SECRET_REF="${Z2M_NETWORK_KEY_SECRET_REF:-zigbee_network_key_${ENV}}"
export Z2M_NETWORK_KEY="$(fetch_secret_value "$Z2M_NETWORK_KEY_SECRET_REF")"
export CF_DNS_API_TOKEN="$(fetch_secret_value "$CF_DNS_API_TOKEN_SECRET_REF")"

cd "$SCRIPT_DIR"
ansible-playbook -i "${INVENTORY}" playbooks/docker-host.yml "$@"
ansible-playbook -i "${INVENTORY}" playbooks/haos.yml "$@"
