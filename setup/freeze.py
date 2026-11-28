#!/usr/bin/env python3
"""Freeze protection: watch a few temperatures (a slab, a probe, a room, a thermostat) from Home Assistant and say
so before pipes freeze. Monitoring only: nothing is switched. It works without the boiler module, with any Home
Assistant temperature sensor or thermostat.

Three rules, checked once a minute (step()):

  * cold (critical): a watched temperature has been below the threshold (40 °F / 4.4 °C by default) for 5 minutes.
    Cleared once it is back 1 °C above the threshold.
  * risk (warning): a hard freeze (20 °F / -6.7 °C by default) is forecast within 24 hours while the heat or the
    power is in doubt: the boiler module reports a fault, or the battery is at or below the site's low level. This
    comes hours before anything in the house gets cold. In away mode (away.py) it is told whatever the heat and the
    battery say, and the cold threshold is raised by away mode's margin. The forecast is the weather module's (wx-forecast.json).
  * sensor (warning): a watched sensor has given no reading for 30 minutes, so it protects nothing.

Temperatures are kept in °C; pages and messages use the site's units. State: state/freeze.json.
"""
import calendar
import json
import os
import time

import hastates
import solar_setup as ss

MAX_SENSORS = 4
DEFAULTS = {"sensors": [], "below_c": 4.44, "hard_freeze_c": -6.67, "alerts": True}
HOLD_S = 300            # below the threshold this long before the alert
CLEAR_C = 1.0           # back this far above the threshold before it clears
STALE_S = 1800          # a sensor silent this long is "no reading"
AHEAD_H = 24            # how far ahead the forecast is searched for a hard freeze
FORECAST_MAX_AGE_S = 3 * 3600
LOG_MAX = 50
RULES = {  # rule -> (tag, priority, title)
    "cold": ("freeze_cold", "critical", "Freeze risk: it is cold inside"),
    "risk": ("freeze_risk", "warning", "Hard freeze forecast"),
    "sensor": ("freeze_sensor", "warning", "Freeze sensor not reporting"),
}


def _path(stack):
    return os.path.join(stack, "state", "freeze.json")


def load(stack):
    st = ss.load_json(_path(stack), {})
    for k, v in (("below", {}), ("lost", {}), ("active", {}), ("log", [])):
        st.setdefault(k, v)
    return st


def save(stack, st):
    ss.write_if_changed(_path(stack), json.dumps(st, indent=1) + "\n", 0o644)


def us(site):
    return (site or {}).get("units", "us_customary") == "us_customary"


def to_c(value, unit):
    return (value - 32) * 5 / 9 if unit and "F" in str(unit) else value


def show(c, us_units, unit=True):
    """A temperature in the site's units: "38 °F"."""
    if c is None:
        return "--"
    v = c * 9 / 5 + 32 if us_units else c
    return f"{v:.0f}{(' °F' if us_units else ' °C') if unit else ''}"


def reading(cur, ha_unit, now=None):
    """One sensor's temperature from its Home Assistant state: {"ok", "c", "raw"}. A thermostat's is its room
    temperature, which Home Assistant gives in its own unit system (ha_unit). Not ok when unavailable, not a
    number, or older than STALE_S."""
    raw = (cur or {}).get("state")
    if not cur or raw in (None, "unavailable", "unknown", ""):
        return {"ok": False, "c": None, "raw": raw}
    attrs = cur.get("attributes") or {}
    if "current_temperature" in attrs:
        v, unit = hastates.number(attrs.get("current_temperature")), ha_unit
    else:
        v, unit = hastates.number(raw), cur.get("unit") or ha_unit
    if v is None or (cur.get("at") and (now or time.time()) - cur["at"] > STALE_S):
        return {"ok": False, "c": None, "raw": raw}
    return {"ok": True, "c": round(to_c(v, unit), 2), "raw": raw}


def names(stack):
    return {k["entity_id"]: k["name"] for k in hastates.known(stack, ("sensor", "climate"))}


def readings(stack, s, site, now=None):
    """[{"entity", "name", "ok", "c"}] for the chosen sensors, in their order."""
    unit = "°F" if us(site) else "°C"            # the box sets Home Assistant's unit system from the site's
    nm = names(stack)
    return [{"entity": e, "name": nm.get(e) or e.split(".", 1)[1].replace("_", " ").capitalize(),
             **reading(hastates.get(stack, e), unit, now)} for e in s.get("sensors") or []]


