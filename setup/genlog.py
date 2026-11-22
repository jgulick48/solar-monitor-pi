#!/usr/bin/env python3
"""Generator run log and service reminder.

Generator runs are found in the inverter history (VictoriaMetrics) a minute at a time, in two ways:

  inverter  the inverters' generator input (lxp_p_gen) above 200 W: a generator wired to the
            inverters' GEN terminals (usually auto-started by the dry contact).
            Service every 200 hours.
  charger   charging the inverters didn't do themselves: the battery BMS's charging power
            (current x voltage) more than 500 W above the inverters' battery power. That is a
            generator charging the battery through a DC charger such as a Chargeverter.
            Service every 100 hours (small portable generators want oil changes more often).

A run is one stretch of generator power; dips shorter than 5 minutes don't end it, and runs
shorter than 3 minutes (10 for the charger estimate, whose BMS and inverter readings are taken at
slightly different moments and spike for a minute at a time) are ignored. Runs, the total hours per kind and the hours at the last
service are kept in state/generator.json. The collector (solar-collector.service) calls
update() every few minutes; the first call works through the existing history (up to 400 days)
a month at a time.

The start page shows the hours since the last service and has a "serviced" button
(/generator); Home Assistant gets the same figures from /api/ha and a reminder when a
service is due.
"""
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solar_setup as ss  # noqa: E402

VM = os.environ.get("SOLAR_VM", "http://127.0.0.1:8428")
STEP = 60
QUERIES = {
    "inverter": ss.GEN_POWER,          # GEN port (with voltage) or AC input: see solar_setup.GEN_POWER
    "charger": "max(lxp_batt_current_bms) * max(lxp_v_bat) - (sum(lxp_p_charge) - sum(lxp_p_discharge))",
}
ON_W = {"inverter": 200, "charger": 500}
SERVICE_HOURS = {"inverter": 200, "charger": 100}
NAMES = {"inverter": "Generator on the inverters' GEN input", "charger": "Generator charging through a DC charger (e.g. Chargeverter)"}
GAP = 5 * 60          # a shorter stop doesn't end a run
MIN_RUN = {"inverter": 3 * 60, "charger": 10 * 60}  # shorter runs are ignored (the charger signal has 1-minute spikes)
BACKFILL_DAYS = 400
CHUNK = 86400         # seconds of history per query
MAX_CHUNKS = 31       # per update() call, so the first pass doesn't hold up the collector
LAG = 120             # leave the newest two minutes: the bridge may not have written them yet
KEEP_RUNS = 500


def path(stack):
    return os.path.join(stack, "state", "generator.json")


def load(stack):
    st = ss.load_json(path(stack), {})
    st.setdefault("processed_until", 0)
    st.setdefault("runs", [])
    st.setdefault("open", {})
    st.setdefault("hours", {k: 0.0 for k in QUERIES})
    st.setdefault("service", {})
    return st


def save(stack, st):
    ss.write_if_changed(path(stack), json.dumps(st, indent=1) + "\n", 0o644)


def vm_range(q, start, end):
    url = f"{VM}/api/v1/query_range?" + urllib.parse.urlencode({"query": q, "start": start, "end": end, "step": STEP})
    with urllib.request.urlopen(url, timeout=60) as r:
        res = json.loads(r.read())["data"]["result"]
    return {int(t): float(v) for s in res[:1] for t, v in s["values"]}


def _close(st, kind, run):
    secs = run["last_on"] + STEP - run["start"]
    if secs < MIN_RUN[kind]:
        return
    hours = secs / 3600
    st["runs"].append({"kind": kind, "start": run["start"], "end": run["last_on"] + STEP, "hours": round(hours, 3),
                       "kwh": round(run["wh"] / 1000, 2), "peak_kw": round(run["peak"] / 1000, 2)})
    st["hours"][kind] = round(st["hours"].get(kind, 0.0) + hours, 3)


def process(st, kind, values, start, end):
    """Walk [start, end) a minute at a time for one kind of generator."""
    run = st["open"].get(kind)
    for t in range(start, end, STEP):
        v = values.get(t)
        if v is not None and v > ON_W[kind]:
            if run is None:
                run = {"start": t, "last_on": t, "wh": 0.0, "peak": 0.0}
            run["last_on"] = t
            run["wh"] += v * STEP / 3600
            run["peak"] = max(run["peak"], v)
        elif run is not None and t - run["last_on"] > GAP:
            _close(st, kind, run)
            run = None
    if run is None:
        st["open"].pop(kind, None)
    else:
        st["open"][kind] = run


