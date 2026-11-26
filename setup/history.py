#!/usr/bin/env python3
"""History: each month against the same month a year earlier (the History page, /history), from per-day totals.

Per-day totals. The inverters' counters reset every night, so a month's total has to be added up day by day, and
days are cut at the site's midnight. The collector does that once per finished day and stores the result as its own
sample at the day's local noon (vmdays.py), so a year later it is one cheap read:

    site_day_solar_kwh, site_day_used_kwh, site_day_generator_kwh, site_day_generator_hours, site_day_low_soc

The first runs go back to the oldest inverter data the box has (BACKFILL_PER_RUN days at a time), so a box that
has been running for a while has its history at once. Propane's finished days (state/propane.json) are stored the
same way as propane_day_used_gal; water and boiler already store theirs.

The page. For each of the last 12 months: the month's total, the same month a year earlier, and the change. The
month under way is compared with the same days of that month last year (the 1st to today's date), never with the
whole month. A month counts only the days that have data, and says so when days are missing on either side.
"""
import datetime as dt
import json
import os
import time
import urllib.parse
import urllib.request

import solar_setup as ss
import vmdays

VM = os.environ.get("SOLAR_VM", "http://127.0.0.1:8428")
BACKFILL_PER_RUN = 90
KEY = "site"
PEAK_KEY, PEAK_UNTIL = "site-peak", "site-peak-until"   # filling in the highest load for days stored before it existed
WEATHER_MARKER = "openmeteo-wind-backfilled"     # written by the collector once Open-Meteo's wind history is in
HDD_BASE_F = 65.0
STATION_MIN_SAMPLES = 200                        # a station's day counts with this many readings (it sends one a minute)
# (id, title, the day series, unit, digits, the module that has to be on, or None)
# how days add up to a month: "sum", "avg" (the mean of the days) or "max"; how the change is said: "pct" or "diff"
METRICS = (
    ("solar", "Solar", "site_day_solar_kwh", "kWh", 0, None, "sum", "pct"),
    ("used", "Used", "site_day_used_kwh", "kWh", 0, None, "sum", "pct"),
    ("peak", "Highest load", "site_day_peak_load_kw", "kW", 1, None, "max", "pct"),
    ("generator", "Generator", "site_day_generator_hours", "h", 1, None, "sum", "pct"),
    ("propane", "Propane", "propane_day_used_gal", "gal", 0, "propane", "sum", "pct"),
    ("water", "Water", "water_day_used_gal", "gal", 0, "water", "sum", "pct"),
    ("temp", "Outdoor temperature, average", "site_day_temp_avg_c", "°C", 0, None, "avg", "diff"),
    ("hdd", "Heating degree days", "site_day_hdd", "°F·days", 0, None, "sum", "pct"),
    ("wind", "Wind, average", "site_day_wind_avg_ms", "m/s", 1, None, "avg", "pct"),
    ("gust", "Strongest gust", "site_day_gust_max_ms", "m/s", 0, None, "max", "diff"),
)
L_PER_GAL = 3.78541


# --------------------------------------------------------------------------
# Storing finished days (the collector, hourly)
# --------------------------------------------------------------------------
def _query(q, ts=None):
    p = {"query": q}
    if ts is not None:
        p["time"] = f"{ts:.0f}"
    with urllib.request.urlopen(f"{VM}/api/v1/query?{urllib.parse.urlencode(p)}", timeout=30) as r:
        return json.loads(r.read())["data"]["result"]


def first_data(now=None):
    """Unix time of the oldest inverter reading (within 5 years), or None."""
    res = _query("min(tfirst_over_time(lxp_e_pv_day[5y]))", now)      # on the stored samples: a subquery with day steps finds nothing
    return float(res[0]["value"][1]) if res else None


def site_lines(st):
    """One finished day's samples from display.day_stats(); a figure the day has no data for is left out."""
    names = (("solar", "site_day_solar_kwh"), ("used", "site_day_used_kwh"), ("gen_kwh", "site_day_generator_kwh"),
             ("gen_hours", "site_day_generator_hours"), ("low_soc", "site_day_low_soc"))
    return [f"{name} {st[k]:.3f}" for k, name in names if st.get(k) is not None] + peak_lines(st)


def peak_lines(st):
    return [] if st.get("peak_load") is None else [f"site_day_peak_load_kw {st['peak_load'] / 1000:.3f}"]


