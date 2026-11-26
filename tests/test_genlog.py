"""Tests for the generator log (genlog.py): run detection, both kinds, service hours, backfill in steps.
Run: python3 -m unittest discover -s tests -v"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import genlog  # noqa: E402

DAY = 86400
T0 = 1_790_000_000 // DAY * DAY      # a midnight UTC


def fake_history(spans):
    """spans: {kind: [(start, end, watts), ...]} -> fetch(q, start, end) like genlog.vm_range."""
    def fetch(q, start, end):
        kind = next(k for k, v in genlog.QUERIES.items() if v == q)
        out = {}
        for t in range(start, end + 1, genlog.STEP):
            out[t] = 0.0
            for a, b, w in spans.get(kind, []):
                if a <= t < b:
                    out[t] = w
        return out
    return fetch


class GenLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))

    def tearDown(self):
        self.tmp.cleanup()

    def run_until(self, now, fetch):
        for _ in range(40):
            genlog.update(self.stack, now=now, fetch=fetch)
            if genlog.load(self.stack)["processed_until"] >= (now - genlog.LAG) // 60 * 60:
                return
        self.fail("backfill didn't finish")

    def test_runs_both_kinds_and_dips(self):
        h = 3600
        fetch = fake_history({
            "inverter": [(T0 - 5 * DAY, T0 - 5 * DAY + 2 * h, 6000),
                         # a 3-minute dip doesn't split the run; a 2-minute blip isn't a run
                         (T0 - 3 * DAY, T0 - 3 * DAY + h, 5000), (T0 - 3 * DAY + h + 180, T0 - 3 * DAY + 2 * h, 5000),
                         (T0 - 2 * DAY, T0 - 2 * DAY + 120, 4000)],
            "charger": [(T0 - DAY, T0 - DAY + 3 * h, 1800), (T0 - DAY + 4 * h, T0 - DAY + 4 * h + 600, 400),  # 400 W: below 500
                        # two 1-minute spikes 3 minutes apart (BMS vs inverter timing): not a run
                        (T0 - DAY + 6 * h, T0 - DAY + 6 * h + 60, 3500), (T0 - DAY + 6 * h + 240, T0 - DAY + 6 * h + 300, 1700)],
        })
        self.run_until(T0, fetch)
        st = genlog.load(self.stack)
        runs = [(r["kind"], round(r["hours"], 2)) for r in st["runs"]]
        self.assertEqual(runs, [("inverter", 2.0), ("inverter", 2.0), ("charger", 3.0)])
        self.assertAlmostEqual(st["runs"][0]["kwh"], 12.0, places=1)
        self.assertEqual(st["runs"][0]["peak_kw"], 6.0)
        self.assertEqual(st["hours"], {"inverter": 4.0, "charger": 3.0})
        s = genlog.status(self.stack, {"generator": "manual"}, now=T0)
        k = {x["kind"]: x for x in s["kinds"]}
        self.assertEqual((k["inverter"]["interval"], k["charger"]["interval"]), (200, 100))
        self.assertEqual(k["charger"]["since_service"], 3.0)
        self.assertEqual(k["charger"]["runs_30d"], 1)
        self.assertFalse(s["running"])

    def test_backfill_is_done_a_month_at_a_time(self):
        fetch = fake_history({})
        genlog.update(self.stack, now=T0, fetch=fetch)
        st = genlog.load(self.stack)
        self.assertEqual(st["processed_until"], T0 - genlog.BACKFILL_DAYS * DAY + genlog.MAX_CHUNKS * DAY)
        self.assertTrue(genlog.status(self.stack, {}, now=T0)["backfilling"])

    def test_running_now_and_service(self):
        start = T0 - 3600
        fetch = fake_history({"inverter": [(start, T0 + DAY, 7000)]})
        self.run_until(T0, fetch)
        s = genlog.status(self.stack, {"generator": "automatic"}, now=T0)
        [k] = s["kinds"]
        self.assertTrue(s["running"] and k["running"])
        self.assertEqual(k["since_service"], 1.0)     # the open run counts already (58 min so far)
        self.assertEqual(genlog.load(self.stack)["runs"], [])
        genlog.mark_serviced(self.stack, "inverter", hours_since=0.5, now=T0)
        k = genlog.status(self.stack, {}, now=T0)["kinds"][0]
        self.assertAlmostEqual(k["since_service"], 0.5, places=2)
        self.assertEqual(k["serviced_on"], __import__("time").strftime("%Y-%m-%d", __import__("time").localtime(T0)))
        f = genlog.ha_fields(self.stack, {}, now=T0)
        self.assertTrue(f["generator_running"])
        self.assertEqual(f["generator_service_interval"], 200)
        self.assertFalse(f["generator_service_due"])

    def test_service_due(self):
        fetch = fake_history({"charger": [(T0 - 10 * DAY + i * DAY, T0 - 10 * DAY + i * DAY + 11 * 3600, 2000) for i in range(10)]})
        self.run_until(T0, fetch)
        f = genlog.ha_fields(self.stack, {"generator": "manual"}, now=T0)
        self.assertTrue(f["generator_service_due"])
        self.assertEqual(f["generator_hours_since_service"], 110.0)
        self.assertEqual(f["generator_runs_30d"], 10)

    def test_no_generator(self):
        self.assertEqual(genlog.status(self.stack, {"generator": "none"})["kinds"], [])
        self.assertEqual(genlog.ha_fields(self.stack, {"generator": "none"})["generator_hours_since_service"], None)
        self.assertEqual([k["kind"] for k in genlog.status(self.stack, {"generator": "manual"})["kinds"]], ["charger"])


if __name__ == "__main__":
    unittest.main()


class FuelAndCost(unittest.TestCase):
    """Fuel and cost are worked out when shown, from the site's generator size, fuel, measured use and fuel price."""
    RUN = {"kind": "charger", "start": 1_795_000_000, "end": 1_795_007_200, "hours": 2.0, "kwh": 9.2, "peak_kw": 4.8}
    SITE = {"generator_kw": "7", "generator_fuel": "propane", "costs": {"fuel_per_gal": "3.50"}}

    def test_typical_figure_follows_the_load(self):
        # 9.2 kWh in 2 h through a charger = 5 kW from the generator, 5/7 of a 7 kW propane one
        want = (0.11 * 7 * (0.3 + 0.7 * 5 / 7) + 0.2) * 2
        self.assertAlmostEqual(genlog.fuel(self.RUN, self.SITE), want, places=3)
        gen_input = {**self.RUN, "kind": "inverter"}             # on the GEN input the reading is the generator's own
        self.assertLess(genlog.fuel(gen_input, self.SITE), want)
        self.assertAlmostEqual(genlog.cost(genlog.fuel(self.RUN, self.SITE), self.SITE), want * 3.5, places=3)
        # a load above the generator's size counts as full load
        self.assertAlmostEqual(genlog.fuel({**self.RUN, "kwh": 40.0}, self.SITE), (0.11 * 7 + 0.2) * 2, places=3)

    def test_measured_figure_wins(self):
        site = {**self.SITE, "generator_gph": "0.6"}
        self.assertAlmostEqual(genlog.fuel(self.RUN, site), 1.2)
        self.assertIn("0.6 gallons an hour, as measured", genlog.fuel_note(site))
        self.assertEqual(genlog.fuel(self.RUN, {"generator_gph": "0.6"}), 1.2)    # no size needed then

    def test_nothing_to_work_from(self):
        self.assertIsNone(genlog.fuel(self.RUN, {"generator_kw": ""}))
        self.assertIn("enter the generator's size", genlog.fuel_note({}))
        self.assertIsNone(genlog.cost(1.0, {"costs": {"fuel_per_gal": ""}}))
        self.assertIsNone(genlog.cost(None, self.SITE))
        t = genlog.totals([self.RUN], {})
        self.assertEqual((t["runs"], t["hours"], t["fuel"], t["cost"]), (1, 2.0, None, None))
        self.assertIn("Enter the fuel price", genlog.fuel_note({"generator_kw": "7"}))

    def test_months(self):
        day = 86400
        runs = [self.RUN, {**self.RUN, "start": self.RUN["start"] + day}, {**self.RUN, "start": self.RUN["start"] - 40 * day}]
        m = genlog.months(runs, {**self.SITE, "generator_gph": "0.5"})
        self.assertEqual([(x["runs"], x["hours"], x["fuel"], x["cost"]) for x in m], [(2, 4.0, 2.0, 7.0), (1, 2.0, 1.0, 3.5)])
        self.assertGreater(m[0]["month"], m[1]["month"])

    def test_status_and_ha_fields(self):
        with tempfile.TemporaryDirectory() as stack:
            os.mkdir(os.path.join(stack, "state"))
            st = genlog.load(stack)
            st["runs"] = [self.RUN]
            st["hours"]["charger"] = 2.0
            st["processed_until"] = self.RUN["end"] + 60
            genlog.save(stack, st)
            site = {**self.SITE, "generator": "manual", "generator_gph": "0.5"}
            now = self.RUN["end"] + 3600
            k = genlog.status(stack, site, now)["kinds"][0]
            self.assertEqual((k["fuel_30d"], k["cost_30d"]), (1.0, 3.5))
            f = genlog.ha_fields(stack, site, now)
            self.assertEqual((f["generator_fuel_30d"], f["generator_cost_30d"], f["generator_last_run_fuel"]), (1.0, 3.5, 1.0))
            f = genlog.ha_fields(stack, {"generator": "manual"}, now)           # no size, no price: no figures
            self.assertEqual((f["generator_fuel_30d"], f["generator_cost_30d"], f["generator_last_run_fuel"]), (None, None, None))
