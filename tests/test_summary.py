"""Tests for the daily summary (display.daily_summary) and the site package with its settings.
Run: python3 -m unittest discover -s tests -v"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import display  # noqa: E402
import solar_setup  # noqa: E402
from test_display import DENVER, SITE  # noqa: E402
from test_forecast import fake_forecast  # noqa: E402


def fake_vm_at(q, ts):
    if "lxp_e_pv_day" in q:
        return 24.2
    if "lxp_e_load_day" in q:
        return 21.04
    if "lxp_e_gen_day" in q:
        return 6.0
    if "lxp_soc" in q:
        return 38.0
    if "lxp_p_gen" in q:
        return 0.0625      # 1.5 h of a day
    return None


class Summary(unittest.TestCase):
    def setUp(self):
        self.orig = display.vm_at
        display.vm_at = fake_vm_at
        display._cache.clear()
        self.stack = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.stack, "state"))

    def tearDown(self):
        display.vm_at = self.orig

    def write(self, name, obj):
        with open(os.path.join(self.stack, "state", name), "w") as f:
            json.dump(obj, f)

    def test_midnight_reports_yesterday(self):
        now = dt.datetime(2026, 11, 21, 0, 0, 5, tzinfo=DENVER)   # Saturday 00:00
        self.write("forecast.json", fake_forecast([5, 2, 5], now))
        s = display.daily_summary(self.stack, SITE, now, DENVER)
        self.assertEqual((s["day"], s["complete"], s["title"]), ("2026-11-20", True, "Solar Fri Nov 20"))
        self.assertEqual(s["text"], "24 kWh solar, 21 kWh used, battery down to 38%, generator 1.5 h (6.0 kWh). "
                                    "Today: about 32 kWh of solar expected.")
        self.assertEqual(s["weekly"], "")

    def test_evening_reports_today_so_far(self):
        now = dt.datetime(2026, 11, 21, 20, 0, tzinfo=DENVER)
        s = display.daily_summary(self.stack, dict(SITE, generator="none"), now, DENVER)
        self.assertEqual((s["day"], s["complete"]), ("2026-11-21", False))
        self.assertTrue(s["title"].endswith("so far"))
        self.assertNotIn("generator", s["text"])
        self.assertNotIn("expected", s["text"])   # no forecast file

    def test_sunday_adds_the_week_and_advisor(self):
        self.write("advisor.json", {"recommendation": ["Biggest reduction: +2 batteries."]})
        now = dt.datetime(2026, 11, 23, 0, 0, 5, tzinfo=DENVER)   # Monday 00:00 -> Sunday
        s = display.daily_summary(self.stack, SITE, now, DENVER)
        self.assertEqual(s["day"], "2026-11-22")
        self.assertEqual(s["weekly"], "This week: 169 kWh solar, 147 kWh used, generator 10.5 h. "
                                      "Advisor: Biggest reduction: +2 batteries.")
        self.assertTrue(s["text"].endswith(s["weekly"]))

    def test_site_package(self):
        p = solar_setup.site_package({})
        self.assertIn("initial: true", p)
        self.assertIn('initial: "00:00:00"', p)
        self.assertNotIn("template:", p)          # nothing about the site until it is set up
        site = dict(SITE, summary_enabled=False, summary_time="21:30", generator_kw="",
                    arrays=[{"panels": "24", "panel_watts": "285", "tilt": "30", "azimuth": "180"}])
        p = solar_setup.site_package(site)
        self.assertIn("initial: false", p)
        self.assertIn('initial: "21:30:00"', p)
        self.assertIn('state: "30.72"', p)
        import yaml
        doc = yaml.safe_load(p)
        self.assertEqual(doc["input_datetime"]["solar_summary_time"]["initial"], "21:30:00")


if __name__ == "__main__":
    unittest.main()
