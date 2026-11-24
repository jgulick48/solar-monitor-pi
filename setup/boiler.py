"""Boiler: watches the boiler and the floor heating, monitoring only (wiki: "Boiler, propane and notifications"). The
boiler module (modules/boiler) is its switch and settings; this file does the work.

Every reading comes from Home Assistant through the HA state feed (hastates.get): the probe board's temperatures, the
thermostat, the boiler's and water pump's metering plugs, and the optional pump clamps and wired call for heat. The
owner picks which Home Assistant entity fills each role; three are required (floor supply, floor return, boiler
supply), the rest add accuracy or a signal. Every minute the collector calls step(), which:

  * learns each probe pair's offset (floor supply/return, boiler supply/return) while no heat flows: both probes then
    sit in the same still water, so the difference they show is their own error (DS18B20s are +-0.5 C each)
  * learns the boiler plug's power levels (controls only, pump, pump + fan + burner) from a week of readings, so it
    knows when the burner runs
  * classes each minute and each burner run: heating, hot water (the hot-water outlet probe jumps, or the water pump
    runs, while the burner fires), idle or unclear
  * learns the boiler's supply temperature against the outdoor temperature (its outdoor reset curve)
  * checks the three fault rules the reset automation will act on later (not running on demand, running but no heat,
    stopped during a call) and LOGS them: nothing is switched in this step of the rollout
  * sends the alerts that never reset anything: floor supply too hot, freeze risk, a device offline

  state/boiler.json   {"since", "last", "hist", "offsets", "power_bins", "levels", "run", "call", "runs", "days",
                       "curve", "timers", "active", "log", "dhw_last", "quiet_since"}
"""
import json
import math
import os
import time
import urllib.request

import hastates
import solar_setup as ss

VM = "http://127.0.0.1:8428"

# role, label, kind, need ("required" | "recommended" | "optional"), what it gives
ROLES = (
    ("floor_supply", "Floor supply", "temperature", "required",
     "with floor return, the heat delivered to the floor; the floor-too-hot alert"),
    ("floor_return", "Floor return", "temperature", "required", "see floor supply; the freeze-risk alert"),
    ("boiler_supply", "Boiler supply", "temperature", "required",
     "whether the boiler heats and how hot; its outdoor reset curve; the fault rules"),
    ("boiler_return", "Boiler return", "temperature", "optional",
     "whether the boiler condenses (return under about 54 °C / 130 °F), and the boiler loop's own difference"),
    ("hot_water", "Hot water outlet", "temperature", "optional",
     "tells a hot-water draw from heating at once (without it, the water pump's plug does)"),
    ("slab_near", "Slab near boiler", "temperature", "optional", "how the floor itself responds and lags"),
    ("slab_far", "Slab far", "temperature", "optional", "with slab near, how evenly the slab heats"),
    ("thermostat", "Thermostat", "climate", "recommended", "the call for heat: the fault rules need it"),
    ("heat_call", "Wired call for heat", "binary_sensor", "optional", "the call for heat without relying on the thermostat"),
    ("boiler_power", "Boiler plug power", "power", "recommended", "whether the burner runs: the fault rules need it"),
    ("boiler_switch", "Boiler plug switch", "switch", "optional", "recorded now; the reset automation (a later step) switches it"),
    ("water_pump_power", "Water pump plug power", "power", "optional", "backs up the hot-water detection"),
    ("boiler_pump_current", "Boiler pump current", "current", "optional", "the boiler pump really running"),
    ("floor_pump_current", "Floor pump current", "current", "optional", "the floor pump really running"),
)
ROLE = {r[0]: r for r in ROLES}
REQUIRED = tuple(r[0] for r in ROLES if r[3] == "required")
TEMPS = tuple(r[0] for r in ROLES if r[2] == "temperature")
PAIRS = (("floor", "floor_supply", "floor_return"), ("boiler", "boiler_supply", "boiler_return"))
# what Home Assistant's entity registry offers for each kind of role: (domains, device_class)
KINDS = {"temperature": (("sensor",), "temperature"), "power": (("sensor",), "power"), "current": (("sensor",), "current"),
         "climate": (("climate",), None), "binary_sensor": (("binary_sensor",), None), "switch": (("switch",), None)}

# the heating fluid's factor in BTU/h = factor x GPM x difference (°F): from its density and heat capacity (approximate)
FLUIDS = (("water", "Water", 500), ("pg30", "Propylene glycol 30%", 477), ("pg40", "Propylene glycol 40%", 466),
          ("pg50", "Propylene glycol 50%", 452), ("eg50", "Ethylene glycol 50%", 428))
FLUID = {k: f for k, _, f in FLUIDS}
DEFAULTS = {"roles": {r[0]: "" for r in ROLES}, "alerts": True, "floor_max_c": 50.0, "freeze_c": 10.0, "burner_w": "",
            "floor_gpm": "", "fluid": "water", "resets": False,
            # efficiency and sizing (entered once; blank = not used)
            "boiler_min_btuh": 18000, "boiler_max_btuh": 120000, "floor_area_ft2": "", "indoor_c": 20.0,
            "design_c": ""}

PROPANE_BTU_PER_GAL = 91500     # higher heating value, as boiler efficiency ratings use
SLAB_BTUH_FT2 = (25, 35)        # what a slab delivers per square foot at comfortable floor temperatures
HDD_BASE_F = 65.0
MIN_DT_F = 5.0                  # days warmer than this under the indoor temperature don't count for heat loss

