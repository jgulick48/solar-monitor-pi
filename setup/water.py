"""Water: the water pump, a storage tank and a water meter, from Home Assistant (wiki: "Modules design", Water). The
water module (modules/water) is its switch and settings; this file does the work. Monitoring only: nothing here
switches the pump.

Every input is optional and comes from Home Assistant through the HA state feed; the owner picks which entity fills
each role:

  * pump_power: the pump's metering plug. Runs are found from every power reading Home Assistant sent (the HA state
    feed keeps them all in VictoriaMetrics), not from a once-a-minute look, so a 20-second run with a pressure tank
    still counts. Gives run time, starts, and the pump alerts.
  * tank_level: a storage tank's level, in % (with the tank's capacity, also gallons) or in gallons or litres.
    Gives days of water left at the usual use, fills, and the low-tank alerts.
  * water_meter: a water meter's running total. Gives measured use and the overnight leak check.

Use per day comes from the meter if there is one, else the tank's drops, else the pump's run time x its flow rate
(entered; an estimate). Volumes are kept in US gallons.

Alerts (through the notifications module, switchable):
  * long_run      the pump has run nonstop longer than the limit (a broken pipe, a stuck pressure switch, lost prime)
  * low_power     running well under its usual power (lost prime or running dry: a jet pump's draw drops)
  * overload      drawing more than its nameplate (amps x 120 V): a failing motor or bearings
  * rapid         many very short runs: a waterlogged pressure tank or a failing pressure switch
  * night_flow    water used in every hour from 1 to 5 am (the meter, or else the pump starting every hour): a
                  running toilet or a leak
  * tank_low, tank_critical, offline

  state/water.json   {"since", "last", "pump", "runs", "days", "meter", "tank", "normal_w", "timers", "active", "log"}
"""
import json
import os
import statistics
import time
import urllib.parse
import urllib.request

import hastates
import solar_setup as ss

VM = os.environ.get("SOLAR_VM", "http://127.0.0.1:8428")

# role, label, kind, need, what it gives
ROLES = (
    ("pump_power", "Pump plug power", "power", "recommended",
     "when the pump runs: run time, starts, and the pump alerts (running nonstop, low power, rapid cycling)"),
    ("tank_level", "Storage tank level", "level", "optional",
     "a storage tank's level (% or volume): days of water left at the usual use, fills, the low-tank alerts"),
    ("water_meter", "Water meter", "volume", "optional", "measured use per day, and the overnight leak check"),
)
ROLE = {r[0]: r for r in ROLES}
KINDS = {"power": (("sensor",), "power"), "level": (("sensor",), None), "volume": (("sensor",), "water")}
DEFAULTS = {"roles": {r[0]: "" for r in ROLES}, "alerts": True, "pump_gpm": "", "pump_amps": "", "max_run_min": 20,
            "tank_gal": "", "tank_warn": 30, "tank_critical": 15}

GAL_PER = {"gal": 1.0, "l": 0.264172, "m³": 264.172, "m3": 264.172, "ft³": 7.48052, "ft3": 7.48052, "ccf": 748.052}
VOLTS = 120
ON_W = 30                       # the pump runs above this
LAG_S = 30                      # power readings this recent may not have reached VictoriaMetrics yet
MAX_GAP_S = 3600                # after an outage, runs are looked for this far back at most
STALE_S = 180                   # no power reading this long during a run: the run's state isn't known now
LEARN_RUNS = 20                 # runs of 60 s or more before its usual power is trusted
LOW_POWER = 0.7                 # running under this share of its usual power ...
OVERLOAD_MIN_S = 60             # (the start's inrush is over by then)
RAPID_S, RAPID_N, RAPID_RUN_S = 10 * 60, 6, 20   # 6 runs of under 20 s within 10 minutes
NIGHT_HOURS = (1, 2, 3, 4)      # 1:00 to 5:00
TANK_STEP = 1.0                 # tank level changes under this (%) are noise
TANK_FILL = 2.0                 # ... and a rise of this much is a fill
METER_MAX_GAL = 2000            # a meter jump bigger than this in a minute is a reset or a new meter, not use
KEEP_RUNS, KEEP_LOG, KEEP_DAYS = 500, 200, 60

