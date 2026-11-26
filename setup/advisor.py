#!/usr/bin/env python3
"""solar-advisor: what would help this system most - batteries, panels, an
inverter or an automatic generator?

Reads the history in VictoriaMetrics (15-minute averages, up to 400 days) and
the site details (state/site.json), then:

  1. Works out how much solar the panels *could* have made, from a weather
     station's solar radiation (converted to each array's tilt and direction)
     or from Open-Meteo's irradiance on each array, calibrated against the
     inverters' actual output while the batteries weren't full.
  2. Measures what happened: house load, solar used, solar lost because the
     batteries were full, days the batteries ran low, and generator charging:
     inverter generator input (lxp_p_gen) plus charging the inverters didn't
     do themselves (a Chargeverter or other DC charger, seen as the BMS
     current being higher than the inverters' battery power).
  3. Replays the period with each option (more batteries, more panels, another
     inverter, an automatic generator) and compares generator use, low
     battery days and wasted solar.
  4. Writes state/advisor.json for the start page's Advisor tab.

Rough by design: it is a comparison between options on this site's own data,
not a forecast. Read the "data" section of the result before trusting it.
"""
import argparse
import collections
import datetime
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solar_setup as ss  # noqa: E402

VM = os.environ.get("SOLAR_VM", "http://127.0.0.1:8428")
STEP = 900  # seconds
GAMMA = -0.0035  # PV power change per °C of cell temperature

# Per-inverter specs: continuous AC output kW, max PV input kW, max battery charge kW (from the generator/AC side).
INVERTERS = {
    "6000xp": {"name": "EG4 6000XP", "ac_kw": 6.0, "pv_kw": 8.0, "charge_kw": 7.0},
    "12000xp": {"name": "EG4 12000XP", "ac_kw": 12.0, "pv_kw": 15.0, "charge_kw": 10.0},
    "12kpv": {"name": "EG4 12kPV", "ac_kw": 12.0, "pv_kw": 15.0, "charge_kw": 10.0},
    "18kpv": {"name": "EG4 18kPV", "ac_kw": 12.0, "pv_kw": 18.0, "charge_kw": 12.5},
}
# Fuel use in gallons per hour as a function of load fraction (rough, typical published curves).
FUEL = {
    "diesel": lambda kw, f: 0.06 * kw * (0.25 + 0.75 * f) + 0.1,
    "propane": lambda kw, f: 0.11 * kw * (0.3 + 0.7 * f) + 0.2,
    "gasoline": lambda kw, f: 0.09 * kw * (0.3 + 0.7 * f) + 0.1,
}


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
def vm_range(q, start, end, step=STEP):
    out = {}
    t = start
    while t < end:
        e = min(t + 50 * 86400, end)
        url = f"{VM}/api/v1/query_range?" + urllib.parse.urlencode({"query": q, "start": t, "end": e, "step": step})
        with urllib.request.urlopen(url, timeout=120) as r:
            res = json.loads(r.read())["data"]["result"]
        for s in res:
            for ts, v in s["values"]:
                out[int(ts)] = float(v)
        t = e
    return out


def vm_range_by(q, label, start, end, step=STEP):
    out = collections.defaultdict(dict)
    url = f"{VM}/api/v1/query_range?" + urllib.parse.urlencode({"query": q, "start": start, "end": end, "step": step})
    with urllib.request.urlopen(url, timeout=120) as r:
        for s in json.loads(r.read())["data"]["result"]:
            for ts, v in s["values"]:
                out[s["metric"].get(label, "")][int(ts)] = float(v)
    return out


