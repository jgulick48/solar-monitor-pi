#!/usr/bin/env python3
"""The house's expected use, hour by hour: a weekly profile instead of a flat average.

Use follows the week (workdays, laundry day, weekends away) and the clock, so "the average of the last 3 days" is a
poor guess for tonight. From the hourly load in VictoriaMetrics:

  * the profile: for each of the 168 hours of the week (site time), the median load in that hour over the last
    8 weeks. The median ignores one-off days;
  * the level: how the last 3 days compare with what the profile expected for those same hours (0.6 to 1.6), so
    guests or a cold snap raise the whole profile at once;
  * expected load at a time = profile for its hour of the week x level.

It has to earn its place: build() also backtests both methods on the site's own history (for each of the last 28
days, at 4 PM: predict the use until 9 the next morning from what was known then, and compare with what happened:
the night the generator advice has to get right) and the box uses the profile only when its error is lower. A
site with no weekly rhythm can still gain from the profile's shape through the day. With under 2 weeks of history, or too few days to test, it stays
on the flat average.

The collector rebuilds it every hour (state/load-profile.json); display.py reads it through predictor().
"""
import datetime as dt
import json
import os
import statistics
import time
import urllib.parse
import urllib.request

import solar_setup as ss

VM = os.environ.get("SOLAR_VM", "http://127.0.0.1:8428")
LOAD = "avg_over_time((sum(lxp_p_load) or sum(lxp_p_eps))[1h:5m])"   # the mean of the hour ending at each sample
HISTORY_DAYS = 84          # fetched: 8 weeks for the profile, and the backtest's earlier days need their own
PROFILE_DAYS = 56
LEVEL_HOURS = 72
LEVEL_RANGE = (0.6, 1.6)
TEST_DAYS = 28
TEST_HOUR = 16             # the backtest predicts from 4 PM, when the generator advice matters,
TEST_SPAN_H = 17           # until 9 the next morning, when the sun is carrying the house again
MIN_DAYS = 14              # of history before a profile is trusted
MIN_TESTS = 7
MAX_AGE_S = 3 * 86400      # a profile older than this isn't used (the collector stopped)


def path(stack):
    return os.path.join(stack, "state", "load-profile.json")


def how(t):
    """Hour of the week of a local datetime: 0 = Monday 00:00-01:00 ... 167 = Sunday 23:00-24:00."""
    return t.weekday() * 24 + t.hour


def fetch(now=None, days=HISTORY_DAYS):
    """{end of hour (Unix s): mean load in W} for the last `days`."""
    end = int(now or time.time()) // 3600 * 3600
    q = urllib.parse.urlencode({"query": LOAD, "start": end - days * 86400, "end": end, "step": 3600})
    with urllib.request.urlopen(f"{VM}/api/v1/query_range?{q}", timeout=30) as r:
        res = json.loads(r.read())["data"]["result"]
    return {int(t): float(v) for t, v in (res[0]["values"] if res else []) if float(v) >= 0}


def _slot(ts, tz):
    """The hour of the week a sample belongs to: it is the mean of the hour that ended at ts."""
    return how(dt.datetime.fromtimestamp(ts - 1800, tz))


def profile(hours, tz, until, days=PROFILE_DAYS):
    """168 values in W from the samples in (until - days, until]: each slot's median; a slot without samples gets
    the median of its hour of the day, or of everything. None with no samples at all."""
    slots = [[] for _ in range(168)]
    for ts, w in hours.items():
        if until - days * 86400 < ts <= until:
            slots[_slot(ts, tz)].append(w)
    every = [w for s in slots for w in s]
    if not every:
        return None
    out = []
    for i, s in enumerate(slots):
        if s:
            out.append(statistics.median(s))
            continue
        same_hour = [w for d in range(7) for w in slots[d * 24 + i % 24]]
        out.append(statistics.median(same_hour or every))
    return out


def level(hours, prof, tz, until, span=LEVEL_HOURS):
    """How the last `span` hours compare with the profile for those hours, within LEVEL_RANGE (1.0 without data)."""
    got = [(w, prof[_slot(ts, tz)]) for ts, w in hours.items() if until - span * 3600 < ts <= until]
    want = sum(p for _, p in got)
    if len(got) < 24 or want <= 0:
        return 1.0
    return min(max(sum(w for w, _ in got) / want, LEVEL_RANGE[0]), LEVEL_RANGE[1])


