#!/usr/bin/env python3
"""Monthly report: one notification and a page (/report) for each finished month.

What it says: solar made, power used, the highest load, the lowest battery level, generator hours with fuel and
cost, propane and water when those modules are on, the average outdoor temperature, each beside the same month a
year earlier once there is a year of data. And, as they stand on the day the report is made: battery health and
the last backup.

The month's figures are the History page's (history.py: the per-day totals, added up), so the two always agree.
Generator fuel and cost come from the generator log's runs that started in the month (genlog.py).

The collector calls step() every hour. The report for last month is made once all of its days are stored, and sent
at or after 8:00 on the first days of the month (SEND_DAYS): a box installed or updated later in a month makes the
report for the page but doesn't send a stale one. Reports are kept in state/report.json (the last KEEP), so the
battery and backup lines of an old report stay as they were. An older month without a stored report is worked out
when its page is opened, without those two lines.
"""
import datetime as dt
import html
import json
import os
import re
import time

import history
import solar_setup as ss
import vmdays

TAG = "monthly_report"
SEND_FROM_HOUR = 8
SEND_DAYS = 7
KEEP = 24
PAGE_MONTHS = 12
MONTH_RE = re.compile(r"^(20\d\d)-(0[1-9]|1[0-2])$")
SHORT = {"solar": "Solar", "used": "used", "peak": "highest load", "low_soc": "lowest battery", "propane": "propane",
         "water": "water", "temp": "average outdoor temperature"}
IN_TEXT = ("solar", "used", "peak", "low_soc", "propane", "water")          # the notification's figures, in order
WITH_CHANGE = ("solar", "used", "propane", "water")                         # ... and the ones said with their change


def _path(stack):
    return os.path.join(stack, "state", "report.json")


def load(stack):
    st = ss.load_json(_path(stack), {})
    st.setdefault("reports", {})
    return st


def key(year, month):
    return f"{year:04d}-{month:02d}"


