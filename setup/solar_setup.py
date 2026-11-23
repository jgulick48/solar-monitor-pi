#!/usr/bin/env python3
"""solar-setup: find EG4/LuxPower WiFi dongles on the LAN and configure the stack.

Runs at every boot (before the stack starts) and hourly. Standard library only.

  * Scans the local IPv4 networks for TCP port 8000 and identifies LuxPower
    dataloggers by listening to the frames they send (heartbeats carry the
    datalog serial, replies to the EG4 cloud's polls carry the inverter
    serial). If nothing is polling a dongle, one read-only request (function 4,
    input registers 0-39, placeholder inverter serial) gets the inverter
    serial. Nothing is ever written to an inverter.
  * Remembers dongles by datalog serial, so a dongle that gets a new IP from
    DHCP is found again.
  * Generates per-box secrets on first run (MQTT accounts, Grafana admin).
  * Writes the bridge config, the Mosquitto password file, Grafana's .env and
    the Home Assistant seed (first run only). The setup page (solar_web.py)
    shows the result and collects the site details.
  * With --apply, restarts the bridge when its config changed.
"""
import argparse
import base64
import concurrent.futures
import datetime
import hashlib
import ipaddress
import json
import os
import threading
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import time

PORT = 8000
FRAME_MAGIC = b"\xa1\x1a"
TCP_HEARTBEAT = 0xC1
TCP_TRANSLATED = 0xC2


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# --------------------------------------------------------------------------
# LuxPower framing
# --------------------------------------------------------------------------
def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def read_input_request(datalog: bytes, serial: bytes = b"0000000000", reg: int = 0, count: int = 40) -> bytes:
    """Read-only request: device function 4 (read input registers)."""
    data = bytes([0, 4]) + serial + struct.pack("<HH", reg, count)
    data += struct.pack("<H", crc16_modbus(data))
    payload = bytes([1, TCP_TRANSLATED]) + datalog + struct.pack("<H", len(data)) + data
    return bytes([0xA1, 0x1A, 1, 0]) + struct.pack("<H", len(payload)) + payload


def parse_frames(buf: bytes):
    """Yield (tcp_function, datalog, inverter_serial_or_None) for complete frames."""
    i = 0
    while i + 6 <= len(buf):
        if buf[i:i + 2] != FRAME_MAGIC:
            i += 1
            continue
        length = struct.unpack("<H", buf[i + 4:i + 6])[0]
        frame = buf[i:i + 6 + length]
        if len(frame) < 6 + length or length < 12:
            break
        i += 6 + length
        func = frame[7]
        datalog = frame[8:18]
        serial = None
        if func == TCP_TRANSLATED and len(frame) >= 32:
            serial = frame[22:32]
        yield func, datalog, serial


def printable_serial(b) -> str:
    if not b:
        return ""
    s = b.decode("ascii", errors="replace")
    return s if s.isalnum() and len(s) == 10 else ""


def identify(host: str, listen_s: float = 75.0, probe_after_s: float = 20.0):
    """Return {'host', 'datalog', 'serial'} or None if host isn't a LuxPower dongle."""
    try:
        sock = socket.create_connection((host, PORT), timeout=3)
    except OSError:
        return None
    sock.settimeout(1.0)
    buf = b""
    datalog = serial = ""
    probed = False
    start = time.time()
    try:
        while time.time() - start < listen_s:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            except socket.timeout:
                pass
            for func, dl, sn in parse_frames(buf):
                datalog = datalog or printable_serial(dl)
                if sn and printable_serial(sn) and printable_serial(sn) != "0000000000":
                    serial = printable_serial(sn)
            if datalog and serial:
                break
            if datalog and not serial and not probed and time.time() - start > probe_after_s:
                sock.sendall(read_input_request(datalog.encode()))
                probed = True
            if len(buf) > 65536:
                buf = buf[-8192:]
    except OSError:
        pass
    finally:
        sock.close()
    if not datalog:
        return None
    return {"host": host, "datalog": datalog, "serial": serial}


