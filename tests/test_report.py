"""Tests for the monthly report (setup/report.py): last month's figures from the stored days, sent once at the
start of a month. Run: python3 -m unittest discover -s tests -v"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import history  # noqa: E402
import report  # noqa: E402

TZ = "America/Denver"
DENVER = ZoneInfo(TZ)
SITE = {"time_zone": TZ, "units": "us_customary", "generator_kw": "7", "generator_fuel": "propane",
        "costs": {"fuel_per_gal": "3.00"}}


def at(y, m, d, h=12):
    return dt.datetime(y, m, d, h, tzinfo=DENVER).timestamp()


def put(path, doc):
    with open(path, "w") as f:
        json.dump(doc, f)


def days(first, last, value):
    out, d = {}, first
    while d <= last:
        out[d] = value(d) if callable(value) else value
        d += dt.timedelta(days=1)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))
        put(os.path.join(self.stack, "state", "site.json"), SITE)
        self.series = {
            "site_day_solar_kwh": {**days(dt.date(2025, 10, 1), dt.date(2025, 10, 31), 20.0),
                                   **days(dt.date(2026, 9, 1), dt.date(2026, 11, 10), 22.0)},
            "site_day_used_kwh": days(dt.date(2026, 10, 3), dt.date(2026, 11, 10), 10.0),
            "site_day_low_soc": days(dt.date(2026, 10, 1), dt.date(2026, 10, 31), lambda d: 23.0 if d.day == 14 else 55.0),
            "site_day_generator_hours": days(dt.date(2026, 10, 1), dt.date(2026, 10, 31), lambda d: 2.0 if d.day == 14 else 0.0),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def fetch(self, series, tz, now=None, months=25):
        return {d: v for d, v in self.series.get(series, {}).items() if dt.datetime.combine(d, dt.time(12), tz).timestamp() <= now}

    def build(self, y=2026, m=10, runs=()):
        return report.build(self.stack, SITE, y, m, fetch=self.fetch, runs=list(runs))

    def stored(self, date):
        put(os.path.join(self.stack, "state", "vm-days.json"), {history.KEY: date})


class Build(Base):
    def test_month_figures(self):
        by = {r["id"]: r for r in self.build()["rows"]}
        self.assertEqual(by["solar"]["value"], 31 * 22.0)
        self.assertEqual(by["solar"]["before"], 31 * 20.0)
        self.assertEqual(by["solar"]["change"], 10)
        self.assertEqual((by["used"]["value"], by["used"]["days"], by["used"]["of"]), (290.0, 29, 31))
        self.assertIsNone(by["used"]["before"])
        self.assertEqual(by["low_soc"]["value"], 23.0)             # the lowest day, not a sum
        self.assertEqual(by["generator"]["value"], 2.0)
        self.assertNotIn("propane", by)

    def test_only_that_month(self):
        self.assertEqual({r["id"]: r["value"] for r in self.build(2026, 9)["rows"]}, {"solar": 30 * 22.0})
        self.assertEqual(self.build(2026, 8)["rows"], [])

    def test_generator_fuel_from_the_months_runs(self):
        runs = [{"kind": "inverter", "start": at(2026, 10, 14, 6), "end": at(2026, 10, 14, 8), "hours": 2.0, "kwh": 7.0, "peak_kw": 4.0},
                {"kind": "inverter", "start": at(2026, 11, 2, 6), "end": at(2026, 11, 2, 7), "hours": 1.0, "kwh": 3.0, "peak_kw": 4.0},
                {"kind": "inverter", "start": at(2026, 9, 30, 23), "end": at(2026, 10, 1, 1), "hours": 2.0, "kwh": 7.0, "peak_kw": 4.0}]
        g = self.build(runs=runs)["generator"]
        self.assertEqual((g["runs"], g["hours"]), (1, 2.0))        # a run belongs to the month it started in
        self.assertGreater(g["fuel"], 0)
        self.assertAlmostEqual(g["cost"], g["fuel"] * 3.0, delta=0.2)
        self.assertIsNone(self.build()["generator"])


class Text(Base):
    def test_message(self):
        rep = self.build()
        rep.update(battery={"level": "ok", "notes": []}, backup={"last_ok": at(2026, 11, 1, 3), "snapshots": 12, "result": "ok"})
        t = report.text(rep, DENVER)
        self.assertEqual(report.title(rep), "Solar report for October 2026")
        self.assertIn("Solar 682 kWh (10% more than a year earlier)", t)
        self.assertIn("used 290 kWh,", t)                          # nothing a year earlier: no comparison
        self.assertIn("lowest battery 23%", t)
        self.assertIn("Generator: 2.0 h.", t)
        self.assertIn("Battery health: good.", t)
        self.assertIn("Last backup: November 1 (12 on the stick).", t)
        self.assertNotIn("days have data", t)                      # solar has every day

    def test_missing_days_generator_and_warnings(self):
        del self.series["site_day_solar_kwh"]
        self.series["site_day_generator_hours"] = days(dt.date(2026, 10, 1), dt.date(2026, 10, 31), 0.0)
        self.series["site_day_generator_hours"][dt.date(2026, 3, 1)] = 1.0     # it has run, but not this month
        rep = self.build()
        rep.update(battery={"level": "warn", "notes": ["State of health 78%."]}, backup={"last_ok": None})
        t = report.text(rep, DENVER)
        self.assertIn("29 of the month's 31 days have data.", t)
        self.assertIn("The generator didn't run.", t)
        self.assertIn("Battery health needs attention: State of health 78%.", t)
        self.assertIn("Backups: none has succeeded yet", t)

    def test_no_generator_line_for_a_site_without_one(self):
        del self.series["site_day_generator_hours"]
        self.assertNotIn("enerator", report.text(self.build(), DENVER))


class Step(Base):
    def run_step(self, now):
        self.sent = getattr(self, "sent", [])
        return report.step(self.stack, now=now, alert=lambda *a: self.sent.append(a) or "",
                           make=lambda stack, site, y, m: self.build(y, m), snap=lambda *a: {"battery": None, "backup": None})

    def test_waits_for_the_days_then_sends_once(self):
        self.stored("2026-10-30")
        self.assertEqual(self.run_step(at(2026, 11, 1, 9)), [])                # the 31st isn't stored yet
        self.stored("2026-10-31")
        self.assertEqual(self.run_step(at(2026, 11, 1, 5)), [])                # not before 8:00
        self.assertIn("sent", self.run_step(at(2026, 11, 1, 8))[-1])
        tag, title, message, priority = self.sent[0]
        self.assertEqual((tag, title, priority), ("monthly_report", "Solar report for October 2026", "info"))
        self.assertEqual(self.run_step(at(2026, 11, 1, 9)), [])
        self.assertEqual(len(self.sent), 1)
        rep = report.load(self.stack)["reports"]["2026-10"]
        self.assertTrue(rep["sent"] and rep["made"])

    def test_late_in_the_month_it_is_made_but_not_sent(self):
        self.stored("2026-11-09")
        self.assertEqual(self.run_step(at(2026, 11, 10, 3)), ["report: October 2026 made"])
        self.assertEqual(self.sent, [])
        self.assertNotIn("sent", report.load(self.stack)["reports"]["2026-10"])

    def test_turned_off(self):
        put(os.path.join(self.stack, "state", "site.json"), {**SITE, "report_enabled": False})
        self.stored("2026-10-31")
        self.run_step(at(2026, 11, 1, 9))
        self.assertEqual(self.sent, [])
        self.assertIn("2026-10", report.load(self.stack)["reports"])

    def test_a_failed_send_is_tried_again(self):
        self.stored("2026-10-31")
        out = report.step(self.stack, now=at(2026, 11, 1, 9), alert=lambda *a: "connection refused",
                          make=lambda stack, site, y, m: self.build(y, m), snap=lambda *a: {})
        self.assertIn("not sent", out[0])
        self.assertIn("sent", self.run_step(at(2026, 11, 1, 10))[-1])

    def test_a_month_without_data_is_skipped(self):
        self.series = {}
        self.stored("2026-10-31")
        self.assertEqual(self.run_step(at(2026, 11, 1, 9)), [])
        self.assertEqual(self.sent, [])
        self.assertEqual(report.load(self.stack)["done"], "2026-10")

    def test_january_reports_december(self):
        self.series["site_day_solar_kwh"].update(days(dt.date(2026, 12, 1), dt.date(2026, 12, 31), 5.0))
        self.stored("2026-12-31")
        self.run_step(at(2027, 1, 1, 9))
        self.assertEqual(self.sent[0][1], "Solar report for December 2026")


class Page(Base):
    def test_stored_and_computed(self):
        import unittest.mock as mock
        now = at(2026, 11, 10)
        real = report.build
        with mock.patch.object(report, "build", lambda stack, site, y, m: real(stack, site, y, m, fetch=self.fetch, runs=[])):
            page = report.render(self.stack, SITE, None, now=now)
            self.assertIn("Report for October 2026", page)
            self.assertIn("Worked out just now", page)
            self.assertIn("href='/report?m=2026-09'", page)
            self.assertIn("29 of 31 days have data", page)
            self.assertIn("Report for September 2026", report.render(self.stack, SITE, "2026-09", now=now))
            self.assertIn("Report for October 2026", report.render(self.stack, SITE, "2020-01", now=now))   # not offered
            self.assertIn("no data from this month", report.render(self.stack, SITE, "2026-08", now=now))
        rep = self.build()
        rep.update(made=int(at(2026, 11, 1, 8)), sent=int(at(2026, 11, 1, 8)), battery={"level": "ok", "notes": []}, backup=None)
        put(os.path.join(self.stack, "state", "report.json"), {"reports": {"2026-10": rep}})
        page = report.render(self.stack, SITE, None, now=now)
        self.assertIn("Made on November 1, 2026, and sent", page)
        self.assertIn("Battery health: good.", page)


if __name__ == "__main__":
    unittest.main()