# (name, hold seconds, title, priority)
RULES = (
    ("long_run", 0, "Water pump running nonstop", "critical"),
    ("low_power", 180, "Water pump drawing too little power", "warning"),
    ("overload", 120, "Water pump drawing more than its nameplate", "warning"),
    ("rapid", 0, "Water pump cycling rapidly", "warning"),
    ("night_flow", 0, "Water used all night", "warning"),
    ("tank_low", 10 * 60, "Water tank low", "warning"),
    ("tank_critical", 10 * 60, "Water tank very low", "critical"),
    ("offline", 30 * 60, "Water sensors offline", "warning"),
)
RULE = {r[0]: r for r in RULES}


def _path(stack):
    return os.path.join(stack, "state", "water.json")


def load(stack):
    st = ss.load_json(_path(stack), {})
    for k, v in (("since", None), ("last", {}), ("pump", None), ("runs", []), ("days", {}), ("meter", None),
                 ("tank", None), ("normal_w", None), ("timers", {}), ("active", {}), ("log", []), ("night", {})):
        st.setdefault(k, v)
    return st


def save(stack, st):
    ss.write_if_changed(_path(stack), json.dumps(st, indent=1) + "\n", 0o644)


def _f(v):
    try:
        return float(v) if str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Readings
# --------------------------------------------------------------------------
def to_gal(value, unit):
    """A volume in US gallons, or None for a unit that isn't a volume."""
    u = str(unit or "").strip().lower()
    return value * GAL_PER[u] if u in GAL_PER else None


def reading(role, cur, tank_gal=None):
    """{"ok", "value", "raw"} for one role: W for the pump; for the tank {"percent", "gal"} (either may be None);
    for the meter its total in gallons."""
    if not cur or cur.get("state") in (None, "unavailable", "unknown", ""):
        return {"ok": False, "value": None, "raw": (cur or {}).get("state")}
    raw = cur.get("state")
    v = hastates.number(raw)
    if v is None:
        return {"ok": False, "value": None, "raw": raw}
    unit = cur.get("unit") or ""
    kind = ROLE[role][2]
    if kind == "power":
        return {"ok": True, "value": v * 1000 if unit.lower() == "kw" else v, "raw": raw}
    if kind == "volume":
        g = to_gal(v, unit)
        return {"ok": g is not None, "value": g, "raw": raw}
    # a tank: % (gallons from its capacity), or a volume (% from its capacity)
    if unit == "%":
        return {"ok": True, "value": {"percent": v, "gal": v / 100 * tank_gal if tank_gal else None}, "raw": raw}
    g = to_gal(v, unit)
    if g is None:
        return {"ok": False, "value": None, "raw": raw}
    return {"ok": True, "value": {"percent": g / tank_gal * 100 if tank_gal else None, "gal": g}, "raw": raw}


def readings(stack, s):
    roles = s.get("roles") or {}
    cap = _f(s.get("tank_gal"))
    return {r: reading(r, hastates.get(stack, roles[r]), cap) for r in ROLE if roles.get(r)}


def vm_samples(entity_id, start, end):
    """[(t, value)] of an entity's state from VictoriaMetrics (every change Home Assistant sent, and the value once a
    minute), oldest first."""
    q = urllib.parse.urlencode({"match[]": f'ha_state{{entity_id="{entity_id}"}}', "start": f"{start:.3f}",
                                "end": f"{end:.3f}"})
    out = []
    with urllib.request.urlopen(f"{VM}/api/v1/export?{q}", timeout=20) as r:
        for line in r.read().decode().splitlines():
            if line.strip():
                d = json.loads(line)
                out += [(ts / 1000, float(v)) for ts, v in zip(d.get("timestamps", []), d.get("values", []))]
    return sorted(out)


