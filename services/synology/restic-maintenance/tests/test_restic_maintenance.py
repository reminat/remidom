"""Tests du script de maintenance — sans Docker, sans restic, sans NAS.

    python3 -m unittest discover -s services/synology/restic-maintenance/tests -v
"""

import datetime as dt
import fcntl
import json
import re
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, SCRIPT_DIR)
import restic_maintenance as rm  # noqa: E402

IMAGE = "restic/restic@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510"
NOW = dt.datetime(2026, 10, 27, 2, 0, tzinfo=dt.timezone.utc)
MOUNTS_OK = """overlay / overlay ro,relatime,lowerdir=/x 0 0
tmpfs /tmp tmpfs rw,nosuid,noexec,relatime,size=524288k 0 0
/dev/mapper/vg1-volume_1 /repo btrfs ro,relatime,space_cache=v2 0 0
/dev/mapper/vg1-volume_1 /run/secrets/restic_password btrfs ro,relatime 0 0
"""
HOST = "docker-test-01"
PATHS = ["/srv/docker/core"]
TAGS = ["core", "test"]
VALIDATION_ID = "1e30db7a" + "c" * 56
GROUPS = [
    {"name": "core", "host": HOST, "paths": PATHS, "tags": TAGS},
    {"name": "validation", "host": HOST, "paths": ["/tmp/restic-test"], "tags": ["validation"],
     "frozen_ids": ["1e30db7a"]},
]


def cfg(**overrides):
    base = {
        "name": "docker-test", "mode": "dry-run", "image": IMAGE, "expected_restic_version": "0.19.1",
        "repository": "/volume1/Restic/docker-test",
        "password_file": "/volume1/docker/restic-maintenance/secrets/restic_password_test",
        "private_dir": "/root/restic-maintenance/docker-test",
        "groups": GROUPS,
        "retention": {"keep_within": "30d", "keep_daily": 14, "keep_weekly": 8, "keep_monthly": 12},
        "window": {"start": "01:00", "end": "04:30"},
        "timeout_minutes": 120,
        "thresholds": {"max_future_skew_minutes": 60, "max_age_hours": 48, "max_snapshots_per_24h": 6,
                       "backdate_tolerance_hours": 24, "max_remove_count": 10,
                       "max_remove_percent": 20, "min_keep_per_group": 7},
    }
    base.update(overrides)
    return base


def snap(sid, when, host=HOST, paths=None, tags=None):
    return {"id": sid * 8 if len(sid) == 1 else sid, "time": when.strftime("%Y-%m-%dT%H:%M:%S.123456789+00:00"),
            "hostname": host, "paths": paths or PATHS, "tags": TAGS if tags is None else tags}


def daily(n, end=NOW - dt.timedelta(hours=1)):
    return [snap("%064x" % i, end - dt.timedelta(days=i)) for i in range(n)]


def validation(when=NOW - dt.timedelta(days=50)):
    return snap(VALIDATION_ID, when, paths=["/tmp/restic-test"], tags=["validation"])


def groups(keep, remove=(), paths=PATHS):
    return [{"host": HOST, "paths": paths, "tags": None, "keep": list(keep), "remove": list(remove)}]


def real_repo(end=NOW - dt.timedelta(hours=1)):
    """Contenu actuel : 38 snapshots core (tags core,test) + le snapshot de validation."""
    core = daily(38, end)
    v = validation()
    forget = groups(core) + groups([v], paths=["/tmp/restic-test"])
    return core + [v], forget


def codes(findings, level="ERROR"):
    return sorted(f["code"] for f in findings if f["level"] == level)


class TrustedUser:
    """En test, l'utilisateur courant remplace root comme propriétaire de confiance."""

    def setUp(self):
        self._uids, rm.TRUSTED_UIDS = rm.TRUSTED_UIDS, (0, os.geteuid())
        self.addCleanup(setattr, rm, "TRUSTED_UIDS", self._uids)
        super().setUp()


class AnalyzeTest(unittest.TestCase):
    def test_clean_repository(self):
        s = daily(20)
        f, summary = rm.analyze(cfg(groups=GROUPS[:1]), s, groups(s), NOW, None)
        self.assertEqual(codes(f), [])
        self.assertEqual(codes(f, "INFO"), ["baseline"])
        self.assertEqual(summary["would_remove"], 0)

    def test_real_repository_is_clean(self):
        s, forget = real_repo()
        f, summary = rm.analyze(cfg(), s, forget, NOW, None)
        self.assertEqual(codes(f), [])
        self.assertEqual(summary["per_group"], {"core": 38, "validation": 1})

    def test_unknown_host(self):
        s = daily(10) + [snap("a", NOW - dt.timedelta(hours=2), host="intrus")]
        f, _ = rm.analyze(cfg(groups=GROUPS[:1]), s, groups(s), NOW, None)
        self.assertEqual(codes(f), ["host_not_allowed"])

    def test_paths_must_match_exactly(self):
        for paths in (["/srv/docker"], ["/srv/docker/core/sub"], ["/srv/docker/core", "/etc"], ["/etc"]):
            s = daily(10) + [snap("a", NOW - dt.timedelta(hours=2), paths=paths)]
            f, _ = rm.analyze(cfg(groups=GROUPS[:1]), s, groups(s), NOW, None)
            self.assertEqual(codes(f), ["path_not_allowed"], paths)

    def test_tags_mismatch(self):
        s = daily(10) + [snap("a", NOW - dt.timedelta(hours=2), tags=["core"])]
        f, _ = rm.analyze(cfg(groups=GROUPS[:1]), s, groups(s), NOW, None)
        self.assertEqual(codes(f), ["tags_mismatch"])

    def test_new_snapshot_in_frozen_group(self):
        s, forget = real_repo()
        extra = snap("e", NOW - dt.timedelta(hours=3), paths=["/tmp/restic-test"], tags=["validation"])
        f, _ = rm.analyze(cfg(), s + [extra], forget + groups([extra], paths=["/tmp/restic-test"]), NOW, None)
        self.assertEqual(codes(f), ["frozen_group_changed"])

    def test_frozen_snapshot_missing(self):
        core = daily(38)
        f, _ = rm.analyze(cfg(), core, groups(core), NOW, None)
        self.assertEqual(codes(f), ["frozen_snapshot_missing"])

    def test_validation_does_not_mask_stale_core(self):
        # Core arrêté depuis 3 jours, validation présente : la fraîcheur doit échouer sur core seulement.
        s, forget = real_repo(end=NOW - dt.timedelta(days=3))
        f, _ = rm.analyze(cfg(), s, forget, NOW, None)
        self.assertEqual(codes(f), ["stale"])
        self.assertIn("groupe core", [x for x in f if x["code"] == "stale"][0]["message"])

    def test_old_validation_never_stale(self):
        s, forget = real_repo()
        self.assertGreater(NOW - rm.parse_time(s[-1]["time"]), dt.timedelta(days=49))  # validation vieille de 50 j
        f, _ = rm.analyze(cfg(), s, forget, NOW, None)
        self.assertNotIn("stale", codes(f))

    def test_validation_not_counted_in_frequency(self):
        # 6 snapshots core dans la même journée (au seuil) + validation ce même jour : pas de rafale.
        day = NOW - dt.timedelta(days=10)
        core = daily(5) + [snap("%064x" % (500 + i), day + dt.timedelta(hours=i)) for i in range(6)]
        v = validation(when=day + dt.timedelta(hours=2, minutes=30))
        forget = groups(core) + groups([v], paths=["/tmp/restic-test"])
        f, _ = rm.analyze(cfg(), core + [v], forget, NOW, None)
        self.assertEqual(codes(f), [])
        # Un 7e snapshot core ce jour-là dépasse bien le seuil.
        core.append(snap("%064x" % 600, day + dt.timedelta(hours=7)))
        f, _ = rm.analyze(cfg(), core + [v], groups(core) + groups([v], paths=["/tmp/restic-test"]), NOW, None)
        self.assertEqual(codes(f), ["burst"])

    def test_future_snapshot(self):
        s = daily(10) + [snap("f", NOW + dt.timedelta(days=400))]
        f, _ = rm.analyze(cfg(groups=GROUPS[:1]), s, groups(s), NOW, None)
        self.assertIn("future_snapshot", codes(f))

    def test_small_clock_skew_tolerated(self):
        s = daily(10) + [snap("f", NOW + dt.timedelta(minutes=30))]
        f, _ = rm.analyze(cfg(groups=GROUPS[:1]), s, groups(s), NOW, None)
        self.assertNotIn("future_snapshot", codes(f))

    def test_missing_group(self):
        s = daily(10)
        extra = {"name": "other", "host": "docker-test-02", "paths": PATHS, "tags": TAGS}
        f, _ = rm.analyze(cfg(groups=GROUPS[:1] + [extra]), s, groups(s), NOW, None)
        self.assertEqual(codes(f), ["no_snapshot"])

    def test_burst_detected_anywhere_in_history(self):
        old = NOW - dt.timedelta(days=200)
        s = daily(10) + [snap("%064x" % (900 + i), old + dt.timedelta(minutes=i)) for i in range(7)]
        f, _ = rm.analyze(cfg(groups=GROUPS[:1]), s, groups(s), NOW, None)
        self.assertIn("burst", codes(f))

    def test_backdated_and_disappeared_snapshots(self):
        s = daily(10)
        state = {"last_run": (NOW - dt.timedelta(days=1)).isoformat(),
                 "snapshot_ids": [x["id"] for x in s] + ["d" * 64]}
        forged = snap("b", NOW - dt.timedelta(days=5))
        f, _ = rm.analyze(cfg(groups=GROUPS[:1]), s + [forged], groups(s + [forged]), NOW, state)
        self.assertEqual(codes(f), ["backdated_snapshot", "snapshots_disappeared"])

    def test_new_snapshot_after_last_run_is_fine(self):
        s = daily(10)
        state = {"last_run": (NOW - dt.timedelta(days=1)).isoformat(), "snapshot_ids": [x["id"] for x in s[1:]]}
        f, _ = rm.analyze(cfg(groups=GROUPS[:1]), s, groups(s), NOW, state)
        self.assertEqual(codes(f), [])

    def test_removal_thresholds_are_warnings_in_dry_run(self):
        # 15 candidats anciens (45 à 59 jours) sur 60 : au-delà des seuils (10, 20 %), sans anomalie en dry-run.
        s = daily(60)
        f, summary = rm.analyze(cfg(groups=GROUPS[:1]), s, groups(s[:45], s[45:]), NOW, None)
        self.assertEqual(codes(f), [])
        self.assertEqual(codes(f, "WARN"), ["remove_count", "remove_percent"])
        self.assertEqual(summary["would_remove"], 15)

    def test_min_keep_per_group(self):
        recent = daily(5)
        old = [snap("%064x" % (700 + i), NOW - dt.timedelta(days=35 + i)) for i in range(5)]
        f, _ = rm.analyze(cfg(groups=GROUPS[:1]), recent + old, groups(recent, old), NOW, None)
        self.assertEqual(codes(f), ["min_keep"])

    def test_validation_group_kept_alone_is_not_min_keep(self):
        # Le groupe de validation ne contient qu'un snapshot, conservé : pas d'alerte min_keep.
        s, forget = real_repo()
        f, _ = rm.analyze(cfg(), s, forget, NOW, None)
        self.assertNotIn("min_keep", codes(f))

    def test_forget_must_cover_all_snapshots(self):
        s = daily(10)
        f, _ = rm.analyze(cfg(groups=GROUPS[:1]), s, groups(s[:8]), NOW, None)
        self.assertIn("forget_mismatch", codes(f))


