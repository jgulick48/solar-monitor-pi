#!/usr/bin/env python3
"""Battery health, for the whole battery bank (what the inverters hear from the BMS).

  Cell spread near full  highest minus lowest cell voltage while the battery is at 98% or more. LiFePO4
                         cells only show their imbalance near full (in the middle they all sit at ~3.3 V),
                         and the BMS balances them there. Under 50 mV is good, 50-100 mV worth watching,
                         over 100 mV means a cell (or a battery) is drifting. Reported per day; days the
                         battery never got that full have none.
  Not full lately        LiFePO4 banks need a full charge now and then to balance and to reset the BMS's
                         charge counter: a note after 14 days without one, a warning after 30.
  Capacity estimate      energy the inverters took out of the battery per 1% of charge on long discharges
                         (30% or more without charging), times 100. It follows the BMS's own charge
                         counter, so it is a cross-check of the BMS (and of the size entered on the setup
                         page), not an independent measurement: a big gap to the nameplate means the BMS is
                         counting with a smaller (or larger) battery than expected.
  Temperatures           lowest and highest cell now and over 24 hours: a warning at 0 °C or below (LiFePO4
                         must not be charged when frozen; the BMS should stop it) and at 45 °C or above.
  State of health, cycles  as the BMS reports them (lowest SOH / highest cycle count of the batteries
                         the inverters see): a warning below 80%.
  BMS faults, warnings   the BMS's fault and warning codes, now and over 24 hours.

With more than one inverter the figures are combined: the highest and lowest cells of any of them.
"""
import datetime as dt
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import display  # noqa: E402

SPREAD_OK_MV, SPREAD_WARN_MV = 50, 100
FULL_SOC = 98
COLD_C, HOT_C = 0, 45
SOH_WARN = 80
NOT_FULL_NOTE_D, NOT_FULL_WARN_D = 14, 30
MIN_DROP = 30           # % of charge, for the capacity estimate
STEP = 300

Q = {
    "max_cell": "max(lxp_max_cell_volt)",
    "min_cell": "min(lxp_min_cell_volt)",
    "max_temp": "max(lxp_max_cell_temp)",
    "min_temp": "min(lxp_min_cell_temp)",
    "soh": "min(lxp_soh)",
    "cycles": "max(lxp_cycle_cnt_bms)",
    "bms_fault": "max(lxp_fault_code_bms)",
    "bms_warning": "max(lxp_warning_code_bms)",
    "soc": "max(lxp_soc)",
    "rated_ah": "sum(max by (deploymentID) (lxp_batt_capacity))",
}
SPREAD_TOP = f"(({Q['max_cell']} - {Q['min_cell']}) * 1000) and on() ({Q['soc']} >= {FULL_SOC})"


def vm_series(q, start, end, step):
    try:
        r = display._vm("/api/v1/query_range", {"query": q, "start": start, "end": end, "step": step}, timeout=30)
    except (OSError, ValueError, KeyError):
        return {}
    return {int(t): float(v) for s in r[:1] for t, v in s["values"]}


def daily_spread(days, now, tz):
    """{date: max cell spread in mV while at >= 98%} for the last `days` local days (today so far included)."""
    out = {}
    today = dt.datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(days):
        start = today - dt.timedelta(days=i)
        end = min((start + dt.timedelta(days=1)).timestamp(), now)
        secs = int(end - start.timestamp())
        if secs < 60:
            continue
        v = display.vm_at(f"max_over_time(({SPREAD_TOP})[{secs}s:1m])", end)
        out[start.date().isoformat()] = None if v is None else round(v)
    return out


def last_full(now, days=60):
    """Timestamp the battery was last at >= 98%, within `days`; None if not."""
    return display.vm_at(f"max_over_time((time() and on() ({Q['soc']} >= {FULL_SOC}))[{days}d:5m])", now)


