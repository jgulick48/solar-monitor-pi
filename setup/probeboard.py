#!/usr/bin/env python3
"""The boiler probe board from the setup page: the Waveshare ESP32-P4-ETH on the carrier board (esphome/carrier/),
running esphome/boiler-probes.yaml.

  * The box builds the firmware itself, with ESPHome's own container (IMAGE, pulled the first time: about 1.5 GB,
    plus the ESP32 toolchain it downloads on its first build), from a copy of boiler-probes.yaml and a small wrapper
    with this box's port names (state/esphome/box.yaml) and its own secrets: the API encryption key and the
    over-the-network update password (state/secrets.json: probeboard_api_key, probeboard_ota_password).
  * Over USB: the first install, with the ESP32 plugged into one of the Pi's USB ports and its PoE unplugged (the
    board isn't isolated: USB from the Pi or PoE, not both).
  * Over the network: every later update, to boiler-probes.local (the board's own name on the LAN).
  * Home Assistant: after the first install the box adds the board to its Home Assistant (an ESPHome config entry
    with the key, written while Home Assistant is stopped, as the MQTT one is when the box is set up). Home
    Assistant ties it to the board's MAC address the first time it connects.

One job runs at a time, in the background; state/probeboard-job.json has its progress for the setup page.

The check-up page reads the board itself, not Home Assistant: live() takes what the board's own web page streams
(http://boiler-probes.local/events) and ports() says, per port, whether a probe answers and what it reads. The ports
that have had a probe are remembered (state/probeboard-seen.json), so one that goes missing shows as a fault and an
unused port doesn't.
"""
import base64
import datetime
import glob
import http.client
import json
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time

import solar_setup as ss

IMAGE = "ghcr.io/esphome/esphome:2026.11.0"
DEVICE = "boiler-probes"                    # substitutions.name in boiler-probes.yaml
HOST = DEVICE + ".local"
PORT = 6053
NAMES = ["Floor supply", "Floor return", "Boiler supply", "Boiler return", "Hot water outlet", "Slab near boiler",
         "Slab far", "Probe P8"]            # boiler-probes.yaml's defaults, P1-P8
NAME_MAX = 24
DEFAULTS = {"names": list(NAMES), "built_names": [], "installed": 0, "ha_added": False,
            "old_chip": False}             # an ESP32-P4 before revision 3 needs its own build (boiler-probes.yaml)
ROOT = "/"                                  # tests point this at a fake root (for /dev)
DOCKER = ["docker"]
_lock = threading.Lock()


def settings(stack):
    import modules
    return {**DEFAULTS, **modules.settings(stack, "probeboard")}


def save(stack, d):
    import modules
    modules.save(stack, "probeboard", d)


def clean_name(s, default):
    """A port name for Home Assistant and the OLED: printable, no quotes or backslashes (it goes into YAML and C++),
    at most NAME_MAX characters; empty means the default."""
    s = "".join(c for c in (s or "") if c.isprintable() and c not in "\"'\\$`{}").strip()
    return s[:NAME_MAX].strip() or default


def api_key(stack):
    return ss.secret(stack, "probeboard_api_key", lambda: base64.b64encode(os.urandom(32)).decode())


def ota_password(stack):
    return ss.secret(stack, "probeboard_ota_password", lambda: secrets.token_hex(16))


# --------------------------------------------------------------------------
# The build files
# --------------------------------------------------------------------------
def source_yaml():
    """boiler-probes.yaml: in the box's source tree (/opt/solar-monitor/src/esphome) or this repository's."""
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.join(here, "..", "src", "esphome"), os.path.join(here, "..", "esphome")):
        p = os.path.join(d, "boiler-probes.yaml")
        if os.path.exists(p):
            return os.path.abspath(p)
    raise FileNotFoundError("boiler-probes.yaml not found")


def workdir(stack):
    return os.path.join(stack, "state", "esphome")


def write_build_files(stack, names, old_chip=False):
    """state/esphome/: boiler-probes.yaml (copied), secrets.yaml (this box's) and box.yaml (the port names)."""
    wd = workdir(stack)
    os.makedirs(wd, mode=0o700, exist_ok=True)
    shutil.copyfile(source_yaml(), os.path.join(wd, "boiler-probes.yaml"))
    ss.write_if_changed(os.path.join(wd, "secrets.yaml"),
                        f"api_encryption_key: {json.dumps(api_key(stack))}\nota_password: {json.dumps(ota_password(stack))}\n", 0o600)
    subs = "".join(f"  p{i}_name: {json.dumps(n)}\n" for i, n in enumerate(names, 1))
    subs += f"  old_chip: \"{'true' if old_chip else 'false'}\"\n"
    ss.write_if_changed(os.path.join(wd, "box.yaml"),
                        "# Written by the solar monitor (the probe board's port names); edits are overwritten.\n"
                        f"substitutions:\n{subs}\npackages:\n  board: !include boiler-probes.yaml\n", 0o600)
    return wd


