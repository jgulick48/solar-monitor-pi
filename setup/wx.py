#!/usr/bin/env python3
"""Weather for the wall display, shown with the solar figures: now, the next hours, the next days and alerts.

  * Forecast and current conditions: Open-Meteo (free, no key), fetched by solar-collector every 30 minutes into
    state/wx-forecast.json, in the site's units (US: F, mph, inches; metric: C, km/h, mm).
  * The site's own weather station, if set up (weather.py reads it every minute; pushes arrive at the start
    page): state/wx-station.json. Its readings replace the forecast's "now" while they're under 10 minutes old.
  * In the US, National Weather Service alerts for the site (api.weather.gov, every 10 minutes) into
    state/wx-alerts.json; outside the US the request just finds nothing.

view() turns these into what the page shows.
"""
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import solar_setup as ss

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
NWS = "https://api.weather.gov/alerts/active"
UA = "solar-monitor-pi (https://github.com/jgulick48/solar-monitor-pi)"
STATION_FRESH_S = 600

# WMO weather codes (Open-Meteo) -> (text, icon)
CODES = {0: ("Clear", "clear"), 1: ("Mostly clear", "clear"), 2: ("Partly cloudy", "partly"), 3: ("Cloudy", "cloud"),
         45: ("Fog", "fog"), 48: ("Freezing fog", "fog"), 51: ("Light drizzle", "rain"), 53: ("Drizzle", "rain"),
         55: ("Heavy drizzle", "rain"), 56: ("Freezing drizzle", "sleet"), 57: ("Freezing drizzle", "sleet"),
         61: ("Light rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy rain", "rain"), 66: ("Freezing rain", "sleet"),
         67: ("Freezing rain", "sleet"), 71: ("Light snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy snow", "snow"),
         77: ("Snow grains", "snow"), 80: ("Showers", "rain"), 81: ("Showers", "rain"), 82: ("Heavy showers", "rain"),
         85: ("Snow showers", "snow"), 86: ("Heavy snow showers", "snow"), 95: ("Thunderstorms", "thunder"),
         96: ("Thunderstorms, hail", "thunder"), 99: ("Thunderstorms, hail", "thunder")}
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def us(site):
    return (site or {}).get("units", "us_customary") == "us_customary"


def units(site):
    return {"temp": "°F", "wind": "mph", "rain": "in", "pressure": "inHg"} if us(site) else \
        {"temp": "°C", "wind": "km/h", "rain": "mm", "pressure": "hPa"}


def h12(site):
    c = (site or {}).get("clock") or ""
    return c == "12" or (c != "24" and us(site))


def path(stack, name):
    return os.path.join(stack, "state", name)