def at(sid, when, paths=None, tags=None):
    """Snapshot dont l'horodatage garde son décalage d'origine (comme dans le repository)."""
    return {"id": sid, "time": when, "hostname": HOST, "paths": paths or PATHS, "tags": TAGS if tags is None else tags}


def hexid(label):
    import hashlib
    return hashlib.sha256(label.encode()).hexdigest()


def paris_0302(day):
    """Sauvegarde planifiée à 03:02, heure de Paris (CEST +02:00 jusqu'au 25/10/2026, puis CET +01:00)."""
    offset = "+01:00" if day >= dt.date(2026, 10, 25) else "+02:00"
    return "%sT03:02:00.123456789%s" % (day.isoformat(), offset)


def observed_repository(manual=True):
    """Modèle du repository docker-test lors des deux dry-runs du 28/10/2026 : sauvegardes quotidiennes
    core du 20/09 au 28/10 (39) + sauvegarde manuelle 2d8aab12 (second dry-run) + snapshot de validation.
    Candidats observés : core du 21 au 26/09 et du 28/09 ; tous les autres sont conservés."""
    days = [dt.date(2026, 9, 20) + dt.timedelta(days=i) for i in range(39)]
    core = [at(hexid(d.isoformat()), paris_0302(d)) for d in days]
    if manual:
        core.append(at("2d8aab12" + hexid("manuel")[8:], "2026-10-28T12:41:07.5+01:00"))
    removed_days = {dt.date(2026, 9, n) for n in (21, 22, 23, 24, 25, 26, 28)}
    remove = [s for s, d in zip(core, days) if d in removed_days]
    keep = [s for s in core if s not in remove]
    valid = at(VALIDATION_ID, "2026-09-12T18:04:00+02:00", paths=["/tmp/restic-test"], tags=["validation"])
    forget = [{"host": HOST, "paths": PATHS, "tags": None, "keep": keep, "remove": remove},
              {"host": HOST, "paths": ["/tmp/restic-test"], "tags": None, "keep": [valid], "remove": []}]
    return core + [valid], forget


def utc(text):
    return rm.parse_time_with_offset(text).astimezone(dt.timezone.utc)