def _day(st, t):
    d = time.strftime("%Y-%m-%d", time.localtime(t))
    return st["days"].setdefault(d, {"runs": 0, "run_s": 0.0, "max_run_s": 0.0, "night_starts": [], "meter_gal": 0.0,
                                     "night_meter": [], "tank_used_gal": 0.0, "tank_filled_gal": 0.0,
                                     "tank_used_pct": 0.0})


def _hour(t):
    return time.localtime(t).tm_hour


def pump_step(st, samples, upto, scale=1.0):
    """Follow the pump through its power readings up to `upto`; finished runs go to st["runs"] and the days.
    Returns the runs finished."""
    p = st["pump"]
    done = []
    for t, w in samples:
        if t <= p["cursor"] or t > upto:
            continue
        w *= scale
        p["cursor"], p["seen"] = t, t
        on = w >= ON_W
        if on and not p["on"]:
            p.update(on=True, since=t, w_sum=0.0, w_n=0)
            if _hour(t) in NIGHT_HOURS:
                d = _day(st, t)
                if _hour(t) not in d["night_starts"]:
                    d["night_starts"].append(_hour(t))
        if on:
            if t - p["since"] >= 5:            # the start's inrush left out of its average
                p["w_sum"] += w
                p["w_n"] += 1
        elif p["on"]:
            dur = t - p["since"]
            avg = p["w_sum"] / p["w_n"] if p["w_n"] else None
            run = {"start": round(p["since"], 1), "s": round(dur, 1), "w": None if avg is None else round(avg, 1)}
            st["runs"] = (st["runs"] + [run])[-KEEP_RUNS:]
            d = _day(st, p["since"])
            d["runs"] += 1
            d["run_s"] = round(d["run_s"] + dur, 1)
            d["max_run_s"] = max(d["max_run_s"], round(dur, 1))
            p.update(on=False, since=None, w_sum=0.0, w_n=0)
            done.append(run)
    return done          # the cursor stays at the last reading: one that reaches VictoriaMetrics late isn't lost


def normal_w(runs):
    """The pump's usual running power: the median of its last 50 runs of a minute or more."""
    ws = [r["w"] for r in runs if r.get("w") and r["s"] >= 60][-50:]
    return round(statistics.median(ws), 1) if len(ws) >= LEARN_RUNS else None


def meter_step(st, total, now):
    """Use from the meter's total since the last reading."""
    m = st["meter"]
    st["meter"] = {"total": total, "t": now}
    if not m:
        return 0.0
    used = total - m["total"]
    if not 0 <= used <= METER_MAX_GAL:
        return 0.0
    d = _day(st, now)
    d["meter_gal"] = round(d["meter_gal"] + used, 3)
    h = _hour(now)
    if used > 0 and h in NIGHT_HOURS and h not in d["night_meter"]:
        d["night_meter"].append(h)
    return used


def tank_step(st, pct, gal, now):
    """Use and fills from the tank's level: drops count as use, a rise of TANK_FILL as a fill; less is noise."""
    level = pct if pct is not None else gal
    if level is None:
        return
    t = st["tank"]
    if not t or t.get("by") != ("pct" if pct is not None else "gal"):
        st["tank"] = {"ref": level, "by": "pct" if pct is not None else "gal", "fills": (t or {}).get("fills", [])}
        return
    # by volume (no capacity entered): 1% and 2% of the level now, at least 1 and 2 gallons
    step = TANK_STEP if t["by"] == "pct" else max(1.0, TANK_STEP / 100 * gal)
    fill = TANK_FILL if t["by"] == "pct" else max(2.0, TANK_FILL / 100 * gal)
    per_gal = (gal / pct) if (pct and gal) else (1.0 if t["by"] == "gal" else None)
    d = _day(st, now)
    if level <= t["ref"] - step:
        drop = t["ref"] - level
        if t["by"] == "pct":
            d["tank_used_pct"] = round(d["tank_used_pct"] + drop, 2)
        if per_gal:
            d["tank_used_gal"] = round(d["tank_used_gal"] + drop * per_gal, 2)
        t["ref"] = level
    elif level >= t["ref"] + fill:
        added = level - t["ref"]
        if per_gal:
            d["tank_filled_gal"] = round(d["tank_filled_gal"] + added * per_gal, 2)
        t["fills"] = (t["fills"] + [{"at": now, "added_gal": None if not per_gal else round(added * per_gal, 1),
                                     "after_pct": None if pct is None else round(pct, 1)}])[-50:]
        t["ref"] = level
    elif level > t["ref"]:
        t["ref"] = level                    # small rises (sloshing, temperature): follow without counting


