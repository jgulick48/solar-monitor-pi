"""Tests for propane (setup/propane.py and setup/modules/propane): picking the sensor, the day's use, fill-ups,
alerts, days to empty, and the module's settings, page and Home Assistant fields.
Run: python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
os.environ.setdefault("SOLAR_COMPOSE", "true")
os.environ["TZ"] = "UTC"
time.tzset()
import modules  # noqa: E402
import propane  # noqa: E402
from modules import FieldError  # noqa: E402
from modules import propane as mod  # noqa: E402

DAY0 = 1795392000            # 2026-11-23 00:00 UTC
S = {**propane.DEFAULTS}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))
        self.alerts = []

    def tearDown(self):
        modules._ALL = None
        self.tmp.cleanup()

    def alert(self, tag, title, message, priority):
        self.alerts.append((tag, message if message == "clear" else priority))

    def feed(self, start, minutes, pct_at, s=S, hdd_of=None):
        """A reading every minute; pct_at(minute) gives the level."""
        msgs = []
        for m in range(minutes):
            msgs += propane.add_reading(self.stack, s, pct_at(m), start + 60 * m, {}, self.alert, hdd_of)
        return msgs


class Engine(Base):
    def test_pick_and_percent(self):
        found = [{"address": "AA:00:00:00:00:01", "sensorReadQuality": 1, "rssi": -60, "tankLevelPercent": {"500g_h": 40.0}},
                 {"address": "AA:00:00:00:00:02", "sensorReadQuality": 3, "rssi": -80, "levelPercent": 72.5, "gallons": 362.5}]
        self.assertEqual(propane.pick(found)["address"], "AA:00:00:00:00:02")              # best quality first
        self.assertEqual(propane.pick(found, "aa:00:00:00:00:01")["address"], "AA:00:00:00:00:01")
        self.assertIsNone(propane.pick(found, "AA:00:00:00:00:09"))
        self.assertEqual(propane.percent_of(found[0], "500g_h"), 40.0)                      # from the per-type map
        self.assertEqual(propane.percent_of(found[1], "500g_h"), 72.5)
        self.assertEqual(propane.gallons(72.5, S), 362.5)

    def test_day_use_and_fill(self):
        # day 1: 60% falling 0.25 points an hour; the next day starts at about 54%
        self.feed(DAY0, 24 * 60, lambda m: 60 - 0.25 * m / 60)
        self.feed(DAY0 + 86400, 60, lambda m: 54 - 0.25 * m / 60)
        st = propane.load(self.stack)
        self.assertEqual(len(st["days"]), 1)
        self.assertAlmostEqual(st["days"][0]["used"], 30.0, delta=1.0)           # 6 points of 500 gal
        # a delivery: 54% -> 80% over 15 minutes, then steady; logged once it has settled for 20 minutes
        t = DAY0 + 86400 + 3600
        msgs = self.feed(t, 15, lambda m: 54 + 26 * (m + 1) / 15)
        self.assertEqual(propane.load(self.stack)["fills"], [])
        msgs += self.feed(t + 15 * 60, 60, lambda m: 80)
        fills = propane.load(self.stack)["fills"]
        self.assertEqual(len(fills), 1, msgs)
        self.assertAlmostEqual(fills[0]["added"], 130, delta=8)
        self.assertIn(("propane_fill", "info"), self.alerts)
        # that day's use counts the fill: start + filled - end
        v = propane.summary(self.stack, S, now=t + 75 * 60)
        self.assertLess(v["used_today"], 3)
        self.assertGreater(v["gallons"], 395)

    def test_low_alerts_with_hysteresis(self):
        self.feed(DAY0, 30, lambda m: 21)
        self.feed(DAY0 + 1800, 30, lambda m: 19)
        self.assertEqual(self.alerts, [("propane_low", "warning")])
        self.feed(DAY0 + 3600, 30, lambda m: 20.5)                 # back over 20 but within 2 points: still low
        self.assertEqual(len(self.alerts), 1)
        self.feed(DAY0 + 5400, 30, lambda m: 9)
        self.assertEqual(self.alerts[-1], ("propane_low", "critical"))
        self.feed(DAY0 + 7200, 60, lambda m: 70)                   # a fill: cleared
        self.assertIn(("propane_low", "clear"), self.alerts)
        self.assertIsNone(propane.load(self.stack)["alerts"]["low"])

    def test_silent(self):
        self.feed(DAY0, 2, lambda m: 50)
        self.assertEqual(propane.check_silent(self.stack, DAY0 + 3600, self.alert), [])
        self.assertEqual(propane.check_silent(self.stack, DAY0 + 7 * 3600, self.alert), ["no reading for 6 hours"])
        self.assertEqual(propane.check_silent(self.stack, DAY0 + 8 * 3600, self.alert), [])      # once
        self.feed(DAY0 + 9 * 3600, 1, lambda m: 50)
        self.assertEqual(self.alerts, [("propane_silent", "warning"), ("propane_silent", "clear")])

    def test_days_left(self):
        st = {"days": [{"date": f"d{i}", "used": 2 + 0.2 * h, "hdd": h} for i, h in enumerate([10, 20, 30, 40] * 4)]}
        a, b = propane.fit(st["days"])
        self.assertAlmostEqual(a, 2.0, places=6)
        self.assertAlmostEqual(b, 0.2, places=6)
        self.assertAlmostEqual(propane.expected_use(st, [50, 50]), 12.0, places=6)
        # 250 gal, 50 in reserve: 200 usable at 12 a day
        self.assertAlmostEqual(propane.days_left(st, S, 250, [50, 50]), 200 / 12, places=6)
        few = {"days": [{"used": 4.0, "hdd": None}] * 5}
        self.assertEqual(propane.expected_use(few, [50]), 4.0)                     # before 14 days: the plain average
        self.assertIsNone(propane.days_left({"days": []}, S, 250))

    def test_forecast_hdd(self):
        with open(os.path.join(self.stack, "state", "wx-forecast.json"), "w") as f:
            json.dump({"units_us": True, "daily": {"temperature_2m_max": [40, 30, 70], "temperature_2m_min": [20, 10, 50]}}, f)
        self.assertEqual(propane.forecast_hdd(self.stack), [45.0, 5.0])                    # today left out
        with open(os.path.join(self.stack, "state", "wx-forecast.json"), "w") as f:
            json.dump({"units_us": False, "daily": {"temperature_2m_max": [0, 0], "temperature_2m_min": [0, -10]}}, f)
        self.assertEqual(propane.forecast_hdd(self.stack), [42.0])


class Module(Base):
    def test_settings_env_and_form(self):
        self.assertFalse(modules.load(self.stack, "propane")["enabled"])
        with open(os.path.join(self.stack, "state", "secrets.json"), "w") as f:
            json.dump({"mqtt_homeassistant": "pw"}, f)
        self.assertEqual(mod.env(self.stack), ["PROPANE_TANK_TYPE=500g_h", "PROPANE_MQTT_PASSWORD=pw"])
        form = {"pr_tank": "500g_h", "pr_address": "e4:c4:15:4b:b6:9e", "pr_warn": "25", "pr_critical": "10",
                "pr_reserve": "10", "pr_price": "2.49"}
        mod.form_check(self.stack, form)
        for bad in ({"pr_tank": "x"}, {"pr_address": "nope"}, {"pr_critical": "30"}, {"pr_price": "two"}, {"pr_warn": "99"}):
            with self.assertRaises(FieldError):
                mod.form_check(self.stack, {**form, **bad})
        started = []
        old = mod.start
        mod.start = lambda stack: started.append(1)
        try:
            msgs = mod.form_save(self.stack, form)
        finally:
            mod.start = old
        self.assertEqual(started, [1])
        self.assertIn("restarted", msgs[0])
        self.assertIn("PROPANE_TANKS=E4:C4:15:4B:B6:9E=500g_h:Propane tank", mod.env(self.stack))
        self.assertEqual(mod.settings(self.stack)["warn"], 25)

    def test_sample_page_and_fields(self):
        sensor = {"address": "E4:C4:15:4B:B6:9E", "sensorReadQuality": 3, "rssi": -70, "levelPercent": 72.5,
                  "tempCelsius": 15.0, "batteryVoltage": 3.1, "ageSeconds": 4}
        old = propane.sensors, propane.vm_push, propane.hdd_from_vm
        propane.sensors = lambda timeout=5: [sensor]
        propane.vm_push = lambda *a: None
        propane.hdd_from_vm = lambda d: None
        sent = []
        import notify
        old_alert = notify.alert
        notify.alert = lambda stack, *a: sent.append(a) or ""
        try:
            mod.sample(self.stack, now=DAY0)
            sensor["sensorReadQuality"] = 0                       # no usable echo: not taken
            mod.sample(self.stack, now=DAY0 + 60)
            self.assertEqual(len(propane.load(self.stack)["samples"]), 1)
            propane.sensors = lambda timeout=5: (_ for _ in ()).throw(OSError("down"))
            mod.sample(self.stack, now=DAY0 + 7 * 3600)          # service down 7 h: the silent alert
            self.assertEqual(sent[-1][0], "propane_silent")
        finally:
            propane.sensors, propane.vm_push, propane.hdd_from_vm = old
            notify.alert = old_alert
        f = mod.ha_fields(self.stack, {})
        self.assertEqual((f["propane_level_percent"], f["propane_gallons"], f["propane_low"]), (72.5, 362.5, False))
        body = mod.page(self.stack, {}).body
        self.assertIn("72", body)
        self.assertIn("E4:C4:15:4B:B6:9E", body)
        self.assertIn("Propane</b>: 72%", mod.card(self.stack))


if __name__ == "__main__":
    unittest.main()
