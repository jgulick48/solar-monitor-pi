"""Tests for the Home Assistant side: /api/ha (display.ha_status), the shipped package and
dashboard, and how solar_setup keeps them current. Run: python3 -m unittest discover -s tests -v"""
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "setup"))
import display  # noqa: E402
import solar_setup  # noqa: E402
from test_display import SITE, FakeVM  # noqa: E402

SEED = os.path.join(HERE, "..", "stack", "homeassistant-seed")


def read(path):
    with open(path) as f:
        return f.read()


def load_yaml(path):
    import yaml

    class L(yaml.SafeLoader):
        pass
    L.add_multi_constructor("!", lambda loader, tag, node: None)  # !include etc.
    with open(path) as f:
        return yaml.load(f, Loader=L)


class HomeAssistant(unittest.TestCase):
    def setUp(self):
        self.vm = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeVM)
        threading.Thread(target=self.vm.serve_forever, daemon=True).start()
        display.VM = f"http://127.0.0.1:{self.vm.server_port}"
        display._cache.clear()
        self.stack = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.stack, "state"))
        with open(os.path.join(self.stack, "state", "inverters.json"), "w") as f:
            json.dump({"a": {}, "b": {}}, f)

    def tearDown(self):
        self.vm.shutdown()
        self.vm.server_close()
        FakeVM.values.update({"max(lxp_fault_code)": 0, "max(lxp_soc)": 62, "sum(lxp_bridge_connected)": 2})

    def test_status_normal(self):
        s = display.ha_status(self.stack, SITE)
        self.assertEqual(s["battery_level"], 62)
        self.assertEqual(s["solar_power"], 3400)
        self.assertEqual(s["battery_stored"], 19.0)        # 62% of 30.72 kWh
        self.assertEqual(s["battery_runtime"], 14.2)       # (62-25)% of 30.72 kWh at 800 W
        self.assertEqual((s["dongles_connected"], s["dongles_expected"]), (2, 2))
        self.assertEqual(s["alerts"], [])
        for k in ("data_stale", "dongle_offline", "inverter_fault", "battery_low"):
            self.assertIs(s[k], False)
        self.assertIn(s["advice"], ("info", "ok", "warn"))  # manual generator: always some advice
        self.assertEqual(s["generator_needed"], s["advice"] == "warn")
        json.dumps(s)

    def test_status_alerts(self):
        FakeVM.values.update({"max(lxp_soc)": 20, "max(lxp_fault_code)": 7, "sum(lxp_bridge_connected)": 1})
        s = display.ha_status(self.stack, SITE)
        self.assertTrue(s["battery_low"])
        self.assertTrue(s["inverter_fault"])
        self.assertEqual(s["fault_code"], 7)
        self.assertTrue(s["dongle_offline"])
        self.assertEqual(len(s["alerts"]), 3)
        self.assertEqual(s["battery_runtime"], 0)

    def test_no_generator(self):
        s = display.ha_status(self.stack, dict(SITE, generator="none"))
        self.assertEqual((s["advice"], s["generator_needed"]), ("none", False))

    def test_package_matches_api(self):
        """Every value_template key and attribute the package reads exists in /api/ha."""
        import re
        keys = set(display.ha_status(self.stack, SITE))
        pkg = load_yaml(os.path.join(SEED, "packages", "solar_monitor.yaml"))
        rest = pkg["rest"][0]
        for kind in ("sensor", "binary_sensor"):
            for e in rest[kind]:
                for k in re.findall(r"value_json\.(\w+)", e["value_template"]):
                    self.assertIn(k, keys, e["name"])
                for k in e.get("json_attributes", []):
                    self.assertIn(k, keys, e["name"])
        ids = [a["id"] for a in pkg["automation"]]
        self.assertEqual(len(ids), len(set(ids)))
        # the dashboard only uses entities the package creates
        entities = {"sensor." + e["unique_id"] for e in rest["sensor"]}
        entities |= {"binary_sensor." + e["unique_id"] for e in rest["binary_sensor"]}
        entities |= {"automation." + re.sub(r"[^a-z0-9]+", "_", a["alias"].lower()).strip("_") for a in pkg["automation"]}
        dash = read(os.path.join(SEED, "dashboards", "solar.yaml"))
        for e in set(re.findall(r"\b(?:sensor|binary_sensor|automation)\.[a-z0-9_]+", dash)):
            self.assertIn(e, entities)
        self.assertEqual(pkg["lovelace"]["dashboards"]["solar-site"]["filename"], "dashboards/solar.yaml")

    def test_startup_check_is_top_level(self):
        """A condition inside if/then only ends that block: the startup re-check must be a top-level step
        (in v0.2.1 it wasn't, and every alert fired 5 minutes after each Home Assistant start)."""
        pkg = load_yaml(os.path.join(SEED, "packages", "solar_monitor.yaml"))
        checked = 0
        for a in pkg["automation"]:
            if not any(t.get("trigger") == "homeassistant" for t in a["triggers"]):
                continue
            self.assertFalse(any("if" in step for step in a["actions"]), a["id"])
            conds = [step for step in a["actions"] if step.get("condition") == "template"]
            self.assertTrue(conds and "trigger.id != 'start'" in conds[0]["value_template"], a["id"])
            idx = a["actions"].index(conds[0])
            self.assertTrue(any("action" in step for step in a["actions"][idx + 1:]), a["id"])
            checked += 1
        self.assertGreaterEqual(checked, 5)

    def test_sync_and_energy(self):
        stack = tempfile.mkdtemp()
        import shutil
        shutil.copytree(SEED, os.path.join(stack, "homeassistant-seed"))
        self.assertTrue(solar_setup.sync_homeassistant(stack))
        self.assertFalse(solar_setup.sync_homeassistant(stack))     # unchanged: no restart
        pkg = os.path.join(stack, "homeassistant", "packages", "solar_monitor.yaml")
        with open(pkg, "a") as f:
            f.write("# local edit\n")
        self.assertTrue(solar_setup.sync_homeassistant(stack))      # image-owned: put back
        self.assertNotIn("local edit", read(pkg))
        self.assertTrue(solar_setup.seed_energy(stack))
        self.assertFalse(solar_setup.seed_energy(stack))            # only once
        e = json.loads(read(os.path.join(stack, "homeassistant", ".storage", "energy")))
        self.assertEqual(e["data"]["energy_sources"][0]["stat_energy_from"], "sensor.site_solar_today")


if __name__ == "__main__":
    unittest.main()
