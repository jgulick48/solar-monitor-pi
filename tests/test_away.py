"""Tests for away mode (setup/away.py, the "away" module): the switch and its end date, the battery and water rules,
the daily report, the freeze module's earlier alerts while away, the setup form and config export.
Run: python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
os.environ.setdefault("SOLAR_COMPOSE", "true")
import away  # noqa: E402
import configfile  # noqa: E402
import freeze  # noqa: E402
import modules  # noqa: E402
from modules import away as mod  # noqa: E402
from modules import freeze as freeze_mod  # noqa: E402

T0 = time.mktime((2026, 12, 1, 12, 0, 0, 0, 0, -1))           # a Tuesday noon, local time
SITE = {"name": "Cabin", "units": "us_customary", "low_soc_alert": "25", "generator": "manual"}


class Away(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state", "modules"))
        with open(os.path.join(self.stack, "state", "site.json"), "w") as f:
            json.dump(SITE, f)
        self.enable("away")
        self.old_alert = mod.notify.alert
        self.cleared = []
        mod.notify.alert = lambda stack, *a: self.cleared.append(a) or ""

    def tearDown(self):
        mod.notify.alert = self.old_alert
        modules._ALL = None
        self.tmp.cleanup()

    def enable(self, mid, **settings):
        d = modules.load(self.stack, mid)
        d["enabled"] = True
        d["settings"] = {**d["settings"], **settings}
        with open(modules.path(self.stack, mid), "w") as f:
            json.dump(d, f)

    def minute(self, t, soc=80, age=10, alerts=(), **advice):
        sent = []
        mod.every_minute(self.stack, {"now": {"soc": soc, "age": age}, "advice": advice or None,
                                      "alerts": [{"text": a} for a in alerts]}, now=t, send=lambda *a: sent.append(a) or "")
        return sent

    # ---------------------------------------------------------------- the switch
    def test_switch_and_end_date(self):
        self.assertFalse(away.active(self.stack))
        self.assertEqual(away.freeze_margin(self.stack), 0.0)
        self.assertIn("Away mode</b>: off", mod.card(self.stack))
        self.assertEqual(self.minute(T0, soc=10), [])                          # off: nothing, whatever the battery
        m, fn = modules.route(self.stack, "POST", "/away/set")
        self.assertEqual(fn(self.stack, {"on": "1"}).location, "/")
        self.assertTrue(away.active(self.stack))
        self.assertAlmostEqual(away.freeze_margin(self.stack), 2.78)
        self.assertIn("card warn", mod.card(self.stack))
        self.assertTrue(modules.ha_fields(self.stack, SITE)["away_mode"])
        self.assertIn("On since", mod.checks(self.stack)[0]["text"])
        fn(self.stack, {"on": "0"})
        self.assertFalse(away.active(self.stack))
        self.assertEqual({a[0] for a in self.cleared}, set(away.TAGS))         # its alerts are cleared with it
        self.assertEqual(mod.checks(self.stack)[0]["state"], "ok")
        # an end date: the last day away; the day after, it turns itself off and says so
        away.set_away(self.stack, True, "2026-12-03", now=T0)
        self.assertTrue(away.active(self.stack, T0 + 2 * 86400 + 11 * 3600))   # late on the 3rd
        self.assertFalse(away.active(self.stack, T0 + 3 * 86400))              # the 4th
        self.assertEqual(self.minute(T0 + 60), [])
        sent = self.minute(T0 + 3 * 86400)
        self.assertEqual(sent[-1][:2], ("away_ended", "Away mode has ended"))
        self.assertFalse(away.settings(self.stack)["away"])
        self.assertEqual(self.minute(T0 + 3 * 86400 + 60), [])
        # with the module off, never away
        away.set_away(self.stack, True, now=T0)
        modules.set_enabled(self.stack, "away", False)
        self.assertFalse(away.active(self.stack))

    def test_setup_form(self):
        m, fn = modules.route(self.stack, "POST", "/away/save")
        html = mod.setup_section(self.stack)
        self.assertIn('name="freeze_margin" type="number" step="any" min="0" value="5.0"', html)      # °F for a US site
        r = fn(self.stack, {"away": "1", "until": "2099-01-05", "report": "1", "report_time": "07:30",
                            "freeze_margin": "9", "battery_margin": "20"})
        self.assertIn("away mode is on since", r.setup_msg)
        s = away.settings(self.stack)
        self.assertEqual((s["report_time"], s["freeze_margin_c"], s["battery_margin"], s["water"], s["until"]),
                         ("07:30", 5.0, 20.0, False, "2099-01-05"))
        self.assertEqual(fn(self.stack, {"away": "1", "until": "soon"}).code, 400)
        self.assertEqual(fn(self.stack, {"away": "1", "report_time": "25:00"}).code, 400)
        r = fn(self.stack, {"away": "1", "until": "2020-01-01", "report_time": "07:30"})
        self.assertEqual(r.code, 400)                                          # an end date in the past
        self.assertFalse(away.active(self.stack))
        fn(self.stack, {"report_time": "07:30"})                               # unticked: off, settings kept
        self.assertEqual(away.settings(self.stack)["report_time"], "07:30")

    def test_config_export_leaves_out_being_away(self):
        self.assertIn("away", configfile.PORTABLE)
        self.assertEqual(sorted(m.id for m in modules.all_modules()), sorted(configfile.PORTABLE + configfile.LOCAL))
        self.assertEqual(configfile.DROP["modules/away.json"], ("settings.away", "settings.since", "settings.until"))

    # ---------------------------------------------------------------- battery
    def test_battery_warning(self):
        away.set_away(self.stack, True, now=T0)
        self.enable("away", report=False)
        self.assertEqual(self.minute(T0 + 60, soc=41), [])                     # above 25 + 15
        self.assertEqual(self.minute(T0 + 120, soc=40), [])                    # there, but not for 10 minutes yet
        self.assertEqual(self.minute(T0 + 600, soc=39, age=900), [])           # stale data decides nothing
        (tag, title, text, prio), = self.minute(T0 + 780, soc=39, level="warn", text="Start the generator before dark.")
        self.assertEqual((tag, prio), ("away_battery", "warning"))
        self.assertIn("The battery is at 39% with nobody at the property; the low-battery level is 25%. Start the generator", text)
        self.assertEqual(self.minute(T0 + 840, soc=30), [])                    # said once
        self.assertEqual(self.minute(T0 + 900, soc=42), [])                    # not back 3 points above yet
        self.assertEqual(self.minute(T0 + 960, soc=43)[0][2], "clear")
        self.assertEqual(self.minute(T0 + 1020, soc=43), [])

    # ---------------------------------------------------------------- water
    def test_water_rule(self):
        st = {}
        since = T0
        runs = [{"start": T0 - 500, "s": 60.0}]                                # before away mode: nobody's business
        self.assertIsNone(away.water_rule(st, runs, None, since, T0 + 60))
        runs += [{"start": T0 + 3600, "s": 90.0}, {"start": T0 + 5400, "s": 45.0}]
        text = away.water_rule(st, runs, None, since, T0 + 5500)
        self.assertIn("The water pump ran 2 times (2 min in all) since Tue 13:00 while away mode is on", text)
        self.assertIsNone(away.water_rule(st, runs, None, since, T0 + 5600))   # told
        runs.append({"start": T0 + 9000, "s": 30.0})
        self.assertIsNone(away.water_rule(st, runs, None, since, T0 + 9100))   # within 6 hours: kept
        text = away.water_rule(st, runs, T0 + 30000, since, T0 + 5500 + away.WATER_GAP_S)
        self.assertIn("ran 2 times (1 min in all)", text)                      # the kept run and the one going now
        self.assertIn("and is running now", text)
        self.assertIn("ran once", away.water_rule({}, [], T0 + 50, since, T0 + 60))

    def test_water_alert_from_the_water_module(self):
        self.enable("water")
        away.set_away(self.stack, True, now=T0)
        self.enable("away", report=False)
        import water
        st = water.load(self.stack)
        st["runs"] = [{"start": T0 + 600, "s": 120.0, "w": 700.0}]
        st["pump"] = {"entity": "sensor.pump", "on": False, "since": None}
        water.save(self.stack, st)
        (tag, title, text, prio), = self.minute(T0 + 900)
        self.assertEqual((tag, title, prio), ("away_water", "Water used while away", "warning"))
        self.assertEqual(self.minute(T0 + 960), [])
        self.enable("away", water=False)                                       # "something uses water while I'm away"
        st["runs"].append({"start": T0 + 90000, "s": 120.0, "w": 700.0})
        water.save(self.stack, st)
        self.assertEqual(self.minute(T0 + 90100), [])

    # ---------------------------------------------------------------- the daily report
    def test_report_once_a_day_at_its_time(self):
        st = {}
        s = {**away.DEFAULTS, "away": True, "since": T0, "report_time": "08:00"}
        self.assertFalse(away.report_due(st, s, T0 + 3600))                    # turned on at noon: not today any more
        self.assertFalse(away.report_due(st, s, T0 + 19 * 3600))               # 07:00 the next day
        self.assertTrue(away.report_due(st, s, T0 + 20 * 3600 + 60))           # 08:01
        st["report_day"] = time.strftime("%Y-%m-%d", time.localtime(T0 + 20 * 3600))
        self.assertFalse(away.report_due(st, s, T0 + 23 * 3600))
        self.assertTrue(away.report_due(st, s, T0 + 44 * 3600 + 60))           # the day after
        self.assertFalse(away.report_due({}, {**s, "report": False}, T0 + 20 * 3600 + 60))
        self.assertEqual(away.report_text("Cabin", ["Battery 80%."], []), ("All is well at Cabin", "Battery 80%."))
        self.assertEqual(away.report_text("", ["Battery 80%."], ["No heat.", "Dongle offline."]),
                         ("Away check: 2 things to look at", "No heat. Dongle offline. Otherwise: Battery 80%."))

    def test_report_content(self):
        import checkup
        import display
        old = display.day_stats, checkup.core
        display.day_stats = lambda a, b: {"low_soc": 61.2, "solar": 14.4, "gen_hours": 0.0}
        checkup.core = lambda stack, site: [{"title": "Inverter dongles", "state": "ok", "text": "2 connected.", "link": None}]
        d = modules.load(self.stack, "notifications")                         # on by default, with nowhere to send
        d["enabled"] = False                                                   # in a bare test box: that would be a problem
        with open(modules.path(self.stack, "notifications"), "w") as f:
            json.dump(d, f)
        try:
            away.set_away(self.stack, True, now=T0)
            sent = self.minute(T0 + 20 * 3600 + 60, soc=82)
            (tag, title, text, prio), = sent
            self.assertEqual((tag, title, prio), ("away_report", "All is well at Cabin", "info"), text)
            self.assertIn("Battery 82% (lowest yesterday 61%, 14 kWh of solar yesterday).", text)
            self.assertEqual(self.minute(T0 + 20 * 3600 + 120, soc=82), [])    # once a day
            # the next day something is wrong: the check-up's red lines and the display's alerts lead
            checkup.core = lambda stack, site: [{"title": "Inverter dongles", "state": "bad", "text": "No connection yet.", "link": None}]
            (tag, title, text, prio), = self.minute(T0 + 44 * 3600 + 60, soc=82, alerts=["1 of 2 inverter dongles connected."])
            self.assertEqual(title, "Away check at Cabin: 2 things to look at")
            self.assertTrue(text.startswith("Inverter dongles: No connection yet. 1 of 2 inverter dongles connected. Otherwise: Battery 82%"))
        finally:
            display.day_stats, checkup.core = old

    # ---------------------------------------------------------------- freeze protection while away
    def test_freeze_speaks_earlier(self):
        self.enable("freeze", sensors=["sensor.slab"])
        self.assertAlmostEqual(freeze_mod.effective(self.stack)["below_c"], 4.44)
        away.set_away(self.stack, True, now=T0)
        e = freeze_mod.effective(self.stack)
        self.assertAlmostEqual(e["below_c"], 4.44 + 2.78)                      # 40 °F -> 45 °F
        self.assertTrue(e["away"])
        self.assertAlmostEqual(freeze_mod.settings(self.stack)["below_c"], 4.44)   # the setup form keeps the owner's value
        self.assertRegex(freeze_mod.form_section(self.stack), r"""name=.fz_below.[^>]*value=.40.""")
        # 43 °F inside: fine at home, cold while away
        rd = [{"entity": "sensor.slab", "name": "Slab", "ok": True, "c": 6.1}]
        sent = []
        alert = lambda *a: sent.append(a)  # noqa: E731
        for i in range(7):
            freeze.step(self.stack, freeze_mod.settings(self.stack), rd, SITE, T0 + i * 60, alert)
        self.assertEqual(sent, [])
        for i in range(7, 14):
            freeze.step(self.stack, e, rd, SITE, T0 + i * 60, alert, away=True)
        self.assertEqual([a[0] for a in sent], ["freeze_cold"])
        self.assertIn("below 45 °F", sent[0][2])
        # a hard freeze in the forecast: told while away even with the heat and the battery fine
        sent.clear()
        warm = [{"entity": "sensor.slab", "name": "Slab", "ok": True, "c": 15.0}]
        freeze.step(self.stack, freeze_mod.settings(self.stack), warm, SITE, T0 + 3600, alert, soc=90, low=(-12.0, T0 + 20000))
        self.assertEqual([a[0] for a in sent if a[2] != "clear"], [])
        freeze.step(self.stack, e, warm, SITE, T0 + 3660, alert, soc=90, low=(-12.0, T0 + 20000), away=True)
        risk = [a for a in sent if a[0] == "freeze_risk"]
        self.assertEqual(len(risk), 1)
        self.assertIn("nobody is at the property (away mode)", risk[0][2])


if __name__ == "__main__":
    unittest.main()