# --------------------------------------------------------------------------
# The ESP32 on USB
# --------------------------------------------------------------------------
USB_IDS = ("espressif", "303a",     # an ESP32's own USB port
           "1a86")                  # a WCH USB-serial chip: the ESP32-P4-ETH's USB-C goes through a CH343, which names
#                                     itself "1a86 USB Single Serial" and not the board (seen on the first board)


def usb_boards():
    """ESP32s plugged into the Pi's USB: /dev/serial/by-id names from Espressif or from the board's USB-serial chip
    (USB_IDS), resolved to /dev/ttyACM* paths. The box uses no other USB serial devices (its radios are on the
    network), so a WCH adapter here is taken to be the board."""
    out = []
    for p in sorted(glob.glob(os.path.join(ROOT, "dev", "serial", "by-id", "*"))):
        n = os.path.basename(p).lower()
        if any(x in n for x in USB_IDS) and os.path.realpath(p) not in out:
            out.append(os.path.realpath(p))
    return out


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------
def job_path(stack):
    return os.path.join(stack, "state", "probeboard-job.json")


def job(stack):
    """The last job's progress. One still marked running but with no job in this process was cut short (the web
    service restarted): reported as failed, so it can be started again."""
    j = ss.load_json(job_path(stack), {})
    if j.get("status") == "running" and not _lock.locked():
        j.update(status="error", message="Interrupted (the box's web service restarted): start it again.")
    return j


def _job(stack, **kw):
    d = {**ss.load_json(job_path(stack), {}), **kw}
    ss.write_if_changed(job_path(stack), json.dumps(d) + "\n", 0o644)
    return d


def running(stack):
    return job(stack).get("status") == "running" and _lock.locked()


def start(stack, kind):
    """Start a job in the background: "usb" (install over USB) or "network" (update over the network). Returns a
    message for the page."""
    if not _lock.acquire(blocking=False):
        return "Not started: the probe board is already being built or installed."
    if kind == "usb" and not usb_boards():
        _lock.release()
        return "Not started: no ESP32 found on the box's USB ports. Plug it in (with its PoE unplugged) and try again."

    def run():
        try:
            run_job(stack, kind)
        finally:
            _lock.release()
    threading.Thread(target=run, daemon=True).start()
    return ("Building the firmware and installing it over USB" if kind == "usb" else
            "Building the firmware and sending it to the board over the network") + ": progress below."


def _run_logged(stack, cmd, log, timeout):
    """Run a command, keeping its last lines in the job's log as it goes. Returns its exit code."""
    last = t0 = time.time()
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1) as p:
        for line in p.stdout:
            log.append(line.rstrip()[-200:])
            del log[:-60]
            if time.time() - last > 2:
                _job(stack, log=log)
                last = time.time()
            if time.time() - t0 > timeout:
                p.kill()
                log.append(f"stopped after {timeout // 60} minutes")
                break
        p.wait()
    _job(stack, log=log)
    return p.returncode


