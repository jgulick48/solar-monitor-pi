#!/usr/bin/env python3
"""When the wall display's screen is on: a schedule, room occupancy from Home Assistant, camera events, and
HDMI-CEC to turn the TV itself on and off.

Settings (in state/display.json, set on the setup page):
  power_mode      "always" | "hours" (on during the on-hours) | "occupied" (when someone's in the room)
                  | "hours_occupied" (during the on-hours, when someone's in the room)
  hours           up to 4 rules {"days": "daily"|"weekdays"|"weekends"|"mon".."sun", "on": "06:30", "off": "22:00"};
                  an off time earlier than the on time runs past midnight (on 18:00, off 01:00)
  occupancy       Home Assistant entity ids (motion/occupancy/presence binary sensors, person.*, input_boolean.*);
                  someone's there while any is on / home
  occupancy_hold  minutes the screen stays on after the last one went off (default 10)
  wake            camera events that turn the screen on outside those times: {"doorbell": true, "person": false, ...}
  wake_minutes    for how long (default 3)
  cec             also put the TV in standby and wake it over HDMI-CEC (default on); the HDMI signal is switched
                  off and on either way, which most monitors and many TVs follow by themselves

Occupancy reaches the box from Home Assistant: the box writes packages/solar_display.yaml, an automation that
POSTs {"occupied": true|false} to http://127.0.0.1/display/presence when one of the entities changes, when HA
starts and every 5 minutes. With no word for 20 minutes the sensors count as unknown and the screen stays on
(a dark screen because Home Assistant is down would look broken).

The decision is made in the web server (it has the camera events); the solar-display-power service asks
/display/power every few seconds and does the switching (wlr-randr for the signal, cec-ctl for the TV).
"""
import datetime as dt
import json
import os
import re
import subprocess
import time

MODES = [("always", "Always on"), ("hours", "During the on-hours"), ("occupied", "When someone's in the room"),
         ("hours_occupied", "During the on-hours, when someone's in the room")]
DAYS = [("daily", "Every day"), ("weekdays", "Mon-Fri"), ("weekends", "Sat-Sun"), ("mon", "Monday"), ("tue", "Tuesday"),
        ("wed", "Wednesday"), ("thu", "Thursday"), ("fri", "Friday"), ("sat", "Saturday"), ("sun", "Sunday")]
DAY_NUMS = {"daily": set(range(7)), "weekdays": {0, 1, 2, 3, 4}, "weekends": {5, 6},
            **{k: {i} for i, k in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}}
MAX_RULES = 4
WAKE_KINDS = [("doorbell", "Doorbell"), ("person", "Person"), ("vehicle", "Vehicle"), ("animal", "Animal"), ("package", "Package")]
DEFAULTS = {"power_mode": "always", "hours": [], "occupancy": [], "occupancy_hold": 10,
            "wake": {"doorbell": True}, "wake_minutes": 3, "cec": True}
ENTITY = re.compile(r"^(binary_sensor|input_boolean|switch|person|device_tracker|group)\.[a-z0-9_]{1,80}$")
STALE_S = 20 * 60

# Live state in the web server
PRESENCE = {"occupied": None, "at": 0.0, "last_on": 0.0}
WAKE = {"until": 0.0, "why": ""}
TEST = {"until": 0.0}                  # the setup page's test: off for a few seconds, then back on
REPORT = {"at": 0.0, "on": None, "cec": "", "tv": "", "signal": ""}   # what the power service last did


def migrate(d):
    """Old single off window (off_from/off_to) -> an on-hours rule, unless the new settings are there."""
    out = {**DEFAULTS, **{k: d[k] for k in DEFAULTS if k in d}}
    if "power_mode" not in d and d.get("off_from") and d.get("off_to") and d["off_from"] != d["off_to"]:
        out["power_mode"] = "hours"
        out["hours"] = [{"days": "daily", "on": d["off_to"], "off": d["off_from"]}]
    return out