def previous(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _tz(site):
    from zoneinfo import ZoneInfo
    return ZoneInfo(site.get("time_zone") or ss.host_timezone())


def build(stack, site, year, month, fetch=None, runs=None):
    """One finished month: {"month", "rows": [{"id", "title", "unit", "digits", "show", "value", "before", "change",
    "days", "of", "days_before"}], "generator": genlog.totals() of the month's runs or None}. Rows without data
    that month are left out."""
    tz = _tz(site)
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    after = dt.datetime.combine(nxt, dt.time(12), tz).timestamp()          # seen from the next month's first day
    rows = []
    for m in history.build(stack, site, now=after, fetch=fetch)["metrics"]:
        r = next((r for r in m["rows"] if (r["year"], r["month"]) == (year, month)), None)
        if r is None or r["value"] is None:
            continue
        rows.append({"id": m["id"], "title": m["title"], "unit": m["unit"], "digits": m["digits"], "show": m["show"],
                     **{k: r[k] for k in ("value", "before", "change", "days", "of", "days_before")}})
    return {"month": key(year, month), "rows": rows, "generator": generator(stack, site, year, month, tz, runs)}


def generator(stack, site, year, month, tz, runs=None):
    """The generator log's runs that started in the month, with fuel and cost; None when the log has none."""
    import genlog
    if runs is None:
        runs = genlog.load(stack)["runs"] if os.path.exists(genlog.path(stack)) else []
    mine = [r for r in runs if (lambda d: (d.year, d.month))(dt.datetime.fromtimestamp(r["start"], tz)) == (year, month)]
    return genlog.totals(mine, site) if mine else None


def snapshot(stack, site, now=None):
    """What can only be said on the day: {"battery": {"level", "notes"} or None, "backup": {"last_ok", "snapshots",
    "result"} or None (the backup module is off)}."""
    out = {"battery": None, "backup": None}
    try:
        import battery
        b = battery.status(stack, site, now)
        if b["level"] != "unknown":
            out["battery"] = {"level": b["level"], "notes": [t for lvl, t in b["findings"] if lvl != "ok"],
                              "soh": b.get("soh"), "capacity_kwh": b.get("capacity_kwh")}
    except Exception:  # noqa: BLE001 - the report goes without its battery line
        pass
    try:
        import backup
        import modules
        if modules.load(stack, "backup")["enabled"]:
            st = ss.load_json(backup.status_path(stack), {})
            out["backup"] = {"last_ok": st.get("last_ok"), "snapshots": st.get("snapshots"), "result": st.get("result")}
    except Exception:  # noqa: BLE001
        pass
    return out


# --------------------------------------------------------------------------
# Saying it
# --------------------------------------------------------------------------
def month_name(k):
    y, m = MONTH_RE.match(k).groups()
    return dt.date(int(y), int(m), 1).strftime("%B %Y")


def amount(r):
    return f"{r['value']:,.{r['digits']}f}{'' if r['unit'] == '%' else ' '}{r['unit']}"


def change(r):
    return history._change(r["change"], r["show"], r["unit"], history.WORDS.get(r["id"], ("more", "less")))


def battery_line(b):
    if not b:
        return ""
    if b["level"] == "ok":
        return "Battery health: good."
    return ("Battery health needs attention: " if b["level"] == "warn" else "Battery health, worth watching: ") + " ".join(b["notes"][:1])


def backup_line(b, tz=None):
    if not b:
        return ""
    if not b.get("last_ok"):
        return "Backups: none has succeeded yet. Check the backup stick."
    when = dt.datetime.fromtimestamp(b["last_ok"], tz).strftime("%B %-d")
    n = b.get("snapshots")
    return f"Last backup: {when}" + (f" ({n} on the stick)." if n else ".")


def generator_line(rep):
    g = rep.get("generator")
    hours = next((r for r in rep["rows"] if r["id"] == "generator"), None)
    if g:
        s = f"Generator: {g['hours']:g} h in {g['runs']} run{'s' if g['runs'] != 1 else ''}"
        if g["fuel"] is not None:
            s += f", about {g['fuel']:g} gal of fuel" + (f" (${g['cost']:,.2f})" if g["cost"] is not None else "")
        return s + "."
    if hours is None:
        return ""                                    # a site that has never run a generator
    return "The generator didn't run." if not hours["value"] else f"Generator: {hours['value']:.1f} h."


def text(rep, tz=None):
    """The notification's message."""
    by = {r["id"]: r for r in rep["rows"]}
    parts = []
    for i in IN_TEXT:
        r = by.get(i)
        if r is None:
            continue
        c = change(r) if i in WITH_CHANGE and r["before"] is not None else ""
        parts.append(f"{SHORT[i]} {amount(r)}" + (f" ({c} than a year earlier)" if c and c != "the same" else
                                                 " (the same as a year earlier)" if c else ""))
    out = [", ".join(parts) + "."] if parts else []
    lead = by.get("solar") or by.get("used")
    if lead and lead["days"] < lead["of"]:
        out.append(f"{lead['days']} of the month's {lead['of']} days {'has' if lead['days'] == 1 else 'have'} data.")
    out += [x for x in (generator_line(rep), battery_line(rep.get("battery")), backup_line(rep.get("backup"), tz)) if x]
    return " ".join(out)


def title(rep):
    return f"Solar report for {month_name(rep['month'])}"


# --------------------------------------------------------------------------
# The collector, hourly
# --------------------------------------------------------------------------
def step(stack, now=None, alert=None, make=None, snap=None):
    """Make last month's report once its days are stored, and send it. Returns log lines."""
    now = now or time.time()
    site = ss.load_json(os.path.join(stack, "state", "site.json"), {})
    tz = _tz(site)
    local = dt.datetime.fromtimestamp(now, tz)
    y, m = previous(local.year, local.month)
    k = key(y, m)
    st = load(stack)
    if st.get("done") == k:
        return []
    last_day = dt.date(local.year, local.month, 1) - dt.timedelta(days=1)
    if (ss.load_json(vmdays._path(stack), {}).get(history.KEY) or "") < last_day.isoformat():
        return []                                    # the month's last days aren't stored yet
    sending = site.get("report_enabled", True) and local.day <= SEND_DAYS
    if sending and local.hour < SEND_FROM_HOUR:
        return []
    rep = (make or build)(stack, site, y, m)
    if not rep["rows"]:                              # the box has nothing from that month
        st["done"] = k
        _save(stack, st)
        return []
    rep.update((snap or snapshot)(stack, site, now))
    rep["made"] = int(now)
    out = [f"report: {month_name(k)} made"]
    if sending:
        if alert is None:
            import notify
            alert = lambda *a: notify.alert(stack, *a)  # noqa: E731
        err = alert(TAG, title(rep), text(rep, tz), "info")
        if err:
            return [f"report: {month_name(k)} not sent ({err}); trying again in an hour"]
        rep["sent"] = int(now)
        out.append(f"report: {month_name(k)} sent")
    st["reports"][k] = rep
    st["reports"] = {x: st["reports"][x] for x in sorted(st["reports"])[-KEEP:]}
    st["done"] = k
    _save(stack, st)
    return out


def _save(stack, st):
    ss.write_if_changed(_path(stack), json.dumps(st, indent=1) + "\n", 0o644)


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------
def page_months(now, tz, n=PAGE_MONTHS):
    """The last n finished months, newest first, as keys."""
    local = dt.datetime.fromtimestamp(now, tz)
    y, m = local.year, local.month
    out = []
    for _ in range(n):
        y, m = previous(y, m)
        out.append(key(y, m))
    return out


def get(stack, site, k, cache=None):
    """The stored report for month k, else one worked out now (cache(key, ttl, fn) keeps it for a while)."""
    rep = load(stack)["reports"].get(k)
    if rep:
        return rep
    y, m = (int(x) for x in MONTH_RE.match(k).groups())
    fn = lambda: build(stack, site, y, m)  # noqa: E731
    return cache(("report", k), 900, fn) if cache else fn()


def render(stack, site, want=None, now=None, cache=None):
    e = html.escape
    now = now or time.time()
    tz = _tz(site)
    months = page_months(now, tz)
    k = want if want in months else months[0]
    rep = get(stack, site, k, cache)
    links = " &middot; ".join(f"<b>{e(month_name(x))}</b>" if x == k else f"<a href='/report?m={x}'>{e(month_name(x))}</a>"
                              for x in months)
    parts = [f"<h1>Report for {e(month_name(k))}</h1>"]
    if not rep["rows"]:
        parts.append("<div class='card'>The box has no data from this month.</div>")
    else:
        rows = []
        for r in rep["rows"]:
            before = "&ndash;" if r["before"] is None else f"{r['before']:,.{r['digits']}f}"
            note = f" <small>({r['days']} of {r['of']} days have data)</small>" if r["days"] < r["of"] else ""
            rows.append(f"<tr><td>{e(r['title'])} ({e(r['unit'])})</td><td class='n'>{r['value']:,.{r['digits']}f}</td>"
                        f"<td class='n'>{before}</td><td>{e(change(r)) if r['before'] is not None else ''}{note}</td></tr>")
        parts.append("<table class='hist'><tr><th></th><th class='n'>This month</th><th class='n'>A year earlier</th>"
                     f"<th>Change</th></tr>{''.join(rows)}</table>")
        extra = [x for x in (generator_line(rep), battery_line(rep.get("battery")), backup_line(rep.get("backup"), tz)) if x]
        if extra:
            parts.append("<div class='card'>" + "<br>".join(e(x) for x in extra) + "</div>")
        if rep.get("made"):
            made = dt.datetime.fromtimestamp(rep["made"], tz).strftime("%B %-d, %Y")
            sent = "and sent as a notification" if rep.get("sent") else "not sent as a notification"
            parts.append(f"<p><small>Made on {e(made)}, {sent}. The battery and backup lines are as they stood that day.</small></p>")
        else:
            parts.append("<p><small>Worked out just now from the stored days. Battery health and backups are only in "
                         "the reports the box made at the start of a month.</small></p>")
    parts.append(f"<p><small>Other months: {links}</small></p>"
                 "<p><small>The box sends last month's report on the first days of each month, after 8:00 "
                 "(setup page: <i>Monthly report</i>). Every month side by side: <a href='/history'>History</a>.</small></p>")
    return "".join(parts)
