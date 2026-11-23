"""Notifications: where the box's alerts go (wiki: "Boiler, propane and notifications"). The notifications module
(modules/notifications) is its switch and settings; this file decides and delivers.

Every alert, from a Home Assistant automation or from a module, arrives here as {tag, title, message, priority}:
Home Assistant's script.solar_notify posts it to /notify/send. By priority:

  critical  every channel, ignores quiet hours, repeats every 4 hours until the alert is cleared
  warning   the phone channels; held during quiet hours and sent when they end (unless cleared meanwhile)
  info      one channel (the HA app, else ntfy); held during quiet hours too

The same tag with the same text is sent once in 30 minutes. message "clear" removes an alert: from the phones
(HA app), from the held ones and from the repeats. Channels: the Home Assistant app on the chosen phones (through a
webhook automation in packages/site.yaml), ntfy (any server: ntfy.sh or your own), and the wall display (spoken by
the voice module, critical only).

  state/notify-state.json   {"held": [...], "active": {tag: {...}}, "recent": {tag+text: time}}
  state/notify-log.json     the last 100 alerts and what happened to each
"""
import contextlib
import fcntl
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request

import solar_setup as ss

PRIORITIES = ("critical", "warning", "info")
# The solar package's alerts (packages/solar_monitor.yaml) don't name a priority: theirs, by tag.
TAG_PRIORITY = {
    "inverter_fault": "critical", "generator": "critical",
    "battery_low": "warning", "dongle_offline": "warning", "no_data": "warning", "backup": "warning",
    "battery_health": "warning", "box_health": "warning",
    "generator_service": "info", "summary": "info",
}
CHANNELS = (("ha_app", "Home Assistant app"), ("ntfy", "ntfy"), ("wall", "Wall display"))
NTFY_PRIORITY = {"critical": 5, "warning": 4, "info": 3}
REPEAT_S = 4 * 3600
DEDUPE_S = 1800
LOG_MAX = 100
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DEFAULTS = {"ha_app": True, "phones": "all", "ntfy": False, "ntfy_server": "https://ntfy.sh", "ntfy_topic": "",
            "ntfy_token": "", "wall": True, "quiet_from": "22:00", "quiet_to": "07:00"}

_lock = threading.Lock()


def _p(stack, name):
    return os.path.join(stack, "state", name)


