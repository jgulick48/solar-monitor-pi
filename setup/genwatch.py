#!/usr/bin/env python3
"""Following a generator run as it happens, for a site where somebody has to walk out and start it.

The generator module calls step() every minute with what the box sees now (generator power, battery level, the
start / stop advice). It keeps state/generator-watch.json and returns what is worth telling the owner:

  started   the generator has run for 3 minutes: that it is charging, and for how long it should run.
  stopped   it has been off for 5 minutes: how long it ran, the energy, the fuel and cost (the generator log's own
            figures), the battery level, and whether it was stopped before the battery had enough.
  remind    a hand-started generator is needed and still isn't running. Home Assistant says so once when the advice
            first shows, again at sunset, and every 4 hours (packages/solar_monitor.yaml, notify.py). This adds a
            reminder that follows the battery: each time it has fallen another 10 points, and once when it is
            within 2 hours of the alert level; never within 30 minutes of the last word.

step() only decides; the module sends (notify.alert) and adds the fuel on hand. The generator log (genlog.py) stays
the record of runs: it is worked out from the history a few minutes behind, so the notices here use their own count
of the run's minutes and energy, which is why a notice and the log can differ by a minute or two.
"""
import json
import os

import solar_setup as ss

ON_W = 200              # generator power above this: running (display.GEN_RUNNING_W, genlog.ON_W)
START_MIN = 3           # minutes of running before "started" is said (the DC-charger signal spikes for a minute)
STOP_MIN = 5            # minutes off before "stopped" is said (genlog.GAP: a shorter dip doesn't end a run)
REMIND_DROP = 10        # battery points fallen since the last word before another reminder
REMIND_GAP = 30 * 60    # seconds between reminders at least
URGENT_H = 2.0          # hours to the alert level: the one "now" reminder
NEED_HOLD = 10 * 60     # the advice must have gone for this long before a need counts as over
TICK_MAX = 300          # seconds: a longer gap between two looks isn't counted as running time


def path(stack):
    return os.path.join(stack, "state", "generator-watch.json")


def load(stack):
    st = ss.load_json(path(stack), {})
    return st if isinstance(st, dict) else {}


def save(stack, st):
    ss.write_if_changed(path(stack), json.dumps(st) + "\n", 0o644)


def step(st, obs, now):
    """One look. st: the state from the last look (changed in place). obs: {"gen_w", "soc", "needed" (the advice
    says start it), "phase" ("run" / "stop" / "" from the stop advice), "need_kwh", "run_hours", "empty_in_h",
    "empty_at", "floor"}; any may be None. Returns [(kind, details)] with kind "started" | "stopped" | "remind"."""
    out = []
    gen = obs.get("gen_w")
    soc = obs.get("soc")
    last = st.get("at") or now
    dt = min(max(now - last, 0), TICK_MAX)
    st["at"] = now
    run = st.get("run")
    if gen is not None and gen >= ON_W:
        if run is None:
            run = st["run"] = {"start": now, "wh": 0.0, "peak": 0.0, "soc": soc, "said": False}
        run["wh"] += gen * dt / 3600
        run["peak"] = max(run["peak"], gen)
        run["last_on"] = now
        run["phase"], run["need_kwh"] = obs.get("phase") or "", obs.get("need_kwh")
        if not run["said"] and now - run["start"] >= START_MIN * 60:
            run["said"] = True
            out.append(("started", {"start": run["start"], "kw": round(gen / 1000, 1), "soc": soc,
                                    "run_hours": obs.get("run_hours") if obs.get("phase") == "run" else None,
                                    "can_stop": obs.get("phase") == "stop"}))
    elif run is not None and gen is not None and now - run.get("last_on", run["start"]) >= STOP_MIN * 60:
        st.pop("run")
        if run["said"]:
            end = run.get("last_on", run["start"])
            out.append(("stopped", {"start": run["start"], "end": end, "hours": round(max(end - run["start"], 60) / 3600, 3),
                                    "kwh": round(run["wh"] / 1000, 2), "soc": soc, "soc_start": run.get("soc"),
                                    "early": run.get("phase") == "run", "need_kwh": run.get("need_kwh")}))
    running = st.get("run") is not None
    need = st.get("need")
    if obs.get("needed") and not running:
        if need is None:
            # the first word is Home Assistant's, when the advice has held for 5 minutes: count from here
            need = st["need"] = {"since": now, "last": now, "soc": soc, "first_soc": soc, "urgent": False}
        need["seen"] = now
        fell = soc is not None and need.get("soc") is not None and need["soc"] - soc >= REMIND_DROP
        near = obs.get("empty_in_h") is not None and obs["empty_in_h"] <= URGENT_H
        if near and not need["urgent"] and now - need["last"] >= REMIND_GAP:
            need.update(urgent=True, last=now, soc=soc)
            out.append(("remind", {"urgent": True, **_need(obs, need)}))
        elif fell and now - need["last"] >= REMIND_GAP:
            need.update(last=now, soc=soc)
            out.append(("remind", {"urgent": False, **_need(obs, need)}))
    elif need is not None and (running or now - need.get("seen", now) >= NEED_HOLD):
        st.pop("need")
    return out


def _need(obs, need):
    return {"soc": obs.get("soc"), "first_soc": need.get("first_soc"), "since": need["since"],
            "empty_at": obs.get("empty_at"), "empty_in_h": obs.get("empty_in_h"), "need_kwh": obs.get("need_kwh"),
            "run_hours": obs.get("run_hours"), "floor": obs.get("floor")}
