"""Tests unitaires de la fusion de configuration.yaml de Zigbee2MQTT (module z2m_config).

Valeurs fictives uniquement. Nécessite ansible-core (pour importer le module) et PyYAML :

    python3 -m unittest discover -s infra/ansible/tests -p 'test_z2m_config.py' -v
"""

import importlib.util
import os
import re
import tempfile
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(HERE, "..", "roles", "compose_deploy", "library", "z2m_config.py")
spec = importlib.util.spec_from_file_location("z2m_config", MODULE)
z2m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(z2m)

KEY = list(range(201, 217))
SECRET = "SENTINEL_MQTT_9c1e"
LEAK = re.compile(r"SENTINEL|201, ?202, ?203")

# Chaînes piégeuses : chacune est lue comme une chaîne par Zigbee2MQTT (entre guillemets).
TRICKY = ["on", "off", "yes", "no", "y", "n", "true", "False", "null", "~", "0x1234", "0o17",
          "012", "1:30", "1_000", "1e3", ".inf", "2026-11-18", "été/salon", "#dièse", "a: b",
          "- tiret", "apostrophe'", '"guillemets"', "", " espace", "{{ value_json.x }}"]


def managed(transmit_power=20, key=None, password=SECRET, version=5):
    """Fragment rendu tel que le produit configuration.yaml.j2."""
    key = KEY if key is None else key
    return (
        "homeassistant:\n  enabled: true\nfrontend:\n  enabled: true\n"
        "mqtt:\n  server: mqtt://mosquitto:1883\n  user: mqtt\n  password: %s\n"
        "serial:\n  port: tcp://smlight.example.invalid:6638\n  baudrate: 115200\n"
        "  adapter: ember\n  disable_led: false\n"
        "advanced:\n  transmit_power: %d\n  network_key: %s\nversion: %d\n"
        % (password, transmit_power, z2m.yaml.dump(key, default_flow_style=True).strip(), version)
    )


def ieee(i):
    return "0x00158d00%08x" % (0xA0000 + i)


def existing(names=39, version=5, transmit_power=20, key=None, extra=""):
    """configuration.yaml dans le style de Zigbee2MQTT (js-yaml) : noms, groupes, clés inconnues."""
    key = KEY if key is None else key
    lines = [
        "homeassistant:", "  enabled: true", "  experimental_event_entities: true",
        "frontend:", "  enabled: true", "  port: 8080",
        "mqtt:", "  server: mqtt://mosquitto:1883", "  user: mqtt", "  password: " + SECRET,
        "  base_topic: zigbee2mqtt", "  version: 5",
        "serial:", "  port: tcp://smlight.example.invalid:6638", "  baudrate: 115200",
        "  adapter: ember", "  disable_led: false",
        "advanced:", "  transmit_power: %d" % transmit_power,
        "  network_key:",
    ] + ["    - %d" % b for b in key] + [
        "  pan_id: 6754", "  ext_pan_id:", "    - 221", "    - 221", "    - 221", "    - 221",
        "    - 221", "    - 221", "    - 221", "    - 221",
        "  channel: 15", "  log_level: info", "  last_seen: ISO_8601",
        "availability:", "  enabled: true", "  active:", "    timeout: 10",
        "ota:", "  update_check_interval: 1440", "  disable_automatic_update_check: false",
        "blocklist:", "  - '0x00158d00ffffffff'",
        "future_feature:", "  nested:", "    deep:", "      - 1", "      - a: b", "        c: null",
        "  ratio: 0.5", "  empty_map: {}", "  empty_list: []",
        "devices:",
    ]
    for i in range(names):
        name = TRICKY[i] if i < len(TRICKY) else "piece/appareil_%02d" % i
        lines += ["  '%s':" % ieee(i), "    friendly_name: " + yaml_quote(name)]
        if i % 5 == 0:
            lines += ["    retain: false", "    qos: 1", "    debounce: 0.5",
                      "    filtered_attributes:", "      - linkquality", "      - battery"]
        if i % 7 == 0:
            lines += ["    homeassistant:", "      temperature:",
                      "        value_template: '{{ value_json.temperature | round(1) }}'"]
    lines += [
        "groups:",
        "  '1':", "    friendly_name: salon_tout", "    devices:",
        "      - %s/1" % ieee(0), "      - %s/1" % ieee(1),
        "  '2':", "    friendly_name: 'on'", "    retain: false",
        "version: %d" % version,
    ]
    return ("\n".join(lines) + "\n" + extra).encode("utf-8")


def yaml_quote(s):
    """Apostrophes à la manière de js-yaml."""
    return "'" + s.replace("'", "''") + "'"


