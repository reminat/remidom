#!/usr/bin/env bash
# Tests isolés de ../deploy.sh et des contrôles de secrets des rôles.
#
# Exécute une copie de deploy.sh dans un dossier temporaire, avec de faux bw, bws,
# ansible-inventory et ansible-playbook en tête du PATH, un HOME temporaire et uniquement des
# valeurs fictives. Aucun identifiant, aucun accès réseau, aucun outil d'infrastructure réel.
#
# Si un vrai ansible-playbook est disponible, les contrôles et les rendus des rôles sont aussi
# exécutés sur localhost, sans élévation, avec des fichiers écrits uniquement dans le dossier
# temporaire. Sinon, ces cas sont signalés comme ignorés.
#
# Usage : bash infra/ansible/tests/deploy-wrapper-test.sh
# Code de sortie : 0 si tous les cas exécutés réussissent, 1 sinon.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ANSIBLE_DIR="$(cd -- "${HERE}/.." && pwd)"
REPO_DIR="$(cd -- "${ANSIBLE_DIR}/../.." && pwd)"
command -v jq >/dev/null 2>&1 || { echo "jq est requis (utilisé par deploy.sh)" >&2; exit 1; }
REAL_JQ_DIR="$(dirname -- "$(command -v jq)")"
REAL_PATH="$PATH"

T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

# Valeurs fictives. Toute fuite, même partielle, est détectée par la même recherche.
FAKE_BWS=SENTINEL_BWS_7f3a
FAKE_SESSION=SENTINEL_SESSION_5b77
FAKE_MQTT=SENTINEL_MQTT_9c1e
FAKE_CF=SENTINEL_CF_44b2
FAKE_REST=SENTINEL_REST_d81f
FAKE_REPO=SENTINEL_REPO_a0e5
FAKE_Z2M='[201,202,203,204,205,206,207,208,209,210,211,212,213,214,215,216]'
LEAK_PATTERN='SENTINEL|201, ?202, ?203'

# --- Installation ------------------------------------------------------------

mkdir -p "$T/bin" "$T/home" "$T/ansible/inventories/test" "$T/ansible/inventories/prod"
cp "${ANSIBLE_DIR}/deploy.sh" "$T/ansible/deploy.sh"
touch "$T/ansible/inventories/test/hosts.ini" "$T/ansible/inventories/prod/hosts.ini"

cat > "$T/bin/bw" <<FAKE
#!/usr/bin/env bash
echo "bw \$1" >> "\$CALLS"
case "\$1" in
  status) printf '{"status":"%s"}\n' "\${FAKE_BW_STATUS:-unlocked}" ;;
  unlock) [ -z "\${FAKE_BW_UNLOCK_FAIL:-}" ] || exit 1; printf '%s' "$FAKE_SESSION" ;;
  login)  exit 0 ;;
  get)    [ -z "\${FAKE_BW_GET_FAIL:-}" ] || exit 1
          [ -n "\${FAKE_BW_GET_EMPTY:-}" ] || printf '%s' "$FAKE_BWS" ;;
esac
FAKE

cat > "$T/bin/bws" <<'FAKE'
#!/usr/bin/env bash
# FAKE_LIST_FAIL / FAKE_LIST_GARBAGE : liste en échec ou illisible.
# FAKE_ABSENT, FAKE_GET_FAIL, FAKE_GET_GARBAGE, FAKE_NULL, FAKE_NOVALUE : clé visée.
# V_<clé> : valeur renvoyée pour cette clé.
keys="mqtt_password zigbee_network_key_test zigbee_network_key_prod cloudflare_dns_api_token
restic_rest_password_test restic_password_test restic_rest_password_prod restic_password_prod"
echo "bws $1 $2 ${3:-}" >> "$CALLS"
case "$1 $2" in
  "secret list")
    if [ -n "${FAKE_LIST_FAIL:-}" ]; then echo "erreur SENTINEL_BWS_STDERR" >&2; exit 1; fi
    if [ -n "${FAKE_LIST_GARBAGE:-}" ]; then echo "SENTINEL_GARBAGE pas du JSON"; exit 0; fi
    for k in $keys; do
      [ "$k" = "${FAKE_ABSENT:-}" ] || jq -n --arg k "$k" '{key: $k, id: ("id-" + $k), value: "SENTINEL_LIST_VALUE"}'
    done | jq -s .
    ;;
  "secret get")
    k="${3#id-}"
    if [ "$k" = "${FAKE_GET_FAIL:-}" ]; then echo "erreur SENTINEL_BWS_STDERR" >&2; exit 1; fi
    if [ "$k" = "${FAKE_GET_GARBAGE:-}" ]; then echo "SENTINEL_GARBAGE pas du JSON"; exit 0; fi
    if [ "$k" = "${FAKE_NULL:-}" ]; then echo '{"value":null}'; exit 0; fi
    if [ "$k" = "${FAKE_NOVALUE:-}" ]; then echo '{}'; exit 0; fi
    var="V_$k"
    jq -n --arg v "${!var}" '{value: $v}'
    ;;