# --------------------------------------------------------------------------
# Network scan
# --------------------------------------------------------------------------
def local_networks(max_prefix: int = 22):
    out = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True, check=True).stdout
    nets = []
    for iface in json.loads(out):
        name = iface.get("ifname", "")
        if name == "lo" or name.startswith(("docker", "br-", "veth", "cni", "flannel", "tailscale")):
            continue
        for a in iface.get("addr_info", []):
            net = ipaddress.ip_network(f"{a['local']}/{a['prefixlen']}", strict=False)
            if net.prefixlen < max_prefix:  # don't sweep huge networks; scan the /22 around us
                net = ipaddress.ip_network(f"{a['local']}/{max_prefix}", strict=False)
            if net.is_private and net not in nets:
                nets.append(net)
    return nets


def port_open(host: str, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, PORT), timeout=timeout):
            return True
    except OSError:
        return False


def scan(networks, extra_hosts):
    hosts = list(dict.fromkeys(extra_hosts + [str(h) for n in networks for h in n.hosts()]))
    log(f"scanning {len(hosts)} addresses on port {PORT} ({', '.join(map(str, networks)) or 'no networks'})")
    with concurrent.futures.ThreadPoolExecutor(128) as ex:
        open_hosts = [h for h, ok in zip(hosts, ex.map(port_open, hosts)) if ok]
    log(f"port {PORT} open on: {', '.join(open_hosts) or 'none'}")
    found = []
    with concurrent.futures.ThreadPoolExecutor(16) as ex:
        for r in ex.map(identify, open_hosts):
            if r:
                found.append(r)
                log(f"dongle {r['datalog']} at {r['host']}, inverter {r['serial'] or '(serial not seen yet)'}")
    return found


# --------------------------------------------------------------------------
# Generated files
# --------------------------------------------------------------------------
def mosquitto_hash(password: str) -> str:
    """Mosquitto 2.x password file entry ($7$: PBKDF2-SHA512, 101 iterations)."""
    salt = os.urandom(12)
    dk = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 101, 64)
    return f"$7$101${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def write_if_changed(path: str, content: str, mode: int = 0o644) -> bool:
    try:
        with open(path) as f:
            if f.read() == content:
                return False
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # a temporary name of its own: two processes (the web page and the collector) may write the same file at once
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return True


def compose_cmd(stack):
    """docker compose for the stack; SOLAR_COMPOSE overrides (e.g. a containerised compose for tests)."""
    base = os.environ.get("SOLAR_COMPOSE", "docker compose").split()
    return base + ["--project-directory", stack]


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# Hold each inverter reading at its last value for up to 5 minutes. When a dongle drops off for a moment the bridge
# stops exporting its readings and VictoriaMetrics marks them stale at once, so sums over the inverters (solar,
# load, battery, today's totals) dipped by that inverter's share until it was back (seen on site). Applied to the
# live queries (wall display, Home Assistant, start page); a dongle gone longer than that stops counting.
HOLD = "5m"
_SELECTOR = re.compile(r"\b(lxp_[a-z0-9_]+)(\{[^}]*\})?(?![\[{(a-z0-9_]|\s*\[)")


def hold(expr, window=HOLD):
    """expr with every bare inverter selector (lxp_*) wrapped in last_over_time(...[window]); selectors that
    already have a range ([1d]) are left alone."""
    return _SELECTOR.sub(lambda m: f"last_over_time({m.group(1)}{m.group(2) or ''}[{window}])", expr)


# The battery's BMS data (cell voltages and temperatures, SOH, cycles, BMS faults, rated capacity) comes from the
# inverter(s) connected to the BMS. In a parallel system the others report 0 for all of it (seen on site: 0 V and
# 0 degrees from the second 6000XP made the coldest cell "0 C" and the cell spread 3.3 V). A unit counts as
# BMS-connected when it reports a cell voltage.
BMS_UNITS = "(max by (deploymentID) (lxp_max_cell_volt) > 0)"


def from_bms(selector):
    """selector limited to the BMS-connected inverter(s)."""
    return f"({selector} and on (deploymentID) {BMS_UNITS})"


# House energy today (kWh): per inverter the larger of its load and EPS (backup output) day counters. Hybrids count the
# whole load (including what a generator passes through) in e_load_day; the EG4 6000XP reports e_load_day as 0 and
# the house's energy only in e_eps_day, so "load or eps" showed 0.0 kWh there (seen on site). Either counter alone works.
LOAD_ENERGY_DAY = "sum((lxp_e_load_day > lxp_e_eps_day) or lxp_e_eps_day or lxp_e_load_day)"


