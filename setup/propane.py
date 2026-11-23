"""Propane: the tank's level, use, days to empty and fill-ups (wiki: "Boiler, propane and notifications"). The
propane module (modules/propane) is its switch and settings; this file does the work.

The level comes from a Mopeka Pro Check through pro-check-server (the `propane` Compose service, which also gives
Home Assistant its own propane entities): GET http://127.0.0.1:9811/sensors. Every minute the collector calls
sample(), which keeps the last 3 hours of readings and works on their 10-minute median (ultrasonic readings move with
temperature and sloshing):

  * a day's use = gallons at its start + gallons filled - gallons at its end (never below 0)
  * a fill = a rise of 10 points or more; it is logged once the level has stopped rising for 20 minutes
  * days to empty = gallons above the reserve / expected daily use: from heating degree days once there are 14 days
    to learn from (use = base + per-degree-day x the coming week's forecast), the last 7 days' average before that
  * alerts (through Home Assistant's script.solar_notify): under the warning level, under the critical level, and no
    reading for 6 hours; each clears itself

  state/propane.json   {"samples", "day", "days", "fills", "filling", "alerts", "last_reading"}
"""
import json
import os
import statistics
import time
import urllib.parse
import urllib.request

import solar_setup as ss

URL = "http://127.0.0.1:9811/sensors"
VM = "http://127.0.0.1:8428"
# pro-check-server's tank types: water capacity in gallons (a tank is filled to 80% of it at most)
TANKS = (("500g_h", "500 gal, horizontal", 500.0), ("100lb_v", "100 lb cylinder", 29.4), ("40lb_v", "40 lb cylinder", 11.8),
         ("30lb_v", "30 lb cylinder", 8.8), ("20lb_v", "20 lb cylinder", 5.9))
CAPACITY = {k: c for k, _, c in TANKS}
DEFAULTS = {"tank_type": "500g_h", "address": "", "reserve": 10, "warn": 20, "critical": 10, "price": ""}
KEEP_S = 3 * 3600
MEDIAN_S = 600
FILL_POINTS = 10.0
FILL_SETTLE_S = 1200
SILENT_S = 6 * 3600
MIN_QUALITY = 1                  # Mopeka read quality 0-3: 0 is no usable echo
HDD_BASE_F = 65.0


def _path(stack):
    return os.path.join(stack, "state", "propane.json")


def load(stack):
    st = ss.load_json(_path(stack), {})
    for k, v in (("samples", []), ("day", {}), ("days", []), ("fills", []), ("filling", None), ("alerts", {}),
                 ("last_reading", {})):
        st.setdefault(k, v)
    return st


def save(stack, st):
    ss.write_if_changed(_path(stack), json.dumps(st, indent=1) + "\n", 0o644)


# --------------------------------------------------------------------------
# Reading the sensor
# --------------------------------------------------------------------------
def sensors(timeout=5):
    """Every Mopeka sensor pro-check-server hears: [{address, levelPercent, gallons, ...}]."""
    with urllib.request.urlopen(URL, timeout=timeout) as r:
        return json.loads(r.read()).get("sensors") or []


def pick(found, address=""):
    """The tank's sensor: the one set up, or (none set) the one heard best."""
    if address:
        return next((x for x in found if x.get("address", "").upper() == address.upper()), None)
    good = sorted(found, key=lambda x: (x.get("sensorReadQuality", 0), x.get("rssi", -999)), reverse=True)
    return good[0] if good else None


def percent_of(sensor, tank_type):
    p = sensor.get("levelPercent")
    if p is None:
        p = (sensor.get("tankLevelPercent") or {}).get(tank_type)
    return None if p is None else float(p)


# --------------------------------------------------------------------------
# Working on the readings
# --------------------------------------------------------------------------
def median_at(samples, now, span=MEDIAN_S):
    vals = [p for t, p in samples if now - span <= t <= now]
    return statistics.median(vals) if vals else None


def gallons(pct, s):
    return None if pct is None else round(pct / 100 * CAPACITY.get(s["tank_type"], 500.0), 1)


