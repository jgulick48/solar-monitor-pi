"""Tests for the wall display's weather (wx.py) and the station readers' extra fields (weather.py).
Run: python3 -m unittest discover -s tests -v"""
import datetime as dt
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import weather  # noqa: E402
import wx  # noqa: E402

NOW = dt.datetime(2026, 11, 21, 16, 20)


def forecast(us=True):
    hours = [f"2026-11-21T{h:02d}:00" for h in range(14, 24)] + [f"2026-11-22T{h:02d}:00" for h in range(0, 12)]
    return {"fetched": int(time.time()), "units_us": us,
            "current": {"time": "2026-11-21T16:15", "temperature_2m": 43.2, "apparent_temperature": 36.1,
                        "relative_humidity_2m": 46, "is_day": 1, "weather_code": 3, "wind_speed_10m": 4.4,
                        "wind_direction_10m": 166, "wind_gusts_10m": 6.3, "pressure_msl": 1014.6},
            "hourly": {"time": hours, "temperature_2m": [40 - i for i in range(len(hours))],
                       "precipitation_probability": [0] * len(hours), "weather_code": [0] * len(hours),
                       "is_day": [1, 1, 1, 0] + [0] * (len(hours) - 4), "wind_speed_10m": [5] * len(hours),
                       "wind_gusts_10m": [9] * len(hours), "wind_direction_10m": [270] * len(hours)},
            "daily": {"time": ["2026-11-21", "2026-11-22", "2026-11-23"], "weather_code": [71, 3, 95],
                      "temperature_2m_max": [45.4, 46, 44], "temperature_2m_min": [26, 23, 25],
                      "precipitation_probability_max": [14, 0, 60], "precipitation_sum": [0.1, 0, 0.4],
                      "wind_speed_10m_max": [10, 7, 20], "wind_gusts_10m_max": [14, 10, 35],
                      "wind_direction_10m_dominant": [225, 0, 90], "uv_index_max": [2.75, 3, 1]}}


class View(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))
        self.site = {"units": "us_customary", "clock": ""}

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name, obj):
        with open(os.path.join(self.stack, "state", name), "w") as f:
            json.dump(obj, f)

    def test_forecast_only(self):
        self.assertIsNone(wx.view(self.stack, self.site))
        self.put("wx-forecast.json", forecast())
        v = wx.view(self.stack, self.site)
        n = v["now"]
        self.assertEqual((n["temp"], n["feels"], n["wind"], n["gust"], n["compass"], n["text"], n["source"]),
                         (43, 36, 4, 6, "SSE", "Cloudy", "forecast"))
        self.assertEqual(n["pressure"], 29.96)                              # hPa -> inHg in US units
        self.assertEqual(v["hours"][0]["t"], "2026-11-21T17:00")           # starts after the current hour
        self.assertEqual((v["hours"][0]["label"], v["hours"][0]["wind"], v["hours"][0]["compass"], v["hours"][0]["deg"]),
                         ("5 PM", 5, "W", 270))
        self.assertEqual(len(v["hours"]), 12)
        d = v["days"]
        self.assertEqual([x["name"] for x in d], ["Today", "Sun", "Mon"])
        self.assertEqual((d[2]["icon"], d[2]["gust"], d[2]["compass"], d[2]["pop"]), ("thunder", 35, "E", 60))
        self.assertEqual(v["units"]["wind"], "mph")
        # 24-hour clock, and a forecast in the other units is ignored until the next fetch
        self.assertEqual(wx.view(self.stack, {**self.site, "clock": "24"})["hours"][0]["label"], "17:00")
        self.assertIsNone(wx.view(self.stack, {"units": "metric"}))

    def test_station_wins_while_fresh(self):
        self.put("wx-forecast.json", forecast())
        self.put("wx-station.json", {"at": int(time.time()), "source": "ecowitt", "temp_c": 6.1, "wind_ms": 3.1,
                                     "gust_ms": 6.7, "wind_dir": 250, "rain_day_mm": 1.3, "humidity": 52})
        n = wx.view(self.stack, self.site)["now"]
        self.assertEqual((n["temp"], n["wind"], n["gust"], n["compass"], n["rain_day"], n["humidity"], n["source"]),
                         (43, 7, 15, "WSW", 0.05, 52, "station"))
        self.assertEqual(n["text"], "Cloudy")                               # conditions still from the forecast
        self.put("wx-station.json", {"at": int(time.time()) - 900, "temp_c": 30})
        self.assertEqual(wx.view(self.stack, self.site)["now"]["source"], "forecast")
        # a station alone (no forecast yet) still gives "now"
        os.remove(os.path.join(self.stack, "state", "wx-forecast.json"))
        self.put("wx-station.json", {"at": int(time.time()), "temp_c": 20, "wind_ms": 2})
        v = wx.view(self.stack, {"units": "metric"})
        self.assertEqual((v["now"]["temp"], v["now"]["wind"], v["hours"], v["days"]), (20, 7, [], []))

    def test_alerts(self):
        self.put("wx-forecast.json", forecast())
        self.put("wx-alerts.json", {"fetched": int(time.time()), "alerts": [{"event": "High Wind Warning", "severity": "Severe"}]})
        self.assertEqual(wx.view(self.stack, self.site)["alerts"][0]["event"], "High Wind Warning")
        self.put("wx-alerts.json", {"fetched": int(time.time()) - 4 * 3600, "alerts": [{"event": "Old"}]})
        self.assertEqual(wx.view(self.stack, self.site)["alerts"], [])     # stale: not shown