class KeepWithinGuardTest(unittest.TestCase):
    def group(self, keep, remove):
        return [{"host": HOST, "paths": PATHS, "keep": keep, "remove": remove}]

    def young(self, keep, remove, now, within="30d"):
        return [f["message"][:8] for f in rm.young_removals(self.group(keep, remove), within, utc(now))]

    def test_observed_candidates_second_run(self):
        snaps, forget = observed_repository(manual=True)
        now = utc("2026-10-28T12:50:00+01:00")
        self.assertEqual(rm.young_removals(forget, "30d", now), [])
        removed = sorted(s["time"][:10] for g in forget for s in g["remove"])
        self.assertEqual(removed, ["2026-09-%02d" % d for d in (21, 22, 23, 24, 25, 26, 28)])
        # dans analyze aussi : aucune anomalie due aux candidats
        f, summary = rm.analyze(cfg(), snaps, forget, now, None, policy_now=now)
        self.assertNotIn("young_removal", codes(f))
        self.assertEqual((summary["would_keep"], summary["would_remove"]), (34, 7))

    def test_observed_candidates_first_run(self):
        # Sans la sauvegarde manuelle : référence = 28/10 03:02+01:00 = 02:02Z ; limite = 28/09 03:02+01:00 = 02:02Z.
        # Le candidat du 28/09 (03:02+02:00 = 01:02Z) est antérieur d'une heure : suppression conforme.
        snaps, forget = observed_repository(manual=False)
        self.assertEqual(rm.young_removals(forget, "30d", utc("2026-10-28T03:30:00+01:00")), [])

    def test_29_september_would_be_flagged(self):
        snaps, forget = observed_repository(manual=True)
        sept29 = [s for s in forget[0]["keep"] if s["time"].startswith("2026-09-29")]
        forget[0]["remove"] = forget[0]["remove"] + sept29
        forget[0]["keep"] = [s for s in forget[0]["keep"] if s not in sept29]
        found = rm.young_removals(forget, "30d", utc("2026-10-28T12:50:00+01:00"))
        self.assertEqual([f["code"] for f in found], ["young_removal"])
        self.assertIn("2026-09-29", found[0]["message"])

    def test_offsets_are_compared_as_instants(self):
        # Référence 28/10 03:02+01:00 (02:02Z) ; limite 28/09 02:02Z. Un candidat 28/09 03:30+02:00 (01:30Z)
        # précède la limite : conforme, alors qu'une comparaison d'heures locales (03:30 > 03:02) le signalerait.
        keep = [at("a" * 64, "2026-10-28T03:02:00+01:00")]
        self.assertEqual(self.young(keep, [at("b" * 64, "2026-09-28T03:30:00+02:00")], "2026-10-28T04:00:00+01:00"), [])
        self.assertEqual(self.young(keep, [at("c" * 64, "2026-09-28T02:05:00Z")], "2026-10-28T04:00:00+01:00"), ["c" * 8])

    def test_exact_cutoff_is_not_kept(self):
        keep = [at("a" * 64, "2026-10-28T03:02:00+01:00")]
        now = "2026-10-28T04:00:00+01:00"
        self.assertEqual(self.young(keep, [at("b" * 64, "2026-09-28T03:02:00+01:00")], now), [])   # = limite
        self.assertEqual(self.young(keep, [at("c" * 64, "2026-09-28T03:02:01+01:00")], now), ["c" * 8])

    def test_group_with_old_latest(self):
        # Sauvegardes arrêtées depuis 60 jours : la limite suit le dernier snapshot du groupe, pas l'heure actuelle.
        keep = [at("a" * 64, "2026-08-29T03:02:00+02:00")]
        now = "2026-10-28T12:00:00+01:00"
        self.assertEqual(self.young(keep, [at("b" * 64, "2026-07-20T03:02:00+02:00")], now), [])
        self.assertEqual(self.young(keep, [at("c" * 64, "2026-08-10T03:02:00+02:00")], now), ["c" * 8])

    def test_future_snapshots_ignored_for_latest(self):
        keep = [at("a" * 64, "2026-10-28T03:02:00+01:00"), at("f" * 64, "2030-01-01T00:00:00Z")]
        remove = [at("b" * 64, "2026-09-20T03:02:00+02:00")]
        self.assertEqual(self.young(keep, remove, "2026-10-28T12:00:00+01:00"), [])

    def test_no_past_snapshot_means_any_removal_is_flagged(self):
        keep = [at("f" * 64, "2030-01-01T00:00:00Z")]
        self.assertEqual(self.young(keep, [at("b" * 64, "2026-01-01T00:00:00Z")], "2026-10-28T12:00:00+01:00"), ["b" * 8])

    def test_snapshot_at_policy_now_is_not_reference(self):
        # restic : référence strictement antérieure à son heure d'exécution
        keep = [at("a" * 64, "2026-10-28T12:00:00+01:00"), at("d" * 64, "2026-10-01T12:00:00+02:00")]
        remove = [at("b" * 64, "2026-09-15T12:00:00+02:00")]
        self.assertEqual(self.young(keep, remove, "2026-10-28T12:00:00+01:00"), ["b" * 8])
        self.assertEqual(self.young(keep, remove, "2026-10-28T12:00:01+01:00"), [])

    def test_months_and_hours(self):
        keep = [at("a" * 64, "2026-03-31T10:00:00+02:00")]
        now = "2026-04-01T00:00:00+02:00"
        # Go AddDate : 31/03 - 1 mois = 31/02, normalisé en 03/03
        self.assertEqual(self.young(keep, [at("b" * 64, "2026-03-03T10:00:00+02:00")], now, "1m"), [])
        self.assertEqual(self.young(keep, [at("c" * 64, "2026-03-03T10:00:01+02:00")], now, "1m"), ["c" * 8])
        keep = [at("a" * 64, "2026-10-28T12:00:00+01:00")]
        now = "2026-10-28T13:00:00+01:00"
        self.assertEqual(self.young(keep, [at("b" * 64, "2026-10-27T00:00:00+01:00")], now, "1d12h"), [])
        self.assertEqual(self.young(keep, [at("c" * 64, "2026-10-27T00:00:01+01:00")], now, "1d12h"), ["c" * 8])

    def test_go_add_date(self):
        def add(text, y, m, d):
            return rm.go_add_date(dt.datetime.fromisoformat(text), y, m, d).isoformat()
        self.assertEqual(add("2026-03-31T10:00:00+02:00", 0, -1, 0), "2026-03-03T10:00:00+02:00")
        self.assertEqual(add("2024-02-29T00:00:00+00:00", 1, 0, 0), "2025-03-01T00:00:00+00:00")
        self.assertEqual(add("2026-01-31T00:00:00+01:00", 0, 1, 0), "2026-03-03T00:00:00+01:00")
        self.assertEqual(add("2026-01-05T08:00:00+01:00", 0, 0, -30), "2025-12-06T08:00:00+01:00")
        self.assertEqual(add("2026-10-28T03:02:00+01:00", 0, 0, -30), "2026-09-28T03:02:00+01:00")

    def test_zero_duration_disables_guard(self):
        keep = [at("a" * 64, "2026-10-28T03:02:00+01:00")]
        self.assertEqual(self.young(keep, [at("b" * 64, "2026-10-27T03:02:00+01:00")], "2026-10-28T04:00:00+01:00", "0d"), [])

    def test_invalid_time_in_forget_output(self):
        found = rm.young_removals(self.group([], [{"id": "b" * 64, "time": "hier"}]), "30d", utc("2026-10-28T04:00:00+01:00"))
        self.assertEqual([f["code"] for f in found], ["forget_time_invalid"])

    def test_parse_duration(self):
        self.assertEqual(rm.parse_duration("1y6m"), {"y": 1, "m": 6, "d": 0, "h": 0})
        self.assertEqual(rm.parse_duration("1d12h"), {"y": 0, "m": 0, "d": 1, "h": 12})
        for bad in ("1d2d", "30", "d", "30x", "", "30d ", "-30d", 30):
            with self.assertRaises(ValueError, msg=bad):
                rm.parse_duration(bad)


class SafetyTest(unittest.TestCase):
    def test_forget_requires_dry_run(self):
        with self.assertRaises(rm.StepError):
            rm.check_safe(["--no-lock", "forget", "--keep-daily", "14"])

    def test_destructive_commands_refused(self):
        for args in (["--no-lock", "prune"], ["--no-lock", "unlock"],
                     ["--no-lock", "forget", "--dry-run", "--prune"], ["--no-lock", "key", "list"]):
            with self.assertRaises(rm.StepError, msg=args):
                rm.check_safe(args)

    def test_no_lock_mandatory(self):
        with self.assertRaises(rm.StepError):
            rm.check_safe(["snapshots", "--json"])

    def test_forget_arguments(self):
        args = rm.build_restic_args("forget", rm.forget_args(cfg()["retention"]))
        self.assertEqual(args[:3], ["--no-lock", "--no-cache", "forget"])
        for expected in ("--dry-run", "--json", "30d", "--keep-daily", "14", "--keep-weekly", "8",
                         "--keep-monthly", "12", "host,paths"):
            self.assertIn(expected, args)

    def test_docker_isolation(self):
        c = rm.docker_command(cfg(), "n", ["--no-lock", "snapshots"])
        joined = " ".join(c)
        self.assertIn("/volume1/Restic/docker-test:/repo:ro", joined)
        self.assertEqual(c[c.index("--entrypoint"):], ["--entrypoint", "/bin/sh", IMAGE, "-c", rm.GATE_SCRIPT,
                                                       "restic-gate", "--no-lock", "snapshots"])
        self.assertIn("restic_password_test:/run/secrets/restic_password:ro", joined)
        for flag in ("--network none", "--read-only", "--cap-drop ALL", "--pull never", "no-new-privileges"):
            self.assertIn(flag, joined)
        self.assertNotIn("RESTIC_PASSWORD=", joined)