def use_of(day, s, roles):
    """(gallons, source) for one day: the meter, else the tank, else the pump estimate."""
    if roles.get("water_meter"):
        return round(day.get("meter_gal", 0.0), 1), "meter"
    if roles.get("tank_level") and (_f(s.get("tank_gal")) or day.get("tank_used_gal")):
        return round(day.get("tank_used_gal", 0.0), 1), "tank"
    gpm = _f(s.get("pump_gpm"))
    if roles.get("pump_power") and gpm:
        return round(day.get("run_s", 0.0) / 60 * gpm, 1), "pump"
    return None, None


def usual_use(st, s, now, span=7):
    """The average use per day over the last `span` full days (at least 3), and how many days it is from."""
    roles = s.get("roles") or {}
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    full = [d for d in sorted(st["days"]) if d < today][-span:]
    if st.get("since"):
        first = time.strftime("%Y-%m-%d", time.localtime(st["since"]))
        full = [d for d in full if d > first]        # the first day was partial
    vals = [u for u in (use_of(st["days"][d], s, roles)[0] for d in full) if u is not None]
    return (round(sum(vals) / len(vals), 1), len(vals)) if len(vals) >= 3 else (None, len(vals))


# --------------------------------------------------------------------------
# Every minute
# --------------------------------------------------------------------------
def step(stack, s, r, now=None, alert=None, fetch=None):
    """One minute: follow the pump, count use, check the rules. `alert(tag, title, message, priority)` sends;
    fetch(entity_id, start, end) -> [(t, value)] (VictoriaMetrics by default). Returns log lines."""
    now = now or time.time()
    st = load(stack)
    st["since"] = st["since"] or now
    roles = s.get("roles") or {}
    msgs = []

    # --- the pump
    pump = None
    if roles.get("pump_power"):
        if not st["pump"] or st["pump"].get("entity") != roles["pump_power"]:
            st["pump"] = {"entity": roles["pump_power"], "cursor": now - 120, "seen": None, "on": False, "since": None,
                          "w_sum": 0.0, "w_n": 0}
        p = st["pump"]
        p["cursor"] = max(p["cursor"], now - MAX_GAP_S)
        cur = hastates.get(stack, roles["pump_power"]) or {}
        scale = 1000.0 if str(cur.get("unit") or "").lower() == "kw" else 1.0
        try:
            samples = (fetch or vm_samples)(roles["pump_power"], p["cursor"], now - LAG_S)
            pump_step(st, samples, now - LAG_S, scale)
        except (OSError, ValueError) as e:
            msgs.append(f"pump readings not fetched: {e}")
        st["normal_w"] = normal_w(st["runs"]) or st["normal_w"]
        known = p["seen"] is not None and now - p["seen"] < STALE_S + LAG_S
        pump = {"on": bool(p["on"]), "for_s": round(now - p["since"], 0) if p["on"] else 0,
                "w": round(p["w_sum"] / p["w_n"], 1) if p["on"] and p["w_n"] else None, "known": known}
    else:
        st["pump"] = None

    # --- the meter and the tank
    if r.get("water_meter", {}).get("ok"):
        meter_step(st, r["water_meter"]["value"], now)
    tank = r.get("tank_level", {}).get("value") if r.get("tank_level", {}).get("ok") else None
    if tank:
        tank_step(st, tank.get("percent"), tank.get("gal"), now)

    # --- overnight: at 5 am, last night's verdict
    today = _day(st, now)
    d0 = time.strftime("%Y-%m-%d", time.localtime(now))
    if _hour(now) >= NIGHT_HOURS[-1] + 1 and st["night"].get("date") != d0:
        hours = today["night_meter"] if roles.get("water_meter") else today["night_starts"] if roles.get("pump_power") else None
        st["night"] = {"date": d0, "flow": hours is not None and all(h in hours for h in NIGHT_HOURS),
                       "by": "meter" if roles.get("water_meter") else "pump"}

    # --- rules
    offline = [ROLE[k][1] for k, v in r.items() if not v["ok"]]
    nw = st["normal_w"]
    amps = _f(s.get("pump_amps"))
    on = bool(pump and pump["on"] and pump["known"])
    recent = [x for x in st["runs"] if x["start"] >= now - LAG_S - RAPID_S]
    pct = tank.get("percent") if tank else None
    cond = {
        "long_run": on and pump["for_s"] >= int(s.get("max_run_min") or 20) * 60,
        "low_power": on and nw is not None and pump["for_s"] >= 60 and pump["w"] is not None and pump["w"] < nw * LOW_POWER,
        "overload": on and amps is not None and pump["for_s"] >= OVERLOAD_MIN_S and pump["w"] is not None
        and pump["w"] > amps * VOLTS,
        "rapid": sum(1 for x in recent if x["s"] < RAPID_RUN_S) >= RAPID_N,
        "night_flow": bool(st["night"].get("flow")) and st["night"].get("date") == d0,
        "tank_low": pct is not None and pct < float(s.get("tank_warn") or 30),
        "tank_critical": pct is not None and pct < float(s.get("tank_critical") or 15),
        "offline": bool(offline),
    }
    snapshot = {"pump": pump, "tank": tank, "normal_w": nw}
    for name, hold, title, prio in RULES:
        if cond[name]:
            since = st["timers"].setdefault(name, now)
            if now - since >= hold and name not in st["active"]:
                st["active"][name] = now
                detail = _detail(name, s, pump, tank, nw, offline, st)
                entry = {"at": now, "rule": name, "title": title, "detail": detail, "readings": snapshot,
                         "action": "alert sent" if s.get("alerts") else "logged"}
                if s.get("alerts") and alert:
                    err = alert(f"water_{name}", title, detail, prio)
                    if err:
                        entry["action"] = f"alert not sent: {err}"
                st["log"] = (st["log"] + [entry])[-KEEP_LOG:]
                msgs.append(f"alert: {title} ({detail})")
        else:
            st["timers"].pop(name, None)
            if name in st["active"]:
                started = st["active"].pop(name)
                for e in reversed(st["log"]):
                    if e["rule"] == name and e["at"] == started:
                        e["cleared"] = now
                        break
                if s.get("alerts") and alert:
                    alert(f"water_{name}", "", "clear", "info")
                msgs.append(f"cleared: {title}")

    st["days"] = dict(sorted(st["days"].items())[-KEEP_DAYS:])
    st["last"] = {"t": now, "pump": pump, "tank": tank, "offline": offline,
                  "meter": r["water_meter"]["value"] if r.get("water_meter", {}).get("ok") else None}
    save(stack, st)
    return msgs


