#!/usr/bin/env bash
# Tests de non-régression de configuration.yaml de Zigbee2MQTT (rôle compose_deploy).
#
# Exécute le vrai fichier de tâches (tasks/zigbee2mqtt_config.yml) et le vrai module
# (library/z2m_config.py) avec ansible-playbook sur localhost, sans élévation, dans un dossier
# temporaire. Faux docker (journal des appels, conteneur simulé, écriture concurrente simulée),
# valeurs fictives uniquement, aucun accès réseau.
#
# Usage : bash infra/ansible/tests/zigbee2mqtt-config-test.sh
#         (ansible-playbook et le module Python yaml requis ; sinon les cas sont ignorés)
# Code de sortie : 0 si tous les cas réussissent, 1 sinon.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ANSIBLE_DIR="$(cd -- "${HERE}/.." && pwd)"
REPO_DIR="$(cd -- "${ANSIBLE_DIR}/../.." && pwd)"

AP="$(command -v ansible-playbook || true)"
if [ -z "$AP" ]; then
  echo "SKIP  ansible-playbook absent"; exit 0
fi
# Interpréteur d'Ansible : il importe le module (fixtures) et sert d'interpréteur au module.
PY="$("$AP" --version | sed -n 's/^ *python version = .*(\(.*\))$/\1/p')"
"$PY" -I -c 'import yaml, ansible' 2>/dev/null || { echo "SKIP  Python d'Ansible sans yaml"; exit 0; }

T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

FAKE_MQTT=SENTINEL_MQTT_9c1e
FAKE_Z2M='[201,202,203,204,205,206,207,208,209,210,211,212,213,214,215,216]'
# Valeurs (secrets, clé, noms d'appareils) : aucune ne doit apparaître dans la sortie.
LEAK_PATTERN='SENTINEL|201, ?202|- 201|piece/appareil|0x00158d00'

DEST="$T/dest/core"
CONF="$DEST/zigbee2mqtt/data/configuration.yaml"
BACKUPS="$DEST/zigbee2mqtt/config-backups"
mkdir -p "$T/bin" "$T/tmp" "$T/home"

# Faux docker. Conteneur « en cours » si $T/running existe ; si $T/concurrent existe, l'arrêt
# simule une écriture de Zigbee2MQTT (nouveau nom) entre l'analyse et l'écriture.
cat > "$T/bin/docker" <<FAKE
#!/bin/sh
echo "\$*" >> "$T/docker.log"
case "\$1" in
  ps)    [ -e "$T/running" ] && echo 0123456789ab ; exit 0 ;;
  stop)  rm -f "$T/running"
         if [ -e "$T/concurrent" ]; then
           printf 'renamed_by_z2m: true\n' >> "$CONF"
         fi ;;
  start) touch "$T/running" ;;
esac
exit 0
FAKE
chmod 755 "$T/bin/docker"

cat > "$T/play.yml" <<PLAY
- hosts: localhost
  connection: local
  become: false
  gather_facts: false
  vars_files:
    - ${ANSIBLE_DIR}/inventories/test/group_vars/docker_hosts.yml
  tasks:
    - ansible.builtin.include_role:
        name: compose_deploy
        tasks_from: zigbee2mqtt_config
      vars:
        z2m_project: { name: core, src: "${REPO_DIR}/services/docker/core", dest: "${DEST}" }
PLAY
: > "$T/ansible.cfg"
# Valeurs du fragment alignées sur les fixtures (test_z2m_config.py).
cat > "$T/extra.yml" <<EXTRA
smlight_serial_url: tcp://smlight.example.invalid:6638
smlight_adapter: ember
z2m_config_owner: "$(id -u)"
z2m_config_group: "$(id -g)"
ansible_python_interpreter: "$PY"
EXTRA

# Fixtures : même générateur que les tests unitaires.
fixture() { # fixture <fichier> [arguments Python de existing()]
  "$PY" -I - "$HERE" "$1" "${2:-}" <<'PYF'
import sys
sys.path.insert(0, sys.argv[1])
import test_z2m_config as t
kwargs = eval("dict(%s)" % sys.argv[3])
open(sys.argv[2], "wb").write(t.existing(**kwargs))
PYF
}

# Empreinte, inode, date et droits : une réécriture à l'identique change aussi l'inode.
snapshot() {
  "$PY" -I -c '
import hashlib, os, sys
st = os.stat(sys.argv[1])
print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest(), st.st_ino, st.st_mtime_ns, oct(st.st_mode & 0o777))
' "$CONF"
}