def capacity_estimate(now, days=60, fetch=None):
    """kWh per 100% of charge from long discharges: [(start_ts, kwh), ...]."""
    fetch = fetch or vm_series
    now = int(now)
    start = int(now - days * 86400) // STEP * STEP
    soc = fetch(Q["soc"], start, now, STEP)
    out_w = fetch("sum(lxp_p_discharge)", start, now, STEP)
    in_w = fetch("sum(lxp_p_charge)", start, now, STEP)
    results, seg = [], None
    for t in sorted(soc):
        s = soc[t]
        charging = in_w.get(t, 0) > 200
        if seg is not None and (charging or s > seg["low"] + 1):   # a discharge ends when charging starts
            if seg["top"] - seg["low"] >= MIN_DROP and seg["kwh"] > 0:
                results.append((seg["t0"], round(seg["kwh"] / (seg["top"] - seg["low"]) * 100, 1)))
            seg = None
        if seg is None and not charging:
            seg = {"t0": t, "top": s, "low": s, "kwh": 0.0}
        elif seg is not None:
            seg["kwh"] += out_w.get(t, 0) * STEP / 3600 / 1000
            seg["low"] = min(seg["low"], s)
    return results


def _cached(name, ttl, fn):
    return display._cached(("battery", name), ttl, fn)


def status(stack, site, now=None):
    now = int(now or time.time())
    tzname = site.get("time_zone") or "UTC"
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tzname)
    except Exception:  # noqa: BLE001
        tz = dt.timezone.utc
    cur = _cached("now", 30, lambda: {k: display.vm_at(q, now) for k, q in Q.items()})
    day = _cached("24h", 300, lambda: {
        "max_temp": display.vm_at(f"max_over_time(({Q['max_temp']})[24h:5m])", now),
        "min_temp": display.vm_at(f"min_over_time(({Q['min_temp']})[24h:5m])", now),
        "bms_fault": display.vm_at(f"max_over_time(({Q['bms_fault']})[24h:1m])", now),
        "bms_warning": display.vm_at(f"max_over_time(({Q['bms_warning']})[24h:1m])", now),
    })
    spread = _cached("spread", 900, lambda: daily_spread(14, now, tz))
    full_ts = _cached("full", 900, lambda: last_full(now))
    first_ts = _cached("first", 3600, lambda: display.vm_at(f"min_over_time((time() and on() {Q['soc']})[60d:5m])", now))
    caps = _cached("capacity", 3600, lambda: capacity_estimate(now))
    try:
        nameplate = round(int(site["battery_count"]) * float(site["battery_kwh_each"]), 1)
    except (KeyError, TypeError, ValueError):
        nameplate = None

    findings = []   # (level, text): level ok / note / warn
    top = next(((d, v) for d, v in sorted(spread.items(), reverse=True) if v is not None), None)
    if top:
        d, mv = top
        lvl = "ok" if mv < SPREAD_OK_MV else "note" if mv <= SPREAD_WARN_MV else "warn"
        findings.append((lvl, f"Cell spread near full: {mv} mV ({d})" + {"ok": ", good.", "note": ", worth watching.",
                         "warn": ": a cell or battery is drifting. Charge to full and let it balance; if it stays high, check the batteries."}[lvl]))
    if not top and cur.get("soc") is not None:
        findings.append(("ok", "Cell spread near full: not measured yet (no full charge in the last 14 days of history)."))
    if cur.get("soc") is not None:
        # days without a full charge, but no more than the history goes back (a new box knows nothing older)
        history_d = (now - first_ts) / 86400 if first_ts else 0
        days_since = (now - full_ts) / 86400 if full_ts else history_d
        if days_since >= NOT_FULL_NOTE_D:
            lvl = "warn" if days_since >= NOT_FULL_WARN_D else "note"
            when = f"for {days_since:.0f} days" if full_ts else f"in the {days_since:.0f} days of history"
            findings.append((lvl, f"Not fully charged {when}: the cells balance, and the BMS resets its charge count, near full."))
    for k, lo in (("min_temp", True), ("max_temp", False)):
        v = [x for x in (cur.get(k), day.get(k)) if x is not None]
        if v:
            x = min(v) if lo else max(v)
            if lo and x <= COLD_C:
                findings.append(("warn", f"Cells at {x:.0f} °C: too cold to charge (the BMS should stop charging). Keep the batteries above freezing."))
            if not lo and x >= HOT_C:
                findings.append(("warn", f"Cells at {x:.0f} °C: too hot. Check the batteries' ventilation."))
    if cur.get("soh") is not None and cur["soh"] < SOH_WARN:
        findings.append(("warn", f"State of health {cur['soh']:.0f}%: the batteries have lost a fifth of their capacity or more."))
    if cur.get("bms_fault") or day.get("bms_fault"):
        findings.append(("warn", f"BMS fault code {int(cur.get('bms_fault') or day.get('bms_fault'))}"
                         + (" now." if cur.get("bms_fault") else " in the last 24 hours.")))
    if cur.get("bms_warning") or day.get("bms_warning"):
        findings.append(("note", f"BMS warning code {int(cur.get('bms_warning') or day.get('bms_warning'))}"
                         + (" now." if cur.get("bms_warning") else " in the last 24 hours.")))
    cap = round(statistics.median(k for _, k in caps[-10:]), 1) if caps else None
    if cap and nameplate and cap < 0.8 * nameplate:
        findings.append(("note", f"Capacity estimate {cap} kWh vs {nameplate} kWh entered on the setup page: "
                         "the BMS counts with a smaller battery than expected (or the setup page's size is off)."))
    elif cap and nameplate and cap > 1.25 * nameplate:
        findings.append(("note", f"Capacity estimate {cap} kWh vs {nameplate} kWh entered on the setup page: "
                         "check the number of batteries and kWh per battery on the setup page."))
    level = "warn" if any(f[0] == "warn" for f in findings) else "note" if any(f[0] == "note" for f in findings) else "ok"
    if cur.get("max_cell") is None:
        level = "unknown"
    return {
        "level": level, "findings": findings,
        "spread_top_mv": top[1] if top else None, "spread_top_day": top[0] if top else None, "spread_days": spread,
        "spread_now_mv": None if cur.get("max_cell") is None or cur.get("min_cell") is None
        else round((cur["max_cell"] - cur["min_cell"]) * 1000),
        "max_cell_v": cur.get("max_cell"), "min_cell_v": cur.get("min_cell"),
        "temp_min": cur.get("min_temp"), "temp_max": cur.get("max_temp"),
        "temp_min_24h": day.get("min_temp"), "temp_max_24h": day.get("max_temp"),
        "soh": cur.get("soh"), "cycles": cur.get("cycles"),
        "bms_fault": cur.get("bms_fault"), "bms_warning": cur.get("bms_warning"),
        "last_full": full_ts, "history_start": first_ts, "capacity_kwh": cap, "capacity_samples": len(caps), "nameplate_kwh": nameplate,
        "rated_ah": cur.get("rated_ah"),
    }


