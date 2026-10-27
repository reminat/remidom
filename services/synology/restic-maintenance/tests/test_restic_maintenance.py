"""Tests du script de maintenance — sans Docker, sans restic, sans NAS.

    python3 -m unittest discover -s services/synology/restic-maintenance/tests -v
"""

import datetime as dt
import json
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import restic_maintenance as rm  # noqa: E402

NOW = dt.datetime(2026, 10, 27, 2, 0, tzinfo=dt.timezone.utc)
HOST = "docker-test-01"
PATHS = ["/srv/docker"]


def cfg(**overrides):
    base = {
        "name": "docker-test", "mode": "dry-run", "image": "restic/restic:0.19.1",
        "repository": "/volume1/Restic/docker-test",
        "password_file": "/volume1/docker/restic-maintenance/secrets/restic_password_test",
        "state_dir": "/tmp/state", "log_dir": "/tmp/logs",
        "allowed_hosts": [HOST], "allowed_paths": PATHS,
        "retention": {"keep_within": "30d", "keep_daily": 14, "keep_weekly": 8, "keep_monthly": 12},
        "window": {"start": "01:00", "end": "04:30"},
        "timeout_minutes": 120,
        "thresholds": {"max_future_skew_minutes": 60, "max_age_hours": 48, "max_snapshots_per_24h": 6,
                       "backdate_tolerance_hours": 24, "max_remove_count": 10,
                       "max_remove_percent": 20, "min_keep_per_group": 7},
    }
    base.update(overrides)
    return base


def snap(sid, when, host=HOST, paths=None):
    return {"id": sid * 8 if len(sid) == 1 else sid, "time": when.strftime("%Y-%m-%dT%H:%M:%S.123456789+00:00"),
            "hostname": host, "paths": paths or PATHS}


def daily(n, end=NOW - dt.timedelta(hours=1)):
    return [snap("%064x" % i, end - dt.timedelta(days=i)) for i in range(n)]


def groups(keep, remove=()):
    return [{"host": HOST, "paths": PATHS, "tags": None, "keep": list(keep), "remove": list(remove)}]


def codes(findings, level="ERROR"):
    return sorted(f["code"] for f in findings if f["level"] == level)


class AnalyzeTest(unittest.TestCase):
    def test_clean_repository(self):
        s = daily(20)
        f, summary = rm.analyze(cfg(), s, groups(s), NOW, None)
        self.assertEqual(codes(f), [])
        self.assertEqual(codes(f, "INFO"), ["baseline"])
        self.assertEqual(summary["would_remove"], 0)

    def test_unknown_host_and_path(self):
        s = daily(10) + [snap("a", NOW - dt.timedelta(hours=2), host="intrus", paths=["/etc"])]
        f, _ = rm.analyze(cfg(), s, groups(s), NOW, None)
        self.assertEqual(codes(f), ["host_not_allowed", "path_not_allowed"])

    def test_future_snapshot(self):
        s = daily(10) + [snap("f", NOW + dt.timedelta(days=400))]
        f, _ = rm.analyze(cfg(), s, groups(s), NOW, None)
        self.assertIn("future_snapshot", codes(f))

    def test_small_clock_skew_tolerated(self):
        s = daily(10) + [snap("f", NOW + dt.timedelta(minutes=30))]
        f, _ = rm.analyze(cfg(), s, groups(s), NOW, None)
        self.assertNotIn("future_snapshot", codes(f))

    def test_stale_and_missing_host(self):
        s = daily(10, end=NOW - dt.timedelta(days=3))
        f, _ = rm.analyze(cfg(allowed_hosts=[HOST, "docker-test-02"]), s, groups(s), NOW, None)
        self.assertEqual(codes(f), ["no_snapshot", "stale"])

    def test_burst_detected_anywhere_in_history(self):
        old = NOW - dt.timedelta(days=200)
        s = daily(10) + [snap("%064x" % (900 + i), old + dt.timedelta(minutes=i)) for i in range(7)]
        f, _ = rm.analyze(cfg(), s, groups(s), NOW, None)
        self.assertIn("burst", codes(f))

    def test_backdated_and_disappeared_snapshots(self):
        s = daily(10)
        state = {"last_run": (NOW - dt.timedelta(days=1)).isoformat(),
                 "snapshot_ids": [x["id"] for x in s] + ["d" * 64]}
        forged = snap("b", NOW - dt.timedelta(days=5))
        f, _ = rm.analyze(cfg(), s + [forged], groups(s + [forged]), NOW, state)
        self.assertEqual(codes(f), ["backdated_snapshot", "snapshots_disappeared"])

    def test_new_snapshot_after_last_run_is_fine(self):
        s = daily(10)
        state = {"last_run": (NOW - dt.timedelta(days=1)).isoformat(), "snapshot_ids": [x["id"] for x in s[1:]]}
        f, _ = rm.analyze(cfg(), s, groups(s), NOW, state)
        self.assertEqual(codes(f), [])

    def test_removal_thresholds(self):
        s = daily(40)
        f, summary = rm.analyze(cfg(), s, groups(s[:25], s[25:]), NOW, None)
        self.assertEqual(codes(f), ["remove_count", "remove_percent"])
        self.assertEqual(summary["would_remove"], 15)

    def test_min_keep_per_group(self):
        s = daily(10)
        f, _ = rm.analyze(cfg(), s, groups(s[:5], s[5:]), NOW, None)
        self.assertIn("min_keep", codes(f))

    def test_forget_must_cover_all_snapshots(self):
        s = daily(10)
        f, _ = rm.analyze(cfg(), s, groups(s[:8]), NOW, None)
        self.assertIn("forget_mismatch", codes(f))


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
        self.assertIn("restic_password_test:/run/secrets/restic_password:ro", joined)
        for flag in ("--network none", "--read-only", "--cap-drop ALL", "--pull never", "no-new-privileges"):
            self.assertIn(flag, joined)
        self.assertNotIn("RESTIC_PASSWORD=", joined)


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

    def test_empty_allowlists_refused(self):
        with self.assertRaises(rm.ConfigError):
            rm.load_config(self.write(cfg(allowed_hosts=[])))

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
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
if args[0] == "kill":
    sys.exit(0)
