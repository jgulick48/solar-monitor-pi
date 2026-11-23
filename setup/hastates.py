"""Home Assistant states for the modules, without a Home Assistant token, and their history in VictoriaMetrics.

Each enabled module names the entities it reads (Module.ha_entities); the "Home Assistant history" module adds any
others the owner wants kept. packages/site.yaml has Home Assistant publish exactly those to the box's own Mosquitto
(mqtt_statestream, under solar/ha/), and the collector listens:

  * the latest state of each is kept in state/ha-states.json, which any process can read:
      get(stack, "sensor.boiler_probes_floor_supply") -> {"state": "41.25", "unit": "°C", "at": 1795400000.0, ...}
  * every change, and the current value once a minute, go to VictoriaMetrics (kept 5 years, re-analysable):
      ha_state{entity_id="sensor.boiler_probes_floor_supply"} 41.25       (in the entity's own unit)
      ha_attribute{entity_id="climate.t6",attribute="hvac_action"} 1        (heating 1, idle or off 0, cooling -1)
    On/off states are 1/0; text states ("heat", "unavailable") are not numbers and are not recorded. While Home
    Assistant is offline (its MQTT status), nothing is repeated, so its gaps show as gaps.

Statestream publishes changes, retained, so the collector has every value again as soon as it (re)subscribes; after a
box restart, until then, get() falls back to Home Assistant's own restore file (.storage/core.restore_state).
"""
import datetime
import json
import math
import os
import re
import subprocess
import threading
import time
import urllib.request

import solar_setup as ss

BASE = "solar/ha"
HA_STATUS = "homeassistant/status"      # Home Assistant's MQTT birth and last-will messages: "online" / "offline"
VM = os.environ.get("SOLAR_VM", "http://127.0.0.1:8428")
REPEAT = 60                             # seconds: the current value again, so graphs and queries have no holes
QUEUE_MAX = 20000                       # lines kept for VictoriaMetrics while it is unreachable
ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
WORDS = {"on": 1, "off": 0, "true": 1, "false": 0, "open": 1, "closed": 0, "home": 1, "not_home": 0,
         "heating": 1, "cooling": -1, "idle": 0, "drying": 0, "fan": 0, "preheating": 1, "defrosting": 0}
# attributes recorded when they are numbers (or one of WORDS for hvac_action); the rest (names, icons, limits) are not
ATTRIBUTES = {"current_temperature", "temperature", "target_temp_high", "target_temp_low", "current_humidity",
              "humidity", "hvac_action", "battery_level", "power", "current", "voltage", "energy"}
# entity domains offered when picking what to record
DOMAINS = ("sensor", "binary_sensor", "climate", "switch", "water_heater", "number", "input_number", "input_boolean")

_lock = threading.Lock()
_latest = {}            # entity_id -> {"state", "unit", "at", "attributes": {name: value}}
_sent = {}              # (entity_id, attribute or "") -> last time a value went to VictoriaMetrics
_queue = []             # lines waiting for VictoriaMetrics
_status = {"ha": "online", "dirty": False}


def _path(stack):
    return os.path.join(stack, "state", "ha-states.json")


# --------------------------------------------------------------------------
# What Home Assistant publishes
# --------------------------------------------------------------------------
def entities(stack):
    """Every entity the enabled modules read, sorted."""
    import modules
    return modules.ha_entities(stack)


def package(ents):
    """(top-level lines, automation lines) for packages/site.yaml; ([], []) when nothing is read.

    mqtt_statestream publishes changes only, so something that rarely changes (a plug that stays on, a setting)
    would never reach the box: the automation also publishes each one's state when Home Assistant starts and every
    10 minutes, retained, on the same topics."""
    ents = sorted(set(ents))
    if not ents:
        return [], []
    top = (["mqtt_statestream:", f"  base_topic: {BASE}", "  publish_attributes: true", "  include:", "    entities:"]
           + [f"      - {e}" for e in ents])
    auto = [
        "  - id: solar_statestream_refresh",
        "    alias: \"Solar: publish the box's entities\"",
        "    mode: single",
        "    triggers:",
        "      - trigger: homeassistant",
        "        event: start",
        "      - trigger: time_pattern",
        "        minutes: \"/10\"",
        "    actions:",
        "      - repeat:",
        "          for_each:",
    ] + [f"            - {e}" for e in ents] + [
        "          sequence:",
        "            - condition: template",
        "              value_template: \"{{ states(repeat.item) not in ['unknown', 'unavailable'] }}\"",
        "            - action: mqtt.publish",
        "              data:",
        f"                topic: \"{BASE}/{{{{ repeat.item | replace('.', '/') }}}}/state\"",
        "                payload: \"{{ states(repeat.item) }}\"",
        "                retain: true",
    ]
    return top, auto