class Stations(unittest.TestCase):
    def test_ecowitt(self):
        d = {"common_list": [{"id": "0x02", "val": "45.3", "unit": "F"}, {"id": "0x07", "val": "55%"},
                             {"id": "3", "val": "44.6", "unit": "F"}, {"id": "0x0B", "val": "3.36 mph"},
                             {"id": "0x0C", "val": "18.0 km/h"}, {"id": "0x0A", "val": "227"}, {"id": "0x15", "val": "120.5 W/m2"},
                             {"id": "0x17", "val": "2"}],
             "rain": [{"id": "0x0E", "val": "0.10 in/Hr"}, {"id": "0x10", "val": "0.25 in"}],
             "piezoRain": [{"id": "0x10", "val": "4.0 mm"}],
             "wh25": [{"intemp": "70.1", "unit": "F", "inhumi": "38%", "abs": "24.85 inHg", "rel": "29.92 inHg"}]}
        r = weather.ecowitt_fields(d)
        self.assertEqual((r["temp_c"], r["feels_c"], r["humidity"], r["wind_ms"], r["gust_ms"], r["wind_dir"]),
                         (7.39, 7.0, 55.0, 1.5, 5.0, 227.0))
        self.assertEqual((r["rain_day_mm"], r["rain_rate_mmh"], r["pressure_hpa"], r["solar_wm2"]), (6.35, 2.54, 1013.21, 120.5))

    def test_tempest_and_push(self):
        obs = [1795300000, 0.5, 2.1, 4.0, 250, 3, 830.2, 6.0, 52, 12000, 1.2, 120, 0.05, 1]
        r = weather.tempest_fields(obs)
        self.assertEqual((r["wind_ms"], r["gust_ms"], r["wind_dir"], r["temp_c"], r["rain_rate_mmh"]), (2.1, 4.0, 250, 6.0, 3.0))
        p = weather.push_fields({"tempf": "50", "windspeedmph": "10", "windgustmph": "", "winddir": "180",
                                 "dailyrainin": "0.5", "baromrelin": "30.0"})
        self.assertEqual((p["temp_c"], p["wind_ms"], p["wind_dir"], p["rain_day_mm"], p["pressure_hpa"]), (10.0, 4.47, 180.0, 12.7, 1015.9))
        self.assertNotIn("gust_ms", p)

    def test_compass(self):
        self.assertEqual([wx.compass(d) for d in (0, 11, 12, 180, 349, 360, None)], ["N", "N", "NNE", "S", "N", "N", None])


if __name__ == "__main__":
    unittest.main()