def without(data, paths):
    """Copie de `data` sans les feuilles listées (chemins pointés)."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in data.items()}
    for p in paths:
        node, parts = out, p.split(".")
        for part in parts[:-1]:
            node = node.get(part, {})
        if isinstance(node, dict):
            node.pop(parts[-1], None)
    return out


MANAGED_LEAVES = z2m.leaves(z2m.load(managed(), "test"))


class Creation(unittest.TestCase):
    def test_absent_file_gets_rendered_fragment_verbatim(self):
        r = z2m.plan(None, managed())
        self.assertTrue(r["created"])
        self.assertEqual(r["content"], managed().encode())
        self.assertIn("advanced.network_key", r["changed_paths"])

    def test_bad_fragment_key_refused(self):
        for bad in ([1, 2, 3], ["GENERATE"], KEY[:15] + [300]):
            with self.assertRaises(z2m.ConfigError):
                z2m.plan(None, managed(key=bad))
        with self.assertRaises(z2m.ConfigError):
            z2m.plan(None, managed().replace(z2m.yaml.dump(KEY, default_flow_style=True).strip(),
                                             "GENERATE"))


class Merge(unittest.TestCase):
    def test_conform_file_untouched(self):
        r = z2m.plan(existing(), managed())
        self.assertIsNone(r["content"])
        self.assertEqual(r["changed_paths"], [])

    def test_formatting_only_untouched(self):
        cur = z2m.load(existing().decode(), "t")
        # Même contenu, autre formatage : style « flow », indentation 4, commentaires.
        text = "# commentaire\n" + yaml.dump(cur, Dumper=z2m.Dumper, default_flow_style=True,
                                              indent=4, sort_keys=False, allow_unicode=True)
        self.assertTrue(z2m.same(z2m.load(text, "t"), cur))
        self.assertIsNone(z2m.plan(text.encode(), managed())["content"])

    def test_repeated_deploys_idempotent(self):
        data = existing(transmit_power=10)
        r = z2m.plan(data, managed())
        self.assertEqual(r["changed_paths"], ["advanced.transmit_power"])
        data = r["content"]
        for _ in range(3):
            self.assertIsNone(z2m.plan(data, managed())["content"])

    def test_managed_change_only_touches_that_leaf(self):
        before = z2m.load(existing(transmit_power=10).decode(), "t")
        r = z2m.plan(existing(transmit_power=10), managed())
        after = z2m.load(r["content"].decode(), "t")
        self.assertEqual(after["advanced"]["transmit_power"], 20)
        # Tout le reste, sections gérées comprises, est identique (valeurs, types, ordre).
        self.assertTrue(z2m.same(without(after, ["advanced.transmit_power"]),
                                 without(before, ["advanced.transmit_power"])))
        self.assertEqual(list(after), list(before))

    def test_all_unmanaged_content_preserved(self):
        before = z2m.load(existing(transmit_power=10).decode(), "t")
        after = z2m.load(z2m.plan(existing(transmit_power=10), managed())["content"].decode(), "t")
        self.assertTrue(z2m.same(without(after, MANAGED_LEAVES), without(before, MANAGED_LEAVES)))
        for section in ("devices", "groups", "availability", "ota", "blocklist", "future_feature"):
            self.assertTrue(z2m.same(after[section], before[section]), section)
        self.assertEqual(len(after["devices"]), 39)
        self.assertEqual(sum(1 for d in after["devices"].values() if "friendly_name" in d), 39)

    def test_types_after_write(self):
        after = z2m.load(z2m.plan(existing(transmit_power=10), managed())["content"].decode(), "t")
        legacy = yaml.safe_load(z2m.plan(existing(transmit_power=10), managed())["content"])
        for doc in (after, legacy):  # lecture js-yaml et YAML 1.1 : mêmes types
            names = [d["friendly_name"] for d in doc["devices"].values()]
            self.assertEqual(names[:len(TRICKY)], TRICKY)
            self.assertTrue(all(isinstance(k, str) and k.startswith("0x") for k in doc["devices"]))
            self.assertEqual(doc["blocklist"], ["0x00158d00ffffffff"])
            self.assertEqual(doc["groups"]["2"]["friendly_name"], "on")
            self.assertEqual(list(doc["groups"]), ["1", "2"])
            self.assertEqual(doc["advanced"]["network_key"], KEY)
            self.assertEqual(doc["advanced"]["ext_pan_id"], [221] * 8)
            self.assertIs(doc["serial"]["disable_led"], False)
            self.assertEqual(doc["future_feature"]["nested"]["deep"], [1, {"a": "b", "c": None}])
            self.assertEqual(doc["future_feature"]["ratio"], 0.5)
            dev0 = doc["devices"][ieee(0)]
            self.assertEqual(dev0["filtered_attributes"], ["linkquality", "battery"])
            self.assertEqual(dev0["homeassistant"]["temperature"]["value_template"],
                             "{{ value_json.temperature | round(1) }}")

    def test_plain_scalars_read_like_zigbee2mqtt(self):
        # Valeurs non quotées : lues comme js-yaml (YAML 1.2), donc « on » reste une chaîne.
        extra = "plain:\n  a: on\n  b: yes\n  c: 012\n  d: 0o17\n  e: 1:30\n  f: 0x1F\n  g: True\n"
        r = z2m.plan(existing(transmit_power=10, extra=extra), managed())
        for doc in (z2m.load(r["content"].decode(), "t"), yaml.safe_load(r["content"])):
            self.assertEqual(doc["plain"], {"a": "on", "b": "yes", "c": 12, "d": 15, "e": "1:30",
                                            "f": 31, "g": True})

    def test_version_never_downgraded_nor_added(self):
        self.assertIsNone(z2m.plan(existing(version=6), managed())["content"])
        r = z2m.plan(existing(version=6, transmit_power=10), managed())
        self.assertEqual(z2m.load(r["content"].decode(), "t")["version"], 6)
        no_version = existing(transmit_power=10).replace(b"version: 5\n", b"")
        r = z2m.plan(no_version, managed())
        self.assertNotIn("version", z2m.load(r["content"].decode(), "t"))

    def test_managed_section_not_a_mapping(self):
        data = existing().replace(b"homeassistant:\n  enabled: true\n  experimental_event_entities: true\n",
                                  b"homeassistant: true\n")
        r = z2m.plan(data, managed())
        self.assertEqual(r["changed_paths"], ["homeassistant"])
        self.assertEqual(z2m.load(r["content"].decode(), "t")["homeassistant"], {"enabled": True})

    def test_new_managed_key_added(self):
        data = existing().replace(b"  disable_led: false\n", b"")
        r = z2m.plan(data, managed())
        self.assertEqual(r["changed_paths"], ["serial.disable_led"])

    def test_type_change_is_a_change(self):
        data = existing().replace(b"  baudrate: 115200\n", b"  baudrate: '115200'\n")
        self.assertEqual(z2m.plan(data, managed())["changed_paths"], ["serial.baudrate"])

    def test_secret_change_reported_by_path_only(self):
        r = z2m.plan(existing(), managed(password="SENTINEL_NEW_PASSWORD"))
        self.assertEqual(r["changed_paths"], ["mqtt.password"])
        self.assertFalse(LEAK.search(repr(r["changed_paths"])))


class Refusals(unittest.TestCase):
    def assertRefused(self, data, frag=None, contains=""):
        with self.assertRaises(z2m.ConfigError) as ctx:
            z2m.plan(data, frag or managed())
        msg = str(ctx.exception)
        self.assertFalse(LEAK.search(msg), msg)
        self.assertIn(contains, msg)

    def test_network_key_different(self):
        self.assertRefused(existing(key=list(range(16))), contains="différente")

    def test_network_key_absent(self):
        data = re.sub(rb"  network_key:\n(    - \d+\n){16}", b"", existing())
        self.assertRefused(data, contains="absente")

    def test_network_key_generate(self):
        data = re.sub(rb"  network_key:\n(    - \d+\n){16}", b"  network_key: GENERATE\n", existing())
        self.assertRefused(data, contains="différente")

    def test_invalid_yaml_no_snippet(self):
        data = existing() + b"mqtt: [SENTINEL_BROKEN: {\n"
        self.assertRefused(data, contains="ligne")

    def test_empty_or_not_mapping(self):
        for data in (b"", b"\n", b"- a\n- b\n", b"SENTINEL_scalar\n"):
            self.assertRefused(data)

    def test_not_utf8(self):
        self.assertRefused(b"mqtt:\n  password: \xff\xfe\n")


class Backups(unittest.TestCase):
    def test_rotation_and_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            bdir = os.path.join(d, "config-backups")
            made = [z2m.backup(os.path.join(d, "configuration.yaml"), b"v%d" % i, bdir, 3)
                    for i in range(5)]
            kept = sorted(os.listdir(bdir))
            self.assertEqual(kept, sorted(os.path.basename(p) for p in made[-3:]))
            self.assertEqual(os.stat(bdir).st_mode & 0o777, 0o700)
            for name in kept:
                self.assertEqual(os.stat(os.path.join(bdir, name)).st_mode & 0o777, 0o600)
            with open(made[-1], "rb") as f:
                self.assertEqual(f.read(), b"v4")


if __name__ == "__main__":
    unittest.main()
