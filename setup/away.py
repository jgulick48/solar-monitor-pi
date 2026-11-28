#!/usr/bin/env python3
"""Away mode: one switch for when nobody is at the property. Nothing is controlled; the alerts speak earlier and the
box sends a short report every day, so silence means "all is well" and not "it stopped working".

While it is on (active()):

  * freeze    the freeze module's "cold inside" threshold is raised by a margin (5 °F / 2.8 °C by default), and a
              hard freeze in the forecast is told even when the heat and the battery look fine: it takes hours to
              get there (freeze_margin(), used by the freeze module).
  * battery   a warning when the battery is within a margin (15 points by default) of the site's low-battery level
              for 10 minutes: for a generator started by hand, somebody has to come.
  * water     with the water module watching the pump: any pump run is told (at most every 6 hours, with the runs
              since the last word). With nobody there, water moving is a leak, a running toilet or an appliance.
  * report    once a day at the chosen time: the battery, yesterday's solar and lowest level, the watched
              temperatures, propane, water, and anything the check-up page or the alerts say is wrong. Titled
              "All is well" when nothing is.

It ends when switched off, or by itself after its end date. Settings are the module's (state/modules/away.json);
what it has said is in state/away.json.
"""
import datetime
import json
import os
import time

import solar_setup as ss

DEFAULTS = {"away": False, "since": 0, "until": "",      # until: "YYYY-MM-DD", the last day away ("" for open-ended)
            "report": True, "report_time": "08:00",
            "freeze_margin_c": 2.78, "battery_margin": 15, "water": True}
BATTERY_HOLD_S = 600        # near the low level this long before the warning
BATTERY_CLEAR = 3           # points back above before it clears
WATER_GAP_S = 6 * 3600      # between two "the pump ran" alerts
TAGS = ("away_battery", "away_water")


def settings(stack):
    import modules
    return {**DEFAULTS, **modules.settings(stack, "away")}


def enabled(stack):
    import modules
    try:
        return bool(modules.load(stack, "away")["enabled"])
    except Exception:  # noqa: BLE001 - a broken settings file means "not away"
        return False


def expired(s, now=None):
    """True when the end date has passed (the last day away is over)."""
    if not s.get("until"):
        return False
    try:
        last = datetime.date.fromisoformat(s["until"])
    except ValueError:
        return False
    return datetime.date.fromtimestamp(now or time.time()) > last


def active(stack, now=None):
    """Away mode is on now."""
    if not enabled(stack):
        return False
    s = settings(stack)
    return bool(s["away"]) and not expired(s, now)


def freeze_margin(stack, now=None):
    """°C to add to the freeze module's threshold now (0 when not away)."""
    return float(settings(stack)["freeze_margin_c"]) if active(stack, now) else 0.0


def set_away(stack, on, until="", now=None):
    import modules
    s = settings(stack)
    if on:
        s.update(away=True, until=until or "", since=s["since"] if s["away"] else int(now or time.time()))
    else:
        s.update(away=False, until="", since=0)
    modules.save(stack, "away", s)
    return s


def _path(stack):
    return os.path.join(stack, "state", "away.json")


def load(stack):
    st = ss.load_json(_path(stack), {})
    return st if isinstance(st, dict) else {}


def save(stack, st):
    ss.write_if_changed(_path(stack), json.dumps(st) + "\n", 0o644)


# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------
def battery_rule(st, s, soc, floor, now, advice_text=""):
    """("raise", text) | ("clear", "") | None. st: the away state (changed in place)."""
    if soc is None:
        return None
    at = floor + float(s["battery_margin"])
    if soc <= at:
        since = st.setdefault("battery_since", now)
        if now - since >= BATTERY_HOLD_S and not st.get("battery_said"):
            st["battery_said"] = True
            return "raise", (f"The battery is at {soc:.0f}% with nobody at the property; the low-battery level is "
                             f"{floor:.0f}%." + (f" {advice_text}" if advice_text else ""))
    elif soc >= at + BATTERY_CLEAR:
        st.pop("battery_since", None)
        if st.pop("battery_said", None):
            return "clear", ""
    return None


def water_rule(st, runs, pump_on_since, since, now):
    """runs: the water module's finished pump runs ([{"start", "s"}]); pump_on_since: start of the run going now, or
    None. Tells of runs that started after away mode began (`since`) and haven't been told yet. Text or None."""
    told = max(st.get("water_told", 0), since)
    new = [r for r in runs if r["start"] > told]
    going = pump_on_since is not None and pump_on_since > told
    if not new and not going:
        return None
    if now - st.get("water_said", 0) < WATER_GAP_S:
        return None                                  # kept for the next word: water_told hasn't moved
    st["water_said"] = now
    st["water_told"] = max([r["start"] for r in new] + ([pump_on_since] if going else []) + [told])
    n = len(new) + (1 if going else 0)
    secs = sum(r["s"] for r in new)
    first = min([r["start"] for r in new] + ([pump_on_since] if going else []))
    ran = "once" if n == 1 else f"{n} times"
    how_long = f" ({max(1, round(secs / 60))} min in all)" if new else ""
    return (f"The water pump ran {ran}{how_long} since {time.strftime('%a %H:%M', time.localtime(first))}"
            + (", and is running now" if going and new else ", and is still running" if going else "")
            + " while away mode is on. With nobody there, something is using water: a leak, a running toilet or an appliance.")


def report_due(st, s, now):
    """True once a day, from the report time on (and not on the day away mode was turned on after that time)."""
    if not s.get("report"):
        return False
    lt = time.localtime(now)
    today = time.strftime("%Y-%m-%d", lt)
    if st.get("report_day") == today:
        return False
    try:
        h, m = (int(x) for x in str(s.get("report_time") or "08:00").split(":"))
    except ValueError:
        h, m = 8, 0
    due_at = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
    if now < due_at:
        return False
    if (s.get("since") or 0) > due_at:               # turned on after today's time: the first report is tomorrow's
        st["report_day"] = today
        return False
    return True


def report_text(name, parts, problems):
    """(title, text) of the daily report. parts: sentences that are fine; problems: sentences that aren't."""
    where = f" at {name}" if name else ""
    if problems:
        n = len(problems)
        return (f"Away check{where}: {n} thing{'s' if n != 1 else ''} to look at",
                " ".join(problems) + (" Otherwise: " + " ".join(parts) if parts else ""))
    return f"All is well{where}", " ".join(parts) or "Nothing to report."
