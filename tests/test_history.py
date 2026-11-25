"""Tests for the History page (setup/history.py): finished days stored once, months against a year earlier.
Run: python3 -m unittest discover -s tests -v"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import history  # noqa: E402
import modules  # noqa: E402

TZ = "America/Denver"
DENVER = ZoneInfo(TZ)


def at(y, m, d, h=12):
    return dt.datetime(y, m, d, h, tzinfo=DENVER).timestamp()


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))
        self.sent, self.asked = [], []

    def tearDown(self):
        self.tmp.cleanup()

    def stats(self, start, end):
        self.asked.append((start, end))
        day = dt.datetime.fromtimestamp(start, DENVER).date()
        if day == dt.date(2026, 11, 2):                                   # the box was off that day
            return {"solar": None, "used": None, "gen_kwh": None, "low_soc": None, "gen_hours": None}
        return {"solar": 20.0 + day.day, "used": 15.5, "gen_kwh": 0.0, "low_soc": 41.0, "gen_hours": None}

    def weather(self, start, end):
        return {"temp_high": 12.0, "temp_low": -3.0, "temp_avg": 4.0, "hdd": 25.8, "wind_avg": 3.5, "gust_max": None}

    def push(self, now, **kw):
        return history.push_site_days(self.stack, TZ, now, stats=self.stats, first=lambda now: at(2026, 10, 30, 15),
                                      send=self.sent.extend, weather=self.weather, **kw)

    def test_days_stored_once_from_the_first_data(self):
        self.assertEqual(self.push(at(2026, 11, 4, 9)), 5)               # Oct 30 to Nov 3; today isn't finished
        # whole local days, also across the end of daylight saving time (Nov 1 has 25 hours)
        self.assertEqual([round((e + 1 - s) / 3600) for s, e in self.asked], [24, 24, 25, 24, 24])
        first = self.sent[0].split()
        self.assertEqual((first[0], first[1]), ("site_day_solar_kwh", "50.000"))
        self.assertEqual(int(first[2]), int(at(2026, 10, 30, 12) * 1000))  # stamped at the day's local noon
        names = {line.split()[0] for line in self.sent}
        self.assertEqual(names, {"site_day_solar_kwh", "site_day_used_kwh", "site_day_generator_kwh", "site_day_low_soc",
                                 "site_day_generator_hours",              # no generator reading: 0 hours that day
                                 "site_day_temp_high_c", "site_day_temp_low_c", "site_day_temp_avg_c", "site_day_hdd",
                                 "site_day_wind_avg_ms"})                 # no gust figure: left out
        # the weather is stored also for the day the inverters gave nothing
        self.assertEqual(len([x for x in self.sent if x.startswith("site_day_temp_avg_c")]), 5)
        self.assertEqual(len([x for x in self.sent if x.startswith("site_day_solar_kwh")]), 4)   # nothing for Nov 2
        self.assertEqual(self.push(at(2026, 11, 4, 10)), 0)              # nothing new an hour later
        self.assertEqual(self.push(at(2026, 11, 5, 1)), 1)
        self.assertEqual(len(self.asked), 6)

    def test_backfill_in_steps_and_nothing_without_data(self):
        self.assertEqual(self.push(at(2026, 11, 20), limit=10), 10)
        self.assertEqual(self.push(at(2026, 11, 20), limit=10), 10)
        self.assertEqual(self.push(at(2026, 11, 20), limit=10), 1)       # Oct 30 to Nov 19: 21 days
        other = os.path.join(self.tmp.name, "new")
        os.makedirs(os.path.join(other, "state"))
        self.assertEqual(history.push_site_days(other, TZ, at(2026, 11, 20), stats=self.stats, first=lambda now: None, send=self.sent.extend,
                                                weather=self.weather), 0)

    def test_failed_send_is_tried_again(self):
        def down(lines):
            raise OSError("no")
        with self.assertRaises(OSError):
            history.push_site_days(self.stack, TZ, at(2026, 11, 4), stats=self.stats, first=lambda now: at(2026, 11, 1), send=down,
                                    weather=self.weather)
        self.assertEqual(history.push_site_days(self.stack, TZ, at(2026, 11, 4), stats=self.stats, first=lambda now: at(2026, 11, 1),
                                                send=self.sent.extend, weather=self.weather), 3)

    def test_weather_of_a_day(self):
        """A station's readings when the day has enough of them, else Open-Meteo's; degree days from the average."""
        def vm(values):
            def query(q, ts):
                for key, v in values.items():
                    if q.startswith(key):
                        return [] if v is None else [{"value": [ts, str(v)]}]
                return []
            return query
        om = {"max_over_time(site_openmeteo_temp_c": 10.0, "min_over_time(site_openmeteo_temp_c": -2.0,
              "avg_over_time(site_openmeteo_temp_c": 5.0, "avg_over_time(site_openmeteo_wind_ms": 4.0,
              "max_over_time(site_openmeteo_gust_ms": 15.0}
        w = history.weather_day_stats(0, 86399, vm(om))
        self.assertEqual((w["temp_high"], w["temp_low"], w["temp_avg"], w["wind_avg"], w["gust_max"]), (10.0, -2.0, 5.0, 4.0, 15.0))
        self.assertAlmostEqual(w["hdd"], 65 - 41)                          # 5 °C is 41 °F
        station = {**om, "max(count_over_time(site_outdoor_temp_c": 1400, "avg(avg_over_time(site_outdoor_temp_c": 6.5,
                   "max(max_over_time(site_outdoor_temp_c": 12.0, "min(min_over_time(site_outdoor_temp_c": -1.0,
                   "max(count_over_time(site_wind_speed_ms": 50, "avg(avg_over_time(site_wind_speed_ms": 9.0}
        w = history.weather_day_stats(0, 86399, vm(station))
        self.assertEqual((w["temp_avg"], w["temp_high"], w["temp_low"]), (6.5, 12.0, -1.0))
        self.assertEqual(w["wind_avg"], 4.0)                               # the station's wind: too few readings that day
        none = history.weather_day_stats(0, 86399, vm({}))
        self.assertEqual(set(none.values()), {None})
        self.assertEqual(history.weather_lines(none), [])
        self.assertEqual(history.weather_lines({"temp_avg": 30.0, "hdd": 0.0}), ["site_day_temp_avg_c 30.00", "site_day_hdd 0.00"])

    def test_waits_for_the_weather_history(self):
        with open(os.path.join(self.stack, "state", "site.json"), "w") as f:
            json.dump({"latitude": "39.7", "time_zone": TZ}, f)
        self.assertEqual(history.collect(self.stack), [])                  # no marker yet: nothing asked, nothing stored
        self.assertFalse(os.path.exists(os.path.join(self.stack, "state", "vm-days.json")))

    def test_propane_days(self):
        with open(os.path.join(self.stack, "state", "propane.json"), "w") as f:
            json.dump({"days": [{"date": "2026-11-01", "used": 3.2}, {"date": "2026-11-02", "used": 0}]}, f)
        self.assertEqual(history.push_propane_days(self.stack, send=self.sent.extend), 2)
        self.assertEqual([x.split()[:2] for x in self.sent], [["propane_day_used_gal", "3.2"], ["propane_day_used_gal", "0"]])
        self.assertEqual(history.push_propane_days(self.stack, send=self.sent.extend), 0)


class Compare(unittest.TestCase):
    def days(self, start, end, value):
        out, d = {}, start
        while d <= end:
            out[d] = value(d) if callable(value) else value
            d += dt.timedelta(days=1)
        return out

    def test_months_against_a_year_earlier(self):
        days = {**self.days(dt.date(2025, 10, 1), dt.date(2025, 12, 31), 10.0),
                **self.days(dt.date(2026, 10, 1), dt.date(2026, 11, 24), 12.0)}
        rows = history.compare(days, dt.date(2026, 11, 25))
        now, oct_, sep = rows[0], rows[1], rows[2]
        # the month under way: the 1st to yesterday, against the same 24 days last year (not all of November)
        self.assertEqual((now["partial"], now["of"], now["value"], now["before"], now["change"]), (True, 24, 288.0, 240.0, 20))
        self.assertEqual((oct_["value"], oct_["before"], oct_["change"], oct_["days"], oct_["partial"]), (372.0, 310.0, 20, 31, False))
        self.assertEqual((sep["value"], sep["before"], sep["change"]), (None, None, None))
        self.assertEqual(len(rows), 12)
        self.assertEqual((rows[-1]["year"], rows[-1]["month"]), (2025, 12))
        self.assertEqual(rows[-1]["value"], 310.0)                         # December 2025: nothing a year before it
        self.assertIsNone(rows[-1]["before"])
        self.assertEqual(history.totals(rows), (660.0, 550.0, 20, 2))     # only the months that have both years
        self.assertIsNone(history.totals(history.compare(self.days(dt.date(2026, 11, 1), dt.date(2026, 11, 24), 1.0), dt.date(2026, 11, 25))))

    def test_averages_peaks_and_differences(self):
        days = {**self.days(dt.date(2025, 10, 1), dt.date(2025, 10, 31), lambda d: 50.0 if d.day <= 10 else 44.0),
                **self.days(dt.date(2026, 10, 1), dt.date(2026, 10, 31), 41.0)}
        avg = history.compare(days, dt.date(2026, 11, 25), agg="avg", show="diff")[1]
        self.assertAlmostEqual(avg["before"], (10 * 50 + 21 * 44) / 31)
        self.assertAlmostEqual(avg["change"], 41.0 - avg["before"])
        self.assertEqual(history._change(avg["change"], "diff", "°F", ("warmer", "colder")), "5 °F colder")
        peak = history.compare(days, dt.date(2026, 11, 25), agg="max", show="diff")[1]
        self.assertEqual((peak["value"], peak["before"], peak["change"]), (41.0, 50.0, -9.0))
        self.assertEqual(history._change(0.2, "diff", "°F"), "the same")
        self.assertEqual((history._change(12), history._change(-7), history._change(0), history._change(None)),
                         ("12% more", "7% less", "the same", ""))

    def test_missing_days_and_leap_year(self):
        days = {**self.days(dt.date(2027, 2, 1), dt.date(2027, 2, 28), 1.0), **self.days(dt.date(2028, 2, 1), dt.date(2028, 2, 20), 2.0)}
        feb = history.compare(days, dt.date(2028, 3, 5))[1]
        self.assertEqual((feb["of"], feb["days"], feb["days_before"], feb["value"], feb["before"]), (29, 20, 28, 40.0, 28.0))
        first = history.compare({}, dt.date(2028, 3, 1))[0]               # the 1st: nothing finished this month yet
        self.assertEqual((first["of"], first["value"]), (0, None))
        zero = history.compare({dt.date(2027, 1, 5): 0.0, dt.date(2028, 1, 5): 3.0}, dt.date(2028, 2, 2))[1]
        self.assertEqual((zero["before"], zero["change"]), (0.0, None))    # no percentage of nothing


class Page(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))

    def tearDown(self):
        modules._ALL = None
        self.tmp.cleanup()

    def test_build_and_render(self):
        now = at(2026, 11, 25)

        def fetch(series, tz, now):
            if series == "site_day_solar_kwh":
                return {dt.date(2025, 10, d): 10.0 for d in range(1, 32)} | {dt.date(2026, 10, d): 12.0 for d in range(1, 31)}
            if series == "site_day_used_kwh":
                return {dt.date(2026, 11, d): 20.0 for d in range(1, 25)}
            if series == "site_day_generator_hours":
                return {dt.date(2026, 11, d): 0.0 for d in range(1, 25)}  # never ran: no table of zeros
            if series == "water_day_used_gal":
                return {dt.date(2026, 11, 1): 100.0}
            return {}
        site = {"time_zone": TZ, "units": "metric"}
        data = history.build(self.stack, site, now, fetch)
        self.assertEqual([m["id"] for m in data["metrics"]], ["solar", "used"])     # no generator data; water module off
        page = history.render(data)
        self.assertIn("<h2>Solar (kWh)</h2>", page)
        self.assertIn("<td>October 2026</td><td class='n'>360</td><td class='n'>310</td><td>16% more <small>(30 of 31 days this year)</small></td>", page)
        self.assertIn("<td>November 2026 (so far)</td><td class='n'>480</td><td class='n'>&ndash;</td>", page)
        self.assertIn("1 month with both years", page)
        self.assertIn("Nothing from a year earlier yet: this box has used data since November 1, 2026.", page)
        # the weather in the site's units: averages and peaks, no total row
        def wx(series, tz, now):
            vals = {"site_day_temp_avg_c": (10.0, 5.0), "site_day_wind_avg_ms": (4.0, 5.0), "site_day_hdd": (18.0, 27.0),
                    "site_day_gust_max_ms": (20.0, 25.0)}.get(series)
            if not vals:
                return {}
            return {dt.date(2025, 10, d): vals[0] for d in range(1, 32)} | {dt.date(2026, 10, d): vals[1] for d in range(1, 32)}
        us = history.build(self.stack, {"time_zone": TZ}, now, wx)
        self.assertEqual([(m["id"], m["unit"]) for m in us["metrics"]], [("temp", "°F"), ("hdd", "°F·days"), ("wind", "mph"), ("gust", "mph")])
        page = history.render(us)
        self.assertIn("<td>October 2026</td><td class='n'>41</td><td class='n'>50</td><td>9 °F colder</td>", page)
        self.assertIn("<td>October 2026</td><td class='n'>837</td><td class='n'>558</td><td>50% more</td>", page)       # degree days
        self.assertIn("<td class='n'>11.2</td><td class='n'>8.9</td><td>25% more</td>", page)                          # wind, mph
        self.assertIn("<td class='n'>56</td><td class='n'>45</td><td>11 mph stronger</td>", page)
        self.assertEqual(page.count("month with both years"), 1)           # only the degree days are added up
        metric = history.build(self.stack, site, now, wx)["metrics"]
        self.assertEqual([m["unit"] for m in metric], ["°C", "°C·days", "km/h", "km/h"])
        d = modules.load(self.stack, "water")
        d["enabled"] = True
        with open(modules.path(self.stack, "water"), "w") as f:
            json.dump(d, f)
        water = history.build(self.stack, site, now, fetch)["metrics"][-1]
        self.assertEqual((water["id"], water["unit"], round(water["rows"][0]["value"])), ("water", "L", 379))
        self.assertIn("No finished day has been stored yet", history.render({"metrics": [], "today": None}))


if __name__ == "__main__":
    unittest.main()
