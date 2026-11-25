#!/usr/bin/env python3
"""Panels and inverters: faults that look normal at a glance. Monitoring only.

Three checks, run by the collector every 5 minutes (run()), kept in state/solar-check.json:

  * a PV string making far less than usual (warn). Each string's share of the site's solar over the last 2 hours
    is compared with its usual share in the same 2 hours of the day over the last 4 weeks (the median; strings
    facing different ways have shares that change through the day, hence the same hours). A string under 20% of
    what its usual share would give now, where that is 300 W or more, is reported: a tripped breaker, a loose
    connector, or snow or shade on just that string. Nothing to set up: the usual share is learned. When all
    of one inverter's strings are out, it is reported as the inverter.
  * an inverter supplying nothing while the others carry the house (warn): the same comparison on each inverter's
    output over the last 30 minutes (parallel inverters normally share the load).
  * the whole site far below what the sunlight should give (snow, dirt, a tripped main PV breaker): under 40% of
    the potential over the last 2 full hours. With a weather station's solar sensor the potential is measured,
    and this is a warning. Without one it comes from Open-Meteo's weather model, which can miss cloud: then it is
    only noted, and becomes a warning when the last 24 hours of daylight were as low. Needs the advisor's
    calibration (a few weeks of data); until then it says so.

None of them is judged while the battery is nearly full (95%): the inverters then hold the panels back, and not
evenly. A finding stays until the same check passes again (so it doesn't "clear" at dusk and come back at dawn);
one that could not be checked again for a week is dropped.

A dongle that is offline or silent has its own alerts already (the Home Assistant package and boxhealth.py).
"""
import json
import os
import statistics
import time

import solar_setup as ss

BASE_DAYS = 28          # how far back the usual shares are learned
MIN_DAYS = 5            # comparable days needed before a share is trusted
FULL_SOC = 95           # at or above this the inverters hold the panels back
STRING_WINDOW = "2h"
STRING_MIN_W = 300      # what the string should be making before its absence means anything
STRING_LOW = 0.2        # under this share of the expected: reported
STRING_FINE = 0.4       # from this share: fine again (between the two: left as it was)
STRING_MIN_SHARE = 0.05
LIGHT_MIN_W = 300       # a day's window needs this much solar in total to count towards the usual shares
OUTPUT_WINDOW = "30m"
OUTPUT_MIN_W = 500
OUTPUT_LOW = 0.1
OUTPUT_FINE = 0.3
OUTPUT_MIN_SHARE = 0.15
SITE_LOW = 0.4          # actual / potential under this: reported
SITE_FINE = 0.5
SITE_HOURS = 2          # full hours the site check looks at
SITE_DAY_HOURS = 4      # daylight hours needed in the last 24 before "it has been like this all day" is said
SNOW_C = 2.0            # at or below this, snow is named first
DROP_S = 7 * 86400      # a finding that couldn't be checked again for this long is dropped
STALE_S = 1800          # a result older than this is "unknown" (the collector stopped)


def path(stack):
    return os.path.join(stack, "state", "solar-check.json")


def watts(w):
    return f"{w / 1000:.1f} kW" if w >= 1000 else f"{w:.0f} W"


# --------------------------------------------------------------------------
# The checks (no I/O: tested with made-up data)
# --------------------------------------------------------------------------
def shares(series, usable, min_w, low, fine, min_share, light_min=LIGHT_MIN_W):
    """series: {key: [value on each earlier day, oldest first, ..., value now]} (W, None where missing), all the
    same length; usable: one bool per position (False: the battery was full then, the shares mean nothing).
    Returns {key: {"state": "low" | "fine" | "unknown", "w", "expected_w", "usual"}}."""
    out = {}
    if not series:
        return out
    n = len(usable)
    totals = [sum(v[j] for v in series.values() if v[j] is not None) for j in range(n)]
    for key, vals in series.items():
        res = {"state": "unknown", "w": vals[-1], "expected_w": None, "usual": None}
        out[key] = res
        past = [vals[j] / totals[j] for j in range(n - 1) if usable[j] and vals[j] is not None and totals[j] >= light_min]
        if len(past) < MIN_DAYS:
            continue
        usual = statistics.median(past)
        res["usual"] = round(usual, 3)
        if vals[-1] is None or not usable[-1] or usual < min_share or usual > 0.98:
            continue                                    # alone (nothing to compare with), unused, or no data now
        expected = usual / (1 - usual) * (totals[-1] - vals[-1])
        res["expected_w"] = round(expected)
        if expected < min_w:
            continue
        if vals[-1] < low * expected:
            res["state"] = "low"
        elif vals[-1] >= fine * expected:
            res["state"] = "fine"
    return out