def _r(v, n=0):
    return None if v is None else round(v, n) if n else round(v)


HA_KEYS = ["battery_health", "battery_health_text", "battery_spread_top", "battery_spread_top_day", "battery_spread_now",
           "battery_temp_min", "battery_temp_max", "battery_soh", "battery_cycles", "battery_capacity_estimate",
           "bms_fault_code", "bms_warning_code", "battery_last_full"]


def ha_fields_empty():
    return {**{k: None for k in HA_KEYS}, "battery_health": "unknown", "battery_health_text": "", "bms_fault_code": 0,
            "bms_warning_code": 0}


def ha_fields(stack, site, now=None):
    s = status(stack, site, now)
    return {
        "battery_health": s["level"],
        "battery_health_text": " ".join(t for lvl, t in s["findings"] if lvl != "ok") or
        (s["findings"][0][1] if s["findings"] else ""),
        "battery_spread_top": s["spread_top_mv"], "battery_spread_top_day": s["spread_top_day"],
        "battery_spread_now": s["spread_now_mv"],
        "battery_temp_min": _r(s["temp_min"]), "battery_temp_max": _r(s["temp_max"]),
        "battery_soh": _r(s["soh"]), "battery_cycles": _r(s["cycles"]),
        "battery_capacity_estimate": s["capacity_kwh"],
        "bms_fault_code": _r(s["bms_fault"]) or 0, "bms_warning_code": _r(s["bms_warning"]) or 0,
        "battery_last_full": dt.datetime.fromtimestamp(s["last_full"]).astimezone().isoformat(timespec="seconds")
        if s["last_full"] else None,
    }
