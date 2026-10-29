#!/usr/bin/python3
"""Maintenance Restic locale sur le NAS.

Exécuté en root par une tâche planifiée DSM. Restic tourne dans un conteneur
Docker (image épinglée), sans réseau. Le mot de passe est monté comme fichier :
ce script ne le lit jamais.

Sans option, contrôle en lecture seule (forget --dry-run). Avec --apply forget|prune,
le même contrôle, puis, s'il est entièrement propre et si l'opération est activée dans
la configuration, l'opération réelle, seule à monter le repository en écriture.

Compatible Python 3.8 (version fournie par DSM 7). À lancer avec « python3 -I ».
"""

import sys

# Garde-fou placé avant tout autre import : sans -I (mode isolé), Python chercherait
# d'abord les modules dans le dossier du script ; un json.py déposé à côté serait
# alors exécuté en root. « sys » est intégré à l'interpréteur et ne peut être substitué.
if __name__ == "__main__" and not sys.flags.isolated:
    sys.stderr.write("refusé : lancer ce script avec « python3 -I » (mode isolé)\n")
    sys.exit(2)

import argparse  # noqa: E402
import datetime as dt  # noqa: E402
import fcntl  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402

ALLOWED_MODES = ("dry-run",)
DOCKER_BIN = "/usr/local/bin/docker"
CONTAINER_REPO = "/repo"
CONTAINER_PASSWORD = "/run/secrets/restic_password"

# Sous-commandes restic autorisées en lecture seule et jetons interdits, quelles que soient les options.
ALLOWED_SUBCOMMANDS = ("version", "snapshots", "forget", "check", "list", "prune")
# Options de « check » autorisées en plus de --no-lock / --no-cache : lecture des données.
CHECK_OPTION_RE = re.compile(r"^--read-data(-subset=[0-9]{1,3}/[0-9]{1,3})?$")
# Lecture des données : jamais si absente de la configuration.
NO_READ_DATA = {"weekdays": [], "parts": 1}
FORBIDDEN_TOKENS = ("--prune", "unlock", "repair", "rebuild-index", "key", "init", "migrate", "tag",
                    "rewrite", "backup", "copy", "--remove-all", "--unsafe-allow-remove-all",
                    "--unsafe-recover-no-free-space", "--repack-uncompressed", "--retry-lock")
SNAPSHOT_ID_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_UNUSED_RE = re.compile(r"^([0-9]|[1-4][0-9]|50)%$")
# Opérations réelles : jamais si absentes de la configuration.
NO_APPLY = {"forget": False, "prune": False, "prune_max_unused": "5%", "repository_owner": "0:0",
            "protected": {"start": "04:55", "end": "08:00"}}
# Durée minimale restant avant la plage protégée pour commencer une opération réelle.
MIN_APPLY_SECONDS = 10 * 60
# Entrées du repository dont le propriétaire est vérifié avant toute écriture.
REPO_ENTRIES = ("config", "data", "index", "keys", "locks", "snapshots")
# Code de sortie de restic quand il ne peut pas verrouiller le repository (rien n'a été modifié).
RESTIC_EXIT_LOCKED = 11
# Arrêt d'une opération réelle au délai : SIGTERM (restic libère son verrou), puis SIGKILL.
STOP_GRACE_SECONDS = 60
# Délais des commandes d'arrêt elles-mêmes (le démon Docker peut ne plus répondre).
STOP_CALL_TIMEOUT = STOP_GRACE_SECONDS + 30
KILL_CALL_TIMEOUT = 30
INSPECT_TIMEOUT = 15

EXIT_OK = 0
EXIT_ANOMALY = 1
EXIT_CONFIG = 2
EXIT_WINDOW = 3
EXIT_RESTIC = 4
EXIT_TIMEOUT = 5
EXIT_BASELINE_REQUIRED = 6
EXIT_BASELINE_EXISTS = 7
EXIT_LOCKED = 75

REQUIRED_KEYS = ("name", "mode", "image", "expected_restic_version", "repository", "password_file",
                 "private_dir", "groups", "retention", "window", "timeout_minutes", "thresholds")
THRESHOLD_KEYS = ("max_future_skew_minutes", "max_age_hours", "max_snapshots_per_24h", "backdate_tolerance_hours",
                  "max_remove_count", "max_remove_percent", "min_keep_per_group")

# Stockage privé (état, rapports, verrou) : propriétaires admis pour chaque niveau du chemin.
TRUSTED_UIDS = (0,)
MAX_STATE_BYTES = 1024 * 1024
MAX_STATE_IDS = 100000
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# Volumes de données DSM (ACL Synology) : repository et secret y sont, le stockage privé jamais.
DATA_VOLUME_RE = re.compile(r"^/volume[0-9]+/")


class ConfigError(Exception):
    pass


class StepError(Exception):
    pass


class StepTimeout(Exception):
    """Délai dépassé. stop : résultat de l'arrêt du conteneur (None si aucun conteneur n'était lancé)."""

    def __init__(self, message, stop=None):
        Exception.__init__(self, message)
        self.stop = stop


class UnsafeMounts(Exception):
    pass


# --------------------------------------------------------------------------- config

def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        raise ConfigError("clés manquantes dans la configuration : %s" % ", ".join(missing))
    if cfg["mode"] not in ALLOWED_MODES:
        raise ConfigError("mode %r refusé : seul 'dry-run' est autorisé" % cfg["mode"])
    if not isinstance(cfg["name"], str) or not re.match(r"^[a-z0-9][a-z0-9-]{0,62}$", cfg["name"]):
        raise ConfigError("name invalide : %r" % cfg["name"])
    validate_path("repository", cfg["repository"], on_volume=True)
    validate_path("password_file", cfg["password_file"], on_volume=True)
    validate_path("private_dir", cfg["private_dir"], on_volume=False)
    validate_retention(cfg["retention"])
    validate_thresholds(cfg["thresholds"])
    if not is_int(cfg["timeout_minutes"], 1, 1440):
        raise ConfigError("timeout_minutes invalide : %r" % cfg["timeout_minutes"])
    if not is_int(cfg.get("keep_reports", 90), 1, 10000):
        raise ConfigError("keep_reports invalide : %r" % cfg.get("keep_reports"))
    if not isinstance(cfg.get("run_check", True), bool):
        raise ConfigError("run_check doit être un booléen")
    validate_read_data(cfg.get("read_data", NO_READ_DATA))
    if cfg.get("read_data", NO_READ_DATA)["weekdays"] and not cfg.get("run_check", True):
        raise ConfigError("read_data suppose run_check : la lecture des données se fait pendant check")
    validate_apply(cfg.get("apply", NO_APPLY))
    if any(cfg.get("apply", NO_APPLY)[op] for op in ("forget", "prune")) and not cfg.get("run_check", True):
        raise ConfigError("apply suppose run_check : aucune opération réelle sans contrôle préalable")
    validate_groups(cfg["groups"])
    image = cfg["image"]
    if not re.match(r"^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?(@sha256:[0-9a-f]{64})?$", image) \
            or not ("@sha256:" in image or re.search(r":[A-Za-z0-9._-]+$", image)):
        raise ConfigError("image invalide : %r (attendu nom:tag ou nom@sha256:…)" % image)
    if not re.match(r"^\d+\.\d+\.\d+$", cfg["expected_restic_version"]):
        raise ConfigError("expected_restic_version invalide : %r" % cfg["expected_restic_version"])
    parse_window(cfg["window"])
    return cfg


def is_int(value, low, high):
    """Entier véritable (pas un booléen, pas une chaîne) compris entre low et high."""
    return type(value) is int and low <= value <= high


def validate_path(key, value, on_volume):
    """Chemin absolu, composants simples : ni « . », « .. », espace, « : » ou « , »."""
    if not isinstance(value, str) or not re.match(r"^(/[A-Za-z0-9._-]+)+$", value) \
            or any(part in (".", "..") for part in value.split("/")[1:]):
        raise ConfigError("%s invalide : %r" % (key, value))
    volume = DATA_VOLUME_RE.match(value) is not None
    if on_volume and not volume:
        raise ConfigError("%s doit être sous /volumeN/ : %r" % (key, value))
    if not on_volume and (volume or value.startswith("/volume")):
        raise ConfigError("%s ne doit pas être sur un volume de données (ACL Synology) : %r" % (key, value))


TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def validate_retention(retention):
    expected = {"keep_within", "keep_daily", "keep_weekly", "keep_monthly"}
    if not isinstance(retention, dict) or not expected <= set(retention) <= expected | {"keep_tags"}:
        raise ConfigError("retention doit contenir exactement %s (et, facultatif, keep_tags)" % ", ".join(sorted(expected)))
    tags = retention.get("keep_tags", [])
    if not isinstance(tags, list) or len(tags) > 10 or len(set(tags)) != len(tags) \
            or any(not isinstance(t, str) or not TAG_RE.match(t) for t in tags):
        raise ConfigError("keep_tags invalide : %r (au plus 10 tags distincts, [a-z0-9_-])" % (tags,))
    within = retention["keep_within"]
    try:
        parse_duration(within)
    except ValueError:
        raise ConfigError("keep_within invalide : %r (ex. 30d, 1y6m ; chaque unité au plus une fois)" % (within,))
    for key in ("keep_daily", "keep_weekly", "keep_monthly"):
        if not is_int(retention[key], 0, 10000):
            raise ConfigError("%s invalide : %r (entier entre 0 et 10000)" % (key, retention[key]))


