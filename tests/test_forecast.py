"""Tests for the solar forecast (collector.forecast_hours, display.forecast_summary) and the
forecast-aware generator advice. Run: python3 -m unittest discover -s tests -v"""
import datetime as dt
import json
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import collector  # noqa: E402
import display  # noqa: E402
from test_display import DENVER, SITE  # noqa: E402

START = dt.datetime(2026, 11, 21, 0, 0, tzinfo=DENVER)


def fake_forecast(peaks, now):
    """Hourly kW for 3 local days, a sine bump 07:00-17:00 with the given peak per day."""
    hours = []
    for i in range(72):
        end = START + dt.timedelta(hours=i + 1)
        h = (end - dt.timedelta(minutes=30)).hour + 0.5
        kw = peaks[i // 24] * max(0.0, math.sin(math.pi * (h - 7) / 10))
        hours.append([int(end.timestamp()), round(kw, 3)])
    return {"generated": int(now.timestamp()), "hours": hours}


class Forecast(unittest.TestCase):
    def test_summary(self):
        now = dt.datetime(2026, 11, 21, 12, 0, tzinfo=DENVER)
        f = display.forecast_summary(fake_forecast([5, 2, 5], now), now, DENVER)
        self.assertAlmostEqual(f["today"], 5 * 6.31, delta=0.5)       # integral of the sine bump ~ peak * 10 h * 2/pi
        self.assertAlmostEqual(f["tomorrow"], 2 * 6.31, delta=0.3)
        self.assertAlmostEqual(f["remaining"], f["today"] / 2, delta=0.5)
        self.assertIsNone(display.forecast_summary(None, now, DENVER))

    def test_advice_sunny_vs_dull_tomorrow(self):
        eve = dt.datetime(2026, 11, 21, 15, 0, tzinfo=DENVER)
        vals = {"soc": 70, "load_avg_3d": 800}           # 13.8 kWh above 25% (+1.4 from today's last sun): lasts the night
        sunny = display.generator_advice(SITE, vals, eve, DENVER, fake_forecast([5, 5, 5], eve))
        self.assertEqual(sunny["level"], "ok", sunny["text"])
        self.assertIn("Tomorrow: about 32 kWh", sunny["text"])
        # today's last sun (07:00-17:00 bump) adds a little before the night's ~13 h at 800 W (~10.4 kWh, 34%)
        self.assertTrue(37 <= sunny["sunrise_soc"] <= 42, sunny["sunrise_soc"])
        self.assertIn(f"about {sunny['sunrise_soc']}% at sunrise", sunny["text"])
        dull = display.generator_advice(SITE, vals, eve, DENVER, fake_forecast([5, 0.3, 5], eve))
        self.assertEqual(dull["level"], "warn", dull["text"])
        self.assertIn("tomorrow looks dull", dull["text"])
        self.assertTrue(dull["empty_at"].endswith("tomorrow"))
        self.assertGreater(dull["need_kwh"], 0.5)
        # running out overnight is still the first message, whatever tomorrow brings
        low = display.generator_advice(SITE, {"soc": 45, "load_avg_3d": 800}, eve, DENVER, fake_forecast([5, 5, 5], eve))
        self.assertEqual(low["level"], "warn")
        self.assertIn("before the sun is back", low["text"])
        # no forecast: the old rule (solar takes over 2 h after sunrise)
        self.assertEqual(display.generator_advice(SITE, {"soc": 75, "load_avg_3d": 800}, eve, DENVER)["level"], "ok")

    def test_before_dawn_says_now(self):
        night = dt.datetime(2026, 11, 22, 3, 0, tzinfo=DENVER)
        a = display.generator_advice(SITE, {"soc": 28, "load_avg_3d": 800}, night, DENVER)
        self.assertEqual(a["level"], "warn")
        self.assertIn("Start the generator now", a["text"])

    def test_stale_forecast_ignored(self):
        stack = tempfile.mkdtemp()
        os.makedirs(os.path.join(stack, "state"))
        now = dt.datetime(2026, 11, 21, 12, 0, tzinfo=DENVER)
        with open(os.path.join(stack, "state", "forecast.json"), "w") as f:
            json.dump(fake_forecast([5, 5, 5], now - dt.timedelta(hours=7)), f)
        self.assertIsNone(display.load_forecast(stack, now.timestamp()))
        self.assertIsNotNone(display.load_forecast(stack, now.timestamp() - 3600))

    def test_collector_hours_and_calibration(self):
        site = dict(SITE, inverter_model="6000xp",
                    arrays=[{"panels": "20", "panel_watts": "400", "tilt": "30", "azimuth": "180"},
                            {"panels": "10", "panel_watts": "400", "tilt": "30", "azimuth": "180"}])
        t = 1795000000
        gti = [{t: 1000.0, t + 3600: 0.0}, {t: 1000.0}]  # array 2 has no t+3600: that hour is skipped
        out = collector.forecast_hours(site, gti, {t: 25.0}, 0.8, 1)
        self.assertEqual(len(out), 1)
        # 12 kWp * 0.8 * temperature loss (cell 50 C: -8.75%) = 8.76 kW, capped at one 6000XP's 8 kW PV input
        self.assertEqual(out[0], [t, 8.0])
        self.assertEqual(collector.forecast_hours(site, gti, {t: 25.0}, 0.8, 2)[0][1], 8.76)
        stack = tempfile.mkdtemp()
        os.makedirs(os.path.join(stack, "state"))
        self.assertEqual(collector.calibration(stack), (0.8, "default"))
        with open(os.path.join(stack, "state", "advisor.json"), "w") as f:
            json.dump({"source": "Open-Meteo", "data": {"calibration": 0.71}}, f)
        self.assertEqual(collector.calibration(stack), (0.71, "calibrated"))
        with open(os.path.join(stack, "state", "advisor.json"), "w") as f:
            json.dump({"source": "weather station", "data": {"calibration": 0.71}}, f)
        self.assertEqual(collector.calibration(stack), (0.8, "default"))      # a station's calibration doesn't fit
        with open(os.path.join(stack, "state", "advisor.json"), "w") as f:
            json.dump({"source": "weather station", "data": {"calibration": 0.71, "calibration_openmeteo": 0.66}}, f)
        self.assertEqual(collector.calibration(stack), (0.66, "calibrated"))   # with a station: Open-Meteo's own


if __name__ == "__main__":
    unittest.main()


class BatteryHigh(unittest.TestCase):
    """display.battery_high: today's estimated highest battery level (SITE: 30 kWh battery)."""

    def high(self, soc, peaks, hour, load=800, so_far=None, minute=0):
        now = dt.datetime(2026, 11, 21, hour, minute, tzinfo=DENVER)
        return display.battery_high(SITE, {"soc": soc, "load_avg_3d": load}, now, DENVER, fake_forecast(peaks, now), so_far)

    def test_sunny_morning_fills(self):
        h = self.high(40, [8, 8, 8], 8)
        self.assertEqual(h["pct"], 100)
        self.assertTrue(h["text"].startswith("Full about\u00a0"))
        self.assertIsNotNone(h["full_at"])

    def test_dull_day_peaks_below_full(self):
        h = self.high(40, [1.5, 5, 5], 8)
        self.assertLess(h["pct"], 100)
        self.assertGreater(h["pct"], 40)
        self.assertIn(" at\u00a0", h["text"])
        self.assertIsNone(h["full_at"])
        # surplus: ~1.5 kW * 6.31 h-equivalent minus 0.8 kW while solar beats use, 95% stored -> a modest rise
        self.assertLess(h["pct"], 70)

    def test_falling_all_day(self):
        h = self.high(60, [0.3, 5, 5], 9)
        self.assertEqual(h["text"], "Not expected to charge higher today")
        self.assertIsNone(h["at"])

    def test_high_already_reached(self):
        h = self.high(85, [5, 5, 5], 15, so_far=100)
        self.assertEqual(h, {"pct": 100, "at": None, "full_at": None, "text": "High today 100%"})

    def test_none_after_sunset_or_without_data(self):
        self.assertIsNone(self.high(80, [5, 5, 5], 18))
        now = dt.datetime(2026, 11, 21, 9, 0, tzinfo=DENVER)
        self.assertIsNone(display.battery_high(SITE, {"soc": 50, "load_avg_3d": 800}, now, DENVER, None))
        self.assertIsNone(display.battery_high(SITE, {"soc": 50}, now, DENVER, fake_forecast([5, 5, 5], now)))
        self.assertIsNone(display.battery_high({k: v for k, v in SITE.items() if k != "battery_count"},
                                               {"soc": 50, "load_avg_3d": 800}, now, DENVER, fake_forecast([5, 5, 5], now)))

    def test_before_dawn_counts_the_whole_day(self):
        h = self.high(35, [8, 8, 8], 5)
        self.assertEqual(h["pct"], 100)


class AdvisorWithStation(unittest.TestCase):
    """With a weather station, the advisor's analysis uses the station's sunlight, but the solar forecast scales
    Open-Meteo's forecast: build_steps keeps Open-Meteo's potential (raw_om) for that calibration."""

    def test_open_meteo_potential_kept_alongside_the_station(self):
        import advisor
        site = {"latitude": "33.4", "longitude": "-112.0", "arrays": [{"panels": "20", "panel_watts": "400", "tilt": "30", "azimuth": "180"}]}
        t0 = 1795000500                                           # 15-minute steps across a sunny midday
        ts = [t0 + i * 900 for i in range(400)]
        S = {"pv": {t: 5000.0 for t in ts}, "load": {t: 1000.0 for t in ts}, "soc": {t: 60 for t in ts},
             "chg": {}, "dis": {}, "gen": {}, "bms_w": {}, "temp": {t: 20.0 for t in ts},
             "ghi": {t: 800.0 for t in ts},                       # the station: enough readings to be used
             "gti": {"1": {(t + 3599) // 3600 * 3600: 900.0 for t in ts}}}
        steps, source = advisor.build_steps(S, site)
        self.assertEqual(source, "weather station")
        self.assertTrue(all(s["raw_om"] is not None for s in steps))
        # Open-Meteo's potential: 8 kWp x 900 W/m2 with the temperature loss, the same for every step
        self.assertAlmostEqual(steps[0]["raw_om"], 8 * 0.9 * (1 + advisor.GAMMA * (20 + 900 / 800 * 20 - 25)), places=6)
        k_om, n = advisor.calibrate(steps, 8.0, "raw_om")
        self.assertEqual(n, len(steps))
        self.assertAlmostEqual(k_om, 5.0 / steps[0]["raw_om"], places=6)


class SiteChange(unittest.TestCase):
    def test_a_new_location_fetches_again(self):
        stack = tempfile.mkdtemp()
        os.makedirs(os.path.join(stack, "state"))
        seen = {}
        self.assertFalse(collector.site_changed(stack, seen))           # the collector starting: no site yet
        self.assertFalse(collector.site_changed(stack, seen))           # nothing changed
        with open(os.path.join(stack, "state", "site.json"), "w") as f:
            json.dump({"latitude": "42.9", "longitude": "-106.3"}, f)
        self.assertTrue(collector.site_changed(stack, seen))            # the location entered: fetch now
        self.assertFalse(collector.site_changed(stack, seen))
        p = os.path.join(stack, "state", "site.json")
        os.utime(p, ns=(os.stat(p).st_atime_ns, os.stat(p).st_mtime_ns + 1_000_000))
        self.assertTrue(collector.site_changed(stack, seen))            # saved again (e.g. new arrays)
