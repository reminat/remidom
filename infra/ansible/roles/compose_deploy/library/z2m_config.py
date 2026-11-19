#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Fusion ciblée de configuration.yaml de Zigbee2MQTT (rôle compose_deploy).

Zigbee2MQTT écrit lui-même dans ce fichier (devices, groups, réglages faits dans l'interface,
version du schéma). Ansible n'y impose que les clés du fragment géré, sans réécrire le reste :

- fichier absent : le fragment rendu est écrit tel quel (création) ;
- fichier présent : les clés gérées sont fusionnées, sauf `version` (Zigbee2MQTT fait migrer
  le schéma) et `advanced.network_key`, jamais écrite : elle doit déjà être égale à celle du
  fragment, sinon arrêt sans modification ;
- écriture seulement si le contenu interprété change ; le formatage seul ne compte pas.

Lecture et écriture suivent YAML 1.2, comme Zigbee2MQTT (js-yaml) : « on » ou « yes » restent
des chaînes. Toute chaîne ambiguë en YAML 1.1 ou 1.2 est écrite entre guillemets.

Aucune valeur du fichier ni du fragment n'apparaît dans le résultat ou les erreurs : seulement
des chemins de clés, des empreintes et des positions (ligne, colonne).
"""

import datetime
import hashlib
import os
import re
import tempfile

from ansible.module_utils.basic import AnsibleModule

try:
    import yaml
    HAS_YAML = True
except ImportError:  # pragma: no cover - dépend de l'hôte
    HAS_YAML = False

# --- YAML ---------------------------------------------------------------------

BOOL, INT, FLOAT = ("tag:yaml.org,2002:" + t for t in ("bool", "int", "float"))

# Schéma par défaut de js-yaml 4 (lu par Zigbee2MQTT) pour les booléens et les nombres.
JS_BOOL = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
JS_INT = re.compile(r"^(?:[-+]?0b[01_]+|[-+]?0o[0-7_]+|[-+]?0x[0-9a-fA-F_]+|[-+]?[0-9][0-9_]*)$")
JS_FLOAT = re.compile(
    r"^(?:[-+]?(?:[0-9][0-9_]*)(?:\.[0-9_]*)?(?:[eE][-+]?[0-9]+)?"
    r"|\.[0-9_]+(?:[eE][-+]?[0-9]+)?|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
)
FIRST = {BOOL: list("tTfF"), INT: list("-+0123456789"), FLOAT: list("-+0123456789.")}
PATTERNS = {BOOL: JS_BOOL, INT: JS_INT, FLOAT: JS_FLOAT}


if HAS_YAML:
    class Loader(yaml.SafeLoader):
        """SafeLoader dont booléens et nombres suivent js-yaml (YAML 1.2), pas YAML 1.1."""

    Loader.yaml_implicit_resolvers = {
        k: [r for r in v if r[0] not in (BOOL, INT, FLOAT)]
        for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    for _tag in (BOOL, INT, FLOAT):
        Loader.add_implicit_resolver(_tag, PATTERNS[_tag], FIRST[_tag])

    def _construct_int(loader, node):
        value = loader.construct_scalar(node).replace("_", "")
        sign = -1 if value.startswith("-") else 1
        value = value.lstrip("+-")
        base = {"0b": 2, "0o": 8, "0x": 16}.get(value[:2].lower(), 10)
        return sign * int(value[2:] if base != 10 else value, base)

    def _construct_float(loader, node):
        value = loader.construct_scalar(node).replace("_", "").lower()
        if value.endswith(".inf"):
            return float("-inf") if value.startswith("-") else float("inf")
        if value == ".nan":
            return float("nan")
        return float(value)

    Loader.add_constructor(INT, _construct_int)
    Loader.add_constructor(FLOAT, _construct_float)

    class Dumper(yaml.SafeDumper):
        """Met entre guillemets toute chaîne qu'un lecteur YAML 1.1 ou 1.2 ne lirait pas comme
        une chaîne (adresses IEEE « 0x… », « on », « 0o17 », etc.)."""

        def ignore_aliases(self, data):
            return True

    Dumper.yaml_implicit_resolvers = {
        k: list(v) for k, v in yaml.SafeDumper.yaml_implicit_resolvers.items()
    }
    for _tag in (BOOL, INT, FLOAT):
        Dumper.add_implicit_resolver(_tag, PATTERNS[_tag], FIRST[_tag])


class ConfigError(Exception):
    """Erreur sans valeur : le message ne cite que des chemins ou des positions."""


def load(text, label):
    try:
        return yaml.load(text, Loader=Loader)  # nosec B506 : Loader dérive de SafeLoader
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        where = " (ligne %d, colonne %d)" % (mark.line + 1, mark.column + 1) if mark else ""
        raise ConfigError("%s : YAML invalide%s" % (label, where))
    except yaml.YAMLError:
        raise ConfigError("%s : YAML invalide" % label)


def dump(data):
    return yaml.dump(data, Dumper=Dumper, default_flow_style=False, sort_keys=False,
                     allow_unicode=True, indent=2, width=4096)


def same(a, b):
    """Égalité stricte : 1 et True, ou 1 et 1.0, sont différents."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return list(a) == list(b) and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, float) and a != a:
        return b != b
    return a == b


