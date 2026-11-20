#!/usr/bin/env python3
"""Local weather station support: find a station on the LAN and read solar radiation.

Supported (all read-only):
  * Ecowitt gateways and consoles (GW1000/GW1100/GW2000/WS View): found by the
    UDP discovery broadcast on port 46000; read with the local HTTP API
    (/get_livedata_info, field 0x15 = solar radiation W/m2, 0x02 = outdoor temp).
  * WeatherFlow Tempest: hub broadcasts obs_st every minute on UDP 50222.
  * Davis WeatherLink Live: HTTP /v1/current_conditions (solar_rad), by IP.
  * Stations that can only push (Ambient Weather, or Ecowitt "customized
    upload"): the start page accepts the Ecowitt / Ambient custom-server
    formats at /weather/ecowitt and /weather/ambient (see solar_web.py).

Readings are {"solar_wm2": float|None, "temp_c": float|None}.
"""
import ipaddress
import json
import socket
import struct
import time
import urllib.request

ECOWITT_DISCOVERY_PORT = 46000
TEMPEST_PORT = 50222


def _http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _num(s):
    try:
        return float(str(s).split()[0].rstrip("%"))
    except (ValueError, IndexError):
        return None


def f_to_c(f):
    return None if f is None else (f - 32) / 1.8


# --------------------------------------------------------------------------
# Ecowitt
# --------------------------------------------------------------------------
def ecowitt_discovery_packet():
    # CMD_BROADCAST (0x12) with a 2-byte size field: header, cmd, size, checksum
    body = bytes([0x12, 0x00, 0x04])
    return b"\xff\xff" + body + bytes([sum(body) & 0xFF])


def parse_ecowitt_discovery(data):
    """-> {"mac", "host", "port", "name"} from a CMD_BROADCAST reply."""
    if len(data) < 16 or data[:3] != b"\xff\xff\x12":
        return None
    p = data[5:]  # after header (2), cmd (1), size (2)
    mac = ":".join(f"{b:02x}" for b in p[0:6])
    host = str(ipaddress.IPv4Address(p[6:10]))
    port = struct.unpack(">H", p[10:12])[0]
    n = p[12]
    name = p[13:13 + n].decode("ascii", "replace")
    return {"type": "ecowitt", "mac": mac, "host": host, "port": port, "name": name}


def ecowitt_read(host):
    d = _http_json(f"http://{host}/get_livedata_info")
    vals = {i.get("id"): i for i in d.get("common_list", [])}
    solar = _num(vals.get("0x15", {}).get("val")) if "0x15" in vals else None
    t = vals.get("0x02")
    temp = None
    if t:
        v = _num(t.get("val"))
        temp = f_to_c(v) if t.get("unit", "C").upper().startswith("F") else v
    return {"solar_wm2": solar, "temp_c": temp}


def scan_ecowitt(timeout=4.0, targets=("255.255.255.255",)):
    found = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.5)
    pkt = ecowitt_discovery_packet()
    for t in targets:
        try:
            s.sendto(pkt, (t, ECOWITT_DISCOVERY_PORT))
        except OSError:
            pass
    end = time.time() + timeout
    while time.time() < end:
        try:
            data, addr = s.recvfrom(1024)
        except socket.timeout:
            continue
        r = parse_ecowitt_discovery(data)
        if r:
            found[r["mac"]] = r
    s.close()
    return list(found.values())


# --------------------------------------------------------------------------
# WeatherFlow Tempest (UDP broadcast, obs_st: index 11 = solar W/m2, 7 = air temp C)
# --------------------------------------------------------------------------
def tempest_listen(timeout=65.0, want_host=None):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", TEMPEST_PORT))
    except OSError:
        return []
    s.settimeout(1.0)
    found = {}
    end = time.time() + timeout
    while time.time() < end:
        try:
            data, addr = s.recvfrom(4096)
        except socket.timeout:
            continue
        try:
            m = json.loads(data)
        except ValueError:
            continue
        if want_host and addr[0] != want_host:
            continue
        if m.get("type") == "obs_st" and m.get("obs"):
            o = m["obs"][0]
            found[addr[0]] = {"type": "tempest", "host": addr[0], "name": m.get("serial_number", "Tempest"),
                              "reading": {"solar_wm2": o[11], "temp_c": o[7]}}
            if want_host:
                break
    s.close()
    return list(found.values())


def tempest_read(host):
    r = tempest_listen(timeout=70, want_host=host)
    return r[0]["reading"] if r else {"solar_wm2": None, "temp_c": None}


# --------------------------------------------------------------------------
# Davis WeatherLink Live
# --------------------------------------------------------------------------
def weatherlink_read(host):
    d = _http_json(f"http://{host}/v1/current_conditions")
    solar = temp = None
    for c in (d.get("data") or {}).get("conditions", []):
        if c.get("solar_rad") is not None:
            solar = float(c["solar_rad"])
        if c.get("temp") is not None and temp is None and c.get("data_structure_type") == 1:
            temp = f_to_c(float(c["temp"]))
    return {"solar_wm2": solar, "temp_c": temp}


# --------------------------------------------------------------------------
READERS = {"ecowitt": ecowitt_read, "tempest": tempest_read, "weatherlink": weatherlink_read}


def probe(host):
    """Identify the station at a given IP. -> {"type", "host", "name", "reading"} or None."""
    for kind, fn in (("ecowitt", ecowitt_read), ("weatherlink", weatherlink_read)):
        try:
            r = fn(host)
            if r["solar_wm2"] is not None or r["temp_c"] is not None:
                return {"type": kind, "host": host, "name": kind, "reading": r}
        except Exception:  # noqa: BLE001 - try the next kind
            pass
    found = tempest_listen(timeout=65, want_host=host)
    return found[0] if found else None


def scan(networks=()):
    """Find stations on the LAN: Ecowitt discovery (broadcast + each network's
    broadcast address) and Tempest broadcasts. Each result includes a reading."""
    targets = ["255.255.255.255"] + [str(n.broadcast_address) for n in networks]
    out = []
    for r in scan_ecowitt(targets=targets):
        try:
            r["reading"] = ecowitt_read(r["host"])
        except Exception as e:  # noqa: BLE001
            r["reading"] = {"error": str(e)}
        out.append(r)
    out += tempest_listen(timeout=65)
    return out


def read(station):
    fn = READERS.get(station.get("type"))
    return fn(station["host"]) if fn else {"solar_wm2": None, "temp_c": None}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(json.dumps(probe(sys.argv[1]), indent=2))
    else:
        print(json.dumps(scan(), indent=2))
