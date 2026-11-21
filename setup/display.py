#!/usr/bin/env python3
"""Wall display: the dashboard shown on a screen plugged into the Pi's HDMI port.

The screen runs Chromium in kiosk mode inside the cage compositor
(solar-display.service) on http://127.0.0.1/display, served by solar_web.py.
The page shows up to two cameras (left half, 960x540 each on a 1080p screen),
the battery, solar, load and generator, today's totals, a 24 hour chart and,
for sites with a hand-started generator, whether to start it before dark.

Cameras come from go2rtc (stack service "go2rtc"), which turns the cameras'
RTSP/RTSPS streams (UniFi Protect: camera > Settings > Advanced > RTSPS) into
something the browser can play (MSE). It listens on 127.0.0.1 only, so the
streams are visible on the box's own screen but not on the network.

Settings (setup page) are kept in state/display.json (mode 600: the stream
links contain access tokens):
  {"enabled": true, "off_from": "22:00", "off_to": "06:00",
   "cameras": [{"name": "Driveway", "url": "rtsps://..."}, ...]}

Command line (used by the systemd units):
  display.py should-run     exit 0 if the display is enabled, the kiosk software
                            is installed and a screen is connected
  display.py wait-web       wait up to 2 minutes for the start page to answer
  display.py power          turn the screen off/on for the off hours (timer)
"""
import argparse
import datetime as dt
import glob
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solar_setup as ss  # noqa: E402

VM = os.environ.get("SOLAR_VM", "http://127.0.0.1:8428")
DEFAULTS = {"enabled": True, "off_from": "", "off_to": "", "cameras": []}
MAX_CAMERAS = 2
KIOSK_USER = "solar-display"


# --------------------------------------------------------------------------
# Settings and go2rtc
# --------------------------------------------------------------------------
def settings_path(stack):
    return os.path.join(stack, "state", "display.json")


def load(stack):
    d = dict(DEFAULTS)
    d.update(ss.load_json(settings_path(stack), {}))
    d["cameras"] = [c for c in (d.get("cameras") or []) if c.get("url")][:MAX_CAMERAS]
    return d


def go2rtc_source(url):
    """Camera link -> go2rtc source. UniFi Protect gives rtsps://host:7441/TOKEN?enableSrtp with a
    self-signed certificate; go2rtc's rtspx:// is RTSPS without the certificate check."""
    u = urllib.parse.urlsplit(url.strip())
    if u.scheme not in ("rtsp", "rtsps", "rtspx") or not u.hostname:
        raise ValueError("a camera link must start with rtsp:// or rtsps:// and include the camera's address")
    if u.scheme == "rtsps":
        q = "&".join(p for p in u.query.split("&") if p and p.lower() != "enablesrtp")
        u = u._replace(scheme="rtspx", query=q)
    return urllib.parse.urlunsplit(u)


def parse_hhmm(s):
    s = (s or "").strip()
    if not s:
        return ""
    try:
        t = dt.datetime.strptime(s, "%H:%M")
    except ValueError:
        raise ValueError(f"'{s}' is not a time like 22:00")
    return t.strftime("%H:%M")


def save(stack, d):
    """Write display.json and go2rtc's config. Returns True if the camera config changed."""
    ss.write_if_changed(settings_path(stack), json.dumps(d, indent=2) + "\n", 0o600)
    lines = ["# Written by the solar monitor setup page (cameras for the HDMI display); edits are overwritten.",
             "api:", "  listen: \":1984\"", "  origin: \"*\"",
             "rtsp:", "  listen: \"\"", "webrtc:", "  listen: \"\"", "log:", "  level: warn", "streams:"]
    for i, c in enumerate(d["cameras"], start=1):
        lines.append(f"  cam{i}: {json.dumps(go2rtc_source(c['url']))}")
    if not d["cameras"]:
        lines[-1] = "streams: {}"
    os.makedirs(os.path.join(stack, "go2rtc"), exist_ok=True)
    return ss.write_if_changed(os.path.join(stack, "go2rtc", "go2rtc.yaml"), "\n".join(lines) + "\n", 0o600)


def screen_off_now(d, now=None):
    a, b = d.get("off_from"), d.get("off_to")
    if not a or not b or a == b:
        return False
    t = (now or dt.datetime.now()).strftime("%H:%M")
    return (a <= t < b) if a < b else (t >= a or t < b)