class MountAndVersionTest(unittest.TestCase):
    def test_readonly_mounts_ok(self):
        self.assertEqual(rm.readonly_problems(MOUNTS_OK), [])

    def test_readonly_mounts_problems(self):
        self.assertEqual(len(rm.readonly_problems(MOUNTS_OK.replace("overlay / overlay ro,", "overlay / overlay rw,"))), 1)
        missing = "\n".join(l for l in MOUNTS_OK.splitlines() if "restic_password" not in l)
        self.assertEqual(rm.readonly_problems(missing), ["/run/secrets/restic_password : point de montage absent"])
        # « ro » doit être une option entière, pas une sous-chaîne (ex. « errors=remount-ro »)
        tricky = MOUNTS_OK.replace("/repo btrfs ro,relatime", "/repo btrfs rw,errors=remount-ro")
        self.assertEqual(len(rm.readonly_problems(tricky)), 1)

    def test_submounts_under_repo(self):
        rw_sub = MOUNTS_OK + "/dev/sdb1 /repo/data ext4 rw,relatime 0 0\n"
        self.assertEqual(rm.readonly_problems(rw_sub), ["/repo/data : monté sans « ro » (rw,relatime)"])
        deep = MOUNTS_OK + "/dev/sdb1 /repo/a/b/c ext4 rw 0 0\n"
        self.assertEqual(len(rm.readonly_problems(deep)), 1)
        spaced = MOUNTS_OK + "/dev/sdb1 /repo/a\\040b ext4 rw 0 0\n"
        self.assertEqual(rm.readonly_problems(spaced), ["/repo/a b : monté sans « ro » (rw)"])
        self.assertEqual(rm.readonly_problems(MOUNTS_OK + "/dev/sdb1 /repo/data ext4 ro 0 0\n"), [])
        # hors de /repo : non concerné
        self.assertEqual(rm.readonly_problems(MOUNTS_OK + "/dev/sdb1 /repository ext4 rw 0 0\n"), [])
        self.assertEqual(rm.readonly_problems(MOUNTS_OK + "/dev/sdb1 /repo\\040x ext4 rw 0 0\n"), [])

    def test_last_mount_wins(self):
        over_rw = MOUNTS_OK + "/dev/sdb1 /repo btrfs rw 0 0\n"   # surmontage inscriptible
        self.assertEqual(len(rm.readonly_problems(over_rw)), 1)
        rw_then_ro = MOUNTS_OK.replace("/repo btrfs ro,", "/repo btrfs rw,") + "/dev/sdb1 /repo btrfs ro 0 0\n"
        self.assertEqual(rm.readonly_problems(rw_then_ro), [])

    def test_parse_restic_version(self):
        self.assertEqual(rm.parse_restic_version("restic 0.19.1 compiled with go1.26.4 on linux/amd64\n"), "0.19.1")
        self.assertIsNone(rm.parse_restic_version("something else"))

    def test_mounts_command_uses_same_isolation(self):
        c = rm.mounts_command(cfg(), "n")
        self.assertEqual(c[:-4], rm.docker_base(cfg(), "n"))
        self.assertEqual(c[-4:], ["--entrypoint", "/bin/cat", IMAGE, "/proc/self/mounts"])


class PrivateStorageTest(TrustedUser, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.base = tempfile.mkdtemp()  # /tmp/xxx : 0700, propriétaire courant, parent /tmp sticky
        self.addCleanup(shutil.rmtree, self.base)

    def test_creates_missing_levels_in_0700(self):
        fd = rm.open_private_dir(self.base + "/a/b")
        os.close(fd)
        for d in ("a", "a/b"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.base, d)).st_mode) & 0o077, 0)

    def test_refuses_symlink_at_any_level(self):
        target = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, target)
        os.symlink(target, self.base + "/lien")
        for path in (self.base + "/lien", self.base + "/lien/sous"):
            with self.assertRaises(rm.UnsafePath, msg=path):
                rm.open_private_dir(path)
        self.assertEqual(os.listdir(target), [])

    def test_refuses_file_group_writable_and_untrusted_owner(self):
        open(self.base + "/fichier", "w").close()
        with self.assertRaises(rm.UnsafePath):
            rm.open_private_dir(self.base + "/fichier")
        os.mkdir(self.base + "/ouvert", 0o770)
        os.chmod(self.base + "/ouvert", 0o770)
        with self.assertRaises(rm.UnsafePath):
            rm.open_private_dir(self.base + "/ouvert/x")
        rm.TRUSTED_UIDS = (0,)  # l'utilisateur courant n'est plus de confiance
        with self.assertRaises(rm.UnsafePath):
            rm.open_private_dir(self.base + "/y")

    def test_refuses_acl_and_synoacl(self):
        orig_x, orig_m = rm.acl_xattrs, rm.mount_options_for
        try:
            rm.acl_xattrs = lambda fd: ["system.posix_acl_access"]
            with self.assertRaises(rm.UnsafePath):
                rm.open_private_dir(self.base + "/acl")
            rm.acl_xattrs = orig_x
            rm.mount_options_for = lambda p: ("/volume1", "btrfs", ["rw", "synoacl"])
            with self.assertRaises(rm.UnsafePath):
                rm.open_private_dir(self.base + "/syno")
        finally:
            rm.acl_xattrs, rm.mount_options_for = orig_x, orig_m

    def test_operations_stay_on_verified_directory(self):
        # Après ouverture, remplacer le chemin par un lien vers un autre dossier ne détourne pas
        # les écritures : elles restent relatives au descripteur vérifié.
        fd = rm.open_private_dir(self.base + "/priv")
        try:
            other = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, other)
            os.rename(self.base + "/priv", self.base + "/priv-deplace")
            os.symlink(other, self.base + "/priv")
            rm.write_json_at(fd, "state.json", {"x": 1})
            self.assertEqual(os.listdir(other), [])
            self.assertTrue(os.path.exists(self.base + "/priv-deplace/state.json"))
        finally:
            os.close(fd)

    def test_write_replaces_symlink_without_following(self):
        fd = rm.open_private_dir(self.base + "/priv")
        try:
            victim = self.base + "/victime"
            with open(victim, "w") as f:
                f.write("intact")
            os.symlink(victim, self.base + "/priv/report-20260101-000000.json")
            rm.write_json_at(fd, "report-20260101-000000.json", {"x": 1})
            with open(victim) as f:
                self.assertEqual(f.read(), "intact")
            self.assertFalse(os.path.islink(self.base + "/priv/report-20260101-000000.json"))
            self.assertEqual([n for n in os.listdir(fd) if n.endswith(".tmp")], [])
        finally:
            os.close(fd)

    def test_write_without_replace(self):
        fd = rm.open_private_dir(self.base + "/priv")
        try:
            rm.write_json_at(fd, "state.json", {"v": 1}, replace=False)
            with self.assertRaises(FileExistsError):
                rm.write_json_at(fd, "state.json", {"v": 2}, replace=False)
            with open(self.base + "/priv/state.json") as f:
                self.assertEqual(json.load(f), {"v": 1})
            self.assertEqual(sorted(os.listdir(fd)), ["state.json"])  # aucun temporaire résiduel
            os.remove(self.base + "/priv/state.json")
            os.symlink(self.base + "/victime", self.base + "/priv/state.json")
            with self.assertRaises(FileExistsError):  # un lien existant n'est ni suivi ni remplacé
                rm.write_json_at(fd, "state.json", {"v": 3}, replace=False)
            self.assertFalse(os.path.exists(self.base + "/victime"))
        finally:
            os.close(fd)

    def test_lock_symlink_refused(self):
        fd = rm.open_private_dir(self.base + "/priv")
        try:
            victim = self.base + "/victime"
            with open(victim, "w") as f:
                f.write("intact")
            os.symlink(victim, self.base + "/priv/lock")
            with self.assertRaises(OSError):
                rm.open_lock(fd)
            with open(victim) as f:
                self.assertEqual(f.read(), "intact")
        finally:
            os.close(fd)

    def test_rotation_only_removes_reports(self):
        fd = rm.open_private_dir(self.base + "/logs")
        try:
            for i in range(5):
                rm.write_json_at(fd, "report-2026010%d-000000.json" % i, {})
            open(self.base + "/logs/autre.json", "w").close()
            rm.rotate_reports(fd, 2)
            self.assertEqual(sorted(os.listdir(fd)), ["autre.json", "report-20260103-000000.json",
                                                      "report-20260104-000000.json"])
        finally:
            os.close(fd)


