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
import time
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


class KeepTagTest(unittest.TestCase):
    C = cfg(retention={"keep_within": "30d", "keep_daily": 14, "keep_weekly": 8, "keep_monthly": 12,
                       "keep_tags": ["keep"]}, groups=GROUPS[:1])

    def test_forget_arguments(self):
        args = rm.forget_args(self.C["retention"])
        self.assertEqual(args[-2:], ["--keep-tag", "keep"])
        self.assertNotIn("--keep-tag", rm.forget_args(cfg()["retention"]))
        rm.check_safe(rm.build_restic_args("forget", args))

    def test_keep_tag_added_to_group_tags_is_accepted(self):
        s = daily(10)
        s[3]["tags"] = TAGS + ["keep"]
        f, _ = rm.analyze(self.C, s, groups(s), NOW, None)
        self.assertEqual(codes(f), [])
        s[4]["tags"] = ["keep"]  # le tag de protection ne remplace pas les tags du groupe
        f, _ = rm.analyze(self.C, s, groups(s), NOW, None)
        self.assertEqual(codes(f), ["tags_mismatch"])
        s[4]["tags"] = TAGS + ["autre"]
        self.assertEqual(codes(rm.analyze(self.C, s, groups(s), NOW, None)[0]), ["tags_mismatch"])

    def state_for(self, s):
        return {"last_run": (NOW - dt.timedelta(hours=1)).isoformat(), "snapshot_ids": sorted(x["id"] for x in s)}

    def retag(self, sn, new_id, tags=None):
        n = dict(sn, id=new_id, original=sn["id"], tags=tags if tags is not None else sn["tags"] + ["keep"])
        return n

    def test_retag_is_recognised(self):
        s = daily(10)
        state = self.state_for(s)
        n = self.retag(s[8], "e" * 64)
        now_s = s[:8] + [n] + s[9:]
        f, _ = rm.analyze(self.C, now_s, groups(now_s), NOW, state)
        self.assertEqual(codes(f), [])
        self.assertEqual(codes(f, "INFO"), ["retagged"])

    def test_retag_without_protection_tag_or_ambiguous_is_not_recognised(self):
        s = daily(10)
        state = self.state_for(s)
        plain = self.retag(s[8], "e" * 64, tags=list(TAGS))      # réécrit sans tag de protection
        now_s = s[:8] + [plain] + s[9:]
        self.assertEqual(codes(rm.analyze(self.C, now_s, groups(now_s), NOW, state)[0]),
                         ["backdated_snapshot", "snapshots_disappeared"])
        a, b = self.retag(s[8], "e" * 64), self.retag(s[8], "f" * 64)  # deux prétendants
        now_s = s[:8] + [a, b] + s[9:]
        self.assertEqual(codes(rm.analyze(self.C, now_s, groups(now_s), NOW, state)[0]),
                         ["backdated_snapshot", "backdated_snapshot", "snapshots_disappeared"])
        still = self.retag(s[8], "e" * 64)                        # l'original existe toujours
        now_s = s + [still]
        self.assertEqual(codes(rm.analyze(self.C, now_s, groups(now_s), NOW, state)[0]), ["backdated_snapshot"])

    def test_protected_snapshot_can_never_be_removed(self):
        s = daily(40)
        s[35]["tags"] = TAGS + ["keep"]
        problems = rm.removal_problems(self.C, s, [s[35]["id"], s[36]["id"]])
        self.assertEqual([p["code"] for p in problems], ["removal_not_allowed"])
        self.assertIn("tag de protection", problems[0]["message"])

    def test_config(self):
        for bad in (["Keep"], ["keep", "keep"], "keep", [""], ["a" * 33], [1], ["k%d" % i for i in range(11)]):
            with self.assertRaises(rm.ConfigError, msg=bad):
                rm.validate_retention(dict(self.C["retention"], keep_tags=bad))
        rm.validate_retention(self.C["retention"])
        rm.validate_retention(cfg()["retention"])


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


class ReadDataTest(unittest.TestCase):
    SUNDAY = dt.date(2026, 11, 1)        # dimanche
    MONDAY = dt.date(2026, 11, 2)

    def test_full_read_on_configured_day(self):
        rd = {"weekdays": [7], "parts": 1}
        self.assertEqual(rm.read_data_args(rd, self.SUNDAY), ["--read-data"])
        self.assertEqual(rm.read_data_args(rd, self.MONDAY), [])
        self.assertEqual(rm.read_data_args(rm.NO_READ_DATA, self.SUNDAY), [])

    def test_forced_read(self):
        self.assertEqual(rm.read_data_args(rm.NO_READ_DATA, self.MONDAY, force=True), ["--read-data"])

    def test_subset_rotation_covers_everything(self):
        # 4 parties, un dimanche par semaine pendant 4 semaines consécutives : 1/4, 2/4, 3/4, 4/4.
        rd = {"weekdays": [7], "parts": 4}
        seen = [rm.read_data_args(rd, self.SUNDAY + dt.timedelta(weeks=w))[0] for w in range(4)]
        self.assertEqual(sorted(seen), ["--read-data-subset=%d/4" % n for n in (1, 2, 3, 4)])

    def test_subset_rotation_across_year_end(self):
        # Pas de rupture au 1er janvier : semaines consécutives = parties consécutives.
        rd = {"weekdays": [7], "parts": 3}
        days = [dt.date(2026, 12, 20) + dt.timedelta(weeks=w) for w in range(6)]
        parts = [int(rm.read_data_args(rd, d)[0].split("=")[1].split("/")[0]) for d in days]
        for a, b in zip(parts, parts[1:]):
            self.assertEqual(b, a % 3 + 1)

    def test_check_options_allowlist(self):
        for ok in (["--no-lock", "--no-cache", "check"], ["--no-lock", "--no-cache", "check", "--read-data"],
                   ["--no-lock", "--no-cache", "check", "--read-data-subset=2/4"]):
            rm.check_safe(ok)
        for bad in (["--no-lock", "check", "--read-data", "--read-data-subset=1/2"],
                    ["--no-lock", "check", "--read-data-subset=10%"], ["--no-lock", "check", "--with-cache"],
                    ["--no-lock", "check", "--read-data-subset=1/2;x"], ["--no-lock", "check", "--repair"]):
            with self.assertRaises(rm.StepError, msg=bad):
                rm.check_safe(bad)


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