# --------------------------------------------------------------------------
# Sun (NOAA approximation, good to a minute or two)
# --------------------------------------------------------------------------
def sun_times(lat, lon, day, tz):
    """-> (sunrise, sunset) as aware datetimes in tz, or (None, None) in polar day/night."""
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    la = math.radians(lat)
    cos_ha = math.cos(math.radians(90.833)) / (math.cos(la) * math.cos(decl)) - math.tan(la) * math.tan(decl)
    if not -1 <= cos_ha <= 1:
        return None, None
    ha = math.degrees(math.acos(cos_ha))
    midnight = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
    rise = midnight + dt.timedelta(minutes=720 - 4 * (lon + ha) - eqt)
    sset = midnight + dt.timedelta(minutes=720 - 4 * (lon - ha) - eqt)
    return rise.astimezone(tz), sset.astimezone(tz)


# --------------------------------------------------------------------------
# Data for the page
# --------------------------------------------------------------------------
INSTANT = {
    "soc": "max(lxp_soc)",
    "pv": "sum(lxp_p_pv)",
    "load": "sum(lxp_p_load) or sum(lxp_p_eps)",
    "battery": "sum(lxp_p_charge) - sum(lxp_p_discharge)",
    "gen": "sum(lxp_p_gen)",
    "pv_today": "sum(lxp_e_pv_day)",
    "used_today": "sum(lxp_e_load_day) or sum(lxp_e_eps_day)",
    "charged_today": "sum(lxp_e_chg_day)",
    "discharged_today": "sum(lxp_e_dischg_day)",
    "gen_today": "sum(lxp_e_gen_day)",
    "online": "sum(lxp_bridge_connected)",
    "fault": "max(lxp_fault_code)",
    "warning": "max(lxp_warning_code)",
    "age": "time() - max(timestamp(lxp_soc))",
    "load_avg_24h": "avg_over_time((sum(lxp_p_load) or sum(lxp_p_eps))[24h:5m])",
    "load_avg_3d": "avg_over_time((sum(lxp_p_load) or sum(lxp_p_eps))[3d:10m])",
}
RANGE = {"pv": "sum(lxp_p_pv)", "load": "sum(lxp_p_load) or sum(lxp_p_eps)", "soc": "max(lxp_soc)"}
_cache = {}
_cache_lock = threading.Lock()


def _vm(path, params, timeout=5):
    with urllib.request.urlopen(f"{VM}{path}?{urllib.parse.urlencode(params)}", timeout=timeout) as r:
        return json.loads(r.read())["data"]["result"]


def vm_instant(q):
    try:
        r = _vm("/api/v1/query", {"query": q})
        return float(r[0]["value"][1]) if r else None
    except (OSError, ValueError, KeyError, IndexError):
        return None


def vm_series(q, hours=24, step=600):
    end = int(time.time()) // step * step
    try:
        r = _vm("/api/v1/query_range", {"query": q, "start": end - hours * 3600, "end": end, "step": step}, timeout=10)
    except (OSError, ValueError, KeyError):
        return []
    return [[int(t), round(float(v), 1)] for t, v in (r[0]["values"] if r else [])]


def _cached(key, ttl, fn):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (time.time(), val)
    return val


def load_forecast(stack, now_ts=None):
    """state/forecast.json (written hourly by solar-collector) if it is fresh, else None."""
    fc = ss.load_json(os.path.join(stack, "state", "forecast.json"), {})
    now_ts = now_ts or time.time()
    if not fc.get("hours") or now_ts - fc.get("generated", 0) > 6 * 3600:
        return None
    return fc


