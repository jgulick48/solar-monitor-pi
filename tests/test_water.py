"""Tests for the water module (setup/water.py and setup/modules/water): pump runs from the plug's power readings,
use from the meter, tank or pump estimate, days left, the alerts, and the module's settings and pages.
Run: python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
os.environ.setdefault("SOLAR_COMPOSE", "true")
os.environ["TZ"] = "UTC"
time.tzset()
import hastates  # noqa: E402
import modules  # noqa: E402
import water  # noqa: E402
from modules import FieldError  # noqa: E402
from modules import water as mod  # noqa: E402

T0 = 1795392000            # 2026-11-23 00:00 UTC
PUMP = "sensor.well_pump_power"
S = {**water.DEFAULTS, "roles": {**water.DEFAULTS["roles"], "pump_power": PUMP}, "pump_gpm": 8, "pump_amps": 10}


def r(**v):
    return {k: {"ok": x is not None, "value": x, "raw": x} for k, x in v.items()}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))
        self.sent = []
        self.t = T0
        self.power = lambda t: 2.0          # watts at time t: the pump off
        self.reads = 0

    def tearDown(self):
        modules._ALL = None
        self.tmp.cleanup()

    def alert(self, tag, title, message, priority):
        self.sent.append((tag, "clear" if message == "clear" else priority))
        return ""

    def fetch(self, eid, start, end):
        """The plug's readings every 10 s, as VictoriaMetrics would return them."""
        self.reads += 1
        t = int(start // 10 * 10) + 10
        out = []
        while t <= end:
            out.append((float(t), self.power(t)))
            t += 10
        return out

    def run_for(self, minutes, s=S, **v):
        msgs = []
        for _ in range(minutes):
            self.t += 60
            vals = {k: (x(self.t) if callable(x) else x) for k, x in v.items()}
            if s["roles"].get("pump_power"):
                vals.setdefault("pump_power", self.power(self.t))
            msgs += water.step(self.stack, s, r(**vals), self.t, self.alert, self.fetch)
        return msgs

    def schedule(self, runs, watts=700.0):
        """Runs as [(start offset s from now, length s)]."""
        base = self.t
        spans = [(base + a, base + a + n) for a, n in runs]
        self.power = lambda t: watts if any(a <= t < b for a, b in spans) else 2.0


class Readings(unittest.TestCase):
    def test_units(self):
        self.assertEqual(water.reading("pump_power", {"state": "0.7", "unit": "kW"})["value"], 700.0)
        self.assertFalse(water.reading("pump_power", {"state": "unavailable"})["ok"])
        self.assertAlmostEqual(water.reading("water_meter", {"state": "1000", "unit": "L"})["value"], 264.172)
        self.assertAlmostEqual(water.reading("water_meter", {"state": "2", "unit": "m³"})["value"], 528.344)
        self.assertFalse(water.reading("water_meter", {"state": "5", "unit": "kWh"})["ok"])
        t = water.reading("tank_level", {"state": "40", "unit": "%"}, 1500)["value"]
        self.assertEqual(t, {"percent": 40.0, "gal": 600.0})
        self.assertEqual(water.reading("tank_level", {"state": "40", "unit": "%"})["value"], {"percent": 40.0, "gal": None})
        t = water.reading("tank_level", {"state": "750", "unit": "gal"}, 1500)["value"]
        self.assertEqual(t, {"percent": 50.0, "gal": 750.0})


class Pump(Base):
    def test_runs_counted_between_minutes(self):
        """Short runs (a pressure tank) count even though the step runs once a minute."""
        self.schedule([(100, 30), (400, 20), (700, 90)])
        self.run_for(15)
        st = water.load(self.stack)
        self.assertEqual([round(x["s"]) for x in st["runs"]], [30, 20, 90])
        day = st["days"]["2026-11-23"]
        self.assertEqual((day["runs"], day["run_s"], day["max_run_s"]), (3, 140.0, 90.0))
        self.assertAlmostEqual(st["runs"][-1]["w"], 700.0)
        v = water.summary(self.stack, S, self.t)
        self.assertEqual((v["use_today"], v["use_source"]), (round(140 / 60 * 8, 1), "pump"))
        self.assertEqual(v["starts_last_hour"], 3)
        self.assertEqual(self.sent, [])

    def test_late_readings_not_lost(self):
        """VictoriaMetrics down for a few minutes: the runs in that time are found once it answers."""
        self.schedule([(100, 40)])
        real = self.fetch
        self.fetch = mock.Mock(side_effect=OSError("down"))
        msgs = self.run_for(5)
        self.assertTrue(any("not fetched" in m for m in msgs))
        self.fetch = real
        self.run_for(2)
        self.assertEqual([round(x["s"]) for x in water.load(self.stack)["runs"]], [40])

    def test_long_run_alert(self):
        self.schedule([(30, 3600)])
        self.run_for(19)
        self.assertEqual(self.sent, [])
        self.run_for(2)
        self.assertEqual(self.sent, [("water_long_run", "critical")])
        self.assertIn("without stopping", water.load(self.stack)["log"][-1]["detail"])
        self.power = lambda t: 2.0
        self.run_for(2)
        self.assertEqual(self.sent[-1], ("water_long_run", "clear"))

    def test_learns_power_then_low_power(self):
        self.schedule([(i * 600 + 60, 120) for i in range(water.LEARN_RUNS)])
        self.run_for(water.LEARN_RUNS * 10 + 1)
        self.assertEqual(water.load(self.stack)["normal_w"], 700.0)
        self.schedule([(30, 600)], watts=400.0)          # lost prime: it runs, drawing much less
        self.run_for(6)
        self.assertIn(("water_low_power", "warning"), self.sent)

    def test_overload(self):
        self.schedule([(30, 600)], watts=1300.0)         # over 10 A x 120 V
        self.run_for(5)
        self.assertIn(("water_overload", "warning"), self.sent)
        self.assertIn("nameplate (10 A x 120 V)", water.load(self.stack)["log"][-1]["detail"])

    def test_rapid_cycling(self):
        self.schedule([(30 + i * 70, 10) for i in range(8)])
        self.run_for(11)
        self.assertIn(("water_rapid", "warning"), self.sent)

    def test_night_flow_from_pump_starts(self):
        self.schedule([(h * 3600 + 900, 40) for h in range(1, 5)])    # a start in every hour from 1 to 5 am
        self.run_for(5 * 60 + 2)
        self.assertIn(("water_night_flow", "warning"), self.sent)
        self.assertIn("pump started in every hour", water.load(self.stack)["log"][-1]["detail"])

    def test_quiet_night(self):
        self.schedule([(h * 3600 + 900, 40) for h in (1, 2, 4)])      # 3 to 4 am quiet
        self.run_for(5 * 60 + 2)
        self.assertNotIn("water_night_flow", [x[0] for x in self.sent])


class MeterAndTank(Base):
    def test_meter_use_and_night_leak(self):
        s = {**water.DEFAULTS, "roles": {**water.DEFAULTS["roles"], "water_meter": "sensor.meter"}}
        total = lambda t: 1000 + (t - T0) / 60 * 0.1        # noqa: E731 - a steady 0.1 gal a minute: a running toilet
        self.run_for(6 * 60, s=s, water_meter=total)
        v = water.summary(self.stack, s, self.t)
        self.assertEqual(v["use_source"], "meter")
        self.assertAlmostEqual(v["use_today"], 36.0, delta=0.2)
        self.assertEqual(self.sent, [("water_night_flow", "warning")])
        self.assertIn("the meter counted water", water.load(self.stack)["log"][-1]["detail"])

    def test_meter_reset_ignored(self):
        s = {**water.DEFAULTS, "roles": {**water.DEFAULTS["roles"], "water_meter": "sensor.meter"}}
        self.run_for(1, s=s, water_meter=5000.0)
        self.run_for(1, s=s, water_meter=5010.0)
        self.run_for(1, s=s, water_meter=3.0)            # replaced or reset
        self.run_for(1, s=s, water_meter=5.0)
        self.assertEqual(water.load(self.stack)["days"]["2026-11-23"]["meter_gal"], 12.0)

    def test_tank_use_fill_days_left_and_alerts(self):
        s = {**water.DEFAULTS, "roles": {**water.DEFAULTS["roles"], "tank_level": "sensor.tank"}, "tank_gal": 1000}
        pct = [80.0]
        def level(t):
            return {"percent": pct[0], "gal": pct[0] * 10}
        for day in range(4):                             # 10% (100 gal) a day, in steps, with noise
            for h in range(24):
                pct[0] -= 10 / 24 + (0.3 if h % 2 else -0.3)
                self.run_for(60, s=s, tank_level=level)
            if day == 1:
                pct[0] += 30                             # a delivery
                self.run_for(1, s=s, tank_level=level)
        st = water.load(self.stack)
        self.assertEqual(len(st["tank"]["fills"]), 1)
        self.assertAlmostEqual(st["tank"]["fills"][0]["added_gal"], 300, delta=15)
        v = water.summary(self.stack, s, self.t)
        self.assertEqual(v["use_source"], "tank")
        self.assertAlmostEqual(v["usual"], 100, delta=10)
        self.assertEqual(v["usual_days"], 3)              # the first (partial) day left out
        self.assertAlmostEqual(v["days_left"], pct[0] * 10 / v["usual"], delta=0.2)
        self.assertEqual(self.sent, [])                    # 80% -> 60% -> 90% (fill) -> 70%
        pct[0] = 25.0
        self.run_for(11, s=s, tank_level=level)
        self.assertEqual(self.sent, [("water_tank_low", "warning")])
        pct[0] = 10.0
        self.run_for(11, s=s, tank_level=level)
        self.assertIn(("water_tank_critical", "critical"), self.sent)

    def test_offline(self):
        self.run_for(29, pump_power=None)
        self.assertEqual(self.sent, [])
        self.run_for(2, pump_power=None)
        self.assertEqual(self.sent, [("water_offline", "warning")])


class ModuleTest(Base):
    def form(self, **kw):
        f = {"wa_pump_power": PUMP, "wa_alerts": "on", "wa_max_run": "20", "wa_amps": "10", "wa_gpm": "8",
             "wa_tank_gal": "", "wa_tank_warn": "30", "wa_tank_critical": "15"}
        f.update(kw)
        return f

    def test_form(self):
        mod.form_check(self.stack, self.form())
        for bad in ({"wa_pump_power": "switch.pump"}, {"wa_max_run": "0"}, {"wa_amps": "lots"},
                    {"wa_tank_warn": "10", "wa_tank_critical": "20"}):
            with self.assertRaises(FieldError):
                mod.form_check(self.stack, self.form(**bad))
        with mock.patch.object(hastates, "apply", return_value=True) as ap:
            self.assertTrue(mod.form_save(self.stack, self.form()))
            self.assertEqual(mod.form_save(self.stack, self.form()), [])      # same entities: no restart
            self.assertEqual(ap.call_count, 1)
        s = mod.settings(self.stack)
        self.assertEqual((s["pump_gpm"], s["pump_amps"], s["max_run_min"]), (8.0, 10.0, 20))
        self.assertEqual(mod.chosen(self.stack), [PUMP])
        html = mod.form_section(self.stack)
        self.assertIn("Pump plug power (Recommended)", html)
        self.assertIn("Pump flow (GPM)", html)

    def test_metric_form(self):
        with open(os.path.join(self.stack, "state", "site.json"), "w") as f:
            json.dump({"units": "metric"}, f)
        with mock.patch.object(hastates, "apply", return_value=False):
            mod.form_save(self.stack, self.form(wa_gpm="30", wa_tank_gal="5000"))
        s = mod.settings(self.stack)
        self.assertAlmostEqual(s["pump_gpm"], 7.925, places=2)
        self.assertAlmostEqual(s["tank_gal"], 1320.9, places=0)
        self.assertIn('value="30"', mod.form_section(self.stack))

    def test_known_entities(self):
        reg = {"data": {"entities": [
            {"entity_id": "sensor.well_pump_power", "original_name": "Pump power", "original_device_class": "power"},
            {"entity_id": "sensor.water_meter", "original_name": "Meter", "original_device_class": "water"},
            {"entity_id": "sensor.fs", "original_name": "Floor", "original_device_class": "temperature"}]}}
        os.makedirs(os.path.join(self.stack, "homeassistant", ".storage"))
        with open(os.path.join(self.stack, "homeassistant", ".storage", "core.entity_registry"), "w") as f:
            json.dump(reg, f)
        html = mod.form_section(self.stack)
        power = html.split("<datalist id='wa_power'>")[1].split("</datalist>")[0]
        self.assertIn("sensor.well_pump_power", power)
        self.assertNotIn("sensor.water_meter", power)
        self.assertIn("sensor.water_meter", html.split("<datalist id='wa_volume'>")[1].split("</datalist>")[0])

    def test_page_card_fields_panel(self):
        self.assertIn("pick the pump plug", mod.card(self.stack))
        self.assertIn("Pick the water entities", mod.display_panel(self.stack, {})["note"])
        with mock.patch.object(hastates, "apply", return_value=False):
            mod.form_save(self.stack, self.form())
        s = mod.settings(self.stack)
        self.schedule([(100, 30), (400, 600)])
        self.run_for(8, s=s)
        with mock.patch.object(time, "time", return_value=self.t):
            card = mod.card(self.stack)
            page = mod.page(self.stack, {}).body
            f = mod.ha_fields(self.stack, {})
            p = mod.display_panel(self.stack, {"units": "us_customary"})
        self.assertIn("pump running for", card)
        self.assertIn("Last runs", page)
        self.assertIn("estimated from the pump", page)
        self.assertEqual((f["water_pump_running"], f["water_pump_starts_today"], f["water_alert"]), (True, 2, "none"))
        tiles = {t["k"]: t for t in p["tiles"]}
        self.assertEqual(tiles["Pump"]["v"], "Running")
        self.assertEqual(tiles["Pump today"]["s"], "2 runs")
        self.assertIn("estimated", tiles["Used today"]["s"])
        with mock.patch.object(hastates, "apply", return_value=False):
            modules.set_enabled(self.stack, "water", True)
        with mock.patch.object(time, "time", return_value=self.t):
            panels = modules.display_panels(self.stack, {})
        self.assertEqual([x["id"] for x in panels], ["water"])

    def test_vm_lines(self):
        self.schedule([(100, 30)])
        self.run_for(4)
        lines = water.vm_lines(self.stack, S, self.t)
        self.assertIn("water_pump_starts_today 1", lines)
        self.assertIn('water_rule_active{rule="long_run"} 0', lines)


if __name__ == "__main__":
    unittest.main()