def load_series(days):
    end = int(time.time()) // STEP * STEP
    start = end - days * 86400
    a = lambda m: f"avg_over_time(({m})[15m])"  # noqa: E731
    S = {
        "pv": vm_range(a("sum(lxp_p_pv)"), start, end),
        "load": vm_range(a("sum(lxp_p_load) or sum(lxp_p_eps)"), start, end),
        "soc": vm_range(f"max_over_time((max(lxp_soc))[15m])", start, end),
        "chg": vm_range(a("sum(lxp_p_charge)"), start, end),
        "dis": vm_range(a("sum(lxp_p_discharge)"), start, end),
        "gen": vm_range(a(ss.GEN_POWER), start, end),
        "bms_w": vm_range(a(ss.BMS_POWER), start, end),
        "ghi": vm_range(a("max(site_solar_radiation_wm2)"), start, end),
        "temp": vm_range(a("max(site_outdoor_temp_c) or max(site_openmeteo_temp_c)"), start, end),
    }
    S["gti"] = vm_range_by("max by (array) (last_over_time(site_openmeteo_gti_wm2[1h]))", "array", start, end, 3600)
    return S


# --------------------------------------------------------------------------
# Sun position and plane-of-array irradiance (NOAA approximation, Erbs split, isotropic sky)
# --------------------------------------------------------------------------
def sunpos(ts, lat, lon):
    t = time.gmtime(ts)
    hr = t.tm_hour + t.tm_min / 60 + t.tm_sec / 3600
    g = 2 * math.pi / 365 * (t.tm_yday - 1 + (hr - 12) / 24)
    eot = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    ha = math.radians((hr * 60 + eot + 4 * lon) / 4 - 180)
    la = math.radians(lat)
    cz = max(-1.0, min(1.0, math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)))
    az = (math.degrees(math.atan2(math.sin(ha), math.cos(ha) * math.sin(la) - math.tan(decl) * math.cos(la))) + 180) % 360
    return 90 - math.degrees(math.acos(cz)), az, t.tm_yday


def poa(ghi, ts, lat, lon, tilt, azimuth):
    el, az, doy = sunpos(ts, lat, lon)
    if el <= 1 or ghi <= 0:
        return 0.0
    cz = max(math.sin(math.radians(el)), 0.087)
    e0 = 1361 * (1 + 0.033 * math.cos(2 * math.pi * doy / 365))
    kt = min(ghi / (e0 * cz), 1.0)
    fd = (1 - 0.09 * kt) if kt <= 0.22 else ((0.9511 - 0.1604 * kt + 4.388 * kt ** 2 - 16.638 * kt ** 3 + 12.336 * kt ** 4) if kt <= 0.8 else 0.165)
    dhi = ghi * fd
    dni = (ghi - dhi) / cz
    tr = math.radians(tilt)
    caoi = math.sin(math.radians(el)) * math.cos(tr) + math.cos(math.radians(el)) * math.sin(tr) * math.cos(math.radians(az - azimuth))
    return max(dni * max(caoi, 0) + dhi * (1 + math.cos(tr)) / 2 + ghi * 0.2 * (1 - math.cos(tr)) / 2, 0)


# --------------------------------------------------------------------------
def median(v):
    v = sorted(v)
    n = len(v)
    return None if n == 0 else (v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2)


def build_steps(S, site):
    """Per 15 minutes: t, pv_kw, load_kw, soc, gen_kw (inverter + external), raw potential (k=1, kW) or None, from the
    station's sunlight when it has enough, else Open-Meteo's; and raw_om, the same from Open-Meteo's alone (for the
    solar forecast's calibration, which scales Open-Meteo's forecast)."""
    lat, lon = float(site["latitude"]), float(site["longitude"])
    arrays = site["arrays"]
    kwp = [int(a["panels"]) * int(a["panel_watts"]) / 1000 for a in arrays]
    use_station = len([v for v in S["ghi"].values() if v is not None]) > 96 * 3
    gti = S["gti"]
    steps = []
    for t in sorted(S["pv"]):
        if t not in S["soc"] or t not in S["load"]:
            continue
        temp = S["temp"].get(t, 15.0)
        raw = raw_om = None
        if gti:
            hour_end = (t + 3599) // 3600 * 3600  # Open-Meteo value at hour end = mean of that hour
            vals = [gti.get(str(i), {}).get(hour_end) for i in range(1, len(arrays) + 1)]
            if all(v is not None for v in vals):
                raw_om = sum(k * x / 1000 * (1 + GAMMA * (temp + x / 800 * 20 - 25)) for k, x in zip(kwp, vals))
        if use_station and t in S["ghi"]:
            p = [poa(S["ghi"][t], t - STEP / 2, lat, lon, int(a["tilt"]), int(a["azimuth"])) for a in arrays]
            raw = sum(k * x / 1000 * (1 + GAMMA * (temp + x / 800 * 20 - 25)) for k, x in zip(kwp, p))
        elif raw_om is not None:
            raw = raw_om
        inv_net = S["chg"].get(t, 0) - S["dis"].get(t, 0)
        ext = max(S["bms_w"].get(t, inv_net) - inv_net, 0) if t in S["bms_w"] else 0.0
        steps.append({"t": t, "pv": S["pv"][t] / 1000, "load": S["load"][t] / 1000, "soc": S["soc"][t],
                      "gen": max(S["gen"].get(t, 0), 0) / 1000 + (ext / 1000 if ext > 500 else 0), "raw": raw,
                      "raw_om": raw_om})
    return steps, ("weather station" if use_station else "Open-Meteo" if gti else "none")