def push_peak_days(stack, tzname, now=None, limit=BACKFILL_PER_RUN, peak=None, first=None, send=None):
    """The day's highest load for the days stored before it was recorded (boxes from before v0.2.58): from the
    oldest data up to the day before the first one stored with it. Returns how many days were looked at."""
    from zoneinfo import ZoneInfo
    import display
    tz = ZoneInfo(tzname)
    now = now or time.time()
    sent = ss.load_json(vmdays._path(stack), {})
    if sent.get(PEAK_KEY) == "done":
        return 0
    if not sent.get(KEY):                               # nothing stored yet: every day will get its peak as it is stored
        vmdays.mark(stack, PEAK_KEY, "done")
        return 0
    if sent.get(PEAK_KEY):
        day = dt.date.fromisoformat(sent[PEAK_KEY]) + dt.timedelta(days=1)
    else:
        t0 = (first or first_data)(now)
        if t0 is None:
            return 0
        day = dt.datetime.fromtimestamp(t0, tz).date()
        sent[PEAK_UNTIL] = sent[KEY]                    # the days up to here were stored without it
        ss.write_if_changed(vmdays._path(stack), json.dumps(sent, indent=1, sort_keys=True) + "\n", 0o644)
    until = dt.date.fromisoformat(sent[PEAK_UNTIL])
    peak = peak or (lambda a, b: display.vm_at(f"max_over_time(({display.INSTANT_RAW_LOAD})[{int(b - a)}s:1m])", b))
    lines, n, last = [], 0, None
    while day <= until and n < limit:
        start = dt.datetime.combine(day, dt.time(0), tz).timestamp()
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), tz).timestamp() - 1
        noon = int(dt.datetime.combine(day, dt.time(12), tz).timestamp() * 1000)
        lines += [f"{line} {noon}" for line in peak_lines({"peak_load": peak(start, end)})]
        last, n, day = day, n + 1, day + dt.timedelta(days=1)
    if lines:
        (send or vmdays.post)(lines)
    vmdays.mark(stack, PEAK_KEY, "done" if day > until else last.isoformat())
    return n


def weather_day_stats(start, end, query=None):
    """One local day's weather from what the box recorded: {"temp_high", "temp_low", "temp_avg" (°C), "hdd" (°F
    days under 65 °F, as the propane and boiler figures use), "wind_avg", "gust_max" (m/s)}; a figure without data
    is None. A weather station's readings are used when the day has enough of them, else Open-Meteo's."""
    query = query or _query
    w = f"[{max(int(end - start), 60)}s]"

    def one(q):
        res = query(q, end)
        return float(res[0]["value"][1]) if res else None

    def pick(fn, station, om):
        n = one(f"max(count_over_time({station}{w}))")
        v = one(f"{fn}({fn}_over_time({station}{w}))") if n and n >= STATION_MIN_SAMPLES else None
        return v if v is not None else one(f"{fn}_over_time({om}{w})")
    out = {"temp_high": pick("max", "site_outdoor_temp_c", "site_openmeteo_temp_c"),
           "temp_low": pick("min", "site_outdoor_temp_c", "site_openmeteo_temp_c"),
           "temp_avg": pick("avg", "site_outdoor_temp_c", "site_openmeteo_temp_c"),
           "wind_avg": pick("avg", "site_wind_speed_ms", "site_openmeteo_wind_ms"),
           "gust_max": pick("max", "site_wind_gust_ms", "site_openmeteo_gust_ms")}
    out["hdd"] = None if out["temp_avg"] is None else max(0.0, HDD_BASE_F - (out["temp_avg"] * 9 / 5 + 32))
    return out


def weather_lines(st):
    names = (("temp_high", "site_day_temp_high_c"), ("temp_low", "site_day_temp_low_c"), ("temp_avg", "site_day_temp_avg_c"),
             ("hdd", "site_day_hdd"), ("wind_avg", "site_day_wind_avg_ms"), ("gust_max", "site_day_gust_max_ms"))
    return [f"{name} {st[k]:.2f}" for k, name in names if st.get(k) is not None]


def push_site_days(stack, tzname, now=None, limit=BACKFILL_PER_RUN, stats=None, first=None, send=None, weather=None):
    """Store the finished days not stored yet, oldest first, at most `limit` a run. Returns how many."""
    from zoneinfo import ZoneInfo
    import display
    tz = ZoneInfo(tzname)
    now = now or time.time()
    today = dt.datetime.fromtimestamp(now, tz).date()
    sent = ss.load_json(vmdays._path(stack), {}).get(KEY)
    if sent:
        day = dt.date.fromisoformat(sent) + dt.timedelta(days=1)
    else:
        t0 = (first or first_data)(now)
        if t0 is None:
            return 0
        day = dt.datetime.fromtimestamp(t0, tz).date()
    lines, n, last = [], 0, None
    while day < today and n < limit:
        start = dt.datetime.combine(day, dt.time(0), tz)
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), tz)
        st = (stats or display.day_stats)(start.timestamp(), end.timestamp() - 1)
        noon = int(dt.datetime.combine(day, dt.time(12), tz).timestamp() * 1000)
        if st.get("solar") is not None or st.get("used") is not None:      # a day with no inverter data has no samples
            st = {**st, "gen_hours": st.get("gen_hours") or 0.0}           # no generator reading that day: it didn't run
            lines += [f"{line} {noon}" for line in site_lines(st)]
        lines += [f"{line} {noon}" for line in weather_lines((weather or weather_day_stats)(start.timestamp(), end.timestamp() - 1))]
        last, n, day = day, n + 1, day + dt.timedelta(days=1)
    if lines:
        (send or vmdays.post)(lines)
    if last:
        vmdays.mark(stack, KEY, last.isoformat())
    return n


