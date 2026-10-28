#!/usr/bin/python3
"""Maintenance Restic locale sur le NAS — mode dry-run uniquement.

Exécuté en root par une tâche planifiée DSM. Restic tourne dans un conteneur
Docker (image épinglée), repository monté en lecture seule, sans réseau.
Le mot de passe est monté comme fichier : ce script ne le lit jamais.

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

# Sous-commandes restic autorisées et jetons interdits, quelles que soient les options.
ALLOWED_SUBCOMMANDS = ("version", "snapshots", "forget", "check", "list")
FORBIDDEN_TOKENS = ("--prune", "prune", "unlock", "repair", "rebuild-index", "key",
                    "init", "migrate", "tag", "rewrite", "backup", "copy", "--remove-all")

EXIT_OK = 0
EXIT_ANOMALY = 1
EXIT_CONFIG = 2
EXIT_WINDOW = 3
EXIT_RESTIC = 4
EXIT_TIMEOUT = 5
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


def validate_retention(retention):
    expected = {"keep_within", "keep_daily", "keep_weekly", "keep_monthly"}
    if not isinstance(retention, dict) or set(retention) != expected:
        raise ConfigError("retention doit contenir exactement %s" % ", ".join(sorted(expected)))
    within = retention["keep_within"]
    if not isinstance(within, str) or not re.match(r"^([0-9]{1,5}[ymdh]){1,4}$", within):
        raise ConfigError("keep_within invalide : %r (ex. 30d, 1y6m)" % (within,))
    for key in ("keep_daily", "keep_weekly", "keep_monthly"):
        if not is_int(retention[key], 0, 10000):
            raise ConfigError("%s invalide : %r (entier entre 0 et 10000)" % (key, retention[key]))


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


def write_json_at(dir_fd, name, data):
    """Écriture atomique relative à dir_fd : fichier temporaire unique (O_EXCL, O_NOFOLLOW), puis renommage."""
    tmp = ".%s.%s.tmp" % (name, os.urandom(8).hex())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
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
    if not isinstance(obj, dict) or set(obj) != {"last_run", "snapshot_ids"}:
        return "structure inattendue (clés last_run et snapshot_ids exactement)"
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
    if "--no-lock" not in args:
        raise StepError("--no-lock obligatoire (repository en lecture seule)")


def docker_base(cfg, container_name):
    """Options communes à tous les conteneurs : isolement et montages en lecture seule."""
    return [
        DOCKER_BIN, "run", "--rm", "--name", container_name,
        "--pull", "never",
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m",
        "--cap-drop", "ALL", "--cap-add", "DAC_READ_SEARCH",
        "--security-opt", "no-new-privileges",
        "-v", "%s:%s:ro" % (cfg["repository"], CONTAINER_REPO),
        "-v", "%s:%s:ro" % (cfg["password_file"], CONTAINER_PASSWORD),
        "-e", "RESTIC_REPOSITORY=%s" % CONTAINER_REPO,
        "-e", "RESTIC_PASSWORD_FILE=%s" % CONTAINER_PASSWORD,
        "-e", "TMPDIR=/tmp",
    ]


def docker_command(cfg, container_name, restic_args):
    return docker_base(cfg, container_name) + [cfg["image"]] + restic_args


def mounts_command(cfg, container_name):
    """Même conteneur, mais affiche ses points de montage au lieu de lancer restic."""
    return docker_base(cfg, container_name) + ["--entrypoint", "/bin/cat", cfg["image"], "/proc/self/mounts"]


def readonly_problems(mounts_text):
    """Vérifie, dans le conteneur réel, que la racine, le repository et le secret sont en lecture seule."""
    options = {}
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            options[fields[1]] = fields[3].split(",")
    problems = []
    for mountpoint in ("/", CONTAINER_REPO, CONTAINER_PASSWORD):
        if mountpoint not in options:
            problems.append("%s : point de montage absent" % mountpoint)
        elif "ro" not in options[mountpoint]:
            problems.append("%s : monté sans « ro » (%s)" % (mountpoint, ",".join(options[mountpoint])))
    return problems


def parse_restic_version(output):
    m = re.match(r"^restic (\d+\.\d+\.\d+)\b", output.strip())
    return m.group(1) if m else None


class Runner:
    def __init__(self, cfg, deadline, log):
        self.cfg = cfg
        self.deadline = deadline
        self.log = log
        self.counter = 0

    def _name(self):
        self.counter += 1
        return "restic-maint-%s-%d-%d" % (re.sub(r"[^a-z0-9-]", "-", self.cfg["name"]), os.getpid(), self.counter)

    def _exec(self, cmd, name, label):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise StepTimeout("délai global dépassé avant « %s »" % label)
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=remaining)
        except subprocess.TimeoutExpired:
            subprocess.run([DOCKER_BIN, "kill", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            raise StepTimeout("délai global dépassé pendant « %s »" % label)
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            raise StepError("%s a échoué (code %d) : %s" % (label, proc.returncode, stderr[-2000:]))
        return proc.stdout.decode("utf-8", "replace")

    def mounts(self):
        name = self._name()
        self.log("vérification des montages du conteneur")
        return self._exec(mounts_command(self.cfg, name), name, "lecture des montages")

    def restic(self, subcommand, *extra):
        args = build_restic_args(subcommand, extra)
        name = self._name()
        self.log("restic %s" % " ".join(args))
        return self._exec(docker_command(self.cfg, name, args), name, "restic %s" % subcommand)


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
    ]


# --------------------------------------------------------------------------- analyse

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


def analyze(cfg, snapshots, forget_groups, now, state, state_error=None):
    """Analyse pure : ne fait aucune entrée/sortie. Retourne (findings, summary)."""
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
        if "tags" in g and sorted(sn.get("tags") or []) != sorted(g["tags"]):
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
        gone = sorted(previous - ids)
        if gone:
            findings.append(finding("ERROR", "snapshots_disappeared",
                                    "%d snapshot(s) disparu(s) depuis la dernière exécution : %s"
                                    % (len(gone), ", ".join(g[:8] for g in gone[:10]))))
        for sn in snapshots:
            if sn.get("id") in previous:
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
        findings.append(finding("INFO", "baseline", "pas d'état précédent : la référence sera créée"))

    # Estimation des suppressions (forget --dry-run).
    total = len(snapshots)
    removed = sum(len(g.get("remove") or []) for g in forget_groups)
    kept = sum(len(g.get("keep") or []) for g in forget_groups)
    if kept + removed != total:
        findings.append(finding("ERROR", "forget_mismatch",
                                "forget couvre %d snapshots, le repository en contient %d" % (kept + removed, total)))
    percent = (100.0 * removed / total) if total else 0.0
    if removed > th["max_remove_count"]:
        findings.append(finding("ERROR", "remove_count", "%d suppressions estimées (max %d)" % (removed, th["max_remove_count"])))
    if percent > th["max_remove_percent"]:
        findings.append(finding("ERROR", "remove_percent", "%.1f %% de suppressions estimées (max %d %%)" % (percent, th["max_remove_percent"])))
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
    parser = argparse.ArgumentParser(description="Maintenance Restic (dry-run uniquement)")
    parser.add_argument("--config", required=True)
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


def run(cfg, opts, log, lines, state_fd, log_fd):
    """Exécution sous verrou exclusif ; état et rapports relatifs aux descripteurs vérifiés."""
    started = dt.datetime.now(dt.timezone.utc)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    report = {"name": cfg["name"], "mode": cfg["mode"], "image": cfg["image"],
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

    log("maintenance %s — mode %s — image %s" % (cfg["name"], cfg["mode"], cfg["image"]))
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

    runner = Runner(cfg, time.monotonic() + 60 * cfg["timeout_minutes"], log)
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
            report["findings"].append(finding("WARN", "locks", "%d verrou(s) présent(s) dans le repository" % len(locks)))
        if cfg.get("run_check", True):
            runner.restic("check")
            log("check : OK")
        forget_groups = parse_json_output(runner.restic("forget", *forget_args(cfg["retention"]))) or []
    except StepTimeout as exc:
        log(str(exc))
        return finish("timeout", EXIT_TIMEOUT)
    except (StepError, ValueError) as exc:
        log(str(exc))
        return finish("restic_error", EXIT_RESTIC)

    state, state_error = read_state(state_fd, started,
                                    dt.timedelta(minutes=cfg["thresholds"]["max_future_skew_minutes"]))
    findings, summary = analyze(cfg, snapshots, forget_groups, started, state, state_error)
    report["findings"].extend(findings)
    report["summary"] = summary

    for f in report["findings"]:
        log("%-5s %-22s %s" % (f["level"], f["code"], f["message"]))
    log("estimation : %d conservés, %d à supprimer (%.1f %%) sur %d — AUCUNE suppression effectuée (dry-run)"
        % (summary["would_keep"], summary["would_remove"], summary["would_remove_percent"], summary["snapshots"]))

    if any(f["level"] == "ERROR" for f in report["findings"]):
        return finish("anomaly", EXIT_ANOMALY)

    # La référence n'est créée ou mise à jour qu'après une exécution sans anomalie
    # (un état invalide produit une anomalie : il n'est donc jamais remplacé).
    write_json_at(state_fd, "state.json", {"last_run": started.isoformat(),
                                           "snapshot_ids": sorted(sn["id"] for sn in snapshots)})
    return finish("ok", EXIT_OK)


if __name__ == "__main__":
    sys.exit(main())
