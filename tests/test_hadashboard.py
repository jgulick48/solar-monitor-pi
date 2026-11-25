"""Tests for the Home dashboard written into Home Assistant (setup/hadashboard.py): the sections follow the enabled
modules, cameras come from Home Assistant's registry (one per camera, the wall display's first), and the file is
valid YAML that Home Assistant can load. Run: python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import hadashboard  # noqa: E402
import modules  # noqa: E402
import protect  # noqa: E402
import yaml  # noqa: E402


def entity(eid, dev, disabled=None, platform="unifiprotect"):
    return {"entity_id": eid, "platform": platform, "device_id": dev, "disabled_by": disabled, "hidden_by": None,
            "name": None, "original_name": "High resolution channel", "has_entity_name": True}


class HomeDashboard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state", "modules"))
        os.makedirs(os.path.join(self.stack, "homeassistant", ".storage"))

    def tearDown(self):
        modules._ALL = None
        self.tmp.cleanup()

    def registry(self, ents, devs):
        st = os.path.join(self.stack, "homeassistant", ".storage")
        with open(os.path.join(st, "core.entity_registry"), "w") as f:
            json.dump({"data": {"entities": ents}}, f)
        # as Home Assistant 2026.11 keeps them: the console a device, its cameras child devices (seen on the Pi 5,
        # where reading only "devices" named every camera "High resolution channel")
        with open(os.path.join(st, "core.device_registry"), "w") as f:
            json.dump({"data": {"devices": [{"id": "nvr", "name": "Console"}], "child_devices": devs,
                                "deleted_devices": []}}, f)

    def sections(self):
        self.assertTrue(hadashboard.write(self.stack))
        with open(os.path.join(self.stack, "homeassistant", "dashboards", "home.yaml")) as f:
            text = f.read()
        self.assertTrue(text.startswith("# The Home dashboard"))
        doc = yaml.safe_load(text)
        view = doc["views"][0]
        self.assertEqual((view["type"], view["path"]), ("sections", "home"))
        return {s["cards"][0]["heading"]: s["cards"] for s in view["sections"]}

    def test_core_only(self):
        s = self.sections()
        self.assertEqual(list(s), ["Now", "Today"])
        self.assertIn("sensor.site_battery_level", [c.get("entity") for c in s["Now"]])
        self.assertFalse(hadashboard.write(self.stack))                    # unchanged: not rewritten

    def test_modules_and_cameras(self):
        for m in ("water", "boiler"):
            modules.save(self.stack, m, {})
            d = modules.load(self.stack, m)
            d["enabled"] = True
            with open(os.path.join(self.stack, "state", "modules", f"{m}.json"), "w") as f:
                json.dump(d, f)
        self.registry([entity("camera.gate_high_resolution_channel", "d1"),
                       entity("camera.gate_medium_resolution_channel", "d1"),
                       entity("camera.gate_low_resolution_channel", "d1", disabled="integration"),
                       entity("camera.driveway_high_resolution_channel", "d2"),
                       entity("camera.office", "d3", platform="generic")],
                      [{"id": "d1", "name": "Gate"}, {"id": "d2", "name": "Driveway", "name_by_user": "Front drive"},
                       {"id": "d3", "name": "Office"}])
        protect.save(self.stack, {**protect.DEFAULTS, "cameras": [{"id": "x", "name": "Gate"}]})
        s = self.sections()
        self.assertEqual(list(s), ["Now", "Cameras", "Water", "Heating", "Today"])
        with open(os.path.join(self.stack, "state", "site.json"), "w") as f:
            json.dump({"generator": "manual"}, f)                         # a site with a generator
        self.assertIn("Generator", [s["cards"][0]["heading"] for s in hadashboard.build(self.stack)["views"][0]["sections"]])
        cams = [(c["entity"], c["name"]) for c in s["Cameras"][1:]]
        self.assertEqual(cams, [("camera.gate_high_resolution_channel", "Gate"),          # the wall display's first
                                ("camera.driveway_high_resolution_channel", "Front drive")])
        fault = [c for c in s["Heating"] if c.get("entity") == "sensor.site_boiler_fault"][0]
        self.assertIn({"condition": "state", "entity": "sensor.site_boiler_fault", "state_not": "none"}, fault["visibility"])

    def test_registered_in_the_package(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "stack", "homeassistant-seed",
                               "packages", "solar_monitor.yaml")) as f:
            pkg = yaml.safe_load(f)
        dash = pkg["lovelace"]["dashboards"]["solar-home"]
        self.assertEqual((dash["mode"], dash["filename"]), ("yaml", hadashboard.REL))
        # every sensor the page shows is one the package defines
        ids = set()
        for kind in ("sensor", "binary_sensor"):
            for r in pkg.get("rest", []):
                ids |= {f"{kind}.{x['name'].lower().replace(' ', '_')}" for x in r.get(kind, [])}
        for kind in ("sensor", "binary_sensor"):
            for t in pkg.get("template", []):
                ids |= {f"{kind}.{x['name'].lower().replace(' ', '_')}" for x in t.get(kind, [])}
        for m in ("propane", "water", "boiler", "generator"):
            d = modules.load(self.stack, m)
            d["enabled"] = True
            with open(os.path.join(self.stack, "state", "modules", f"{m}.json"), "w") as f:
                json.dump(d, f)
        used = {c["entity"] for s in hadashboard.build(self.stack)["views"][0]["sections"] for c in s["cards"]
                if c.get("entity", "").startswith(("sensor.site_", "binary_sensor.site_"))}
        self.assertTrue(used)
        self.assertEqual(used - ids, set())


if __name__ == "__main__":
    unittest.main()
