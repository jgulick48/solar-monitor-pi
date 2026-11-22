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
  display.py power-daemon   switch the screen (HDMI signal, and the TV over HDMI-CEC) as screenpower.py decides
  display.py power          one-shot: the HDMI signal only
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
import screenpower as sp  # noqa: E402
import solar_setup as ss  # noqa: E402
from cameras import MAX_CAMERAS, SOUND_KINDS  # noqa: E402,F401 - camera settings live in display.json

VM = os.environ.get("SOLAR_VM", "http://127.0.0.1:8428")
DEFAULTS = {"enabled": True, "off_from": "", "off_to": "", "cameras": [],
            "sounds": {k: on for k, _, on in SOUND_KINDS}, "volume": 70, "sound_quiet": True,
            # what voice announcements (the voice module) say for a camera event
            # how often sounds may play: the same alert (camera + kind) is repeated at most every sound_cooldown s,
            # and two sounds are at least sound_gap s apart; doorbell rings always (the highlight isn't limited)
            "sound_cooldown": 60, "sound_gap": 5, "doorbell_always": True,
            "speak_template": "{object} at the {camera}", "speak_doorbell": "Someone's at the {camera}"}
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
    d.update(sp.migrate(d))              # when the screen is on (screenpower.py); the old off_from/off_to become a rule
    return d


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
    """Write display.json and go2rtc's config (cameras.py). Returns True if the camera config changed."""
    import cameras
    ss.write_if_changed(settings_path(stack), json.dumps(d, indent=2) + "\n", 0o600)
    return cameras.write_go2rtc(stack, d["cameras"])


def events(stack):
    """For the screen's fast poll (and the power service): whether the screen should be on, quiet hours, the test
    chime, and what the modules add: the cameras module's detections ("active"), the voice module's announcements."""
    import modules
    d = load(stack)
    ev = {"active": {}, "connected": False, "say": [], **modules.display_events(stack)}   # cameras note their wake-ups first
    on, why = sp.wanted(d)
    return {**ev, "test_sound": TEST_SOUND["at"], "screen_on": on, "screen_why": why, "quiet_hours": quiet_hours(d)}


def quiet_hours(d, now=None):
    """Outside the on-hours (when there are any): the sounds' "not during the screen-off hours"."""
    return d.get("power_mode") in ("hours", "hours_occupied") and bool(d.get("hours")) and \
        not sp.in_hours(d["hours"], now or dt.datetime.now())


def power_state(stack):
    """For the power service: should the screen be on now, and should the TV be switched over HDMI-CEC."""
    ev = events(stack)                  # also notes camera events that wake the screen
    return {"on": ev["screen_on"], "why": ev["screen_why"], "cec": bool(load(stack).get("cec", True))}


TEST_SOUND = {"at": 0}   # the setup page's test button: the screen plays a chime when this changes