esac
FAKE

cat > "$T/bin/ansible-inventory" <<'FAKE'
#!/usr/bin/env bash
# Note si un secret est déjà présent dans l'environnement à ce moment-là.
echo "ansible-inventory secrets=${BWS_ACCESS_TOKEN:+token}${MQTT_PASSWORD:+mqtt}" >> "$CALLS"
if [ -n "${FAKE_INV_FAIL:-}" ]; then exit 1; fi
if [ -n "${FAKE_INV_GARBAGE:-}" ]; then echo "pas du JSON"; exit 0; fi
printf '{"_meta":{"hostvars":{"nas":{},"hote":{%s}}}}\n' "${FAKE_INV_VARS:-}"
FAKE

cat > "$T/bin/ansible-playbook" <<'FAKE'
#!/usr/bin/env bash
echo "ansible-playbook $*" >> "$CALLS"
{
  echo "MQTT_PASSWORD=${MQTT_PASSWORD:-}"
  echo "Z2M_NETWORK_KEY=${Z2M_NETWORK_KEY:-}"
  echo "CF_DNS_API_TOKEN=${CF_DNS_API_TOKEN:-}"
  echo "RESTIC_REST_PASSWORD=${RESTIC_REST_PASSWORD:-}"
  echo "RESTIC_REPO_PASSWORD=${RESTIC_REPO_PASSWORD:-}"
} > "$ENVSEEN"
exit "${FAKE_PLAYBOOK_RC:-0}"
FAKE
chmod 755 "$T/bin/"*

# --- Exécution ---------------------------------------------------------------

PASS=0
FAIL=0
SKIP=0

record() { # record <ok: 1|0> <libellé> <raison>
  if [ "$1" -eq 1 ]; then
    PASS=$((PASS + 1)); printf 'PASS  %s\n' "$2"
  else
    FAIL=$((FAIL + 1)); printf 'FAIL  %s:%s\n' "$2" "$3"
  fi
}