# Resets (the Home Assistant automation in packages/solar_boiler.yaml switches the plug; the box decides whether it may)
RESET_OFF_S = 30                # the plug is off this long
HEAT_BACK_S = 20 * 60           # after it is back on, heat must be back within this
HEAT_BACK_FLOOR_C = 0.5         # ... the floor supply climbing at least this much
LIMITS = ((6 * 3600, 1), (24 * 3600, 2))   # at most 1 reset per 6 hours and 2 per 24 hours
UNLOCK_S = 24 * 3600            # after the limit: faults only notify until Clear, or this long with no fault
NOT_STARTED_S = 10 * 60         # allowed this long with no reset from Home Assistant: say so
PROPANE_MIN = 10.0
PROPANE_AGE_S = 6 * 3600
SEASON = "input_boolean.solar_boiler_resets"   # Home Assistant's switch: "Boiler automatic resets" (the heating season)
KEEP_ATTEMPTS = 100

HIST_S = 3600                   # temperatures kept for rises and baselines
QUIET_S = 30 * 60               # no heat flowing this long before the probe pairs are zeroed
STILL_C = 0.3                   # ... and both probes moved less than this over the last 10 minutes
OFFSET_MAX_C = 2.0              # a pair further apart than this at rest is a probe problem, not an offset
OFFSET_ALPHA = 0.05
LEARN_SAMPLES = 24 * 60         # a day of plug readings before its power levels are trusted
BIN_W = 5
PUMP_ON_A = 0.3
WATER_PUMP_ON_W = 30
DHW_RISE_C = 8.0                # the hot-water outlet this far over its last hour's low: water is being drawn
DHW_PAUSE_S = 600               # fault rules wait this long after a hot-water draw
RUN_CHECK_S = 600               # a burner run must have raised the boiler supply by NO_HEAT_C within this
NO_HEAT_C = 5.0
CURVE_BIN_C = 5
KEEP_RUNS, KEEP_LOG, KEEP_DAYS = 300, 200, 60

STATES = {"idle": 0, "heating": 1, "hot water": 2, "unclear": 3, "offline": -1, "setup": -2}
# (name, kind, hold seconds, title): kind "fault" = what the reset automation will act on (logged only for now);
# "alert" = sent through the notifications module, never resets; "note" = logged only
RULES = (
    ("no_run", "fault", 15 * 60, "Not running on demand: the thermostat calls, the plug shows controls only"),
    ("no_heat", "fault", 0, "Running but no heat: the burner draws power, the boiler supply hasn't risen 5 °C in 10 minutes"),
    ("stopped", "fault", 10 * 60, "Stopped during a call: the burner stopped and the boiler supply is cold while the thermostat calls"),
    ("floor_hot", "alert", 5 * 60, "Floor supply too hot"),
    ("freeze", "alert", 5 * 60, "Freeze risk: floor return cold"),
    ("offline", "alert", 30 * 60, "Boiler sensors offline"),
    ("unexplained", "note", 30 * 60, "Burner running with no call for heat and no hot-water draw"),
)
RULE = {r[0]: r for r in RULES}


def _path(stack):
    return os.path.join(stack, "state", "boiler.json")


def load(stack):
    st = ss.load_json(_path(stack), {})
    for k, v in (("since", None), ("last", {}), ("hist", []), ("offsets", {}), ("power_bins", {}), ("levels", None),
                 ("run", None), ("call", None), ("runs", []), ("days", {}), ("curve", {}), ("timers", {}),
                 ("active", {}), ("log", []), ("dhw_last", 0), ("quiet_since", None)):
        st.setdefault(k, v)
    return st


def save(stack, st):
    ss.write_if_changed(_path(stack), json.dumps(st, indent=1) + "\n", 0o644)


# --------------------------------------------------------------------------
# Readings
# --------------------------------------------------------------------------
def to_c(value, unit):
    return (value - 32) * 5 / 9 if unit and "F" in str(unit) else value


def reading(role, cur, ha_unit="°C"):
    """One role's reading from its Home Assistant state: {"ok", "value", "raw"}; value in °C, W, A, or for the
    thermostat and the call for heat True while calling. The thermostat's also carries "indoor" (°C): its room
    temperature, which Home Assistant gives in its own unit system (ha_unit), with no unit of its own."""
    if not cur or cur.get("state") in (None, "unavailable", "unknown", ""):
        return {"ok": False, "value": None, "raw": (cur or {}).get("state")}
    kind, raw = ROLE[role][2], cur.get("state")
    if kind == "climate":
        attrs = cur.get("attributes") or {}
        action = str(attrs.get("hvac_action") or "").lower()
        room = hastates.number(attrs.get("current_temperature"))
        return {"ok": True, "value": action in ("heating", "preheating") if action else None, "raw": action or raw,
                "indoor": None if room is None else round(to_c(room, ha_unit), 2)}
    if kind in ("binary_sensor", "switch"):
        return {"ok": True, "value": str(raw).lower() == "on", "raw": raw}
    v = hastates.number(raw)
    if v is None:
        return {"ok": False, "value": None, "raw": raw}
    unit = cur.get("unit") or ""
    if kind == "temperature":
        v = to_c(v, unit)
    elif kind == "power" and unit.lower() == "kw":
        v *= 1000
    return {"ok": True, "value": v, "raw": raw}


