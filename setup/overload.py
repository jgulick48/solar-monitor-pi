#!/usr/bin/env python3
"""Overload warning: how close the house load comes to what the inverters can supply. Monitoring only.

Each inverter has its own limit, and on a split-phase inverter each 120 V leg has half of it (a 6000XP: 6 kW, 3 kW a
leg), so two inverters with 12 kW between them can still trip on one leg at 3 kW. For every inverter the box looks
at its whole output and at each leg, in VA where the inverter reports it (that is what the limit is in; a motor
draws more VA than W), and takes the tightest one:

  * near the limit now (warn): the highest 1-minute average in the last 3 minutes is 90% or more. Short enough to
    act on ("don't start the dryer now"), long enough that a motor starting doesn't count.
  * came close today (note): today's highest reading was 80% or more. No alert; on the pages.

The readings come every 10 seconds or so: a surge shorter than that isn't seen. The limit is the model's
continuous rating from the setup page; inverters ride through short surges above it.
"""
import time

import solar_setup as ss

WARN = 0.90
NOTE = 0.80
LOAD = "sum(lxp_p_load) or sum(lxp_p_eps)"
# (channel, the VA reading, the W reading used when the inverter gives no VA, share of the inverter's rating)
CHANNELS = (("all", "lxp_s_eps", "lxp_p_eps", 1.0), ("leg 1", "lxp_s_eps_l1", "lxp_p_eps_l1", 0.5),
            ("leg 2", "lxp_s_eps_l2", "lxp_p_eps_l2", 0.5))
_cache = {}


def day_start(now):
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def fetch(now=None):
    """{"recent": {(inverter, channel): W}, "today": {...}, "peak_w": today's highest house load}. Raises when
    VictoriaMetrics doesn't answer."""
    import display
    now = now or time.time()
    secs = max(int(now - day_start(now)), 60)
    out = {"recent": {}, "today": {}}
    for ch, va, w, _ in CHANNELS:
        for key, fn in (("recent", lambda m: f"max_over_time(avg_over_time({m}[1m])[3m:15s])"),
                        ("today", lambda m: f"max_over_time({m}[{secs}s])")):
            for s in display._vm("/api/v1/query", {"query": f"({fn(va)}) or ({fn(w)})"}):
                out[key][(s["metric"].get("deploymentID") or "inverter", ch)] = float(s["value"][1])
    r = display._vm("/api/v1/query", {"query": f"max_over_time(({LOAD})[{secs}s:30s])"})
    out["peak_w"] = float(r[0]["value"][1]) if r else None
    return out


def tightest(readings, ac_kw):
    """(share of the limit, inverter, channel, W, limit in W) of the reading closest to its limit, or None."""
    shares = {ch: share for ch, _, _, share in CHANNELS}
    best = None
    for (inv, ch), w in readings.items():
        cap = ac_kw * 1000 * shares[ch]
        if cap > 0 and (best is None or w / cap > best[0]):
            best = (w / cap, inv, ch, w, cap)
    return best


def where(inv, ch, several):
    """"leg 1 of inverter X" / "inverter X" / "leg 1" / "the inverter"."""
    unit = f"inverter {inv}" if several else "the inverter"
    return unit if ch == "all" else f"{ch} of {unit}" if several else ch


def evaluate(d, ac_kw):
    """{"level": ok | note | warn | unknown, "text", "now_pct", "peak_pct", "peak_kw", "limit_kw"}."""
    several = len({inv for inv, _ in d["recent"]} | {inv for inv, _ in d["today"]}) > 1
    n = max(len({inv for inv, _ in d["recent"]} | {inv for inv, _ in d["today"]}), 1)
    now, day = tightest(d["recent"], ac_kw), tightest(d["today"], ac_kw)
    out = {"level": "unknown", "text": "", "now_pct": None if not now else round(now[0] * 100),
           "peak_pct": None if not day else round(day[0] * 100),
           "peak_kw": None if d.get("peak_w") is None else round(d["peak_w"] / 1000, 2), "limit_kw": round(n * ac_kw, 1)}
    if not now and not day:
        return out
    leg_note = " A leg is one 120 V side: moving a big 120 V load to the other side evens them out."
    if now and now[0] >= WARN:
        _, inv, ch, w, cap = now
        out.update(level="warn", text=(
            f"The house is close to what the inverters can supply: {where(inv, ch, several)} is at {now[0] * 100:.0f}% "
            f"({w / 1000:.1f} of {cap / 1000:g} kW). Turn something off before starting another big load."
            + (leg_note if ch != "all" else "")))
    elif day and day[0] >= NOTE:
        _, inv, ch, w, cap = day
        out.update(level="note", text=(
            f"Today's highest load came to {day[0] * 100:.0f}% of the limit on {where(inv, ch, several)} "
            f"({w / 1000:.1f} of {cap / 1000:g} kW)." + (leg_note if ch != "all" else "")))
    else:
        out["level"] = "ok"
    return out


def status(site, now=None):
    """evaluate() on live data, kept for 20 seconds; level "unknown" when there is none."""
    import advisor
    hit = _cache.get("status")
    if hit and time.time() - hit[0] < 20 and now is None:
        return hit[1]
    ac = advisor.INVERTERS.get(site.get("inverter_model", "6000xp"), advisor.INVERTERS["6000xp"])["ac_kw"]
    try:
        res = evaluate(fetch(now), ac)
    except (OSError, ValueError, KeyError):
        res = {"level": "unknown", "text": "", "now_pct": None, "peak_pct": None, "peak_kw": None, "limit_kw": None}
    _cache["status"] = (time.time(), res)
    return res


def ha_fields(site):
    try:
        s = status(site)
    except Exception:  # noqa: BLE001 - never break /api/ha
        s = {"level": "unknown", "text": "", "now_pct": None, "peak_pct": None, "peak_kw": None}
    return {"overload": s["level"], "overload_text": s["text"], "inverter_load_pct": s["now_pct"],
            "inverter_load_peak_pct": s["peak_pct"], "peak_load_today": s["peak_kw"]}


def card(s):
    import html
    if s["level"] == "unknown":
        return ""
    bits = []
    if s["now_pct"] is not None:
        bits.append(f"now {s['now_pct']}% of the limit")
    if s["peak_pct"] is not None:
        bits.append(f"highest today {s['peak_pct']}%" + (f" ({s['peak_kw']:g} kW in all)" if s["peak_kw"] is not None else ""))
    text = f"<br>{html.escape(s['text'])}" if s["text"] else ""
    return (f"<div class='card{' warn' if s['level'] == 'warn' else ''}'><b>Inverter load</b>: {', '.join(bits)}"
            f"{text}<br><small>The tightest of each inverter's whole output and each of its legs; "
            f"an alert goes out at {WARN * 100:.0f}%.</small></div>")


if __name__ == "__main__":
    import json
    import os
    import sys
    stack_dir = sys.argv[1] if len(sys.argv) > 1 else "/opt/solar-monitor/stack"
    print(json.dumps(status(ss.load_json(os.path.join(stack_dir, "state", "site.json"), {})), indent=1))