def run_job(stack, kind):
    d = settings(stack)
    names = [clean_name(n, NAMES[i]) for i, n in enumerate((d["names"] + NAMES)[:8])]
    log = []
    _job(stack, kind=kind, status="running", started=int(time.time()), ended=0, step="Preparing the build", log=log,
         message="")
    try:
        wd = write_build_files(stack, names, bool(d.get("old_chip")))
        if subprocess.run(DOCKER + ["image", "inspect", IMAGE], capture_output=True).returncode != 0:
            _job(stack, step="Getting ESPHome (the first time only: about 1.5 GB)")
            if _run_logged(stack, DOCKER + ["pull", IMAGE], log, 3600) != 0:
                raise RuntimeError("couldn't download ESPHome")
        cmd = DOCKER + ["run", "--rm", "--network", "host", "-v", f"{wd}:/config"]
        if kind == "usb":
            boards = usb_boards()
            if not boards:
                raise RuntimeError("the ESP32 is no longer on USB")
            cmd += ["--device", f"{boards[0]}:{boards[0]}"]
            target = boards[0]
        else:
            target = HOST
        _job(stack, step="Building the firmware and installing it (the first build downloads the ESP32 toolchain "
                         "and takes a while; later ones a few minutes)")
        if _run_logged(stack, cmd + [IMAGE, "run", "box.yaml", "--device", target, "--no-logs"], log, 5400) != 0:
            if kind == "usb" and any("Failed to connect" in line or "No serial data received" in line for line in log):
                raise RuntimeError("the firmware was built, but the board didn't go into its download mode by itself. "
                                   "Hold BOOT, press and release RESET, then release BOOT, and install again")
            raise RuntimeError("the build or the install failed (the log above says why)")
        d = settings(stack)
        d.update(installed=int(time.time()), built_names=names)
        save(stack, d)
        msgs = ["installed over USB" if kind == "usb" else "updated over the network"]
        if not d["ha_added"]:
            _job(stack, step="Adding it to Home Assistant")
            msgs += add_to_ha(stack)
        _job(stack, status="ok", step="Done", ended=int(time.time()), message="; ".join(msgs) + ".")
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        _job(stack, status="error", ended=int(time.time()), message=f"Failed: {e}.")


# --------------------------------------------------------------------------
# Home Assistant
# --------------------------------------------------------------------------
def entries_path(stack):
    return os.path.join(stack, "homeassistant", ".storage", "core.config_entries")


def ha_entry(stack):
    """This board's ESPHome config entry in Home Assistant, or None."""
    doc = ss.load_json(entries_path(stack), {})
    for e in (doc.get("data") or {}).get("entries", []):
        if e.get("domain") == "esphome" and (e.get("data", {}).get("device_name") == DEVICE
                                             or e.get("data", {}).get("host") in (HOST, DEVICE)):
            return e
    return None


def write_ha_entry(stack):
    """Add (or fix the key of) the board's ESPHome entry in .storage/core.config_entries. Only while Home Assistant
    is stopped: it would overwrite the file. True when changed."""
    path = entries_path(stack)
    doc = ss.load_json(path, None)
    if not doc:
        raise RuntimeError("Home Assistant isn't set up yet")
    entries = doc.setdefault("data", {}).setdefault("entries", [])
    key = api_key(stack)
    for e in entries:
        if e.get("domain") == "esphome" and (e.get("data", {}).get("device_name") == DEVICE
                                             or e.get("data", {}).get("host") in (HOST, DEVICE)):
            if e["data"].get("noise_psk") == key:
                return False
            e["data"]["noise_psk"] = key
            break
    else:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        entries.append({
            "created_at": now, "modified_at": now, "domain": "esphome", "entry_id": secrets.token_hex(16),
            "data": {"host": HOST, "port": PORT, "password": "", "noise_psk": key, "device_name": DEVICE},
            "options": {}, "disabled_by": None, "discovery_keys": {}, "pref_disable_new_entities": False,
            "pref_disable_polling": False, "source": "user", "subentries": [], "title": "Boiler probes",
            "unique_id": None,              # Home Assistant sets the board's MAC address on its first connection
            "version": 1, "minor_version": 1})
    return ss.write_if_changed(path, json.dumps(doc, indent=2) + "\n", 0o600)