def site_check(pot, pv, soc, kwp, hour_end, measured):
    """pot, pv: {hour end: kW} expected and actual; soc: {hour end: highest % in that hour}. Returns
    {"state": "low" | "fine" | "unknown", "level", "kw", "expected_kw", "all_day"}."""
    need = max(1.0, 0.15 * kwp)

    def ok(t):
        return pot.get(t) is not None and pot[t] >= need and t in pv and soc.get(t, 100) < FULL_SOC
    res = {"state": "unknown", "level": None, "kw": None, "expected_kw": None, "all_day": False}
    recent = [hour_end - i * 3600 for i in range(SITE_HOURS)]
    if not all(ok(t) for t in recent):
        return res
    got, want = sum(pv[t] for t in recent) / SITE_HOURS, sum(pot[t] for t in recent) / SITE_HOURS
    res.update(kw=round(got, 2), expected_kw=round(want, 2))
    if got >= SITE_FINE * want:
        res["state"] = "fine"
    elif got < SITE_LOW * want:
        day = [t for t in (hour_end - i * 3600 for i in range(24)) if ok(t)]
        res["all_day"] = len(day) >= SITE_DAY_HOURS and sum(pv[t] for t in day) < SITE_LOW * sum(pot[t] for t in day)
        res.update(state="low", level="warn" if measured or res["all_day"] else "note")
    return res


def potential(site, fc, adv, ghi, temp_c, hours):
    """({hour end: kW the panels could make}, "station" | "model" | None, why not). From the weather station's
    sunlight when the advisor has calibrated against it, else the calibrated forecast (Open-Meteo's model)."""
    data = (adv or {}).get("data") or {}
    if (adv or {}).get("source") == "weather station" and data.get("calibration") and site.get("latitude") \
            and all(t in ghi for t in hours[:SITE_HOURS]):
        import advisor
        lat, lon = float(site["latitude"]), float(site["longitude"])
        temp = 15.0 if temp_c is None else temp_c
        pot = {}
        for t in hours:
            if t not in ghi:
                continue
            kw = 0.0
            for a in site["arrays"]:
                x = advisor.poa(ghi[t], t - 1800, lat, lon, int(a["tilt"]), int(a["azimuth"]))
                kw += int(a["panels"]) * int(a["panel_watts"]) / 1000 * x / 1000 * (1 + advisor.GAMMA * (temp + x / 800 * 20 - 25))
            pot[t] = kw * data["calibration"]
        return pot, "station", ""
    if not fc or not fc.get("hours"):
        return {}, None, "there is no solar forecast yet"
    if fc.get("calibration_source") != "calibrated":
        return {}, None, "the forecast isn't tuned to this site yet (the advisor needs a few weeks of data)"
    by_end = {int(t): kw for t, kw in fc["hours"]}
    return {t: by_end[t] for t in hours if t in by_end}, "model", ""


