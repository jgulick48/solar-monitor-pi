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
import genlog  # noqa: E402
import solar_setup as ss  # noqa: E402

VM = os.environ.get("SOLAR_VM", "http://127.0.0.1:8428")
COOLDOWNS = [(0, "every time"), (30, "30 seconds"), (60, "1 minute"), (120, "2 minutes"), (300, "5 minutes"), (600, "10 minutes"),
             (1800, "30 minutes")]
GAPS = [(0, "no limit"), (5, "5 seconds"), (10, "10 seconds"), (30, "30 seconds"), (60, "1 minute")]
SOUND_KINDS = [("doorbell", "Doorbell", True), ("person", "Person", True), ("vehicle", "Vehicle", False),
               ("animal", "Animal", False), ("package", "Package", False)]
DEFAULTS = {"enabled": True, "off_from": "", "off_to": "", "cameras": [],
            "sounds": {k: on for k, _, on in SOUND_KINDS}, "volume": 70, "sound_quiet": True,
            # spoken announcements (tts.py): off by default; "after" the chime, or "only" speech
            # how often sounds may play: the same alert (camera + kind) is repeated at most every sound_cooldown s,
            # and two sounds are at least sound_gap s apart; doorbell rings always (the highlight isn't limited)
            "sound_cooldown": 60, "sound_gap": 5, "doorbell_always": True,
            "speak": False, "speak_mode": "after", "speak_template": "{object} at the {camera}",
            "speak_doorbell": "Someone's at the {camera}", "voice": "en_US-lessac-medium"}
MAX_CAMERAS = 16   # all of them rotate through the two places on screen
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


def events(stack):
    """Cameras with a detection now (UniFi Protect), keyed by the display's camera ids (cam1...)."""
    import protect
    protect.listen(stack)                 # (re)starts the event listener if the settings changed
    d = load(stack)
    active = protect.EVENTS.active(protect.settings(stack).get("highlight_motion", False))
    out = {}
    for i, c in enumerate(d["cameras"], 1):
        if c.get("protect_id") in active:
            out[f"cam{i}"] = active[c["protect_id"]]
    return {"active": out, "connected": protect.EVENTS.connected, "test_sound": TEST_SOUND["at"],
            "test_speak": TEST_SOUND.get("speak", 0)}


TEST_SOUND = {"at": 0, "speak": 0}   # the setup page's test buttons: the screen plays a chime / an announcement when these change


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
    # charging the inverters didn't do: a generator on a DC charger (Chargeverter); see genlog.py
    "ext_charge": "max(lxp_batt_current_bms) * max(lxp_v_bat) - (sum(lxp_p_charge) - sum(lxp_p_discharge))",
    "pv_today": "sum(lxp_e_pv_day)",
    "used_today": "sum(lxp_e_load_day) or sum(lxp_e_eps_day)",
    "charged_today": "sum(lxp_e_chg_day)",
    "discharged_today": "sum(lxp_e_dischg_day)",
    "gen_today": "sum(lxp_e_gen_day)",
    "online": "sum(lxp_bridge_connected)",
    "fault": "max(lxp_fault_code)",
    "bms_fault": "max(lxp_fault_code_bms)",
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


def clock12(site):
    """12-hour clock? The setup page's choice; by default 12-hour with US units, 24-hour with metric."""
    c = (site or {}).get("clock") or ""
    return c == "12" or (c != "24" and (site or {}).get("units", "us_customary") == "us_customary")


def hm(t, site=None):
    """A time of day the way this site writes it: 4:41 PM or 16:41."""
    return t.strftime("%-I:%M %p") if clock12(site) else t.strftime("%H:%M")