def validate_read_data(read_data):
    """weekdays : jours ISO (1 = lundi … 7 = dimanche) ; parts : 1 = lecture intégrale,
    t > 1 = un groupe de packs sur t par exécution, en rotation (couverture complète en t semaines)."""
    if not isinstance(read_data, dict) or set(read_data) != {"weekdays", "parts"}:
        raise ConfigError("read_data doit contenir exactement weekdays et parts")
    days = read_data["weekdays"]
    if not isinstance(days, list) or any(not is_int(d, 1, 7) for d in days) or len(set(days)) != len(days):
        raise ConfigError("read_data.weekdays invalide : %r (jours ISO 1 à 7, sans doublon)" % (days,))
    if not is_int(read_data["parts"], 1, 52):
        raise ConfigError("read_data.parts invalide : %r (entier entre 1 et 52)" % (read_data["parts"],))


def read_data_args(read_data, today, force=False):
    """Options de « restic check » pour la lecture des données à la date « today » (date locale).
    [] = contrôle de structure seulement. Rotation sur un compteur de semaines continu
    (lundi à dimanche), sans rupture au changement d'année."""
    if force:
        return ["--read-data"]
    if today.isoweekday() not in read_data["weekdays"]:
        return []
    parts = read_data["parts"]
    if parts == 1:
        return ["--read-data"]
    week = (today.toordinal() - 1) // 7
    return ["--read-data-subset=%d/%d" % (week % parts + 1, parts)]


def validate_apply(apply):
    """Opérations réelles : forget / prune activés séparément ; propriétaire attendu du repository."""
    if not isinstance(apply, dict) or set(apply) != set(NO_APPLY):
        raise ConfigError("apply doit contenir exactement %s" % ", ".join(sorted(NO_APPLY)))
    for op in ("forget", "prune"):
        if not isinstance(apply[op], bool):
            raise ConfigError("apply.%s doit être un booléen" % op)
    if not isinstance(apply["prune_max_unused"], str) or not MAX_UNUSED_RE.match(apply["prune_max_unused"]):
        raise ConfigError("apply.prune_max_unused invalide : %r (pourcentage entier de 0%% à 50%%)"
                          % (apply["prune_max_unused"],))
    if not isinstance(apply["repository_owner"], str) or not re.match(r"^[0-9]{1,10}:[0-9]{1,10}$",
                                                                     apply["repository_owner"]):
        raise ConfigError("apply.repository_owner invalide : %r (uid:gid)" % (apply["repository_owner"],))
    if not isinstance(apply["protected"], dict) or set(apply["protected"]) != {"start", "end"}:
        raise ConfigError("apply.protected doit contenir exactement start et end")
    parse_window(apply["protected"])


def seconds_until(hhmm, now_ts):
    """Secondes jusqu'à la prochaine occurrence locale de HH:MM (changements d'heure compris)."""
    minutes = parse_hhmm(hhmm)
    local = dt.datetime.fromtimestamp(now_ts)
    target = local.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)
    if target <= local:
        target += dt.timedelta(days=1)
    return time.mktime(target.timetuple()) - now_ts


def validate_thresholds(thresholds):
    if not isinstance(thresholds, dict) or set(thresholds) != set(THRESHOLD_KEYS):
        raise ConfigError("thresholds doit contenir exactement %s" % ", ".join(THRESHOLD_KEYS))
    for key in THRESHOLD_KEYS:
        high = 100 if key == "max_remove_percent" else 100000
        if not is_int(thresholds[key], 0, high):
            raise ConfigError("seuil %s invalide : %r" % (key, thresholds[key]))


def validate_groups(groups):
    """Groupes attendus : hôte + ensemble exact de chemins (+ tags), comme --group-by host,paths."""
    if not groups:
        raise ConfigError("groups ne doit pas être vide")
    seen = set()
    for g in groups:
        if not g.get("name") or not g.get("host") or not g.get("paths"):
            raise ConfigError("groupe incomplet (name, host et paths requis) : %r" % g)
        for path in g["paths"]:
            if not path.startswith("/") or path.rstrip("/") != path or "*" in path:
                raise ConfigError("chemin invalide dans %s : %r (chemin absolu exact attendu)" % (g["name"], path))
        for sid in g.get("frozen_ids", []):
            if not re.match(r"^[0-9a-f]{8,64}$", sid):
                raise ConfigError("identifiant figé invalide dans %s : %r" % (g["name"], sid))
        key = group_key(g["host"], g["paths"])
        if key in seen:
            raise ConfigError("groupe en double : %s" % g["name"])
        seen.add(key)


def group_key(host, paths):
    return (host, tuple(sorted(paths or [])))


def match_group(groups, sn):
    key = group_key(sn.get("hostname", ""), sn.get("paths"))
    for g in groups:
        if group_key(g["host"], g["paths"]) == key:
            return g
    return None


def parse_hhmm(value):
    m = re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", value or "")
    if not m:
        raise ConfigError("heure invalide : %r (format HH:MM)" % value)
    return int(m.group(1)) * 60 + int(m.group(2))


def parse_window(window):
    start, end = parse_hhmm(window.get("start")), parse_hhmm(window.get("end"))
    if start == end:
        raise ConfigError("fenêtre vide : début et fin identiques")
    return start, end


def in_window(window, now_local):
    start, end = parse_window(window)
    minute = now_local.hour * 60 + now_local.minute
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end  # fenêtre à cheval sur minuit


# --------------------------------------------------------------------------- stockage privé

class UnsafePath(Exception):
    pass


def unescape_mount(field):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def mount_options_for(path):
    """Options (montage + superbloc) du système de fichiers qui contient path, via /proc/self/mountinfo."""
    best = None
    with open("/proc/self/mountinfo") as f:
        for line in f:
            left, _, right = line.partition(" - ")
            a, b = left.split(), right.split()
            if len(a) < 6 or len(b) < 3:
                continue
            mountpoint = unescape_mount(a[4])
            inside = mountpoint == "/" or path == mountpoint or path.startswith(mountpoint.rstrip("/") + "/")
            if inside and (best is None or len(mountpoint) >= len(best[0])):
                best = (mountpoint, b[0], a[5].split(",") + b[2].split(","))
    return best


def acl_xattrs(fd):
    """Attributs étendus portant des ACL (POSIX ou Synology) sur un répertoire ouvert."""
    try:
        names = os.listxattr(fd)
    except OSError:
        return []
    return [n for n in names if n.startswith("system.posix_acl") or "syno" in n.lower()]


def check_dir(fd, label, last):
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode):
        raise UnsafePath("%s n'est pas un répertoire" % label)
    if st.st_uid not in TRUSTED_UIDS:
        raise UnsafePath("%s appartient à l'uid %d" % (label, st.st_uid))
    writable_by_others = st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    if last and st.st_mode & 0o077:
        raise UnsafePath("%s doit être en 0700 (actuellement %o)" % (label, stat.S_IMODE(st.st_mode)))
    # Un parent inscriptible par d'autres n'est admis que s'il a le bit sticky : ses entrées
    # appartenant à un propriétaire de confiance ne peuvent alors être ni renommées ni supprimées.
    if writable_by_others and not (st.st_mode & stat.S_ISVTX):
        raise UnsafePath("%s est inscriptible par le groupe ou les autres (%o)" % (label, stat.S_IMODE(st.st_mode)))
    acl = acl_xattrs(fd)
    if acl:
        raise UnsafePath("%s porte des ACL (%s)" % (label, ", ".join(acl)))


