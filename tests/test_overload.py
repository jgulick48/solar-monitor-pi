"""Tests for the overload warning (overload.py) and the day's highest load in the history.
Run: python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import history  # noqa: E402
import overload as ov  # noqa: E402
import vmdays  # noqa: E402


def d(recent, today=None, peak=4200.0):
    return {"recent": recent, "today": today or recent, "peak_w": peak}


TWO = {("A", "all"): 2000.0, ("A", "leg 1"): 1200.0, ("A", "leg 2"): 800.0,
       ("B", "all"): 1900.0, ("B", "leg 1"): 1100.0, ("B", "leg 2"): 800.0}


class Overload(unittest.TestCase):
    def test_fine(self):
        r = ov.evaluate(d(TWO), 6.0)
        self.assertEqual((r["level"], r["text"], r["now_pct"], r["peak_pct"], r["peak_kw"], r["limit_kw"]),
                         ("ok", "", 40, 40, 4.2, 12.0))          # leg 1 of A: 1.2 of 3 kW

    def test_one_leg_near_its_limit(self):
        # 6.5 kW of 12 in all, but one leg of one inverter carries 2.8 of its 3 kW
        r = ov.evaluate(d({**TWO, ("A", "all"): 3600.0, ("A", "leg 1"): 2800.0}), 6.0)
        self.assertEqual((r["level"], r["now_pct"]), ("warn", 93))
        self.assertIn("leg 1 of inverter A is at 93% (2.8 of 3 kW). Turn something off", r["text"])
        self.assertIn("one 120 V side", r["text"])

    def test_whole_inverter_and_single_inverter_wording(self):
        r = ov.evaluate(d({("A", "all"): 11000.0, ("A", "leg 1"): 5200.0, ("A", "leg 2"): 5300.0}), 12.0)
        self.assertEqual(r["level"], "warn")
        self.assertIn("the inverter is at 92% (11.0 of 12 kW)", r["text"])
        self.assertNotIn("120 V", r["text"])

    def test_came_close_today_is_a_note(self):
        r = ov.evaluate(d(TWO, {**TWO, ("B", "leg 2"): 2500.0}), 6.0)
        self.assertEqual((r["level"], r["now_pct"], r["peak_pct"]), ("note", 40, 83))
        self.assertIn("Today's highest load came to 83% of the limit on leg 2 of inverter B (2.5 of 3 kW).", r["text"])

    def test_no_data(self):
        r = ov.evaluate({"recent": {}, "today": {}, "peak_w": None}, 6.0)
        self.assertEqual((r["level"], r["now_pct"], r["peak_kw"]), ("unknown", None, None))
        self.assertEqual(ov.card(r), "")

    def test_card(self):
        c = ov.card(ov.evaluate(d({**TWO, ("A", "leg 1"): 2800.0}), 6.0))
        self.assertIn("class='card warn'", c)
        self.assertIn("now 93% of the limit, highest today 93% (4.2 kW in all)", c)


class PeakDays(unittest.TestCase):
    NOW = 1_795_000_000

    def run_push(self, stack, **kw):
        sent = []
        n = history.push_peak_days(stack, "UTC", now=self.NOW, peak=lambda a, b: 5300.0, first=lambda now: now - 5 * 86400,
                                   send=sent.extend, **kw)
        return n, sent

    def test_fills_days_stored_before_it(self):
        with tempfile.TemporaryDirectory() as stack:
            os.mkdir(os.path.join(stack, "state"))
            vmdays.mark(stack, history.KEY, "2026-11-17")          # the old days go up to here
            n, sent = self.run_push(stack, limit=2)
            self.assertEqual((n, len(sent)), (2, 2))
            self.assertTrue(sent[0].startswith("site_day_peak_load_kw 5.300 "))
            vmdays.mark(stack, history.KEY, "2026-11-19")          # newer days are stored with their peak already
            n, sent = self.run_push(stack)
            st = json.load(open(vmdays._path(stack)))
            self.assertEqual((st[history.PEAK_KEY], st[history.PEAK_UNTIL]), ("done", "2026-11-17"))
            self.assertEqual(self.run_push(stack), (0, []))
            self.assertGreaterEqual(n, 1)

    def test_new_box_has_nothing_to_fill(self):
        with tempfile.TemporaryDirectory() as stack:
            os.mkdir(os.path.join(stack, "state"))
            self.assertEqual(self.run_push(stack), (0, []))
            self.assertEqual(json.load(open(vmdays._path(stack)))[history.PEAK_KEY], "done")

    def test_new_days_carry_their_peak(self):
        self.assertEqual(history.site_lines({"solar": 10.0, "peak_load": 6150.0}),
                         ["site_day_solar_kwh 10.000", "site_day_peak_load_kw 6.150"])
        self.assertEqual(history.site_lines({"solar": 10.0}), ["site_day_solar_kwh 10.000"])


if __name__ == "__main__":
    unittest.main()