def _hhmm(t, now, site=None):
    return hm(t, site) + (" tomorrow" if t.date() > now.date() else "")


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
        return {"level": "info", "text": f"Sunset {hm(sset, site)}.{tomorrow_txt}", "sunset": sset.strftime("%H:%M"),
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
    base = {"sunset": sset.strftime("%H:%M"), "empty_at": empty_at and _hhmm(empty_at, now, site),
            "need_kwh": round(need, 1), "forecast_tomorrow_kwh": fsum and fsum["tomorrow"]}
    when = "now" if now >= sset - dt.timedelta(minutes=30) or now < rise else f"before dark ({hm(sset, site)})"
    if empty_at is not None and empty_at < next_rise + dt.timedelta(hours=2):
        return dict(base, level="warn", text=(f"Start the generator {when}: at the usual use the battery reaches "
                                              f"{floor:.0f}% about {_hhmm(empty_at, now, site)}, before the sun is back. "
                                              f"About {need:.1f} kWh more is needed.{tomorrow_txt}"))
    if empty_at is not None:                          # lasts the night, but the next day is too dull to recover
        return dict(base, level="warn", text=(f"Start the generator {when}: tomorrow looks dull (about "
                                              f"{fsum['tomorrow']:.0f} kWh of solar expected), so at the usual use the "
                                              f"battery reaches {floor:.0f}% about {_hhmm(empty_at, now, site)}. "
                                              f"About {need:.1f} kWh more is needed."))
    return dict(base, level="ok", text=f"No generator needed tonight: the battery should stay above {floor:.0f}% "
                                         f"until the sun is back (sunset {hm(sset, site)}).{tomorrow_txt}")


def data(stack, site):
    vals = dict(_cached("instant", 5, lambda: {k: vm_instant(q) for k, q in INSTANT.items()}))
    if (vals.get("ext_charge") or 0) > 500:  # same threshold as genlog: a generator on a DC charger is running
        vals["gen"] = (vals.get("gen") or 0) + vals["ext_charge"]
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
    if vals.get("bms_fault"):
        alerts.append({"id": "bms_fault", "level": "warn", "text": f"Battery (BMS) fault code {int(vals['bms_fault'])}."})
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
        "time": hm(now, site), "date": now.strftime("%a %b %-d"),
        "sounds": d.get("sounds") or {}, "volume": d.get("volume", 70), "sound_quiet": d.get("sound_quiet", True),
        "speak": d.get("speak", False), "speak_mode": d.get("speak_mode", "after"),
        "sound_cooldown": d.get("sound_cooldown", 60), "sound_gap": d.get("sound_gap", 5),
        "doorbell_always": d.get("doorbell_always", True),
        "expected": expected, "forecast": forecast_summary(fc, now, tz),
    }


def vm_at(q, ts):
    try:
        r = _vm("/api/v1/query", {"query": q, "time": int(ts)})
        return float(r[0]["value"][1]) if r else None
    except (OSError, ValueError, KeyError, IndexError):
        return None


EXT = "(" + INSTANT["ext_charge"] + ")"


def day_stats(start, end):
    """Totals for [start, end): the inverters' daily counters at their highest, lowest battery, generator time."""
    secs = max(int(end - start), 60)
    w = f"[{secs}s:1m]"
    return {
        "solar": vm_at(f"max_over_time((sum(lxp_e_pv_day)){w})", end),
        "used": vm_at(f"max_over_time((sum(lxp_e_load_day) or sum(lxp_e_eps_day)){w})", end),
        "gen_kwh": _add(vm_at(f"max_over_time((sum(lxp_e_gen_day)){w})", end),
                        (lambda f: None if f is None else f * secs / 3600 / 1000)(
                            vm_at(f"avg_over_time((clamp_min({EXT}, 0) * ({EXT} > bool 500)){w})", end))),
        "low_soc": vm_at(f"min_over_time((max(lxp_soc)){w})", end),
        "gen_hours": (lambda f: None if f is None else f * secs / 3600)(
            vm_at(f"avg_over_time(clamp_max((sum(lxp_p_gen) > bool 200) + ({EXT} > bool 500), 1){w})", end)
            if vm_at(f"count_over_time((max(lxp_batt_current_bms)){w})", end)
            else vm_at(f"avg_over_time((sum(lxp_p_gen) > bool 200){w})", end)),
    }


def _add(a, b):
    return a if b is None else b if a is None else a + b


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
    f = forecast_summary(load_forecast(stack, now.timestamp()), now, tz)
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
        **backup_fields(stack),
        **_battery_fields(stack, site),
        **genlog.ha_fields(stack, site),
        **_box_fields(stack),
    }


def _box_fields(stack):
    import boxhealth  # here, like battery: keeps the module light for the kiosk
    return boxhealth.ha_fields(stack)


