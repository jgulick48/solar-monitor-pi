"""Tests for the panel and inverter checks (solarcheck.py).
Run: python3 -m unittest discover -s tests -v"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import checkup  # noqa: E402
import solarcheck as sc  # noqa: E402

N = sc.BASE_DAYS + 1
NOW = 1_800_000_000 // 3600 * 3600 + 600          # ten minutes past an hour
HOUR = NOW // 3600 * 3600
SITE = {"latitude": "40", "longitude": "-105", "arrays": [{"panels": "12", "panel_watts": "400", "tilt": "30", "azimuth": "180"},
                                                         {"panels": "12", "panel_watts": "400", "tilt": "30", "azimuth": "180"}]}


def flat(now, usual=1000.0):
    return [usual] * (N - 1) + [now]


def data(**kw):
    """Two inverters with two equal strings each, sharing the house, on a bright day with the battery at 60%."""
    d = {"strings": {("A", 1): flat(1000), ("A", 2): flat(1000), ("B", 1): flat(1000), ("B", 2): flat(1000)},
         "soc_days": [60.0] * N, "output": {"A": flat(800, 800), "B": flat(800, 800)},
         "pv_h": {HOUR - i * 3600: 4000.0 for i in range(24)}, "soc_h": {HOUR - i * 3600: 60.0 for i in range(24)},
         "ghi_h": {}, "temp_c": 15.0}
    d.update(kw)
    return d


def forecast(kw=5.0, source="calibrated"):
    return {"generated": NOW, "calibration_source": source, "hours": [[HOUR - i * 3600, kw] for i in range(30)]}


def ev(d, fc=None, adv=None):
    return sc.evaluate(d, SITE, forecast() if fc is None else fc, adv or {}, NOW)


def low(res):
    return {k: r for k, r in res["results"].items() if r["state"] == "low"}


class Shares(unittest.TestCase):
    def test_all_as_usual(self):
        r = ev(data())
        self.assertEqual(low(r), {})
        self.assertEqual(r["results"]["string:A:1"], {"state": "fine"})
        self.assertEqual(r["results"]["output:B"], {"state": "fine"})

    def test_one_string_out(self):
        d = data()
        d["strings"][("A", 2)] = flat(40)
        r = low(ev(d))
        self.assertEqual(list(r), ["string:A:2"])
        self.assertEqual(r["string:A:2"]["level"], "warn")
        self.assertIn("Inverter A, PV string 2 is making 40 W where about 1.0 kW is usual", r["string:A:2"]["text"])
        self.assertIn("shade or dirt", r["string:A:2"]["text"])
        # in the cold, snow is named first
        self.assertIn("Snow on those panels", low(ev({**d, "temp_c": -3.0}))["string:A:2"]["text"])

    def test_whole_inverter_out_is_one_finding(self):
        d = data()
        d["strings"][("A", 1)] = flat(0)
        d["strings"][("A", 2)] = flat(5)
        r = ev(d)
        self.assertEqual(list(low(r)), ["pv:A"])
        self.assertIn("Inverter A is making almost no solar", r["results"]["pv:A"]["text"])
        self.assertEqual(r["results"]["pv:B"], {"state": "fine"})

    def test_not_judged_with_a_full_battery_or_poor_light(self):
        d = data(soc_days=[60.0] * (N - 1) + [99.0])
        d["strings"][("A", 2)] = flat(40)
        r = ev(d)
        self.assertEqual(low(r), {})
        self.assertNotIn("string:A:2", r["results"])          # unknown: an earlier finding would stay
        self.assertTrue(r["battery_full"])
        d = data(strings={("A", 1): flat(200), ("A", 2): flat(10), ("B", 1): flat(200), ("B", 2): flat(200)})
        self.assertEqual(low(ev(d)), {})                      # 200 W expected: too little light to say

    def test_shares_are_learned_per_string(self):
        # a small east string that usually makes a tenth of the site at this hour is not "low" at a tenth
        d = data(strings={("A", 1): flat(1800, 1800), ("A", 2): flat(200, 200)}, output={"A": flat(800, 800)})
        self.assertEqual(low(ev(d)), {})
        # days when the battery was full don't count towards the usual share, and five good days are needed
        d = data(soc_days=[99.0] * (N - 4) + [60.0] * 4)
        d["strings"][("A", 2)] = flat(40)
        r = ev(d)
        self.assertEqual(low(r), {})
        self.assertIsNone([s for s in r["strings"] if (s["inverter"], s["string"]) == ("A", 2)][0]["usual"])
        # a string that was never used isn't listed or judged
        d = data()
        d["strings"][("A", 3)] = flat(0, 0)
        r = ev(d)
        self.assertNotIn("string:A:3", r["results"])
        self.assertEqual(len(r["strings"]), 4)

    def test_missing_data_is_not_a_dead_string(self):
        d = data()
        d["strings"][("B", 1)] = [1000.0] * (N - 1) + [None]
        d["strings"][("B", 2)] = [1000.0] * (N - 1) + [None]
        self.assertEqual(low(ev(d)), {})                      # the dongle is offline: that has its own alert

    def test_inverter_output(self):
        r = low(ev(data(output={"A": flat(20, 800), "B": flat(1600, 800)})))
        self.assertEqual(list(r), ["output:A"])
        self.assertIn("supplying almost nothing (20 W) while the others carry the house (1.6 kW)", r["output:A"]["text"])
        # a quiet house: nothing to say
        self.assertEqual(low(ev(data(output={"A": flat(0, 800), "B": flat(300, 800)}))), {})
        # judged with a full battery too (the output doesn't depend on it)
        full = data(soc_days=[99.0] * N, output={"A": flat(20, 800), "B": flat(1600, 800)})
        self.assertEqual(list(low(ev(full))), ["output:A"])


class Site(unittest.TestCase):
    def pv(self, recent, before=4000.0, hours=2):
        return {HOUR - i * 3600: (recent if i < hours else before) for i in range(24)}

    def test_model_notes_first_then_warns(self):
        r = ev(data(pv_h=self.pv(1500.0)))
        self.assertEqual((r["results"]["site"]["level"], r["site"]["all_day"]), ("note", False))
        self.assertIn("1.5 kW over the last 2 hours where about 5.0 kW was expected", r["results"]["site"]["text"])
        self.assertIn("cloud the forecast didn't see", r["results"]["site"]["text"])
        r = ev(data(pv_h=self.pv(1500.0, before=1500.0), temp_c=-5.0))
        self.assertEqual(r["results"]["site"]["level"], "warn")
        self.assertIn("Snow on the panels is the usual cause in this cold", r["results"]["site"]["text"])

    def test_fine_and_in_between(self):
        self.assertEqual(ev(data())["results"]["site"], {"state": "fine"})
        self.assertNotIn("site", ev(data(pv_h=self.pv(2200.0)))["results"])      # 44%: left as it was

    def test_not_judged(self):
        d = data(pv_h=self.pv(500.0))
        for fc, why in ((forecast(source="default"), "isn't tuned"), ({}, "no solar forecast")):
            r = sc.evaluate(d, SITE, fc, {}, NOW)
            self.assertNotIn("site", r["results"])
            self.assertIn(why, r["site_why"])
        self.assertNotIn("site", ev(d, forecast(kw=0.8))["results"])                              # poor light
        self.assertNotIn("site", ev({**d, "soc_h": {HOUR - i * 3600: 97.0 for i in range(24)}})["results"])   # battery full
        self.assertNotIn("site", sc.evaluate(d, {**SITE, "arrays": []}, forecast(), {}, NOW)["results"])

    def test_station_sunlight_warns_at_once(self):
        adv = {"source": "weather station", "data": {"calibration": 0.85}}
        noon = 1_782_324_000                                   # 2026-06-24 18:00 UTC: midday at 105 W
        site = dict(SITE)
        d = data(ghi_h={noon - i * 3600: 900.0 for i in range(3)}, pv_h={noon - i * 3600: 900.0 for i in range(24)},
                 soc_h={noon - i * 3600: 60.0 for i in range(24)})
        r = sc.evaluate(d, site, forecast(source="default"), adv, noon + 300)
        self.assertEqual(r["site_source"], "station")
        self.assertEqual(r["results"]["site"]["level"], "warn")
        self.assertIn("from the weather station's sunlight", r["results"]["site"]["text"])
        self.assertGreater(r["site"]["expected_kw"], 5)
        ok = sc.evaluate({**d, "pv_h": {noon - i * 3600: 6000.0 for i in range(24)}}, site, {}, adv, noon + 300)
        self.assertEqual(ok["results"]["site"], {"state": "fine"})


class Latch(unittest.TestCase):
    def test_stays_until_the_check_passes(self):
        lowr = {"string:A:2": {"state": "low", "level": "warn", "text": "String 2 is low."}}
        a, msgs = sc.latch({}, lowr, 1000)
        self.assertEqual((a["string:A:2"]["since"], len(msgs)), (1000, 1))
        a, msgs = sc.latch(a, lowr, 1300)
        self.assertEqual((a["string:A:2"]["since"], a["string:A:2"]["seen"], msgs), (1000, 1300, []))
        a, msgs = sc.latch(a, {}, 50000)                       # night: not judged, still there
        self.assertIn("string:A:2", a)
        self.assertEqual(msgs, [])
        gone, msgs = sc.latch(a, {"string:A:2": {"state": "fine"}}, 90000)
        self.assertEqual((gone, msgs), ({}, ["solar check: cleared: String 2 is low."]))
        self.assertEqual(sc.latch(a, {}, 1300 + sc.DROP_S + 1)[0], {})        # couldn't be checked for a week
        # a note that becomes a warning is said again
        n, _ = sc.latch({}, {"site": {"state": "low", "level": "note", "text": "x"}}, 1)
        self.assertEqual(len(sc.latch(n, {"site": {"state": "low", "level": "warn", "text": "x"}}, 2)[1]), 1)


class RunAndPages(unittest.TestCase):
    def test_run_status_pages(self):
        with tempfile.TemporaryDirectory() as stack:
            os.mkdir(os.path.join(stack, "state"))
            self.assertEqual(sc.status(stack, NOW)["level"], "unknown")
            self.assertEqual(checkup.solar_check(sc.status(stack, NOW))["state"], "warn")
            self.assertIn("not checked yet", sc.card(sc.status(stack, NOW)))
            self.assertEqual(sc.run(stack, NOW, data()), [])
            s = sc.status(stack, NOW + 60)
            self.assertEqual((s["level"], s["findings"]), ("ok", []))
            self.assertEqual(checkup.solar_check(s)["state"], "ok")
            self.assertIn("not checked, the solar arrays aren't entered yet", sc.site_line(s))
            d = data()
            d["strings"][("A", 2)] = flat(40)
            self.assertEqual(len(sc.run(stack, NOW + 300, d)), 1)
            s = sc.status(stack, NOW + 360)
            self.assertEqual(s["level"], "warn")
            c = checkup.solar_check(s)
            self.assertEqual((c["state"], c["link"]), ("bad", ("/panels", "Panels")))
            page = sc.render(s)
            self.assertIn("far below usual", page)
            self.assertIn("<td>A</td><td>2</td><td>40 W</td><td>1.0 kW</td><td>25%</td>", page)
            self.assertIn("class='card warn'", sc.card(s))
            f = sc.ha_fields(stack)                              # real clock: the file is from "NOW", long ago
            self.assertEqual(f, {"solar_check": "unknown", "solar_check_text": ""})
            # a stopped collector: unknown, not a stale "all good"
            self.assertEqual(sc.status(stack, NOW + 300 + sc.STALE_S + 1)["level"], "unknown")


if __name__ == "__main__":
    unittest.main()