def apply(stack):
    """Rewrite packages/site.yaml for the current list and restart Home Assistant (in the background) if it changed:
    statestream's settings are only read when Home Assistant starts. Returns True if a restart was started."""
    site = ss.load_json(os.path.join(stack, "state", "site.json"), {})
    p = os.path.join(stack, "homeassistant", "packages", "site.yaml")
    if not os.path.isdir(os.path.dirname(p)) or not ss.write_if_changed(p, ss.site_yaml(stack, site), 0o644):
        return False

    def run():
        try:
            subprocess.run(ss.compose_cmd(stack) + ["up", "-d", "--force-recreate", "homeassistant"],
                           capture_output=True, text=True)
        except OSError:
            pass
    threading.Thread(target=run, daemon=True).start()
    return True


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------
def number(text):
    """A state or attribute as a number for VictoriaMetrics, or None (text, unavailable, NaN)."""
    if isinstance(text, bool):
        return 1.0 if text else 0.0
    if isinstance(text, (int, float)):
        v = float(text)
    else:
        t = str(text).strip().strip('"').lower()
        if t in WORDS:
            return float(WORDS[t])
        try:
            v = float(t)
        except ValueError:
            return None
    return v if math.isfinite(v) else None


def _label(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _line(eid, attr, value, ts_ms):
    """One sample. No unit label: Home Assistant sends the unit after the first state, and a label that changes
    splits a series in two; the unit is in state/ha-states.json and Home Assistant."""
    if attr:
        return f'ha_attribute{{entity_id="{eid}",attribute="{_label(attr)}"}} {value:.6g} {ts_ms}'
    return f'ha_state{{entity_id="{eid}"}} {value:.6g} {ts_ms}'


def on_message(topic, payload, now=None):
    """solar/ha/<domain>/<object_id>/<state | unit_of_measurement | attribute> (attributes JSON-encoded), and Home
    Assistant's own online/offline status."""
    now = now if now is not None else time.time()
    text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)
    if topic == HA_STATUS:
        with _lock:
            _status["ha"] = text.strip()
        return
    parts = topic.split("/")
    if len(parts) != 5 or "/".join(parts[:2]) != BASE:
        return
    eid, key = f"{parts[2]}.{parts[3]}", parts[4]
    if not ENTITY_RE.match(eid):
        return
    with _lock:
        cur = _latest.setdefault(eid, {"state": None, "unit": None, "at": 0.0, "attributes": {}})
        if key == "state":
            changed = text != cur["state"]
            cur["state"] = text
            if changed or not cur["at"]:
                cur["at"] = now
            v = number(text)
            if v is not None:
                _queue.append(_line(eid, "", v, int(now * 1000)))
                _sent[(eid, "")] = now
            else:
                _sent.pop((eid, ""), None)
        elif key == "unit_of_measurement":
            try:
                cur["unit"] = json.loads(text)
            except ValueError:
                cur["unit"] = text
        elif key in ATTRIBUTES:
            try:
                val = json.loads(text)
            except ValueError:
                val = text
            cur["attributes"][key] = val
            v = number(val)
            if v is not None:
                _queue.append(_line(eid, key, v, int(now * 1000)))
                _sent[(eid, key)] = now
            else:
                _sent.pop((eid, key), None)
        else:
            return
        del _queue[:-QUEUE_MAX]
        _status["dirty"] = True


def repeat_due(now=None):
    """The current value of everything not sent in the last minute (none while Home Assistant is offline)."""
    now = now if now is not None else time.time()
    with _lock:
        if _status["ha"] != "online":
            return 0
        n = 0
        for (eid, attr), last in list(_sent.items()):
            if now - last < REPEAT:
                continue
            cur = _latest.get(eid) or {}
            v = number(cur.get("attributes", {}).get(attr) if attr else cur.get("state"))
            if v is None:
                _sent.pop((eid, attr), None)
                continue
            _queue.append(_line(eid, attr, v, int(now * 1000)))
            _sent[(eid, attr)] = now
            n += 1
        del _queue[:-QUEUE_MAX]
        return n