class StateValidationTest(TrustedUser, unittest.TestCase):
    SKEW = dt.timedelta(minutes=60)

    def setUp(self):
        super().setUp()
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base)
        self.fd = rm.open_private_dir(self.base + "/state")
        self.addCleanup(os.close, self.fd)

    def put(self, content, mode=0o600):
        path = self.base + "/state/state.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(content if isinstance(content, bytes) else json.dumps(content).encode())
        os.chmod(path, mode)

    def read(self):
        return rm.read_state(self.fd, NOW, self.SKEW)

    def test_absent(self):
        self.assertEqual(self.read(), (None, None))

    def test_valid(self):
        state = {"last_run": (NOW - dt.timedelta(days=1)).isoformat(), "snapshot_ids": ["a" * 64, "b" * 64]}
        self.put(state)
        self.assertEqual(self.read(), (state, None))

    def test_invalid_contents(self):
        good = {"last_run": (NOW - dt.timedelta(days=1)).isoformat(), "snapshot_ids": ["a" * 64]}
        cases = [
            b"{tronque", b"\xff\xfe", b"null", b"[]",
            dict(good, extra=1), {"last_run": good["last_run"]},
            dict(good, last_run=12), dict(good, last_run="hier"),
            dict(good, last_run=(NOW + dt.timedelta(days=2)).isoformat()),
            dict(good, snapshot_ids="a" * 64), dict(good, snapshot_ids=["A" * 64]),
            dict(good, snapshot_ids=["a" * 63]), dict(good, snapshot_ids=[1]),
            dict(good, snapshot_ids=["a" * 64, "a" * 64]),
        ]
        for content in cases:
            self.put(content)
            state, error = self.read()
            self.assertIsNone(state, content)
            self.assertTrue(error, content)

    def test_too_large(self):
        self.put(b" " * (rm.MAX_STATE_BYTES + 1))
        self.assertIn("volumineux", self.read()[1])

    def test_unsafe_file(self):
        self.put({"last_run": NOW.isoformat(), "snapshot_ids": []}, mode=0o644)
        self.assertIn("inattendu", self.read()[1])
        os.remove(self.base + "/state/state.json")
        os.symlink("/etc/hostname", self.base + "/state/state.json")
        self.assertIn("inaccessible", self.read()[1])
        os.remove(self.base + "/state/state.json")
        os.mkdir(self.base + "/state/state.json")
        self.assertTrue(self.read()[1])

    def test_invalid_state_gives_anomaly_without_baseline(self):
        s = daily(10)
        f, _ = rm.analyze(cfg(groups=GROUPS[:1]), s, groups(s), NOW, None, "state.json n'est pas un JSON valide")
        self.assertEqual(codes(f), ["state_invalid"])
        self.assertEqual(codes(f, "INFO"), [])


