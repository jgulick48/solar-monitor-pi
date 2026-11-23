"""Tests for the boiler module (setup/boiler.py and setup/modules/boiler): readings, learned power levels and probe
offsets, burner runs, hot water, the fault rules (logged only) and the alerts, and the module's settings and pages.
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
import boiler  # noqa: E402
import hastates  # noqa: E402
import modules  # noqa: E402
from modules import FieldError  # noqa: E402
from modules import boiler as mod  # noqa: E402

T0 = 1795392000            # 2026-11-23 00:00 UTC
ROLES = {"floor_supply": "sensor.fs", "floor_return": "sensor.fr", "boiler_supply": "sensor.bs",
         "boiler_return": "sensor.br", "hot_water": "sensor.hw", "thermostat": "climate.t6",
         "boiler_power": "sensor.boiler_plug_power"}
S = {**boiler.DEFAULTS, "roles": {**boiler.DEFAULTS["roles"], **ROLES}, "burner_w": "100"}


def r(**v):
    """Readings: temperatures and power as numbers, calling as a bool; None = unavailable."""
    out = {}
    for k, x in v.items():
        out[k] = {"ok": x is not None, "value": x, "raw": x}
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))
        self.sent = []
        self.t = T0

    def tearDown(self):
        modules._ALL = None
        self.tmp.cleanup()

    def alert(self, tag, title, message, priority):
        self.sent.append((tag, "clear" if message == "clear" else priority))
        return ""

    def run_for(self, minutes, s=S, outdoor=0.0, **v):
        msgs = []
        for _ in range(minutes):
            self.t += 60
            vals = {k: (x(self.t) if callable(x) else x) for k, x in v.items()}
            msgs += boiler.step(self.stack, s, r(**vals), self.t, outdoor, self.alert)
        return msgs

    def st(self):
        return boiler.load(self.stack)


IDLE = dict(floor_supply=25.3, floor_return=25.0, boiler_supply=24.0, boiler_return=24.1, hot_water=18.0,
            thermostat=False, boiler_power=8.0)


class Readings(unittest.TestCase):
    def test_reading(self):
        self.assertAlmostEqual(boiler.reading("floor_supply", {"state": "104", "unit": "°F"})["value"], 40.0)
        self.assertEqual(boiler.reading("floor_supply", {"state": "41.5", "unit": "°C"})["value"], 41.5)
        self.assertFalse(boiler.reading("floor_supply", {"state": "unavailable"})["ok"])
        self.assertFalse(boiler.reading("floor_supply", None)["ok"])
        self.assertEqual(boiler.reading("boiler_power", {"state": "0.18", "unit": "kW"})["value"], 180.0)
        self.assertTrue(boiler.reading("thermostat", {"state": "heat", "attributes": {"hvac_action": "heating"}})["value"])
        self.assertFalse(boiler.reading("thermostat", {"state": "heat", "attributes": {"hvac_action": "idle"}})["value"])
        self.assertIsNone(boiler.reading("thermostat", {"state": "heat", "attributes": {}})["value"])
        self.assertTrue(boiler.reading("heat_call", {"state": "on"})["value"])

    def test_power_levels(self):
        self.assertIsNone(boiler.power_levels({"5": 100}))
        bins = {"5": 900, "10": 100, "70": 300, "75": 100, "180": 200, "185": 100}
        lv = boiler.power_levels(bins)
        self.assertLess(lv["idle"], 15)
        self.assertTrue(60 < lv["pump"] < 85)
        self.assertTrue(175 < lv["burner"] < 195)
        self.assertTrue(lv["pump"] < lv["burner_on"] < lv["burner"])
        # one flat level is not three levels
        self.assertIsNone(boiler.power_levels({"5": 2000}))


class Step(Base):
    def test_setup_until_required_chosen(self):
        s = {**S, "roles": {**S["roles"], "boiler_supply": ""}}
        self.run_for(1, s=s, **IDLE)
        self.assertEqual(self.st()["last"]["state"], "setup")
        self.assertEqual(self.st()["last"]["missing"], ["boiler_supply"])

    def test_offsets_learned_when_quiet(self):
        self.run_for(25, **IDLE)
        self.assertEqual(self.st()["offsets"], {})           # not quiet for 30 minutes yet
        self.run_for(20, **IDLE)
        o = self.st()["offsets"]
        self.assertAlmostEqual(o["floor"]["c"], 0.3, places=2)
        self.assertAlmostEqual(o["boiler"]["c"], -0.1, places=2)
        # corrected: the return now reads as the supply
        self.assertAlmostEqual(self.st()["last"]["temps"]["floor_return"], 25.3, places=2)
        self.assertAlmostEqual(self.st()["last"]["floor_diff"], 0.0, places=2)
        # a call for heat stops the learning
        n = o["floor"]["n"]
        self.run_for(5, **{**IDLE, "thermostat": True})
        self.assertEqual(self.st()["offsets"]["floor"]["n"], n)

    def test_heating_run(self):
        self.run_for(2, **IDLE)
        start = self.t
        bs = lambda t: min(60.0, 24 + (t - start) / 60 * 3)           # noqa: E731
        fs = lambda t: min(45.0, 25 + (t - start) / 60)               # noqa: E731
        msgs = self.run_for(20, **{**IDLE, "thermostat": True, "boiler_power": 180.0, "boiler_supply": bs,
                                   "floor_supply": fs, "floor_return": 25.0})
        self.assertEqual(self.st()["last"]["state"], "heating")
        self.assertEqual(self.st()["run"]["supply_start"], 27.0)
        self.assertEqual(msgs, [])                                   # heat made: no fault
        self.assertIn("0", self.st()["curve"])                      # outdoor 0 °C: the 0 to 5 °C bin
        msgs = self.run_for(1, **{**IDLE, "thermostat": True, "boiler_power": 70.0, "boiler_supply": 60.0})
        self.assertIn("burner run: heating, 20.0 min", msgs)
        run = self.st()["runs"][-1]
        self.assertEqual(run["class"], "heating")
        day = self.st()["days"]["2026-11-23"]
        self.assertEqual(day["runs"], 1)
        self.assertAlmostEqual(day["heat_min"], 20, places=0)

    def test_hot_water_draw(self):
        self.run_for(5, **IDLE)
        msgs = self.run_for(4, **{**IDLE, "boiler_power": 180.0, "hot_water": 45.0})
        self.assertEqual(self.st()["last"]["state"], "hot water")
        msgs += self.run_for(1, **IDLE)
        self.assertIn("burner run: hot water, 4.0 min", msgs)
        self.assertEqual(self.st()["days"]["2026-11-23"]["dhw_min"], 4)
        # a hot-water run is never a fault or an unexplained burner, and heat calls wait 10 minutes after it
        self.assertEqual([x["rule"] for x in self.st()["log"]], [])
        self.assertGreater(self.st()["dhw_last"], 0)

    def test_no_run_fault_is_logged_not_sent(self):
        self.run_for(2, **IDLE)
        msgs = self.run_for(14, **{**IDLE, "thermostat": True})
        self.assertEqual(msgs, [])
        msgs = self.run_for(2, **{**IDLE, "thermostat": True})
        self.assertTrue(any("Not running on demand" in m for m in msgs), msgs)
        e = self.st()["log"][-1]
        self.assertEqual((e["rule"], e["kind"], e["action"]), ("no_run", "fault", "logged; resets are not on yet"))
        self.assertEqual(self.sent, [])
        self.assertEqual(mod.engine.summary(self.stack, S)["fault"], boiler.RULE["no_run"][3])
        self.run_for(1, **IDLE)
        self.assertIn("cleared", self.st()["log"][-1])
        self.assertEqual(self.st()["days"]["2026-11-23"]["faults"], 1)

    def test_no_heat_and_stopped(self):
        self.run_for(2, **IDLE)
        msgs = self.run_for(12, **{**IDLE, "thermostat": True, "boiler_power": 180.0, "boiler_supply": 26.0})
        self.assertTrue(any("Running but no heat" in m for m in msgs), msgs)
        # the burner stops while the call goes on, and the supply has fallen to the return's temperature
        msgs = self.run_for(11, **{**IDLE, "thermostat": True, "boiler_power": 8.0, "boiler_supply": 25.5})
        self.assertTrue(any("Stopped during a call" in m for m in msgs), msgs)
        self.assertFalse(any(x["rule"] == "no_run" for x in self.st()["log"]))

    def test_alerts_sent_and_cleared(self):
        self.run_for(6, **{**IDLE, "floor_return": 8.0, "floor_supply": 8.5})
        self.assertIn(("boiler_freeze", "critical"), self.sent)
        self.run_for(1, **IDLE)
        self.assertIn(("boiler_freeze", "clear"), self.sent)
        self.sent.clear()
        self.run_for(6, **{**IDLE, "floor_supply": 55.0})
        self.assertIn(("boiler_floor_hot", "warning"), self.sent)
        # offline after 30 minutes; alerts off: logged, not sent
        self.sent.clear()
        self.run_for(31, s={**S, "alerts": False}, **{**IDLE, "thermostat": None})
        self.assertEqual(self.sent, [])
        self.assertEqual(self.st()["log"][-1]["rule"], "offline")
        self.assertIn("Thermostat", self.st()["log"][-1]["detail"])

    def test_required_probe_offline(self):
        self.run_for(1, **{**IDLE, "boiler_supply": None})
        self.assertEqual(self.st()["last"]["state"], "offline")

    def test_vm_lines(self):
        self.run_for(2, **IDLE)
        lines = boiler.vm_lines(self.stack)
        self.assertIn("boiler_state 0", lines)
        self.assertIn("boiler_burner_on 0", lines)
        self.assertIn('boiler_temperature_c{role="floor_supply"} 25.3', lines)
        self.assertIn('boiler_rule_active{rule="no_run",kind="fault"} 0', lines)


class ModuleTest(Base):
    def form(self, **kw):
        f = {f"bo_{k}": v for k, v in ROLES.items()}
        f.update(bo_alerts="on", bo_floor_max="122", bo_freeze="50", bo_burner_w="")
        f.update(kw)
        return f

    def test_form(self):
        mod.form_check(self.stack, self.form())
        with self.assertRaises(FieldError):
            mod.form_check(self.stack, self.form(bo_thermostat="sensor.not_a_climate"))
        with self.assertRaises(FieldError):
            mod.form_check(self.stack, self.form(bo_floor_max="200"))
        with self.assertRaises(FieldError):
            mod.form_check(self.stack, self.form(bo_burner_w="lots"))
        with mock.patch.object(hastates, "apply", return_value=True) as ap:
            msgs = mod.form_save(self.stack, self.form())
            self.assertEqual(ap.call_count, 1)
            self.assertTrue(msgs)
            self.assertEqual(mod.form_save(self.stack, self.form()), [])     # same entities: no restart
        s = mod.settings(self.stack)
        self.assertAlmostEqual(s["floor_max_c"], 50.0)
        self.assertAlmostEqual(s["freeze_c"], 10.0)
        self.assertEqual(mod.chosen(self.stack), sorted(ROLES.values()))
        html = mod.form_section(self.stack)
        self.assertIn("Floor supply (<b>Required</b>)", html)
        self.assertIn("Thermostat (Recommended)", html)
        self.assertIn("Gives: tells a hot-water draw", html)
        self.assertNotIn("Still needed", html)

    def test_form_lists_missing_and_known(self):
        reg = {"data": {"entities": [
            {"entity_id": "sensor.boiler_probes_floor_supply", "original_name": "Floor supply", "original_device_class": "temperature"},
            {"entity_id": "sensor.plug_power", "original_name": "Plug power", "original_device_class": "power"},
            {"entity_id": "climate.t6", "original_name": "T6"}]}}
        os.makedirs(os.path.join(self.stack, "homeassistant", ".storage"))
        with open(os.path.join(self.stack, "homeassistant", ".storage", "core.entity_registry"), "w") as f:
            json.dump(reg, f)
        html = mod.form_section(self.stack)
        self.assertIn("Still needed:</b> Floor supply, Floor return, Boiler supply", html)
        temps = html.split("<datalist id='bo_temperature'>")[1].split("</datalist>")[0]
        self.assertIn("sensor.boiler_probes_floor_supply", temps)
        self.assertNotIn("sensor.plug_power", temps)
        self.assertIn("climate.t6", html.split("<datalist id='bo_climate'>")[1].split("</datalist>")[0])

    def test_page_card_fields(self):
        with mock.patch.object(hastates, "apply", return_value=False):
            mod.form_save(self.stack, self.form())
        self.assertIn("no reading yet", mod.card(self.stack))
        self.run_for(2, **IDLE)
        with mock.patch.object(time, "time", return_value=self.t):
            card = mod.card(self.stack)
            self.assertIn("<b>idle</b>", card)
            self.assertIn("°F", card)
            page = mod.page(self.stack, {}).body
            self.assertIn("Monitoring only", page)
            f = mod.ha_fields(self.stack, {})
        self.assertEqual(f["boiler_state"], "idle")
        self.assertEqual(f["boiler_fault"], "")
        self.assertIn("Fault rules", page)

    def test_ha_entities_in_statestream(self):
        with mock.patch.object(hastates, "apply", return_value=False):
            mod.form_save(self.stack, self.form())
        with mock.patch.object(hastates, "apply", return_value=False):
            modules.set_enabled(self.stack, "boiler", True)
        self.assertIn("climate.t6", hastates.entities(self.stack))


if __name__ == "__main__":
    unittest.main()