def clean_rules(rows):
    """[(days, on, off)] from the form -> rules; rows with no times are dropped. Raises ValueError."""
    rules = []
    for days, on, off in rows:
        if not on and not off:
            continue
        if not (on and off):
            raise ValueError("each on-hours line needs both an on and an off time")
        if days not in DAY_NUMS:
            days = "daily"
        if on == off:
            raise ValueError("the on and off times can't be the same")
        rules.append({"days": days, "on": on, "off": off})
    return rules[:MAX_RULES]


def clean_entities(text):
    ents = []
    for e in re.split(r"[\s,]+", (text or "").strip().lower()):
        if not e:
            continue
        if not ENTITY.match(e):
            raise ValueError(f"'{e}' isn't a Home Assistant entity id like binary_sensor.living_room_motion")
        if e not in ents:
            ents.append(e)
    return ents[:10]


def in_hours(rules, now):
    t, wd = now.strftime("%H:%M"), now.weekday()
    for r in rules:
        days, on, off = DAY_NUMS.get(r.get("days"), DAY_NUMS["daily"]), r.get("on"), r.get("off")
        if not on or not off:
            continue
        if on < off:
            if wd in days and on <= t < off:
                return True
        else:                                           # past midnight: started today, or yesterday
            if (wd in days and t >= on) or ((wd - 1) % 7 in days and t < off):
                return True
    return False


def next_change(rules, now):
    """The next on or off time (for the setup page), or None."""
    best = None
    for add in range(0, 8):
        day = now.date() + dt.timedelta(days=add)
        for r in rules:
            for k in ("on", "off"):
                try:
                    h, m = map(int, r[k].split(":"))
                except (KeyError, ValueError):
                    continue
                at = dt.datetime.combine(day, dt.time(h, m))
                if at > now and in_hours(rules, at) != in_hours(rules, at - dt.timedelta(minutes=1)):
                    best = at if best is None or at < best else best
        if best:
            return best
    return None


def report_presence(occupied, now=None):
    now = now or time.time()
    PRESENCE.update(occupied=bool(occupied), at=now)
    if occupied:
        PRESENCE["last_on"] = now


def occupancy(d, now=None):
    """True/False, or None when there are no sensors or no recent word from Home Assistant."""
    now = now or time.time()
    if not d.get("occupancy"):
        return None
    if not PRESENCE["at"] or now - PRESENCE["at"] > STALE_S:
        return None
    return bool(PRESENCE["occupied"]) or now - PRESENCE["last_on"] < float(d.get("occupancy_hold") or 0) * 60


def note_events(d, active, cam_names, now=None):
    """Camera events that wake the screen (display.events()['active']: cam -> {label, ...})."""
    now = now or time.time()
    wake = d.get("wake") or {}
    for cam, ev in (active or {}).items():
        kind = str(ev.get("label", "")).lower()
        if wake.get(kind):
            until = now + float(d.get("wake_minutes") or 3) * 60
            if until > WAKE["until"]:
                WAKE.update(until=until, why=f"{ev.get('label')} at the {cam_names.get(cam, cam)}")


def wanted(d, now_dt=None, now=None):
    """-> (on, reason) for the screen right now."""
    now = now or time.time()
    now_dt = now_dt or dt.datetime.now()
    if not d.get("enabled", True):
        return False, "the display is turned off on the setup page"
    if TEST["until"] > now:
        return False, "testing: off for a few seconds"
    mode = d.get("power_mode", "always")
    if mode == "always":
        return True, "always on"
    hours_ok = in_hours(d.get("hours") or [], now_dt) if mode in ("hours", "hours_occupied") else True
    occ = occupancy(d, now) if mode in ("occupied", "hours_occupied") else True
    if hours_ok and occ:
        why = {"hours": "on-hours", "occupied": "someone's in the room",
               "hours_occupied": "on-hours and someone's in the room"}[mode]
        return True, why
    if hours_ok and occ is None:
        return True, ("no occupancy sensors set" if not d.get("occupancy") else "no word from Home Assistant's sensors")
    if WAKE["until"] > now:
        return True, f"woken by a camera: {WAKE['why']}"
    return False, ("outside the on-hours" if not hours_ok else "nobody in the room")


