"""Finished days into VictoriaMetrics, once each, for Grafana's per-day bars.

A module's "today" figures reset at local midnight, but Grafana cuts days at UTC midnight (17:00 in Arizona), so a
bar per day built from them mixes two days. Instead, each finished day is written once as its own sample, stamped
at that day's local noon: any day-wide window then holds exactly one sample per day. The first push after a module
starts sends every day it still keeps, so the bars have history at once.

  state/vm-days.json   {"<module id>": "<last date sent>"}
"""
import json
import os
import time
import urllib.request

import solar_setup as ss

VM = os.environ.get("SOLAR_VM", "http://127.0.0.1:8428")


def _path(stack):
    return os.path.join(stack, "state", "vm-days.json")


def noon_ms(date):
    """A date's local noon, in ms since the epoch."""
    t = time.strptime(date + " 12:00", "%Y-%m-%d %H:%M")
    return int(time.mktime(t) * 1000)


def pending(stack, key, dates, now=None):
    """The finished days (before today) not yet sent, oldest first."""
    today = time.strftime("%Y-%m-%d", time.localtime(now or time.time()))
    last = ss.load_json(_path(stack), {}).get(key)
    return [d for d in sorted(dates) if d < today and (last is None or d > last)]


def mark(stack, key, date):
    sent = ss.load_json(_path(stack), {})
    sent[key] = date
    ss.write_if_changed(_path(stack), json.dumps(sent, indent=1, sort_keys=True) + "\n", 0o644)


def post(lines):
    req = urllib.request.Request(f"{VM}/api/v1/import/prometheus", data=("\n".join(lines) + "\n").encode(), method="POST")
    urllib.request.urlopen(req, timeout=20).read()


def push(stack, key, days, lines_for, now=None, send=None):
    """Send the finished days of `days` ({date: record}) not sent yet. lines_for(date, record) -> ["name{labels} value"];
    each line gets the day's noon timestamp. Returns the number of days sent; nothing is marked sent if posting fails
    (it raises, and the next push tries again)."""
    todo = pending(stack, key, days, now)
    if not todo:
        return 0
    lines = []
    for d in todo:
        ts = noon_ms(d)
        lines += [f"{line} {ts}" for line in lines_for(d, days[d])]
    if lines:
        (send or post)(lines)
    mark(stack, key, todo[-1])
    return len(todo)
