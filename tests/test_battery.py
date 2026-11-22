"""Tests for battery health (battery.py) with a fake VictoriaMetrics.
Run: python3 -m unittest discover -s tests -v"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import battery  # noqa: E402
import display  # noqa: E402

NOW = 1_795_300_000
DAY = 86400
SITE = {"time_zone": "America/Denver", "battery_count": "9", "battery_kwh_each": "5.12"}


class Fake:
    """Answers battery.py's queries by keyword."""

    def __init__(self, **kw):
        self.v = dict(max_cell=3.35, min_cell=3.33, max_temp=22, min_temp=18, soh=98, cycles=310, bms_fault=0, bms_warning=0,
                      soc=80, rated_ah=900, spread_top=12, last_full=NOW - 2 * DAY, first=NOW - 200 * DAY,
                      max_temp_24h=25, min_temp_24h=15, bms_fault_24h=0, bms_warning_24h=0)
        self.v.update(kw)

    def vm_at(self, q, ts):
        v = self.v
        q = q.replace(battery.ss.BMS_UNITS, "")      # the "BMS-connected inverter" filter isn't a value
        if "time() and on()" in q:
            return v["last_full"] if "max_over_time" in q else v["first"]
        if "* 1000) and on()" in q:
            return v["spread_top"]
        table = [("lxp_max_cell_temp", "max_temp"), ("lxp_min_cell_temp", "min_temp"), ("lxp_fault_code_bms", "bms_fault"),
                 ("lxp_warning_code_bms", "bms_warning")]
        for metric, key in table:
            if metric in q:
                return v[key + "_24h"] if "_over_time" in q else v[key]
        for metric, key in [("lxp_max_cell_volt", "max_cell"), ("lxp_min_cell_volt", "min_cell"), ("lxp_soh", "soh"),
                            ("lxp_cycle_cnt_bms", "cycles"), ("lxp_batt_capacity", "rated_ah"), ("lxp_soc", "soc")]:
            if metric in q:
                return v[key]
        return None


def discharge_series(drop_from=95, drop_to=45, kw=2.3):  # 10 h, so energy = kw x 10 h
    """A night: 50% of a 46 kWh bank used at 2.3 kW (10 h), then charging back up."""
    soc, out_w, in_w = {}, {}, {}
    t0 = NOW - 2 * DAY
    hours = 10
    n = int(hours * 3600 / battery.STEP)
    for i in range(n + 1):
        t = t0 + i * battery.STEP
        soc[t] = round(drop_from - (drop_from - drop_to) * i / n)
        out_w[t] = kw * 1000
        in_w[t] = 0
    for i in range(1, 30):
        t = t0 + (n + i) * battery.STEP
        soc[t] = drop_to + i
        out_w[t] = 0
        in_w[t] = 5000
    return {"max(lxp_soc)": soc, "sum(lxp_p_discharge)": out_w, "sum(lxp_p_charge)": in_w}


class Primary(unittest.TestCase):
    def test_bms_data_only_from_the_inverter_that_has_it(self):
        # the other inverter of a parallel system reports 0 V / 0 C / 0 cycles: filtered out by inverter, not by value
        for k in ("min_cell", "min_temp", "max_temp", "cycles", "rated_ah", "bms_fault"):
            self.assertIn("and on (deploymentID) (max by (deploymentID) (lxp_max_cell_volt) > 0)", battery.Q[k])
        self.assertIn("> 0)", battery.Q["soh"])                    # SOH 0 = not reported (6000XP)
        self.assertNotIn("and on (deploymentID)", battery.Q["soc"])

    def test_bms_line(self):
        s = {"bms_inverters": ["DJ1"], "other_inverters": ["DJ2"]}
        self.assertEqual(battery.bms_line(s), "DJ1 (connected to the battery's BMS); DJ2 reports no BMS data, "
                                              "as usual for the other inverters of a parallel system")
        self.assertIn("none of the inverters", battery.bms_line({"bms_inverters": [], "other_inverters": ["DJ1"]}))
        self.assertEqual(battery.bms_line({"bms_inverters": ["A"], "other_inverters": []}), "A (connected to the battery's BMS)")