class WriteSafetyTest(unittest.TestCase):
    ID = "a" * 64

    def test_write_allowlist(self):
        for ok in (["--no-cache", "forget", self.ID], ["--no-cache", "forget", self.ID, "b" * 64],
                   ["--no-cache", "prune", "--max-unused=5%"], ["--no-cache", "prune", "--max-unused=0%"]):
            rm.check_write_safe(ok)
        for bad in (["--no-cache", "forget"], ["--no-cache", "forget", self.ID[:8]],
                    ["--no-cache", "forget", self.ID, self.ID], ["--no-cache", "forget", "--keep-last", "1"],
                    ["--no-cache", "forget", self.ID, "--prune"], ["--no-lock", "--no-cache", "forget", self.ID],
                    ["forget", self.ID], ["--no-cache", "forget", "latest"], ["--no-cache", "prune"],
                    ["--no-cache", "prune", "--max-unused=unlimited"], ["--no-cache", "prune", "--max-unused=60%"],
                    ["--no-cache", "prune", "--max-unused=5%", "--repack-uncompressed"],
                    ["--no-cache", "prune", "--unsafe-recover-no-free-space", "x"], ["--no-cache", "unlock"],
                    ["--no-cache", "check"], ["--no-cache", "repair", "packs", self.ID]):
            with self.assertRaises(rm.StepError, msg=bad):
                rm.check_write_safe(bad)

    def test_readonly_prune_only_as_dry_run(self):
        rm.check_safe(["--no-lock", "--no-cache", "prune", "--dry-run", "--max-unused=5%"])
        for bad in (["--no-lock", "--no-cache", "prune", "--max-unused=5%"],
                    ["--no-lock", "--no-cache", "prune", "--dry-run"],
                    ["--no-lock", "--no-cache", "prune", "--dry-run", "--max-unused=5%", "--repack-uncompressed"],
                    ["--no-lock", "--no-cache", "prune", "--dry-run", "--max-unused=unlimited"],
                    ["--no-lock", "--no-cache", "forget", "--dry-run", self.ID, "--prune"]):
            with self.assertRaises(rm.StepError, msg=bad):
                rm.check_safe(bad)

    def test_write_container(self):
        c = cfg(apply=dict(rm.NO_APPLY, forget=True, repository_owner="1026:100"))
        w = rm.docker_command(c, "n", ["--no-cache", "forget", self.ID], write=True)
        joined = " ".join(w)
        self.assertIn("-v /volume1/Restic/docker-test:/repo -v", joined)
        self.assertIn("--user 1026:100", joined)
        self.assertIn("restic_password_test:/run/secrets/restic_password:ro", joined)
        for flag in ("--network none", "--read-only", "--cap-drop ALL", "--pull never", "no-new-privileges"):
            self.assertIn(flag, joined)
        self.assertEqual(w[w.index("-c") + 1], rm.gate_script(True))
        r = rm.docker_command(c, "n", ["--no-lock", "snapshots"])
        self.assertNotIn("--user", r)
        self.assertNotIn("DAC_OVERRIDE", r)
        self.assertIn("/volume1/Restic/docker-test:/repo:ro", " ".join(r))

    def test_write_mounts(self):
        rw = MOUNTS_OK.replace("/repo btrfs ro,", "/repo btrfs rw,")
        self.assertEqual(rm.readonly_problems(rw, write=True), [])
        self.assertTrue(rm.readonly_problems(MOUNTS_OK, write=True))  # /repo en lecture seule
        self.assertTrue(rm.readonly_problems(rw.replace("restic_password btrfs ro", "restic_password btrfs rw"),
                                             write=True))
        self.assertTrue(rm.readonly_problems(rw + "/dev/sdb1 /repo/data ext4 rw 0 0\n", write=True))
        self.assertTrue(rm.readonly_problems(rw.replace("overlay / overlay ro,", "overlay / overlay rw,"), write=True))
        self.assertTrue(rm.readonly_problems(rw))  # le même texte reste refusé en lecture seule


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

    def test_in_progress(self):
        good = {"last_run": (NOW - dt.timedelta(days=1)).isoformat(), "snapshot_ids": ["a" * 64]}
        for ip in ({"operation": "forget", "started": NOW.isoformat(), "ids": ["b" * 64]},
                   {"operation": "prune", "started": NOW.isoformat(), "ids": []}):
            self.put(dict(good, in_progress=ip))
            self.assertEqual(self.read(), (dict(good, in_progress=ip), None))
        for ip in (None, {}, {"operation": "unlock", "started": NOW.isoformat(), "ids": []},
                   {"operation": "forget", "started": "hier", "ids": []},
                   {"operation": "forget", "started": 1, "ids": []},
                   {"operation": "forget", "started": NOW.isoformat(), "ids": ["b" * 8]},
                   {"operation": "prune", "started": NOW.isoformat(), "ids": ["b" * 64]},
                   {"operation": "forget", "started": NOW.isoformat(), "ids": [], "x": 1}):
            self.put(dict(good, in_progress=ip))
            self.assertIsNone(self.read()[0], ip)

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

    def test_write_gate_matches_python(self):
        rw = MOUNTS_OK.replace("/repo btrfs ro,", "/repo btrfs rw,")
        cases = {"ok": rw, "repo ro": MOUNTS_OK, "secret rw": rw.replace("restic_password btrfs ro", "restic_password btrfs rw"),
                 "racine rw": rw.replace("overlay / overlay ro,", "overlay / overlay rw,"),
                 "sous-montage rw": rw + "/dev/sdb1 /repo/data ext4 rw 0 0\n", "vide": ""}
        shells = [["sh"]]
        bindir = busybox_shell()
        if bindir:
            self.addCleanup(shutil.rmtree, bindir)
            shells.append([os.path.join(bindir, "sh")])
        for shell in shells:
            for name, text in cases.items():
                d = tempfile.mkdtemp()
                self.addCleanup(shutil.rmtree, d)
                with open(os.path.join(d, "mounts"), "w") as f:
                    f.write(text)
                script = rm.gate_script(True).replace("/proc/self/mounts", os.path.join(d, "mounts")).replace(
                    "exec /usr/bin/restic", "exec echo RESTIC_LANCE")
                proc = subprocess.run(shell + ["-c", script, "restic-gate"], stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE)
                self.assertEqual(proc.returncode == 0, rm.readonly_problems(text, write=True) == [], (shell, name))
                self.assertEqual(proc.returncode == 0, name == "ok", (shell, name))

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

    def test_apply_config(self):
        self.assertEqual(rm.load_config(self.write(cfg())).get("apply", rm.NO_APPLY), rm.NO_APPLY)
        ok = dict(rm.NO_APPLY, forget=True, prune=True, prune_max_unused="10%", repository_owner="1026:100")
        self.assertEqual(rm.load_config(self.write(cfg(apply=ok)))["apply"], ok)
        for bad in (dict(ok, forget="true"), dict(ok, prune=1), dict(ok, prune_max_unused="60%"),
                    dict(ok, prune_max_unused="5"), dict(ok, prune_max_unused="unlimited"),
                    dict(ok, repository_owner="root"), dict(ok, repository_owner="0:0 --privileged"),
                    dict(ok, extra=1), {"forget": True}, dict(ok, protected={"start": "04:55"}),
                    dict(ok, protected={"start": "05:00", "end": "05:00"}),
                    dict(ok, protected={"start": "4:55", "end": "08:00"})):
            with self.assertRaises(rm.ConfigError, msg=bad):
                rm.load_config(self.write(cfg(apply=bad)))
        with self.assertRaises(rm.ConfigError):
            rm.load_config(self.write(cfg(apply=ok, run_check=False)))

    def test_read_data_config(self):
        self.assertEqual(rm.load_config(self.write(cfg(read_data={"weekdays": [7], "parts": 1})))["read_data"]["parts"], 1)
        for bad in ({"weekdays": [0], "parts": 1}, {"weekdays": [8], "parts": 1}, {"weekdays": [7, 7], "parts": 1},
                    {"weekdays": "7", "parts": 1}, {"weekdays": [7], "parts": 0}, {"weekdays": [7], "parts": 53},
                    {"weekdays": [7], "parts": "1"}, {"weekdays": [7]}, {"weekdays": [7], "parts": 1, "x": 1}):
            with self.assertRaises(rm.ConfigError, msg=bad):
                rm.load_config(self.write(cfg(read_data=bad)))
        with self.assertRaises(rm.ConfigError):
            rm.load_config(self.write(cfg(read_data={"weekdays": [7], "parts": 1}, run_check=False)))

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
if args[0] in ("kill", "stop"):
    sys.exit(0)