# --- Fusion -------------------------------------------------------------------

def merge(current, managed, prefix=""):
    """Copie de `current` où chaque feuille de `managed` est imposée.
    Renvoie (résultat, chemins des feuilles réellement modifiées)."""
    result = dict(current)
    changed = []
    for key, value in managed.items():
        path = "%s.%s" % (prefix, key) if prefix else str(key)
        old = current.get(key)
        if isinstance(value, dict) and isinstance(old, dict):
            result[key], sub = merge(old, value, path)
            changed.extend(sub)
        else:
            if key not in current or not same(old, value):
                changed.append(path)
            result[key] = value
    return result, changed


def leaves(data, prefix=""):
    out = []
    for key, value in data.items():
        path = "%s.%s" % (prefix, key) if prefix else str(key)
        out.extend(leaves(value, path) if isinstance(value, dict) and value else [path])
    return out


def plan(current_bytes, managed_text):
    """Calcule le contenu cible. Renvoie un dict : content (bytes ou None si inchangé),
    created, changed_paths. Lève ConfigError sans valeur dans le message."""
    managed = load(managed_text, "fragment géré")
    if not isinstance(managed, dict):
        raise ConfigError("fragment géré : un dictionnaire est attendu")
    advanced = managed.get("advanced")
    key = advanced.get("network_key") if isinstance(advanced, dict) else None
    if not (isinstance(key, list) and len(key) == 16
            and all(type(b) is int and 0 <= b <= 255 for b in key)):
        raise ConfigError("fragment géré : advanced.network_key doit être un tableau de 16 octets")

    if current_bytes is None:
        text = managed_text if managed_text.endswith("\n") else managed_text + "\n"
        check(text, managed)
        return {"content": text.encode("utf-8"), "created": True, "changed_paths": leaves(managed)}

    try:
        current_text = current_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError("configuration existante : encodage UTF-8 invalide")
    current = load(current_text, "configuration existante")
    if not isinstance(current, dict):
        raise ConfigError("configuration existante : vide ou n'est pas un dictionnaire")
    cur_adv = current.get("advanced")
    cur_key = cur_adv.get("network_key") if isinstance(cur_adv, dict) else None
    if cur_key is None:
        raise ConfigError("clé réseau Zigbee absente de la configuration existante : "
                          "arrêt sans modification (jamais générée ni remplacée automatiquement)")
    if not same(cur_key, key):
        raise ConfigError("clé réseau Zigbee existante différente de celle de Bitwarden : "
                          "arrêt sans modification (jamais remplacée automatiquement)")

    # Jamais imposées sur un fichier existant : version (pas de rétrogradation du schéma) et
    # clé réseau (vérifiée ci-dessus).
    applied = {k: v for k, v in managed.items() if k != "version"}
    applied["advanced"] = {k: v for k, v in advanced.items() if k != "network_key"}
    target, changed = merge(current, applied)
    if same(target, current):
        return {"content": None, "created": False, "changed_paths": []}
    text = dump(target)
    check(text, target)
    return {"content": text.encode("utf-8"), "created": False, "changed_paths": changed}