PASS=0
FAIL=0
record() { # record <ok: 1|0> <libellé> <raison>
  if [ "$1" -eq 1 ]; then
    PASS=$((PASS + 1)); printf 'PASS  %s\n' "$2"
  else
    FAIL=$((FAIL + 1)); printf 'FAIL  %s:%s\n' "$2" "$3"
  fi
}

# deploy [options d'ansible-playbook] : --diff -vvv ; rc dans $RC, sortie dans $T/out.txt.
deploy() {
  : > "$T/docker.log"
  RC=0
  env -i HOME="$T/home" PATH="$T/bin:$(dirname -- "$AP"):/usr/bin:/bin" ANSIBLE_CONFIG="$T/ansible.cfg" \
      ANSIBLE_ROLES_PATH="${ANSIBLE_DIR}/roles" ANSIBLE_LOCAL_TEMP="$T/tmp" \
      ANSIBLE_REMOTE_TEMP="$T/tmp" ANSIBLE_LOCALHOST_WARNING=False \
      ANSIBLE_INVENTORY_UNPARSED_WARNING=False \
      MQTT_PASSWORD="$FAKE_MQTT" Z2M_NETWORK_KEY="$FAKE_Z2M" \
      "$AP" "$T/play.yml" -e "@$T/extra.yml" --diff -vvv "$@" > "$T/out.txt" 2>&1 || RC=$?
}

# check <libellé> <rc attendu> [contrôles...] ; contrôles :
#   same=<snapshot>  fichier strictement inchangé      docker=<liste>  appels docker exacts
#   says=<regex>     présent dans la sortie            nochange        aucune tâche « changed »
#   py=<expression>  assertion Python sur le fichier (variables : doc, before, t, z2m, mode)
check() {
  local label="$1" exp="$2" ok=1 why=""
  shift 2
  [ "$RC" -eq "$exp" ] || { ok=0; why+=" rc=$RC ($exp attendu)"; }
  if grep -Eq "$LEAK_PATTERN" "$T/out.txt"; then ok=0; why+=" valeur dans la sortie"; fi
  local c
  for c in "$@"; do
    case "$c" in
      same=*)   [ "$(snapshot)" = "${c#same=}" ] || { ok=0; why+=" fichier modifié"; } ;;
      docker=*) [ "$(tr '\n' ';' < "$T/docker.log")" = "${c#docker=}" ] \
                  || { ok=0; why+=" docker : $(tr '\n' ';' < "$T/docker.log") (${c#docker=} attendu)"; } ;;
      says=*)   grep -Eq "${c#says=}" "$T/out.txt" || { ok=0; why+=" sortie sans « ${c#says=} »"; } ;;
      nochange) grep -Eq 'localhost +: ok=[0-9]+ +changed=0 ' "$T/out.txt" || { ok=0; why+=" tâche « changed »"; } ;;
      py=*)     "$PY" -I - "$HERE" "$CONF" "${BEFORE:-}" "${c#py=}" <<'PYC' || { ok=0; why+=" assertion : ${c#py=}"; }
import os, sys
sys.path.insert(0, sys.argv[1])
import test_z2m_config as t
z2m = t.z2m
doc = z2m.load(open(sys.argv[2], encoding="utf-8").read(), "t")
before = z2m.load(open(sys.argv[3], encoding="utf-8").read(), "t") if sys.argv[3] else None
mode = os.stat(sys.argv[2]).st_mode & 0o777
assert eval(sys.argv[4]), sys.argv[4]
PYC
                ;;
    esac
  done
  record "$ok" "$label (rc=$RC)" "$why"
}

reset() { rm -rf "$T/dest" "$T/running" "$T/concurrent"; mkdir -p "$(dirname -- "$CONF")"; }

# --- Création ------------------------------------------------------------------

reset; rm -rf "$DEST"
deploy
check "premier déploiement, fichier et dossier absents : création" 0 \
  "py=doc['advanced']['network_key'] == t.KEY and doc['version'] == 5 and mode == 0o640" \
  "py=doc['mqtt']['password'] == 'SENTINEL_MQTT_9c1e'" "docker=ps --quiet --filter name=^zigbee2mqtt$;" \
  "says=création du fichier"
[ ! -e "$BACKUPS" ] && record 1 "création : aucune copie de retour arrière" "" \
  || record 0 "création : aucune copie de retour arrière" " dossier présent"

# --- Fichier existant conforme : aucune écriture ---------------------------------

reset; fixture "$CONF"; chmod 0640 "$CONF"; touch "$T/running"
S0="$(snapshot)"
deploy
check "39 noms, groupes, clés inconnues, conforme : rien d'écrit" 0 "same=$S0" "docker=" nochange
for i in 1 2 3; do
  deploy
  check "déploiement répété n°$i : rien d'écrit" 0 "same=$S0" "docker=" nochange
