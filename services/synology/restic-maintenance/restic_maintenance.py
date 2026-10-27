#!/usr/bin/python3
"""Maintenance Restic locale sur le NAS — mode dry-run uniquement.

Exécuté en root par une tâche planifiée DSM. Restic tourne dans un conteneur
Docker (image épinglée), repository monté en lecture seule, sans réseau.
Le mot de passe est monté comme fichier : ce script ne le lit jamais.

Compatible Python 3.8 (version fournie par DSM 7).
"""

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
import time

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

REQUIRED_KEYS = ("name", "mode", "image", "repository", "password_file", "state_dir",
                 "log_dir", "groups", "retention", "window",
                 "timeout_minutes", "thresholds")


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
    for key in ("repository", "password_file", "state_dir", "log_dir"):
        if not os.path.isabs(cfg[key]):
            raise ConfigError("%s doit être un chemin absolu" % key)
    validate_groups(cfg["groups"])
    if not re.match(r"^[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9._-]+(@sha256:[0-9a-f]{64})?$", cfg["image"]):
        raise ConfigError("image invalide : %r" % cfg["image"])
    parse_window(cfg["window"])
    return cfg


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


def docker_command(cfg, container_name, restic_args):
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
        cfg["image"],
    ] + restic_args


class Runner:
    def __init__(self, cfg, deadline, log):
        self.cfg = cfg
        self.deadline = deadline
        self.log = log
        self.counter = 0

    def restic(self, subcommand, *extra):
        args = build_restic_args(subcommand, extra)
        self.counter += 1
        name = "restic-maint-%s-%d-%d" % (re.sub(r"[^a-z0-9-]", "-", self.cfg["name"]), os.getpid(), self.counter)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise StepTimeout("délai global dépassé avant « restic %s »" % subcommand)
        self.log("restic %s" % " ".join(args))
        try:
            proc = subprocess.run(docker_command(self.cfg, name, args), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=remaining)
        except subprocess.TimeoutExpired:
            subprocess.run([DOCKER_BIN, "kill", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            raise StepTimeout("délai global dépassé pendant « restic %s »" % subcommand)
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            raise StepError("restic %s a échoué (code %d) : %s" % (subcommand, proc.returncode, stderr[-2000:]))
        return proc.stdout.decode("utf-8", "replace")


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


def analyze(cfg, snapshots, forget_groups, now, state):
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
    if state:
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

def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def rotate_reports(log_dir, keep):
    reports = sorted(f for f in os.listdir(log_dir) if re.match(r"^report-\d{8}-\d{6}\.json$", f))
    for old in reports[:-keep] if keep > 0 else []:
        os.remove(os.path.join(log_dir, old))


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

    for d in (cfg["state_dir"], cfg["log_dir"]):
        os.makedirs(d, mode=0o700, exist_ok=True)

    with open(os.path.join(cfg["state_dir"], "lock"), "w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("une maintenance %s est déjà en cours" % cfg["name"], file=sys.stderr)
            return EXIT_LOCKED
        return run(cfg, opts, log, lines)


def run(cfg, opts, log, lines):
    """Exécution sous verrou exclusif."""
    started = dt.datetime.now(dt.timezone.utc)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    report = {"name": cfg["name"], "mode": cfg["mode"], "image": cfg["image"],
              "started": started.isoformat(), "findings": [], "summary": None, "status": None}

    def finish(status, code):
        report["status"] = status
        report["finished"] = dt.datetime.now(dt.timezone.utc).isoformat()
        report["log"] = lines
        write_json_atomic(os.path.join(cfg["log_dir"], "report-%s.json" % stamp), report)
        rotate_reports(cfg["log_dir"], cfg.get("keep_reports", 90))
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
        log(runner.restic("version").strip())
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

    state_path = os.path.join(cfg["state_dir"], "state.json")
    findings, summary = analyze(cfg, snapshots, forget_groups, started, load_state(state_path))
    report["findings"].extend(findings)
    report["summary"] = summary

    for f in report["findings"]:
        log("%-5s %-22s %s" % (f["level"], f["code"], f["message"]))
    log("estimation : %d conservés, %d à supprimer (%.1f %%) sur %d — AUCUNE suppression effectuée (dry-run)"
        % (summary["would_keep"], summary["would_remove"], summary["would_remove_percent"], summary["snapshots"]))

    if any(f["level"] == "ERROR" for f in report["findings"]):
        return finish("anomaly", EXIT_ANOMALY)

    # La référence n'est mise à jour qu'après une exécution sans anomalie.
    write_json_atomic(state_path, {"last_run": started.isoformat(),
                                   "snapshot_ids": sorted(sn["id"] for sn in snapshots)})
    return finish("ok", EXIT_OK)


if __name__ == "__main__":
    sys.exit(main())