def _battery_fields(stack, site):
    import battery  # here: battery.py imports this module
    try:
        return battery.ha_fields(stack, site)
    except Exception:  # noqa: BLE001 - never let the health check break the main figures
        return battery.ha_fields_empty()


def backup_fields(stack, now=None):
    """Backup state for HA: overdue once a chosen stick has had no good backup for 2 days."""
    now = now or time.time()
    cfg = ss.load_json(os.path.join(stack, "state", "backup.json"), {})
    st = ss.load_json(os.path.join(stack, "state", "backup-status.json"), {})
    last = st.get("last_ok")
    since = last or (os.path.getmtime(os.path.join(stack, "state", "backup.json")) if cfg.get("uuid") else None)
    return {
        "backup_configured": bool(cfg.get("uuid")),
        "backup_last_ok": dt.datetime.fromtimestamp(last, dt.timezone.utc).isoformat() if last else None,
        "backup_overdue": bool(cfg.get("uuid")) and since is not None and now - since > 2 * 86400,
        "backup_message": st.get("message") or "",
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
/* --u: the layout's unit, 1% of the width on a 16:9 screen; on other shapes the tighter of width and height,
   so the same layout fits 4:3, 16:10, 21:9 and small screens (the screen mode itself comes from the TV's EDID, see screen_mode). */
:root{--u:min(1vw,1.7778vh);--bg:#0b0d10;--panel:#14181d;--fg:#f2f4f6;--muted:#8a96a3;--line:#252b33;
--solar:#f5b301;--load:#4aa3ff;--batt:#2ecc71;--gen:#c77dff;--warn:#ff8c42;--bad:#ff4d4d}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
font-family:system-ui,"DejaVu Sans",sans-serif;overflow:hidden;cursor:none}
#wrap{display:grid;grid-template-columns:50vw 50vw;height:100vh}
#wrap.nocam{grid-template-columns:0 100vw}
#cams{display:grid;grid-template-rows:50vh 50vh;background:#000}
.cam{position:relative;border-bottom:2px solid var(--bg);overflow:hidden}
solar-cam{display:block;width:100%;height:100%;background:#000}
solar-cam video{width:100%;height:100%;object-fit:cover;display:block}
.cam .lbl{position:absolute;z-index:2;left:14px;top:10px;font-size:calc(var(--u)*1.5);font-weight:600;text-shadow:0 1px 4px #000}
.cam{transition:box-shadow .3s}.cam.hot{box-shadow:inset 0 0 0 calc(var(--u)*.35) var(--warn)}
.cam.ring{box-shadow:inset 0 0 0 calc(var(--u)*.5) var(--bad)}
.cam .badge{position:absolute;z-index:2;right:14px;top:10px;font-size:calc(var(--u)*1.4);font-weight:700;color:#000;
background:var(--warn);border-radius:8px;padding:.1em .5em;display:none}.cam.hot .badge{display:block}.cam.ring .badge{background:var(--bad);color:#fff}
.cam .msg{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:calc(var(--u)*1.4)}
#info{display:flex;flex-direction:column;padding:calc(var(--u)*1.6) calc(var(--u)*1.8);gap:calc(var(--u)*1.1);min-width:0;height:100vh;overflow:hidden}
#top{display:flex;justify-content:space-between;align-items:baseline}
#name{font-size:calc(var(--u)*2);font-weight:600}#clock{font-size:calc(var(--u)*2.6);font-variant-numeric:tabular-nums}
#date{color:var(--muted);font-size:calc(var(--u)*1.1);text-align:right}
#tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:calc(var(--u)*1)}
.nocam #tiles{grid-template-columns:repeat(5,1fr)}
.tile{background:var(--panel);border-radius:14px;padding:calc(var(--u)*1) calc(var(--u)*1.2);min-width:0}
.tile .k{color:var(--muted);font-size:calc(var(--u)*1.05)}.tile .v{font-size:calc(var(--u)*3.3);font-weight:700;line-height:1.1;font-variant-numeric:tabular-nums}
.tile .s{color:var(--muted);font-size:calc(var(--u)*1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nocam .tile .v{font-size:calc(var(--u)*2.6)}
#soc .v{font-size:calc(var(--u)*4.6)}#socbar{height:calc(var(--u)*.8);border-radius:calc(var(--u)*.4);background:var(--line);margin-top:calc(var(--u)*.5);overflow:hidden}
#socfill{height:100%;background:var(--batt);width:0}
#soc{grid-row:span 2}.nocam #soc{grid-row:auto}
#chart{flex:1;background:var(--panel);border-radius:14px;padding:calc(var(--u)*.8) calc(var(--u)*1);min-height:0;display:flex;flex-direction:column}
#chart .k{color:var(--muted);font-size:calc(var(--u)*1);display:flex;gap:calc(var(--u)*1.4)}
#chart svg{flex:1;width:100%;min-height:0}
.dot{display:inline-block;width:calc(var(--u)*.8);height:calc(var(--u)*.8);border-radius:50%;margin-right:calc(var(--u)*.35);vertical-align:middle}
#notes{display:flex;flex-direction:column;gap:calc(var(--u)*.6)}
.note{border-radius:12px;padding:calc(var(--u)*.8) calc(var(--u)*1.1);font-size:calc(var(--u)*1.25);background:var(--panel);border-left:calc(var(--u)*.5) solid var(--muted)}
.note.warn{border-left-color:var(--warn);background:#2a1d12}.note.ok{border-left-color:var(--batt)}
#foot{color:var(--muted);font-size:calc(var(--u)*.9);display:flex;justify-content:space-between}
#blank{position:fixed;inset:0;background:#000;display:none;z-index:9}
/* Burn-in protection (see the script): layouts rotate every 20 minutes and the page drifts a few pixels */
#wrap{transition:opacity .6s,transform 2s}#wrap.fade{opacity:0}
#wrap.swap #cams{order:2}#wrap.swap{grid-template-columns:50vw 50vw}#wrap.swap.nocam{grid-template-columns:100vw 0}
#wrap.flip #info{flex-direction:column-reverse}#wrap.flip #top{flex-direction:row-reverse}#wrap.flip #date{text-align:left}
#wrap.rtl #tiles{direction:rtl}#wrap.rtl #tiles .tile{direction:ltr}
/* A screen on its side: cameras side by side on top, the figures below */
@media (orientation:portrait){:root{--u:min(1.9vw,1.15vh)}
#wrap,#wrap.swap,#wrap.nocam,#wrap.swap.nocam{grid-template-columns:100vw;grid-template-rows:34vh 66vh}
#wrap.nocam{grid-template-rows:0 100vh}#wrap.swap{grid-template-rows:66vh 34vh}#wrap.swap.nocam{grid-template-rows:100vh 0}
#wrap.nocam #tiles{grid-template-columns:repeat(3,1fr)}#wrap.nocam #soc{grid-row:span 2}
#cams{grid-template-rows:100% !important;grid-auto-flow:column;grid-auto-columns:1fr;height:100%;min-height:0}
#info{height:100%}}
</style></head><body>
<div id="wrap" class="nocam"><div id="cams"></div>
<div id="info">
 <div id="top"><div id="name">Solar</div><div><div id="clock">--:--</div><div id="date"></div></div></div>
 <div id="tiles">
  <div class="tile" id="soc"><div class="k">Battery</div><div class="v">--</div><div id="socbar"><div id="socfill"></div></div><div class="s"></div><div class="s" id="runtime" style="margin-top:calc(var(--u)*.6);white-space:normal"></div></div>
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
let rev=null;
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
// Cameras: up to two on screen at a time, rotating every 15 s through all of them. A camera with a
// detection (UniFi Protect events, /display/events) comes to the front with a coloured frame and a label
// and stays there until a minute after the last event; a doorbell ring wins. Only cameras on screen play.
const ROTATE_S=15;
let camList=[], camKey="", rot=0, lastRot=0, events={}, slots=[];
async function buildCams(cams){
  const key=JSON.stringify(cams); if(key===camKey)return; camKey=key; camList=cams;
  const box=$("cams"); for(const s of slots)dropPlayer(s); box.innerHTML=""; slots=[];
  $("wrap").classList.toggle("nocam",!cams.length); layoutNow=null; if(typeof applyLayout==="function")applyLayout();
  const n=Math.min(2,cams.length);
  box.style.gridTemplateRows=n>1?"50vh 50vh":"100vh";
  for(let i=0;i<n;i++){
    const d=document.createElement("div"); d.className="cam";
    d.innerHTML='<div class="msg"></div><div class="lbl"></div><div class="badge"></div>';
    box.appendChild(d); slots.push({el:d,cam:null,player:null});
  }
  showCams();
}
function dropPlayer(s){ if(s.player){ try{s.player.ondisconnect()}catch(e){} s.player.remove(); s.player=null; } }
function pickCams(){
  const n=slots.length; if(!n)return[];
  const hot=camList.filter(c=>events[c.id]).sort((a,b)=>(events[b.id].level-events[a.id].level)||(events[b.id].since-events[a.id].since));
  // never the same camera twice on screen: not the same entry, and not two entries with the same name
  // (a camera added by its stream link and again from UniFi Protect)
  const same=(a,b)=>a===b||a.name.trim().toLowerCase()===b.name.trim().toLowerCase();
  const chosen=[];
  for(const c of hot){ if(chosen.length<n&&!chosen.some(x=>same(x,c)))chosen.push(c); }
  const rest=camList.filter(c=>!chosen.some(x=>same(x,c)));
  for(let i=0;chosen.length<n&&i<rest.length;i++){ const c=rest[(rot+i)%rest.length]; if(!chosen.some(x=>same(x,c)))chosen.push(c); }
  return chosen;
}
async function showCams(){
  const want=pickCams(), local=location.hostname==="127.0.0.1"||location.hostname==="localhost";
  for(let i=0;i<slots.length;i++){
    const s=slots[i], c=want[i]; if(!c)continue;
    const ev=events[c.id];
    s.el.classList.toggle("hot",!!ev); s.el.classList.toggle("ring",!!ev&&ev.level>=3);
    s.el.querySelector(".badge").textContent=ev?ev.label:"";
    s.el.querySelector(".lbl").textContent=c.name;
    if(s.cam===c.id)continue;
    dropPlayer(s); s.cam=c.id; const m=s.el.querySelector(".msg"); m.textContent="";
    if(!local){m.textContent="Cameras show on the box's own screen";continue}
    try{ s.player=await camElement(c.id); s.el.insertBefore(s.player,s.el.firstChild); }
    catch(e){ m.textContent="Camera service not running"; }
  }
}
// Sounds through the screen's speakers (HDMI): short chimes made with Web Audio, so there are no sound files.
// A camera's event plays once, when it starts; doorbell rings and people by default (setup page).
const CHIMES={doorbell:[[659,0,1.3],[523,.5,1.6]],person:[[784,0,.5],[1047,.17,.7]],vehicle:"horn",
  animal:[[880,0,.6]],package:[[988,0,.35],[1175,.14,.35],[1319,.28,.6]]};
let lastSoundCam={}, lastSound=0;
let actx=null, played={}, lastTest=null, lastSpeak=null, soundCfg={sounds:{},volume:70,quiet:true,off:false,speak:false,mode:"after",cooldown:60,gap:5,doorbellAlways:true};
// Spoken announcement (Piper on the box, /display/say): after the chime has rung out, or instead of it.
// Played through the same Web Audio context as the chimes: the Pi's HDMI sound device takes one stream at a
// time (no mixing), and once a chime has played Chromium keeps it open, so a separate <audio> element was
// silently dropped (heard on the test Pi: dings, no speech).
async function say(url, afterKind){
  const wait=soundCfg.mode==="only"||!afterKind?0:(afterKind==="doorbell"?2.1:.9);
  try{
    actx=actx||new AudioContext(); if(actx.state==="suspended")await actx.resume();
    const r=await fetch(url,{cache:"force-cache"}); if(!r.ok)return;
    const buf=await actx.decodeAudioData(await r.arrayBuffer());
    const src=actx.createBufferSource(), g=actx.createGain();
    g.gain.value=Math.max(0,Math.min(1,(soundCfg.volume||0)/100));
    src.buffer=buf; src.connect(g).connect(actx.destination);
    src.start(Math.max(actx.currentTime,(window.__chimeAt||0)+wait));
  }catch(e){}
}
// A car horn: two square waves a major third apart (about 400 and 500 Hz, slightly detuned, like a real
// two-trumpet horn) through a low-pass filter, "beep-beep".
function horn(vol,t){
  const f=actx.createBiquadFilter(); f.type="lowpass"; f.frequency.value=1800; f.Q.value=.7; f.connect(actx.destination);
  for(const [at,dur] of [[0,.22],[.32,.45]]){
    const g=actx.createGain(); g.connect(f);
    g.gain.setValueAtTime(0,t+at); g.gain.linearRampToValueAtTime(vol*.5,t+at+.015);
    g.gain.setValueAtTime(vol*.5,t+at+dur-.03); g.gain.linearRampToValueAtTime(0,t+at+dur);
    for(const hz of [404,508]){ const o=actx.createOscillator(); o.type="square"; o.frequency.value=hz; o.connect(g); o.start(t+at); o.stop(t+at+dur+.02); }
  }
}
function chime(kind){
  const notes=CHIMES[kind]; if(!notes)return;
  try{ actx=actx||new AudioContext(); if(actx.state==="suspended")actx.resume(); }catch(e){return}
  const vol=Math.max(0,Math.min(1,(soundCfg.volume||0)/100))*.6, t=actx.currentTime+.05;
  window.__chimeAt=t;
  if(notes==="horn")return horn(vol,t);
  for(const [f,at,dur] of notes){
    const o=actx.createOscillator(), g=actx.createGain(); o.type="sine"; o.frequency.value=f;
    g.gain.setValueAtTime(0,t+at); g.gain.linearRampToValueAtTime(vol,t+at+.02); g.gain.exponentialRampToValueAtTime(.0001,t+at+dur);
    o.connect(g).connect(actx.destination); o.start(t+at); o.stop(t+at+dur+.05);
  }
}
function soundFor(ev){
  const kind={Doorbell:"doorbell",Person:"person",Vehicle:"vehicle",Animal:"animal",Package:"package"}[ev.label];
  if(!kind||!soundCfg.sounds[kind]||(soundCfg.quiet&&soundCfg.off))return null; return kind;
}
async function pollEvents(){
  try{ const r=await fetch("/display/events",{cache:"no-store"}); const d=await r.json();
    if(lastTest===null)lastTest=d.test_sound; else if(d.test_sound!==lastTest){lastTest=d.test_sound; chime("doorbell");}
    if(lastSpeak===null)lastSpeak=d.test_speak; else if(d.test_speak!==lastSpeak){lastSpeak=d.test_speak; say("/display/say?test=1",null);}
    for(const [cam,ev] of Object.entries(d.active||{})){
      const key=cam+"@"+ev.since; if(played[key])continue; played[key]=1;
      const kind=soundFor(ev); if(!kind)continue;
      // rate limit (setup page): same camera / any camera; a doorbell ring always gets through if so set
      const nowS=Date.now()/1000, ring=kind==="doorbell"&&soundCfg.doorbellAlways;
      const rep=cam+"|"+kind;     // a repeat = the same alert: same camera, same kind
      if(!ring&&(nowS-(lastSoundCam[rep]||0)<soundCfg.cooldown||nowS-lastSound<soundCfg.gap))continue;
      lastSoundCam[rep]=nowS; lastSound=nowS;
      if(!(soundCfg.speak&&soundCfg.mode==="only"))chime(kind);
      if(soundCfg.speak)say("/display/say?cam="+encodeURIComponent(cam)+"&label="+encodeURIComponent(ev.label),kind);
    }
    const before=JSON.stringify(events); events=d.active||{};
    if(JSON.stringify(events)!==before)showCams();
  }catch(e){}
}
setInterval(pollEvents,1500);
setInterval(()=>{ if(camList.length>slots.length&&!camList.some(c=>events[c.id])){rot=(rot+slots.length)%camList.length;showCams();} },ROTATE_S*1000);
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
  soundCfg={sounds:d.sounds||{},volume:d.volume,quiet:d.sound_quiet,off:!!d.screen_off,speak:!!d.speak,mode:d.speak_mode||"after",
    cooldown:+(d.sound_cooldown??60),gap:+(d.sound_gap??5),doorbellAlways:d.doorbell_always!==false};
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
// Burn-in protection. Every 20 minutes one of four layouts, picked from the clock (so a reload keeps it):
// cameras left or right (without cameras: tiles in order or reversed) x info panel top-down or flipped.
// Every minute the whole page moves a few pixels along a small circle. ?layout=0..3 pins a layout.
const LAYOUT_MIN=20, pinned=new URLSearchParams(location.search).get("layout");
let layoutNow=null;
function applyLayout(){
  const n=pinned!==null?(+pinned%4):Math.floor(Date.now()/60000/LAYOUT_MIN)%4;
  const w=$("wrap"), nocam=w.classList.contains("nocam");
  const want={swap:!nocam&&(n&1)===1, rtl:nocam&&(n&1)===1, flip:(n&2)===2};
  const key=nocam+":"+n; if(key===layoutNow)return;
  const first=layoutNow===null; layoutNow=key;
  const set=()=>{for(const c in want)w.classList.toggle(c,want[c])};
  if(first){set();return}
  w.classList.add("fade");setTimeout(()=>{set();w.classList.remove("fade")},700);
}
function drift(){
  if(pinned!==null)return;
  const a=(Date.now()/60000)%60/60*2*Math.PI;
  $("wrap").style.transform=`translate(${(Math.cos(a)*6).toFixed(1)}px,${(Math.sin(a)*6).toFixed(1)}px)`;
}
tick().then(applyLayout);setInterval(tick,10000);
setInterval(()=>{applyLayout();drift()},60000);drift();
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


# --------------------------------------------------------------------------
# Screen mode from the TV's EDID
# --------------------------------------------------------------------------
DRM = "/sys/class/drm"
MAX_PIXELS = 1920 * 1200   # bigger screens (4K TVs, 1440p) get 1080p: the Pi draws and decodes the cameras there, the TV upscales


def parse_edid(b):
    """Monitor name, preferred mode (first detailed timing) and size in cm from an EDID block. {} if not valid."""
    if len(b) < 128 or b[:8] != bytes.fromhex("00ffffffffffff00"):
        return {}
    out = {"width_cm": b[21], "height_cm": b[22]}
    for off in range(54, 126, 18):
        d = b[off:off + 18]
        if d[0] or d[1]:                                   # a detailed timing descriptor
            if "preferred" not in out:
                out["preferred"] = (d[2] | (d[4] & 0xF0) << 4, d[5] | (d[7] & 0xF0) << 4)
        elif d[3] == 0xFC:                                 # monitor name
            out["name"] = d[5:18].split(b"\n")[0].decode("ascii", "replace").strip()
    return out


def _wh(mode):
    w, _, h = mode.partition("x")
    return int(w), int("".join(c for c in h if c.isdigit()))


def choose_mode(modes, preferred=None):
    """The TV's preferred mode, or 1920x1080 when that is bigger (and the TV offers 1080p or something close)."""
    sizes = []
    for m in modes:
        try:
            wh = _wh(m)
        except ValueError:
            continue
        if wh not in sizes:
            sizes.append(wh)
    if not sizes:
        return None
    pref = preferred if preferred in sizes else sizes[0]   # the kernel lists the preferred mode first
    if pref[0] * pref[1] <= MAX_PIXELS:
        return pref
    if (1920, 1080) in sizes:
        return (1920, 1080)
    smaller = [wh for wh in sizes if wh[0] * wh[1] <= MAX_PIXELS * 1.2]
    same_shape = [wh for wh in smaller if abs(wh[0] / wh[1] - pref[0] / pref[1]) < 0.05]
    return max(same_shape or smaller or [pref], key=lambda wh: wh[0] * wh[1])


def screens(drm=DRM):
    """Connected screens: [{output, name, preferred, modes, mode (chosen), width_cm, height_cm}]."""
    out = []
    for st in sorted(glob.glob(os.path.join(drm, "card*-*", "status"))):
        d = os.path.dirname(st)
        try:
            if open(st).read().strip() != "connected":
                continue
            modes = [m.strip() for m in open(os.path.join(d, "modes")).read().split() if m.strip()]
        except OSError:
            continue
        try:
            info = parse_edid(open(os.path.join(d, "edid"), "rb").read())
        except OSError:
            info = {}
        chosen = choose_mode(modes, info.get("preferred"))
        out.append({"output": os.path.basename(d).split("-", 1)[1], "name": info.get("name") or "",
                    "preferred": info.get("preferred") or (modes and _wh(modes[0])) or None, "modes": modes,
                    "mode": chosen, "width_cm": info.get("width_cm"), "height_cm": info.get("height_cm")})
    return out


def screen_mode():
    """Inside the kiosk session (kiosk.sh, before Chromium): set each screen to its chosen mode with wlr-randr."""
    for sc in screens():
        if not sc["mode"]:
            continue
        mode = f"{sc['mode'][0]}x{sc['mode'][1]}"
        r = subprocess.run(["wlr-randr", "--output", sc["output"], "--mode", mode], capture_output=True, text=True)
        print(f"{sc['output']} ({sc['name'] or 'unknown screen'}, prefers {sc['preferred']}): {mode}"
              + ("" if r.returncode == 0 else f" failed: {r.stderr.strip()[-200:]}"))


def asoundrc(outputs, cards):
    """~/.asoundrc for the kiosk user: ALSA's default device = the HDMI port a screen is on (Pi 4: HDMI-A-1 is
    the vc4hdmi0 card, HDMI-A-2 vc4hdmi1). None if there is no such card (then ALSA's own default stays)."""
    for out in outputs:
        if not out.startswith("HDMI-A-"):
            continue
        card = f"vc4hdmi{int(out.rsplit('-', 1)[1]) - 1}"
        if card in cards:
            return (f"# Written by the solar monitor kiosk: sound to the screen on {out}.\n"
                    f'pcm.!default {{ type plug slave.pcm "hdmi:CARD={card},DEV=0" }}\n'
                    f"ctl.!default {{ type hw card {card} }}\n")
    return None


def screen_audio(home=None, proc="/proc/asound/cards"):
    """Inside the kiosk session: point the kiosk user's sound at the connected HDMI screen."""
    try:
        cards = [ln.split("[", 1)[1].split("]", 1)[0].strip() for ln in open(proc) if "[" in ln]
    except OSError:
        cards = []
    conf = asoundrc([s["output"] for s in screens()], cards)
    target = os.path.join(home or os.path.expanduser("~"), ".asoundrc")
    if conf:
        with open(target, "w") as f:
            f.write(conf)
        print(f"sound: {conf.splitlines()[1]}")
    else:
        print(f"sound: no HDMI sound card for the connected screen (cards: {', '.join(cards) or 'none'})")


def screen_line():
    """For the setup page."""
    sc = screens()
    if not sc:
        return "no screen connected"
    parts = []
    for s in sc:
        pref = "x".join(map(str, s["preferred"])) if s["preferred"] else "?"
        used = "x".join(map(str, s["mode"])) if s["mode"] else "?"
        size = f", {round(((s['width_cm'] or 0) ** 2 + (s['height_cm'] or 0) ** 2) ** .5 / 2.54)}\"" if s.get("width_cm") else ""
        parts.append(f"{s['name'] or s['output']}{size}: shown at {used}" + (f" (its own best is {pref})" if pref != used else ""))
    return "; ".join(parts)


def should_run(stack):
    try:
        settings = load(stack)
    except OSError as e:   # say so in the journal (the start page shows it) instead of a bare traceback
        print(f"can't read the display settings: {e}")
        return False
    if not settings.get("enabled", True):
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
    ap.add_argument("command", choices=["should-run", "wait-web", "power", "screen-mode", "screen-audio"])
    ap.add_argument("--stack", default="/opt/solar-monitor/stack")
    a = ap.parse_args()
    if a.command == "should-run":
        sys.exit(0 if should_run(a.stack) else 1)
    if a.command == "wait-web":
        wait_web()  # start anyway after the timeout; the page retries by itself
    elif a.command == "power":
        power(a.stack)
    elif a.command == "screen-mode":
        screen_mode()
    elif a.command == "screen-audio":
        screen_audio()


if __name__ == "__main__":
    main()