def add_to_ha(stack):
    """Stop Home Assistant, add the board, start it again. Returns messages."""
    def compose(*a):
        try:
            return subprocess.run(ss.compose_cmd(stack) + list(a), capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as e:
            return subprocess.CompletedProcess(a, 1, "", str(e))
    if ha_entry(stack) and ha_entry(stack)["data"].get("noise_psk") == api_key(stack):
        d = settings(stack)
        d["ha_added"] = True
        save(stack, d)
        return ["already in Home Assistant"]
    r = compose("stop", "homeassistant")
    if r.returncode != 0:
        return [f"couldn't stop Home Assistant to add the board: {r.stderr.strip()[-200:]}"]
    try:
        write_ha_entry(stack)
    finally:
        r = compose("up", "-d", "homeassistant")
    d = settings(stack)
    d["ha_added"] = True
    save(stack, d)
    return ["added to Home Assistant" if r.returncode == 0 else f"added, but Home Assistant didn't start: {r.stderr.strip()[-200:]}"]


# --------------------------------------------------------------------------
# The board's own readings, for the check-up page
# --------------------------------------------------------------------------
CLAMPS = (("CT1", "Boiler pump current"), ("CT2", "Floor pump current"))   # boiler-probes.yaml's names
HEAT_CALL = "Call for heat"
EVENTS_WAIT = 2.5       # seconds listened to at most: the board sends every reading as soon as one connects
EVENTS_QUIET = 0.8      # ...and nothing more for this long means it has sent them all


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")


def fetch_events(host=HOST, port=80):
    """The lines the board's web page streams on connecting (server-sent events). Raises OSError when the board
    doesn't answer."""
    conn = http.client.HTTPConnection(host, port, timeout=3)
    lines = []
    try:
        conn.connect()
        sock = conn.sock                # kept: the connection lets go of it when the answer has no length
        conn.request("GET", "/events", headers={"Accept": "text/event-stream"})
        r = conn.getresponse()
        if r.status != 200:
            raise OSError(f"the board answered {r.status}")
        sock.settimeout(EVENTS_QUIET)
        end = time.time() + EVENTS_WAIT
        while time.time() < end:
            try:
                line = r.readline()
            except (socket.timeout, TimeoutError):
                if lines:
                    break
                continue
            if not line:
                break
            lines.append(line.decode("utf-8", "replace").rstrip("\r\n"))
        r.close()
    except http.client.HTTPException as e:
        raise OSError(str(e)) from None
    finally:
        conn.close()
    return lines


def parse_events(lines):
    """{(domain, name): value} from the stream's "state" events: value is a number, True / False, or None for a
    sensor with no reading (a port with no probe). The name is normalised ("Floor supply" -> "floor_supply"); an
    entity is found by its name when the event carries one and by its id either way, whichever form the ESPHome
    version uses ("sensor-floor_supply", "sensor/Floor supply")."""
    out = {}
    event = ""
    for line in lines:
        if line.startswith("event:"):
            event = line[6:].strip()
            continue
        if not line.startswith("data:") or event != "state":
            continue
        try:
            d = json.loads(line[5:].strip())
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        eid = str(d.get("name_id") or d.get("id") or "")
        m = re.match(r"^([a-z_]+)[-/](.+)$", eid)
        if not m:
            continue
        domain = m.group(1)
        v = d.get("value")
        if domain == "sensor":
            v = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None
        elif domain == "binary_sensor":
            v = v if isinstance(v, bool) else {"ON": True, "OFF": False}.get(str(d.get("state") or "").upper())
        else:
            continue
        for name in {_norm(m.group(2)), _norm(d.get("name"))} - {""}:
            out[(domain, name)] = v
    return out


def live(fetch=None):
    """What the board reads now ({(domain, name): value}); raises OSError when it doesn't answer or sends nothing."""
    got = parse_events((fetch or fetch_events)())
    if not got:
        raise OSError("the board sent no readings")
    return got


def seen_path(stack):
    return os.path.join(stack, "state", "probeboard-seen.json")


def seen(stack):
    """Port numbers (1-8) that have had a probe."""
    return sorted({int(p) for p in ss.load_json(seen_path(stack), {}).get("ports", []) if str(p).isdigit() and 1 <= int(p) <= 8})


def forget(stack):
    """Forget which ports have had a probe (after taking one out on purpose)."""
    ss.write_if_changed(seen_path(stack), json.dumps({"ports": []}) + "\n", 0o644)


def ports(stack, readings):
    """[{"port": 1-8, "name", "c": °C or None, "missing": had a probe before and has none now}] from live()'s
    readings, by the names the board was built with. Remembers the ports that have a probe now."""
    d = settings(stack)
    names = [clean_name(n, NAMES[i]) for i, n in enumerate(((d["built_names"] or d["names"]) + NAMES)[:8])]
    before = seen(stack)
    out = []
    for i, n in enumerate(names, 1):
        c = readings.get(("sensor", _norm(n)))
        out.append({"port": i, "name": n, "c": c, "missing": c is None and i in before})
    now = sorted(set(before) | {p["port"] for p in out if p["c"] is not None})
    if now != before:
        ss.write_if_changed(seen_path(stack), json.dumps({"ports": now}) + "\n", 0o644)
    return out


def remember(stack, fetch=None):
    """The collector, every few minutes: note which ports have a probe, so a port counts as used without anyone
    opening the check-up page. Nothing happens when the board isn't installed or doesn't answer."""
    if not settings(stack)["installed"]:
        return
    try:
        ports(stack, live(fetch))
    except OSError:
        pass
