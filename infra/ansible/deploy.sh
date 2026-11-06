#!/usr/bin/env bash
# Ne jamais tracer ce script : il lit des secrets. Coupe aussi une trace héritée de
# « bash -x » ou de SHELLOPTS, et tout piège DEBUG/RETURN, avant toute autre commande.
set +x +v
trap - DEBUG RETURN

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MQTT_SECRET_REF="${MQTT_SECRET_REF:-mqtt_password}"
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

Z2M_NETWORK_KEY_SECRET_REF="${Z2M_NETWORK_KEY_SECRET_REF:-zigbee_network_key_${ENV}}"
RESTIC_REST_PASSWORD_SECRET_REF="${RESTIC_REST_PASSWORD_SECRET_REF:-restic_rest_password_${ENV}}"
RESTIC_PASSWORD_SECRET_REF="${RESTIC_PASSWORD_SECRET_REF:-restic_password_${ENV}}"

die() {
  echo "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing command: $1"
}

require_cmd bws
require_cmd jq
require_cmd ansible-inventory
require_cmd ansible-playbook

# ansible.cfg est lu dans le répertoire courant.
cd "$SCRIPT_DIR"

# Sauvegarde activée sur un hôte de l'inventaire ? Lu avant tout secret. Les variables ne
# sont pas évaluées ici : toute valeur autre qu'un faux explicite compte comme activée.
# Un « -e restic_backup_enabled=true » n'est pas vu ici : le rôle s'arrête alors sur ses
# propres contrôles, faute de secrets.
restic_backup_required() {
  ansible-inventory -i "${INVENTORY}" --list | jq -r '
    [._meta.hostvars | to_entries[].value.restic_backup_enabled // false | tostring | ascii_downcase]
    | any(. as $v | ["false", "no", "off", "0", "n", "f"] | index($v) | not)
  '
}

RESTIC_REQUIRED="$(restic_backup_required)" || die "Inventaire '${ENV}' illisible : arrêt avant toute lecture de secret."
case "$RESTIC_REQUIRED" in
  true|false) ;;
  *) die "Inventaire '${ENV}' : réponse inattendue, arrêt avant toute lecture de secret." ;;
esac

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
      die "Bitwarden CLI is not logged in. Run 'bw login' or set BW_CLIENTID/BW_CLIENTSECRET."
    fi
    status="$(bw_status || true)"
  fi

  # Ensure vault is unlocked. Assignation et export séparés : export masquerait l'échec.
  if [ "$status" != "unlocked" ]; then
    if [ -z "${BW_SESSION:-}" ]; then
      BW_SESSION="$(bw unlock --raw)" || die "Déverrouillage de Bitwarden impossible."
      export BW_SESSION
    fi
  fi

  BWS_ACCESS_TOKEN="$(bw get password bws_machine_token)" || die "Lecture de bws_machine_token impossible."
  [ -n "$BWS_ACCESS_TOKEN" ] || die "bws_machine_token est vide."
  export BWS_ACCESS_TOKEN
fi

# Les sorties d'erreur de bws et de jq sont écartées : les journaux de la CI sont publics.
# Les messages ne citent que la référence du secret, jamais sa valeur.
resolve_secret_id() {
  local ref="$1"
  if [[ "$ref" =~ ^(urn:uuid:)?[0-9a-fA-F-]{36}$ ]]; then
    printf '%s\n' "$ref"
    return 0
  fi

  bws secret list 2>/dev/null | jq -r --arg ref "$ref" '
    (map(select((.key // "") == $ref or (.name // "") == $ref)) | .[0].id) // empty
  ' 2>/dev/null
}

fetch_secret_value() {
  local ref="$1" id
  id="$(resolve_secret_id "$ref")" || die "Liste des secrets illisible (bws) en cherchant '${ref}'."
  [ -n "$id" ] || die "Secret '${ref}' introuvable (clé, nom ou UUID attendu)."
  # -e et « strings » : une valeur absente, null ou non textuelle est une erreur.
  bws secret get "$id" 2>/dev/null | jq -er '.value | strings' 2>/dev/null \
    || die "Lecture du secret '${ref}' impossible."
}

is_single_line() {
  [[ "$1" != *$'\n'* ]]
}

# Tableau JSON de 16 octets : seul format accepté, GENERATE compris est refusé.
is_zigbee_key() {
  printf '%s' "$1" | jq -se '
    length == 1 and (.[0] | type == "array" and length == 16
      and all(.[]; type == "number" and . == floor and . >= 0 and . <= 255))
  ' >/dev/null 2>&1
}

# load_secret <variable> <référence> [contrôle] : exporte la variable, ou arrête le script.
load_secret() {
  local name="$1" ref="$2" check="${3:-is_single_line}" value
  value="$(fetch_secret_value "$ref")" || exit 1
  case "${value//[[:space:]]/}" in
    ""|null) die "Secret '${ref}' vide ou null." ;;
  esac
  "$check" "$value" || die "Secret '${ref}' : format invalide."
  printf -v "$name" '%s' "$value"
  export "${name?}"
}

load_secret MQTT_PASSWORD "$MQTT_SECRET_REF"
load_secret Z2M_NETWORK_KEY "$Z2M_NETWORK_KEY_SECRET_REF" is_zigbee_key
load_secret CF_DNS_API_TOKEN "$CF_DNS_API_TOKEN_SECRET_REF"
if [ "$RESTIC_REQUIRED" = "true" ]; then
  load_secret RESTIC_REST_PASSWORD "$RESTIC_REST_PASSWORD_SECRET_REF"
  load_secret RESTIC_REPO_PASSWORD "$RESTIC_PASSWORD_SECRET_REF"
fi

ansible-playbook -i "${INVENTORY}" playbooks/docker-host.yml "$@"
ansible-playbook -i "${INVENTORY}" playbooks/haos.yml "$@"