# run <libellé> <rc attendu : 0 | nz | N> <bash | bash-x> [VAR=valeur ...] -- <arguments de deploy.sh>
# Contrôles facultatifs : EXPECT_CALLS / FORBID_CALLS (regex sur le journal des appels),
# EXPECT_ENV (lignes exactes vues par le faux ansible-playbook).
# Par défaut, un échec interdit tout appel à ansible-playbook.
run() {
  local label="$1" exp="$2" launcher="$3" rc=0 ok=1 why=""
  shift 3
  local envs=()
  while [ "$1" != "--" ]; do envs+=("$1"); shift; done
  shift
  : > "$T/calls.log"
  : > "$T/env.txt"
  local cmd=(bash)
  [ "$launcher" = "bash-x" ] && cmd=(bash -x)
  ( cd "$T/ansible" && env -i HOME="$T/home" PATH="$T/bin:${REAL_JQ_DIR}:/usr/bin:/bin" \
      CALLS="$T/calls.log" ENVSEEN="$T/env.txt" \
      V_mqtt_password="$FAKE_MQTT" V_cloudflare_dns_api_token="$FAKE_CF" \
      V_zigbee_network_key_test="$FAKE_Z2M" V_zigbee_network_key_prod="$FAKE_Z2M" \
      V_restic_rest_password_test="$FAKE_REST" V_restic_password_test="$FAKE_REPO" \
      V_restic_rest_password_prod="$FAKE_REST" V_restic_password_prod="$FAKE_REPO" \
      ${envs[@]+"${envs[@]}"} "${cmd[@]}" ./deploy.sh "$@" ) > "$T/out.txt" 2>&1 || rc=$?

  case "$exp" in
    0)  [ "$rc" -eq 0 ] || { ok=0; why+=" rc=$rc (0 attendu)"; } ;;
    nz) [ "$rc" -ne 0 ] || { ok=0; why+=" rc=0 (échec attendu)"; }
        if grep -q '^ansible-playbook' "$T/calls.log"; then ok=0; why+=" ansible-playbook appelé"; fi ;;
    *)  [ "$rc" -eq "$exp" ] || { ok=0; why+=" rc=$rc ($exp attendu)"; } ;;
  esac
  if grep -Eq "$LEAK_PATTERN" "$T/out.txt"; then ok=0; why+=" valeur fictive dans la sortie"; fi
  if [ -n "${EXPECT_CALLS:-}" ] && ! grep -Eq "$EXPECT_CALLS" "$T/calls.log"; then
    ok=0; why+=" appel manquant /$EXPECT_CALLS/"
  fi
  if [ -n "${FORBID_CALLS:-}" ] && grep -Eq "$FORBID_CALLS" "$T/calls.log"; then
    ok=0; why+=" appel inattendu /$FORBID_CALLS/"
  fi
  if [ -n "${EXPECT_ENV:-}" ] && [ "$(cat "$T/env.txt")" != "$EXPECT_ENV" ]; then
    ok=0; why+=" valeurs transmises à Ansible différentes"
  fi
  EXPECT_CALLS="" FORBID_CALLS="" EXPECT_ENV=""
  record "$ok" "$label (rc=$rc)" "$why"
}

TOKEN="BWS_ACCESS_TOKEN=$FAKE_BWS"
ON='FAKE_INV_VARS="restic_backup_enabled":true'
ENV_OFF=$(printf 'MQTT_PASSWORD=%s\nZ2M_NETWORK_KEY=%s\nCF_DNS_API_TOKEN=%s\nRESTIC_REST_PASSWORD=\nRESTIC_REPO_PASSWORD=' "$FAKE_MQTT" "$FAKE_Z2M" "$FAKE_CF")
ENV_ON=$(printf 'MQTT_PASSWORD=%s\nZ2M_NETWORK_KEY=%s\nCF_DNS_API_TOKEN=%s\nRESTIC_REST_PASSWORD=%s\nRESTIC_REPO_PASSWORD=%s' "$FAKE_MQTT" "$FAKE_Z2M" "$FAKE_CF" "$FAKE_REST" "$FAKE_REPO")

# --- Parcours nominal --------------------------------------------------------

EXPECT_ENV="$ENV_OFF"; FORBID_CALLS='^bw |restic'; EXPECT_CALLS='haos.yml'
run "nominal : sauvegarde désactivée, Restic jamais lu" 0 bash "$TOKEN" -- test
EXPECT_ENV="$ENV_ON"; FORBID_CALLS='^bw '; EXPECT_CALLS='id-restic_password_prod'
run "nominal : sauvegarde activée, Restic lu"           0 bash "$TOKEN" "$ON" -- prod
EXPECT_CALLS='ansible-playbook -i .*docker-host.yml --check --diff --limit x'
run "arguments transmis à ansible-playbook"             0 bash "$TOKEN" -- test --check --diff --limit x
EXPECT_CALLS='bw get'; EXPECT_ENV="$ENV_OFF"
run "nominal : jeton lu dans le vault"                  0 bash -- test
EXPECT_CALLS='bw unlock'; EXPECT_ENV="$ENV_OFF"
run "nominal : vault verrouillé"                        0 bash FAKE_BW_STATUS=locked -- test
EXPECT_ENV="$ENV_OFF"
run "autre référence de secret (MQTT_SECRET_REF)"       0 bash "$TOKEN" MQTT_SECRET_REF=mqtt_password -- test
EXPECT_ENV="$ENV_ON"
run "trace héritée : bash -x"                           0 bash-x "$TOKEN" "$ON" -- prod
EXPECT_ENV="$ENV_ON"
run "trace héritée : SHELLOPTS=xtrace"                  0 bash "$TOKEN" "$ON" SHELLOPTS=xtrace -- prod
run "ansible-playbook en échec : code conservé"         4 bash "$TOKEN" FAKE_PLAYBOOK_RC=4 -- test
FORBID_CALLS='haos.yml'
run "premier playbook en échec : pas de second"         3 bash "$TOKEN" FAKE_PLAYBOOK_RC=3 -- test