def forecast_low(stack, now=None, hours=AHEAD_H):
    """(°C, Unix time) of the coldest hour in the next `hours` from the weather forecast, or None when there is no
    fresh one (the weather module is off)."""
    now = now or time.time()
    fc = ss.load_json(os.path.join(stack, "state", "wx-forecast.json"), {})
    h = fc.get("hourly") or {}
    if now - fc.get("fetched", 0) > FORECAST_MAX_AGE_S or not h.get("time"):
        return None
    off = fc.get("utc_offset_seconds") or 0
    best = None
    for t, temp in zip(h["time"], h.get("temperature_2m") or []):
        if temp is None:
            continue
        try:
            ts = calendar.timegm(time.strptime(t, "%Y-%m-%dT%H:%M")) - off
        except ValueError:
            continue
        if now - 3600 < ts <= now + hours * 3600:
            c = to_c(temp, "°F" if fc.get("units_us") else "°C")
            if best is None or c < best[0]:
                best = (round(c, 1), ts)
    return best


def lowest(rd):
    ok = [r for r in rd if r["ok"]]
    return min(ok, key=lambda r: r["c"]) if ok else None


def step(stack, s, rd, site, now=None, alert=None, soc=None, boiler_fault="", low=None, away=False):
    """One minute: check the rules, raise and clear their alerts. rd: readings(); soc: the battery's level (%);
    boiler_fault: the boiler module's fault text ("" for none, or with the module off); low: forecast_low();
    away: nobody is at the property (away mode), which is reason enough to tell of a hard freeze in the forecast.
    `alert(tag, title, message, priority)` sends. Returns log lines."""
    now = now or time.time()
    st = load(stack)
    u = us(site)
    below_c = float(s.get("below_c", DEFAULTS["below_c"]))
    chosen = {r["entity"] for r in rd}
    for k in ("below", "lost"):
        st[k] = {e: t for e, t in st[k].items() if e in chosen}
    for r in rd:
        e = r["entity"]
        if not r["ok"]:
            st["lost"].setdefault(e, now)
            continue                                 # a silent sensor keeps its cold timer: it may still be cold
        st["lost"].pop(e, None)
        if r["c"] < below_c:
            st["below"].setdefault(e, now)
        elif r["c"] >= below_c + CLEAR_C:
            st["below"].pop(e, None)
    want = {}
    cold = [r for r in rd if r["ok"] and now - st["below"].get(r["entity"], now) >= HOLD_S]
    if cold:
        want["cold"] = ("; ".join(f"{r['name']} is {show(r['c'], u)}" for r in cold)
                        + f", below {show(below_c, u)}. Check the heat before pipes freeze.")
    try:
        floor = float(site.get("low_soc_alert") or 25)
    except (TypeError, ValueError):
        floor = 25.0
    why = []
    if boiler_fault:
        why.append(f"the boiler has a fault ({boiler_fault})")
    if soc is not None and soc <= floor:
        why.append(f"the battery is low ({soc:.0f}%)")
    if away and not why:
        why.append("nobody is at the property (away mode)")
    if low and low[0] <= float(s.get("hard_freeze_c", DEFAULTS["hard_freeze_c"])) and why:
        at = time.strftime("%-I:%M %p" if u else "%H:%M", time.localtime(low[1]))
        coldest = lowest(rd)
        inside = f" Coldest watched temperature now: {coldest['name']}, {show(coldest['c'], u)}." if coldest else ""
        want["risk"] = f"{show(low[0], u)} is forecast about {at} and {' and '.join(why)}.{inside}"
    lost = [r for r in rd if now - st["lost"].get(r["entity"], now) >= STALE_S]
    if lost:
        want["sensor"] = (", ".join(r["name"] for r in lost) + ": no temperature for 30 minutes. "
                          "Freeze protection can't see it; check the sensor.")
    msgs = []
    for rule, (tag, priority, title) in RULES.items():
        if rule in want and rule not in st["active"]:
            st["active"][rule] = now
            st["log"] = (st["log"] + [{"at": int(now), "rule": rule, "title": title, "detail": want[rule]}])[-LOG_MAX:]
            msgs.append(f"freeze: {title}: {want[rule]}")
            if alert and s.get("alerts", True):
                alert(tag, title, want[rule], priority)
        elif rule not in want and rule in st["active"]:
            del st["active"][rule]
            for x in reversed(st["log"]):
                if x["rule"] == rule and not x.get("cleared"):
                    x["cleared"] = int(now)
                    break
            msgs.append(f"freeze: cleared: {title}")
            if alert and s.get("alerts", True):
                alert(tag, title, "clear", priority)
    st["text"] = {r: want[r] for r in want}
    save(stack, st)
    return msgs


def summary(stack, s, site, now=None):
    """For the pages, the wall display and Home Assistant: {"readings", "lowest", "active": {rule: since},
    "text": {rule: message}, "log", "forecast_low": (°C, time) or None}."""
    now = now or time.time()
    st = load(stack)
    rd = readings(stack, s, site, now)
    return {"readings": rd, "lowest": lowest(rd), "active": st["active"], "text": st.get("text") or {},
            "log": st["log"], "forecast_low": forecast_low(stack, now)}