with open(os.environ["FAKE_FIXTURES"]) as fx:
    fixtures = json.load(fx)
write = any(a.endswith(":/repo") for a in args)
mounts_text = fixtures.get("write_mounts", fixtures["mounts"].replace("/repo btrfs ro,", "/repo btrfs rw,")) \
    if write else fixtures["mounts"]
entry = args[args.index("--entrypoint") + 1]
if entry == "/bin/cat":
    sys.stdout.write(mounts_text)
    sys.exit(0)
# /bin/sh -c <barrière> restic-gate <args restic> : la barrière réelle est exécutée,
# avec les montages simulés du conteneur restic et un faux binaire restic.
script = args[args.index("-c") + 1]
rargs = args[args.index("restic-gate") + 1:]
mounts = os.path.join(os.environ["FAKE_DIR"], "gate-mounts")
with open(mounts, "w") as f:
    f.write(fixtures.get("write_gate_mounts", mounts_text) if write else fixtures.get("gate_mounts", mounts_text))
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
real = sub in ("forget", "prune") and "--dry-run" not in args
if real and sub == "forget" and fixtures.get("write_exits", {}).get("forget", 0) != 11:
    # Suppression simulée (éventuellement partielle) : le repository simulé est réécrit.
    ids = args[args.index("forget") + 1:][:fixtures.get("forget_partial", 10 ** 6)]
    fixtures["snapshots"] = [s for s in fixtures["snapshots"] if s["id"] not in ids] + fixtures.get("appear", [])
    for g in fixtures["forget"]:
        g["remove"] = [s for s in g["remove"] if s["id"] not in ids]
    with open(os.environ["FAKE_FIXTURES"], "w") as fx:
        json.dump(fixtures, fx)