def screen_off_now(d, now=None):
    """Is the screen off now (schedule, occupancy, camera wake-ups: screenpower.wanted)?"""
    return not sp.wanted({**d, **sp.migrate(d)}, now)[0]


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
    "gen": ss.GEN_POWER,
    # charging the inverters didn't do: a generator on a DC charger (Chargeverter); see genlog.py
    "ext_charge": ss.EXT_CHARGE,
    "pv_today": "sum(lxp_e_pv_day)",
    "used_today": ss.LOAD_ENERGY_DAY,
    "charged_today": "sum(lxp_e_chg_day)",
    "discharged_today": "sum(lxp_e_dischg_day)",
    "gen_today": ss.GEN_ENERGY_DAY,
    "online": "sum(lxp_bridge_connected)",
    "fault": "max(lxp_fault_code)",
    "bms_fault": "max(lxp_fault_code_bms)",
    "warning": "max(lxp_warning_code)",
    "age": "time() - max(timestamp(lxp_soc))",
    "load_avg_24h": "avg_over_time((sum(lxp_p_load) or sum(lxp_p_eps))[24h:5m])",
    "load_avg_3d": "avg_over_time((sum(lxp_p_load) or sum(lxp_p_eps))[3d:10m])",
}
RANGE = {"pv": "sum(lxp_p_pv)", "load": "sum(lxp_p_load) or sum(lxp_p_eps)", "soc": "max(lxp_soc)"}
# a dongle dropping off for a moment doesn't make the totals dip (solar_setup.hold); the connection count and the
# data age stay live, so a dongle that's gone still shows as gone
INSTANT = {k: (q if k in ("online", "age") else ss.hold(q)) for k, q in INSTANT.items()}
RANGE = {k: ss.hold(q) for k, q in RANGE.items()}
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
    empty_at, lowest, at_rise = None, usable, None
    if fsum and forecast_kw(fc, next_set.timestamp() - 3600) is not None:
        e, t, step = usable, now, dt.timedelta(minutes=15)
        while t < next_set:
            if at_rise is None and t >= next_rise:
                at_rise = e
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
    if at_rise is None:                               # overnight there's no solar: the usual use until sunrise
        at_rise = usable - max(0.0, (next_rise - now).total_seconds() / 3600) * load_kw
    rise_soc = round(min(100.0, max(0.0, floor + at_rise / kwh * 100)))
    base = {"sunset": sset.strftime("%H:%M"), "empty_at": empty_at and _hhmm(empty_at, now, site),
            "need_kwh": round(need, 1), "forecast_tomorrow_kwh": fsum and fsum["tomorrow"],
            "sunrise": hm(next_rise, site), "sunrise_soc": rise_soc}
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
    nb = lambda t: hm(t, site).replace(" ", NBSP)  # noqa: E731 - keeps "6:51 AM" on one line on the wall display
    return dict(base, level="ok", text=f"No generator needed tonight: about {rise_soc}% at sunrise ({nb(next_rise)}), "
                                         f"above {floor:.0f}% until the sun is back. Sunset {nb(sset)}.{tomorrow_txt}")


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
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    so_far = _cached(f"soc_high_{midnight.date()}", 300, lambda: vm_at(
        f"max_over_time((max(lxp_soc))[{max(60, int((now - midnight).total_seconds()))}s])", now.timestamp()))
    high = battery_high(site, vals, now, tz, fc, so_far)
    try:
        kwh = round(int(site["battery_count"]) * float(site["battery_kwh_each"]), 1)
    except (KeyError, TypeError, ValueError):
        kwh = None
    return {
        "name": site.get("name") or "Solar", "now": vals, "series": series, "alerts": alerts, "advice": advice,
        "battery_kwh": kwh, "generator": site.get("generator"), "low_soc": site.get("low_soc_alert"),
        "cameras": [],                                   # the cameras module (when it's on)
        "screen_off": screen_off_now(d, now.replace(tzinfo=None)), "quiet_hours": quiet_hours(d, now.replace(tzinfo=None)),
        "rev": max(int(os.path.getmtime(settings_path(stack))) if os.path.exists(settings_path(stack)) else 0,
                   _modules_rev(stack)),
        "time": hm(now, site), "date": now.strftime("%a %b %-d"),
        "volume": d.get("volume", 70), "sound_quiet": d.get("sound_quiet", True),
        "sounds": {}, "sound_cooldown": 60, "sound_gap": 5, "doorbell_always": True,   # the cameras module
        "speak": False, "speak_mode": "after",           # the voice module (when it's on)
        "expected": expected, "forecast": forecast_summary(fc, now, tz), "battery_high": high,
        "weather": None, "wx_days": 7, "wx_page": 8,    # weather: the weather module (when it's on)
        **_module_display(stack, site, now),
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
        "used": vm_at(f"max_over_time(({ss.LOAD_ENERGY_DAY}){w})", end),
        "gen_kwh": _add(vm_at(f"max_over_time(({ss.GEN_ENERGY_DAY}){w})", end),
                        (lambda f: None if f is None else f * secs / 3600 / 1000)(
                            vm_at(f"avg_over_time((clamp_min({EXT}, 0) * ({EXT} > bool 500)){w})", end))),
        "low_soc": vm_at(f"min_over_time((max(lxp_soc)){w})", end),
        "gen_hours": (lambda f: None if f is None else f * secs / 3600)(
            vm_at(f"avg_over_time(clamp_max(({ss.GEN_POWER} > bool 200) + ({EXT} > bool 500), 1){w})", end)
            if vm_at(f"count_over_time((max(lxp_batt_current_bms)){w})", end)
            else vm_at(f"avg_over_time(({ss.GEN_POWER} > bool 200){w})", end)),
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


def _quarter(t):
    """t rounded to the nearest 15 minutes (None stays None)."""
    if t is None:
        return None
    t = t + dt.timedelta(minutes=7.5)
    return t.replace(minute=t.minute // 15 * 15, second=0, microsecond=0)


NBSP = "\u00a0"             # keeps "at 4:00 PM" on one line on the wall display
CHARGE_EFFICIENCY = 0.95    # share of surplus solar that ends up stored


def battery_high(site, now_vals, now, tz, fc, high_so_far=None):
    """Today's estimated highest battery level: the battery simulated in 15-minute steps from now until
    sunset, forecast solar against the usual use (the last 3 days' average), capped at full; combined
    with the highest level already reached today. None after sunset, or without a forecast, the
    battery size or the usual use.
    {"pct": 0-100, "at": "2:15 PM" or None (already reached), "full_at": time it fills or None, "text": ...}"""
    try:
        lat, lon = float(site["latitude"]), float(site["longitude"])
        kwh = int(site["battery_count"]) * float(site["battery_kwh_each"])
    except (KeyError, TypeError, ValueError):
        return None
    soc, load_w = now_vals.get("soc"), now_vals.get("load_avg_3d") or now_vals.get("load_avg_24h")
    if not fc or soc is None or not load_w or load_w <= 0 or kwh <= 0:
        return None
    rise, sset = sun_times(lat, lon, now.date(), tz)
    if rise is None or now >= sset or forecast_kw(fc, sset.timestamp() - 1800) is None:
        return None
    load_kw, step = load_w / 1000, dt.timedelta(minutes=15)
    e = best = min(soc, 100) / 100 * kwh
    best_t = full_t = None
    t = now
    while t < sset:
        net = (forecast_kw(fc, t.timestamp()) or 0.0) - load_kw
        e = min(max(e + net * 0.25 * (CHARGE_EFFICIENCY if net > 0 else 1.0), 0.0), kwh)
        t += step
        if e > best + 0.0005 * kwh:
            best, best_t = e, t
        if full_t is None and e >= 0.995 * kwh:
            full_t = t
    pct = best / kwh * 100
    best_t, full_t = _quarter(best_t), _quarter(full_t)     # shown to the quarter hour: it's an estimate
    if high_so_far is not None and high_so_far >= pct:   # the day's high is behind us
        return {"pct": round(min(high_so_far, 100)), "at": None, "full_at": None,
                "text": f"High today {min(high_so_far, 100):.0f}%"}
    if best_t is None:
        return {"pct": round(pct), "at": None, "full_at": None, "text": "Not expected to charge higher today"}
    if full_t is not None:
        return {"pct": 100, "at": hm(full_t, site), "full_at": hm(full_t, site),
                "text": "Full about" + NBSP + hm(full_t, site).replace(" ", NBSP)}
    return {"pct": round(pct), "at": hm(best_t, site), "full_at": None,
            "text": f"High today about {pct:.0f}% at" + NBSP + hm(best_t, site).replace(" ", NBSP)}


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
        "battery_at_sunrise": adv.get("sunrise_soc"), "sunrise": adv.get("sunrise"),
        "generator_needed": adv.get("level") == "warn",
        "forecast_today": (d["forecast"] or {}).get("today"), "forecast_remaining": (d["forecast"] or {}).get("remaining"),
        "forecast_tomorrow": (d["forecast"] or {}).get("tomorrow"),
        "battery_high_estimate": (d.get("battery_high") or {}).get("pct"),
        "battery_high_at": (d.get("battery_high") or {}).get("at"),
        "battery_full_at": (d.get("battery_high") or {}).get("full_at"),
        **_summary_fields(stack, site),
        **_module_fields(stack, site),
        **_battery_fields(stack, site),
        **genlog.ha_fields(stack, site),
        **_box_fields(stack),
    }


def _module_display(stack, site, now):
    import modules  # here: keeps the module light for the kiosk
    return modules.display_data(stack, site, now)


def _modules_rev(stack):
    import modules
    return modules.settings_rev(stack)


def _module_fields(stack, site):
    import modules  # here: the modules' fields (backup, ...) only for /api/ha
    return modules.ha_fields(stack, site)


def _box_fields(stack):
    import boxhealth  # here, like battery: keeps the module light for the kiosk
    return boxhealth.ha_fields(stack)


def _battery_fields(stack, site):
    import battery  # here: battery.py imports this module
    try:
        return battery.ha_fields(stack, site)
    except Exception:  # noqa: BLE001 - never let the health check break the main figures
        return battery.ha_fields_empty()


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
# The page itself is display.html (HTML, CSS and the script), served as it is at /display.
PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "display.html"), encoding="utf-8").read()


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