def open_private_dir(path):
    """Ouvre path niveau par niveau depuis « / », sans suivre de lien symbolique, en vérifiant
    chaque niveau ; crée en 0700 les niveaux manquants. Retourne un descripteur du dernier
    niveau : toutes les opérations ultérieures sont relatives à ce descripteur."""
    mount = mount_options_for(path)
    if mount and "synoacl" in mount[2]:
        raise UnsafePath("%s est sur un système de fichiers avec ACL Synology (%s)" % (path, mount[0]))
    fd = os.open("/", DIR_FLAGS)
    try:
        check_dir(fd, "/", last=False)
        parts = path.strip("/").split("/")
        current = ""
        for i, part in enumerate(parts):
            current += "/" + part
            try:
                child = os.open(part, DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=fd)
                child = os.open(part, DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                raise UnsafePath("%s : %s (lien symbolique ou fichier ?)" % (current, exc.strerror))
            os.close(fd)
            fd = child
            check_dir(fd, current, last=(i == len(parts) - 1))
        return fd
    except BaseException:
        os.close(fd)
        raise


def check_private_file(fd, label):
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid not in TRUSTED_UIDS or st.st_nlink != 1 or st.st_mode & 0o077:
        raise UnsafePath("%s : fichier inattendu (type, propriétaire, liens ou droits)" % label)
    return st


def write_json_at(dir_fd, name, data, replace=True):
    """Écriture atomique relative à dir_fd : fichier temporaire unique (O_EXCL, O_NOFOLLOW), puis renommage.
    Avec replace=False, la publication se fait par link() : elle échoue (FileExistsError) si name existe."""
    tmp = ".%s.%s.tmp" % (name, os.urandom(8).hex())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        if replace:
            os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        else:
            os.link(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
            os.unlink(tmp, dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    os.fsync(dir_fd)


def rotate_reports(log_fd, keep):
    reports = sorted(f for f in os.listdir(log_fd) if re.match(r"^report-\d{8}-\d{6}\.json$", f))
    for old in reports[:-keep]:
        os.unlink(old, dir_fd=log_fd)


def open_lock(state_fd):
    fd = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=state_fd)
    try:
        check_private_file(fd, "lock")
    except BaseException:
        os.close(fd)
        raise
    return fd


# --------------------------------------------------------------------------- état

def validate_state(obj, now, max_skew):
    if not isinstance(obj, dict) or not {"last_run", "snapshot_ids"} <= set(obj) \
            or not set(obj) <= {"last_run", "snapshot_ids", "meta", "in_progress"}:
        return "structure inattendue (clés last_run, snapshot_ids et, facultatives, meta, in_progress)"
    if "in_progress" in obj:
        error = validate_in_progress(obj["in_progress"])
        if error:
            return error
    if not isinstance(obj["last_run"], str):
        return "last_run n'est pas une chaîne"
    try:
        last_run = parse_time(obj["last_run"])
    except ValueError:
        return "last_run illisible"
    if last_run > now + max_skew:
        return "last_run dans le futur"
    ids = obj["snapshot_ids"]
    if not isinstance(ids, list) or len(ids) > MAX_STATE_IDS:
        return "snapshot_ids n'est pas une liste de taille raisonnable"
    if any(not isinstance(i, str) or not re.match(r"^[0-9a-f]{64}$", i) for i in ids):
        return "identifiant de snapshot invalide"
    if len(set(ids)) != len(ids):
        return "identifiants en double"
    meta = obj.get("meta", {})
    if not isinstance(meta, dict) or not set(meta) <= set(ids):
        return "meta : identifiants absents de snapshot_ids"
    if any(not valid_meta(m) for m in meta.values()):
        return "meta : métadonnées de snapshot invalides"
    return None


META_KEYS = ("time", "hostname", "paths", "tags", "tree")


def snapshot_meta(sn):
    """Ce que la référence garde de chaque snapshot pour reconnaître un marquage (restic tag)."""
    return {"time": sn.get("time"), "hostname": sn.get("hostname"), "paths": sn.get("paths") or [],
            "tags": sorted(sn.get("tags") or []), "tree": sn.get("tree")}


def valid_meta(m):
    return isinstance(m, dict) and set(m) == set(META_KEYS) \
        and all(isinstance(m[k], str) for k in ("time", "hostname")) \
        and isinstance(m["tree"], str) and SNAPSHOT_ID_RE.match(m["tree"]) is not None \
        and all(isinstance(m[k], list) and all(isinstance(x, str) for x in m[k]) for k in ("paths", "tags"))


def state_doc(started, snapshots, ids):
    """Référence : identifiants retenus et, pour chacun, ses métadonnées (si restic les fournit complètes)."""
    by_id = {sn.get("id"): sn for sn in snapshots}
    meta = {i: snapshot_meta(by_id[i]) for i in ids if i in by_id and valid_meta(snapshot_meta(by_id[i]))}
    return {"last_run": started.isoformat(), "snapshot_ids": sorted(ids), "meta": meta}


def validate_in_progress(ip):
    """Opération réelle commencée et non terminée : {"operation", "started", "ids"}."""
    if not isinstance(ip, dict) or set(ip) != {"operation", "started", "ids"}:
        return "in_progress : structure inattendue"
    if ip["operation"] not in ("forget", "prune"):
        return "in_progress : opération inconnue"
    try:
        parse_time(ip["started"])
    except (TypeError, ValueError):
        return "in_progress : started illisible"
    ids = ip["ids"]
    if not isinstance(ids, list) or len(ids) > MAX_STATE_IDS \
            or any(not isinstance(i, str) or not SNAPSHOT_ID_RE.match(i) for i in ids):
        return "in_progress : identifiants invalides"
    if ip["operation"] == "prune" and ids:
        return "in_progress : prune ne supprime aucun snapshot"
    return None


def read_state(state_fd, now, max_skew):
    """Retourne (état, None), (None, None) si absent, ou (None, erreur) si invalide."""
    try:
        fd = os.open("state.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=state_fd)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, "state.json inaccessible : %s" % exc.strerror
    try:
        st = check_private_file(fd, "state.json")  # avant fdopen : refuse dossier, FIFO, lien multiple…
    except UnsafePath as exc:
        os.close(fd)
        return None, str(exc)
    with os.fdopen(fd, "rb") as f:
        if st.st_size > MAX_STATE_BYTES:
            return None, "state.json trop volumineux (%d octets)" % st.st_size
        data = f.read(MAX_STATE_BYTES + 1)
    if len(data) > MAX_STATE_BYTES:
        return None, "state.json trop volumineux"
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "state.json n'est pas un JSON valide"
    error = validate_state(obj, now, max_skew)
    return (None, error) if error else (obj, None)


# --------------------------------------------------------------------------- restic

def parse_duration(text):
    """Durée restic (« 30d », « 1y6m », « 1d12h ») -> dict y/m/d/h. Chaque unité au plus une fois :
    restic écraserait silencieusement une unité répétée."""
    if not isinstance(text, str):
        raise ValueError("durée non textuelle")
    parts = re.findall(r"([0-9]{1,5})([ymdh])", text)
    units = [u for _, u in parts]
    if not parts or "".join(n + u for n, u in parts) != text or len(set(units)) != len(units):
        raise ValueError("durée invalide : %r" % text)
    duration = dict.fromkeys("ymdh", 0)
    for number, unit in parts:
        duration[unit] = int(number)
    return duration


def parse_time_with_offset(value):
    """Comme parse_time, mais conserve le décalage d'origine (+02:00…), comme restic dans le conteneur."""
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)$", value)
    if not m:
        raise ValueError("horodatage illisible : %r" % value)
    frac = (m.group(2) or ".0")[1:7].ljust(6, "0")
    tz = "+00:00" if m.group(3) == "Z" else m.group(3)
    return dt.datetime.fromisoformat("%s.%s%s" % (m.group(1), frac, tz))


def go_add_date(t, years, months, days):
    """Équivalent de time.AddDate de Go : calcul sur l'heure locale du décalage de t, débordements
    normalisés (31 mars - 1 mois = 3 mars). Exact pour un décalage fixe, cas des dates restic ici
    (le conteneur ne définit pas de fuseau)."""
    month_index = t.year * 12 + (t.month - 1) + years * 12 + months
    first = t.replace(year=month_index // 12, month=month_index % 12 + 1, day=1)
    return first + dt.timedelta(days=t.day - 1 + days)


def keep_within_cutoff(latest, duration):
    """Limite de --keep-within : un snapshot est conservé s'il est strictement postérieur."""
    return go_add_date(latest, -duration["y"], -duration["m"], -duration["d"]) - dt.timedelta(hours=duration["h"])


def young_removals(forget_groups, keep_within, policy_now):
    """Candidats à la suppression que --keep-within aurait dû conserver.

    Règle de restic 0.19.1, groupe par groupe : référence = snapshot le plus récent strictement antérieur
    à l'exécution de restic ; limite = référence - durée (calendaire, dans le décalage de la référence) ;
    conservé si strictement postérieur à la limite. policy_now est pris juste après « forget » : il ne
    peut que retarder la référence, donc seulement réduire les signalements (jamais en créer à tort).
    Retourne une liste de constats (findings)."""
    duration = parse_duration(keep_within)
    if not any(duration.values()):
        return []
    findings = []
    for g in forget_groups:
        label = "%s %s" % (g.get("host"), g.get("paths"))
        try:
            snaps = [(s.get("id", "?"), parse_time_with_offset(s["time"]))
                     for s in (g.get("keep") or []) + (g.get("remove") or [])]
            removed = [(s.get("id", "?"), parse_time_with_offset(s["time"])) for s in (g.get("remove") or [])]
        except (KeyError, TypeError, ValueError) as exc:
            findings.append(finding("ERROR", "forget_time_invalid", "groupe %s : %s" % (label, exc)))
            continue
        past = [t for _, t in snaps if t < policy_now]
        latest = max(past) if past else None
        cutoff = keep_within_cutoff(latest, duration) if latest else None
        for sid, t in removed:
            if cutoff is None or t > cutoff:
                findings.append(finding("ERROR", "young_removal",
                                        "%s (%s) proposé à la suppression alors que --keep-within %s le conserve"
                                        " (groupe %s, référence %s, limite %s)"
                                        % (sid[:8], t.isoformat(), keep_within, label,
                                           latest.isoformat() if latest else "aucune",
                                           cutoff.isoformat() if cutoff else "aucune")))
    return findings


def parse_time(value):
    """Convertit un horodatage restic (nanosecondes, fuseau) en datetime UTC."""
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)$", value)
    if not m:
        raise ValueError("horodatage illisible : %r" % value)
    frac = (m.group(2) or ".0")[1:7].ljust(6, "0")
    tz = "+00:00" if m.group(3) == "Z" else m.group(3)
    return dt.datetime.fromisoformat("%s.%s%s" % (m.group(1), frac, tz)).astimezone(dt.timezone.utc)


def build_restic_args(subcommand, extra):
    args = ["--no-lock", "--no-cache", subcommand] + list(extra)
    check_safe(args)
    return args


def build_write_args(subcommand, extra):
    args = ["--no-cache", subcommand] + list(extra)
    check_write_safe(args)
    return args


def check_write_safe(args):
    """Seules commandes admises sur le repository monté en écriture, sous verrou restic :
    « forget <identifiants complets> » et « prune --max-unused=N% ». Rien d'autre."""
    if len(args) < 3 or args[0] != "--no-cache":
        raise StepError("commande d'écriture refusée : %r" % (args[:3],))
    if args[1] == "forget":
        if not all(SNAPSHOT_ID_RE.match(a) for a in args[2:]) or len(set(args[2:])) != len(args[2:]):
            raise StepError("forget réel : seuls des identifiants complets, distincts, sont admis")
        return
    if args[1] == "prune" and len(args) == 3 and args[2].startswith("--max-unused=") \
            and MAX_UNUSED_RE.match(args[2][len("--max-unused="):]):
        return
    raise StepError("commande d'écriture refusée : %r" % (args,))


def check_safe(args):
    """Dernier garde-fou avant exécution : refuse toute commande potentiellement destructive."""
    sub = [a for a in args if not a.startswith("-")]
    if not sub or sub[0] not in ALLOWED_SUBCOMMANDS:
        raise StepError("sous-commande restic refusée : %r" % (sub[:1],))
    for token in args:
        if token in FORBIDDEN_TOKENS:
            raise StepError("option restic refusée : %r" % token)
    if sub[0] == "forget" and "--dry-run" not in args:
        raise StepError("forget sans --dry-run refusé")
    if sub[0] == "check":
        extra = [a for a in args if a not in ("--no-lock", "--no-cache", "check")]
        if len(extra) > 1 or any(not CHECK_OPTION_RE.match(a) for a in extra):
            raise StepError("option de check refusée : %r" % (extra,))
    if sub[0] == "prune":
        extra = [a for a in args if a not in ("--no-lock", "--no-cache", "prune", "--dry-run")]
        if "--dry-run" not in args or len(extra) != 1 or not extra[0].startswith("--max-unused=") \
                or not MAX_UNUSED_RE.match(extra[0][len("--max-unused="):]):
            raise StepError("prune en lecture seule : --dry-run et --max-unused=N%% seulement : %r" % (extra,))
    if "--no-lock" not in args:
        raise StepError("--no-lock obligatoire (repository en lecture seule)")


def docker_base(cfg, container_name, write=False):
    """Options communes à tous les conteneurs : isolement et montages en lecture seule.
    write=True (opérations réelles seulement) : /repo en écriture, sous l'uid:gid propriétaire
    du repository, avec DAC_OVERRIDE pour les dossiers régis par les ACL Synology."""
    if write:
        repo_mount = "%s:%s" % (cfg["repository"], CONTAINER_REPO)
        identity = ["--user", cfg.get("apply", NO_APPLY)["repository_owner"],
                    "--cap-drop", "ALL", "--cap-add", "DAC_READ_SEARCH", "--cap-add", "DAC_OVERRIDE"]
    else:
        repo_mount = "%s:%s:ro" % (cfg["repository"], CONTAINER_REPO)
        identity = ["--cap-drop", "ALL", "--cap-add", "DAC_READ_SEARCH"]
    return [
        DOCKER_BIN, "run", "--rm", "--name", container_name,
        "--hostname", "restic-maint-%s" % cfg["name"],
        "--pull", "never",
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m",
    ] + identity + [
        "--security-opt", "no-new-privileges",
        "-v", repo_mount,
        "-v", "%s:%s:ro" % (cfg["password_file"], CONTAINER_PASSWORD),
        "-e", "RESTIC_REPOSITORY=%s" % CONTAINER_REPO,
        "-e", "RESTIC_PASSWORD_FILE=%s" % CONTAINER_PASSWORD,
        "-e", "TMPDIR=/tmp",
    ]


# Barrière exécutée dans chaque conteneur restic, avant restic : lit les montages de CE
# conteneur et refuse (code GATE_EXIT) si /, /repo, le secret ou tout sous-montage de /repo
# n'est pas en lecture seule. Même règle que readonly_problems() ; busybox sh/awk de l'image.
# Conteneur d'écriture (rw=1) : /repo exactement doit au contraire être « rw » ; tout le reste « ro ».
GATE_EXIT = 97
GATE_MARKER = "RESTIC_GATE_REFUSED"
GATE_AWK = r"""
{
  mp = $2
  if (mp == "/" || mp == repo || mp == secret || index(mp, repo "/") == 1) {
    n = split($4, opts, ","); ro = 0; wr = 0
    for (i = 1; i <= n; i++) { if (opts[i] == "ro") ro = 1; if (opts[i] == "rw") wr = 1 }
    state[mp] = (rw == 1 && mp == repo) ? wr : ro
  }
}
END {
  bad = 0
  if (!("/" in state) || !(repo in state) || !(secret in state)) { print "montage attendu absent" > "/dev/stderr"; bad = 1 }
  for (m in state) if (!state[m]) { print "montage refusé (ro attendu, rw pour /repo en écriture) : " m > "/dev/stderr"; bad = 1 }
  exit bad
}
"""

def gate_script(write=False):
    return ("awk -v repo=%s -v secret=%s -v rw=%d '%s' /proc/self/mounts || { echo %s >&2; exit %d; }\n"
            "exec /usr/bin/restic \"$@\"\n"
            % (CONTAINER_REPO, CONTAINER_PASSWORD, 1 if write else 0, GATE_AWK, GATE_MARKER, GATE_EXIT))


GATE_SCRIPT = gate_script()


def docker_command(cfg, container_name, restic_args, write=False):
    """Conteneur restic : la barrière de montages s'exécute d'abord, puis « exec restic »."""
    return docker_base(cfg, container_name, write) + ["--entrypoint", "/bin/sh", cfg["image"],
                                                      "-c", gate_script(write), "restic-gate"] + restic_args


def mounts_command(cfg, container_name, write=False):
    """Même conteneur, mais affiche ses points de montage au lieu de lancer restic."""
    return docker_base(cfg, container_name, write) + ["--entrypoint", "/bin/cat", cfg["image"], "/proc/self/mounts"]


def readonly_problems(mounts_text, write=False):
    """Exige l'option ro sur /, /repo, le secret et tout sous-montage de /repo.
    Pour un point de montage répété, seule la dernière ligne (le montage visible) compte.
    write=True : /repo exactement doit au contraire être en écriture (le reste inchangé)."""
    options = {}
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        mp = fields[1]
        if mp in ("/", CONTAINER_REPO, CONTAINER_PASSWORD) or mp.startswith(CONTAINER_REPO + "/"):
            options[mp] = fields[3].split(",")
    problems = ["%s : point de montage absent" % mp
                for mp in ("/", CONTAINER_REPO, CONTAINER_PASSWORD) if mp not in options]
    for mp in sorted(options):
        if write and mp == CONTAINER_REPO:
            if "rw" not in options[mp]:
                problems.append("%s : monté sans « rw » pour une opération réelle" % mp)
        elif "ro" not in options[mp]:
            problems.append("%s : monté sans « ro » (%s)" % (unescape_mount(mp), ",".join(options[mp])))
    return problems


def repository_owner_problems(repository, owner):
    """Les fichiers créés par une opération réelle doivent rester lisibles par le REST Server :
    on écrit sous l'identité qui possède déjà le repository, et on refuse si elle n'est pas homogène."""
    problems = []
    for entry in REPO_ENTRIES:
        try:
            st = os.lstat(os.path.join(repository, entry))
        except OSError as exc:
            problems.append("%s : %s" % (entry, exc.strerror))
            continue
        actual = "%d:%d" % (st.st_uid, st.st_gid)
        if stat.S_ISLNK(st.st_mode):
            problems.append("%s : lien symbolique" % entry)
        elif actual != owner:
            problems.append("%s : propriétaire %s au lieu de %s" % (entry, actual, owner))
    return problems


def parse_restic_version(output):
    m = re.match(r"^restic (\d+\.\d+\.\d+)\b", output.strip())
    return m.group(1) if m else None


class ResticFailed(StepError):
    def __init__(self, message, returncode):
        StepError.__init__(self, message)
        self.returncode = returncode


class Runner:
    def __init__(self, cfg, deadline, log):
        self.cfg = cfg
        self.deadline = deadline
        self.log = log
        self.counter = 0

    def _name(self):
        self.counter += 1
        return "restic-maint-%s-%d-%d" % (re.sub(r"[^a-z0-9-]", "-", self.cfg["name"]), os.getpid(), self.counter)

    def _exec(self, cmd, name, label, graceful=False):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise StepTimeout("délai global dépassé avant « %s »" % label)
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=remaining)
        except subprocess.TimeoutExpired:
            stop = self.stop_container(name, graceful)
            raise StepTimeout("délai global dépassé pendant « %s » ; arrêt du conteneur %s"
                              % (label, "confirmé" if stop["confirmed"] else "NON CONFIRMÉ"), stop)
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        if proc.returncode == GATE_EXIT and GATE_MARKER in stderr:
            raise UnsafeMounts("%s : barrière du conteneur : %s" % (label, stderr[-500:]))
        if proc.returncode != 0:
            raise ResticFailed("%s a échoué (code %d) : %s" % (label, proc.returncode, stderr[-2000:]),
                               proc.returncode)
        return proc.stdout.decode("utf-8", "replace")

    def _docker(self, args, timeout):
        """Commande Docker bornée : (code, sortie) ; code None si elle n'a pas répondu à temps."""
        try:
            proc = subprocess.run([DOCKER_BIN] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, "sans réponse après %d s" % timeout
        except OSError as exc:
            return None, str(exc)
        out = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
        return proc.returncode, out[-300:]

    def stop_container(self, name, graceful):
        """Arrête le conteneur et confirme qu'il ne tourne plus. Chaque étape est bornée dans le temps.
        graceful : docker stop (SIGTERM, restic retire son verrou ; SIGKILL après le délai de grâce),
        puis docker kill en secours ; sinon docker kill directement."""
        steps = []
        if graceful:
            # « --time » : seule forme connue de Docker 24 (DSM) ; les versions récentes l'acceptent encore.
            code, out = self._docker(["stop", "--time", str(STOP_GRACE_SECONDS), name], STOP_CALL_TIMEOUT)
            steps.append({"command": "stop", "code": code, "output": out})
        if not graceful or steps[-1]["code"] != 0:
            code, out = self._docker(["kill", name], KILL_CALL_TIMEOUT)
            steps.append({"command": "kill", "code": code, "output": out})
        code, out = self._docker(["inspect", "-f", "{{.State.Running}}", name], INSPECT_TIMEOUT)
        if code == 0 and out == "false":
            state = "arrêté"
        elif code is not None and code != 0 and "no such" in out.lower():
            state = "absent (supprimé par --rm)"
        elif code == 0 and out == "true":
            state = "EN COURS D'EXÉCUTION"
        else:
            state = "inconnu (%s)" % out
        steps.append({"command": "inspect", "code": code, "output": out})
        stop = {"container": name, "confirmed": state.startswith(("arrêté", "absent")), "state": state,
                "steps": steps}
        self.log("arrêt du conteneur %s : %s (%s)" % (name, state, ", ".join(
            "%s → %s" % (st["command"], "sans réponse" if st["code"] is None else "code %d" % st["code"])
            for st in steps)))
        return stop

    def mounts(self, write=False):
        name = self._name()
        self.log("vérification des montages du conteneur%s" % (" d'écriture" if write else ""))
        return self._exec(mounts_command(self.cfg, name, write), name, "lecture des montages")

    def restic(self, subcommand, *extra):
        args = build_restic_args(subcommand, extra)
        name = self._name()
        self.log("restic %s" % " ".join(args))
        return self._exec(docker_command(self.cfg, name, args), name, "restic %s" % subcommand)

    def restic_write(self, subcommand, *extra):
        """Opération réelle : repository en écriture, verrou restic normal (jamais --no-lock)."""
        args = build_write_args(subcommand, extra)
        name = self._name()
        self.log("ÉCRITURE : restic %s" % " ".join(a if not SNAPSHOT_ID_RE.match(a) else a[:8] for a in args))
        return self._exec(docker_command(self.cfg, name, args, write=True), name,
                          "restic %s (écriture)" % subcommand, graceful=True)


def parse_json_output(text):
    """Extrait le document JSON de la sortie restic (ignore d'éventuelles lignes de texte)."""
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("[") or line.startswith("{"):
            return json.loads(line)
    if not text.strip():
        return []
    return json.loads(text)


def forget_args(retention):
    return [
        "--dry-run", "--json",
        "--group-by", "host,paths",
        "--keep-within", retention["keep_within"],
        "--keep-daily", str(retention["keep_daily"]),
        "--keep-weekly", str(retention["keep_weekly"]),
        "--keep-monthly", str(retention["keep_monthly"]),
    ] + [a for tag in retention.get("keep_tags", []) for a in ("--keep-tag", tag)]


def keep_tags_of(cfg, sn):
    """Tags de protection (retention.keep_tags) portés par un snapshot."""
    return sorted(set(sn.get("tags") or []) & set(cfg["retention"].get("keep_tags", [])))


# --------------------------------------------------------------------------- analyse

def retag_problem(cfg, old, claimants):
    """None si le seul prétendant est le même snapshot que old, avec seulement un tag de protection en plus."""
    if len(claimants) != 1:
        return "%d snapshots se réclament du même original" % len(claimants)
    if old is None:
        return "métadonnées de l'original absentes de la référence : comparaison impossible"
    new = snapshot_meta(claimants[0])
    diff = [k for k in ("tree", "time", "hostname", "paths") if new[k] != old[k]]
    if diff:
        return "%s modifié(s)" % ", ".join(diff)
    added, removed = set(new["tags"]) - set(old["tags"]), set(old["tags"]) - set(new["tags"])
    keep = set(cfg["retention"].get("keep_tags", []))
    if removed or not added or not added <= keep:
        return "tags %r → %r : seul l'ajout d'un tag de protection %r est admis" % (old["tags"], new["tags"], sorted(keep))
    return None


def finding(level, code, message):
    return {"level": level, "code": code, "message": message}


def max_in_window(times, window):
    """Nombre maximal de snapshots dans une fenêtre glissante (times triés)."""
    best, j = 0, 0
    for i in range(len(times)):
        while times[i] - times[j] > window:
            j += 1
        best = max(best, i - j + 1)
    return best


def analyze(cfg, snapshots, forget_groups, now, state, state_error=None, policy_now=None, apply=False):
    """Analyse pure : ne fait aucune entrée/sortie. Retourne (findings, summary).
    apply=True : seuils de volume bloquants (ERROR) au lieu d'informatifs (WARN)."""
    th = cfg["thresholds"]
    findings = []
    groups = cfg["groups"]
    known_hosts = {g["host"] for g in groups}
    future_limit = now + dt.timedelta(minutes=th["max_future_skew_minutes"])

    by_group = {g["name"]: [] for g in groups}
    ids = set()
    for sn in snapshots:
        sid = sn.get("id", "?")
        ids.add(sid)
        try:
            t = parse_time(sn["time"])
        except (KeyError, ValueError) as exc:
            findings.append(finding("ERROR", "time_unreadable", "%s : %s" % (sid[:8], exc)))
            continue
        if t > future_limit:
            findings.append(finding("ERROR", "future_snapshot", "%s : daté dans le futur (%s)" % (sid[:8], sn["time"])))
        g = match_group(groups, sn)
        if g is None:
            host = sn.get("hostname", "")
            if host not in known_hosts:
                findings.append(finding("ERROR", "host_not_allowed", "%s : hôte non autorisé %r" % (sid[:8], host)))
            else:
                findings.append(finding("ERROR", "path_not_allowed", "%s : %s, chemins non autorisés %r"
                                        % (sid[:8], host, sn.get("paths"))))
            continue
        # Un tag de protection (keep_tags) s'ajoute aux tags du groupe sans les remplacer.
        own = sorted(set(sn.get("tags") or []) - set(keep_tags_of(cfg, sn)))
        if "tags" in g and own != sorted(g["tags"]):
            findings.append(finding("ERROR", "tags_mismatch", "%s : groupe %s, tags %r au lieu de %r"
                                    % (sid[:8], g["name"], sn.get("tags") or [], g["tags"])))
        frozen = g.get("frozen_ids")
        if frozen is not None and not any(sid.startswith(f) for f in frozen):
            findings.append(finding("ERROR", "frozen_group_changed", "%s : nouveau snapshot dans le groupe figé %s"
                                    % (sid[:8], g["name"])))
        by_group[g["name"]].append(t)

    for g in groups:
        times = by_group[g["name"]]
        if g.get("frozen_ids") is not None:
            # Groupe historique : ni fraîcheur ni fréquence, seulement la présence des snapshots attendus.
            for f in g["frozen_ids"]:
                if not any(sid.startswith(f) for sid in ids):
                    findings.append(finding("ERROR", "frozen_snapshot_missing", "groupe %s : %s absent" % (g["name"], f)))
            continue
        # Fraîcheur du groupe (hors snapshots futurs).
        past = [t for t in times if t <= future_limit]
        if not past:
            findings.append(finding("ERROR", "no_snapshot", "groupe %s : aucun snapshot" % g["name"]))
        elif now - max(past) > dt.timedelta(hours=th["max_age_hours"]):
            findings.append(finding("ERROR", "stale", "groupe %s : dernier snapshot trop ancien (%s)"
                                    % (g["name"], max(past).isoformat())))
        # Fréquence anormale sur 24 h glissantes, sur tout l'historique du groupe.
        peak = max_in_window(sorted(times), dt.timedelta(hours=24))
        if peak > th["max_snapshots_per_24h"]:
            findings.append(finding("ERROR", "burst", "groupe %s : %d snapshots sur 24 h (max %d)"
                                    % (g["name"], peak, th["max_snapshots_per_24h"])))

    # Comparaison avec la dernière exécution validée (état local, hors repository).
    if state_error:
        findings.append(finding("ERROR", "state_invalid", "référence inutilisable, conservée telle quelle : %s"
                                % state_error))
    elif state:
        previous = set(state.get("snapshot_ids", []))
        last_run = parse_time(state["last_run"])
        tolerance = dt.timedelta(hours=th["backdate_tolerance_hours"])
        ip = state.get("in_progress")
        requested = set(ip["ids"]) if ip else set()
        if ip:
            findings.append(finding("ERROR", "operation_interrupted",
                                    "%s réel commencé le %s et non terminé (%d/%d suppressions demandées constatées) :"
                                    " voir « Opération interrompue » dans le README"
                                    % (ip["operation"], ip["started"], len(requested - ids), len(requested))))
        # Marquage (« restic tag ») : restic réécrit le snapshot sous un nouvel identifiant et
        # supprime l'ancien ; le nouveau porte « original » = l'ancien. Admis seulement si
        # l'ancien a disparu, qu'un seul nouveau s'en réclame, et que les métadonnées gardées dans
        # la référence (arbre, date, hôte, chemins, tags) sont identiques, à l'ajout près d'un tag
        # de protection. Sans métadonnées (référence antérieure), jamais admis.
        claims = {}
        for sn in snapshots:
            if sn.get("id") not in previous and sn.get("original") in previous - ids:
                claims.setdefault(sn["original"], []).append(sn)
        retagged = {}
        for orig, c in sorted(claims.items()):
            reason = retag_problem(cfg, (state.get("meta") or {}).get(orig), c)
            if reason:
                findings.append(finding("ERROR", "retag_mismatch", "%s → %s : %s"
                                        % (orig[:8], ", ".join(x["id"][:8] for x in c), reason)))
            else:
                retagged[orig] = c[0]
        for orig, sn in sorted(retagged.items()):
            findings.append(finding("INFO", "retagged", "%s remplacé par %s (marquage %s), même snapshot"
                                    % (orig[:8], sn["id"][:8], ",".join(keep_tags_of(cfg, sn)))))
        retagged_ids = {sn["id"] for sn in retagged.values()}
        gone = sorted(previous - ids - requested - set(retagged))
        if gone:
            findings.append(finding("ERROR", "snapshots_disappeared",
                                    "%d snapshot(s) disparu(s) depuis la dernière exécution : %s"
                                    % (len(gone), ", ".join(g[:8] for g in gone[:10]))))
        for sn in snapshots:
            if sn.get("id") in previous or sn.get("id") in retagged_ids:
                continue
            try:
                t = parse_time(sn["time"])
            except (KeyError, ValueError):
                continue
            if t < last_run - tolerance:
                findings.append(finding("ERROR", "backdated_snapshot",
                                        "%s : nouveau snapshot daté de %s, antérieur à la dernière exécution"
                                        % (sn["id"][:8], sn["time"])))
    else:
        findings.append(finding("INFO", "baseline", "aucune référence (state.json absent) : comparaisons impossibles"))

    # Estimation des suppressions (forget --dry-run).
    total = len(snapshots)
    removed = sum(len(g.get("remove") or []) for g in forget_groups)
    kept = sum(len(g.get("keep") or []) for g in forget_groups)
    if kept + removed != total:
        findings.append(finding("ERROR", "forget_mismatch",
                                "forget couvre %d snapshots, le repository en contient %d" % (kept + removed, total)))
    percent = (100.0 * removed / total) if total else 0.0
    # En dry-run, rien n'est supprimé : les candidats s'accumulent normalement d'une exécution à
    # l'autre. Ces seuils, faits pour encadrer une suppression réelle, ne sont alors qu'informatifs.
    volume_level = "ERROR" if apply else "WARN"
    if removed > th["max_remove_count"]:
        findings.append(finding(volume_level, "remove_count", "%d suppressions estimées (max %d)" % (removed, th["max_remove_count"])))
    if percent > th["max_remove_percent"]:
        findings.append(finding(volume_level, "remove_percent", "%.1f %% de suppressions estimées (max %d %%)" % (percent, th["max_remove_percent"])))
    findings.extend(young_removals(forget_groups, cfg["retention"]["keep_within"], policy_now or now))
    groups = []
    for g in forget_groups:
        k, r = len(g.get("keep") or []), len(g.get("remove") or [])
        groups.append({"host": g.get("host"), "paths": g.get("paths"), "keep": k, "remove": r,
                       "remove_ids": [s.get("id", "?")[:8] for s in (g.get("remove") or [])]})
        if k < th["min_keep_per_group"] and r > 0:
            findings.append(finding("ERROR", "min_keep",
                                    "%s %s : %d conservés seulement (min %d)" % (g.get("host"), g.get("paths"), k, th["min_keep_per_group"])))

    summary = {
        "snapshots": total,
        "hosts": sorted({sn.get("hostname", "") for sn in snapshots}),
        "paths": sorted({p for sn in snapshots for p in sn.get("paths", [])}),
        "per_group": {name: len(times) for name, times in sorted(by_group.items())},
        "would_keep": kept,
        "would_remove": removed,
        "would_remove_percent": round(percent, 1),
        "groups": groups,
    }
    return findings, summary


# --------------------------------------------------------------------------- main

def main(argv=None):
    parser = argparse.ArgumentParser(description="Maintenance Restic : contrôle dry-run, opérations réelles explicites")
    parser.add_argument("--config", required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--init-baseline", action="store_true",
                        help="crée la référence si state.json est absent (premier lancement manuel uniquement)")
    action.add_argument("--apply", choices=("forget", "prune"),
                        help="opération réelle après un contrôle entièrement propre (activée dans la configuration)")
    action.add_argument("--resolve-interrupted", action="store_true",
                        help="après vérification manuelle, clôt une opération réelle interrompue (lecture seule)")
    parser.add_argument("--read-data", action="store_true",
                        help="force la lecture intégrale des données pour cette exécution (lancement manuel)")
    parser.add_argument("--ignore-window", action="store_true",
                        help="exécution manuelle hors fenêtre (sans effet sur le mode)")
    opts = parser.parse_args(argv)

    lines = []

    def log(msg):
        line = "%s %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
        lines.append(line)
        print(line, flush=True)

    try:
        cfg = load_config(opts.config)
    except (OSError, ValueError, ConfigError) as exc:
        print("CONFIG: %s" % exc, file=sys.stderr)
        return EXIT_CONFIG

    os.umask(0o077)
    fds = []
    try:
        try:
            state_fd = open_private_dir(cfg["private_dir"] + "/state")
            fds.append(state_fd)
            log_fd = open_private_dir(cfg["private_dir"] + "/logs")
            fds.append(log_fd)
            lock_fd = open_lock(state_fd)
            fds.append(lock_fd)
        except (UnsafePath, OSError) as exc:
            print("STOCKAGE: %s" % exc, file=sys.stderr)
            return EXIT_CONFIG
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("une maintenance %s est déjà en cours" % cfg["name"], file=sys.stderr)
            return EXIT_LOCKED
        return run(cfg, opts, log, lines, state_fd, log_fd)
    finally:
        for fd in reversed(fds):
            os.close(fd)


def tail(text, n=40):
    return [line for line in text.strip().splitlines() if line.strip()][-n:]


def run(cfg, opts, log, lines, state_fd, log_fd):
    """Exécution sous verrou exclusif ; état et rapports relatifs aux descripteurs vérifiés."""
    started = dt.datetime.now(dt.timezone.utc)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    operation = opts.apply or ("resolve-interrupted" if opts.resolve_interrupted else "control")
    report = {"name": cfg["name"], "mode": cfg["mode"], "operation": operation, "image": cfg["image"],
              "expected_restic_version": cfg["expected_restic_version"],
              "started": started.isoformat(), "findings": [], "summary": None, "status": None}

    def finish(status, code):
        report["status"] = status
        report["finished"] = dt.datetime.now(dt.timezone.utc).isoformat()
        report["log"] = lines
        write_json_at(log_fd, "report-%s.json" % stamp, report)
        rotate_reports(log_fd, cfg.get("keep_reports", 90))
        log("statut : %s" % status)
        return code

    log("maintenance %s — %s — image %s" % (cfg["name"], "OPÉRATION RÉELLE : %s" % opts.apply if opts.apply
                                            else "contrôle dry-run" if operation == "control" else operation,
                                            cfg["image"]))
    if not opts.ignore_window and not in_window(cfg["window"], dt.datetime.now()):
        log("hors fenêtre %s–%s : rien à faire" % (cfg["window"]["start"], cfg["window"]["end"]))
        return finish("outside_window", EXIT_WINDOW)

    for key in ("repository", "password_file"):
        if not os.path.exists(cfg[key]):
            log("introuvable : %s" % cfg[key])
            return finish("config_error", EXIT_CONFIG)
    if not os.path.exists(DOCKER_BIN):
        log("Docker introuvable : %s" % DOCKER_BIN)
        return finish("config_error", EXIT_CONFIG)

    apply_cfg = cfg.get("apply", NO_APPLY)
    budget = 60 * cfg["timeout_minutes"]
    if opts.apply:
        if not apply_cfg[opts.apply]:
            log("--apply %s refusé : opération désactivée dans la configuration (apply.%s)" % (opts.apply, opts.apply))
            return finish("apply_disabled", EXIT_CONFIG)
        # Plage protégée (snapshots Btrfs, Hyper Backup) : jamais d'écriture dedans, même avec
        # --ignore-window. Le délai est raccourci pour que le conteneur d'écriture soit arrêté
        # (SIGTERM, puis SIGKILL après STOP_GRACE_SECONDS) avant son début.
        protected = apply_cfg["protected"]
        if in_window(protected, dt.datetime.now()):
            log("--apply refusé : plage protégée %s–%s en cours" % (protected["start"], protected["end"]))
            return finish("protected_period", EXIT_WINDOW)
        budget = min(budget, seconds_until(protected["start"], time.time()) - STOP_GRACE_SECONDS - 30)
        if budget < MIN_APPLY_SECONDS:
            log("--apply refusé : moins de %d min avant la plage protégée (%s)"
                % (MIN_APPLY_SECONDS // 60, protected["start"]))
            return finish("protected_period", EXIT_WINDOW)
        log("opération réelle : arrêt garanti avant %s (délai %d min)" % (protected["start"], budget // 60))
        problems = repository_owner_problems(cfg["repository"], apply_cfg["repository_owner"])
        if problems:
            for p in problems:
                log("repository refusé pour l'écriture : %s" % p)
            return finish("repository_owner", EXIT_CONFIG)

    state, state_error = read_state(state_fd, started,
                                    dt.timedelta(minutes=cfg["thresholds"]["max_future_skew_minutes"]))
    report["baseline"] = "invalid" if state_error else ("present" if state else "absent")
    if opts.init_baseline:
        if state:
            log("--init-baseline refusé : une référence valide existe déjà (%d snapshots, dernière exécution %s) ;"
                " elle n'est pas remplacée" % (len(state["snapshot_ids"]), state["last_run"]))
            return finish("baseline_exists", EXIT_BASELINE_EXISTS)
        if state_error:
            log("--init-baseline sans effet : state.json existe mais est invalide ; il est conservé tel quel")
    if opts.resolve_interrupted and not (state and state.get("in_progress")):
        log("--resolve-interrupted : aucune opération interrompue dans la référence ; rien à faire")
        return finish("nothing_to_resolve", EXIT_CONFIG)

    # Toute exécution autre que le contrôle quotidien exige un repository sans verrou.
    strict = operation != "control"
    runner = Runner(cfg, time.monotonic() + budget, log)
    try:
        problems = readonly_problems(runner.mounts())
        if problems:
            for p in problems:
                log("montage refusé : %s" % p)
            return finish("unsafe_mounts", EXIT_CONFIG)
        report["mounts_readonly"] = True
        log("montages en lecture seule vérifiés : /, %s, %s" % (CONTAINER_REPO, CONTAINER_PASSWORD))

        version_output = runner.restic("version").strip()
        log(version_output)
        report["restic_version"] = parse_restic_version(version_output)
        if report["restic_version"] != cfg["expected_restic_version"]:
            log("version restic %r inattendue (attendu %s)" % (report["restic_version"], cfg["expected_restic_version"]))
            return finish("unexpected_version", EXIT_RESTIC)
        snapshots = parse_json_output(runner.restic("snapshots", "--json")) or []
        log("%d snapshots lus" % len(snapshots))
        locks = runner.restic("list", "locks").split()
        if locks:
            report["findings"].append(finding("ERROR" if strict else "WARN", "locks",
                                              "%d verrou(s) présent(s) dans le repository%s"
                                              % (len(locks), " : aucune suppression automatique de verrou"
                                                 if strict else "")))
        if cfg.get("run_check", True):
            # Lecture des données réservée au contrôle quotidien : durée des opérations réelles bornée.
            check_args = read_data_args(cfg.get("read_data", NO_READ_DATA), dt.date.today(),
                                        opts.read_data) if not strict or opts.read_data else []
            report["check"] = {"read_data": " ".join(check_args) or "aucune", "ok": False}
            runner.restic("check", *check_args)
            report["check"]["ok"] = True
            log("check : OK (%s)" % ("lecture des données : " + " ".join(check_args) if check_args
                                     else "structure seulement"))
        forget_groups = parse_json_output(runner.restic("forget", *forget_args(cfg["retention"]))) or []
        policy_now = dt.datetime.now(dt.timezone.utc)  # borne supérieure de l'heure utilisée par restic
    except UnsafeMounts as exc:
        log(str(exc))
        return finish("unsafe_mounts", EXIT_CONFIG)
    except StepTimeout as exc:
        record_timeout(report, log, exc)
        return finish("timeout", EXIT_TIMEOUT)
    except (StepError, ValueError) as exc:
        log(str(exc))
        return finish("restic_error", EXIT_RESTIC)

    findings, summary = analyze(cfg, snapshots, forget_groups, started, state, state_error, policy_now,
                                apply=opts.apply == "forget")
    removal = [sn.get("id", "") for g in forget_groups for sn in (g.get("remove") or [])]
    if opts.apply == "forget":
        findings.extend(removal_problems(cfg, snapshots, removal))
    report["findings"].extend(findings)
    report["summary"] = summary

    for f in report["findings"]:
        log("%-5s %-22s %s" % (f["level"], f["code"], f["message"]))
    log("estimation : %d conservés, %d à supprimer (%.1f %%) sur %d%s"
        % (summary["would_keep"], summary["would_remove"], summary["would_remove_percent"], summary["snapshots"],
           "" if opts.apply == "forget" else " — AUCUNE suppression effectuée (dry-run)"))

    blocking = [f for f in report["findings"] if f["level"] == "ERROR"
                and not (opts.resolve_interrupted and f["code"] == "operation_interrupted")]
    if blocking:
        if opts.apply:
            log("OPÉRATION REFUSÉE : contrôle non conforme ; rien n'a été modifié")
            return finish("apply_refused", EXIT_ANOMALY)
        return finish("anomaly", EXIT_ANOMALY)

    # Un état invalide produit toujours une anomalie (ci-dessus) : il n'est jamais remplacé.
    current_ids = sorted(sn["id"] for sn in snapshots)
    new_state = state_doc(started, snapshots, current_ids)
    if state is None:
        if not opts.init_baseline:
            log("RÉFÉRENCE ABSENTE : aucune référence créée%s. Après vérification, relancer manuellement avec"
                " --init-baseline." % (" ; aucune opération réelle" if opts.apply else ""))
            return finish("baseline_required", EXIT_BASELINE_REQUIRED)
        try:
            write_json_at(state_fd, "state.json", new_state, replace=False)
        except FileExistsError:
            log("référence apparue pendant l'exécution : elle n'est pas remplacée")
            return finish("baseline_exists", EXIT_BASELINE_EXISTS)
        report["baseline"] = "created"
        log("RÉFÉRENCE CRÉÉE : %d snapshots (%s). À valider avant de s'y fier."
            % (len(new_state["snapshot_ids"]), ", ".join("%s %d" % kv for kv in summary["per_group"].items())))
        return finish("baseline_created", EXIT_OK)

    if opts.resolve_interrupted:
        ip = state["in_progress"]
        write_json_at(state_fd, "state.json", new_state)
        log("OPÉRATION INTERROMPUE CLOSE : %s du %s ; repository sans verrou, check OK ; référence mise à jour"
            " (%d snapshots)" % (ip["operation"], ip["started"], len(current_ids)))
        return finish("interrupted_resolved", EXIT_OK)
    if not opts.apply:
        write_json_at(state_fd, "state.json", new_state)
        return finish("ok", EXIT_OK)
    return apply_operation(cfg, opts.apply, runner, state_fd, report, log, finish, started, snapshots,
                           removal if opts.apply == "forget" else [])


def record_timeout(report, log, exc):
    """Délai dépassé : consigne l'arrêt du conteneur ; s'il n'est pas confirmé, le signale explicitement."""
    log(str(exc))
    if exc.stop is None:
        return
    report["container_stop"] = exc.stop
    if not exc.stop["confirmed"]:
        report["findings"].append(finding("ERROR", "stop_unconfirmed",
                                          "conteneur %s : arrêt non confirmé, état %s ; vérifier « docker ps » et le"
                                          " démon Docker avant toute autre action" % (exc.stop["container"],
                                                                                    exc.stop["state"])))
        log("ERROR stop_unconfirmed : ARRÊT DU CONTENEUR %s NON CONFIRMÉ (%s)"
            % (exc.stop["container"], exc.stop["state"]))


def removal_problems(cfg, snapshots, removal):
    """Chaque suppression doit viser un identifiant complet, présent, d'un groupe déclaré et non figé."""
    by_id = {sn.get("id"): sn for sn in snapshots}
    problems = []
    for sid in removal:
        sn = by_id.get(sid)
        g = match_group(cfg["groups"], sn) if sn else None
        if not SNAPSHOT_ID_RE.match(sid or "") or sn is None:
            problems.append("%r : identifiant inconnu ou incomplet" % (sid,))
        elif g is None:
            problems.append("%s : hors des groupes déclarés" % sid[:8])
        elif g.get("frozen_ids") is not None:
            problems.append("%s : groupe figé %s" % (sid[:8], g["name"]))
        elif keep_tags_of(cfg, sn):
            problems.append("%s : porte le tag de protection %s" % (sid[:8], ",".join(keep_tags_of(cfg, sn))))
    if len(set(removal)) != len(removal):
        problems.append("identifiants en double dans la liste de suppression")
    return [finding("ERROR", "removal_not_allowed", p) for p in problems]


def apply_operation(cfg, op, runner, state_fd, report, log, finish, started, snapshots, removal):
    """Opération réelle, après un contrôle sans aucune erreur. La référence porte un marqueur
    in_progress pendant l'écriture : une interruption reste visible (operation_interrupted)."""
    before = {sn["id"] for sn in snapshots}
    times = {sn["id"]: sn.get("time", "?") for sn in snapshots}
    max_unused = "--max-unused=%s" % cfg["apply"]["prune_max_unused"]
    result = {"operation": op, "ok": False}
    report["apply"] = result
    if op == "forget":
        result["requested"] = sorted(removal)
        if not removal:
            write_json_at(state_fd, "state.json", state_doc(started, snapshots, before))
            log("forget : aucun snapshot à supprimer selon la politique ; rien n'a été modifié")
            result["ok"] = True
            return finish("nothing_to_remove", EXIT_OK)
    try:
        if op == "prune":
            result["dry_run"] = tail(runner.restic("prune", "--dry-run", max_unused))
            for line in result["dry_run"]:
                log("prune --dry-run | %s" % line)
        problems = readonly_problems(runner.mounts(write=True), write=True)
        if problems:
            for p in problems:
                log("montage refusé : %s" % p)
            return finish("unsafe_mounts", EXIT_CONFIG)
    except UnsafeMounts as exc:
        log(str(exc))
        return finish("unsafe_mounts", EXIT_CONFIG)
    except StepTimeout as exc:
        record_timeout(report, log, exc)
        return finish("timeout", EXIT_TIMEOUT)
    except (StepError, ValueError) as exc:
        log(str(exc))
        return finish("restic_error", EXIT_RESTIC)

    clean_state = state_doc(started, snapshots, before)
    if runner.deadline - time.monotonic() < STOP_GRACE_SECONDS:
        log("délai global presque épuisé avant l'écriture : opération non commencée")
        write_json_at(state_fd, "state.json", clean_state)
        return finish("timeout", EXIT_TIMEOUT)
    marker = dict(clean_state, in_progress={"operation": op, "started": started.isoformat(),
                                            "ids": sorted(removal) if op == "forget" else []})
    write_json_at(state_fd, "state.json", marker)

    def observe_removed():
        """Après un échec de forget : constat en lecture seule des suppressions effectives."""
        if op != "forget":
            return
        try:
            left = {sn["id"] for sn in parse_json_output(runner.restic("snapshots", "--json")) or []}
        except StepTimeout as exc:
            record_timeout(report, log, exc)
            log("constat impossible : délai dépassé")
            return
        except (StepError, UnsafeMounts, ValueError) as exc:
            log("constat impossible : %s" % exc)
            return
        result["removed"] = sorted(set(removal) - left)
        log("constat : %d/%d suppression(s) effectuée(s) avant l'échec" % (len(result["removed"]), len(removal)))

    try:
        if op == "forget":
            for sid in sorted(removal, key=lambda i: times[i]):
                log("SUPPRESSION DEMANDÉE : %s (%s)" % (sid[:8], times[sid]))
            output = runner.restic_write("forget", *sorted(removal))
        else:
            output = runner.restic_write("prune", max_unused)
        result["restic_output"] = tail(output)
    except ResticFailed as exc:
        log(str(exc))
        if exc.returncode == RESTIC_EXIT_LOCKED:
            # restic n'a pas pu verrouiller : il n'a rien modifié ; le marqueur est retiré.
            write_json_at(state_fd, "state.json", clean_state)
            log("repository verrouillé par un autre processus : rien n'a été modifié ; verrou laissé en place")
            return finish("repository_locked", EXIT_RESTIC)
        observe_removed()
        log("OPÉRATION INTERROMPUE : marqueur conservé dans la référence (voir « Opération interrompue »)")
        return finish("apply_failed", EXIT_RESTIC)
    except UnsafeMounts as exc:
        # Barrière du conteneur d'écriture : refus avant le lancement de restic, rien n'a été modifié.
        log(str(exc))
        write_json_at(state_fd, "state.json", clean_state)
        return finish("unsafe_mounts", EXIT_CONFIG)
    except StepTimeout as exc:
        record_timeout(report, log, exc)
        log("OPÉRATION INTERROMPUE : marqueur conservé dans la référence (voir « Opération interrompue »)")
        if exc.stop is not None and not exc.stop["confirmed"]:
            return finish("timeout_stop_unconfirmed", EXIT_TIMEOUT)
        return finish("timeout", EXIT_TIMEOUT)
    except StepError as exc:
        log(str(exc))
        observe_removed()
        log("OPÉRATION INTERROMPUE : marqueur conservé dans la référence (voir « Opération interrompue »)")
        return finish("apply_failed", EXIT_RESTIC)

    # Vérification après écriture, en lecture seule : ensemble exact des snapshots, verrous, structure.
    try:
        after_snapshots = parse_json_output(runner.restic("snapshots", "--json")) or []
        after = {sn["id"] for sn in after_snapshots}
        locks = runner.restic("list", "locks").split()
        runner.restic("check")
    except (StepError, StepTimeout, UnsafeMounts, ValueError) as exc:
        if isinstance(exc, StepTimeout):
            record_timeout(report, log, exc)
        else:
            log(str(exc))
        log("VÉRIFICATION APRÈS %s IMPOSSIBLE OU EN ÉCHEC : marqueur conservé" % op.upper())
        return finish("apply_unverified", EXIT_RESTIC)
    expected = before - set(removal)
    problems = []
    if set(removal) & after:
        problems.append("toujours présents : %s" % ", ".join(i[:8] for i in sorted(set(removal) & after)))
    if expected - after:
        problems.append("disparus sans avoir été demandés : %s" % ", ".join(i[:8] for i in sorted(expected - after)))
    if locks:
        problems.append("%d verrou(s) restant(s)" % len(locks))
    if problems:
        for p in problems:
            report["findings"].append(finding("ERROR", "apply_unexpected_result", p))
            log("ERROR apply_unexpected_result %s" % p)
        log("RÉSULTAT INATTENDU : marqueur conservé dans la référence (voir « Opération interrompue »)")
        return finish("apply_unexpected", EXIT_ANOMALY)

    if after - before:
        # Sauvegarde survenue entre-temps : non intégrée à la référence, le contrôle suivant la vérifiera.
        log("INFO  nouveau(x) snapshot(s) pendant l'opération, laissé(s) au contrôle suivant : %s"
            % ", ".join(i[:8] for i in sorted(after - before)))
    write_json_at(state_fd, "state.json", state_doc(started, after_snapshots, after & before))
    result["ok"] = True
    report["check_after"] = True
    if op == "forget":
        result["removed"] = sorted(removal)
        for sid in sorted(removal, key=lambda i: times[i]):
            log("SUPPRIMÉ : %s (%s)" % (sid[:8], times[sid]))
        log("forget : %d snapshot(s) supprimé(s), %d conservé(s) ; check OK ; référence mise à jour"
            % (len(removal), len(after)))
        return finish("forget_done", EXIT_OK)
    for line in result["restic_output"]:
        log("prune | %s" % line)
    log("prune : terminé ; %d snapshots inchangés ; check OK ; aucun verrou restant" % len(after))
    return finish("prune_done", EXIT_OK)


if __name__ == "__main__":
    sys.exit(main())