def evaluate(d, site, fc, adv, now):
    """All three checks on fetched data d (fetch()). Returns {"results": {key: {...}}, "strings": [...],
    "site": {...}, "site_source", "site_why"}; a result has "state", and when low: "level", "text"."""
    results = {}
    usable = [s is not None and s < FULL_SOC for s in d["soc_days"]]
    cold = d.get("temp_c") is not None and d["temp_c"] <= SNOW_C
    st = shares(d["strings"], usable, STRING_MIN_W, STRING_LOW, STRING_FINE, STRING_MIN_SHARE)
    by_inv = {}
    for (inv, n), r in st.items():
        by_inv.setdefault(inv, {})[n] = r
    for inv, rs in sorted(by_inv.items()):
        judged = {n: r for n, r in rs.items() if r["state"] != "unknown"}
        low = {n: r for n, r in judged.items() if r["state"] == "low"}
        used = [r for r in rs.values() if (r["usual"] or 0) >= STRING_MIN_SHARE]
        whole = len(by_inv) > 1 and bool(used) and all(r["state"] == "low" for r in used)
        if whole:
            got, want = sum(r["w"] for r in low.values()), sum(r["expected_w"] for r in low.values())
            results[f"pv:{inv}"] = {"state": "low", "level": "warn", "text": (
                f"Inverter {inv} is making almost no solar ({watts(got)} over the last 2 hours where about {watts(want)} "
                "is usual in this light) while the others are. Check its PV disconnect and breakers, and its screen for a fault.")}
        elif judged:
            results[f"pv:{inv}"] = {"state": "fine"}
        for n, r in sorted(rs.items()):
            if r["state"] == "low" and not whole:
                why = ("Snow on those panels, a tripped breaker or a loose connector." if cold else
                       "A tripped breaker, a loose connector, or shade or dirt on those panels.")
                results[f"string:{inv}:{n}"] = {"state": "low", "level": "warn", "text": (
                    f"Inverter {inv}, PV string {n} is making {watts(r['w'])} where about {watts(r['expected_w'])} is usual "
                    f"at this time of day in this light. {why}")}
            elif r["state"] != "unknown":
                results[f"string:{inv}:{n}"] = {"state": "fine"}      # also when reported as the whole inverter
    out = shares(d["output"], [True] * len(d["soc_days"]), OUTPUT_MIN_W, OUTPUT_LOW, OUTPUT_FINE, OUTPUT_MIN_SHARE)
    for inv, r in sorted(out.items()):
        if r["state"] == "low":
            others = sum(v[-1] for k, v in d["output"].items() if k != inv and v[-1] is not None)
            results[f"output:{inv}"] = {"state": "low", "level": "warn", "text": (
                f"Inverter {inv} is supplying almost nothing ({watts(r['w'])}) while the others carry the house "
                f"({watts(others)}); usually they share it. Check its screen for a fault and its output breaker.")}
        elif r["state"] == "fine":
            results[f"output:{inv}"] = {"state": "fine"}
    hour_end = int(now) // 3600 * 3600
    hours = [hour_end - i * 3600 for i in range(24)]
    kwp = sum(int(a["panels"]) * int(a["panel_watts"]) for a in site.get("arrays") or []) / 1000
    pot, source, why_not = potential(site, fc, adv, d.get("ghi_h") or {}, d.get("temp_c"), hours) if kwp else ({}, None, "the solar arrays aren't entered yet")
    sc = site_check(pot, {t: w / 1000 for t, w in d["pv_h"].items()}, d["soc_h"], kwp, hour_end, source == "station")
    if sc["state"] == "low":
        cause = ("Snow on the panels is the usual cause in this cold; otherwise" if cold else "Usually snow or dirt on the panels, or")
        text = (f"Solar is far below what the sunlight should give: {sc['kw']:.1f} kW over the last {SITE_HOURS} hours where "
                f"about {sc['expected_kw']:.1f} kW was expected")
        if source == "station":
            text += f" from the weather station's sunlight. {cause} a tripped PV breaker."
        elif sc["all_day"]:
            text += f", and it has been like this through the last day of sunlight. {cause} a tripped PV breaker."
        else:
            text += (" from the weather forecast. It may be cloud the forecast didn't see; if it stays like this "
                     "it becomes an alert.")
        results["site"] = {"state": "low", "level": sc["level"], "text": text}
    elif sc["state"] == "fine":
        results["site"] = {"state": "fine"}
    strings = [{"inverter": inv, "string": n, "w": round(r["w"]), "expected_w": r["expected_w"], "usual": r["usual"], "state": r["state"]}
               for (inv, n), r in sorted(st.items())       # not the PV inputs that were never used
               if r["w"] is not None and max((v for v in d["strings"][(inv, n)] if v is not None), default=0) >= 50]
    return {"results": results, "strings": strings, "site": sc, "site_source": source, "site_why": why_not,
            "battery_full": not usable[-1] if usable else False}


def latch(active, results, now):
    """The findings to keep: new and repeated ones from `results`, earlier ones whose check couldn't be made this
    time (night, a full battery). One whose check passed is gone. Returns (active, log lines)."""
    msgs = []
    keep = {}
    for key, r in results.items():
        if r["state"] != "low":
            if key in active:
                msgs.append(f"solar check: cleared: {active[key]['text']}")
            continue
        was = active.get(key)
        keep[key] = {"level": r["level"], "text": r["text"], "since": was["since"] if was else int(now), "seen": int(now)}
        if not was or was["level"] != r["level"]:
            msgs.append(f"solar check: {r['level']}: {r['text']}")
    for key, a in active.items():
        if key not in results and now - a.get("seen", 0) < DROP_S:
            keep[key] = a
    return keep, msgs