# Generator power from the inverters (W) and today's generator energy (kWh), for the display, the generator log and the
# advisor. The GEN port counts only while it has generator voltage, and below 25 kW: on the EG4 6000XP those registers
# hold other data (seen on site: 55-64 kW, 51.2 kWh today and a dry contact "on" with 0 V and no generator). Plus the AC
# input while it has voltage: an off-grid box has nothing but a generator on it, and the 6000XP's generator connects
# there. The GEN port's energy counter only counts if the port had voltage in the last day.
GEN_POWER = ("((sum((lxp_p_gen < 25000) * (lxp_v_gen > bool 50)) or vector(0))"
             " + (sum(lxp_p_to_user * (lxp_v_ac_r > bool 50)) or vector(0)))")
# The battery's own power from its BMS (W, + charging). With two or more inverters on one bank only one of them gets
# the BMS's current; the others report 0 (seen on site: -17.1 A and 0 while discharging, so max() read "idle" and
# the inverters' 855 W discharge looked like 855 W of DC charging). Take the non-zero one, in either direction; empty
# when no inverter reports it.
BMS_POWER = ("(max(lxp_batt_current_bms) > 0 or min(lxp_batt_current_bms) < 0 or max(lxp_batt_current_bms))"
             " * max(lxp_v_bat)")
# Charging the inverters didn't do: a generator on a DC charger (Chargeverter), see genlog.py. The battery's own power
# (BMS) minus what the inverters charged/discharged, made robust against what isn't a charger (checked on 30 days of
# the ranch's data, no DC charger there: the plain difference gave 5 phantom 10-minute "charger runs", all while
# charging from solar, up to 5.4 kW; this gives none):
#   - both averaged over 5 minutes: the BMS current and the inverters' power are read at different moments, so a
#     changing load or cloud made them disagree for a reading or two;
#   - more than 20% of the battery power: the two meters differ by a few percent (conversion losses, calibration),
#     which at 8 kW of charging is hundreds of watts;
#   - not while the inverters see a generator themselves (GEN port or AC input with voltage, or in the last 15 min):
#     that one is counted by GEN_POWER, and its start/stop briefly skews the difference.
# Consumers still require more than 500 W (and the generator log 10 minutes of it).
_INV_NET = "(sum(lxp_p_charge) - sum(lxp_p_discharge))"
EXT_CHARGE = (f"((avg_over_time(({BMS_POWER})[5m:30s]) - avg_over_time(({_INV_NET})[5m:30s]))"
              f" > 0.2 * abs(avg_over_time(({_INV_NET})[5m:30s])))"
              " unless on () (max_over_time((max(lxp_v_gen) > 100 or max(lxp_v_ac_r) > 100)[15m:1m]) > 0)")
GEN_ENERGY_DAY = ("((sum(lxp_e_gen_day * (max_over_time(lxp_v_gen[1d]) > bool 50)) or vector(0))"
                  " + (sum(lxp_e_to_user_day) or vector(0)))")


def forget_dongle(stack, datalog):
    """Remove a dongle that is no longer found (a moved box, a replaced dongle, a test simulator): from inverters.json
    and the bridge config, and its retained MQTT messages, so Home Assistant drops its device. Returns messages;
    raises ValueError for an unknown dongle or one that is still there."""
    state_path = os.path.join(stack, "state", "inverters.json")
    inverters = load_json(state_path, {})
    if datalog not in inverters:
        raise ValueError(f"no dongle {datalog}")
    if inverters[datalog].get("present"):
        raise ValueError(f"{datalog} was found in the last scan; only dongles that are gone can be forgotten")
    del inverters[datalog]
    write_if_changed(state_path, json.dumps(inverters, indent=2) + "\n", 0o644)
    sec = ensure_secrets(stack)
    msgs = [f"{datalog} forgotten"]
    if write_bridge_config(stack, inverters, sec):
        r = subprocess.run(compose_cmd(stack) + ["restart", "bridge"], capture_output=True, text=True)
        msgs.append("bridge restarted" if r.returncode == 0 else f"bridge restart failed: {r.stderr.strip()[-200:]}")
    # its retained messages (Home Assistant discovery and last values): an empty retained message removes each
    mq = compose_cmd(stack) + ["exec", "-T", "mosquitto"]
    auth = ["-u", "homeassistant", "-P", sec["mqtt_homeassistant"]]
    r = subprocess.run(mq + ["mosquitto_sub", *auth, "-t", "#", "--retained-only", "-W", "4", "-F", "%t"],
                       capture_output=True, text=True, timeout=60)
    topics = sorted({t for t in r.stdout.splitlines() if datalog.lower() in t.lower()})
    for t in topics:
        subprocess.run(mq + ["mosquitto_pub", *auth, "-t", t, "-r", "-n"], capture_output=True, timeout=30)
    if topics:
        msgs.append(f"{len(topics)} Home Assistant entries removed")
    return msgs