def readings(stack, s):
    roles = s.get("roles") or {}
    site = ss.load_json(os.path.join(stack, "state", "site.json"), {})
    unit = "°F" if site.get("units", "us_customary") == "us_customary" else "°C"   # the box sets HA's unit system
    return {r: reading(r, hastates.get(stack, roles[r]), unit) for r in ROLE if roles.get(r)}


def outdoor_c(stack, now=None):
    """The outdoor temperature from the forecast the box already fetches (Open-Meteo), if fresh."""
    fc = ss.load_json(os.path.join(stack, "state", "wx-forecast.json"), {})
    t = (fc.get("current") or {}).get("temperature_2m")
    if t is None or (now or time.time()) - fc.get("fetched", 0) > 3 * 3600:
        return None
    return round(to_c(t, "°F" if fc.get("units_us") else "°C"), 1)


# --------------------------------------------------------------------------
# Learning
# --------------------------------------------------------------------------
def power_levels(bins):
    """Three power levels (controls only, pump, pump + fan + burner) from a histogram of plug readings, by k-means on
    one dimension; None until there's a day of readings and three clearly separate levels."""
    pts = sorted((float(k), n) for k, n in bins.items() if n)
    total = sum(n for _, n in pts)
    if total < LEARN_SAMPLES:
        return None
    def pct(p):
        acc = 0
        for v, n in pts:
            acc += n
            if acc >= p * total:
                return v
        return pts[-1][0]
    c = [pct(0.05), pct(0.5), pct(0.95)]
    for _ in range(30):
        groups = [[0.0, 0], [0.0, 0], [0.0, 0]]
        for v, n in pts:
            i = min(range(3), key=lambda j: abs(v - c[j]))
            groups[i][0] += v * n
            groups[i][1] += n
        new = [g[0] / g[1] if g[1] else c[i] for i, g in enumerate(groups)]
        if new == c:
            break
        c = new
    c = sorted(c)
    weights = [sum(n for v, n in pts if min(range(3), key=lambda j: abs(v - c[j])) == i) for i in range(3)]
    if c[2] - c[0] < 30 or min(weights) < 0.02 * total:
        return None
    return {"idle": round(c[0] + BIN_W / 2), "pump": round(c[1] + BIN_W / 2), "burner": round(c[2] + BIN_W / 2),
            "burner_on": round((c[1] + c[2]) / 2 + BIN_W / 2), "idle_max": round((c[0] + c[1]) / 2 + BIN_W / 2),
            "samples": total}


def levels(st, s):
    """The learned levels, or the owner's own burner threshold."""
    if str(s.get("burner_w") or "").strip():
        w = float(s["burner_w"])
        lv = dict(st.get("levels") or {})
        lv.update(burner_on=w, idle_max=min(lv.get("idle_max", w / 2), w), manual=True)
        return lv
    return st.get("levels")


def heat_output(s, floor_diff_c, flowing):
    """Heat delivered to the floor, BTU/h: factor x GPM x the floor difference in °F, while the floor pump runs; None
    when the floor flow isn't entered."""
    try:
        gpm = float(s.get("floor_gpm") or 0)
    except (TypeError, ValueError):
        gpm = 0
    if gpm <= 0 or floor_diff_c is None:
        return None
    if not flowing:
        return 0.0
    return round(FLUID.get(s.get("fluid"), 500) * gpm * max(0.0, floor_diff_c) * 9 / 5)


def burner_minutes(st, now, span=3600):
    """Minutes the burner ran in the last `span` seconds (finished runs and the one running now)."""
    total = 0.0
    for run in st["runs"] + ([st["run"]] if st["run"] else []):
        end = run.get("end") or now
        total += max(0.0, min(end, now) - max(run["start"], now - span))
    return round(total / 60, 1)


def offset(st, pair):
    return (st["offsets"].get(pair) or {}).get("c", 0.0)


def corrected(st, r):
    """Temperatures in °C with each pair's learned offset applied to its return probe."""
    out = {k: r[k]["value"] for k in TEMPS if k in r and r[k]["ok"]}
    for pair, sup, ret in PAIRS:
        if ret in out:
            out[ret] = out[ret] + offset(st, pair)
    return out


def _hist_at(st, now, age):
    """The temperatures kept about `age` seconds ago (the oldest sample not newer than that), or None."""
    best = None
    for h in st["hist"]:
        if h["t"] <= now - age:
            best = h
    return best


