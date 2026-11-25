"""Tests for the expected-use profile (setup/loadprofile.py): the hour-of-week profile and level, the backtest that
decides between it and the flat 3-day average, the predictor's fallbacks, and the generator advice using it.
Run: python3 -m unittest discover -s tests -v"""
import datetime as dt
import json
import os
import random
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import display  # noqa: E402
import loadprofile  # noqa: E402

TZN = "America/Denver"
TZ = ZoneInfo(TZN)
NOW = int(dt.datetime(2026, 11, 20, 18, 0, tzinfo=TZ).timestamp())      # a Friday evening


def history(days, fn, now=NOW):
    """{end of hour: W} for the last `days`, from fn(local datetime of the hour's middle)."""
    end = now // 3600 * 3600
    return {ts: fn(dt.datetime.fromtimestamp(ts - 1800, TZ)) for ts in range(end - days * 86400 + 3600, end + 1, 3600)}


def weekly(t):
    """A house that is busy on weekday evenings, quiet at night and away at weekends."""
    if t.weekday() >= 5:
        return 400.0
    return 2500.0 if 17 <= t.hour < 22 else 600.0 if t.hour < 6 else 1000.0


class LoadProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_hour_of_week(self):
        self.assertEqual(loadprofile.how(dt.datetime(2026, 11, 16, 0, 30)), 0)            # Monday 00:30
        self.assertEqual(loadprofile.how(dt.datetime(2026, 11, 22, 23, 59)), 167)         # Sunday 23:59

    def test_weekly_pattern_is_learned_and_chosen(self):
        doc = loadprofile.build(self.stack, TZN, NOW, history(70, weekly))
        self.assertEqual(doc["use"], "weekly", doc["why"])
        self.assertLess(doc["mae_weekly_kwh"], doc["mae_flat_kwh"])
        self.assertGreaterEqual(doc["tested_days"], loadprofile.MIN_TESTS)
        p = doc["profile_w"]
        self.assertEqual((p[0 * 24 + 19], p[0 * 24 + 3], p[5 * 24 + 19]), (2500, 600, 400))   # Mon 7 PM, Mon 3 AM, Sat 7 PM
        self.assertAlmostEqual(doc["level"], 1.0, places=2)
        fn, method = loadprofile.predictor(self.stack, TZN, 900, NOW)
        self.assertEqual(method, "weekly")
        self.assertAlmostEqual(fn(dt.datetime(2026, 11, 20, 19, 0, tzinfo=TZ)), 2.5)        # Friday evening
        self.assertAlmostEqual(fn(dt.datetime(2026, 11, 21, 19, 0, tzinfo=TZ)), 0.4)        # Saturday evening
        # Saturday's expected use: 24 h at 0.4 kW
        sat = dt.datetime(2026, 11, 21, 0, 0, tzinfo=TZ)
        self.assertAlmostEqual(loadprofile.energy_kwh(fn, sat, sat + dt.timedelta(days=1)), 9.6, places=1)

    def test_level_follows_the_last_three_days(self):
        end = NOW // 3600 * 3600
        busy = history(70, lambda t: weekly(t))
        for ts in busy:
            if ts > end - 72 * 3600:
                busy[ts] *= 1.3                                                             # guests since Tuesday
        doc = loadprofile.build(self.stack, TZN, NOW, busy)
        self.assertAlmostEqual(doc["level"], 1.3, delta=0.08)
        lots = {ts: w * 5 if ts > end - 72 * 3600 else w for ts, w in history(70, weekly).items()}
        self.assertEqual(loadprofile.build(self.stack, TZN, NOW, lots)["level"], 1.6)      # limited

    def test_no_pattern_stays_flat(self):
        rnd = random.Random(7)
        doc = loadprofile.build(self.stack, TZN, NOW, history(70, lambda t: 800 + rnd.uniform(-300, 300)))
        # noise has no weekly shape: whichever wins, the choice follows the measured errors
        self.assertEqual(doc["use"], "weekly" if doc["mae_weekly_kwh"] < doc["mae_flat_kwh"] else "flat")
        steady = loadprofile.build(self.stack, TZN, NOW, history(70, lambda t: 800.0))
        self.assertEqual(steady["use"], "flat")                                            # no better: keep it simple
        self.assertIn("no better", steady["why"])

    def test_too_little_history(self):
        doc = loadprofile.build(self.stack, TZN, NOW, history(9, weekly))
        self.assertEqual(doc["use"], "flat")
        self.assertIn("days of history", doc["why"])
        self.assertEqual(loadprofile.build(self.stack, TZN, NOW, {})["why"], "no load history yet")
        fn, method = loadprofile.predictor(self.stack, TZN, 900, NOW)
        self.assertEqual(method, "flat")
        self.assertEqual(fn(dt.datetime(2026, 11, 20, 19, 0, tzinfo=TZ)), 0.9)
        self.assertEqual(loadprofile.predictor(self.stack, TZN, None, NOW), (None, "none"))

    def test_predictor_ignores_an_old_or_foreign_profile(self):
        loadprofile.build(self.stack, TZN, NOW, history(70, weekly))
        self.assertEqual(loadprofile.predictor(self.stack, TZN, 900, NOW + 4 * 86400)[1], "flat")     # the collector stopped
        self.assertEqual(loadprofile.predictor(self.stack, "America/New_York", 900, NOW)[1], "flat")  # the site moved
        with open(loadprofile.path(self.stack), "w") as f:
            json.dump({"use": "weekly", "profile_w": [1, 2, 3], "built": NOW, "time_zone": TZN}, f)
        self.assertEqual(loadprofile.predictor(self.stack, TZN, 900, NOW)[1], "flat")                 # a broken file

    def test_generator_advice_uses_the_expected_load(self):
        site = {"latitude": "39.74", "longitude": "-104.99", "battery_count": "6", "battery_kwh_each": "5.12",
                "low_soc_alert": "25", "generator": "manual"}
        fri = dt.datetime(2026, 11, 20, 15, 0, tzinfo=TZ)
        vals = {"soc": 60, "load_avg_3d": 1000}
        flat = display.generator_advice(site, vals, fri, TZ)
        # 60% -> 25% of 30.72 kWh = 10.75 kWh. Flat 1 kW lasts 10.75 h (to about 01:45): generator needed.
        self.assertEqual(flat["level"], "warn")
        quiet = display.generator_advice(site, vals, fri, TZ, load=lambda t: 0.4)       # a quiet night: it lasts
        self.assertEqual(quiet["level"], "ok")
        busy = display.generator_advice(site, vals, fri, TZ, load=lambda t: 2.5 if 17 <= t.hour < 22 else 1.0)
        self.assertEqual(busy["level"], "warn")
        self.assertGreater(busy["need_kwh"], flat["need_kwh"])                           # the evening peak counts
        # when it runs out: 2 h at 1 kW, then 2.5 kW from 5 PM: 8.75 kWh left / 2.5 = 3.5 h -> 8:30 PM
        out = display.drained_at(lambda t: 2.5 if 17 <= t.hour < 22 else 1.0, fri, 10.75)
        self.assertEqual((out.hour, out.minute), (20, 30))
        self.assertIsNone(display.drained_at(lambda t: 0.0, fri, 1.0))


if __name__ == "__main__":
    unittest.main()