def ensure_secrets(stack):
    path = os.path.join(stack, "state", "secrets.json")
    s = load_json(path, {})
    changed = False
    for key in ("mqtt_bridge", "mqtt_homeassistant", "grafana_admin"):
        if not s.get(key):
            s[key] = secrets.token_urlsafe(18)
            changed = True
    if changed:
        write_if_changed(path, json.dumps(s, indent=2) + "\n", 0o600)
    passwd = os.path.join(stack, "mosquitto", "passwd")
    if not os.path.exists(passwd) or changed:
        lines = [f"bridge:{mosquitto_hash(s['mqtt_bridge'])}", f"homeassistant:{mosquitto_hash(s['mqtt_homeassistant'])}"]
        if os.geteuid() == 0:
            # Mosquitto runs as uid/gid 1883 in its image and wants to own a 0600 file.
            write_if_changed(passwd, "\n".join(lines) + "\n", 0o600)
            os.chown(passwd, 1883, 1883)
        else:
            write_if_changed(passwd, "\n".join(lines) + "\n", 0o644)  # dev runs: can't chown, keep it readable
    return s


def host_timezone():
    try:
        return open("/etc/timezone").read().strip() or "UTC"
    except OSError:
        link = os.path.realpath("/etc/localtime")
        return link.split("zoneinfo/", 1)[1] if "zoneinfo/" in link else "UTC"


def env_profiles(stack):
    """Optional compose services that are turned on, from the modules: cloudflared (remote access), cloudflared-support
    (remote support), tts (voice announcements)."""
    import modules
    return modules.compose_profiles(stack)


def env_extra(stack):
    """Other generated .env settings, from the modules (the voice module: the Piper voice for the tts service)."""
    import modules
    return modules.env_lines(stack)


def load_env_text(sec, tz, profiles=(), extra=()):
    return (
        "# Generated by solar-setup\n"
        "GF_SECURITY_ADMIN_USER=admin\n"
        f"GF_SECURITY_ADMIN_PASSWORD={sec['grafana_admin']}\n"
        f"TZ={tz}\n"
        + (f"COMPOSE_PROFILES={','.join(profiles)}\n" if profiles else "")
        + "".join(line + "\n" for line in extra)
    )


def site_timezone(stack):
    tz = load_json(os.path.join(stack, "state", "site.json"), {}).get("time_zone")
    return tz or host_timezone()


def write_env(stack, sec):
    return write_if_changed(os.path.join(stack, ".env"), load_env_text(sec, site_timezone(stack), env_profiles(stack), env_extra(stack)), 0o600)


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent; picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def write_bridge_config(stack, inverters, sec):
    # JSON is valid YAML, so no YAML library is needed.
    cfg = {
        "loglevel": "info",
        "read_only": True,
        "inverters": [
            {
                "enabled": True,
                "host": inv["host"],
                "port": PORT,
                "datalog": dl,
                "serial": inv.get("serial") or "0000000000",
                "read_timeout": "300s",
                "short_read": "10s",
                "long_read": "30s",
                "hold_read": "15m",
                "health_check_duration": "5m",
            }
            for dl, inv in sorted(inverters.items())
        ],
        "mqtt": {
            "enabled": True,
            "host": "mosquitto",
            "port": 1883,
            "username": "bridge",
            "password": sec["mqtt_bridge"],
            "namespace": "lxp",
            "homeassistant": {"enabled": True, "prefix": "homeassistant"},
        },
        "statsd": {"server": ""},
        "prometheus": {"enabled": True},
    }
    header = "# Generated by solar-setup from the discovered dongles; edits are overwritten.\n"
    return write_if_changed(os.path.join(stack, "bridge", "config.yml"), header + json.dumps(cfg, indent=2) + "\n", 0o644)


