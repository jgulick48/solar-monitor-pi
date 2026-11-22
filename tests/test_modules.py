"""Tests for the module core (setup/modules/): settings files and their migration, and what an enabled or disabled
module contributes to the web pages and /api/ha.
Run: python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import modules  # noqa: E402
from modules import Module, Response  # noqa: E402


def new_stack():
    stack = tempfile.mkdtemp()
    os.makedirs(os.path.join(stack, "state"))
    return stack


class Modules(unittest.TestCase):
    def setUp(self):
        self.stack = new_stack()
        self.saved = modules._ALL
        modules.all_modules()            # the real ones, then the test's own added below

    def tearDown(self):
        modules._ALL = self.saved

    def add(self, m):
        modules._ALL = modules.all_modules() + [m]
        return m

    def test_backup_is_found(self):
        self.assertIn("backup", [m.id for m in modules.all_modules()])

    def test_backup_settings_move_over_from_the_old_file(self):
        old = os.path.join(self.stack, "state", "backup.json")
        with open(old, "w") as f:
            json.dump({"uuid": "62A1-77F0", "history": False}, f)
        os.utime(old, (1_700_000_000, 1_700_000_000))
        d = modules.load(self.stack, "backup")
        self.assertEqual((d["enabled"], d["uuid"], d["history"], d["chosen_at"]), (True, "62A1-77F0", False, 1_700_000_000))
        with open(modules.path(self.stack, "backup")) as f:
            self.assertEqual(json.load(f)["version"], 1)
        self.assertTrue(os.path.exists(old))           # left in place: rolling back to an older release still works
        self.assertEqual(oct(os.stat(modules.path(self.stack, "backup")).st_mode & 0o777), "0o600")

    def test_fresh_box(self):
        d = modules.load(self.stack, "backup")
        self.assertEqual((d["enabled"], d["uuid"], d["history"]), (True, "", True))

    def test_version_upgrade_keeps_enabled(self):
        seen = []

        def migrate(stack, old, version):
            seen.append((old, version))
            return {**(old or {}), "b": 2}
        self.add(Module(id="demo", title="Demo", version=2, migrate=migrate, defaults={"a": 0, "b": 0}))
        os.makedirs(os.path.dirname(modules.path(self.stack, "demo")))
        with open(modules.path(self.stack, "demo"), "w") as f:
            json.dump({"version": 1, "enabled": True, "a": 1}, f)
        d = modules.load(self.stack, "demo")
        self.assertEqual(seen, [({"a": 1}, 1)])
        self.assertEqual((d["version"], d["enabled"], d["a"], d["b"]), (2, True, 1, 2))
        modules.load(self.stack, "demo")
        self.assertEqual(len(seen), 1)                  # only once

    def test_save_keeps_enabled_and_version(self):
        modules.set_enabled(self.stack, "backup", False)
        modules.save(self.stack, "backup", {"uuid": "x", "enabled": True, "version": 9})
        d = modules.load(self.stack, "backup")
        self.assertEqual((d["enabled"], d["version"], d["uuid"]), (False, 1, "x"))
        self.assertNotIn("enabled", modules.settings(self.stack, "backup"))

    def test_disabled_module_contributes_nothing(self):
        self.assertIn(("/backup", "Backups", "Backups"), modules.nav(self.stack))
        self.assertIn("Backups", modules.cards(self.stack))
        self.assertIsNotNone(modules.route(self.stack, "GET", "/backup"))
        self.assertIn("backup_configured", modules.ha_fields(self.stack, {}))
        modules.set_enabled(self.stack, "backup", False)
        self.assertEqual(modules.nav(self.stack), [])
        self.assertNotIn("Backups", modules.cards(self.stack))
        self.assertIsNone(modules.route(self.stack, "GET", "/backup"))
        self.assertNotIn("backup_configured", modules.ha_fields(self.stack, {}))
        modules.set_enabled(self.stack, "backup", True)
        self.assertIsNotNone(modules.route(self.stack, "GET", "/backup"))

    def test_ha_fields_need_the_module_prefix(self):
        self.add(Module(id="demo", title="Demo", default_enabled=True, ha_fields=lambda s, site: {"level": 1}))
        with self.assertRaises(ValueError):
            modules.ha_fields(self.stack, {})

    def test_a_failing_module_does_not_break_the_others(self):
        def boom(*a):
            raise RuntimeError("no")
        self.add(Module(id="demo", title="Demo", default_enabled=True, ha_fields=boom, card=boom))
        self.assertIn("backup_configured", modules.ha_fields(self.stack, {}))
        self.assertIn("Demo</b>: couldn't be shown", modules.cards(self.stack))
        with open(modules.path(self.stack, "backup"), "w") as f:
            f.write("{broken")
        self.assertNotIn("backup", [m.id for m in modules.enabled(self.stack)])

    def test_route_response(self):
        self.add(Module(id="demo", title="Demo", default_enabled=True,
                        routes={("POST", "/demo"): lambda stack, form: Response(location="/")}))
        self.assertEqual(modules.route(self.stack, "POST", "/demo")(self.stack, {}).location, "/")
        self.assertIsNone(modules.route(self.stack, "GET", "/demo"))

    def test_overdue_counts_from_choosing_the_stick(self):
        import backup
        from modules import backup as bm
        backup.save_settings(self.stack, {"uuid": "62A1-77F0", "history": True})
        t = backup.settings(self.stack)["chosen_at"]
        self.assertAlmostEqual(t, time.time(), delta=5)
        backup.save_settings(self.stack, {**backup.settings(self.stack), "history": False})
        self.assertEqual(backup.settings(self.stack)["chosen_at"], t)   # same stick: still counting
        self.assertTrue(bm.ha_fields(self.stack, None, t + 3 * 86400)["backup_overdue"])


if __name__ == "__main__":
    unittest.main()