done

"$PY" -I - "$HERE" "$CONF" <<'PYR'
import sys
sys.path.insert(0, sys.argv[1])
import test_z2m_config as t
doc = t.z2m.load(open(sys.argv[2], encoding="utf-8").read(), "t")
text = "# reformaté\n" + t.yaml.dump(doc, Dumper=t.z2m.Dumper, default_flow_style=True, indent=4,
                                     sort_keys=False, allow_unicode=True)
open(sys.argv[2], "w", encoding="utf-8").write(text)
PYR
S1="$(snapshot)"
deploy
check "formatage seul différent (style, commentaire) : rien d'écrit" 0 "same=$S1" "docker=" nochange

reset; fixture "$CONF" "version=6"; chmod 0640 "$CONF"
S0="$(snapshot)"
deploy
check "version 6 sur l'hôte, 5 dans le modèle : pas de rétrogradation" 0 "same=$S0" "docker=" nochange

# --- Propriétaire et permissions ----------------------------------------------------

reset; fixture "$CONF"; chmod 0644 "$CONF"
S0="$(snapshot)"
deploy
check "permissions 0644 corrigées sans réécrire le contenu" 0 "docker=" \
  "py=mode == 0o640" "same=${S0% *} 0o640"

# --- Paramètre géré modifié ------------------------------------------------------------

reset; fixture "$CONF" "transmit_power=10"; chmod 0640 "$CONF"; touch "$T/running"
S0="$(snapshot)"
deploy
check "clé gérée modifiée, conteneur actif, redémarrage non autorisé : refus" 2 "same=$S0" \
  "docker=ps --quiet --filter name=^zigbee2mqtt$;" "says=advanced\\.transmit_power" \
  "says=z2m_restart_allowed=true"
[ ! -e "$BACKUPS" ] && record 1 "refus : aucune copie créée" "" || record 0 "refus : aucune copie créée" " dossier présent"

deploy --check
check "même cas en --check : aucune écriture, aucun arrêt" 0 "same=$S0" \
  "docker=ps --quiet --filter name=^zigbee2mqtt$;" "says=advanced\\.transmit_power"

cp -p "$CONF" "$T/before.yaml"
deploy -e z2m_restart_allowed=true
BEFORE="$T/before.yaml" check "redémarrage autorisé : écriture entre arrêt et redémarrage" 0 \
  "docker=ps --quiet --filter name=^zigbee2mqtt$;stop zigbee2mqtt;start zigbee2mqtt;" \
  "py=doc['advanced']['transmit_power'] == 20 and mode == 0o640" \
  "py=z2m.same(t.without(doc, t.MANAGED_LEAVES), t.without(before, t.MANAGED_LEAVES))" \
  "py=z2m.same(t.without(doc, ['advanced.transmit_power']), t.without(before, ['advanced.transmit_power']))" \
  "py=len(doc['devices']) == 39 and len(doc['groups']) == 2 and doc['version'] == 5" \
  "py=[d['friendly_name'] for d in doc['devices'].values()][:len(t.TRICKY)] == t.TRICKY" \
  "says=advanced\\.transmit_power"
BK="$(ls "$BACKUPS" 2>/dev/null | head -1)"
if [ -n "$BK" ] && cmp -s "$BACKUPS/$BK" "$T/before.yaml" \
   && [ "$(stat -c %a "$BACKUPS")" = 700 ] && [ "$(stat -c %a "$BACKUPS/$BK")" = 600 ]; then
  record 1 "copie de retour arrière identique à l'ancien fichier (0600, dossier 0700)" ""
else
  record 0 "copie de retour arrière identique à l'ancien fichier (0600, dossier 0700)" " ${BK:-absente}"
fi

S0="$(snapshot)"
deploy -e z2m_restart_allowed=true
check "redéploiement suivant : rien d'écrit, aucun redémarrage" 0 "same=$S0" "docker=" nochange

# Procédure de retour arrière documentée (HOWTO), sans -o/-g faute de droits root ici.
cp -p "$CONF" "$T/merged.yaml"
PATH="$T/bin:$PATH" docker stop zigbee2mqtt >/dev/null
install -m 0640 "$BACKUPS/$BK" "$CONF"
PATH="$T/bin:$PATH" docker start zigbee2mqtt >/dev/null
if cmp -s "$CONF" "$T/before.yaml" && [ "$(stat -c %a "$CONF")" = 640 ] && [ -e "$T/running" ]; then
  record 1 "retour arrière : fichier précédent restauré à l'octet près, conteneur relancé" ""