def push_propane_days(stack, send=None):
    """Propane's finished days (kept by the propane module) as propane_day_used_gal."""
    days = {d["date"]: d for d in ss.load_json(os.path.join(stack, "state", "propane.json"), {}).get("days") or [] if d.get("date")}
    return vmdays.push(stack, "propane-days", days, lambda date, d: [f"propane_day_used_gal {float(d.get('used') or 0):g}"],
                       send=send)


def collect(stack):
    """The collector's hourly job. Returns log lines."""
    site = ss.load_json(os.path.join(stack, "state", "site.json"), {})
    out = []
    if site.get("latitude") and not os.path.exists(os.path.join(stack, "state", WEATHER_MARKER)):
        return out          # Open-Meteo's temperature and wind history isn't in yet: the days would be stored without it
    tzname = site.get("time_zone") or ss.host_timezone()
    push_peak_days(stack, tzname)                       # before the new days: it notes how far the old ones go
    n = push_site_days(stack, tzname)
    if n:
        out.append(f"history: {n} day{'s' if n != 1 else ''} stored")
    try:
        push_propane_days(stack)
    except OSError as e:
        out.append(f"history: propane days not stored: {e}")
    return out


# --------------------------------------------------------------------------
# Reading: months against a year earlier
# --------------------------------------------------------------------------
def fetch_days(series, tz, now=None, months=25):
    """{date: value} of a day series for the last `months` months (several series, such as water's by source, are
    added up per day)."""
    now = now or time.time()
    q = urllib.parse.urlencode({"match[]": series, "start": f"{now - months * 31 * 86400:.0f}", "end": f"{now:.0f}"})
    out = {}
    with urllib.request.urlopen(f"{VM}/api/v1/export?{q}", timeout=30) as r:
        for line in r.read().decode().splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            for ts, v in zip(d.get("timestamps", []), d.get("values", [])):
                day = dt.datetime.fromtimestamp(ts / 1000, tz).date()
                out[day] = out.get(day, 0.0) + float(v)
    return out


def _month_days(year, month):
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    return (nxt - dt.date(year, month, 1)).days


AGG = {"sum": sum, "avg": lambda v: sum(v) / len(v), "max": max}
WORDS = {"temp": ("warmer", "colder"), "gust": ("stronger", "weaker")}


def compare(days, today, months=12, agg="sum", show="pct"):
    """[{"year", "month", "value", "days", "of", "before", "days_before", "change", "partial"}] newest first, for
    the last `months` months. "of": how many days the period has (the month under way: up to yesterday); "days":
    how many of them have data. "before": the same days a year earlier, None without any. The days of a period
    are added up, averaged or their highest taken (agg). "change": percent (show "pct"; None when the earlier one
    is 0) or the plain difference (show "diff", for temperatures); None when either side is missing."""
    out = []
    y, m = today.year, today.month
    for _ in range(months):
        partial = (y, m) == (today.year, today.month)
        upto = today.day - 1 if partial else _month_days(y, m)          # today isn't finished: not counted
        cur = [days[d] for d in (dt.date(y, m, i) for i in range(1, upto + 1)) if d in days]
        prev_len = min(upto, _month_days(y - 1, m))
        prev = [days[d] for d in (dt.date(y - 1, m, i) for i in range(1, prev_len + 1)) if d in days]
        value, before = (AGG[agg](cur) if cur else None), (AGG[agg](prev) if prev else None)
        change = None
        if show == "diff" and value is not None and before is not None:
            change = value - before
        elif show == "pct" and value is not None and before:
            change = round((value - before) / before * 100)
        out.append({"year": y, "month": m, "value": value, "days": len(cur), "of": upto, "before": before,
                    "days_before": len(prev), "change": change, "partial": partial})
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out


def totals(rows):
    """The 12 months together, counting only months that have both years: (value, before, change %) or None."""
    both = [r for r in rows if r["value"] is not None and r["before"] is not None]
    if not both:
        return None
    v, b = sum(r["value"] for r in both), sum(r["before"] for r in both)
    return v, b, (round((v - b) / b * 100) if b else None), len(both)