# --- Sauvegarde activée ou non, d'après l'inventaire ------------------------

# Une valeur explicitement fausse : aucune lecture Restic.
for v in false '"false"' '"no"' '"off"' 0 null; do
  FORBID_CALLS='restic'
  run "restic_backup_enabled=$v : non requis" 0 bash "$TOKEN" "FAKE_INV_VARS=\"restic_backup_enabled\":$v" -- test
done
# Vraie, ou non interprétable : requis.
for v in true '"yes"' '"{{ x }}"'; do
  EXPECT_CALLS='id-restic_password_test'
  run "restic_backup_enabled=$v : requis" 0 bash "$TOKEN" "FAKE_INV_VARS=\"restic_backup_enabled\":$v" -- test
done
FORBID_CALLS='bws|^bw '
run "inventaire en échec : arrêt avant tout secret"   nz bash "$TOKEN" FAKE_INV_FAIL=1 -- test
FORBID_CALLS='bws|^bw '
run "inventaire illisible : arrêt avant tout secret"  nz bash "$TOKEN" FAKE_INV_GARBAGE=1 -- test
FORBID_CALLS='secrets=[a-z]'
run "inventaire lu sans secret dans l'environnement"  0 bash -- test
FORBID_CALLS='restic'
run "secrets Restic absents, sauvegarde désactivée"   0 bash "$TOKEN" FAKE_ABSENT=restic_password_test -- test

# --- Récupération en échec ---------------------------------------------------

run "liste des secrets en échec"         nz bash "$TOKEN" FAKE_LIST_FAIL=1 -- test
run "liste des secrets illisible"        nz bash "$TOKEN" FAKE_LIST_GARBAGE=1 -- test
for k in mqtt_password zigbee_network_key_test cloudflare_dns_api_token; do
  run "$k : lecture en échec"            nz bash "$TOKEN" FAKE_GET_FAIL=$k -- test
  run "$k : réponse illisible"           nz bash "$TOKEN" FAKE_GET_GARBAGE=$k -- test
done
for k in restic_rest_password_prod restic_password_prod; do
  run "$k : lecture en échec"            nz bash "$TOKEN" "$ON" FAKE_GET_FAIL=$k -- prod
done
FORBID_CALLS='bws'
run "vault : déverrouillage en échec"    nz bash FAKE_BW_STATUS=locked FAKE_BW_UNLOCK_FAIL=1 FAKE_BW_GET_FAIL=1 -- test
FORBID_CALLS='bws'
run "vault : lecture du jeton en échec"  nz bash FAKE_BW_GET_FAIL=1 -- test
FORBID_CALLS='bws'
run "vault : jeton vide"                 nz bash FAKE_BW_GET_EMPTY=1 -- test
FORBID_CALLS='bw get|bws'
run "vault : non connecté, sans clé API" nz bash FAKE_BW_STATUS=unauthenticated -- test

# --- Secret absent, vide ou null ---------------------------------------------

for k in mqtt_password zigbee_network_key_test cloudflare_dns_api_token; do
  run "$k : absent"                      nz bash "$TOKEN" FAKE_ABSENT=$k -- test
  run "$k : value null"                  nz bash "$TOKEN" FAKE_NULL=$k -- test
  run "$k : value manquante"             nz bash "$TOKEN" FAKE_NOVALUE=$k -- test
  run "$k : vide"                        nz bash "$TOKEN" "V_$k=" -- test
  run "$k : espaces seuls"               nz bash "$TOKEN" "V_$k=   " -- test
  run "$k : texte null"                  nz bash "$TOKEN" "V_$k=null" -- test