class Battery(unittest.TestCase):
    def setUp(self):
        self.orig = display.vm_at
        display._cache.clear()
        self.series = discharge_series()
        self.orig_series = battery.vm_series
        battery.vm_series = lambda q, s, e, step: self.series.get(q, {})

    def tearDown(self):
        display.vm_at = self.orig
        battery.vm_series = self.orig_series
        display._cache.clear()

    def status(self, **kw):
        display._cache.clear()
        display.vm_at = Fake(**kw).vm_at
        return battery.status("/tmp", SITE, now=NOW)

    def test_healthy(self):
        s = self.status()
        self.assertEqual(s["level"], "ok")
        self.assertEqual(s["spread_top_mv"], 12)
        self.assertIn("good", s["findings"][0][1])
        self.assertEqual(s["spread_now_mv"], 20)
        self.assertAlmostEqual(s["capacity_kwh"], 46.0, delta=1.0)
        f = battery.ha_fields("/tmp", SITE, now=NOW)
        self.assertEqual(set(f), set(battery.HA_KEYS))
        self.assertEqual((f["battery_health"], f["battery_soh"], f["bms_fault_code"]), ("ok", 98, 0))

    def test_spread_levels(self):
        self.assertEqual(self.status(spread_top=75)["level"], "note")
        s = self.status(spread_top=140)
        self.assertEqual(s["level"], "warn")
        self.assertIn("drifting", s["findings"][0][1])

    def test_not_full(self):
        self.assertEqual(self.status(last_full=NOW - 20 * DAY)["level"], "note")
        s = self.status(last_full=None, spread_top=None)
        self.assertEqual(s["level"], "warn")
        self.assertTrue(any("Not fully charged in the 200 days" in t for _, t in s["findings"]))
        # a box with 2 days of history and no full charge yet: nothing to warn about
        s = self.status(last_full=None, spread_top=None, first=NOW - 2 * DAY)
        self.assertEqual(s["level"], "ok")
        self.assertIn("not measured yet", s["findings"][0][1])

    def test_temperature_soh_bms(self):
        self.assertIn("too cold", " ".join(t for _, t in self.status(min_temp_24h=-2)["findings"]))
        self.assertIn("too hot", " ".join(t for _, t in self.status(max_temp=47)["findings"]))
        self.assertEqual(self.status(soh=75)["level"], "warn")
        s = self.status(bms_fault_24h=4)
        self.assertEqual(s["level"], "warn")
        self.assertIn("BMS fault code 4 in the last 24 hours", " ".join(t for _, t in s["findings"]))
        self.assertEqual(self.status(bms_warning=2)["level"], "note")

    def test_capacity_flags_small_battery(self):
        self.series = discharge_series(kw=1.0)  # same % drop, less than half the energy: the BMS counts a ~20 kWh bank
        s = self.status()
        self.assertLess(s["capacity_kwh"], 25)
        self.assertTrue(any("smaller" in t for _, t in s["findings"]))

    def test_capacity_flags_wrong_setup_size(self):
        self.series = discharge_series(kw=4.6)    # ~92 kWh bank vs 46 entered
        s = self.status()
        self.assertTrue(any("check the number of batteries" in t for _, t in s["findings"]))

    def test_short_discharges_dont_count(self):
        self.series = discharge_series(drop_from=80, drop_to=60)
        self.assertEqual(battery.capacity_estimate(NOW, fetch=lambda q, s, e, st: self.series.get(q, {})), [])

    def test_no_data(self):
        display._cache.clear()
        display.vm_at = lambda q, ts: None
        self.series = {}
        s = battery.status("/tmp", SITE, now=NOW)
        self.assertEqual(s["level"], "unknown")
        self.assertEqual(battery.ha_fields_empty()["battery_health"], "unknown")


if __name__ == "__main__":
    unittest.main()