@contextlib.contextmanager
def locked(stack):
    """One process at a time on the state file (the web server takes alerts, the collector sends held ones)."""
    os.makedirs(os.path.join(stack, "state"), exist_ok=True)
    with _lock, open(_p(stack, "notify.lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _load_state(stack):
    st = ss.load_json(_p(stack, "notify-state.json"), {})
    return {"held": st.get("held", []), "active": st.get("active", {}), "recent": st.get("recent", {})}


def _save_state(stack, st):
    ss.write_if_changed(_p(stack, "notify-state.json"), json.dumps(st, indent=1) + "\n", 0o600)


def log(stack):
    """The sent log, newest first."""
    return ss.load_json(_p(stack, "notify-log.json"), [])


def _log(stack, entry):
    with locked(stack):
        entries = [entry] + log(stack)
        ss.write_if_changed(_p(stack, "notify-log.json"), json.dumps(entries[:LOG_MAX], indent=1) + "\n", 0o600)


def held(stack):
    return _load_state(stack)["held"]


def active(stack):
    return _load_state(stack)["active"]


# --------------------------------------------------------------------------
# Phones and quiet hours
# --------------------------------------------------------------------------
def phones(stack):
    """Phones with the Home Assistant app signed in: [{"id": notify name after mobile_app_, "name": the phone's name}].
    Read from Home Assistant's registries (no HA token needed): the app's device_tracker entity carries the same name
    as its notify action."""
    ha = os.path.join(stack, "homeassistant", ".storage")
    entries = (ss.load_json(os.path.join(ha, "core.config_entries"), {}).get("data") or {}).get("entries", [])
    titles = {e.get("entry_id"): e.get("title") for e in entries if e.get("domain") == "mobile_app"}
    out, seen = [], set()
    for e in (ss.load_json(os.path.join(ha, "core.entity_registry"), {}).get("data") or {}).get("entities", []):
        eid = e.get("entity_id", "")
        if e.get("platform") == "mobile_app" and eid.startswith("device_tracker."):
            pid = eid.split(".", 1)[1]
            seen.add(e.get("config_entry_id"))
            out.append({"id": pid, "name": titles.get(e.get("config_entry_id")) or e.get("original_name") or pid,
                        "registered": True})
    for entry_id, title in titles.items():          # not in the registry yet: HA names the action after the phone
        if entry_id not in seen and title:
            out.append({"id": re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_"), "name": title})
    return sorted(out, key=lambda p: p["name"].lower())


def chosen_phones(stack, s):
    """The phones alerts go to: every phone ("all", so a new phone is included), or the ones ticked. Only phones in
    Home Assistant's entity registry: their notify action is known to exist."""
    ids = [p["id"] for p in phones(stack) if p.get("registered")]
    return ids if s.get("phones", "all") == "all" else [i for i in ids if i in (s.get("phones") or [])]


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_quiet(s, now):
    """True during quiet hours (box local time); none when either end is empty or both are the same."""
    a, b = s.get("quiet_from") or "", s.get("quiet_to") or ""
    if not (TIME_RE.match(a) and TIME_RE.match(b)) or a == b:
        return False
    t = time.localtime(now)
    m, start, end = t.tm_hour * 60 + t.tm_min, _minutes(a), _minutes(b)
    return start <= m < end if start < end else (m >= start or m < end)


# --------------------------------------------------------------------------
# Deciding
# --------------------------------------------------------------------------
def priority_of(alert):
    p = alert.get("priority")
    return p if p in PRIORITIES else TAG_PRIORITY.get(alert.get("tag", ""), "warning")


def channels_for(stack, s, priority, voice_on):
    """The channels this priority goes to, from the ones that are set up."""
    ready = []
    if s.get("ha_app") and chosen_phones(stack, s):
        ready.append("ha_app")
    if s.get("ntfy") and s.get("ntfy_topic"):
        ready.append("ntfy")
    if priority == "critical":
        return ready + (["wall"] if s.get("wall") and voice_on else [])
    if priority == "info":
        return ready[:1]
    return ready


def clean(alert):
    """The fields Home Assistant or a module sent, trimmed."""
    tag = re.sub(r"[^a-z0-9_]", "_", str(alert.get("tag") or "alert").lower())[:48] or "alert"
    out = {"tag": tag, "title": " ".join(str(alert.get("title") or "").split())[:120],
           "message": str(alert.get("message") or "").strip()[:2000]}
    out["priority"] = priority_of({**alert, "tag": tag})
    return out


def submit(stack, s, alert, now=None, voice_on=False, deliver=None):
    """Take one alert: send it now, hold it for the end of quiet hours, or drop it as a repeat. Returns what
    happened, as a short text (also in the log)."""
    deliver = deliver or send
    now = now or time.time()
    a = clean(alert)
    if a["tag"] == "test":                                   # the setup page's test: everything set up, now
        a["priority"] = "info"
        chans = channels_for(stack, s, "critical", voice_on)
        res = deliver(stack, s, a, chans)
        _log(stack, {"at": now, **a, "result": res or {"-": "no channel is set up"}})
        return "sent"
    if a["message"].lower() == "clear":
        with locked(stack):
            st = _load_state(stack)
            st["active"].pop(a["tag"], None)
            st["held"] = [h for h in st["held"] if h["tag"] != a["tag"]]
            st["recent"] = {k: v for k, v in st["recent"].items() if not k.startswith(a["tag"] + "\n")}
            _save_state(stack, st)
        if s.get("ha_app") and chosen_phones(stack, s):
            deliver(stack, s, {**a, "message": "clear_notification", "title": ""}, ["ha_app"])
        return "cleared"
    key = a["tag"] + "\n" + a["message"]
    with locked(stack):
        st = _load_state(stack)
        st["recent"] = {k: v for k, v in st["recent"].items() if now - v < DEDUPE_S}
        if key in st["recent"]:
            _save_state(stack, st)
            return "repeat, not sent again"
        st["recent"][key] = now
        if a["priority"] == "critical":
            st["active"][a["tag"]] = {**a, "first": now, "last_sent": now}
        hold = a["priority"] != "critical" and in_quiet(s, now)
        if hold:
            st["held"] = [h for h in st["held"] if h["tag"] != a["tag"]] + [{**a, "at": now}]
        _save_state(stack, st)
    if hold:
        _log(stack, {"at": now, **a, "result": {"-": f"held for the end of quiet hours ({s.get('quiet_to')})"}})
        return "held"
    res = deliver(stack, s, a, channels_for(stack, s, a["priority"], voice_on))
    _log(stack, {"at": now, **a, "result": res or {"-": "no channel is set up"}})
    return "sent"


def due(stack, s, now=None, voice_on=False, deliver=None):
    """The collector, every minute: send held alerts once quiet hours are over, repeat critical ones every 4 hours."""
    deliver = deliver or send
    now = now or time.time()
    out = []
    with locked(stack):
        st = _load_state(stack)
        if st["held"] and not in_quiet(s, now):
            out += [(h, "after quiet hours") for h in st["held"]]
            st["held"] = []
        for tag, a in st["active"].items():
            if now - a.get("last_sent", 0) >= REPEAT_S:
                a["last_sent"] = now
                out.append(({k: a[k] for k in ("tag", "title", "message", "priority")}, "repeat, not cleared yet"))
        _save_state(stack, st)
    msgs = []
    for a, why in out:
        a = {k: a[k] for k in ("tag", "title", "message", "priority")}
        res = deliver(stack, s, a, channels_for(stack, s, a["priority"], voice_on))
        _log(stack, {"at": now, **a, "result": {**(res or {"-": "no channel is set up"}), "why": why}})
        msgs.append(f"{a['title'] or a['tag']}: {why}")
    return msgs


# --------------------------------------------------------------------------
# Delivering
# --------------------------------------------------------------------------
def ha_webhook(stack):
    """This box's secret webhook id for delivering to the HA app (kept in state/secrets.json)."""
    return ss.secret(stack, "ha_notify_webhook", lambda: "solar-notify-" + secrets.token_urlsafe(24))


def box_alert_webhook(stack):
    """The webhook the box's own modules raise alerts through (packages/site.yaml: solar_notify_from_box)."""
    return ss.secret(stack, "ha_alert_webhook", lambda: "solar-alert-" + secrets.token_urlsafe(24))


def alert(stack, tag, title, message, priority="warning"):
    """Raise (or, with message "clear", remove) an alert from a module: it goes through Home Assistant's
    script.solar_notify, so it shows in Home Assistant and is routed like every other alert. Returns an error text
    or ""."""
    try:
        _post(f"http://127.0.0.1:8123/api/webhook/{box_alert_webhook(stack)}",
              {"tag": tag, "title": title, "message": message, "priority": priority}, {})
        return ""
    except (OSError, ValueError) as e:
        return str(getattr(e, "reason", e))


def box_token(stack):
    """The token Home Assistant sends with each alert to /notify/send."""
    return ss.secret(stack, "notify_token", lambda: secrets.token_urlsafe(24))


def _post(url, body, headers, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def ha_data(a):
    """The HA app's notification options for this alert: replaces the earlier one with the same tag; urgent ones
    arrive at once (Android high priority, iOS time-sensitive)."""
    d = {"tag": f"solar_{a['tag']}", "group": "solar"}
    if a["priority"] in ("critical", "warning"):
        d.update({"ttl": 0, "priority": "high"})
    if a["priority"] == "critical":
        d["push"] = {"interruption-level": "time-sensitive"}
    return d


def send(stack, s, a, channels):
    """Deliver one alert to the given channels; {channel: what happened}."""
    res = {}
    for ch in channels:
        try:
            if ch == "ha_app":
                p = chosen_phones(stack, s)
                _post(f"http://127.0.0.1:8123/api/webhook/{ha_webhook(stack)}",
                      {"phones": p, "title": a["title"], "message": a["message"], "data": ha_data(a)}, {})
                names = {x["id"]: x["name"] for x in phones(stack)}
                res[ch] = "sent to " + ", ".join(names.get(i, i) for i in p)
            elif ch == "ntfy":
                body = {"topic": s["ntfy_topic"], "title": a["title"] or "Solar monitor", "message": a["message"],
                        "priority": NTFY_PRIORITY[a["priority"]], "tags": [a["tag"]]}
                hdr = {"Authorization": f"Bearer {s['ntfy_token']}"} if s.get("ntfy_token") else {}
                _post(s["ntfy_server"].rstrip("/"), body, hdr)
                res[ch] = "sent"
            elif ch == "wall":
                from modules import voice
                said = voice.announce(stack, f"{a['title']}. {a['message']}" if a["title"] else a["message"], chime="doorbell")
                res[ch] = "announced" if said else "voice announcements are off"
        except urllib.error.HTTPError as e:
            res[ch] = f"failed: HTTP {e.code}"
        except (OSError, ValueError) as e:
            res[ch] = f"failed: {getattr(e, 'reason', e)}"
    return res