done
for k in restic_rest_password_prod restic_password_prod; do
  run "$k : absent, sauvegarde activée"  nz bash "$TOKEN" "$ON" FAKE_ABSENT=$k -- prod
  run "$k : null, sauvegarde activée"    nz bash "$TOKEN" "$ON" FAKE_NULL=$k -- prod
  run "$k : vide, sauvegarde activée"    nz bash "$TOKEN" "$ON" "V_$k=" -- prod
done

# --- Valeur invalide ---------------------------------------------------------

K=zigbee_network_key_test
for bad in '"GENERATE"' 'GENERATE' 'SENTINEL_pas_du_json' '[1,2,3]' \
           '[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,300]' '[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,-1]' \
           '[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,1.5]' '[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,"15"]' \
           '{"SENTINEL_cle":1}' '[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15] [0]'; do
  run "clé Zigbee invalide : ${bad:0:24}" nz bash "$TOKEN" "V_$K=$bad" -- test
done
run "MQTT sur plusieurs lignes"          nz bash "$TOKEN" "V_mqtt_password=SENTINEL_a
SENTINEL_b" -- test
run "Cloudflare sur plusieurs lignes"    nz bash "$TOKEN" "V_cloudflare_dns_api_token=SENTINEL_a
SENTINEL_b" -- test
EXPECT_CALLS='haos.yml'
run "clé Zigbee sur plusieurs lignes : acceptée" 0 bash "$TOKEN" "V_$K=[0,1,2,3,4,5,6,7,
8,9,10,11,12,13,14,15]" -- test

# --- Rôles, avec un vrai ansible-playbook ------------------------------------

REAL_AP="$(PATH="$REAL_PATH" command -v ansible-playbook || true)"
if [ -z "$REAL_AP" ]; then
  SKIP=$((SKIP + 1)); echo "SKIP  rôles : ansible-playbook absent"
elif ! python3 -I -c 'import yaml' 2>/dev/null; then
  SKIP=$((SKIP + 1)); echo "SKIP  rôles : module Python yaml absent"
else
  mkdir -p "$T/real/tmp" "$T/real/dest/core/zigbee2mqtt/data"
  # Tâches réelles du rôle, sans propriétaire imposé ; rendues uniquement dans le dossier temporaire.
  python3 -I - "${ANSIBLE_DIR}/roles/compose_deploy/tasks/main.yml" "$T/real/render.yml" <<'PY'
import sys, yaml
names = {"Render .env", "Render Zigbee2MQTT configuration"}
tasks = [t for t in yaml.safe_load(open(sys.argv[1])) if t.get("name") in names]
assert len(tasks) == 2
for t in tasks:
    assert t.get("no_log") is True and t.get("diff") is False, t["name"]
    t["ansible.builtin.template"].pop("owner", None)
    t["ansible.builtin.template"].pop("group", None)
yaml.safe_dump(tasks, open(sys.argv[2], "w"))
PY
  # Protection déclarée sur les autres tâches qui manipulent ces valeurs.
  python3 -I - "${ANSIBLE_DIR}/roles" <<'PY'
import sys, yaml
def flat(tasks):
    for t in tasks:
        yield t
        yield from flat(t.get("block", []))
def load(rel):
    return {t.get("name"): t for t in flat(yaml.safe_load(open(sys.argv[1] + "/" + rel)))}
compose = load("compose_deploy/tasks/main.yml")
checks = load("compose_deploy/tasks/check_secrets.yml")
restic = load("restic_backup/tasks/main.yml")
assert checks["Check compose secrets presence"]["no_log"] is True
assert restic["Check Restic secrets presence"]["no_log"] is True
for t in (compose["Update Mosquitto password via running container"],
          restic["Write repository environment file"], restic["Write repository password file"]):
    assert t["no_log"] is True and t["diff"] is False, t["name"]
PY
  cat > "$T/real/check.yml" <<PLAY
- hosts: localhost
  connection: local
  become: false
  gather_facts: false
  vars_files:
    - ${ANSIBLE_DIR}/inventories/test/group_vars/docker_hosts.yml
  tasks:
    - ansible.builtin.import_role:
        name: compose_deploy
        tasks_from: check_secrets
    - ansible.builtin.import_tasks: $T/real/render.yml