def _get(url, timeout=30, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# --------------------------------------------------------------------------
# Fetching (solar-collector)
# --------------------------------------------------------------------------
def fetch_forecast(stack):
    site = ss.load_json(path(stack, "site.json"), {})
    if not site.get("latitude"):
        return False
    q = {"latitude": site["latitude"], "longitude": site["longitude"], "timezone": "auto", "forecast_days": 7,
         "current": "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,weather_code,"
                    "wind_speed_10m,wind_direction_10m,wind_gusts_10m,pressure_msl",
         "hourly": "temperature_2m,precipitation_probability,weather_code,is_day,wind_speed_10m,wind_gusts_10m,wind_direction_10m",
         "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,"
                  "wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant,uv_index_max,sunrise,sunset"}
    if us(site):
        q.update(temperature_unit="fahrenheit", wind_speed_unit="mph", precipitation_unit="inch")
    d = _get(f"{OPEN_METEO}?{urllib.parse.urlencode(q)}", timeout=60)
    d["fetched"] = int(time.time())
    d["units_us"] = us(site)
    ss.write_if_changed(path(stack, "wx-forecast.json"), json.dumps(d) + "\n", 0o644)
    return True


def fetch_alerts(stack):
    """NWS alerts for the site (US only; elsewhere api.weather.gov answers 400/404: no alerts)."""
    site = ss.load_json(path(stack, "site.json"), {})
    if not site.get("latitude"):
        return 0
    out = []
    try:
        point = "%.4f,%.4f" % (float(site["latitude"]), float(site["longitude"]))
        d = _get(f"{NWS}?{urllib.parse.urlencode({'point': point})}", headers={"Accept": "application/geo+json"})
        for f in d.get("features") or []:
            p = f.get("properties") or {}
            out.append({"event": p.get("event", "Alert"), "severity": p.get("severity", ""), "headline": p.get("headline", ""),
                        "ends": p.get("ends") or p.get("expires") or ""})
    except urllib.error.HTTPError as e:
        if e.code not in (400, 404):
            raise
    ss.write_if_changed(path(stack, "wx-alerts.json"), json.dumps({"fetched": int(time.time()), "alerts": out}) + "\n", 0o644)
    return len(out)


def save_station(stack, reading, source):
    vals = {k: v for k, v in (reading or {}).items() if v is not None}
    if vals:
        ss.write_if_changed(path(stack, "wx-station.json"), json.dumps({"at": int(time.time()), "source": source, **vals}) + "\n", 0o644)


# --------------------------------------------------------------------------
# For the page
# --------------------------------------------------------------------------
def _from_metric(st, site):
    """A station reading (metric) in the site's units."""
    u = us(site)
    t = lambda c: None if c is None else round(c * 1.8 + 32, 1) if u else round(c, 1)  # noqa: E731
    w = lambda ms: None if ms is None else round(ms * (2.23694 if u else 3.6), 1)  # noqa: E731
    r = lambda mm: None if mm is None else round(mm / 25.4, 2) if u else round(mm, 1)  # noqa: E731
    p = st.get("pressure_hpa")
    return {"temp": t(st.get("temp_c")), "feels": t(st.get("feels_c")), "humidity": st.get("humidity"),
            "wind": w(st.get("wind_ms")), "gust": w(st.get("gust_ms")), "wind_dir": st.get("wind_dir"),
            "rain_day": r(st.get("rain_day_mm")), "rain_rate": r(st.get("rain_rate_mmh")), "uv": st.get("uv"),
            "pressure": None if p is None else (round(p / 33.8639, 2) if u else round(p))}


def compass(deg):
    return None if deg is None else COMPASS[int((float(deg) % 360) / 22.5 + .5) % 16]


def view(stack, site, now=None):
    """{now, hours, days, alerts, units} for the wall display, or None without any weather data."""
    fc = ss.load_json(path(stack, "wx-forecast.json"), {})
    now_ts = time.time() if now is None else now.timestamp()
    if not fc.get("current") or now_ts - fc.get("fetched", 0) > 6 * 3600 or fc.get("units_us") != us(site):
        fc = {}
    st = ss.load_json(path(stack, "wx-station.json"), {})
    fresh = st and now_ts - st.get("at", 0) < STATION_FRESH_S
    if not fc and not fresh:
        return None
    cur = fc.get("current") or {}
    code = cur.get("weather_code")
    text, icon = CODES.get(code, ("", "cloud"))
    out_now = {"temp": cur.get("temperature_2m"), "feels": cur.get("apparent_temperature"),
               "humidity": cur.get("relative_humidity_2m"), "wind": cur.get("wind_speed_10m"), "gust": cur.get("wind_gusts_10m"),
               "wind_dir": cur.get("wind_direction_10m"), "rain_day": None, "rain_rate": None, "uv": None,
               "pressure": None if cur.get("pressure_msl") is None else (round(cur["pressure_msl"] / 33.8639, 2) if us(site) else round(cur["pressure_msl"])),
               "text": text, "icon": icon, "day": bool(cur.get("is_day", 1)), "source": "forecast"}
    if fresh:
        out_now.update({k: v for k, v in _from_metric(st, site).items() if v is not None})
        out_now["source"] = "station"
    out_now["compass"] = compass(out_now.get("wind_dir"))
    for k in ("temp", "feels", "wind", "gust"):
        if out_now.get(k) is not None:
            out_now[k] = round(out_now[k])
    hours, days = [], []
    h = fc.get("hourly") or {}
    cur_hour = (cur.get("time") or "")[:13]
    for i, t in enumerate(h.get("time") or []):
        if t[:13] <= cur_hour:
            continue
        c = (h.get("weather_code") or [None])[i]
        hh = dt.datetime.fromisoformat(t)
        hours.append({"t": t, "label": hh.strftime("%-I %p") if h12(site) else hh.strftime("%H:%M"),
                      "temp": _r(h, "temperature_2m", i), "pop": _r(h, "precipitation_probability", i),
                      "wind": _r(h, "wind_speed_10m", i), "gust": _r(h, "wind_gusts_10m", i),
                      "deg": (h.get("wind_direction_10m") or [None] * (i + 1))[i],
                      "compass": compass((h.get("wind_direction_10m") or [None] * (i + 1))[i]),
                      "icon": CODES.get(c, ("", "cloud"))[1], "day": bool((h.get("is_day") or [1])[i])})
        if len(hours) == 12:
            break
    dd = fc.get("daily") or {}
    for i, t in enumerate(dd.get("time") or []):
        c = (dd.get("weather_code") or [None])[i]
        days.append({"date": t, "name": "Today" if i == 0 else dt.date.fromisoformat(t).strftime("%a"),
                     "hi": _r(dd, "temperature_2m_max", i), "lo": _r(dd, "temperature_2m_min", i),
                     "pop": _r(dd, "precipitation_probability_max", i), "rain": (dd.get("precipitation_sum") or [None])[i],
                     "wind": _r(dd, "wind_speed_10m_max", i), "gust": _r(dd, "wind_gusts_10m_max", i),
                     "deg": (dd.get("wind_direction_10m_dominant") or [None] * (i + 1))[i],
                     "compass": compass((dd.get("wind_direction_10m_dominant") or [None] * (i + 1))[i]),
                     "text": CODES.get(c, ("", ""))[0],
                     "icon": CODES.get(c, ("", "cloud"))[1]})
    if days and out_now.get("uv") is None:
        out_now["uv_max"] = (dd.get("uv_index_max") or [None])[0]
    al = ss.load_json(path(stack, "wx-alerts.json"), {})
    alerts = al.get("alerts") if now_ts - al.get("fetched", 0) < 3 * 3600 else []
    return {"now": out_now, "hours": hours, "days": days, "alerts": alerts or [], "units": units(site)}


def _r(d, k, i):
    v = (d.get(k) or [None] * (i + 1))[i]
    return None if v is None else round(v)