def build(stack, site, now=None, fetch=None):
    """Everything the page shows: {"metrics": [{"id", "title", "unit", "digits", "rows", "total", "since"}], "today"}.
    A metric without any data is left out (a module that is off, a box without that sensor)."""
    import modules
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(site.get("time_zone") or ss.host_timezone())
    now = now or time.time()
    today = dt.datetime.fromtimestamp(now, tz).date()
    us = site.get("units", "us_customary") == "us_customary"
    out = []
    for mid, title, series, unit, digits, module, agg, show in METRICS:
        if module:
            try:
                if not modules.load(stack, module)["enabled"]:
                    continue
            except Exception:  # noqa: BLE001 - a module's broken file leaves its figures out
                continue
        days = (fetch or fetch_days)(series, tz, now)
        if not days or (agg == "sum" and mid != "hdd" and not any(days.values())):
            continue                                # nothing stored, or never anything but 0 (a site without a generator)
        scale, shift = 1.0, 0.0                     # the stored units (SI, gallons) into the site's
        if unit == "gal" and not us:
            scale, unit = L_PER_GAL, "L"
        elif unit == "°C" and us:
            scale, shift, unit = 1.8, 32.0, "°F"
        elif unit == "°F·days" and not us:
            scale, unit = 1 / 1.8, "°C·days"
        elif unit == "m/s":
            scale, unit = (2.23694, "mph") if us else (3.6, "km/h")
        days = {d: v * scale + shift for d, v in days.items()}
        rows = compare(days, today, agg=agg, show=show)
        out.append({"id": mid, "title": title, "unit": unit, "digits": digits, "rows": rows, "show": show,
                    "total": totals(rows) if agg == "sum" else None, "summed": agg == "sum", "since": min(days)})
    return {"metrics": out, "today": today}


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------
def _num(v, digits):
    return "&ndash;" if v is None else f"{v:,.{digits}f}"


def _change(c, show="pct", unit="", words=("more", "less")):
    if c is None:
        return ""
    if show == "diff":
        n = round(abs(c))
        return "the same" if n == 0 else f"{n} {unit} {words[0] if c > 0 else words[1]}"
    return f"{abs(c)}% {words[0] if c > 0 else words[1]}" if c else "the same"


def render(data):
    import html
    e = html.escape
    if not data["metrics"]:
        return ("<h1>History</h1><div class='card'>No finished day has been stored yet. The first days appear within an "
                "hour of the box having inverter data, and a comparison with last year once there is a year of it.</div>")
    parts = ["<h1>History</h1><p><small>Each month against the same month a year earlier, with the weather beside the energy: a colder or windier month explains more propane or generator time. The month under way is compared "
             "with the same days last year. Only days with data are counted; a month that misses days says so.</small></p>"]
    for m in data["metrics"]:
        rows = []
        for r in m["rows"]:
            name = dt.date(r["year"], r["month"], 1).strftime("%B %Y") + (" (so far)" if r["partial"] else "")
            miss = []
            if r["value"] is not None and r["days"] < r["of"]:
                miss.append(f"{r['days']} of {r['of']} days this year")
            if r["before"] is not None and r["days_before"] < min(r["of"], _month_days(r["year"] - 1, r["month"])):
                miss.append(f"{r['days_before']} days a year earlier")
            note = f" <small>({e(', '.join(miss))})</small>" if miss else ""
            rows.append(f"<tr><td>{e(name)}</td><td class='n'>{_num(r['value'], m['digits'])}</td>"
                        f"<td class='n'>{_num(r['before'], m['digits'])}</td><td>{e(_change(r['change'], m.get('show', 'pct'), m['unit'], WORDS.get(m['id'], ('more', 'less'))))}{note}</td></tr>")
        foot = ""
        if m["total"]:
            v, b, c, n = m["total"]
            foot = (f"<tr class='total'><td><b>{n} month{'s' if n != 1 else ''} with both years</b></td><td class='n'><b>{_num(v, m['digits'])}</b></td>"
                    f"<td class='n'><b>{_num(b, m['digits'])}</b></td><td><b>{e(_change(c))}</b></td></tr>")
        elif any(r["before"] is not None for r in m["rows"]):
            foot = ""                                # an average or a peak: no total over the months
        else:
            foot = (f"<tr><td colspan='4'><small>Nothing from a year earlier yet: this box has {e(m['title'].lower())} data since "
                    f"{m['since'].strftime('%B %-d, %Y')}.</small></td></tr>")
        parts.append(f"<h2>{e(m['title'])} ({e(m['unit'])})</h2><table class='hist'><tr><th>Month</th><th class='n'>{'Total' if m.get('summed', True) else 'This year'}</th>"
                     f"<th class='n'>A year earlier</th><th>Change</th></tr>{''.join(rows)}{foot}</table>")
    return "".join(parts)
