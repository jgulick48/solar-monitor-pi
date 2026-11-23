"""Tests for the boiler module (setup/boiler.py and setup/modules/boiler): readings, learned power levels and probe
offsets, burner runs, hot water, the fault rules (logged only) and the alerts, and the module's settings and pages.
Run: python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
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
        self.assertEqual((e["rule"], e["kind"], e["action"]), ("no_run", "fault", "logged; resets are off"))
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


class Output(Base):
    def test_heat_output(self):
        s = {**S, "floor_gpm": "4", "fluid": "water"}
        self.assertIsNone(boiler.heat_output(S, 8.0, True))                 # no flow entered
        self.assertEqual(boiler.heat_output(s, 15 * 5 / 9, True), 30000)     # the design's example: 4 GPM, 15 °F
        self.assertEqual(boiler.heat_output(s, 15 * 5 / 9, False), 0.0)
        self.assertEqual(boiler.heat_output({**s, "fluid": "pg40"}, 15 * 5 / 9, True), 27960)
        # while heating with the floor flow entered: BTU/h now and kBTU over the day
        self.run_for(2, s=s, **IDLE)
        self.run_for(60, s=s, **{**IDLE, "thermostat": True, "boiler_power": 180.0, "boiler_supply": 50.0,
                                 "floor_supply": 33.33, "floor_return": 25.0})
        last = self.st()["last"]
        self.assertTrue(29000 < last["output"] < 31000, last["output"])
        self.assertTrue(29 < self.st()["days"]["2026-11-23"]["heat_kbtu"] < 31)
        self.assertIn("boiler_heat_output_btuh", " ".join(boiler.vm_lines(self.stack)))

    def test_burner_minutes(self):
        st = {"runs": [{"start": T0 - 7200, "end": T0 - 5400}, {"start": T0 - 1800, "end": T0 - 1200}],
              "run": {"start": T0 - 300}}
        self.assertEqual(boiler.burner_minutes(st, T0), 15.0)


SR = {**S, "roles": {**S["roles"], "boiler_switch": "switch.boiler_plug"}, "resets": True}
CALL = {**IDLE, "thermostat": True, "boiler_switch": True}     # calling, the plug at controls only: "not running on demand"


class Resets(Base):
    ctx = {"propane": None, "season": True}

    def go(self, minutes, s=SR, ctx=None, **v):
        msgs = []
        for _ in range(minutes):
            self.t += 60
            vals = {k: (x(self.t) if callable(x) else x) for k, x in v.items()}
            msgs += boiler.step(self.stack, s, r(**vals), self.t, 0.0, self.alert, ctx or self.ctx)
        return msgs

    def last(self):
        return self.st()["last"]

    def test_allowed_then_started_then_heat_back(self):
        self.go(2, **{**IDLE, "boiler_switch": True})
        self.assertFalse(self.last()["reset_allowed"])
        self.go(16, **CALL)
        self.assertTrue(self.last()["reset_allowed"], self.last()["reset_why"])
        self.assertEqual(self.sent, [])                         # Home Assistant does the reset and its notices
        with unittest.mock.patch.object(time, "time", return_value=self.t + 5):
            self.assertEqual(boiler.start_reset(self.stack), "")
            self.assertEqual(boiler.start_reset(self.stack), "a reset is running")
        self.go(1, **CALL)
        self.assertEqual(self.last()["reset_state"], "resetting")
        self.assertFalse(self.last()["reset_allowed"])
        start = self.t
        heat = {**CALL, "boiler_power": 180.0, "boiler_supply": lambda t: 24 + (t - start) / 60 * 3,
                "floor_supply": lambda t: 25.3 + (t - start) / 60 * 0.3}
        msgs = self.go(4, **heat)
        self.assertTrue(any("heat back" in m for m in msgs), msgs)
        self.assertEqual(self.last()["reset_state"], "heat back")
        a = self.st()["resets"]["attempts"][-1]
        self.assertEqual((a["fault"], a["result"]), (boiler.RULE["no_run"][3], "heat back"))

    def test_failed_then_limits_and_lock(self):
        self.go(16, **CALL)
        with unittest.mock.patch.object(time, "time", return_value=self.t):
            boiler.start_reset(self.stack)
        msgs = self.go(22, **CALL)                              # no heat for 20 minutes after the plug is back on
        self.assertTrue(any("failed" in m for m in msgs), msgs)
        self.assertEqual(self.last()["reset_state"], "failed")
        # the fault is still there: one reset in the last 6 hours -> not reset, a critical alert (once)
        self.assertEqual(self.last()["reset_why"], "1 reset in the last 6 hours")
        self.assertEqual(self.sent.count(("boiler_fault", "critical")), 1)
        self.go(5, **CALL)
        self.assertEqual(self.sent.count(("boiler_fault", "critical")), 1)
        # 6 hours on (still faulted): a second reset may run; after it, the 24-hour limit locks
        self.go(6 * 60, **CALL)
        self.assertTrue(self.last()["reset_allowed"])
        with unittest.mock.patch.object(time, "time", return_value=self.t):
            boiler.start_reset(self.stack)
        n = self.sent.count(("boiler_fault", "critical"))
        self.go(25, **CALL)                                     # fails again: 2 in 24 hours locks at once
        self.assertTrue(self.last()["reset_locked"])
        self.assertEqual(self.sent.count(("boiler_fault", "critical")), n + 1)
        self.go(6 * 60, **CALL)
        self.assertTrue(self.last()["reset_locked"])
        self.assertIn("press Clear", self.last()["reset_why"])
        # Clear (the boiler was checked)
        boiler.note_request(self.stack, "cleared", self.t)
        self.go(1, **CALL)
        self.assertFalse(self.last()["reset_locked"])

    def test_unlocks_after_a_day_without_faults(self):
        self.go(16, **CALL)
        st = self.st()
        st["resets"]["locked"] = self.t
        boiler.save(self.stack, st)
        self.go(10, **{**IDLE, "boiler_switch": True})
        self.assertTrue(self.st()["resets"]["locked"])
        self.go(24 * 60, **{**IDLE, "boiler_switch": True})
        self.assertFalse(self.st()["resets"]["locked"])

    def test_blocked(self):
        cases = [({**SR, "roles": {**SR["roles"], "boiler_switch": ""}}, self.ctx, "no boiler plug switch chosen"),
                 (SR, {"propane": {"percent": 8.0, "reading_age": 60}, "season": True}, "propane low (8%)"),
                 (SR, {"propane": {"percent": 50.0, "reading_age": 9 * 3600}, "season": True}, "no recent propane reading"),
                 (SR, {"propane": None, "season": False}, "\"Boiler automatic resets\" switch is off")]
        for s, ctx, why in cases:
            with self.subTest(why=why):
                self.tearDown(); self.setUp()
                self.go(16, s=s, ctx=ctx, **CALL)
                self.assertFalse(self.last()["reset_allowed"])
                self.assertIn(why, self.last()["reset_why"])
                self.assertIn(("boiler_fault", "critical"), self.sent)
                self.go(1, s=s, ctx=ctx, **{**IDLE, "boiler_switch": True})
                self.assertIn(("boiler_fault", "clear"), self.sent)
        self.tearDown(); self.setUp()
        self.go(16, **{**CALL, "boiler_switch": None})
        self.assertIn("no reading from Boiler plug switch", self.last()["reset_why"])

    def test_resets_off_only_logs(self):
        self.go(16, s={**SR, "resets": False}, **CALL)
        self.assertFalse(self.last()["reset_allowed"])
        self.assertEqual(self.last()["reset_why"], "")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.st()["log"][-1]["action"], "logged; resets are off")

    def test_not_started_by_home_assistant(self):
        self.go(16, **CALL)
        self.go(9, **CALL)
        self.assertEqual(self.sent, [])
        self.go(2, **CALL)
        self.assertEqual(self.sent, [("boiler_fault", "critical")])
        self.assertIn("hasn't started one", self.st()["resets"]["alerted"])

    def test_start_refused_when_not_allowed_or_stale(self):
        self.go(2, **{**IDLE, "boiler_switch": True})
        with unittest.mock.patch.object(time, "time", return_value=self.t):
            self.assertIn("no fault", boiler.start_reset(self.stack))
        with unittest.mock.patch.object(time, "time", return_value=self.t + 600):
            self.assertEqual(boiler.start_reset(self.stack), "the boiler module has no recent state")


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

    def test_display_panel(self):
        self.assertIn("Pick the boiler's sensors", mod.display_panel(self.stack, {})["note"])
        with mock.patch.object(hastates, "apply", return_value=False):
            mod.form_save(self.stack, self.form(bo_floor_gpm="4", bo_burner_w="100"))
        self.assertIn("No boiler readings", mod.display_panel(self.stack, {})["note"])
        s = mod.settings(self.stack)
        self.run_for(2, s=s, **IDLE)
        self.run_for(10, s=s, **{**IDLE, "thermostat": True, "boiler_power": 180.0, "boiler_supply": 50.0,
                                 "floor_supply": 33.33, "floor_return": 25.0})
        with mock.patch.object(time, "time", return_value=self.t):
            p = mod.display_panel(self.stack, {"units": "us_customary"})
            metric = mod.display_panel(self.stack, {"units": "metric"})
        tiles = {t["k"]: t for t in p["tiles"]}
        self.assertEqual(p["level"], "")
        self.assertEqual(tiles["Boiler"]["v"], "Heating")
        self.assertEqual(tiles["Burner, last hour"]["v"], "15%")       # 9 of the last 60 minutes
        self.assertEqual(tiles["Floor supply / return"]["v"], "92° / 77°")
        self.assertEqual(tiles["Heat to the floor"]["v"], "30k BTU/h")
        self.assertNotIn("Propane", tiles)                   # the propane module is off
        self.assertEqual({t["k"]: t for t in metric["tiles"]}["Floor supply / return"]["v"], "33° / 25°")
        # through the framework, once the module is on
        with mock.patch.object(hastates, "apply", return_value=False):
            modules.set_enabled(self.stack, "boiler", True)
        with mock.patch.object(time, "time", return_value=self.t):
            panels = modules.display_panels(self.stack, {})
        self.assertEqual([x["id"] for x in panels], ["boiler"])
        self.assertTrue(all(set(t) == {"k", "v", "s", "tone", "bar"} for t in panels[0]["tiles"]))

    def test_form_flow(self):
        with self.assertRaises(FieldError):
            mod.form_check(self.stack, self.form(bo_floor_gpm="fast"))
        with self.assertRaises(FieldError):
            mod.form_check(self.stack, self.form(bo_fluid="mercury"))
        with mock.patch.object(hastates, "apply", return_value=False):
            mod.form_save(self.stack, self.form(bo_floor_gpm="4.5", bo_fluid="pg30"))
        self.assertEqual((mod.settings(self.stack)["floor_gpm"], mod.settings(self.stack)["fluid"]), ("4.5", "pg30"))

    def test_reset_package_and_route(self):
        with self.assertRaises(FieldError) as e:
            mod.form_check(self.stack, self.form(bo_resets="on", bo_boiler_switch=""))
        self.assertIn("Boiler plug switch", str(e.exception))
        os.makedirs(os.path.join(self.stack, "homeassistant", "packages"))
        pkg = os.path.join(self.stack, "homeassistant", "packages", "solar_boiler.yaml")
        with mock.patch.object(hastates, "apply", return_value=False), mock.patch.object(mod.subprocess, "run") as run:
            modules.set_enabled(self.stack, "boiler", True)
            self.assertIn("resets are off", open(pkg).read())
            msgs = mod.form_save(self.stack, self.form(bo_resets="on", bo_boiler_switch="switch.boiler_plug"))
            self.assertTrue(msgs)
            for t in threading.enumerate():
                if t is not threading.current_thread() and t.daemon:
                    t.join(2)
            self.assertTrue(run.called)                      # Home Assistant restarted for the new automation
        import yaml
        d = yaml.safe_load(open(pkg))
        self.assertEqual([a["id"] for a in d["automation"]], ["solar_boiler_reset", "solar_boiler_reset_plug_back_on"])
        acts = d["automation"][0]["actions"]
        self.assertEqual(acts[0]["action"], "rest_command.solar_boiler_reset_started")
        self.assertEqual([a.get("action") for a in acts if a.get("action", "").startswith("switch.")],
                         ["switch.turn_off", "switch.turn_on"])
        self.assertEqual(acts[3]["target"]["entity_id"], "switch.boiler_plug")
        auth = d["rest_command"]["solar_boiler_reset_started"]["headers"]["Authorization"]
        self.assertEqual(auth, "Bearer " + mod.token(self.stack))
        self.assertIn("input_boolean.solar_boiler_resets", mod.chosen(self.stack))
        # the route: token from the Authorization header only
        self.assertEqual(mod.reset_started(self.stack, {}).code, 403)
        self.assertEqual(mod.reset_started(self.stack, {"token": mod.token(self.stack)}).code, 403)
        r409 = mod.reset_started(self.stack, {"_bearer": mod.token(self.stack)})
        self.assertEqual((r409.code, r409.body), (409, "the boiler module has no recent state"))
        # turning the module off leaves no automation behind
        with mock.patch.object(hastates, "apply", return_value=False), mock.patch.object(mod.subprocess, "run"):
            modules.set_enabled(self.stack, "boiler", False)
        self.assertIn("resets are off", open(pkg).read())

    def test_ha_entities_in_statestream(self):
        with mock.patch.object(hastates, "apply", return_value=False):
            mod.form_save(self.stack, self.form())
        with mock.patch.object(hastates, "apply", return_value=False):
            modules.set_enabled(self.stack, "boiler", True)
        self.assertIn("climate.t6", hastates.entities(self.stack))


if __name__ == "__main__":
    unittest.main()
