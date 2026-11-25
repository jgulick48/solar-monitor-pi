"""Tests for the settings file (setup/configfile.py): what is exported, secrets left out or encrypted, and the plan
an import makes before anything is written.
Run: python3 -m unittest discover -s tests -v"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import configfile  # noqa: E402
import display  # noqa: E402
import modules  # noqa: E402
import protect  # noqa: E402

SITE = {"name": "Ranch", "time_zone": "America/Denver", "latitude": "39.7", "battery_count": "6", "low_soc_alert": "25",
        "spare_notify": False, "arrays": [{"name": "Array 1", "panels": "24"}], "costs": {"fuel_per_gal": "3.5"}}
SITE_DEFAULTS = {"name": "Solar", "time_zone": "", "latitude": "", "battery_count": "6", "low_soc_alert": "25",
                 "spare_notify": False, "arrays": [], "costs": {"fuel_per_gal": ""}}
HAVE_OPENSSL = shutil.which("openssl") is not None


def box(tmp, name, site=SITE):
    stack = os.path.join(tmp, name)
    os.makedirs(os.path.join(stack, "state"))
    with open(os.path.join(stack, "state", "site.json"), "w") as f:
        json.dump(site, f)
    return stack


class ConfigFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SOLAR_COMPOSE"] = "true"
        self.a = box(self.tmp.name, "a")
        d = display.load(self.a)
        d.update(volume=40, cameras=[{"name": "Gate", "url": "rtsps://h:7441/SECRETTOKEN"}])
        display.save(self.a, d)
        protect.save(self.a, {**protect.DEFAULTS, "host": "192.0.2.1", "api_key": "PROTECTKEY", "ha_added": True,
                              "cameras": [{"id": "c1", "name": "Gate"}]})
        modules.save(self.a, "notifications", {**modules.settings(self.a, "notifications"), "ntfy": True,
                                               "ntfy_topic": "TOPICSECRET", "ntfy_token": "TOKENSECRET", "quiet_from": "21:30"})
        modules.save(self.a, "freeze", {"sensors": ["sensor.slab"], "below_c": 7.0})
        modules.save(self.a, "probeboard", {**modules.settings(self.a, "probeboard"), "installed": 123, "names": ["Kitchen"]})
        modules.save(self.a, "tunnel", {**modules.settings(self.a, "tunnel"), "api_token": "CFTOKEN"})
        self.b = box(self.tmp.name, "b", {"name": "Solar", "time_zone": "America/Denver", "battery_count": "4"})

    def tearDown(self):
        os.environ.pop("SOLAR_COMPOSE", None)
        modules._ALL = None
        self.tmp.cleanup()

    def test_every_module_is_decided(self):
        """A new module has to be put in PORTABLE (its settings travel) or LOCAL (they belong to one box)."""
        self.assertEqual(sorted(m.id for m in modules.all_modules()), sorted(configfile.PORTABLE + configfile.LOCAL))

    def test_export_leaves_secrets_and_box_state_out(self):
        doc = configfile.export(self.a, "v1")
        text = json.dumps(doc)
        for secret in ("SECRETTOKEN", "PROTECTKEY", "TOPICSECRET", "TOKENSECRET", "CFTOKEN"):
            self.assertNotIn(secret, text)
        self.assertEqual((doc["solar_monitor_config"], doc["name"], doc["secrets"], doc["secrets_left_out"]), (1, "Ranch", None, 4))
        f = doc["files"]
        self.assertEqual(f["site.json"]["battery_count"], "6")
        self.assertNotIn("cameras", f["display.json"])                     # stream links are secrets
        self.assertEqual(f["display.json"]["volume"], 40)
        self.assertEqual(f["protect.json"]["host"], "192.0.2.1")
        self.assertNotIn("ha_added", f["protect.json"])                    # this box's state
        self.assertEqual(f["modules/freeze.json"], {"enabled": False, "settings": {"sensors": ["sensor.slab"], "below_c": 7.0}})
        self.assertEqual(f["modules/probeboard.json"]["settings"], {"names": ["Kitchen"]})   # not that it was installed
        self.assertNotIn("modules/tunnel.json", f)                         # remote access belongs to one box
        self.assertNotIn("secrets.json", f)

    def test_plan_changes_nothing_until_applied(self):
        doc = configfile.parse(json.dumps(configfile.export(self.a, "v1")))
        before = json.dumps(configfile.current(self.b), sort_keys=True)
        plan = configfile.plan(self.b, doc, "", SITE_DEFAULTS)
        self.assertEqual(json.dumps(configfile.current(self.b), sort_keys=True), before)
        ch = {(n, p): (old, new, sec) for n, p, old, new, sec in plan["changes"]}
        self.assertEqual(ch[("site.json", "name")], ("Solar", "Ranch", False))
        self.assertEqual(ch[("site.json", "battery_count")], ("4", "6", False))
        self.assertEqual(ch[("modules/freeze.json", "settings.below_c")][1], 7.0)
        self.assertEqual(ch[("modules/notifications.json", "settings.quiet_from")][1], "21:30")
        self.assertNotIn(("site.json", "time_zone"), ch)                   # already the same
        self.assertGreater(plan["same"], 3)
        self.assertEqual(plan["files"]["site.json"]["costs"], {"fuel_per_gal": "3.5"})
        self.assertTrue(any("exported without its 4 secrets" in n for n in plan["notes"]))
        self.assertNotIn("api_key", [p for (n, p) in ch if n == "protect.json"])
        # importing a box's own export changes nothing
        own = configfile.plan(self.a, doc, "", SITE_DEFAULTS)
        self.assertEqual((own["changes"], own["files"]), ([], {}))

    def test_bad_files_and_values(self):
        for text in ("not json", "[]", '{"files": {}}', json.dumps({"solar_monitor_config": 99, "files": {}}), "x" * (configfile.MAX_BYTES + 1)):
            with self.assertRaises(configfile.ConfigError):
                configfile.parse(text)
        doc = {"solar_monitor_config": 1, "files": {
            "site.json": {"battery_count": ["6"], "spare_notify": "yes", "name": "Ranch", "arrays": "none"},
            "secrets.json": {"grafana_admin": "x"}, "modules/tunnel.json": {"enabled": True}, "../../etc/passwd": {"a": 1},
            "modules/freeze.json": {"enabled": "yes", "settings": {"sensors": "sensor.slab", "below_c": 6}}, "display.json": [1]}}
        plan = configfile.plan(self.b, configfile.parse(json.dumps(doc)), "", SITE_DEFAULTS)
        self.assertEqual([(n, p) for n, p, *_ in plan["changes"]], [("modules/freeze.json", "settings.below_c"), ("site.json", "name")])
        self.assertEqual(set(plan["files"]), {"site.json", "modules/freeze.json"})
        notes = " ".join(plan["notes"])
        for part in ("site.json: battery_count isn't the kind", "site.json: spare_notify", "site.json: arrays", "secrets.json: not something",
                     "modules/tunnel.json: not something", "../../etc/passwd: not something", "modules/freeze.json: enabled",
                     "modules/freeze.json: settings.sensors", "display.json: not in the expected form"):
            self.assertIn(part, notes)
        doc["files"] = {"site.json": {"time_zone": "Mars/Olympus"}}
        with self.assertRaises(configfile.ConfigError):
            configfile.plan(self.b, doc, "", SITE_DEFAULTS)

    @unittest.skipUnless(HAVE_OPENSSL, "needs openssl")
    def test_secrets_encrypted_with_a_passphrase(self):
        with self.assertRaises(configfile.ConfigError):
            configfile.export(self.a, "v1", "short")
        doc = configfile.export(self.a, "v1", "correct horse battery")
        text = json.dumps(doc)
        for secret in ("SECRETTOKEN", "PROTECTKEY", "TOPICSECRET", "TOKENSECRET"):
            self.assertNotIn(secret, text)
        self.assertEqual((doc["secrets"]["cipher"], doc["secrets_left_out"]), ("aes-256-cbc", 0))
        doc = configfile.parse(text)
        with self.assertRaises(configfile.ConfigError):
            configfile.plan(self.b, doc, "wrong passphrase", SITE_DEFAULTS)
        skipped = configfile.plan(self.b, doc, "", SITE_DEFAULTS)
        self.assertTrue(any("no passphrase was given" in n for n in skipped["notes"]))
        self.assertFalse(skipped["files"].get("protect.json", {}).get("api_key"))     # the box keeps its own (none)
        plan = configfile.plan(self.b, doc, "correct horse battery", SITE_DEFAULTS)
        self.assertEqual(plan["files"]["protect.json"]["api_key"], "PROTECTKEY")
        self.assertEqual(plan["files"]["display.json"]["cameras"][0]["url"], "rtsps://h:7441/SECRETTOKEN")
        self.assertEqual(plan["files"]["modules/notifications.json"]["settings"]["ntfy_topic"], "TOPICSECRET")
        sec = {(n, p): s for n, p, _, _, s in plan["changes"]}
        self.assertTrue(sec[("protect.json", "api_key")] and sec[("display.json", "cameras")])
        self.assertEqual(configfile.show("PROTECTKEY", True), "(hidden)")   # never shown in the preview

    def test_import_through_the_web_pages(self):
        """Preview shows the changes and hides nothing it shouldn't; import writes them, keeps the old settings in a
        file, and turns modules on as the file says."""
        import solar_web
        old = solar_web.STACK, solar_web.ss.host_timezone, solar_web.apply_site
        solar_web.STACK = self.b
        solar_web.ss.host_timezone = lambda: "America/Denver"            # don't try to set this machine's time zone
        applied = []
        solar_web.apply_site = lambda site: applied.append(site["name"]) or ["restarted homeassistant"]
        try:
            d = modules.load(self.a, "freeze")
            d["enabled"] = True
            with open(modules.path(self.a, "freeze"), "w") as f:
                json.dump(d, f)
            text = json.dumps(configfile.export(self.a, "v1"))
            page = solar_web.config_preview({"config": text})
            self.assertIn("Import: what would change", page)
            self.assertIn("<td>battery_count</td><td>4</td><td><b>6</b></td>", page)
            self.assertIn("<td>enabled</td><td>off</td><td><b>on</b></td>", page)
            self.assertEqual(json.load(open(os.path.join(self.b, "state", "site.json")))["battery_count"], "4")   # not yet
            self.assertIn("Nothing imported", solar_web.config_preview({"config": "nope"}))
            page = solar_web.config_import({"config": text})
            self.assertIn("Imported", page)
            self.assertEqual(applied, ["Ranch"])
            site = json.load(open(os.path.join(self.b, "state", "site.json")))
            self.assertEqual((site["name"], site["battery_count"]), ("Ranch", "6"))
            self.assertEqual(display.load(self.b)["volume"], 40)
            self.assertEqual(protect.settings(self.b)["host"], "192.0.2.1")
            fz = modules.load(self.b, "freeze")
            self.assertEqual((fz["enabled"], fz["settings"]["below_c"]), (True, 7.0))
            kept = json.load(open(os.path.join(self.b, "state", "config-before-import.json")))
            self.assertEqual(kept["files"]["site.json"]["battery_count"], "4")
            self.assertEqual(os.stat(os.path.join(self.b, "state", "config-before-import.json")).st_mode & 0o777, 0o600)
            self.assertIn("Nothing would change", solar_web.config_preview({"config": text}))
            self.assertIn("Nothing to import", solar_web.config_import({"config": text}))
            self.assertIn("Download the settings file", solar_web.config_page())
        finally:
            solar_web.STACK, solar_web.ss.host_timezone, solar_web.apply_site = old

    def test_show_and_command_line(self):
        self.assertEqual([configfile.show(v) for v in (None, True, False, "x", 5, ["a"])], ["(not set)", "on", "off", "x", "5", '["a"]'])
        self.assertTrue(configfile.show("y" * 200).endswith("..."))
        out = io.StringIO()
        with redirect_stdout(out):
            configfile.main(["export", "--stack", self.a])
        self.assertEqual(json.loads(out.getvalue())["name"], "Ranch")


if __name__ == "__main__":
    unittest.main()