def signal(on):
    """HDMI signal on/off through the compositor (wlr-randr). -> 'on' / 'off' / '' when the kiosk isn't running."""
    if subprocess.run(["systemctl", "is-active", "--quiet", "solar-display.service"]).returncode != 0:
        return ""
    try:
        import pwd
        uid = pwd.getpwnam(KIOSK_USER).pw_uid
    except KeyError:
        return ""
    env = dict(os.environ, XDG_RUNTIME_DIR=f"/run/user/{uid}", WAYLAND_DISPLAY="wayland-0")
    run_as = ["runuser", "-u", KIOSK_USER, "--"]
    r = subprocess.run(run_as + ["wlr-randr"], capture_output=True, text=True, env=env)
    outputs = [ln.split()[0] for ln in r.stdout.splitlines() if ln and not ln[0].isspace()]
    for o in outputs:
        subprocess.run(run_as + ["wlr-randr", "--output", o, "--on" if on else "--off"], capture_output=True, env=env)
    return ("on" if on else "off") if outputs else ""


def _post(path, obj):
    try:
        urllib.request.urlopen(urllib.request.Request("http://127.0.0.1" + path, data=json.dumps(obj).encode(),
                                                      headers={"Content-Type": "application/json"}), timeout=10).read()
    except OSError:
        pass