def _mins(sec):
    return f"{sec / 60:.0f} min" if sec >= 120 else f"{sec:.0f} s"


def _detail(name, s, pump, tank, nw, offline, st):
    if name == "long_run":
        return (f"The pump has run for {_mins(pump['for_s'])} without stopping (limit {s.get('max_run_min') or 20} min): "
                f"check for a broken pipe, an open tap, or a pump that has lost its prime.")
    if name == "low_power":
        return (f"It is drawing {pump['w']:.0f} W, its usual is {nw:.0f} W: it may have lost its prime or be running "
                f"dry. Check the water supply.")
    if name == "overload":
        return (f"It is drawing {pump['w']:.0f} W, more than its nameplate ({float(s['pump_amps']):g} A x {VOLTS} V): "
                f"the motor or bearings may be failing.")
    if name == "rapid":
        return (f"{RAPID_N} or more runs under {RAPID_RUN_S} s in {RAPID_S // 60} minutes: the pressure tank may be "
                f"waterlogged (check its air charge) or the pressure switch failing.")
    if name == "night_flow":
        how = "the meter counted water" if st["night"].get("by") == "meter" else "the pump started"
        return f"Between 1 and 5 am {how} in every hour: check for a running toilet or a leak."
    if name in ("tank_low", "tank_critical"):
        g = f" ({tank['gal']:,.0f} gal)" if tank.get("gal") is not None else ""
        return f"The tank is at {tank['percent']:.0f}%{g}."
    if name == "offline":
        return "No reading for 30 minutes from: " + ", ".join(offline)
    return ""