# --------------------------------------------------------------------------
# Data and state
# --------------------------------------------------------------------------
def _daily(metric, window, end):
    """{deploymentID: [the mean over `window` ending at this time of day, for each of the last BASE_DAYS days and
    now]} with None where there was no data."""
    import display
    times = [end - i * 86400 for i in range(BASE_DAYS, -1, -1)]
    r = display._vm("/api/v1/query_range", {"query": f"avg_over_time({metric}[{window}])", "start": times[0], "end": end,
                                            "step": 86400}, timeout=15)
    out = {}
    for s in r:
        got = {int(t): float(v) for t, v in s["values"]}
        out[s["metric"].get("deploymentID") or "inverter"] = [got.get(t) for t in times]
    return out


def _hourly(q, hour_end, hours=24):
    import display
    r = display._vm("/api/v1/query_range", {"query": q, "start": hour_end - (hours - 1) * 3600, "end": hour_end, "step": 3600}, timeout=15)
    return {int(t): float(v) for t, v in (r[0]["values"] if r else [])}


def fetch(now):
    import display
    end = int(now) // 300 * 300
    hour_end = int(now) // 3600 * 3600
    strings = {}
    for n in (1, 2, 3):
        for inv, vals in _daily(f"lxp_p_pv_{n}", STRING_WINDOW, end).items():
            strings[(inv, n)] = vals
    soc = _daily("lxp_soc", STRING_WINDOW, end)             # the mean is close enough: full is full for hours
    n = BASE_DAYS + 1
    soc_days = [max((v[j] for v in soc.values() if v[j] is not None), default=None) for j in range(n)]
    return {
        "strings": strings, "soc_days": soc_days, "output": _daily("lxp_p_eps", OUTPUT_WINDOW, end),
        "pv_h": _hourly("avg_over_time(sum(lxp_p_pv)[1h:5m])", hour_end),
        "soc_h": _hourly("max_over_time(max(lxp_soc)[1h])", hour_end),
        "ghi_h": _hourly("avg_over_time(max(site_solar_radiation_wm2)[1h:5m])", hour_end),
        "temp_c": display.vm_instant("max(last_over_time(site_outdoor_temp_c[30m])) or max(last_over_time(site_openmeteo_temp_c[3h]))"),
    }


def run(stack, now=None, data=None):
    """One round: fetch, check, keep the findings. Returns log lines (new and cleared findings)."""
    now = now or time.time()
    site = ss.load_json(os.path.join(stack, "state", "site.json"), {})
    fc = ss.load_json(os.path.join(stack, "state", "forecast.json"), {})
    if now - fc.get("generated", 0) > 6 * 3600:
        fc = {}
    adv = ss.load_json(os.path.join(stack, "state", "advisor.json"), {})
    ev = evaluate(fetch(now) if data is None else data, site, fc, adv, now)
    old = ss.load_json(path(stack), {})
    active, msgs = latch(old.get("active") or {}, ev["results"], now)
    doc = {"checked": int(now), "active": active, "strings": ev["strings"], "site": ev["site"],
           "site_source": ev["site_source"], "site_why": ev["site_why"], "battery_full": ev["battery_full"],
           "judged": sorted(k for k, r in ev["results"].items())}
    ss.write_if_changed(path(stack), json.dumps(doc, indent=1) + "\n", 0o644)
    return msgs


def status(stack, now=None):
    """For the pages and Home Assistant: {"level": ok | note | warn | unknown, "findings": [(level, text)],
    "strings", "site", "site_source", "site_why", "checked", "battery_full"}."""
    now = now or time.time()
    doc = ss.load_json(path(stack), {})
    if not doc or abs(now - doc.get("checked", 0)) > STALE_S:
        return {"level": "unknown", "findings": [], "strings": [], "site": {}, "site_source": None, "site_why": "",
                "checked": doc.get("checked"), "battery_full": False}
    order = {"warn": 0, "note": 1}
    finds = sorted(((a["level"], a["text"]) for a in doc["active"].values()), key=lambda f: (order.get(f[0], 2), f[1]))
    level = "warn" if any(f[0] == "warn" for f in finds) else "note" if finds else "ok"
    return {"level": level, "findings": finds, "strings": doc.get("strings") or [], "site": doc.get("site") or {},
            "site_source": doc.get("site_source"), "site_why": doc.get("site_why") or "", "checked": doc["checked"],
            "battery_full": bool(doc.get("battery_full"))}