def _date(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def add_reading(stack, s, pct, now, extra=None, alerts=None, hdd_of=None):
    """Take one reading (percent of the tank); returns messages. alerts(tag, title, message, priority) raises one."""
    alerts = alerts or (lambda *a: None)
    st = load(stack)
    st["samples"] = [x for x in st["samples"] if now - x[0] < KEEP_S] + [[now, round(pct, 2)]]
    st["last_reading"] = {"at": now, "percent": round(pct, 1), **(extra or {})}
    level = median_at(st["samples"], now)
    gal = gallons(level, s)
    msgs = []
    # the day: its first level, and the day before closed when the date changes
    today = _date(now)
    day = st["day"]
    if day.get("date") != today:
        if day.get("date") and day.get("start") is not None:
            used = max(0.0, day["start"] + day.get("filled", 0.0) - gal)
            st["days"] = (st["days"] + [{"date": day["date"], "used": round(used, 1), "filled": round(day.get("filled", 0.0), 1),
                                         "hdd": hdd_of(day["date"]) if hdd_of else None}])[-400:]
        st["day"] = day = {"date": today, "start": gal, "filled": 0.0}
    # fills: a rise of 10 points from the lowest level of the last 3 hours, logged once it has settled
    low = None
    if st["filling"] is None and level - min(p for _, p in st["samples"]) >= FILL_POINTS:   # cheap check first
        low = min((median_at(st["samples"], t) for t, _ in st["samples"] if t <= now - MEDIAN_S), default=None)
    if st["filling"] is None and low is not None and level - low >= FILL_POINTS:
        st["filling"] = {"from": low, "since": now, "peak": level, "peak_at": now}
    f = st["filling"]
    if f is not None:
        if level > f["peak"] + 0.5:
            f.update(peak=level, peak_at=now)
        elif now - f["peak_at"] >= FILL_SETTLE_S:
            before, after = gallons(f["from"], s), gallons(level, s)
            added = round(after - before, 1)
            st["fills"] = (st["fills"] + [{"at": now, "before": before, "after": after, "added": added,
                                           "percent_after": round(level, 1)}])[-50:]
            day["filled"] = day.get("filled", 0.0) + added
            st["filling"] = None
            st["samples"] = [x for x in st["samples"] if x[0] >= f["peak_at"]]       # the fill's rise is done
            msgs.append(f"filled: +{added:g} gal, now {level:.0f}%")
            alerts("propane_fill", "Propane filled", f"+{added:g} gal, now {level:.0f}% ({after:g} gal).", "info")
    # level alerts, with 2 points of hysteresis so a reading at the line doesn't flap
    was = st["alerts"].get("low")
    now_alert = ("critical" if level < s["critical"] else "warning" if level < s["warn"] else None)
    if now_alert is None and was and level < (s["critical"] if was == "critical" else s["warn"]) + 2:
        now_alert = was
    if now_alert != was:
        st["alerts"]["low"] = now_alert
        if now_alert:
            left = days_left(st, s, gal)
            extra_txt = f", about {left:.0f} days left" if left is not None else ""
            alerts("propane_low", "Propane low" if now_alert == "warning" else "Propane very low",
                   f"{level:.0f}% ({gal:g} gal){extra_txt}. Order propane.", now_alert)
        else:
            alerts("propane_low", "", "clear", "info")
    if st["alerts"].get("silent"):
        st["alerts"]["silent"] = False
        alerts("propane_silent", "", "clear", "info")
    save(stack, st)
    return msgs


def check_silent(stack, now, alerts=None):
    """No reading for 6 hours: one warning, cleared by the next reading."""
    st = load(stack)
    last = (st.get("last_reading") or {}).get("at")
    if last and now - last >= SILENT_S and not st["alerts"].get("silent"):
        st["alerts"]["silent"] = True
        save(stack, st)
        if alerts:
            alerts("propane_silent", "Propane sensor silent",
                   f"No reading from the tank sensor since {time.strftime('%b %d %H:%M', time.localtime(last))}.", "warning")
        return ["no reading for 6 hours"]
    return []


# --------------------------------------------------------------------------
# Use and days to empty
# --------------------------------------------------------------------------
def fit(days):
    """(base gal/day, gal per heating degree day) from the days that have degree days, or None."""
    pts = [(d["hdd"], d["used"]) for d in days if d.get("hdd") is not None]
    if len(pts) < 14:
        return None
    xs, ys = zip(*pts[-30:])
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    if vx < 1e-6:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx
    a = my - b * mx
    return max(0.0, a), max(0.0, b)


def expected_use(st, forecast_hdd=None):
    """Expected gallons per day over the coming week."""
    f = fit(st["days"])
    if f and forecast_hdd:
        a, b = f
        return a + b * (sum(forecast_hdd) / len(forecast_hdd))
    recent = [d["used"] for d in st["days"][-7:]]
    return sum(recent) / len(recent) if recent else None


def days_left(st, s, gal, forecast_hdd=None):
    use = expected_use(st, forecast_hdd)
    if gal is None or not use or use < 0.1:
        return None
    usable = gal - s["reserve"] / 100 * CAPACITY.get(s["tank_type"], 500.0)
    return max(0.0, usable / use)


def forecast_hdd(stack):
    """Heating degree days (65 °F) for the forecast's coming days, from state/wx-forecast.json."""
    fc = ss.load_json(os.path.join(stack, "state", "wx-forecast.json"), {})
    d = fc.get("daily") or {}
    hi, lo = d.get("temperature_2m_max") or [], d.get("temperature_2m_min") or []
    out = []
    for h, l in zip(hi, lo):
        if h is None or l is None:
            continue
        mean = (h + l) / 2
        mean_f = mean if fc.get("units_us") else mean * 9 / 5 + 32
        out.append(max(0.0, HDD_BASE_F - mean_f))
    return out[1:] or out


def hdd_from_vm(date):
    """Heating degree days of a past local date, from the hourly Open-Meteo temperature the collector keeps."""
    end = time.mktime(time.strptime(date, "%Y-%m-%d")) + 86400
    q = urllib.parse.urlencode({"query": "avg_over_time(site_openmeteo_temp_c[1d])", "time": int(end)})
    try:
        with urllib.request.urlopen(f"{VM}/api/v1/query?{q}", timeout=10) as r:
            res = json.loads(r.read())["data"]["result"]
        c = float(res[0]["value"][1])
    except (OSError, ValueError, KeyError, IndexError):
        return None
    return round(max(0.0, HDD_BASE_F - (c * 9 / 5 + 32)), 1)


def vm_push(s, pct, extra):
    """The level into VictoriaMetrics (Grafana, history)."""
    lines = [f"propane_level_percent {pct}", f"propane_gallons {gallons(pct, s)}"]
    for k, metric in (("tempCelsius", "propane_tank_temperature_celsius"), ("sensorReadQuality", "propane_read_quality"),
                      ("batteryVoltage", "propane_sensor_battery_volts")):
        if extra.get(k) is not None:
            lines.append(f"{metric} {extra[k]}")
    req = urllib.request.Request(f"{VM}/api/v1/import/prometheus", data=("\n".join(lines) + "\n").encode(), method="POST")
    urllib.request.urlopen(req, timeout=10).read()


def summary(stack, s, now=None):
    """What the pages, the card and Home Assistant show."""
    now = now or time.time()
    st = load(stack)
    lvl = median_at(st["samples"], now) if st["samples"] else None
    if lvl is None and st["samples"]:
        lvl = st["samples"][-1][1]
    gal = gallons(lvl, s)
    left = days_left(st, s, gal, forecast_hdd(stack))
    day = st["day"]
    today = max(0.0, day["start"] + day.get("filled", 0.0) - gal) if day.get("start") is not None and gal is not None else None
    last = st.get("last_reading") or {}
    return {"percent": None if lvl is None else round(lvl, 1), "gallons": gal,
            "days_left": None if left is None else round(left), "used_today": None if today is None else round(today, 1),
            "used_yesterday": st["days"][-1]["used"] if st["days"] else None, "last_fill": st["fills"][-1] if st["fills"] else None,
            "reading_age": None if not last.get("at") else round(now - last["at"]), "alert": st["alerts"].get("low"),
            "days": st["days"], "fills": st["fills"], "last_reading": last}