out = fixtures.get(sub, "")
sys.stdout.write(out if isinstance(out, str) else json.dumps(out))
code = fixtures.get("write_exits" if real else "exits", {}).get(sub, 0)
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

    def run_main(self, snapshots, forget, extra_args, exits=None):
        fixtures = {"version": "restic 0.19.1 compiled with go1.26.4 on linux/amd64", "snapshots": snapshots,
                    "list": "", "check": "no errors were found", "forget": forget, "mounts": MOUNTS_OK,
                    "exits": exits or {}}
        with open(self.fixtures, "w") as f:
            json.dump(fixtures, f)
        open(self.restic_log, "w").close()
        open(self.log, "w").close()
        return rm.main(["--config", self.config, "--ignore-window"] + extra_args)

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

    def check_call(self):
        return [c for c in self.restic_calls() if "check" in c][0]

    def test_forced_read_data(self):
        s, forget = self.clean_repo()
        code = self.run_main(s, forget, ["--init-baseline", "--read-data"])
        self.assertEqual(code, rm.EXIT_OK)
        self.assertEqual(self.check_call(), ["--no-lock", "--no-cache", "check", "--read-data"])
        self.assertEqual(self.report()["check"], {"read_data": "--read-data", "ok": True})

    def test_daily_check_without_read_data(self):
        s, forget = self.clean_repo()
        code, _ = self.run_with(s, forget, init=True)
        self.assertEqual(code, rm.EXIT_OK)
        self.assertEqual(self.check_call(), ["--no-lock", "--no-cache", "check"])
        self.assertEqual(self.report()["check"], {"read_data": "aucune", "ok": True})

    def test_read_data_failure_is_reported(self):
        s, forget = self.clean_repo()
        code = self.run_main(s, forget, ["--init-baseline", "--read-data"], exits={"check": 1})
        self.assertEqual(code, rm.EXIT_RESTIC)
        r = self.report()
        self.assertEqual((r["status"], r["check"]), ("restic_error", {"read_data": "--read-data", "ok": False}))
        self.assertFalse(os.path.exists(self.state_path()))

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

    # ------------------------------------------------------------------ opérations réelles

    @staticmethod
    def hhmm(minutes_from_now):
        return (dt.datetime.now() + dt.timedelta(minutes=minutes_from_now)).strftime("%H:%M")

    def enable(self, **apply):
        # Plage protégée loin de l'heure du test, sauf si le test la fixe lui-même.
        apply.setdefault("protected", {"start": self.hhmm(360), "end": self.hhmm(420)})
        for entry in rm.REPO_ENTRIES:
            path = os.path.join(self.repo, entry)
            if not os.path.exists(path):
                (os.mkdir if entry != "config" else lambda p: open(p, "w").close())(path)
        with open(self.config) as f:
            c = json.load(f)
        c["apply"] = dict(rm.NO_APPLY, repository_owner="%d:%d" % (os.getuid(), os.getgid()), **apply)
        with open(self.config, "w") as f:
            json.dump(c, f)

    def aged_repo(self, removed_days=(33, 34, 35), n=40):
        """n sauvegardes quotidiennes ; forget --dry-run propose de supprimer celles de removed_days."""
        now = dt.datetime.now(dt.timezone.utc)
        core = daily(n, end=now - dt.timedelta(hours=1))
        keep = [s for i, s in enumerate(core) if i not in removed_days]
        remove = [s for i, s in enumerate(core) if i in removed_days]
        v = validation()
        return core + [v], groups(keep, remove) + groups([v], paths=["/tmp/restic-test"]), remove

    def run_apply(self, snapshots, forget, args, locks="", **fixtures_extra):
        fixtures = {"version": "restic 0.19.1 compiled with go1.26.4 on linux/amd64", "snapshots": snapshots,
                    "list": locks, "check": "no errors were found", "forget": forget, "mounts": MOUNTS_OK,
                    "prune": "repacking packs\ndone", "exits": {}}
        fixtures.update(fixtures_extra)
        with open(self.fixtures, "w") as f:
            json.dump(fixtures, f)
        open(self.restic_log, "w").close()
        open(self.log, "w").close()
        return rm.main(["--config", self.config, "--ignore-window"] + args)

    def current_fixtures(self):
        with open(self.fixtures) as f:
            return json.load(f)

    def write_calls(self):
        with open(self.log) as f:
            calls = [json.loads(line) for line in f]
        return [c[c.index("restic-gate") + 1:] for c in calls
                if "restic-gate" in c and any(a.endswith(":/repo") for a in c)]

    def state(self):
        with open(self.state_path()) as f:
            return json.load(f)

    def baseline(self, snapshots, forget):
        self.assertEqual(self.run_apply(snapshots, forget, ["--init-baseline"]), rm.EXIT_OK)
        with open(self.state_path(), "rb") as f:
            self.reference = f.read()

    def assert_reference_unchanged(self):
        with open(self.state_path(), "rb") as f:
            self.assertEqual(f.read(), self.reference)

    def test_apply_disabled_by_default(self):
        s, forget, _ = self.aged_repo()
        for op in ("forget", "prune"):
            code = self.run_apply(s, forget, ["--apply", op])
            self.assertEqual(code, rm.EXIT_CONFIG)
            self.assertEqual(self.report()["status"], "apply_disabled")
            self.assertEqual(self.restic_calls(), [])

    def test_apply_is_exclusive_with_other_actions(self):
        with self.assertRaises(SystemExit):
            rm.main(["--config", self.config, "--apply", "forget", "--init-baseline"])
        with self.assertRaises(SystemExit):
            rm.main(["--config", self.config, "--apply", "unlock"])

    def test_forget_removes_exactly_the_dry_run_candidates(self):
        self.enable(forget=True)
        s, forget, remove = self.aged_repo()
        self.baseline(s, forget)
        code = self.run_apply(s, forget, ["--apply", "forget"])
        self.assertEqual(code, rm.EXIT_OK, self.report()["log"])
        ids = sorted(x["id"] for x in remove)
        self.assertEqual(self.write_calls(), [["--no-cache", "forget"] + ids])
        with open(self.log) as f:
            calls = [json.loads(line) for line in f]
        for c in calls:
            write = any(a.endswith(":/repo") for a in c)
            self.assertEqual("--user" in c, write)
            if write and "restic-gate" in c:
                self.assertNotIn("--no-lock", c)
        subs = [[a for a in c if not a.startswith("-")][0] for c in self.restic_calls()]
        self.assertEqual(subs, ["version", "snapshots", "list", "check", "forget", "forget", "snapshots", "list",
                                "check"])
        r = self.report()
        self.assertEqual((r["status"], r["apply"]["ok"], r["apply"]["removed"]), ("forget_done", True, ids))
        self.assertTrue(any(line.endswith("SUPPRIMÉ : %s (%s)" % (remove[0]["id"][:8], remove[0]["time"]))
                            for line in r["log"]))
        st = self.state()
        self.assertNotIn("in_progress", st)
        self.assertEqual(st["snapshot_ids"], sorted(x["id"] for x in s if x not in remove))
        # Le contrôle quotidien suivant ne voit aucune disparition inattendue.
        fx = self.current_fixtures()
        self.assertEqual(self.run_apply(fx["snapshots"], fx["forget"], []), rm.EXIT_OK)
        self.assertEqual(self.report()["status"], "ok")

    def test_backup_between_dry_run_and_forget(self):
        # Une sauvegarde terminée entre le contrôle et l'écriture : jamais supprimée, absente de la
        # référence, puis vérifiée comme nouvelle par le contrôle suivant.
        self.enable(forget=True)
        s, forget, remove = self.aged_repo()
        self.baseline(s, forget)
        new = snap("f" * 64, dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1))
        code = self.run_apply(s, forget, ["--apply", "forget"], appear=[new])
        r = self.report()
        self.assertEqual((code, r["status"]), (rm.EXIT_OK, "forget_done"), r["log"])
        self.assertEqual(self.write_calls(), [["--no-cache", "forget"] + sorted(x["id"] for x in remove)])
        self.assertTrue(any("laissé(s) au contrôle suivant : ffffffff" in l for l in r["log"]))
        self.assertNotIn(new["id"], self.state()["snapshot_ids"])
        fx = self.current_fixtures()
        self.assertIn(new["id"], [x["id"] for x in fx["snapshots"]])
        fx["forget"][0]["keep"].append(new)
        self.assertEqual(self.run_apply(fx["snapshots"], fx["forget"], []), rm.EXIT_OK)
        self.assertIn(new["id"], self.state()["snapshot_ids"])

    def test_keep_tag_end_to_end(self):
        # Le snapshot marqué n'est ni proposé par restic (fixture : keep) ni supprimable par le script
        # même s'il apparaissait dans les candidats.
        with open(self.config) as f:
            c = json.load(f)
        c["retention"]["keep_tags"] = ["keep"]
        with open(self.config, "w") as f:
            json.dump(c, f)
        self.enable(forget=True)
        s, forget, remove = self.aged_repo()
        self.baseline(s, forget)
        old = remove[0]
        marked = dict(old, id="d" * 64, original=old["id"], tags=TAGS + ["keep"])
        s2 = [marked if x is old else x for x in s]
        f2 = [dict(g, keep=g["keep"] + [marked], remove=[x for x in g["remove"] if x is not old]) if i == 0 else g
              for i, g in enumerate(forget)]
        self.assertEqual(self.run_apply(s2, f2, []), rm.EXIT_OK)        # marquage reconnu au contrôle
        self.assertIn("retagged", codes(self.report()["findings"], "INFO"))
        self.assertIn(marked["id"], self.state()["snapshot_ids"])
        self.assertIn("--keep-tag", self.restic_calls()[-1])
        self.assertEqual(self.run_apply(s2, f2, ["--apply", "forget"]), rm.EXIT_OK)
        self.assertEqual(self.write_calls(), [["--no-cache", "forget"] + sorted(x["id"] for x in remove[1:])])
        # Défense supplémentaire : si restic le proposait quand même, refus.
        fx = self.current_fixtures()
        bad = [dict(g, remove=[marked]) if i == 0 else g for i, g in enumerate(fx["forget"])]
        bad[0]["keep"] = [x for x in bad[0]["keep"] if x["id"] != marked["id"]]
        code = self.run_apply(fx["snapshots"], bad, ["--apply", "forget"])
        self.assertEqual((code, self.report()["status"]), (rm.EXIT_ANOMALY, "apply_refused"))
        self.assertIn("removal_not_allowed", codes(self.report()["findings"]))

    def test_forget_with_nothing_to_remove_writes_nothing(self):
        self.enable(forget=True)
        s, forget, _ = self.aged_repo(removed_days=())
        self.baseline(s, forget)
        self.assertEqual(self.run_apply(s, forget, ["--apply", "forget"]), rm.EXIT_OK)
        self.assertEqual(self.report()["status"], "nothing_to_remove")
        self.assertEqual(self.write_calls(), [])

    def assert_refused(self, code, finding_code, expected_code=rm.EXIT_ANOMALY, status="apply_refused"):
        """Refus : aucun conteneur d'écriture lancé, référence inchangée."""
        r = self.report()
        self.assertEqual((code, r["status"]), (expected_code, status), r["log"])
        if finding_code:
            self.assertIn(finding_code, codes(r["findings"]))
        self.assertEqual(self.write_calls(), [])

    def test_refusals(self):
        self.enable(forget=True, prune=True)
        s, forget, remove = self.aged_repo(n=60)
        self.baseline(s, forget)
        now = dt.datetime.now(dt.timezone.utc)
        old = s[:-1][3:]  # les trois dernières sauvegardes manquent : dernière il y a 3 jours
        intruder = snap("e" * 64, now - dt.timedelta(hours=2), host="inconnu")
        many = self.aged_repo(removed_days=tuple(range(31, 42)), n=60)  # 11 suppressions > 10, 18 % < 20 %
        frozen = groups([x for x in s[:-1] if x not in remove], remove + [s[-1]])
        cases = [
            ("stale", old + s[-1:], None),
            ("host_not_allowed", s + [intruder], None),
            ("remove_count", many[0], many[1]),
            ("removal_not_allowed", s, frozen),
            ("snapshots_disappeared", [x for x in s if x is not s[5]], None),
        ]
        for finding_code, snaps, fg in cases:
            fg = fg or groups([x for x in snaps if x["hostname"] == HOST and x["paths"] == PATHS and x not in remove],
                              [x for x in remove if x in snaps]) + groups([s[-1]], paths=["/tmp/restic-test"])
            if finding_code == "host_not_allowed":
                fg.append({"host": "inconnu", "paths": PATHS, "tags": None, "keep": [intruder], "remove": []})
            code = self.run_apply(snaps, fg, ["--apply", "forget"])
            self.assert_refused(code, finding_code)
            if finding_code == "remove_count":
                self.assertEqual(codes(self.report()["findings"]), ["remove_count"])  # ce seuil seul suffit
            self.assert_reference_unchanged()
        # Verrou présent : aucune opération réelle, aucun verrou supprimé.
        for op in ("forget", "prune"):
            code = self.run_apply(s, forget, ["--apply", op], locks="f" * 64)
            self.assert_refused(code, "locks")
            self.assert_reference_unchanged()
            self.assertNotIn("unlock", json.dumps(self.restic_calls()))
        # check en échec.
        code = self.run_apply(s, forget, ["--apply", "forget"], exits={"check": 1})
        self.assert_refused(code, None, rm.EXIT_RESTIC, "restic_error")
        self.assert_reference_unchanged()

    def test_refused_without_or_with_invalid_baseline(self):
        self.enable(forget=True)
        s, forget, _ = self.aged_repo()
        code = self.run_apply(s, forget, ["--apply", "forget"])
        self.assert_refused(code, None, rm.EXIT_BASELINE_REQUIRED, "baseline_required")
        self.assertFalse(os.path.exists(self.state_path()))
        self.write_state(b"{tronque")
        code = self.run_apply(s, forget, ["--apply", "forget"])
        self.assert_refused(code, "state_invalid")
        with open(self.state_path(), "rb") as f:
            self.assertEqual(f.read(), b"{tronque")

    def test_repository_owner_must_match(self):
        self.enable(forget=True)
        with open(self.config) as f:
            c = json.load(f)
        c["apply"]["repository_owner"] = "4242:4242"
        with open(self.config, "w") as f:
            json.dump(c, f)
        s, forget, _ = self.aged_repo()
        code = self.run_apply(s, forget, ["--apply", "forget"])
        self.assertEqual((code, self.report()["status"]), (rm.EXIT_CONFIG, "repository_owner"))
        self.assertEqual(self.restic_calls(), [])

    def test_write_container_mounts_checked_before_marker(self):
        self.enable(forget=True)
        s, forget, _ = self.aged_repo()
        self.baseline(s, forget)
        bad = MOUNTS_OK.replace("/repo btrfs ro,", "/repo btrfs rw,").replace("restic_password btrfs ro",
                                                                              "restic_password btrfs rw")
        code = self.run_apply(s, forget, ["--apply", "forget"], write_mounts=bad)
        self.assert_refused(code, None, rm.EXIT_CONFIG, "unsafe_mounts")
        self.assert_reference_unchanged()

    def test_write_gate_refusal_leaves_nothing_to_resolve(self):
        self.enable(forget=True)
        s, forget, _ = self.aged_repo()
        self.baseline(s, forget)
        bad = MOUNTS_OK.replace("/repo btrfs ro,", "/repo btrfs rw,") + "/dev/sdb1 /repo/data ext4 rw 0 0\n"
        code = self.run_apply(s, forget, ["--apply", "forget"], write_gate_mounts=bad)
        self.assertEqual((code, self.report()["status"]), (rm.EXIT_CONFIG, "unsafe_mounts"))
        self.assertNotIn("in_progress", self.state())
        self.assertEqual(len(self.current_fixtures()["snapshots"]), len(s))  # restic jamais lancé

    def test_locked_by_backup_leaves_nothing_to_resolve(self):
        self.enable(forget=True)
        s, forget, remove = self.aged_repo()
        self.baseline(s, forget)
        code = self.run_apply(s, forget, ["--apply", "forget"], write_exits={"forget": 11})
        self.assertEqual((code, self.report()["status"]), (rm.EXIT_RESTIC, "repository_locked"))
        self.assertNotIn("in_progress", self.state())
        self.assertEqual(len(self.current_fixtures()["snapshots"]), len(s))

    def test_interrupted_forget_is_reported_until_resolved(self):
        self.enable(forget=True)
        s, forget, remove = self.aged_repo()
        self.baseline(s, forget)
        code = self.run_apply(s, forget, ["--apply", "forget"], write_exits={"forget": 1}, forget_partial=1)
        self.assertEqual((code, self.report()["status"]), (rm.EXIT_RESTIC, "apply_failed"))
        self.assertEqual(self.report()["apply"]["removed"], [sorted(x["id"] for x in remove)[0]])
        ip = self.state()["in_progress"]
        self.assertEqual((ip["operation"], ip["ids"]), ("forget", sorted(x["id"] for x in remove)))
        fx = self.current_fixtures()
        self.assertEqual(len(fx["snapshots"]), len(s) - 1)
        # Contrôle quotidien : anomalie explicite (e-mail), pas de « disparition » pour l'identifiant demandé.
        code = self.run_apply(fx["snapshots"], fx["forget"], [])
        r = self.report()
        self.assertEqual(code, rm.EXIT_ANOMALY)
        self.assertEqual(codes(r["findings"]), ["operation_interrupted"])
        self.assertIn("in_progress", self.state())
        # Nouvelle opération réelle refusée tant que ce n'est pas résolu.
        code = self.run_apply(fx["snapshots"], fx["forget"], ["--apply", "forget"])
        self.assert_refused(code, "operation_interrupted")
        self.assertIn("in_progress", self.state())
        # Résolution refusée si un verrou subsiste ; jamais de suppression de verrou.
        code = self.run_apply(fx["snapshots"], fx["forget"], ["--resolve-interrupted"], locks="f" * 64)
        self.assertEqual((code, self.report()["status"]), (rm.EXIT_ANOMALY, "anomaly"))
        self.assertIn("in_progress", self.state())
        self.assertEqual(self.write_calls(), [])
        # Puis résolution (lecture seule) et retour à la normale.
        code = self.run_apply(fx["snapshots"], fx["forget"], ["--resolve-interrupted"])
        self.assertEqual((code, self.report()["status"]), (rm.EXIT_OK, "interrupted_resolved"))
        self.assertEqual(self.write_calls(), [])
        self.assertNotIn("in_progress", self.state())
        self.assertEqual(self.run_apply(fx["snapshots"], fx["forget"], []), rm.EXIT_OK)
        # La suppression peut reprendre : les deux restants.
        self.assertEqual(self.run_apply(fx["snapshots"], fx["forget"], ["--apply", "forget"]), rm.EXIT_OK)
        self.assertEqual(len(self.write_calls()[0]), 2 + 2)

    def test_resolve_refused_without_interruption(self):
        s, forget = self.clean_repo()
        self.assertEqual(self.run_apply(s, forget, ["--init-baseline"]), rm.EXIT_OK)
        code = self.run_apply(s, forget, ["--resolve-interrupted"])
        self.assertEqual((code, self.report()["status"]), (rm.EXIT_CONFIG, "nothing_to_resolve"))

    def test_unexpected_disappearance_during_interruption_still_reported(self):
        self.enable(forget=True)
        s, forget, remove = self.aged_repo()
        self.baseline(s, forget)
        self.run_apply(s, forget, ["--apply", "forget"], write_exits={"forget": 1}, forget_partial=1)
        fx = self.current_fixtures()
        other = fx["snapshots"][2]  # non demandé
        snaps = [x for x in fx["snapshots"] if x is not other and x["id"] != other["id"]]
        fg = [dict(g, keep=[x for x in g["keep"] if x["id"] != other["id"]]) for g in fx["forget"]]
        self.run_apply(snaps, fg, [])
        self.assertEqual(codes(self.report()["findings"]), ["operation_interrupted", "snapshots_disappeared"])
        code = self.run_apply(snaps, fg, ["--resolve-interrupted"])
        self.assertEqual((code, self.report()["status"]), (rm.EXIT_ANOMALY, "anomaly"))
        self.assertIn("in_progress", self.state())

    def test_prune(self):
        self.enable(prune=True)
        s, forget, _ = self.aged_repo()
        self.baseline(s, forget)
        code = self.run_apply(s, forget, ["--apply", "prune"])
        r = self.report()
        self.assertEqual((code, r["status"]), (rm.EXIT_OK, "prune_done"), r["log"])
        self.assertEqual(self.write_calls(), [["--no-cache", "prune", "--max-unused=5%"]])
        subs = [[a for a in c if not a.startswith("-")][0] for c in self.restic_calls()]
        self.assertEqual(subs, ["version", "snapshots", "list", "check", "forget", "prune", "prune", "snapshots",
                                "list", "check"])
        self.assertIn("--dry-run", self.restic_calls()[5])
        self.assertNotIn("in_progress", self.state())
        # Prune en échec après écriture : marqueur conservé, puis signalé par le contrôle quotidien.
        code = self.run_apply(s, forget, ["--apply", "prune"], write_exits={"prune": 1})
        self.assertEqual((code, self.report()["status"]), (rm.EXIT_RESTIC, "apply_failed"))
        self.assertEqual(self.state()["in_progress"]["operation"], "prune")
        self.assertEqual(self.run_apply(s, forget, []), rm.EXIT_ANOMALY)
        self.assertEqual(codes(self.report()["findings"]), ["operation_interrupted"])

    def test_prune_volume_of_pending_candidates_does_not_block_prune(self):
        self.enable(prune=True)
        s, forget, _ = self.aged_repo(removed_days=tuple(range(31, 43)), n=50)
        self.baseline(s, forget)
        self.assertEqual(self.run_apply(s, forget, ["--apply", "prune"]), rm.EXIT_OK)

    def test_graceful_stop_on_timeout_for_writes(self):
        runner = rm.Runner(cfg(), time.monotonic() + 0.5, lambda m: None)
        with self.assertRaises(rm.StepTimeout):
            runner._exec(["sleep", "5"], "conteneur-x", "restic prune (écriture)", graceful=True)
        with open(self.log) as f:
            calls = [json.loads(line) for line in f]
        self.assertEqual(calls[-1], ["stop", "--time", str(rm.STOP_GRACE_SECONDS), "conteneur-x"])

    def test_protected_period_refuses_and_bounds_real_operations(self):
        s, forget, _ = self.aged_repo()
        # Pendant la plage protégée : refus avant tout conteneur, même avec --ignore-window.
        self.enable(forget=True, protected={"start": self.hhmm(-2), "end": self.hhmm(3)})
        self.assertEqual(self.run_apply(s, forget, ["--apply", "forget"]), rm.EXIT_WINDOW)
        self.assertEqual(self.report()["status"], "protected_period")
        self.assertEqual(self.restic_calls(), [])
        # Trop près de son début : refus.
        self.enable(forget=True, protected={"start": self.hhmm(5), "end": self.hhmm(60)})
        self.assertEqual(self.run_apply(s, forget, ["--apply", "forget"]), rm.EXIT_WINDOW)
        self.assertEqual(self.restic_calls(), [])
        # Plus loin : délai raccourci pour arrêter le conteneur d'écriture avant le début.
        self.enable(forget=True, protected={"start": self.hhmm(31), "end": self.hhmm(60)})
        self.baseline(s, forget)
        self.assertEqual(self.run_apply(s, forget, ["--apply", "forget"]), rm.EXIT_OK)
        line = [l for l in self.report()["log"] if "arrêt garanti" in l][0]
        minutes = int(re.search(r"délai (\d+) min", line).group(1))
        self.assertTrue(27 <= minutes <= 29, line)
        # Le contrôle quotidien n'est pas concerné par la plage protégée.
        self.enable(protected={"start": self.hhmm(-2), "end": self.hhmm(3)})
        fx = self.current_fixtures()
        self.assertEqual(self.run_apply(fx["snapshots"], fx["forget"], []), rm.EXIT_OK)

    def test_real_operations_skip_scheduled_read_data(self):
        with open(self.config) as f:
            c = json.load(f)
        c["read_data"] = {"weekdays": list(range(1, 8)), "parts": 1}
        with open(self.config, "w") as f:
            json.dump(c, f)
        self.enable(forget=True)
        s, forget, _ = self.aged_repo()
        self.baseline(s, forget)
        self.assertIn("--read-data", self.check_call())          # contrôle : lecture des données
        self.assertEqual(self.run_apply(s, forget, ["--apply", "forget"]), rm.EXIT_OK)
        self.assertEqual(self.check_call(), ["--no-lock", "--no-cache", "check"])  # opération réelle : structure

    def test_seconds_until(self):
        now = time.mktime(dt.datetime(2026, 11, 7, 4, 0).timetuple())
        self.assertEqual(rm.seconds_until("04:55", now), 55 * 60)
        self.assertEqual(rm.seconds_until("03:00", now), 23 * 3600)

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