def ha_fields(stack):
    try:
        s = status(stack)
    except Exception:  # noqa: BLE001 - never break /api/ha
        return {"solar_check": "unknown", "solar_check_text": ""}
    return {"solar_check": s["level"],
            "solar_check_text": " ".join(t for _, t in s["findings"]) or ("All strings and inverters are making what they usually do."
                                                                         if s["level"] == "ok" else "")}


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
WORD = {"ok": "good", "note": "worth a look", "warn": "<b>needs attention</b>", "unknown": "not checked yet"}


def site_line(s):
    """The whole-site check in words."""
    sc = s.get("site") or {}
    how = {"station": "the weather station's sunlight", "model": "the weather forecast's sunlight"}.get(s.get("site_source"))
    if not how:
        return f"The whole site against the sunlight: not checked, {s.get('site_why') or 'no data yet'}."
    if sc.get("kw") is None:
        return (f"The whole site against {how}: nothing to compare right now (it needs 2 hours of good light with "
                "the battery below 95%).")
    return (f"The whole site against {how}: {sc['kw']:.1f} kW over the last 2 hours where about "
            f"{sc['expected_kw']:.1f} kW was expected.")


def card(s):
    import html
    finds = "".join(f"<li>{html.escape(t)}</li>" for _, t in s["findings"])
    return (f"<div class='card{' warn' if s['level'] == 'warn' else ''}'><b>Panels and inverters</b>: {WORD[s['level']]}"
            f"{('<ul>' + finds + '</ul>') if finds else ''} &nbsp;<a href='/panels'>details</a></div>")


def render(s):
    import html
    e = html.escape
    finds = "".join(f"<li{' style=\"color:var(--warn)\"' if lvl == 'warn' else ''}>{e(t)}</li>" for lvl, t in s["findings"])
    if s["level"] == "unknown":
        top = ("<div class='card'>Not checked yet. The checks run every 5 minutes once the inverters report; "
               "if this stays, see the <a href='/check'>check-up</a>.</div>")
    else:
        top = (f"<div class='card{' warn' if s['level'] == 'warn' else ''}'><b>Overall</b>: {WORD[s['level']]}"
               f"<ul>{finds or '<li>Every string and inverter is making what it usually does.</li>'}</ul></div>")
    rows = ""
    for r in s["strings"]:
        usual = "learning (needs 5 comparable days)" if r["usual"] is None else f"{r['usual'] * 100:.0f}%"
        exp = "" if r["expected_w"] is None else watts(r["expected_w"])
        state = {"low": "<b class='err'>far below usual</b>", "fine": "as usual"}.get(r["state"], "not judged now")
        rows += (f"<tr><td>{e(str(r['inverter']))}</td><td>{r['string']}</td><td>{watts(r['w'])}</td><td>{exp}</td>"
                 f"<td>{usual}</td><td>{state}</td></tr>")
    table = (f"<h2>PV strings, last 2 hours</h2><table><tr><th>Inverter</th><th>String</th><th>Made</th><th>Usual in this light</th>"
             f"<th>Usual share of the site</th><th></th></tr>{rows}</table>" if rows else "")
    full = ("<p><small>The battery is nearly full, so the inverters are holding the panels back: nothing is judged "
            "until there is room again.</small></p>" if s.get("battery_full") else "")
    when = time.strftime("%H:%M", time.localtime(s["checked"])) if s.get("checked") else "never"
    return f"""<h1>Panels and inverters</h1>{top}{table}{full}
<p>{e(site_line(s))}</p>
<p><small>Each string is compared with its own usual share of the site's solar at this time of day over the last
4 weeks, and each inverter's output with its usual share of the house. Nothing is judged in poor light, or while the
battery is nearly full. A finding stays until the same check passes again. Last checked {when}.</small></p>
<p><a href='/'>Start page</a></p>"""


if __name__ == "__main__":
    import sys
    stack_dir = sys.argv[1] if len(sys.argv) > 1 else "/opt/solar-monitor/stack"
    print(json.dumps(evaluate(fetch(time.time()), ss.load_json(os.path.join(stack_dir, "state", "site.json"), {}),
                              ss.load_json(os.path.join(stack_dir, "state", "forecast.json"), {}),
                              ss.load_json(os.path.join(stack_dir, "state", "advisor.json"), {}), time.time()),
                     indent=1, default=str))