def backtest(hours, tz, now):
    """Both methods' mean error (kWh from 4 PM to 9 the next morning) on the last TEST_DAYS nights, each predicted
    with only what was known at 4 PM. {"days", "mae_flat_kwh", "mae_weekly_kwh"}."""
    today = dt.datetime.fromtimestamp(now, tz).replace(hour=TEST_HOUR, minute=0, second=0, microsecond=0)
    err_flat, err_week = [], []
    for back in range(1, TEST_DAYS + 2):
        cut = int((today - dt.timedelta(days=back)).timestamp())
        span = range(cut + 3600, cut + TEST_SPAN_H * 3600 + 1, 3600)
        if span[-1] > now:
            continue
        actual = [hours[t] for t in span if t in hours]
        before = [w for ts, w in hours.items() if cut - LEVEL_HOURS * 3600 < ts <= cut]
        known = [ts for ts in hours if ts <= cut]
        if len(actual) < TEST_SPAN_H - 2 or len(before) < 24 or not known or cut - min(known) < MIN_DAYS * 86400:
            continue
        actual_kwh = sum(actual) / len(actual) * TEST_SPAN_H / 1000
        flat_kwh = sum(before) / len(before) * TEST_SPAN_H / 1000
        prof = profile(hours, tz, cut)
        lvl = level(hours, prof, tz, cut)
        week_kwh = sum(prof[_slot(t, tz)] for t in span) * lvl / 1000
        err_flat.append(abs(flat_kwh - actual_kwh))
        err_week.append(abs(week_kwh - actual_kwh))
    if not err_flat:
        return {"days": 0, "mae_flat_kwh": None, "mae_weekly_kwh": None}
    return {"days": len(err_flat), "mae_flat_kwh": round(sum(err_flat) / len(err_flat), 2),
            "mae_weekly_kwh": round(sum(err_week) / len(err_week), 2)}


def build(stack, tzname, now=None, hours=None):
    """Rebuild state/load-profile.json. Returns the document (use: "weekly" or "flat", with the reason)."""
    from zoneinfo import ZoneInfo
    now = int(now or time.time())
    tz = ZoneInfo(tzname)
    hours = fetch(now) if hours is None else hours
    until = now // 3600 * 3600
    doc = {"built": now, "time_zone": tzname, "use": "flat", "why": "", "days": 0, "level": 1.0, "profile_w": None}
    if hours:
        doc["days"] = round((until - min(hours)) / 86400, 1)
    prof = profile(hours, tz, until) if hours else None
    if not prof:
        doc["why"] = "no load history yet"
    else:
        doc["profile_w"] = [round(w) for w in prof]
        doc["level"] = round(level(hours, prof, tz, until), 3)
        bt = backtest(hours, tz, now)
        doc.update(tested_days=bt["days"], mae_flat_kwh=bt["mae_flat_kwh"], mae_weekly_kwh=bt["mae_weekly_kwh"])
        if doc["days"] < MIN_DAYS:
            doc["why"] = f"only {doc['days']:.0f} days of history (needs {MIN_DAYS})"
        elif bt["days"] < MIN_TESTS:
            doc["why"] = f"only {bt['days']} days to test it on (needs {MIN_TESTS})"
        elif bt["mae_weekly_kwh"] >= bt["mae_flat_kwh"]:
            doc["why"] = (f"the weekly pattern predicted no better here: off by {bt['mae_weekly_kwh']} kWh a night "
                          f"against {bt['mae_flat_kwh']} for the 3-day average")
        else:
            doc["use"] = "weekly"
            doc["why"] = (f"off by {bt['mae_weekly_kwh']} kWh a night against {bt['mae_flat_kwh']} for the 3-day "
                          f"average, over {bt['days']} nights")
    ss.write_if_changed(path(stack), json.dumps(doc) + "\n", 0o644)
    return doc


def load(stack):
    return ss.load_json(path(stack), {})


def predictor(stack, tzname, flat_w, now=None):
    """(fn, method): fn(local datetime) -> expected load in kW. The weekly profile when the last build chose it, it
    is recent and for this time zone; otherwise the flat average flat_w (W) at every hour. fn is None without
    either."""
    doc = load(stack)
    prof = doc.get("profile_w")
    if (doc.get("use") == "weekly" and prof and len(prof) == 168 and doc.get("time_zone") == tzname
            and (now or time.time()) - doc.get("built", 0) <= MAX_AGE_S):
        lvl = doc.get("level") or 1.0
        return (lambda t: prof[how(t)] * lvl / 1000), "weekly"
    if flat_w and flat_w > 0:
        return (lambda t: flat_w / 1000), "flat"
    return None, "none"


def energy_kwh(fn, start, end):
    """Expected use between two local datetimes, in kWh (hour steps, part hours counted in proportion)."""
    total, t = 0.0, start
    while t < end:
        nxt = min((t + dt.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0), end)
        total += fn(t) * (nxt - t).total_seconds() / 3600
        t = nxt
    return total