def seed_homeassistant(stack, sec):
    """First run only: copy the seed config and pre-create the MQTT integration."""
    ha = os.path.join(stack, "homeassistant")
    entries = os.path.join(ha, ".storage", "core.config_entries")
    if os.path.exists(entries):
        return False
    seed = os.path.join(stack, "homeassistant-seed")
    if os.path.isdir(seed):
        shutil.copytree(seed, ha, dirs_exist_ok=True)
    os.makedirs(os.path.dirname(entries), exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    mqtt_entry = {
        "created_at": now,
        "data": {"broker": "127.0.0.1", "port": 1883, "username": "homeassistant",
                 "password": sec["mqtt_homeassistant"], "protocol": "5"},
        "disabled_by": None,
        "discovery_keys": {},
        "domain": "mqtt",
        "entry_id": secrets.token_hex(16),
        "minor_version": 1,
        "modified_at": now,
        "options": {"discovery": True, "discovery_prefix": "homeassistant",
                    "birth_message": {"topic": "homeassistant/status", "payload": "online", "qos": 0, "retain": False},
                    "will_message": {"topic": "homeassistant/status", "payload": "offline", "qos": 0, "retain": False}},
        "pref_disable_new_entities": False,
        "pref_disable_polling": False,
        "source": "user",
        "subentries": [],
        "title": "Solar monitor broker",
        "unique_id": None,
        "version": 2,
    }
    doc = {"version": 1, "minor_version": 5, "key": "core.config_entries", "data": {"entries": [mqtt_entry]}}
    write_if_changed(entries, json.dumps(doc, indent=2) + "\n", 0o600)
    log("Home Assistant seeded (MQTT integration pre-configured)")
    return True


def secret(stack, key, make):
    """A value kept in state/secrets.json, made with make() the first time."""
    path = os.path.join(stack, "state", "secrets.json")
    s = load_json(path, {})
    if not s.get(key):
        s[key] = make()
        write_if_changed(path, json.dumps(s, indent=2) + "\n", 0o600)
    return s[key]


def ha_webhook_id(stack):
    """This box's secret webhook id for the setup page's "send a test notification" (kept in state/secrets.json)."""
    return secret(stack, "ha_webhook", lambda: "solar-" + secrets.token_urlsafe(24))


def notify_package(stack, port=80):
    """The parts of packages/site.yaml the notifications go through: script.solar_notify (packages/solar_monitor.yaml)
    hands each alert to this box (rest_command.solar_notify_box, with its token), and the box delivers to the chosen
    phones through a webhook automation (local network only; the id is this box's secret)."""
    import notify
    js = json.dumps
    # the token goes in a header: Home Assistant logs a rest_command's payload when the call fails
    payload = ("tag={{ tag | urlencode }}&title={{ title | urlencode }}"
               "&message={{ message | urlencode }}&priority={{ priority | default('') | urlencode }}")
    rest = [
        "rest_command:",
        "  solar_notify_box:",
        f"    url: \"http://127.0.0.1:{int(port)}/notify/send\"",
        "    method: POST",
        "    headers:",
        f"      Authorization: {js('Bearer ' + notify.box_token(stack))}",
        "    content_type: application/x-www-form-urlencoded",
        f"    payload: {js(payload)}",
        "    timeout: 5",
    ]
    deliver = [
        "  - id: solar_notify_deliver",
        "    alias: \"Solar: deliver a notification to the chosen phones\"",
        "    mode: queued",
        "    triggers:",
        "      - trigger: webhook",
        f"        webhook_id: {js(notify.ha_webhook(stack))}",
        "        allowed_methods: [POST]",
        "        local_only: true",
        "    actions:",
        # only phones the app still has (a missing notify action would stop the loop, error or not)
        "      - repeat:",
        "          for_each: >-",
        "            {{ trigger.json.phones | select('in', integration_entities('mobile_app')",
        "               | select('match', 'device_tracker\\.') | map('replace', 'device_tracker.', '') | list) | list }}",
        "          sequence:",
        "            - action: notify.mobile_app_{{ repeat.item }}",
        "              continue_on_error: true",
        "              data:",
        "                title: \"{{ trigger.json.title }}\"",
        "                message: \"{{ trigger.json.message }}\"",
        "                data: \"{{ trigger.json.data }}\"",
        # alerts from the box's own modules (propane, boiler): through script.solar_notify like Home Assistant's
        "  - id: solar_notify_from_box",
        "    alias: \"Solar: alert from the box\"",
        "    mode: queued",
        "    triggers:",
        "      - trigger: webhook",
        f"        webhook_id: {js(notify.box_alert_webhook(stack))}",
        "        allowed_methods: [POST]",
        "        local_only: true",
        "    actions:",
        "      - action: script.solar_notify",
        "        data:",
        "          tag: \"{{ trigger.json.tag }}\"",
        "          title: \"{{ trigger.json.title }}\"",
        "          message: \"{{ trigger.json.message }}\"",
        "          priority: \"{{ trigger.json.priority }}\"",
    ]
    return rest, deliver


def site_yaml(stack, site=None):
    """packages/site.yaml for this box as it is set up now."""
    import hastates
    site = site if site is not None else load_json(os.path.join(stack, "state", "site.json"), {})
    return site_package(site, ha_webhook_id(stack), notify_package(stack), hastates.package(hastates.entities(stack)))


def site_package(site, webhook_id=None, notify=None, statestream=None):
    """packages/site.yaml: the site's sizes, the daily summary settings, the test notification, the notification
    hand-off (notify = notify_package(stack)) and the entities Home Assistant publishes to the box (statestream =
    hastates.package(...): its top-level lines and its automations)."""
    js = json.dumps
    on = site.get("summary_enabled", True)
    at = site.get("summary_time") or "00:00"
    pkg = [
        "# Written by the solar monitor (setup page settings); edits are overwritten.",
        "input_boolean:",
        "  solar_daily_summary:",
        "    name: Solar daily summary",
        "    icon: mdi:message-text-clock",
        f"    initial: {'true' if on else 'false'}",
        "input_datetime:",
        "  solar_summary_time:",
        "    name: Solar daily summary time",
        "    has_date: false",
        "    has_time: true",
        f"    initial: {js(at + ':00')}",
    ]
    stream_top, stream_auto = statestream or ([], [])
    pkg += stream_top
    if notify:
        pkg += notify[0]
    if webhook_id or notify or stream_auto:
        pkg.append("automation:")
    if notify:
        pkg += notify[1]
    pkg += stream_auto
    if webhook_id:
        # the setup page's "Send a test notification" (local network only; the id is this box's secret)
        pkg += [
            "  - id: solar_test_notification",
            "    alias: \"Solar: test notification\"",
            "    triggers:",
            "      - trigger: webhook",
            f"        webhook_id: {js(webhook_id)}",
            "        allowed_methods: [POST]",
            "        local_only: true",
            "    actions:",
            "      - action: script.solar_notify",
            "        data:",
            "          tag: test",
            "          title: Test from the solar monitor",
            "          message: \"If you can read this, alerts from the solar monitor reach this phone.\"",
        ]
    if not site.get("battery_count"):
        return "\n".join(pkg) + "\n"
    battery_kwh = round(int(site["battery_count"]) * float(site["battery_kwh_each"]), 2)
    arrays_kwp = [round(int(a["panels"]) * int(a["panel_watts"]) / 1000, 3) for a in site["arrays"]]
    pkg += [
        "template:",
        "  - sensor:",
        "      - name: Site battery capacity",
        "        unique_id: site_battery_capacity",
        "        unit_of_measurement: kWh",
        "        icon: mdi:battery",
        f"        state: \"{battery_kwh}\"",
        "        attributes:",
        f"          batteries: \"{site['battery_count']}\"",
        f"          kwh_each: \"{site['battery_kwh_each']}\"",
        "      - name: Site solar array size",
        "        unique_id: site_solar_array_size",
        "        unit_of_measurement: kW",
        "        icon: mdi:solar-panel-large",
        f"        state: \"{round(sum(arrays_kwp), 3)}\"",
        "        attributes:",
        f"          arrays: {js(js([dict(a, kwp=k) for a, k in zip(site['arrays'], arrays_kwp)]))}",
        "      - name: Site generator mode",
        "        unique_id: site_generator_mode",
        "        icon: mdi:engine",
        f"        state: {js(site.get('generator', 'manual'))}",
        "        attributes:",
        f"          kw: \"{site.get('generator_kw', '')}\"",
        "      - name: Site low battery alert level",
        "        unique_id: site_low_battery_alert_level",
        "        unit_of_measurement: \"%\"",
        f"        state: \"{site.get('low_soc_alert', 25)}\"",
    ]
    return "\n".join(pkg) + "\n"


# Files the image owns inside Home Assistant's config: replaced on every run (so updates
# reach them). Everything else under homeassistant/ is only seeded on the first run.
HA_OWNED = ["packages/solar_monitor.yaml", "dashboards/solar.yaml"]


def sync_homeassistant(stack):
    """Copy the image-owned HA files in; True if any changed."""
    site = load_json(os.path.join(stack, "state", "site.json"), {})
    changed = write_if_changed(os.path.join(stack, "homeassistant", "packages", "site.yaml"), site_yaml(stack, site), 0o644)
    for rel in HA_OWNED:
        src = os.path.join(stack, "homeassistant-seed", rel)
        if os.path.exists(src):
            with open(src) as f:
                changed |= write_if_changed(os.path.join(stack, "homeassistant", rel), f.read(), 0o644)
    return changed


def seed_energy(stack):
    """Fill in HA's Energy dashboard with the site sensors, once (later edits in HA are kept)."""
    path = os.path.join(stack, "homeassistant", ".storage", "energy")
    if os.path.exists(path):
        return False
    prefs = {
        "energy_sources": [
            {"type": "solar", "stat_energy_from": "sensor.site_solar_today", "config_entry_solar_forecast": None},
            {"type": "battery", "stat_energy_from": "sensor.site_battery_discharged_today",
             "stat_energy_to": "sensor.site_battery_charged_today"},
        ],
        "device_consumption": [],
    }
    doc = {"version": 1, "minor_version": 1, "key": "energy", "data": prefs}
    write_if_changed(path, json.dumps(doc, indent=2) + "\n", 0o600)
    log("Home Assistant energy dashboard seeded")
    return True


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stack", default="/opt/solar-monitor/stack")
    ap.add_argument("--host", action="append", default=[], help="extra address to check (repeatable)")
    ap.add_argument("--no-scan", action="store_true", help="skip the LAN sweep, only check known and --host addresses")
    ap.add_argument("--apply", action="store_true", help="restart the bridge if its config changed")
    args = ap.parse_args()

    stack = args.stack
    state_path = os.path.join(stack, "state", "inverters.json")
    inverters = load_json(state_path, {})
    sec = ensure_secrets(stack)
    import modules  # each module's settings file, moved over from the old places the first time
    modules.migrate_all(stack)
    write_env(stack, sec)
    first = seed_homeassistant(stack, sec)
    ha_changed = sync_homeassistant(stack)
    ha_changed |= seed_energy(stack)
    if ha_changed and not first:
        # running HA only reads packages and YAML dashboards at start; harmless if it isn't up yet
        r = subprocess.run(compose_cmd(stack) + ["restart", "homeassistant"], capture_output=True, text=True)
        log("Home Assistant restarted for the solar monitor package" if r.returncode == 0
            else f"Home Assistant restart skipped: {r.stderr.strip()[-200:]}")
    if not os.path.exists(os.path.join(stack, "go2rtc", "go2rtc.yaml")):
        import display  # the camera service's config (no cameras until the setup page adds them)
        display.save(stack, display.load(stack))

    known_hosts = [inv["host"] for inv in inverters.values()]
    nets = [] if args.no_scan else local_networks()
    found = scan(nets, args.host + known_hosts)
    now = time.strftime("%Y-%m-%d %H:%M")
    for inv in inverters.values():
        inv["present"] = False
    for r in found:
        cur = inverters.setdefault(r["datalog"], {})
        if cur.get("host") and cur["host"] != r["host"]:
            log(f"dongle {r['datalog']} moved {cur['host']} -> {r['host']}")
        cur.update(host=r["host"], last_seen=now, present=True)
        if r["serial"]:
            cur["serial"] = r["serial"]
    inverters_meta = {"last_scan": now}
    write_if_changed(state_path, json.dumps(inverters, indent=2) + "\n", 0o644)
    write_if_changed(os.path.join(stack, "state", "scan.json"), json.dumps(inverters_meta) + "\n", 0o644)

    changed = write_bridge_config(stack, inverters, sec)
    log(f"{len(inverters)} dongle(s) configured; bridge config {'changed' if changed else 'unchanged'}")
    if changed and args.apply:
        r = subprocess.run(compose_cmd(stack) + ["restart", "bridge"], capture_output=True, text=True)
        log("bridge restarted" if r.returncode == 0 else f"bridge restart failed: {r.stderr.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