def update(stack, now=None, fetch=vm_range):
    """Bring the log up to date (or up to MAX_CHUNKS days further). Returns the number of new runs."""
    now = int(now or time.time())
    st = load(stack)
    n0 = len(st["runs"])
    t = st["processed_until"] or (now - BACKFILL_DAYS * 86400) // STEP * STEP
    stop = (now - LAG) // STEP * STEP
    chunks = 0
    while t < stop and chunks < MAX_CHUNKS:
        e = min(t + CHUNK, stop)
        for kind, q in QUERIES.items():
            process(st, kind, fetch(q, t, e - STEP), t, e)
        t = e
        chunks += 1
        st["processed_until"] = t
    st["runs"] = st["runs"][-KEEP_RUNS:]
    save(stack, st)
    return len(st["runs"]) - n0


def mark_serviced(stack, kind, hours_since=0.0, now=None):
    """Record a service now; hours_since = hours the generator has already run since that service."""
    st = load(stack)
    total = total_hours(st, kind)
    st["service"][kind] = {"at_hours": round(total - max(hours_since, 0.0), 3),
                           "date": time.strftime("%Y-%m-%d", time.localtime(now or time.time()))}
    save(stack, st)


def total_hours(st, kind):
    """Closed runs plus the run going on now."""
    run = st["open"].get(kind)
    extra = (run["last_on"] + STEP - run["start"]) / 3600 if run else 0.0
    return st["hours"].get(kind, 0.0) + extra


def kinds_shown(st, site):
    """The kinds this site has used, or the one its generator setting suggests."""
    used = [k for k in QUERIES if st["hours"].get(k) or k in st["open"]]
    if used:
        return used
    g = (site or {}).get("generator")
    return ["inverter"] if g == "automatic" else ["charger"] if g == "manual" else []


def status(stack, site, now=None):
    """Per kind: hours since service, interval, due, last run, last 30 days. Plus running now."""
    now = now or time.time()
    st = load(stack)
    out = {"kinds": [], "running": False, "processed_until": st["processed_until"],
           "backfilling": bool(st["processed_until"]) and now - st["processed_until"] > 3600}
    for kind in kinds_shown(st, site):
        total = total_hours(st, kind)
        svc = st["service"].get(kind) or {}
        since = total - svc.get("at_hours", 0.0)
        runs = [r for r in st["runs"] if r["kind"] == kind]
        recent = [r for r in runs if r["end"] > now - 30 * 86400]
        running = kind in st["open"] and st["processed_until"] - st["open"][kind]["last_on"] <= GAP + LAG
        out["running"] |= running
        out["kinds"].append({
            "kind": kind, "name": NAMES[kind], "interval": SERVICE_HOURS[kind], "total_hours": round(total, 1),
            "since_service": round(since, 1), "left": round(SERVICE_HOURS[kind] - since, 1),
            "due": since >= SERVICE_HOURS[kind], "serviced_on": svc.get("date"), "running": running,
            "last_run": runs[-1] if runs else None,
            "runs_30d": len(recent), "hours_30d": round(sum(r["hours"] for r in recent), 1),
            "kwh_30d": round(sum(r["kwh"] for r in recent), 1),
        })
    return out


def ha_fields(stack, site, now=None):
    """For /api/ha: the kind closest to its service (most of its interval used)."""
    s = status(stack, site, now)
    if not s["kinds"]:
        return {"generator_running": s["running"], "generator_service_due": False, "generator_hours_since_service": None,
                "generator_service_interval": None, "generator_service_left": None, "generator_last_run": None,
                "generator_last_run_hours": None, "generator_runs_30d": None, "generator_hours_30d": None}
    k = max(s["kinds"], key=lambda x: x["since_service"] / x["interval"])
    last = max((x["last_run"] for x in s["kinds"] if x["last_run"]), key=lambda r: r["end"], default=None)
    iso = lambda ts: datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")  # noqa: E731
    return {
        "generator_running": s["running"], "generator_service_due": k["due"],
        "generator_hours_since_service": k["since_service"], "generator_service_interval": k["interval"],
        "generator_service_left": k["left"],
        "generator_last_run": iso(last["end"]) if last else None, "generator_last_run_hours": last and round(last["hours"], 1),
        "generator_runs_30d": sum(x["runs_30d"] for x in s["kinds"]),
        "generator_hours_30d": round(sum(x["hours_30d"] for x in s["kinds"]), 1),
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stack", default="/opt/solar-monitor/stack")
    ap.add_argument("cmd", choices=["update", "status"])
    a = ap.parse_args()
    if a.cmd == "update":
        while True:  # the whole backlog, a month per pass
            n = update(a.stack)
            print(f"{n} new run(s), up to {time.ctime(load(a.stack)['processed_until'])}")
            if load(a.stack)["processed_until"] >= (time.time() - LAG) // STEP * STEP - STEP:
                break
    print(json.dumps(status(a.stack, ss.load_json(os.path.join(a.stack, "state", "site.json"), {})), indent=1))