PLAY

  : > "$T/real/ansible.cfg"
  cat > "$T/real/extra.yml" <<EXTRA
timezone: Europe/Paris
compose_projects:
  - { name: core, src: "${REPO_DIR}/services/docker/core", dest: "$T/real/dest/core" }
EXTRA

  # real <libellé> <0 | nz> [VAR=valeur ...] : --diff -vvv, aucune valeur attendue en sortie.
  real() {
    local label="$1" exp="$2" rc=0 ok=1 why=""
    shift 2
    rm -f "$T/real/dest/core/.env" "$T/real/dest/core/zigbee2mqtt/data/configuration.yaml"
    env -i HOME="$T/home" PATH="$REAL_PATH" ANSIBLE_CONFIG="$T/real/ansible.cfg" \
        ANSIBLE_ROLES_PATH="${ANSIBLE_DIR}/roles" ANSIBLE_LOCAL_TEMP="$T/real/tmp" \
        ANSIBLE_REMOTE_TEMP="$T/real/tmp" ANSIBLE_LOCALHOST_WARNING=False \
        ANSIBLE_INVENTORY_UNPARSED_WARNING=False \
        MQTT_PASSWORD="$FAKE_MQTT" CF_DNS_API_TOKEN="$FAKE_CF" Z2M_NETWORK_KEY="$FAKE_Z2M" "$@" \
        "$REAL_AP" "$T/real/check.yml" -e "@$T/real/extra.yml" --diff -vvv > "$T/out.txt" 2>&1 || rc=$?
    case "$exp" in
      0)  [ "$rc" -eq 0 ] || { ok=0; why+=" rc=$rc (0 attendu)"; }
          grep -q 'network_key: \[201, 202, 203' "$T/real/dest/core/zigbee2mqtt/data/configuration.yaml" 2>/dev/null \
            || { ok=0; why+=" clé Zigbee non rendue"; } ;;
      # Le refus doit venir du contrôle, pas d'une erreur d'exécution d'Ansible.
      nz) [ "$rc" -eq 2 ] || { ok=0; why+=" rc=$rc (2 attendu)"; }
          grep -q 'TASK \[compose_deploy : Check compose secrets presence\]' "$T/out.txt" \
            || { ok=0; why+=" contrôle non exécuté"; }
          [ ! -e "$T/real/dest/core/zigbee2mqtt/data/configuration.yaml" ] || { ok=0; why+=" rendu malgré l'échec"; }
          [ ! -e "$T/real/dest/core/.env" ] || { ok=0; why+=" .env rendu malgré l'échec"; } ;;
    esac
    if grep -Eq "$LEAK_PATTERN|GENERATE" "$T/out.txt"; then ok=0; why+=" valeur dans la sortie"; fi
    record "$ok" "rôle : $label (rc=$rc)" "$why"
  }

  real "nominal, --diff -vvv"       0
  real "second passage sans fuite"  0
  real "MQTT vide"                  nz MQTT_PASSWORD=
  real "MQTT texte null"            nz MQTT_PASSWORD=null
  real "MQTT sur plusieurs lignes"  nz "MQTT_PASSWORD=SENTINEL_a
SENTINEL_b"
  real "Cloudflare vide"            nz CF_DNS_API_TOKEN=
  real "clé Zigbee vide"            nz Z2M_NETWORK_KEY=
  real "clé Zigbee GENERATE"        nz 'Z2M_NETWORK_KEY="GENERATE"'
  real "clé Zigbee non JSON"        nz Z2M_NETWORK_KEY=SENTINEL_pas_du_json
  real "clé Zigbee trop courte"     nz 'Z2M_NETWORK_KEY=[1,2,3]'
  real "clé Zigbee hors plage"      nz 'Z2M_NETWORK_KEY=[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,300]'
  real "clé Zigbee objet"           nz 'Z2M_NETWORK_KEY={"a":1}'
fi

echo
echo "réussis : ${PASS}, en échec : ${FAIL}, ignorés : ${SKIP}"
[ "$FAIL" -eq 0 ]