image = args.index("{image}")
sub = [a for a in args[image + 1:] if not a.startswith("-")][0]
with open(os.environ["FAKE_FIXTURES"]) as fx:
    fixtures = json.load(fx)
out = fixtures.get(sub, "")
sys.stdout.write(out if isinstance(out, str) else json.dumps(out))
'''


class EndToEndTest(unittest.TestCase):
    """Exécute main() avec un faux binaire docker qui enregistre ses arguments."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        os.mkdir(self.repo)
        self.secret = os.path.join(self.tmp, "secret")
        open(self.secret, "w").close()
        os.chmod(self.secret, 0)  # illisible : le script ne doit jamais l'ouvrir
        self.log = os.path.join(self.tmp, "docker.log")
        self.fixtures = os.path.join(self.tmp, "fixtures.json")
        docker = os.path.join(self.tmp, "docker")
        with open(docker, "w") as f:
            f.write(FAKE_DOCKER.replace("{python}", sys.executable).replace("{image}", "restic/restic:0.19.1"))
        os.chmod(docker, stat.S_IRWXU)
        self._orig = rm.DOCKER_BIN
        rm.DOCKER_BIN = docker
        os.environ.update(FAKE_LOG=self.log, FAKE_FIXTURES=self.fixtures)
        self.config = os.path.join(self.tmp, "config.json")
        c = cfg(repository=self.repo, password_file=self.secret,
                state_dir=os.path.join(self.tmp, "state"), log_dir=os.path.join(self.tmp, "logs"))
        with open(self.config, "w") as f:
            json.dump(c, f)

    def tearDown(self):
        rm.DOCKER_BIN = self._orig

    def run_with(self, snapshots, forget):
        with open(self.fixtures, "w") as f:
            json.dump({"version": "restic 0.19.1", "snapshots": snapshots, "list": "",
                       "check": "no errors were found", "forget": forget}, f)
        open(self.log, "w").close()
        code = rm.main(["--config", self.config, "--ignore-window"])
        with open(self.log) as f:
            calls = [json.loads(line) for line in f]
        return code, calls

    def test_full_dry_run_then_backdated_insertion(self):
        now = dt.datetime.now(dt.timezone.utc)
        s = daily(20, end=now - dt.timedelta(hours=1))
        code, calls = self.run_with(s, groups(s))
        self.assertEqual(code, rm.EXIT_OK)
        subs = []
        for call in calls:
            joined = " ".join(call)
            self.assertIn("%s:/repo:ro" % self.repo, joined)
            self.assertIn("--no-lock", call)
            self.assertNotIn("prune", call)
            subs.append([a for a in call[call.index("restic/restic:0.19.1") + 1:] if not a.startswith("-")][0])
            if "forget" in call:
                self.assertIn("--dry-run", call)
        self.assertEqual(subs, ["version", "snapshots", "list", "check", "forget"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "state", "state.json")))

        forged = snap("b", now - dt.timedelta(days=4))
        code, _ = self.run_with(s + [forged], groups(s + [forged]))
        self.assertEqual(code, rm.EXIT_ANOMALY)
        reports = sorted(os.listdir(os.path.join(self.tmp, "logs")))
        with open(os.path.join(self.tmp, "logs", reports[-1])) as f:
            last = json.load(f)
        self.assertEqual(last["status"], "anomaly")
        self.assertIn("backdated_snapshot", [f["code"] for f in last["findings"]])

    def test_concurrent_run_refused(self):
        import fcntl
        os.makedirs(os.path.join(self.tmp, "state"))
        with open(os.path.join(self.tmp, "state", "lock"), "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, calls = self.run_with([], [])
        self.assertEqual(code, rm.EXIT_LOCKED)
        self.assertEqual(calls, [])

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