def forecast_kw(fc, ts):
    """Expected solar kW at ts (the hour containing ts), or None outside the forecast."""
    if not fc:
        return None
    if "_by_end" not in fc:
        fc["_by_end"] = {int(t): kw for t, kw in fc["hours"]}
    return fc["_by_end"].get(-(-int(ts) // 3600) * 3600)


def forecast_summary(fc, now, tz):
    """kWh expected today (whole day), for the rest of today and tomorrow (local days)."""
    if not fc:
        return None
    today, tomorrow = now.date(), now.date() + dt.timedelta(days=1)
    out = {"today": 0.0, "remaining": 0.0, "tomorrow": 0.0}
    seen_tomorrow = False
    nts = now.timestamp()
    for t, kw in fc["hours"]:
        day = dt.datetime.fromtimestamp(t - 1800, tz).date()
        if day == today:
            out["today"] += kw
            out["remaining"] += kw * min(1.0, max(0.0, (t - nts) / 3600))
        elif day == tomorrow:
            out["tomorrow"] += kw
            seen_tomorrow = True
    if not seen_tomorrow:
        return None
    return {k: round(v, 1) for k, v in out.items()}


def _hhmm(t, now):
    return f"{t:%H:%M}" + (" tomorrow" if t.date() > now.date() else "")


def generator_advice(site, now_vals, now, tz, fc=None):
    """For a hand-started generator: will the battery last until the sun is back?
    With a solar forecast the battery is simulated in 15-minute steps (forecast solar against the
    average use of the last 3 days) until the next sunset after tomorrow's sunrise, so a cloudy
    tomorrow counts. Without one, solar is assumed to carry the load from 2 h after sunrise."""
    try:
        lat, lon = float(site["latitude"]), float(site["longitude"])
        kwh = int(site["battery_count"]) * float(site["battery_kwh_each"])
        floor = float(site.get("low_soc_alert") or 25)
    except (KeyError, TypeError, ValueError):
        return None
    soc, load_w = now_vals.get("soc"), now_vals.get("load_avg_3d") or now_vals.get("load_avg_24h")
    if soc is None or not load_w or load_w <= 0:
        return None
    today = now.date()
    rise, sset = sun_times(lat, lon, today, tz)
    if rise is None:
        return None
    fsum = forecast_summary(fc, now, tz)
    tomorrow_txt = f" Tomorrow: about {fsum['tomorrow']:.0f} kWh of solar expected." if fsum else ""
    if now < rise:                                  # before dawn: tonight is today's sunrise
        next_rise, next_set = rise, sset
    elif now < sset - dt.timedelta(hours=4):        # daytime: judge the coming night from mid-afternoon on
        return {"level": "info", "text": f"Sunset {sset:%H:%M}.{tomorrow_txt}", "sunset": sset.strftime("%H:%M"),
                "forecast_tomorrow_kwh": fsum and fsum["tomorrow"]}
    else:
        next_rise, next_set = sun_times(lat, lon, today + dt.timedelta(days=1), tz)
        next_rise = next_rise or rise + dt.timedelta(days=1)
        next_set = next_set or sset + dt.timedelta(days=1)
    usable = (soc - floor) / 100 * kwh                # kWh above the alert level
    headroom = (100 - floor) / 100 * kwh
    load_kw = load_w / 1000
    empty_at, lowest = None, usable
    if fsum and forecast_kw(fc, next_set.timestamp() - 3600) is not None:
        e, t, step = usable, now, dt.timedelta(minutes=15)
        while t < next_set:
            pv = forecast_kw(fc, t.timestamp()) or 0.0
            e = min(e + (pv - load_kw) * 0.25, headroom)
            if e < 0 and empty_at is None:
                empty_at = t
            lowest = min(lowest, e)
            t += step
        need = max(0.0, -lowest)
    else:                                             # no forecast: solar carries the load 2 h after sunrise
        fsum = None
        relief = next_rise + dt.timedelta(hours=2)
        hours_left = max(0.0, usable) / load_kw
        if now + dt.timedelta(hours=hours_left) < relief:
            empty_at = now + dt.timedelta(hours=hours_left)
        need = max(0.0, (relief - now).total_seconds() / 3600 * load_kw - max(usable, 0))
    base = {"sunset": sset.strftime("%H:%M"), "empty_at": empty_at and _hhmm(empty_at, now),
            "need_kwh": round(need, 1), "forecast_tomorrow_kwh": fsum and fsum["tomorrow"]}
    when = "now" if now >= sset - dt.timedelta(minutes=30) or now < rise else f"before dark ({sset:%H:%M})"
    if empty_at is not None and empty_at < next_rise + dt.timedelta(hours=2):
        return dict(base, level="warn", text=(f"Start the generator {when}: at the usual use the battery reaches "
                                              f"{floor:.0f}% about {_hhmm(empty_at, now)}, before the sun is back. "
                                              f"About {need:.1f} kWh more is needed.{tomorrow_txt}"))
    if empty_at is not None:                          # lasts the night, but the next day is too dull to recover
        return dict(base, level="warn", text=(f"Start the generator {when}: tomorrow looks dull (about "
                                              f"{fsum['tomorrow']:.0f} kWh of solar expected), so at the usual use the "
                                              f"battery reaches {floor:.0f}% about {_hhmm(empty_at, now)}. "
                                              f"About {need:.1f} kWh more is needed."))
    return dict(base, level="ok", text=f"No generator needed tonight: the battery should stay above {floor:.0f}% "
                                         f"until the sun is back (sunset {sset:%H:%M}).{tomorrow_txt}")


def data(stack, site):
    vals = _cached("instant", 5, lambda: {k: vm_instant(q) for k, q in INSTANT.items()})
    series = _cached("range", 300, lambda: {k: vm_series(q) for k, q in RANGE.items()})
    tzname = site.get("time_zone") or ss.host_timezone()
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tzname)
    except Exception:  # noqa: BLE001
        tz = dt.timezone.utc
    now = dt.datetime.now(tz)
    d = load(stack)
    alerts = []
    expected = len(ss.load_json(os.path.join(stack, "state", "inverters.json"), {}))
    if vals.get("age") is None or vals["age"] > 300:
        alerts.append({"id": "stale", "level": "warn", "text": "No inverter data for over 5 minutes: check the dongles' network."})
    elif expected and (vals.get("online") or 0) < expected:
        alerts.append({"id": "offline", "level": "warn", "text": f"{int(vals.get('online') or 0)} of {expected} inverter dongles connected."})
    if vals.get("fault"):
        alerts.append({"id": "fault", "level": "warn", "text": f"Inverter fault code {int(vals['fault'])}."})
    try:
        low = float(site.get("low_soc_alert") or 25)
        if vals.get("soc") is not None and vals["soc"] <= low:
            alerts.append({"id": "low_battery", "level": "warn", "text": f"Battery low: {vals['soc']:.0f}%."})
    except ValueError:
        pass
    fc = load_forecast(stack)
    advice = generator_advice(site, vals, now, tz, fc) if site.get("generator") == "manual" else None
    try:
        kwh = round(int(site["battery_count"]) * float(site["battery_kwh_each"]), 1)
    except (KeyError, TypeError, ValueError):
        kwh = None
    return {
        "name": site.get("name") or "Solar", "now": vals, "series": series, "alerts": alerts, "advice": advice,
        "battery_kwh": kwh, "generator": site.get("generator"), "low_soc": site.get("low_soc_alert"),
        "cameras": [{"id": f"cam{i}", "name": c.get("name") or f"Camera {i}"} for i, c in enumerate(d["cameras"], 1)],
        "screen_off": screen_off_now(d, now.replace(tzinfo=None)),
        "rev": int(os.path.getmtime(settings_path(stack))) if os.path.exists(settings_path(stack)) else 0,
        "time": now.strftime("%H:%M"), "date": now.strftime("%a %b %-d"),
        "expected": expected, "forecast": forecast_summary(fc, now, tz),
    }


def vm_at(q, ts):
    try:
        r = _vm("/api/v1/query", {"query": q, "time": int(ts)})
        return float(r[0]["value"][1]) if r else None
    except (OSError, ValueError, KeyError, IndexError):
        return None


def day_stats(start, end):
    """Totals for [start, end): the inverters' daily counters at their highest, lowest battery, generator time."""
    secs = max(int(end - start), 60)
    w = f"[{secs}s:1m]"
    return {
        "solar": vm_at(f"max_over_time((sum(lxp_e_pv_day)){w})", end),
        "used": vm_at(f"max_over_time((sum(lxp_e_load_day) or sum(lxp_e_eps_day)){w})", end),
        "gen_kwh": vm_at(f"max_over_time((sum(lxp_e_gen_day)){w})", end),
        "low_soc": vm_at(f"min_over_time((max(lxp_soc)){w})", end),
        "gen_hours": (lambda f: None if f is None else f * secs / 3600)(
            vm_at(f"avg_over_time((sum(lxp_p_gen) > bool 200){w})", end)),
    }


def _kwh(v):
    return "?" if v is None else f"{v:.0f}" if v >= 20 else f"{v:.1f}"


def daily_summary(stack, site, now, tz):
    """The day to report (before 06:00 the day before, else today so far) as a short phone message."""
    day = now.date() - dt.timedelta(days=1) if now.hour < 6 else now.date()
    start = dt.datetime.combine(day, dt.time(0), tz)
    end = min(start + dt.timedelta(days=1), now)
    complete = end < now or end == start + dt.timedelta(days=1)
    st = _cached(("day", str(day), complete), 300, lambda: day_stats(start.timestamp(), end.timestamp() - 1))
    parts = [f"{_kwh(st['solar'])} kWh solar, {_kwh(st['used'])} kWh used"]
    if st["low_soc"] is not None:
        parts.append(f"battery down to {st['low_soc']:.0f}%")
    if site.get("generator", "manual") != "none":
        h = st["gen_hours"] or 0
        parts.append(f"generator {h:.1f} h ({_kwh(st['gen_kwh'] or 0)} kWh)" if h >= 0.05 else "no generator")
    text = ", ".join(parts) + "."
    f = forecast_summary(load_forecast(stack), now, tz)
    if f:
        nxt = "Today" if day < now.date() else "Tomorrow"
        text += f" {nxt}: about {_kwh(f['today'] if day < now.date() else f['tomorrow'])} kWh of solar expected."
    title = f"Solar {start:%a %b %-d}" + ("" if complete else " so far")
    weekly = ""
    if day.weekday() == 6:                            # Sunday: the week, and what the advisor found
        wk = _cached(("week", str(day)), 3600, lambda: [day_stats(
            dt.datetime.combine(day - dt.timedelta(days=i), dt.time(0), tz).timestamp(),
            min(dt.datetime.combine(day - dt.timedelta(days=i - 1), dt.time(0), tz), now).timestamp() - 1)
            for i in range(7)])
        tot = lambda k: sum(d[k] or 0 for d in wk)  # noqa: E731
        weekly = f"This week: {_kwh(tot('solar'))} kWh solar, {_kwh(tot('used'))} kWh used"
        if site.get("generator", "manual") != "none":
            weekly += f", generator {tot('gen_hours'):.1f} h"
        weekly += "."
        adv = ss.load_json(os.path.join(stack, "state", "advisor.json"), {})
        if adv.get("recommendation"):
            weekly += " Advisor: " + adv["recommendation"][0]
        elif adv.get("message"):
            weekly += " Advisor: " + adv["message"]
        text += " " + weekly
    return {"day": str(day), "complete": complete, "title": title, "text": text, "weekly": weekly}


def _r(v, n=0):
    return None if v is None else round(v, n) if n else int(round(v))


def ha_status(stack, site):
    """Site totals and alerts for Home Assistant (/api/ha; packages/solar_monitor.yaml reads it).
    The same figures and alerts as the wall display, so the two never disagree."""
    d = data(stack, site)
    v, kwh = d["now"], d["battery_kwh"]
    try:
        floor = float(site.get("low_soc_alert") or 25)
    except ValueError:
        floor = 25.0
    soc, load_w = v.get("soc"), v.get("load_avg_3d") or v.get("load_avg_24h")
    stored = None if soc is None or kwh is None else soc / 100 * kwh
    runtime = None
    if stored is not None and load_w and load_w > 0:
        runtime = max(0.0, (soc - floor) / 100 * kwh) / (load_w / 1000)
    ids = {a["id"] for a in d["alerts"]}
    adv = d["advice"] or {}
    return {
        "battery_level": _r(soc), "solar_power": _r(v.get("pv")), "house_load": _r(v.get("load")),
        "battery_power": _r(v.get("battery")), "generator_power": _r(v.get("gen")),
        "solar_today": _r(v.get("pv_today"), 2), "used_today": _r(v.get("used_today"), 2),
        "charged_today": _r(v.get("charged_today"), 2), "discharged_today": _r(v.get("discharged_today"), 2),
        "generator_today": _r(v.get("gen_today"), 2),
        "battery_stored": _r(stored, 1), "battery_capacity": kwh, "battery_runtime": _r(runtime, 1),
        "usual_load": _r(load_w), "low_battery_level": floor,
        "dongles_connected": _r(v.get("online")), "dongles_expected": d["expected"],
        "fault_code": _r(v.get("fault")) or 0, "warning_code": _r(v.get("warning")) or 0,
        "data_age": _r(v.get("age")),
        "data_stale": "stale" in ids, "dongle_offline": "offline" in ids, "inverter_fault": "fault" in ids,
        "battery_low": "low_battery" in ids,
        "alerts": [a["text"] for a in d["alerts"]],
        "generator_mode": site.get("generator") or "none",
        "advice": adv.get("level") or "none", "advice_text": adv.get("text") or "",
        "advice_need_kwh": adv.get("need_kwh"), "advice_empty_at": adv.get("empty_at"), "sunset": adv.get("sunset"),
        "generator_needed": adv.get("level") == "warn",
        "forecast_today": (d["forecast"] or {}).get("today"), "forecast_remaining": (d["forecast"] or {}).get("remaining"),
        "forecast_tomorrow": (d["forecast"] or {}).get("tomorrow"),
        **_summary_fields(stack, site),
    }


def _summary_fields(stack, site):
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(site.get("time_zone") or ss.host_timezone())
    except Exception:  # noqa: BLE001
        tz = dt.timezone.utc
    sm = daily_summary(stack, site, dt.datetime.now(tz), tz)
    return {"summary_day": sm["day"], "summary_title": sm["title"], "summary_text": sm["text"]}


# --------------------------------------------------------------------------
# The page (1920x1080; scales to other sizes)
# --------------------------------------------------------------------------
PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Solar display</title><link rel="icon" href="data:,">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0b0d10;--panel:#14181d;--fg:#f2f4f6;--muted:#8a96a3;--line:#252b33;
--solar:#f5b301;--load:#4aa3ff;--batt:#2ecc71;--gen:#c77dff;--warn:#ff8c42;--bad:#ff4d4d}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
font-family:system-ui,"DejaVu Sans",sans-serif;overflow:hidden;cursor:none}
#wrap{display:grid;grid-template-columns:50vw 50vw;height:100vh}
#wrap.nocam{grid-template-columns:0 100vw}
#cams{display:grid;grid-template-rows:50vh 50vh;background:#000}
.cam{position:relative;border-bottom:2px solid var(--bg);overflow:hidden}
solar-cam{display:block;width:100%;height:100%;background:#000}
solar-cam video{width:100%;height:100%;object-fit:cover;display:block}
.cam .lbl{position:absolute;z-index:2;left:14px;top:10px;font-size:1.5vw;font-weight:600;text-shadow:0 1px 4px #000}
.cam .msg{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:1.4vw}
#info{display:flex;flex-direction:column;padding:1.6vw 1.8vw;gap:1.1vw;min-width:0;height:100vh;overflow:hidden}
#top{display:flex;justify-content:space-between;align-items:baseline}
#name{font-size:2vw;font-weight:600}#clock{font-size:2.6vw;font-variant-numeric:tabular-nums}
#date{color:var(--muted);font-size:1.1vw;text-align:right}
#tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:1vw}
.nocam #tiles{grid-template-columns:repeat(5,1fr)}
.tile{background:var(--panel);border-radius:14px;padding:1vw 1.2vw;min-width:0}
.tile .k{color:var(--muted);font-size:1.05vw}.tile .v{font-size:3.3vw;font-weight:700;line-height:1.1;font-variant-numeric:tabular-nums}
.tile .s{color:var(--muted);font-size:1vw;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nocam .tile .v{font-size:2.6vw}
#soc .v{font-size:4.6vw}#socbar{height:.8vw;border-radius:.4vw;background:var(--line);margin-top:.5vw;overflow:hidden}
#socfill{height:100%;background:var(--batt);width:0}
#soc{grid-row:span 2}.nocam #soc{grid-row:auto}
#chart{flex:1;background:var(--panel);border-radius:14px;padding:.8vw 1vw;min-height:0;display:flex;flex-direction:column}
#chart .k{color:var(--muted);font-size:1vw;display:flex;gap:1.4vw}
#chart svg{flex:1;width:100%;min-height:0}
.dot{display:inline-block;width:.8vw;height:.8vw;border-radius:50%;margin-right:.35vw;vertical-align:middle}
#notes{display:flex;flex-direction:column;gap:.6vw}
.note{border-radius:12px;padding:.8vw 1.1vw;font-size:1.25vw;background:var(--panel);border-left:.5vw solid var(--muted)}
.note.warn{border-left-color:var(--warn);background:#2a1d12}.note.ok{border-left-color:var(--batt)}
#foot{color:var(--muted);font-size:.9vw;display:flex;justify-content:space-between}
#blank{position:fixed;inset:0;background:#000;display:none;z-index:9}
</style></head><body>
<div id="wrap" class="nocam"><div id="cams"></div>
<div id="info">
 <div id="top"><div id="name">Solar</div><div><div id="clock">--:--</div><div id="date"></div></div></div>
 <div id="tiles">
  <div class="tile" id="soc"><div class="k">Battery</div><div class="v">--</div><div id="socbar"><div id="socfill"></div></div><div class="s"></div><div class="s" id="runtime" style="margin-top:.6vw;white-space:normal"></div></div>
  <div class="tile" id="pv"><div class="k">Solar</div><div class="v" style="color:var(--solar)">--</div><div class="s"></div></div>
  <div class="tile" id="load"><div class="k">House</div><div class="v" style="color:var(--load)">--</div><div class="s"></div></div>
  <div class="tile" id="bat"><div class="k">Battery power</div><div class="v">--</div><div class="s"></div></div>
  <div class="tile" id="gen"><div class="k">Generator</div><div class="v" style="color:var(--gen)">--</div><div class="s"></div></div>
 </div>
 <div id="chart"><div class="k"><span><span class="dot" style="background:var(--solar)"></span>Solar</span>
  <span><span class="dot" style="background:var(--load)"></span>House</span>
  <span><span class="dot" style="background:var(--batt)"></span>Battery %</span><span style="margin-left:auto">last 24 hours</span></div>
  <svg id="svg" viewBox="0 0 1000 300" preserveAspectRatio="none"></svg></div>
 <div id="notes"></div>
 <div id="foot"><span id="upd"></span><span>solar-monitor</span></div>
</div></div><div id="blank"></div>
<script>
const $=id=>document.getElementById(id);
let rev=null, camsBuilt="";
function kw(w){if(w==null)return"--";const a=Math.abs(w);return a>=1000?(w/1000).toFixed(a>=10000?0:1)+" kW":Math.round(w)+" W"}
function setTile(id,v,s,color){const t=$(id);t.querySelector(".v").textContent=v;t.querySelector(".s").textContent=s||"";if(color)t.querySelector(".v").style.color=color}
// Cameras: go2rtc's player (video-rtc.js) without controls, over MSE. go2rtc only listens on
// 127.0.0.1, so the pictures are shown on the box's own screen and nowhere else.
let camClass=null;
async function camElement(id){
  if(!camClass){
    const {VideoRTC}=await import("http://127.0.0.1:1984/video-rtc.js");
    camClass=class extends VideoRTC{oninit(){super.oninit();this.video.controls=false;this.video.muted=true;}};
    customElements.define("solar-cam",camClass);
  }
  const el=document.createElement("solar-cam");
  el.mode="mse";el.media="video";el.background=true;el.visibilityCheck=false;
  el.src=`ws://127.0.0.1:1984/api/ws?src=${encodeURIComponent(id)}`;
  return el;
}
async function buildCams(cams){
  const key=JSON.stringify(cams); if(key===camsBuilt)return; camsBuilt=key;
  const box=$("cams"); box.innerHTML="";
  $("wrap").className=cams.length?"":"nocam";
  box.style.gridTemplateRows=cams.length>1?"50vh 50vh":"100vh";
  const host=location.hostname;
  for(const c of cams){
    const d=document.createElement("div"); d.className="cam";
    const local=host==="127.0.0.1"||host==="localhost";
    const m=document.createElement("div");m.className="msg";d.appendChild(m);
    if(local){try{d.appendChild(await camElement(c.id))}
      catch(e){m.textContent="Camera service not running";camsBuilt="";}}
    else m.textContent="Cameras show on the box's own screen";
    const l=document.createElement("div");l.className="lbl";l.textContent=c.name;d.appendChild(l);
    box.insertBefore(d,null);
  }
}
function line(pts,t0,t1,max,color,w){if(!pts.length||!max)return"";
  const d=pts.map((p,i)=>(i?"L":"M")+((p[0]-t0)/(t1-t0)*1000).toFixed(1)+","+(290-p[1]/max*270).toFixed(1)).join("");
  return `<path d="${d}" fill="none" stroke="${color}" stroke-width="${w}" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>`}
function chart(s){
  const all=[...(s.pv||[]),...(s.load||[])];if(!all.length){$("svg").innerHTML="";return}
  const t1=Math.floor(Date.now()/1000), t0=t1-86400;
  const max=Math.max(1000,...all.map(p=>p[1]))*1.05;
  let g="";for(let i=1;i<4;i++)g+=`<line x1="0" x2="1000" y1="${290-i*67.5}" y2="${290-i*67.5}" stroke="#252b33" vector-effect="non-scaling-stroke"/>`;
  $("svg").innerHTML=g+line(s.pv||[],t0,t1,max,"#f5b301",3)+line(s.load||[],t0,t1,max,"#4aa3ff",3)+line((s.soc||[]).map(p=>[p[0],Math.min(100,p[1])]),t0,t1,100,"#2ecc71",2);
}
async function tick(){
  let d;try{const r=await fetch("/display/data",{cache:"no-store"});d=await r.json()}catch(e){$("upd").textContent="Waiting for the monitor...";return}
  if(rev!==null&&d.rev!==rev){location.reload();return} rev=d.rev;
  $("blank").style.display=d.screen_off?"block":"none";
  $("name").textContent=d.name;$("clock").textContent=d.time;$("date").textContent=d.date;
  buildCams(d.cameras);
  const n=d.now||{};
  const soc=n.soc; $("soc").querySelector(".v").textContent=soc==null?"--":Math.round(soc)+"%";
  $("socfill").style.width=(soc||0)+"%";
  const low=parseFloat(d.low_soc||25);
  $("socfill").style.background=soc==null?"var(--line)":soc<=low?"var(--bad)":soc<=low+15?"var(--warn)":"var(--batt)";
  const kwhLeft=(soc!=null&&d.battery_kwh)?(soc/100*d.battery_kwh).toFixed(1)+" kWh stored":"";
  $("soc").querySelector(".s").textContent=kwhLeft;
  const use=n.load_avg_3d||n.load_avg_24h;
  if(soc!=null&&d.battery_kwh&&use>0){const h=Math.max(0,(soc-low)/100*d.battery_kwh)/(use/1000);
    $("runtime").textContent=`About ${h<1?Math.round(h*60)+" min":h.toFixed(h<10?1:0)+" h"} to ${low}% at the usual ${kw(use)} use`;}
  else $("runtime").textContent="";
  const fc=d.forecast;
  setTile("pv",kw(n.pv),(n.pv_today!=null?n.pv_today.toFixed(1)+" kWh today":"")+(fc?`, ${fc.tomorrow.toFixed(0)} tomorrow`:""));
  setTile("load",kw(n.load),n.used_today!=null?n.used_today.toFixed(1)+" kWh today":"");
  const b=n.battery; setTile("bat",kw(b==null?null:Math.abs(b)),b==null?"":b>50?"charging":b<-50?"discharging":"idle",
    b==null?null:b>50?"var(--batt)":b<-50?"var(--warn)":"var(--fg)");
  $("gen").style.display=(d.generator==="none"&&!(n.gen>0))?"none":"";
  setTile("gen",(n.gen||0)>50?kw(n.gen):"Off",(n.gen||0)>50?"charging the battery":(d.generator==="manual"?"started by hand":""));
  chart(d.series||{});
  const notes=[...(d.alerts||[])]; if(d.advice)notes.push(d.advice);
  $("notes").innerHTML="";for(const a of notes){const e=document.createElement("div");e.className="note "+(a.level||"");e.textContent=a.text;$("notes").appendChild(e)}
  const age=n.age; $("upd").textContent=age==null?"No inverter data":"Inverter data "+(age<90?"live":Math.round(age/60)+" min old");
}
tick();setInterval(tick,10000);
setTimeout(()=>location.reload(),6*3600*1000);
</script></body></html>"""


# --------------------------------------------------------------------------
# systemd helpers
# --------------------------------------------------------------------------
def screen_connected():
    for p in glob.glob("/sys/class/drm/card*-*/status"):
        try:
            if open(p).read().strip() == "connected":
                return True
        except OSError:
            pass
    return False


def should_run(stack):
    if not load(stack).get("enabled", True):
        print("display turned off on the setup page")
        return False
    if not os.path.exists("/usr/bin/cage") or not (os.path.exists("/usr/bin/chromium") or os.path.exists("/usr/bin/chromium-browser")):
        print("kiosk software (cage, chromium) not installed")
        return False
    if not screen_connected():
        print("no screen connected")
        return False
    return True


def wait_web(url="http://127.0.0.1/display/data", timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=5).read()
            return True
        except OSError:
            time.sleep(3)
    return False


def power(stack):
    """Screen on/off for the off hours, through the compositor (wlr-randr)."""
    if subprocess.run(["systemctl", "is-active", "--quiet", "solar-display.service"]).returncode != 0:
        return
    try:
        import pwd
        uid = pwd.getpwnam(KIOSK_USER).pw_uid
    except KeyError:
        return
    env = dict(os.environ, XDG_RUNTIME_DIR=f"/run/user/{uid}", WAYLAND_DISPLAY="wayland-0")
    run_as = ["runuser", "-u", KIOSK_USER, "--"]
    r = subprocess.run(run_as + ["wlr-randr"], capture_output=True, text=True, env=env)
    outputs = [ln.split()[0] for ln in r.stdout.splitlines() if ln and not ln[0].isspace()]
    want = "--off" if screen_off_now(load(stack)) else "--on"
    for o in outputs:
        subprocess.run(run_as + ["wlr-randr", "--output", o, want], capture_output=True, env=env)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["should-run", "wait-web", "power"])
    ap.add_argument("--stack", default="/opt/solar-monitor/stack")
    a = ap.parse_args()
    if a.command == "should-run":
        sys.exit(0 if should_run(a.stack) else 1)
    if a.command == "wait-web":
        wait_web()  # start anyway after the timeout; the page retries by itself
    elif a.command == "power":
        power(a.stack)


if __name__ == "__main__":
    main()
