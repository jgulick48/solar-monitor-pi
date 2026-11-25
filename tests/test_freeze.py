"""Tests for freeze protection (setup/freeze.py, the "freeze" module): readings, the forecast low, the three rules and
their alerts, the setup form.
Run: python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import freeze  # noqa: E402
import modules  # noqa: E402
from modules import FieldError  # noqa: E402
from modules import freeze as mod  # noqa: E402

T0 = 1795000000.0
SITE = {"units": "us_customary", "low_soc_alert": "25"}
S = {**freeze.DEFAULTS, "sensors": ["sensor.slab", "climate.shop"]}


def rd(slab=50.0, shop=60.0):
    """Readings in °F; None: no reading."""
    out = []
    for e, name, f in (("sensor.slab", "Slab", slab), ("climate.shop", "Shop", shop)):
        out.append({"entity": e, "name": name, "ok": f is not None, "c": None if f is None else round((f - 32) * 5 / 9, 2)})
    return out


class Freeze(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))
        self.sent = []
        self.t = T0

    def tearDown(self):
        modules._ALL = None
        self.tmp.cleanup()

    def step(self, minutes=1, s=S, site=SITE, **kw):
        readings = kw.pop("readings", None) or rd()
        for _ in range(minutes):
            self.t += 60
            freeze.step(self.stack, s, readings, site, self.t, lambda *a: self.sent.append(a), **kw)

    def test_reading(self):
        now = T0
        self.assertEqual(freeze.reading({"state": "41", "unit": "°F", "at": now}, "°F", now), {"ok": True, "c": 5.0, "raw": "41"})
        self.assertEqual(freeze.reading({"state": "5.5", "unit": "°C", "at": now}, "°F", now)["c"], 5.5)
        # a thermostat: its room temperature, in Home Assistant's unit system
        t = freeze.reading({"state": "heat", "attributes": {"current_temperature": 68}, "at": now}, "°F", now)
        self.assertEqual((t["ok"], t["c"]), (True, 20.0))
        self.assertFalse(freeze.reading({"state": "unavailable"}, "°F", now)["ok"])
        self.assertFalse(freeze.reading(None, "°F", now)["ok"])
        self.assertFalse(freeze.reading({"state": "41", "unit": "°F", "at": now - 3600}, "°F", now)["ok"])   # stale
        self.assertEqual((freeze.show(5.0, True), freeze.show(5.0, False), freeze.show(None, True)), ("41 °F", "5 °C", "--"))

    def test_forecast_low(self):
        self.assertIsNone(freeze.forecast_low(self.stack, T0))
        lt = lambda h: time.strftime("%Y-%m-%dT%H:00", time.gmtime(T0 // 3600 * 3600 + h * 3600 - 25200))  # noqa: E731
        doc = {"fetched": T0 - 600, "units_us": True, "utc_offset_seconds": -25200,
               "hourly": {"time": [lt(h) for h in range(-3, 40)],
                          "temperature_2m": [5.0 if h == -3 else 14.0 if h == 9 else 0.0 if h == 30 else 30.0 for h in range(-3, 40)]}}
        with open(os.path.join(self.stack, "state", "wx-forecast.json"), "w") as f:
            json.dump(doc, f)
        c, at = freeze.forecast_low(self.stack, T0)                       # 14 °F in 9 hours; not the past, not hour 30
        self.assertEqual((c, at), (-10.0, T0 // 3600 * 3600 + 9 * 3600))
        self.assertIsNone(freeze.forecast_low(self.stack, T0 + 4 * 3600))  # the forecast is too old by then

    def test_cold(self):
        self.step(3, readings=rd(slab=38))
        self.assertEqual(self.sent, [])                                    # under 5 minutes: a door left open
        self.step(3, readings=rd(slab=38))
        self.assertEqual(len(self.sent), 1)
        tag, title, msg, prio = self.sent[0]
        self.assertEqual((tag, prio), ("freeze_cold", "critical"))
        self.assertEqual(msg, "Slab is 38 °F, below 40 °F. Check the heat before pipes freeze.")
        self.step(30, readings=rd(slab=37))
        self.assertEqual(len(self.sent), 1)                                # said once
        self.step(2, readings=rd(slab=41))                                 # back over, but not by 1 °C: still on
        self.assertIn("cold", freeze.load(self.stack)["active"])
        self.step(1, readings=rd(slab=43))
        self.assertEqual(self.sent[-1][:1] + self.sent[-1][2:3], ("freeze_cold", "clear"))
        st = freeze.load(self.stack)
        self.assertEqual(st["active"], {})
        self.assertTrue(st["log"][0]["cleared"])

    def test_alerts_off(self):
        self.step(10, s={**S, "alerts": False}, readings=rd(slab=30))
        self.assertEqual(self.sent, [])
        self.assertIn("cold", freeze.load(self.stack)["active"])           # still shown on the pages

    def test_risk(self):
        low = (-10.0, T0 + 9 * 3600)
        self.step(2, low=low, soc=80)                                      # a hard freeze, heat and power fine
        self.step(2, low=(-2.0, T0), soc=10, boiler_fault="no heat")       # trouble, but only a light frost
        self.assertEqual(self.sent, [])
        self.step(1, low=low, soc=80, boiler_fault="no heat")
        tag, title, msg, prio = self.sent[0]
        self.assertEqual((tag, prio, title), ("freeze_risk", "warning", "Hard freeze forecast"))
        self.assertRegex(msg, r"^14 °F is forecast about \d+:\d\d [AP]M and the boiler has a fault \(no heat\)\. "
                              r"Coldest watched temperature now: Slab, 50 °F\.$")
        self.step(1, low=low, soc=80)                                      # fault gone
        self.assertEqual(self.sent[-1][2], "clear")
        self.step(1, low=low, soc=22)
        self.assertIn("the battery is low (22%)", self.sent[-1][2])

    def test_silent_sensor(self):
        self.step(29, readings=rd(shop=None))
        self.assertEqual(self.sent, [])
        self.step(2, readings=rd(shop=None))
        self.assertEqual(self.sent[0][0], "freeze_sensor")
        self.assertIn("Shop: no temperature for 30 minutes", self.sent[0][2])
        self.step(1)
        self.assertEqual(self.sent[-1][2], "clear")

    def test_form(self):
        with open(os.path.join(self.stack, "state", "site.json"), "w") as f:
            json.dump(SITE, f)
        html = mod.form_section(self.stack)
        self.assertIn("name=\"fz_below\" type=\"number\" step=\"any\" value=\"40\"", html)
        self.assertIn("name=\"fz_hard\" type=\"number\" step=\"any\" value=\"20\"", html)
        got = mod._parse(self.stack, {"fz_sensor1": " Sensor.Slab ", "fz_sensor3": "climate.shop", "fz_sensor4": "sensor.slab",
                                      "fz_below": "45", "fz_hard": "10", "fz_alerts": "on"})
        self.assertEqual(got, {"sensors": ["sensor.slab", "climate.shop"], "below_c": 7.22, "hard_freeze_c": -12.22, "alerts": True})
        self.assertEqual(mod._parse(self.stack, {})["below_c"], freeze.DEFAULTS["below_c"])
        for bad in ({"fz_sensor1": "switch.pump"}, {"fz_sensor2": "not an entity"}, {"fz_below": "20"}, {"fz_hard": "40"}):
            with self.assertRaises(FieldError):
                mod._parse(self.stack, bad)

    def test_fields_and_panel(self):
        self.assertEqual(mod.display_panel(self.stack, SITE)["note"], "Pick the temperatures to watch in setup")
        modules.save(self.stack, "freeze", {"sensors": ["sensor.slab"]})
        f = mod.ha_fields(self.stack, SITE)
        self.assertEqual((f["freeze_alert"], f["freeze_lowest_c"], f["freeze_cold"]), ("none", None, False))
        self.assertTrue(all(k.startswith("freeze_") for k in f))
        p = mod.display_panel(self.stack, SITE)
        self.assertEqual((p["level"], p["tiles"][0]["v"], p["tiles"][0]["s"]), ("", "--", "no reading"))
        self.assertIn("Freeze protection", mod.card(self.stack))


if __name__ == "__main__":
    unittest.main()