# --------------------------------------------------------------------------
# Every minute
# --------------------------------------------------------------------------
def step(stack, s, r, now=None, outdoor=None, alert=None, ctx=None):
    """One minute: learn, class, check the rules, and decide whether a reset may run. `r` is readings();
    `alert(tag, title, message, priority)` sends; ctx = {"propane": the propane module's summary or None (module
    off), "season": Home Assistant's resets switch (True, False, None = not known)}. Returns messages to log."""
    now = now if now is not None else time.time()
    st = load(stack)
    msgs = []
    st["since"] = st["since"] or now
    last_t = st["last"].get("t")
    dt = min(max(now - last_t, 0), 180) / 60 if last_t else 1.0
    roles = s.get("roles") or {}
    raw = {k: r[k]["value"] for k in TEMPS if k in r and r[k]["ok"]}
    t = corrected(st, r)

    def val(role):
        return r[role]["value"] if role in r and r[role]["ok"] else None

    # --- signals
    power = val("boiler_power")
    if power is not None:
        b = str(int(power // BIN_W * BIN_W))
        st["power_bins"][b] = st["power_bins"].get(b, 0) + 1
        st["levels"] = power_levels(st["power_bins"])
    lv = levels(st, s)
    burner = (power >= lv["burner_on"]) if (power is not None and lv) else None
    calls = [v for v in (val("thermostat"), val("heat_call")) if v is not None]
    calling = any(calls) if calls else None
    currents = [v for v in (val("boiler_pump_current"), val("floor_pump_current")) if v is not None]
    pumps = any(a >= PUMP_ON_A for a in currents) if currents else calling

    st["hist"].append({"t": now, **{k: round(v, 3) for k, v in raw.items()}})
    st["hist"] = [h for h in st["hist"] if h["t"] >= now - HIST_S]

    dhw = False
    if "hot_water" in raw:
        low = min(h.get("hot_water", raw["hot_water"]) for h in st["hist"])
        prev = _hist_at(st, now, 110) or {}
        rising = raw["hot_water"] - prev.get("hot_water", raw["hot_water"]) >= 1.0
        dhw = raw["hot_water"] - low >= DHW_RISE_C and (rising or bool(burner))
    elif val("water_pump_power") is not None:
        dhw = val("water_pump_power") >= WATER_PUMP_ON_W and bool(burner)
    if dhw:
        st["dhw_last"] = now
    recent_dhw = now - st["dhw_last"] < DHW_PAUSE_S

    missing = [k for k in REQUIRED if not roles.get(k)]
    offline = [k for k in roles if roles.get(k) and k in r and not r[k]["ok"]]
    floor_diff = (t["floor_supply"] - t["floor_return"]) if {"floor_supply", "floor_return"} <= t.keys() else None
    if missing:
        state = "setup"
    elif any(k in offline for k in REQUIRED):
        state = "offline"
    elif dhw:
        state = "hot water"
    elif burner:
        state = "heating" if calling or (calling is None and floor_diff is not None and floor_diff >= 2) else "unclear"
    elif burner is False:
        state = "idle"
    else:                                  # no plug: from the temperatures alone
        state = "heating" if (calling is not False and floor_diff is not None and floor_diff >= 2) else "idle"

    # --- heat delivered to the floor (while the floor pump runs: the pump clamp, else the call for heat)
    floor_pump = val("floor_pump_current")
    flowing = (floor_pump >= PUMP_ON_A) if floor_pump is not None else bool(calling) if calling is not None \
        else state == "heating"
    output = heat_output(s, floor_diff, flowing)

    # --- probe offsets, while no heat flows
    still = False
    old = _hist_at(st, now, 600)
    if old and "floor_supply" in raw and "floor_supply" in old:
        still = abs(raw["floor_supply"] - old["floor_supply"]) < STILL_C and \
            all(abs(raw[k] - old[k]) < STILL_C for k in ("floor_return", "boiler_supply", "boiler_return") if k in raw and k in old)
    quiet = still and not burner and not pumps and not dhw and state not in ("heating", "hot water")
    if calling is None and not currents:     # nothing says the pumps are off: only trust a small, still difference
        quiet = quiet and floor_diff is not None and abs(raw["floor_supply"] - raw["floor_return"]) < OFFSET_MAX_C
    st["quiet_since"] = (st["quiet_since"] or now) if quiet else None
    if quiet and now - st["quiet_since"] >= QUIET_S:
        for pair, sup, ret in PAIRS:
            if sup in raw and ret in raw:
                d = raw[sup] - raw[ret]
                if abs(d) <= OFFSET_MAX_C:
                    o = st["offsets"].setdefault(pair, {"c": d, "n": 0})
                    o["c"] = round(o["c"] + OFFSET_ALPHA * (d - o["c"]) if o["n"] else d, 3)
                    o["n"] += 1
                    o["at"] = now
                else:
                    st["offsets"].setdefault(pair, {"c": 0.0, "n": 0})["wide"] = round(d, 2)

    # --- burner runs, and the call
    day = st["days"].setdefault(time.strftime("%Y-%m-%d", time.localtime(now)),
                                {"burner_min": 0.0, "heat_min": 0.0, "dhw_min": 0.0, "runs": 0, "faults": 0})
    day.setdefault("heat_kbtu", 0.0)
    indoor = r["thermostat"].get("indoor") if r.get("thermostat", {}).get("ok") else None
    for key, v in (("out", outdoor), ("in", indoor)):
        if v is not None:
            day[key + "_sum"] = round(day.get(key + "_sum", 0.0) + v, 2)
            day[key + "_n"] = day.get(key + "_n", 0) + 1
    if output:
        day["heat_kbtu"] = round(day["heat_kbtu"] + output * dt / 60 / 1000, 2)
    if burner:
        run = st["run"]
        if run is None:
            run = st["run"] = {"start": now, "heat_min": 0.0, "dhw_min": 0.0, "other_min": 0.0,
                               "supply_start": t.get("boiler_supply"), "supply_max": t.get("boiler_supply"),
                               "outdoor": outdoor}
            day["runs"] += 1
        cls = "dhw_min" if dhw else "heat_min" if state == "heating" else "other_min"
        run[cls] = round(run[cls] + dt, 2)
        if t.get("boiler_supply") is not None:
            run["supply_max"] = max(run["supply_max"] if run["supply_max"] is not None else -99, t["boiler_supply"])
            if run["supply_start"] is None:
                run["supply_start"] = t["boiler_supply"]
        day["burner_min"] = round(day["burner_min"] + dt, 2)
        if cls != "other_min":
            day[cls] = round(day[cls] + dt, 2)
        # the outdoor reset curve: the supply after 10 minutes of steady heating
        if state == "heating" and not recent_dhw and outdoor is not None and now - run["start"] >= RUN_CHECK_S \
                and t.get("boiler_supply") is not None:
            b = str(int(math.floor(outdoor / CURVE_BIN_C) * CURVE_BIN_C))
            c = st["curve"].setdefault(b, {"c": t["boiler_supply"], "n": 0})
            c["c"] = round(c["c"] + 0.1 * (t["boiler_supply"] - c["c"]), 2)
            c["n"] += 1
    elif burner is False and st["run"]:
        run = st["run"]
        run["end"] = now
        run["minutes"] = round((now - run["start"]) / 60, 1)
        run["class"] = ("hot water" if run["dhw_min"] >= max(run["heat_min"], run["other_min"]) else
                        "heating" if run["heat_min"] >= run["other_min"] else "unclear")
        st["runs"] = (st["runs"] + [run])[-KEEP_RUNS:]
        st["run"] = None
        msgs.append(f"burner run: {run['class']}, {run['minutes']} min")
    if calling:
        st["call"] = st["call"] or {"start": now, "burned": False}
        if burner:
            st["call"]["burned"] = True
    else:
        st["call"] = None

    # --- rules
    run = st["run"]
    rules_ready = calling is not None and burner is not None
    cond = {
        "no_run": rules_ready and calling and not recent_dhw and burner is False and not (st["call"] or {}).get("burned"),
        "stopped": rules_ready and calling and not recent_dhw and burner is False and bool((st["call"] or {}).get("burned"))
        and "boiler_supply" in t and "floor_return" in t and t["boiler_supply"] < t["floor_return"] + 2,
        "no_heat": bool(burner) and not recent_dhw and run is not None and now - run["start"] >= RUN_CHECK_S
        and run.get("supply_start") is not None and (run.get("supply_max") or -99) - run["supply_start"] < NO_HEAT_C,
        "floor_hot": t.get("floor_supply") is not None and t["floor_supply"] > float(s["floor_max_c"]),
        "freeze": t.get("floor_return") is not None and t["floor_return"] < float(s["freeze_c"]),
        "offline": bool(offline),
        "unexplained": bool(burner) and calling is False and not recent_dhw,
    }
    snapshot = {k: round(v, 1) for k, v in t.items()}
    snapshot.update(power=power, calling=calling, outdoor=outdoor, state=state)
    for name, kind, hold, title in RULES:
        if cond[name]:
            since = st["timers"].setdefault(name, now)
            if now - since >= hold and name not in st["active"]:
                st["active"][name] = now
                detail = _detail(name, s, t, offline, roles)
                entry = {"at": now, "rule": name, "kind": kind, "title": title, "detail": detail, "readings": snapshot,
                         "action": ("see Resets" if s.get("resets") else "logged; resets are off") if kind == "fault" else
                                   ("alert sent" if kind == "alert" and s.get("alerts") else "logged")}
                if kind == "fault":
                    day["faults"] += 1
                if kind == "alert" and s.get("alerts") and alert:
                    err = alert(f"boiler_{name}", title, detail, "critical" if name == "freeze" else "warning")
                    if err:
                        entry["action"] = f"alert not sent: {err}"
                st["log"] = (st["log"] + [entry])[-KEEP_LOG:]
                msgs.append(f"{kind}: {title} ({detail})")
        else:
            st["timers"].pop(name, None)
            if name in st["active"]:
                started = st["active"].pop(name)
                for e in reversed(st["log"]):
                    if e["rule"] == name and e["at"] == started:
                        e["cleared"] = now
                        break
                if kind == "alert" and s.get("alerts") and alert:
                    alert(f"boiler_{name}", "", "clear", "info")
                msgs.append(f"cleared: {RULE[name][3]}")

    reset = resets_step(stack, st, s, r, t, burner, now, alert, msgs, offline, ctx or {})

    st["days"] = dict(sorted(st["days"].items())[-KEEP_DAYS:])
    st["last"] = {**reset, "t": now, "state": state, "burner": burner, "calling": calling, "pumps": pumps, "dhw": dhw,
                  "power": power, "outdoor": outdoor, "temps": {k: round(v, 2) for k, v in t.items()},
                  "floor_diff": None if floor_diff is None else round(floor_diff, 2), "missing": missing,
                  "output": output, "flowing": flowing, "indoor": indoor,
                  "offline": offline}
    save(stack, st)
    return msgs


def _started_file(stack):
    return os.path.join(stack, "state", "boiler-reset.json")


def reset_requests(stack):
    """What the web service recorded for the collector: {"started": time of the last reset Home Assistant started,
    "cleared": time of the last Clear}. Kept apart from boiler.json, which only the collector writes."""
    return ss.load_json(_started_file(stack), {})


def note_request(stack, key, now=None):
    d = reset_requests(stack)
    d[key] = now or time.time()
    ss.write_if_changed(_started_file(stack), json.dumps(d) + "\n", 0o644)


def _gate(st, s, r, now, offline, ctx):
    """Why a reset may not run now ("" = it may)."""
    rs = st["resets"]
    roles = s.get("roles") or {}
    if not roles.get("boiler_switch"):
        return "no boiler plug switch chosen"
    if rs["attempts"] and rs["attempts"][-1].get("result") is None:
        return "a reset is running"
    locked = "the reset limit was reached: press Clear on the boiler page once the boiler has been checked"
    if rs.get("locked"):
        return locked
    for span, most in sorted(LIMITS, reverse=True):     # the longest first: reaching it locks
        n = sum(1 for a in rs["attempts"] if now - a["at"] < span and a["at"] > rs.get("cleared", 0))  # Clear starts afresh
        if n >= most:
            if span == max(sp for sp, _ in LIMITS):
                rs["locked"] = now
                return locked
            return f"{n} reset{'s' if n > 1 else ''} in the last {span // 3600} hours"
    need = [k for k in REQUIRED + ("boiler_power", "boiler_switch", "thermostat", "heat_call") if roles.get(k)]
    down = [ROLE[k][1] for k in need if k in offline or k not in r]
    if down:
        return "no reading from " + ", ".join(down)
    pr = ctx.get("propane")
    if pr is not None:
        if pr.get("percent") is None or (pr.get("reading_age") or 0) > PROPANE_AGE_S:
            return "no recent propane reading"
        if pr["percent"] < PROPANE_MIN:
            return f"propane low ({pr['percent']:.0f}%): a reset won't help"
    if ctx.get("season") is False:
        return "Home Assistant's \"Boiler automatic resets\" switch is off"
    return ""


def resets_step(stack, st, s, r, t, burner, now, alert, msgs, offline, ctx):
    """Resets: record a reset Home Assistant started, judge whether heat came back, keep the limits, and decide
    whether the next one may run. Returns the fields for st["last"]."""
    rs = st.setdefault("resets", {"attempts": [], "locked": None, "last_fault": 0, "cleared": 0, "allowed_since": None})
    req = reset_requests(stack)
    fault = next((n for n in st["active"] if RULE[n][1] == "fault"), None)
    if fault:
        rs["last_fault"] = now
    if req.get("cleared", 0) > rs.get("cleared", 0):
        rs["cleared"] = req["cleared"]
        if rs.get("locked"):
            rs["locked"] = None
            msgs.append("resets: cleared by hand")
    if rs.get("locked") and now - rs.get("last_fault", 0) >= UNLOCK_S:
        rs["locked"] = None
        msgs.append("resets: 24 hours without a fault, limit cleared")
    # a reset Home Assistant started (the web service noted it)
    last_at = rs["attempts"][-1]["at"] if rs["attempts"] else 0
    if req.get("started", 0) > last_at:
        rs["attempts"].append({"at": req["started"], "fault": RULE[fault][3] if fault else "", "result": None,
                               "supply0": t.get("boiler_supply"), "floor0": t.get("floor_supply"),
                               "readings": {k: round(v, 1) for k, v in t.items()}})
        rs["attempts"] = rs["attempts"][-KEEP_ATTEMPTS:]
        msgs.append(f"resets: Home Assistant started a reset ({RULE[fault][3] if fault else 'no fault now'})")
    att = rs["attempts"][-1] if rs["attempts"] and rs["attempts"][-1].get("result") is None else None
    if att:
        back_on = att["at"] + RESET_OFF_S
        supply, floor = t.get("boiler_supply"), t.get("floor_supply")
        if supply is not None and att.get("supply0") is None:
            att["supply0"] = supply
        if floor is not None and att.get("floor0") is None:
            att["floor0"] = floor
        heat = (now >= back_on and bool(burner) and supply is not None and floor is not None
                and supply - att["supply0"] >= NO_HEAT_C and floor - att["floor0"] >= HEAT_BACK_FLOOR_C)
        if heat or now - back_on >= HEAT_BACK_S:
            att["result"], att["result_at"] = ("heat back" if heat else "failed"), now
            att["after"] = {k: round(v, 1) for k, v in t.items()}
            msgs.append(f"resets: {att['result']} after {(now - att['at']) / 60:.0f} min")
    why = _gate(st, s, r, now, offline, ctx) if (fault and s.get("resets")) else ""
    allowed = bool(fault and s.get("resets") and not why)
    rs["allowed_since"] = (rs.get("allowed_since") or now) if allowed else None
    # a fault that won't be reset is sent at once (with the reason); one Home Assistant never acts on, after 10 minutes
    tag = "boiler_fault"
    if fault and s.get("resets") and (why and why != "a reset is running"
                                      or (allowed and now - rs["allowed_since"] >= NOT_STARTED_S)):
        text = (f"{RULE[fault][3]}. Not reset: {why}." if why else
                f"{RULE[fault][3]}. A reset is allowed but Home Assistant hasn't started one: check its \"Solar: boiler "
                f"reset\" automation.")
        if rs.get("alerted") != text and alert:
            err = alert(tag, "Boiler fault", text, "critical")
            rs["alerted"] = text if not err else None
            msgs.append(f"resets: alert: {text}" + (f" (not sent: {err})" if err else ""))
    elif not fault and rs.get("alerted"):
        if alert:
            alert(tag, "", "clear", "info")
        rs["alerted"] = None
    last_att = rs["attempts"][-1] if rs["attempts"] else None
    if att and att.get("result") is None:
        reset_state = "resetting"
    elif last_att and now - last_att.get("result_at", 0) < 3600:
        reset_state = last_att["result"]
    else:
        reset_state = "idle"
    return {"reset_allowed": allowed, "reset_why": why, "reset_state": reset_state,
            "reset_fault": RULE[fault][3] if fault else "", "reset_locked": bool(rs.get("locked"))}


def start_reset(stack, now=None):
    """Home Assistant asks to start a reset (POST /boiler/reset-started): "" if it may (and it is recorded, so
    it counts towards the limits), else why not."""
    now = now or time.time()
    st = load(stack)
    last = st["last"] or {}
    if not last.get("t") or now - last["t"] > 180:
        return "the boiler module has no recent state"
    if not last.get("reset_allowed"):
        return last.get("reset_why") or "no fault the box would reset"
    if now - reset_requests(stack).get("started", 0) < RESET_OFF_S + HEAT_BACK_S:
        return "a reset is running"
    note_request(stack, "started", now)
    return ""


def _detail(name, s, t, offline, roles):
    f = lambda c: f"{c:.1f} °C ({c * 9 / 5 + 32:.0f} °F)"  # noqa: E731
    if name == "floor_hot":
        return f"Floor supply {f(t['floor_supply'])}, over {f(float(s['floor_max_c']))}: check the boiler's outdoor reset settings."
    if name == "freeze":
        return f"Floor return {f(t['floor_return'])}, under {f(float(s['freeze_c']))}: check the boiler is heating."
    if name == "offline":
        return "No reading from: " + ", ".join(ROLE[k][1] for k in offline) + "."
    if name in ("no_run", "stopped", "no_heat") and "boiler_supply" in t:
        return f"Boiler supply {f(t['boiler_supply'])}."
    return ""


def _mean(d, key):
    return d[key + "_sum"] / d[key + "_n"] if d.get(key + "_n") else None


def day_figures(date, d, propane_used=None):
    """One finished day: means (°C), degree days, heat delivered, and the propane burned for heating (the day's
    propane use split by burner time: heating, hot water, the rest)."""
    out, ind = _mean(d, "out"), _mean(d, "in")
    out_f = None if out is None else out * 9 / 5 + 32
    burner = d.get("burner_min") or 0
    split = None
    if propane_used is not None and burner > 0:
        heat_share, dhw_share = d.get("heat_min", 0) / burner, d.get("dhw_min", 0) / burner
        split = {"heating": round(propane_used * heat_share, 2), "hot_water": round(propane_used * dhw_share, 2),
                 "other": round(propane_used * max(0.0, 1 - heat_share - dhw_share), 2)}
    return {"date": date, "outdoor_c": None if out is None else round(out, 1), "indoor_c": None if ind is None else round(ind, 1),
            "hdd": None if out_f is None else round(max(0.0, HDD_BASE_F - out_f), 1), "heat_kbtu": d.get("heat_kbtu", 0.0),
            "burner_min": burner, "heat_min": d.get("heat_min", 0), "dhw_min": d.get("dhw_min", 0), "runs": d.get("runs", 0),
            "propane_used": propane_used, "propane": split}


def efficiency(days, s, span=7, price=None):
    """Heat loss, boiler heating efficiency and propane per degree day over the last `span` finished days
    (day_figures()). Each figure is None until there are 3 days that give it."""
    days = days[-span:]
    loss = [(d["heat_kbtu"] * 1000, 24 * (d["indoor_c"] - d["outdoor_c"]) * 9 / 5) for d in days
            if d["heat_kbtu"] and d["indoor_c"] is not None and d["outdoor_c"] is not None
            and (d["indoor_c"] - d["outdoor_c"]) * 9 / 5 >= MIN_DT_F]
    burned = [(d["heat_kbtu"] * 1000, d["propane"]["heating"] * PROPANE_BTU_PER_GAL) for d in days
              if d["heat_kbtu"] and d.get("propane") and d["propane"]["heating"] > 0]
    per_hdd = [(d["propane"]["heating"], d["hdd"]) for d in days if d.get("propane") and d["hdd"]]
    ua = round(sum(a for a, _ in loss) / sum(b for _, b in loss)) if len(loss) >= 3 else None
    eff = round(100 * sum(a for a, _ in burned) / sum(b for _, b in burned), 1) if len(burned) >= 3 else None
    gal_hdd = round(sum(a for a, _ in per_hdd) / sum(b for _, b in per_hdd), 3) if len(per_hdd) >= 3 and sum(b for _, b in per_hdd) else None
    heat_gal = sum(d["propane"]["heating"] for d in days if d.get("propane"))
    n_prop = sum(1 for d in days if d.get("propane"))
    return {"days": len(days), "heat_loss_btuh_f": ua, "efficiency_pct": eff, "gal_per_hdd": gal_hdd,
            "heat_mbtu": round(sum(d["heat_kbtu"] for d in days) / 1000, 2),
            "heating_gal": round(heat_gal, 1) if n_prop else None,
            "heating_cost_per_day": round(heat_gal / n_prop * price, 2) if n_prop and price is not None else None,
            "loss_days": len(loss), "burned_days": len(burned)}


def sizing(s, ua, eff_pct=None):
    """The heat the house needs on the design day (heat loss x (indoor - design temperature)) against what the
    boiler can put out and what the slab can deliver; None until the heat loss is known and the design temperature
    entered."""
    try:
        design_c = float(s.get("design_c"))
    except (TypeError, ValueError):
        return None
    if ua is None:
        return None
    need = round(ua * (float(s.get("indoor_c") or 20) - design_c) * 9 / 5)
    boiler = round(float(s.get("boiler_max_btuh") or 0) * (eff_pct / 100 if eff_pct else 0.95))
    slab = None
    try:
        area = float(s.get("floor_area_ft2") or 0)
    except (TypeError, ValueError):
        area = 0
    if area > 0:
        slab = (round(area * SLAB_BTUH_FT2[0]), round(area * SLAB_BTUH_FT2[1]))
    limits = [("the boiler", boiler)] + ([("the slab", slab[1])] if slab else [])
    first = min(limits, key=lambda x: x[1])
    return {"need_btuh": need, "boiler_btuh": boiler, "slab_btuh": slab, "short": need > first[1], "first": first[0],
            "margin_pct": round(100 * (first[1] - need) / need) if need > 0 else None}


def short_cycling(runs, now, span=7 * 86400):
    """Heating runs in the last `span`: how many, their median length, and the share under 10 minutes."""
    heat = sorted(r["minutes"] for r in runs if r.get("class") == "heating" and now - r["start"] < span)
    if not heat:
        return None
    return {"runs": len(heat), "median_min": heat[len(heat) // 2],
            "short_pct": round(100 * sum(1 for m in heat if m < 10) / len(heat))}


def vm_lines(stack, now=None):
    st = load(stack)
    last = st["last"]
    if not last:
        return []
    lines = [f"boiler_state {STATES.get(last['state'], 3)}"]
    for k, metric in (("burner", "boiler_burner_on"), ("calling", "boiler_call_for_heat"), ("dhw", "boiler_hot_water_draw"),
                      ("floor_diff", "boiler_floor_difference_c"), ("output", "boiler_heat_output_btuh")):
        if last.get(k) is not None:
            lines.append(f"{metric} {float(last[k]):g}")
    for k, v in last["temps"].items():
        lines.append(f'boiler_temperature_c{{role="{k}"}} {v:g}')
    for pair, o in st["offsets"].items():
        lines.append(f'boiler_probe_offset_c{{pair="{pair}"}} {o.get("c", 0):g}')
    for name, kind, _, _ in RULES:
        lines.append(f'boiler_rule_active{{rule="{name}",kind="{kind}"}} {1 if name in st["active"] else 0}')
    if last.get("outdoor") is not None:
        lines.append(f"boiler_outdoor_c {float(last['outdoor']):g}")
    today = st["days"].get(time.strftime("%Y-%m-%d", time.localtime(now or time.time())))
    if today:
        for kind, key in (("all", "burner_min"), ("heating", "heat_min"), ("hot_water", "dhw_min")):
            lines.append(f'boiler_burner_minutes_today{{kind="{kind}"}} {float(today.get(key, 0)):g}')
        lines.append(f"boiler_heat_today_kbtu {float(today.get('heat_kbtu', 0)):g}")
    return lines


def day_lines(date, d):
    """One finished day for VictoriaMetrics (vmdays): burner minutes by kind, runs, faults, heat, outdoor."""
    f = day_figures(date, d)
    lines = [f'boiler_day_burner_minutes{{kind="all"}} {float(f["burner_min"]):g}',
             f'boiler_day_burner_minutes{{kind="heating"}} {float(f["heat_min"]):g}',
             f'boiler_day_burner_minutes{{kind="hot_water"}} {float(f["dhw_min"]):g}',
             f"boiler_day_runs {int(f['runs'])}", f"boiler_day_faults {int(d.get('faults', 0))}",
             f"boiler_day_heat_kbtu {float(f['heat_kbtu']):g}"]
    if f["outdoor_c"] is not None:
        lines += [f"boiler_day_outdoor_c {f['outdoor_c']:g}", f"boiler_day_hdd {f['hdd']:g}"]
    return lines


def vm_push(stack):
    import vmdays
    lines = vm_lines(stack)
    if lines:
        req = urllib.request.Request(f"{VM}/api/v1/import/prometheus", data=("\n".join(lines) + "\n").encode(), method="POST")
        urllib.request.urlopen(req, timeout=10).read()
    vmdays.push(stack, "boiler", load(stack)["days"], day_lines)


def summary(stack, s, now=None):
    now = now or time.time()
    st = load(stack)
    today = st["days"].get(time.strftime("%Y-%m-%d", time.localtime(now))) or {}
    return {"last": st["last"], "age": (now - st["last"]["t"]) if st["last"] else None, "today": today,
            "burner_last_hour": burner_minutes(st, now),
            "levels": levels(st, s), "learned_levels": st.get("levels"), "power_samples": sum(st["power_bins"].values()),
            "offsets": st["offsets"], "curve": st["curve"], "runs": st["runs"], "run": st["run"], "days": st["days"],
            "log": st["log"], "active": st["active"], "since": st["since"],
            "resets": st.get("resets") or {"attempts": [], "locked": None},
            "fault": next((RULE[n][3] for n in st["active"] if RULE[n][1] == "fault"), None)}