def status_line(d, now_dt=None):
    on, why = wanted(d, now_dt)
    s = f"{'On' if on else 'Off'} now ({why})"
    if d.get("power_mode") in ("hours", "hours_occupied") and d.get("hours"):
        nxt = next_change(d["hours"], now_dt or dt.datetime.now())
        if nxt:
            s += f"; on-hours {'end' if in_hours(d['hours'], now_dt or dt.datetime.now()) else 'start'} {nxt.strftime('%a %H:%M')}"
    if d.get("occupancy"):
        if PRESENCE["at"]:
            ago = int((time.time() - PRESENCE["at"]) // 60)
            s += f"; sensors: {'someone there' if PRESENCE['occupied'] else 'nobody'} (Home Assistant, {ago} min ago)"
        else:
            s += "; sensors: no word from Home Assistant yet"
    if REPORT["at"]:
        tv = {"on": "the TV says it's on", "standby": "the TV says it's in standby",
              "no answer": "the TV doesn't answer HDMI-CEC (most monitors don't; the signal is switched instead)",
              "no cec": "no HDMI-CEC on this port"}.get(REPORT["tv"], REPORT["tv"])
        if d.get("cec", True) and tv:
            s += f"; {tv}"
    return s + "."


# --------------------------------------------------------------------------
# Home Assistant package
# --------------------------------------------------------------------------
def ha_package(d):
    """packages/solar_display.yaml: tell the box when the room's occupancy changes."""
    ents = d.get("occupancy") or []
    head = "# Written by the solar monitor (setup page: Screen and cameras); edits are overwritten.\n"
    if not ents or d.get("power_mode") not in ("occupied", "hours_occupied"):
        return head
    js = json.dumps
    payload = ('{"occupied": {{ (expand(' + js(ents).replace('"', "'") + ') | selectattr("state", "in", ["on", "home"])'
               ' | list | count > 0) | tojson }}}')
    return head + "\n".join([
        "rest_command:",
        "  solar_display_presence:",
        "    url: \"http://127.0.0.1/display/presence\"",
        "    method: post",
        "    content_type: \"application/json\"",
        f"    payload: {js(payload)}",
        "automation:",
        "  - id: solar_display_presence",
        "    alias: \"Solar: room occupancy for the wall display\"",
        "    mode: queued",
        "    triggers:",
        "      - trigger: state",
        f"        entity_id: {js(ents)}",
        "      - trigger: homeassistant",
        "        event: start",
        "      - trigger: time_pattern",
        "        minutes: \"/5\"",
        "    actions:",
        "      - action: rest_command.solar_display_presence",
    ]) + "\n"


# --------------------------------------------------------------------------
# HDMI-CEC (cec-ctl from v4l-utils), used by the power service as root
# --------------------------------------------------------------------------
def _cec(dev, *args, timeout=15):
    try:
        return subprocess.run(["cec-ctl", "-d", dev, *args], capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def cec_device():
    """The CEC adapter on the port the screen is on (a real physical address), or ''."""
    for dev in sorted(f"/dev/{n}" for n in os.listdir("/dev") if re.fullmatch(r"cec\d+", n)):
        out = _cec(dev, "--playback", "--osd-name", "Solar monitor")
        m = re.search(r"Physical Address\s*:\s*([0-9a-f.]+)", out)
        if m and m.group(1) != "f.f.f.f":
            return dev
    return ""


def parse_power(out):
    """cec-ctl --give-device-power-status output -> 'on' | 'standby' | 'no answer' | ''."""
    if "Not Acknowledged" in out or "NACK" in out:
        return "no answer"
    m = re.search(r"pwr-state:\s*([a-z-]+)", out)
    if m:
        return "on" if m.group(1) in ("on", "to-on") else "standby"
    return ""


def cec_power(dev):
    return parse_power(_cec(dev, "--to", "0", "--give-device-power-status"))


def cec_on(dev):
    _cec(dev, "--to", "0", "--image-view-on")
    m = re.search(r"Physical Address\s*:\s*([0-9a-f.]+)", _cec(dev))
    if m and m.group(1) != "f.f.f.f":                   # and switch the TV to this input
        _cec(dev, "--to", "15", "--active-source", f"phys-addr={m.group(1)}")


def cec_off(dev):
    _cec(dev, "--to", "0", "--standby")