def check(text, expected):
    """Le texte écrit doit se relire à l'identique, selon js-yaml comme selon YAML 1.1."""
    if not same(load(text, "contenu cible"), expected):
        raise ConfigError("contenu cible : relecture YAML 1.2 différente, rien n'est écrit")
    try:
        legacy = yaml.safe_load(text)
    except yaml.YAMLError:
        legacy = None
    if not same(legacy, expected):
        raise ConfigError("contenu cible : relecture YAML 1.1 différente, rien n'est écrit")


# --- Fichiers -------------------------------------------------------------------

def sha256(data):
    return hashlib.sha256(data).hexdigest() if data is not None else "absent"


def read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def backup(path, data, backup_dir, keep):
    """Copie 0600 dans un dossier 0700 ; seules les `keep` plus récentes sont conservées."""
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    base = os.path.basename(path)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest = os.path.join(backup_dir, "%s.%s" % (base, stamp))
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    pattern = re.compile(r"^%s\.\d{8}T\d{12}Z$" % re.escape(base))
    copies = sorted(n for n in os.listdir(backup_dir) if pattern.match(n))
    for name in copies[:-keep]:
        os.unlink(os.path.join(backup_dir, name))
    return dest


def main():
    module = AnsibleModule(
        argument_spec=dict(
            path=dict(type="path", required=True),
            managed=dict(type="str", required=True, no_log=True),
            backup_dir=dict(type="path", required=True),
            backups_keep=dict(type="int", default=5),
            expected_sha256=dict(type="str"),
        ),
        add_file_common_args=True,
        supports_check_mode=True,
    )
    if not HAS_YAML:
        module.fail_json(msg="module Python yaml absent sur l'hôte (paquet python3-yaml)")
    p = module.params
    path = p["path"]
    if p["backups_keep"] < 1:
        module.fail_json(msg="backups_keep doit valoir au moins 1")

    data = read(path)
    digest = sha256(data)
    result = dict(changed=False, content_changed=False, created=False, changed_paths=[],
                  sha256=digest, backup_file=None)

    if p["expected_sha256"] and p["expected_sha256"] != digest:
        module.fail_json(msg="configuration.yaml modifié depuis l'analyse (écriture concurrente "
                             "de Zigbee2MQTT ?) : rien n'est écrit, relancer le déploiement",
                         **result)
    try:
        target = plan(data, p["managed"])
    except ConfigError as exc:
        module.fail_json(msg=str(exc), **result)

    result["changed_paths"] = target["changed_paths"]
    result["created"] = target["created"]
    file_args = module.load_file_common_arguments(module.params, path=path)

    if target["content"] is not None:
        result.update(changed=True, content_changed=True)
        if module.check_mode:
            module.exit_json(**result)
        directory = os.path.dirname(path) or "."
        if data is None:
            os.makedirs(directory, mode=0o755, exist_ok=True)  # premier déploiement
        else:
            result["backup_file"] = backup(path, data, p["backup_dir"], p["backups_keep"])
        fd, tmp = tempfile.mkstemp(prefix=".configuration.yaml.", dir=directory)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(target["content"])
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            module.set_fs_attributes_if_different(
                module.load_file_common_arguments(module.params, path=tmp), False)
            # Dernier contrôle juste avant le remplacement atomique.
            if sha256(read(path)) != digest:
                module.fail_json(msg="configuration.yaml modifié pendant l'écriture : "
                                     "rien n'est remplacé, relancer le déploiement", **result)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        module.exit_json(**result)

    if data is not None:
        result["changed"] = module.set_fs_attributes_if_different(file_args, False)
    module.exit_json(**result)


if __name__ == "__main__":
    main()