else
  record 0 "retour arrière : fichier précédent restauré à l'octet près, conteneur relancé" ""
fi

reset; fixture "$CONF" "transmit_power=10"; chmod 0640 "$CONF"
deploy
check "clé gérée modifiée, conteneur arrêté : écriture sans redémarrage" 0 \
  "docker=ps --quiet --filter name=^zigbee2mqtt$;" "py=doc['advanced']['transmit_power'] == 20"

# --- Écriture concurrente ----------------------------------------------------------------

reset; fixture "$CONF" "transmit_power=10"; chmod 0640 "$CONF"; touch "$T/running" "$T/concurrent"
deploy -e z2m_restart_allowed=true
check "écriture de Zigbee2MQTT entre analyse et écriture : refus, conteneur relancé" 2 \
  "docker=ps --quiet --filter name=^zigbee2mqtt$;stop zigbee2mqtt;start zigbee2mqtt;" \
  "py=doc['renamed_by_z2m'] is True and doc['advanced']['transmit_power'] == 10" \
  "says=modifié depuis l'analyse"
[ ! -e "$BACKUPS" ] && record 1 "concurrence : aucune copie, rien d'écrit" "" \
  || record 0 "concurrence : aucune copie, rien d'écrit" " dossier présent"

# --- Rotation des copies ---------------------------------------------------------------------

reset; fixture "$CONF" "transmit_power=10"; chmod 0640 "$CONF"
for p in 11 12 13 14 15; do
  sed -i "s/^  transmit_power: .*/  transmit_power: $p/" "$CONF"
  deploy -e z2m_config_backups_keep=3
done
N="$(ls "$BACKUPS" | wc -l)"
LAST="$(ls "$BACKUPS" | tail -1)"
if [ "$N" -eq 3 ] && grep -q '^  transmit_power: 15$' "$BACKUPS/$LAST"; then
  record 1 "5 écritures, 3 copies conservées, la plus récente en dernier" ""
else
  record 0 "5 écritures, 3 copies conservées, la plus récente en dernier" " $N copies"
fi

# --- Refus sans modification ------------------------------------------------------------------

refuse() { # refuse <libellé> <message attendu>
  chmod 0640 "$CONF"; touch "$T/running"
  local s; s="$(snapshot)"
  deploy -e z2m_restart_allowed=true
  check "$1 : refus sans modification" 2 "same=$s" "docker=" "says=$2"
}
reset; fixture "$CONF" "key=list(range(16))"
refuse "clé réseau différente de Bitwarden" "différente de celle de Bitwarden"
reset; fixture "$CONF"
"$PY" -I -c 'import re,sys; p=sys.argv[1]; d=open(p).read(); open(p,"w").write(re.sub(r"  network_key:\n(    - \d+\n){16}", "", d))' "$CONF"
refuse "clé réseau absente" "absente"
reset; fixture "$CONF"
"$PY" -I -c 'import re,sys; p=sys.argv[1]; d=open(p).read(); open(p,"w").write(re.sub(r"  network_key:\n(    - \d+\n){16}", "  network_key: GENERATE\n", d))' "$CONF"
refuse "clé réseau GENERATE" "différente de celle de Bitwarden"
reset; fixture "$CONF"; printf 'mqtt: [SENTINEL_BROKEN: {\n' >> "$CONF"
refuse "YAML invalide (secret dans la ligne fautive)" "YAML invalide \\(ligne"
reset; : > "$CONF"
refuse "fichier vide" "vide ou n'est pas un dictionnaire"

# --- Protection déclarée -------------------------------------------------------------------------

"$PY" -I - "${ANSIBLE_DIR}/roles/compose_deploy" <<'PYS' && record 1 "fragment géré déclaré no_log, diff désactivé" "" \
  || record 0 "fragment géré déclaré no_log, diff désactivé" ""
import ast, sys, yaml
root = sys.argv[1]
src = open(root + "/library/z2m_config.py").read()
spec = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.keyword) and n.arg == "managed"]
assert any("no_log" in ast.dump(n.value) and "True" in ast.dump(n.value) for n in spec)
def flat(tasks):
    for t in tasks:
        yield t
        yield from flat(t.get("block", []) + t.get("always", []))
for t in flat(yaml.safe_load(open(root + "/tasks/zigbee2mqtt_config.yml"))):
    if "z2m_config" in t:
        assert t.get("diff") is False, t["name"]
PYS

echo
echo "réussis : ${PASS}, en échec : ${FAIL}"
[ "$FAIL" -eq 0 ]