class GateScriptTest(unittest.TestCase):
    """La barrière shell/awk doit décider exactement comme readonly_problems(), avec dash/gawk
    et avec busybox (interpréteur de l'image)."""

    CASES = {
        "ok": MOUNTS_OK,
        "racine rw": MOUNTS_OK.replace("overlay / overlay ro,", "overlay / overlay rw,"),
        "repo rw": MOUNTS_OK.replace("/repo btrfs ro,", "/repo btrfs rw,"),
        "secret absent": "\n".join(l for l in MOUNTS_OK.splitlines() if "restic_password" not in l) + "\n",
        "repo absent": "\n".join(l for l in MOUNTS_OK.splitlines() if " /repo " not in l) + "\n",
        "sous-montage rw": MOUNTS_OK + "/dev/sdb1 /repo/data ext4 rw,relatime 0 0\n",
        "sous-montage ro": MOUNTS_OK + "/dev/sdb1 /repo/data ext4 ro,relatime 0 0\n",
        "sous-montage profond rw": MOUNTS_OK + "/dev/sdb1 /repo/a/b ext4 rw 0 0\n",
        "sous-montage avec espace rw": MOUNTS_OK + "/dev/sdb1 /repo/a\\040b ext4 rw 0 0\n",
        "voisin /repository rw": MOUNTS_OK + "/dev/sdb1 /repository ext4 rw 0 0\n",
        "surmontage rw": MOUNTS_OK + "/dev/sdb1 /repo btrfs rw 0 0\n",
        "rw puis ro": MOUNTS_OK.replace("/repo btrfs ro,", "/repo btrfs rw,") + "/dev/sdb1 /repo btrfs ro 0 0\n",
        "faux ro (remount-ro)": MOUNTS_OK.replace("/repo btrfs ro,relatime", "/repo btrfs rw,errors=remount-ro"),
        "vide": "",
    }

    def run_gate(self, shell, mounts_text):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        mounts = os.path.join(d, "mounts")
        with open(mounts, "w") as f:
            f.write(mounts_text)
        script = rm.GATE_SCRIPT.replace("/proc/self/mounts", mounts).replace(
            "exec /usr/bin/restic", "exec echo RESTIC_LANCE")
        return subprocess.run(shell + ["-c", script, "restic-gate", "--no-lock", "version"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def check_shell(self, shell):
        for name, text in self.CASES.items():
            proc = self.run_gate(shell, text)
            expected_ok = rm.readonly_problems(text) == []
            if expected_ok:
                self.assertEqual(proc.returncode, 0, (shell, name, proc.stderr))
                self.assertEqual(proc.stdout.decode().strip(), "RESTIC_LANCE --no-lock version", (shell, name))
            else:
                self.assertEqual(proc.returncode, rm.GATE_EXIT, (shell, name))
                self.assertIn(rm.GATE_MARKER, proc.stderr.decode(), (shell, name))
                self.assertEqual(proc.stdout, b"", (shell, name))

    def test_dash_gawk(self):
        self.check_shell(["sh"])

    def test_busybox(self):
        bindir = busybox_shell()
        if not bindir:
            self.skipTest("busybox absent")
        self.addCleanup(shutil.rmtree, bindir)
        env_path = os.environ["PATH"]
        os.environ["PATH"] = bindir + os.pathsep + env_path
        self.addCleanup(os.environ.__setitem__, "PATH", env_path)
        self.check_shell([os.path.join(bindir, "sh")])

    def test_expected_verdicts(self):
        refused = {n for n, t in self.CASES.items() if rm.readonly_problems(t)}
        self.assertEqual(refused, {"racine rw", "repo rw", "secret absent", "repo absent", "sous-montage rw",
                                   "sous-montage profond rw", "sous-montage avec espace rw", "surmontage rw",
                                   "faux ro (remount-ro)", "vide"})


class IsolatedModeTest(unittest.TestCase):
    """Le script doit refuser de démarrer sans « python3 -I », avant d'importer quoi que ce soit."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        shutil.copy(os.path.join(SCRIPT_DIR, "restic_maintenance.py"), self.dir)
        self.marker = os.path.join(self.dir, "PARASITE_EXECUTE")
        # Module parasite : s'il était importé, il créerait le fichier témoin.
        with open(os.path.join(self.dir, "json.py"), "w") as f:
            f.write("open(%r, 'w').close()\nraise SystemExit(99)\n" % self.marker)

    def run_script(self, *flags):
        return subprocess.run([sys.executable] + list(flags) +
                              [os.path.join(self.dir, "restic_maintenance.py"), "--config", "/inexistant.json"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.dir)

    def test_refused_without_isolated_mode(self):
        proc = self.run_script()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("python3 -I", proc.stderr.decode())
        self.assertFalse(os.path.exists(self.marker), "le module parasite a été exécuté")

    def test_parasite_ignored_in_isolated_mode(self):
        proc = self.run_script("-I")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("CONFIG", proc.stderr.decode())  # le script a démarré, puis rejeté la configuration absente
        self.assertFalse(os.path.exists(self.marker), "le module parasite a été exécuté")


class ConfigTest(unittest.TestCase):
    def write(self, data):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        self.addCleanup(os.remove, path)
        return path

    def test_only_dry_run_accepted(self):
        for mode in ("forget", "prune", "", "DRY-RUN"):
            with self.assertRaises(rm.ConfigError, msg=mode):
                rm.load_config(self.write(cfg(mode=mode)))

    def test_groups_validation(self):
        bad = [
            [],
            [{"name": "x", "host": HOST, "paths": []}],
            [{"name": "x", "host": HOST, "paths": ["/srv/docker/"]}],
            [{"name": "x", "host": HOST, "paths": ["/srv/docker/*"]}],
            [{"name": "x", "host": HOST, "paths": ["srv/docker/core"]}],
            [{"name": "x", "host": HOST, "paths": PATHS, "frozen_ids": ["1e30"]}],
            GROUPS + [{"name": "dup", "host": HOST, "paths": PATHS}],
        ]
        for groups_ in bad:
            with self.assertRaises(rm.ConfigError, msg=groups_):
                rm.load_config(self.write(cfg(groups=groups_)))
        self.assertEqual(rm.load_config(self.write(cfg()))["groups"], GROUPS)

    def test_paths_strict(self):
        for key, bad in (("repository", "/volume1/Restic/a:b"), ("repository", "/volume1/Restic/a,b"),
                         ("repository", "/volume1/Restic/a b"), ("repository", "/volume1/Restic/../etc"),
                         ("repository", "/volume1/Restic/./x"), ("repository", "volume1/Restic"),
                         ("repository", "/volume1/Restic/"), ("repository", "/srv/restic"),
                         ("password_file", "/volume1/x:/etc/shadow"), ("password_file", "/tmp/secret"),
                         ("private_dir", "/volume1/docker/restic-maintenance/state"), ("private_dir", "/volumeX"),
                         ("private_dir", "/root/../tmp"), ("private_dir", "relatif"), ("private_dir", 7)):
            with self.assertRaises(rm.ConfigError, msg=(key, bad)):
                rm.load_config(self.write(cfg(**{key: bad})))

    def test_retention_values_cannot_inject_arguments(self):
        base = cfg()["retention"]
        bad = [dict(base, keep_within="30d --prune"), dict(base, keep_within="--prune"),
               dict(base, keep_within="-1d"), dict(base, keep_within=""), dict(base, keep_within="30"),
               dict(base, keep_within=30), dict(base, keep_daily="14"), dict(base, keep_daily="--unsafe-allow-remove-all"),
               dict(base, keep_daily=True), dict(base, keep_daily=-1), dict(base, keep_daily=1.5),
               dict(base, keep_weekly=10001), dict(base, keep_last=3), {k: v for k, v in base.items() if k != "keep_monthly"},
               dict(base, keep_within="1d2d")]
        for retention in bad:
            with self.assertRaises(rm.ConfigError, msg=retention):
                rm.load_config(self.write(cfg(retention=retention)))
        args = rm.forget_args(rm.load_config(self.write(cfg(retention=dict(base, keep_within="1y6m"))))["retention"])
        self.assertEqual(args[args.index("--keep-within") + 1], "1y6m")

    def test_other_parameters(self):
        th = cfg()["thresholds"]
        for kw in ({"name": "Docker test"}, {"name": "../x"}, {"timeout_minutes": 0}, {"timeout_minutes": "120"},
                   {"keep_reports": 0}, {"run_check": "yes"}, {"thresholds": dict(th, max_remove_percent=101)},
                   {"thresholds": dict(th, max_age_hours="48")}, {"thresholds": dict(th, x=1)}, {"thresholds": {k: v for k, v in th.items() if k != "max_age_hours"}}):
            with self.assertRaises(rm.ConfigError, msg=kw):
                rm.load_config(self.write(cfg(**kw)))

    def test_image_reference(self):
        for ok in (IMAGE, "restic/restic:0.19.1", "restic/restic:0.19.1@" + IMAGE.split("@")[1]):
            self.assertEqual(rm.load_config(self.write(cfg(image=ok)))["image"], ok)
        for bad in ("restic/restic", "restic/restic@sha256:abc", "Restic/restic:1", "restic/restic:0.19.1; rm -rf /"):
            with self.assertRaises(rm.ConfigError, msg=bad):
                rm.load_config(self.write(cfg(image=bad)))

    def test_expected_version_required(self):
        c = cfg()
        del c["expected_restic_version"]
        with self.assertRaises(rm.ConfigError):
            rm.load_config(self.write(c))
        with self.assertRaises(rm.ConfigError):
            rm.load_config(self.write(cfg(expected_restic_version="latest")))

    def test_window(self):
        w = {"start": "23:00", "end": "02:00"}
        self.assertTrue(rm.in_window(w, dt.datetime(2026, 1, 1, 23, 30)))
        self.assertTrue(rm.in_window(w, dt.datetime(2026, 1, 1, 1, 59)))
        self.assertFalse(rm.in_window(w, dt.datetime(2026, 1, 1, 2, 0)))
        with self.assertRaises(rm.ConfigError):
            rm.parse_window({"start": "03:00", "end": "03:00"})

    def test_parse_time_nanoseconds(self):
        t = rm.parse_time("2026-10-27T03:00:01.123456789+01:00")
        self.assertEqual(t, dt.datetime(2026, 10, 27, 2, 0, 1, 123456, tzinfo=dt.timezone.utc))


FAKE_DOCKER = r'''#!{python}
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
if args[0] == "kill":
    sys.exit(0)
with open(os.environ["FAKE_FIXTURES"]) as fx:
    fixtures = json.load(fx)
entry = args[args.index("--entrypoint") + 1]
if entry == "/bin/cat":
    sys.stdout.write(fixtures["mounts"])
    sys.exit(0)
# /bin/sh -c <barrière> restic-gate <args restic> : la barrière réelle est exécutée,
# avec les montages simulés du conteneur restic et un faux binaire restic.
script = args[args.index("-c") + 1]
rargs = args[args.index("restic-gate") + 1:]
mounts = os.path.join(os.environ["FAKE_DIR"], "gate-mounts")
with open(mounts, "w") as f:
    f.write(fixtures.get("gate_mounts", fixtures["mounts"]))
script = script.replace("/proc/self/mounts", mounts).replace(
    "exec /usr/bin/restic", "exec %s %s" % (sys.executable, os.environ["FAKE_RESTIC"]))
shell = json.loads(os.environ.get("FAKE_SH", '["sh"]'))
proc = subprocess.run(shell + ["-c", script, "restic-gate"] + rargs)
sys.exit(proc.returncode)
'''

FAKE_RESTIC = r'''import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_RESTIC_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
with open(os.environ["FAKE_FIXTURES"]) as fx:
    fixtures = json.load(fx)
sub = [a for a in args if not a.startswith("-")][0]
out = fixtures.get(sub, "")
sys.stdout.write(out if isinstance(out, str) else json.dumps(out))
code = fixtures.get("exits", {}).get(sub, 0)
if code:
    sys.stderr.write("erreur restic simulée\\n")
sys.exit(code)
'''


def busybox_shell():
    # Interpréteur identique à l'image (busybox sh + busybox awk), si disponible.
    import shutil as sh
    busybox = sh.which("busybox")
    if not busybox:
        return None
    bindir = tempfile.mkdtemp()
    for tool in ("sh", "awk"):
        os.symlink(busybox, os.path.join(bindir, tool))
    return bindir




class EndToEndTest(TrustedUser, unittest.TestCase):
    """Exécute main() avec un faux binaire docker qui enregistre ses arguments."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.private = os.path.join(tempfile.mkdtemp(), "private")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.private))
        # Le dossier de test joue le rôle du volume de données (/volumeN/).
        self._volume_re, rm.DATA_VOLUME_RE = rm.DATA_VOLUME_RE, re.compile("^" + re.escape(self.tmp) + "/")
        self.addCleanup(setattr, rm, "DATA_VOLUME_RE", self._volume_re)
        self.repo = os.path.join(self.tmp, "repo")
        os.mkdir(self.repo)
        self.secret = os.path.join(self.tmp, "secret")
        open(self.secret, "w").close()
        os.chmod(self.secret, 0)  # illisible : le script ne doit jamais l'ouvrir
        self.log = os.path.join(self.tmp, "docker.log")
        self.fixtures = os.path.join(self.tmp, "fixtures.json")
        docker = os.path.join(self.tmp, "docker")
        with open(docker, "w") as f:
            f.write(FAKE_DOCKER.replace("{python}", sys.executable).replace("{image}", IMAGE))
        os.chmod(docker, stat.S_IRWXU)
        self._orig = rm.DOCKER_BIN
        rm.DOCKER_BIN = docker
        self.restic_log = os.path.join(self.tmp, "restic.log")
        fake_restic = os.path.join(self.tmp, "fake_restic.py")
        with open(fake_restic, "w") as f:
            f.write(FAKE_RESTIC)
        os.environ.update(FAKE_LOG=self.log, FAKE_FIXTURES=self.fixtures, FAKE_DIR=self.tmp,
                          FAKE_RESTIC=fake_restic, FAKE_RESTIC_LOG=self.restic_log)
        os.environ.pop("FAKE_SH", None)
        self.config = os.path.join(self.tmp, "config.json")
        c = cfg(repository=self.repo, password_file=self.secret, private_dir=self.private)
        with open(self.config, "w") as f:
            json.dump(c, f)

    def tearDown(self):
        rm.DOCKER_BIN = self._orig

    def run_with(self, snapshots, forget, mounts=None, version="restic 0.19.1 compiled with go1.26.4 on linux/amd64",
                 gate_mounts=None, exits=None, init=False):
        fixtures = {"version": version, "snapshots": snapshots, "list": "",
                    "check": "no errors were found", "forget": forget,
                    "mounts": MOUNTS_OK if mounts is None else mounts, "exits": exits or {}}
        if gate_mounts is not None:
            fixtures["gate_mounts"] = gate_mounts
        with open(self.fixtures, "w") as f:
            json.dump(fixtures, f)
        open(self.restic_log, "w").close()
        open(self.log, "w").close()
        code = rm.main(["--config", self.config, "--ignore-window"] + (["--init-baseline"] if init else []))
        with open(self.log) as f:
            calls = [json.loads(line) for line in f]
        return code, calls

    def test_full_dry_run_then_backdated_insertion(self):
        now = dt.datetime.now(dt.timezone.utc)
        core = daily(20, end=now - dt.timedelta(hours=1))
        s = core + [validation()]
        forget = groups(core) + groups([s[-1]], paths=["/tmp/restic-test"])
        code, calls = self.run_with(s, forget, init=True)
        self.assertEqual(code, rm.EXIT_OK)
        self.assertEqual(calls[0][calls[0].index("--entrypoint") + 1], "/bin/cat")  # pré-contrôle d'abord
        subs = []
        for call in calls:
            joined = " ".join(call)
            self.assertIn("%s:/repo:ro" % self.repo, joined)
            self.assertIn("--read-only", call)
            self.assertNotIn("prune", call)
            if call[call.index("--entrypoint") + 1] == "/bin/cat":
                continue
            # chaque conteneur restic passe par la barrière, puis restic
            self.assertEqual(call[call.index(IMAGE):call.index("restic-gate") + 1],
                             [IMAGE, "-c", rm.GATE_SCRIPT, "restic-gate"])
            rargs = call[call.index("restic-gate") + 1:]
            self.assertIn("--no-lock", rargs)
            subs.append([a for a in rargs if not a.startswith("-")][0])
            if "forget" in rargs:
                self.assertIn("--dry-run", rargs)
        self.assertEqual(subs, ["version", "snapshots", "list", "check", "forget"])
        self.assertEqual([[a for a in c if not a.startswith("-")][0] for c in self.restic_calls()], subs)
        self.assertTrue(os.path.exists(os.path.join(self.private, "state", "state.json")))

        forged = snap("b", now - dt.timedelta(days=4))
        code, _ = self.run_with(s + [forged], groups(core + [forged]) + forget[1:])
        self.assertEqual(code, rm.EXIT_ANOMALY)
        reports = sorted(r for r in os.listdir(os.path.join(self.private, "logs")) if r.startswith("report-"))
        with open(os.path.join(self.private, "logs", reports[-1])) as f:
            last = json.load(f)
        self.assertEqual(last["status"], "anomaly")
        self.assertIn("backdated_snapshot", [f["code"] for f in last["findings"]])

    def test_concurrent_run_refused(self):
        os.makedirs(os.path.join(self.private, "state"), mode=0o700)
        os.chmod(self.private, 0o700)
        fd = os.open(os.path.join(self.private, "state", "lock"), os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd) as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, calls = self.run_with([], [])
        self.assertEqual(code, rm.EXIT_LOCKED)
        self.assertEqual(calls, [])

    def restic_calls(self):
        with open(self.restic_log) as f:
            return [json.loads(line) for line in f]

    def state_path(self):
        return os.path.join(self.private, "state", "state.json")

    def write_state(self, content):
        os.makedirs(os.path.join(self.private, "state"), mode=0o700, exist_ok=True)
        os.chmod(self.private, 0o700)
        fd = os.open(self.state_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(content)

    def clean_repo(self):
        now = dt.datetime.now(dt.timezone.utc)
        core = daily(20, end=now - dt.timedelta(hours=1))
        s = core + [validation()]
        return s, groups(core) + groups([s[-1]], paths=["/tmp/restic-test"])

    def report(self):
        with open(self.latest_report()) as f:
            return json.load(f)

    def test_missing_baseline_requires_explicit_init(self):
        s, forget = self.clean_repo()
        code, _ = self.run_with(s, forget)
        self.assertEqual(code, rm.EXIT_BASELINE_REQUIRED)
        self.assertNotEqual(code, 0)
        r = self.report()
        self.assertEqual((r["status"], r["baseline"]), ("baseline_required", "absent"))
        self.assertTrue(any("RÉFÉRENCE ABSENTE" in line for line in r["log"]))
        self.assertFalse(os.path.exists(self.state_path()))
        # les contrôles ont bien été exécutés
        self.assertEqual(len(self.restic_calls()), 5)

    def test_init_baseline_created_after_checks(self):
        s, forget = self.clean_repo()
        code, _ = self.run_with(s, forget, init=True)
        self.assertEqual(code, rm.EXIT_OK)
        r = self.report()
        self.assertEqual((r["status"], r["baseline"]), ("baseline_created", "created"))
        self.assertTrue(any("RÉFÉRENCE CRÉÉE : 21 snapshots" in line for line in r["log"]))
        with open(self.state_path()) as f:
            self.assertEqual(sorted(json.load(f)["snapshot_ids"]), sorted(x["id"] for x in s))
        # lancement suivant, sans option : comparaison normale
        code, _ = self.run_with(s, forget)
        self.assertEqual(code, rm.EXIT_OK)
        self.assertEqual((self.report()["status"], self.report()["baseline"]), ("ok", "present"))

    def test_init_baseline_not_created_on_anomaly(self):
        s, forget = self.clean_repo()
        intrus = snap("a", dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2), host="intrus")
        code, _ = self.run_with(s + [intrus], forget + groups([intrus]), init=True)
        self.assertEqual(code, rm.EXIT_ANOMALY)
        self.assertFalse(os.path.exists(self.state_path()))
        self.assertEqual(self.report()["baseline"], "absent")

    def test_init_baseline_refused_when_valid_baseline_exists(self):
        s, forget = self.clean_repo()
        self.assertEqual(self.run_with(s, forget, init=True)[0], rm.EXIT_OK)
        with open(self.state_path(), "rb") as f:
            before = f.read()
        code, calls = self.run_with(s, forget, init=True)
        self.assertEqual(code, rm.EXIT_BASELINE_EXISTS)
        self.assertEqual(calls, [])  # refus avant tout conteneur
        r = self.report()
        self.assertEqual((r["status"], r["baseline"]), ("baseline_exists", "present"))
        with open(self.state_path(), "rb") as f:
            self.assertEqual(f.read(), before)

    def test_init_baseline_does_not_replace_invalid_state(self):
        s, forget = self.clean_repo()
        self.write_state(b"{invalide")
        code, _ = self.run_with(s, forget, init=True)
        self.assertEqual(code, rm.EXIT_ANOMALY)
        r = self.report()
        self.assertEqual(r["baseline"], "invalid")
        self.assertIn("state_invalid", [f["code"] for f in r["findings"]])
        self.assertTrue(any("sans effet" in line for line in r["log"]))
        with open(self.state_path(), "rb") as f:
            self.assertEqual(f.read(), b"{invalide")

    def test_volume_warnings_do_not_block_dry_run(self):
        now = dt.datetime.now(dt.timezone.utc)
        core = daily(60, end=now - dt.timedelta(hours=1))
        s = core + [validation()]
        forget = groups(core[:45], core[45:]) + groups([s[-1]], paths=["/tmp/restic-test"])
        code, _ = self.run_with(s, forget, init=True)
        self.assertEqual(code, rm.EXIT_OK)
        r = self.report()
        self.assertEqual(r["status"], "baseline_created")
        self.assertEqual(sorted(f["code"] for f in r["findings"] if f["level"] == "WARN"), ["remove_count", "remove_percent"])
        self.assertEqual([f for f in r["findings"] if f["level"] == "ERROR"], [])

    def test_young_removal_is_an_anomaly(self):
        s, forget = self.clean_repo()
        core = forget[0]["keep"]
        forget[0] = {"host": HOST, "paths": PATHS, "tags": None, "keep": core[:-1], "remove": core[-1:]}
        code, _ = self.run_with(s, forget, init=True)
        self.assertEqual(code, rm.EXIT_ANOMALY)
        self.assertIn("young_removal", [f["code"] for f in self.report()["findings"]])
        self.assertFalse(os.path.exists(self.state_path()))

    def test_invalid_state_is_kept_and_reported(self):
        now = dt.datetime.now(dt.timezone.utc)
        core = daily(20, end=now - dt.timedelta(hours=1))
        s = core + [validation()]
        forget = groups(core) + groups([s[-1]], paths=["/tmp/restic-test"])
        for content in (b"{pas du json", b'{"last_run": 1, "snapshot_ids": []}', b"[" + b"0" * (2 * 1024 * 1024) + b"]"):
            self.write_state(content)
            code, _ = self.run_with(s, forget)
            self.assertEqual(code, rm.EXIT_ANOMALY, content[:30])
            with open(self.state_path(), "rb") as f:
                self.assertEqual(f.read(), content, "la référence existante a été remplacée")

    def test_symlinked_lock_is_refused_and_target_untouched(self):
        victim = os.path.join(self.tmp, "victime.txt")
        with open(victim, "w") as f:
            f.write("CONTENU IMPORTANT")
        os.makedirs(os.path.join(self.private, "state"), mode=0o700)
        os.chmod(self.private, 0o700)
        os.symlink(victim, os.path.join(self.private, "state", "lock"))
        code, calls = self.run_with([], [])
        self.assertEqual(code, rm.EXIT_CONFIG)
        self.assertEqual(calls, [])
        with open(victim) as f:
            self.assertEqual(f.read(), "CONTENU IMPORTANT")

    def test_private_dir_symlink_is_refused(self):
        elsewhere = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, elsewhere)
        os.symlink(elsewhere, self.private)
        code, calls = self.run_with([], [])
        self.assertEqual(code, rm.EXIT_CONFIG)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_writable_repository_mount_aborts_before_restic(self):
        rw = MOUNTS_OK.replace("/repo btrfs ro,", "/repo btrfs rw,")
        code, calls = self.run_with([], [], mounts=rw)
        self.assertEqual(code, rm.EXIT_CONFIG)
        self.assertEqual(len(calls), 1)  # aucun appel à restic après l'échec du contrôle
        self.assertEqual(calls[0][calls[0].index("--entrypoint") + 1], "/bin/cat")
        self.assertEqual(self.restic_calls(), [])

    def test_gate_refuses_writable_submount_even_if_precheck_passed(self):
        # Le pré-contrôle voit des montages sûrs, mais le conteneur restic a un sous-montage
        # inscriptible : la barrière l'arrête avant restic.
        gate = MOUNTS_OK + "/dev/sdb1 /repo/data ext4 rw,relatime 0 0\n"
        for shell in self.shells():
            os.environ["FAKE_SH"] = json.dumps(shell)
            code, calls = self.run_with([], [], gate_mounts=gate)
            self.assertEqual(code, rm.EXIT_CONFIG, shell)
            self.assertEqual(self.restic_calls(), [], shell)
            self.assertEqual(len(calls), 2, shell)  # pré-contrôle + premier conteneur restic refusé
            with open(self.latest_report()) as f:
                report = json.load(f)
            self.assertEqual(report["status"], "unsafe_mounts")
            self.assertTrue(any("/repo/data" in line for line in report["log"]), shell)

    def test_gate_refuses_missing_secret_mount(self):
        gate = "\n".join(l for l in MOUNTS_OK.splitlines() if "restic_password" not in l) + "\n"
        code, _ = self.run_with([], [], gate_mounts=gate)
        self.assertEqual(code, rm.EXIT_CONFIG)
        self.assertEqual(self.restic_calls(), [])

    def test_restic_exit_97_is_not_mistaken_for_gate(self):
        # restic qui sort lui-même en 97 (sans le marqueur de la barrière) reste une erreur restic.
        code, _ = self.run_with([], [], exits={"snapshots": 97})
        self.assertEqual(code, rm.EXIT_RESTIC)
        with open(self.latest_report()) as f:
            self.assertEqual(json.load(f)["status"], "restic_error")

    def shells(self):
        shells = [["sh"]]
        bindir = busybox_shell()
        if bindir:
            self.addCleanup(shutil.rmtree, bindir)
            shells.append([os.path.join(bindir, "sh")])
        return shells

    def latest_report(self):
        logs = os.path.join(self.private, "logs")
        return os.path.join(logs, sorted(r for r in os.listdir(logs) if r.startswith("report-"))[-1])

    def test_unexpected_restic_version_aborts(self):
        code, calls = self.run_with([], [], version="restic 0.18.0 compiled with go1.24 on linux/amd64")
        self.assertEqual(code, rm.EXIT_RESTIC)
        self.assertEqual(self.restic_calls(), [["--no-lock", "--no-cache", "version"]])  # rien d'autre

    def test_refuses_non_dry_run_config(self):
        with open(self.config) as f:
            c = json.load(f)
        c["mode"] = "forget"
        with open(self.config, "w") as f:
            json.dump(c, f)
        code, calls = self.run_with([], [])
        self.assertEqual(code, rm.EXIT_CONFIG)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