def vm_push():
    """Send what is queued to VictoriaMetrics; kept for the next try if it is unreachable."""
    with _lock:
        lines = list(_queue)
    if not lines:
        return 0
    req = urllib.request.Request(f"{VM}/api/v1/import/prometheus", data=("\n".join(lines) + "\n").encode(), method="POST")
    urllib.request.urlopen(req, timeout=20).read()
    with _lock:
        del _queue[:len(lines)]
    return len(lines)


def flush(stack):
    """Write the latest states to state/ha-states.json if anything arrived."""
    with _lock:
        if not _status["dirty"]:
            return False
        _status["dirty"] = False
        data = {k: {**v, "attributes": dict(v["attributes"])} for k, v in _latest.items()}
    return ss.write_if_changed(_path(stack), json.dumps(data, indent=1, sort_keys=True) + "\n", 0o644)


# --------------------------------------------------------------------------
# Reading (any process)
# --------------------------------------------------------------------------
def _restore(stack):
    """{entity_id: {"state", "unit", "at", "attributes"}} from Home Assistant's restore file."""
    p = os.path.join(stack, "homeassistant", ".storage", "core.restore_state")
    out = {}
    for item in (ss.load_json(p, {}).get("data") or []):
        st = item.get("state") or {}
        eid = st.get("entity_id")
        if not eid:
            continue
        at = st.get("last_changed") or st.get("last_updated") or ""
        try:
            ts = datetime.datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = 0.0
        attrs = st.get("attributes") or {}
        out[eid] = {"state": st.get("state"), "unit": attrs.get("unit_of_measurement"), "at": ts,
                    "attributes": {k: v for k, v in attrs.items() if k in ATTRIBUTES}}
    return out


def get(stack, entity_id):
    """The latest state of a Home Assistant entity, or None."""
    live = ss.load_json(_path(stack), {}).get(entity_id)
    if live and live.get("state") is not None:
        return live
    return _restore(stack).get(entity_id)


def known(stack, domains=DOMAINS, device_class=None):
    """Home Assistant's entities, for pickers: [{"entity_id", "name", "unit", "device_class"}] from its entity
    registry (no token needed), sorted by entity id; disabled entities left out."""
    reg = ss.load_json(os.path.join(stack, "homeassistant", ".storage", "core.entity_registry"), {})
    out = []
    for e in (reg.get("data") or {}).get("entities", []):
        eid = e.get("entity_id", "")
        if e.get("disabled_by") or eid.split(".", 1)[0] not in domains:
            continue
        dc = e.get("device_class") or e.get("original_device_class")
        if device_class and dc != device_class:
            continue
        out.append({"entity_id": eid, "name": e.get("name") or e.get("original_name") or "",
                    "unit": e.get("unit_of_measurement") or "", "device_class": dc or ""})
    return sorted(out, key=lambda x: x["entity_id"])


# --------------------------------------------------------------------------
# The collector's listener
# --------------------------------------------------------------------------
def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "hastates:", *a, flush=True)


def listen(stack, stop):
    """Stay subscribed to the statestream (and Home Assistant's status), reconnecting after a drop."""
    import mqttsub
    while not stop.is_set():
        if not entities(stack):
            stop.wait(60)                       # nothing is read: no connection
            continue
        sec = ss.load_json(os.path.join(stack, "state", "secrets.json"), {})
        try:
            mqttsub.run("127.0.0.1", 1883, "homeassistant", sec.get("mqtt_homeassistant", ""), [BASE + "/#", HA_STATUS],
                        on_message, client_id="solar-box-hastates", stop=stop)
        except Exception as e:  # noqa: BLE001 - the broker restarts with the stack: try again
            log("listening stopped:", e)
        stop.wait(15)


def keep(stack, stop, every=10):
    """Every 10 seconds: the state file and VictoriaMetrics, with the once-a-minute repeat."""
    failing = False
    while not stop.wait(every):
        try:
            flush(stack)
            repeat_due()
            vm_push()
            if failing:
                log("VictoriaMetrics reachable again")
            failing = False
        except Exception as e:  # noqa: BLE001 - VictoriaMetrics restarts with the stack: the queue waits
            if not failing:
                log("could not write to VictoriaMetrics (kept for later):", e)
            failing = True


def start(stack):
    stop = threading.Event()
    for fn, name in ((listen, "hastates"), (keep, "hastates-keep")):
        threading.Thread(target=fn, args=(stack, stop), daemon=True, name=name).start()
    return stop