# --------------------------------------------------------------------------
# Reading back
# --------------------------------------------------------------------------
def starts_since(st, t0):
    return sum(1 for x in st["runs"] if x["start"] >= t0) + (1 if (st["pump"] or {}).get("on") and
                                                              (st["pump"]["since"] or 0) >= t0 else 0)


def summary(stack, s, now=None):
    now = now or time.time()
    st = load(stack)
    roles = s.get("roles") or {}
    d0 = time.strftime("%Y-%m-%d", time.localtime(now))
    today = st["days"].get(d0) or {}
    use, source = use_of(today, s, roles) if today else (None, None)
    usual, n = usual_use(st, s, now)
    tank = (st["last"] or {}).get("tank")
    gal = tank.get("gal") if tank else None
    days_left = None
    if gal is not None and usual:
        days_left = round(gal / usual, 1) if gal / usual < 10 else round(gal / usual)
    pump = (st["last"] or {}).get("pump")
    run_s = today.get("run_s", 0.0) + ((pump or {}).get("for_s") or 0 if (pump or {}).get("on") else 0)
    return {"last": st["last"], "age": (now - st["last"]["t"]) if st["last"] else None, "today": today,
            "use_today": use, "use_source": source, "usual": usual, "usual_days": n, "days_left": days_left,
            "pump": pump, "run_s_today": run_s, "starts_today": today.get("runs", 0) + (1 if (pump or {}).get("on") else 0),
            "starts_last_hour": starts_since(st, now - 3600), "normal_w": st["normal_w"], "runs": st["runs"],
            "days": st["days"], "log": st["log"], "active": st["active"], "since": st["since"],
            "fills": (st["tank"] or {}).get("fills", []), "night": st["night"]}


def vm_lines(stack, s, now=None):
    v = summary(stack, s, now)
    if not v["last"]:
        return []
    lines = []
    if v["pump"]:
        lines += [f"water_pump_running {1 if v['pump']['on'] else 0}", f"water_pump_run_seconds_today {v['run_s_today']:g}",
                  f"water_pump_starts_today {v['starts_today']}"]
        if v["normal_w"]:
            lines.append(f"water_pump_usual_watts {v['normal_w']:g}")
    if v["use_today"] is not None:
        lines.append(f'water_used_today_gal{{source="{v["use_source"]}"}} {v["use_today"]:g}')
    tank = v["last"].get("tank") or {}
    if tank.get("percent") is not None:
        lines.append(f"water_tank_level_percent {tank['percent']:g}")
    if tank.get("gal") is not None:
        lines.append(f"water_tank_gallons {tank['gal']:g}")
    if v["days_left"] is not None:
        lines.append(f"water_days_left {v['days_left']:g}")
    for name, *_ in RULES:
        lines.append(f'water_rule_active{{rule="{name}"}} {1 if name in v["active"] else 0}')
    return lines


def vm_push(stack, s):
    lines = vm_lines(stack, s)
    if lines:
        req = urllib.request.Request(f"{VM}/api/v1/import/prometheus", data=("\n".join(lines) + "\n").encode(), method="POST")
        urllib.request.urlopen(req, timeout=10).read()