def power_daemon(poll=3):
    """solar-display-power.service (root): every few seconds ask the web server whether the screen should be on;
    on a change switch the HDMI signal and, if set, the TV over HDMI-CEC. The signal is re-checked every minute
    (the kiosk may have restarted); CEC is sent only on changes, so a TV turned off with its remote stays off."""
    last, last_sig, dev, dev_at = None, 0.0, "", 0.0
    while True:
        try:
            with urllib.request.urlopen("http://127.0.0.1/display/power", timeout=10) as r:
                w = json.load(r)
        except (OSError, ValueError):
            time.sleep(10)
            continue
        on, now = bool(w.get("on")), time.time()
        rep = {}
        if w.get("cec") and (not dev or now - dev_at > 600) and (on != last):
            dev, dev_at = sp.cec_device(), now
        if on != last:
            if on:
                rep["signal"] = signal(True)
                if w.get("cec") and dev:
                    sp.cec_on(dev)
            else:
                if w.get("cec") and dev:
                    sp.cec_off(dev)
                rep["signal"] = signal(False)
            if w.get("cec"):
                time.sleep(2)
                rep["tv"] = sp.cec_power(dev) if dev else "no cec"
            print(f"screen {'on' if on else 'off'}: {w.get('why')}" + (f"; TV: {rep['tv']}" if rep.get("tv") else ""), flush=True)
            last, last_sig = on, now
            _post("/display/power-report", {"on": on, **rep})
        elif now - last_sig > 60:
            signal(on)
            last_sig = now
        time.sleep(poll)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["should-run", "wait-web", "power", "power-daemon", "screen-mode", "screen-audio"])
    ap.add_argument("--stack", default="/opt/solar-monitor/stack")
    a = ap.parse_args()
    if a.command == "should-run":
        sys.exit(0 if should_run(a.stack) else 1)
    if a.command == "wait-web":
        wait_web()  # start anyway after the timeout; the page retries by itself
    elif a.command == "power":          # one-shot (older timer): the signal only
        signal(not screen_off_now(load(a.stack)))
    elif a.command == "power-daemon":
        power_daemon()
    elif a.command == "screen-mode":
        screen_mode()
    elif a.command == "screen-audio":
        screen_audio()


if __name__ == "__main__":
    main()