def calibrate(steps, total_kwp, key="raw"):
    """k = actual / raw potential (steps' `key`), median over daylight samples with the batteries not full."""
    r = [s["pv"] / s[key] for s in steps if s.get(key) and s["soc"] < 95 and s[key] > 0.15 * total_kwp and s["pv"] > 0.05 * total_kwp]
    return (median(r), len(r)) if len(r) >= 40 else (None, len(r))


def day_of(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def measured(steps, low_soc):
    M = collections.defaultdict(lambda: collections.defaultdict(float))
    runs, cur = [], None
    for s in steps:
        m = M[day_of(s["t"])[:7]]
        m["days"] += 1 / 96
        m["load"] += s["load"] / 4
        m["pv"] += s["pv"] / 4
        if s["pot"] is not None:
            m["pot"] += s["pot"] / 4
            if s["soc"] >= 95:
                m["lost"] += max(s["pot"] - s["pv"], 0) / 4
        m["gen"] += s["gen"] / 4
        if s["gen"] > 0.5:
            m["gen_h"] += 0.25
            if cur is None:
                cur = [s["t"], 0]
                runs.append(cur)
                m["starts"] += 1
            cur[1] += 0.25
        else:
            cur = None
    low_days = collections.defaultdict(set)
    for s in steps:
        if s["soc"] <= low_soc:
            low_days[day_of(s["t"])[:7]].add(day_of(s["t"]))
    out = []
    for k in sorted(M):
        m = M[k]
        d = max(m["days"], 1e-9)
        out.append({"month": k, "days": round(m["days"], 1), "load_per_day": round(m["load"] / d, 1), "pv_per_day": round(m["pv"] / d, 1),
                    "potential_per_day": round(m["pot"] / d, 1) if m["pot"] else None,
                    "lost_to_full_per_day": round(m["lost"] / d, 1) if m["pot"] else None,
                    "generator_kwh": round(m["gen"]), "generator_starts": int(m["starts"]), "generator_hours": round(m["gen_h"], 1),
                    "low_battery_days": len(low_days[k])})
    return out


def with_generator_log(months, stack, start, end):
    """The months' generator figures from the generator log (genlog.py) instead of the 15-minute history: the log
    reads a minute at a time, bridges short dips and drops spikes, and is what the Generator page, the monthly
    report and Home Assistant show, so the advisor's table agrees with them. Returns (months, "log"), or the
    months unchanged and "history" when the log can't speak for the whole period (the generator module is off,
    or it is still working through the history)."""
    import genlog
    if not os.path.exists(genlog.path(stack)):
        return months, "history"
    st = genlog.load(stack)
    if not genlog.covers(st, start, end):
        return months, "history"
    by = genlog.by_month(st["runs"], start, end)
    out = []
    for m in months:
        g = by.get(m["month"], {"kwh": 0.0, "starts": 0, "hours": 0.0})
        out.append({**m, "generator_kwh": round(g["kwh"]), "generator_starts": g["starts"], "generator_hours": round(g["hours"], 1)})
    return out, "log"


def simulate(steps, batt_kwh, pv_scale, pv_cap_kw, floor_soc, eff=0.92):
    """Replay without a generator: how much energy the batteries come up short of
    the generator start level (floor_soc), on how many days, in how many separate
    episodes, and how much solar is wasted because they're full."""
    soc = 60.0
    r = collections.defaultdict(float)
    short_days, last_short = set(), None
    for s in steps:
        dt = 0.25
        pv = min((s["pot"] if s["pot"] is not None else s["pv"]) * pv_scale, pv_cap_kw)
        net = pv - s["load"]
        if net >= 0:
            add = net * eff * dt / batt_kwh * 100
            r["wasted"] += max(soc + add - 100, 0) / 100 * batt_kwh / eff
            soc = min(soc + add, 100.0)
        else:
            soc += net * dt / batt_kwh * 100
        if soc < floor_soc:
            r["shortfall"] += (floor_soc - soc) / 100 * batt_kwh
            soc = floor_soc
            short_days.add(day_of(s["t"]))
            if last_short is None or s["t"] - last_short > 6 * 3600:
                r["episodes"] += 1
            last_short = s["t"]
    r["short_days"] = len(short_days)
    return {k: round(v, 1) for k, v in r.items()}


def generator_estimate(shortfall_kwh, episodes, gen_kw, charge_kw, fuel_fn):
    """Hours and fuel to make up a shortfall by charging at charge_kw."""
    if shortfall_kwh <= 0:
        return {"hours": 0.0, "fuel": 0.0}
    hours = shortfall_kwh / charge_kw / 0.92 + 0.25 * episodes  # plus warm-up/cool-down per start
    fuel = fuel_fn(gen_kw, min(charge_kw / gen_kw, 1)) * hours if fuel_fn and gen_kw else 0.0
    return {"hours": round(hours, 1), "fuel": round(fuel, 1)}


def analyse(stack, days=400):
    site = ss.load_json(os.path.join(stack, "state", "site.json"), {})
    inv = ss.load_json(os.path.join(stack, "state", "inverters.json"), {})
    if not site.get("latitude"):
        return {"status": "needs-setup", "message": "Enter the site details on the setup page first."}
    S = load_series(days)
    steps, source = build_steps(S, site)
    n_days = len(steps) / 96
    total_kwp = sum(int(a["panels"]) * int(a["panel_watts"]) for a in site["arrays"]) / 1000
    batt_kwh = int(site["battery_count"]) * float(site["battery_kwh_each"])
    unit_kwh = float(site["battery_kwh_each"])
    model = INVERTERS.get(site.get("inverter_model", "6000xp"), INVERTERS["6000xp"])
    n_inv = max(len(inv), int(site.get("inverter_count") or 0), 1)
    low_soc = int(site.get("low_soc_alert", 25))
    result = {"status": "ok", "generated": time.strftime("%Y-%m-%d %H:%M"), "source": source,
              "site": {"battery_kwh": round(batt_kwh, 1), "array_kwp": round(total_kwp, 2), "inverters": n_inv, "inverter": model["name"]}}
    if n_days < 7:
        return {**result, "status": "collecting", "message": f"{n_days:.0f} days of data so far; the first rough analysis needs a week, a good one a full winter."}

    k, n_cal = calibrate(steps, total_kwp)
    # the solar forecast scales Open-Meteo's forecast, so it needs the calibration against Open-Meteo, also when a
    # weather station's sunlight is what the analysis uses
    k_om = k if source == "Open-Meteo" else calibrate(steps, total_kwp, "raw_om")[0]
    for s in steps:
        s["pot"] = s["raw"] * k if (k and s["raw"] is not None) else None
    have_pot = k is not None
    months = [m["month"] for m in measured(steps, low_soc)]
    winter = any(m[5:] in ("11", "12", "01", "02") for m in months)
    result["data"] = {"days": round(n_days), "from": day_of(steps[0]["t"]), "to": day_of(steps[-1]["t"]),
                      "calibration": round(k, 3) if k else None, "calibration_samples": n_cal, "covers_winter": winter,
                      "calibration_openmeteo": round(k_om, 3) if k_om else None,
                      "confidence": "low" if n_days < 60 or not have_pot else ("medium" if not winter or n_days < 300 else "good")}
    result["months"], result["data"]["generator_from"] = with_generator_log(measured(steps, low_soc), stack, steps[0]["t"], steps[-1]["t"])

    # Capacity headroom
    loads = sorted(s["load"] for s in steps)
    pvs = sorted(s["pv"] for s in steps)
    result["headroom"] = {
        "peak_load_kw": round(loads[-1], 1), "p99_load_kw": round(loads[int(len(loads) * 0.99)], 1),
        "inverter_output_kw": round(n_inv * model["ac_kw"], 1),
        "peak_pv_kw": round(pvs[-1], 1), "pv_input_limit_kw": round(n_inv * model["pv_kw"], 1), "array_kwp": round(total_kwp, 2),
    }

    # Scenarios: replay without a generator, then size the generator time that would make up the shortfall.
    gen_mode = site.get("generator", "manual")
    gen_kw = float(site.get("generator_kw") or 0) or (5.0 if gen_mode == "manual" else 20.0)
    fuel_fn = FUEL.get(site.get("generator_fuel") or ("gasoline" if gen_mode == "manual" else "propane"))
    charge_kw = 5.0 if gen_mode == "manual" else min(gen_kw * 0.9, n_inv * model["charge_kw"])
    floor = 20
    pv_cap = n_inv * model["pv_kw"]

    sc = [("Today's system", {}, None)]
    for n in (1, 2, 4):
        sc.append((f"+{n} batter{'y' if n == 1 else 'ies'} (+{n * unit_kwh:.1f} kWh)", {"batt_kwh": batt_kwh + n * unit_kwh}, ("battery", n)))
    for pct in (25, 50, 100):
        sc.append((f"+{pct}% panels (+{total_kwp * pct / 100:.1f} kW)", {"pv_scale": 1 + pct / 100}, ("panels", total_kwp * pct / 100)))
    if total_kwp * 2 > pv_cap:
        sc.append((f"+100% panels and +1 inverter (PV input {pv_cap:.0f} -> {pv_cap + model['pv_kw']:.0f} kW)",
                   {"pv_scale": 2.0, "pv_cap_kw": pv_cap + model["pv_kw"]}, ("panels+inverter", total_kwp)))
    rows = []
    for name, kw, cost_key in sc:
        p = dict(batt_kwh=batt_kwh, pv_scale=1.0, pv_cap_kw=pv_cap, floor_soc=floor)
        p.update(kw)
        r = simulate(steps, **p)
        r.update(generator_estimate(r.get("shortfall", 0), r.get("episodes", 0), gen_kw, charge_kw, fuel_fn))
        r.update(name=name, cost_key=cost_key)
        rows.append(r)
    base = rows[0]
    if gen_mode != "automatic":
        auto_kw = float(site.get("future_generator_kw") or 28)
        auto_charge = min(auto_kw * 0.9, n_inv * model["charge_kw"])
        result["automatic_generator"] = {"kw": auto_kw, "charge_kw": round(auto_charge, 1),
                                         **generator_estimate(base.get("shortfall", 0), base.get("episodes", 0), auto_kw, auto_charge, FUEL["propane"]),
                                         "starts": base.get("episodes", 0)}
    scale = 365 / n_days
    costs = site.get("costs") or {}
    fuel_price = float(costs.get("fuel_per_gal") or 0)
    for r in rows:
        r["shortfall_saved"] = round(base.get("shortfall", 0) - r.get("shortfall", 0), 1)
        r["generator_hours_saved"] = round(base.get("hours", 0) - r.get("hours", 0), 1)
        r["fuel_saved"] = round(base.get("fuel", 0) - r.get("fuel", 0), 1)
        price = None
        ck = r.pop("cost_key")
        if ck and costs:
            kind, amount = ck
            if kind == "battery" and costs.get("battery_each"):
                price = amount * float(costs["battery_each"])
            elif kind == "panels" and costs.get("panel_per_w"):
                price = amount * 1000 * float(costs["panel_per_w"])
            elif kind == "panels+inverter" and costs.get("panel_per_w") and costs.get("inverter_each"):
                price = amount * 1000 * float(costs["panel_per_w"]) + float(costs["inverter_each"])
        r["price"] = round(price) if price else None
        yearly = r["fuel_saved"] * scale * fuel_price if fuel_price else None
        r["yearly_fuel_saving"] = round(yearly) if yearly else None
        r["payback_years"] = round(price / yearly, 1) if price and yearly and yearly > 0 else None
    result["scenarios"] = rows
    result["period_note"] = f"Scenario numbers are for the {n_days:.0f} days replayed" + (
        "" if n_days >= 300 else "; they are not a full year, so seasonal differences are missing") + "."
    result["recommendation"] = recommend(result, rows, gen_mode)
    return result


def recommend(res, rows, gen_mode):
    base = rows[0]
    h = res["headroom"]
    lines = []
    if h["p99_load_kw"] > 0.8 * h["inverter_output_kw"]:
        lines.append(f"Inverter output is the tight spot: the load reaches {h['p99_load_kw']} kW (99th percentile, peak {h['peak_load_kw']} kW) of {h['inverter_output_kw']} kW. Another inverter adds output capacity.")
    if base.get("shortfall", 0) < 1:
        lines.append("Over this period the batteries never ran low enough to need the generator, so no addition is needed for energy. Revisit after a winter.")
    else:
        lines.append(f"Without a generator the batteries would have come up {base.get('shortfall', 0):.0f} kWh short on {base.get('short_days', 0):.0f} days "
                     f"({base.get('episodes', 0):.0f} separate times); making that up takes about {base.get('hours', 0)} generator hours.")
        cands = rows[1:]
        best = max(cands, key=lambda r: (r["shortfall_saved"], -r.get("wasted", 0), -(r.get("price") or 0)))
        lines.append(f"Biggest reduction: {best['name']} ({best.get('shortfall', 0):.0f} kWh short on {best.get('short_days', 0):.0f} days, "
                     f"about {best.get('hours', 0)} generator hours).")
        priced = [r for r in cands if r.get("price") and r["shortfall_saved"] > 0]
        if priced:
            c = min(priced, key=lambda r: r["price"] / r["shortfall_saved"])
            lines.append(f"Best value for money: {c['name']} (about ${c['price'] / c['shortfall_saved']:.0f} per kWh of shortfall removed over this period).")
    ag = res.get("automatic_generator")
    if ag and base.get("shortfall", 0) >= 1:
        lines.append(f"An automatic {ag['kw']:.0f} kW generator would cover the same shortfall without anyone there: about {ag['starts']:.0f} starts, "
                     f"{ag['hours']} h and {ag['fuel']} gal of propane over this period (charging at {ag['charge_kw']} kW, the inverters' limit).")
    if base.get("wasted", 0) > 0.3 * sum(m.get("potential_per_day") or 0 for m in res["months"]) * 30 / max(len(res["months"]), 1):
        lines.append("A lot of solar is lost because the batteries are already full. More panels mostly add to that loss; more storage or "
                     "using the surplus during the day helps more.")
    if res.get("source") == "Open-Meteo":
        lines.append("Sunlight comes from Open-Meteo, which tends to see more sun than a local meter on cloudy days, so this may "
                     "understate how often the batteries run short. A local weather station with a solar sensor makes it more accurate.")
    if res["data"]["confidence"] != "good":
        lines.append(f"Confidence: {res['data']['confidence']} ({res['data']['days']} days of data"
                     f"{', no winter yet' if not res['data']['covers_winter'] else ''}). Winter storms decide most of this.")
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stack", default="/opt/solar-monitor/stack")
    ap.add_argument("--days", type=int, default=400)
    args = ap.parse_args()
    try:
        res = analyse(args.stack, args.days)
    except Exception as e:  # noqa: BLE001 - show it on the page instead of failing silently
        res = {"status": "error", "message": f"{type(e).__name__}: {e}", "generated": time.strftime("%Y-%m-%d %H:%M")}
    ss.write_if_changed(os.path.join(args.stack, "state", "advisor.json"), json.dumps(res, indent=2) + "\n", 0o644)
    print(json.dumps({k: res.get(k) for k in ("status", "message", "data", "recommendation")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
