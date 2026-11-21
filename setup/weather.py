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

Readings are {"solar_wm2": float|None, "temp_c": float|None} plus, where the station has them (all metric):
humidity (%), dew_c, feels_c, wind_ms, gust_ms, wind_dir (degrees), rain_day_mm, rain_rate_mmh, pressure_hpa
(sea level / relative), uv. The wall display's weather view uses them (wx.py).
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


SPEED = {"mph": 0.44704, "km/h": 1 / 3.6, "kmh": 1 / 3.6, "m/s": 1.0, "knots": 0.514444, "kn": 0.514444, "kt": 0.514444}
LENGTH = {"in": 25.4, "mm": 1.0, "in/hr": 25.4, "in/h": 25.4, "mm/hr": 1.0, "mm/h": 1.0}
PRESSURE = {"inhg": 33.8639, "hpa": 1.0, "mbar": 1.0, "mmhg": 1.33322, "kpa": 10.0}


def _unit(val, unit=""):
    """'5.4 mph' -> (5.4, 'mph'); the unit from the value text, else the given one."""
    parts = str(val).split()
    return _num(val), (parts[1] if len(parts) > 1 else unit or "").lower()


def _convert(val, table, default, unit=""):
    v, u = _unit(val, unit)
    if v is None:
        return None
    return round(v * table.get(u, table[default]), 2)


def _temp(val, unit="C"):
    v, u = _unit(val, unit)
    if v is None:
        return None
    return round(f_to_c(v), 2) if u.upper().startswith("F") else v


def ecowitt_fields(d):
    """All we use from an Ecowitt /get_livedata_info answer."""
    common = {i.get("id"): i for i in d.get("common_list", [])}
    rain = {i.get("id"): i for i in (d.get("piezoRain") or []) + (d.get("rain") or [])}   # tipping bucket wins
    out = {}
    if "0x15" in common:
        out["solar_wm2"] = _num(common["0x15"].get("val"))
    for key, i in (("temp_c", "0x02"), ("dew_c", "0x03"), ("feels_c", "3")):
        if i in common:
            out[key] = _temp(common[i].get("val"), common[i].get("unit", "C"))
    if "0x07" in common:
        out["humidity"] = _num(common["0x07"].get("val"))
    for key, i in (("wind_ms", "0x0B"), ("gust_ms", "0x0C")):
        if i in common:
            out[key] = _convert(common[i].get("val"), SPEED, "mph", common[i].get("unit", ""))
    if "0x0A" in common:
        out["wind_dir"] = _num(common["0x0A"].get("val"))
    if "0x17" in common:
        out["uv"] = _num(common["0x17"].get("val"))
    if "0x10" in rain:
        out["rain_day_mm"] = _convert(rain["0x10"].get("val"), LENGTH, "in", rain["0x10"].get("unit", ""))
    if "0x0E" in rain:
        out["rain_rate_mmh"] = _convert(rain["0x0E"].get("val"), LENGTH, "in", rain["0x0E"].get("unit", ""))
    for w in d.get("wh25") or []:
        if w.get("rel"):
            out["pressure_hpa"] = _convert(w["rel"], PRESSURE, "inhg", w.get("unit", ""))
    return out


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
    return {"solar_wm2": None, "temp_c": None, **ecowitt_fields(_http_json(f"http://{host}/get_livedata_info"))}


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
                              "reading": tempest_fields(o)}
            if want_host:
                break
    s.close()
    return list(found.values())


def tempest_fields(o):
    """obs_st: 1 lull, 2 wind avg, 3 gust, 4 direction (m/s, degrees), 6 station pressure (hPa), 7 temp (C),
    8 humidity, 10 UV, 11 solar W/m2, 12 rain in the last minute (mm)."""
    g = lambda i: o[i] if len(o) > i else None  # noqa: E731
    return {"solar_wm2": g(11), "temp_c": g(7), "humidity": g(8), "wind_ms": g(2), "gust_ms": g(3), "wind_dir": g(4),
            "uv": g(10), "rain_rate_mmh": None if g(12) is None else round(g(12) * 60, 2)}


def tempest_read(host):
    r = tempest_listen(timeout=70, want_host=host)
    return r[0]["reading"] if r else {"solar_wm2": None, "temp_c": None}


# --------------------------------------------------------------------------
# Davis WeatherLink Live
# --------------------------------------------------------------------------
def weatherlink_read(host):
    """Imperial units (F, mph, rain in counts of rain_size: 1 = 0.01 in, 2 = 0.2 mm, 3 = 0.1 mm), as the API sends."""
    d = _http_json(f"http://{host}/v1/current_conditions")
    out = {"solar_wm2": None, "temp_c": None}
    for c in (d.get("data") or {}).get("conditions", []):
        if c.get("solar_rad") is not None:
            out["solar_wm2"] = float(c["solar_rad"])
        if c.get("data_structure_type") == 1 and c.get("temp") is not None and out["temp_c"] is None:
            out["temp_c"] = f_to_c(float(c["temp"]))
            size = {1: 0.254, 2: 0.2, 3: 0.1}.get(c.get("rain_size"), 0.254)
            for key, k, fn in (("humidity", "hum", float), ("dew_c", "dew_point", f_to_c), ("feels_c", "thw_index", f_to_c),
                               ("wind_ms", "wind_speed_avg_last_1_min", lambda v: v * 0.44704),
                               ("gust_ms", "wind_speed_hi_last_10_min", lambda v: v * 0.44704),
                               ("wind_dir", "wind_dir_last", float), ("uv", "uv_index", float),
                               ("rain_day_mm", "rainfall_daily", lambda v: v * size),
                               ("rain_rate_mmh", "rain_rate_last", lambda v: v * size)):
                if c.get(k) is not None:
                    out[key] = round(fn(float(c[k])), 2)
        if c.get("data_structure_type") == 3 and c.get("bar_sea_level") is not None:
            out["pressure_hpa"] = round(float(c["bar_sea_level"]) * 33.8639, 1)
    return out


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


def push_fields(q):
    """Ecowitt / Ambient custom-server upload fields (imperial) -> a reading."""
    f = lambda k: _num(q.get(k)) if q.get(k) not in (None, "") else None  # noqa: E731
    mph = lambda v: None if v is None else round(v * 0.44704, 2)  # noqa: E731
    inch = lambda v: None if v is None else round(v * 25.4, 2)  # noqa: E731
    return {k: v for k, v in {
        "solar_wm2": f("solarradiation"), "temp_c": f_to_c(f("tempf")), "humidity": f("humidity"),
        "dew_c": f_to_c(f("dewptf")), "feels_c": f_to_c(f("feelslikef")), "wind_ms": mph(f("windspeedmph")),
        "gust_ms": mph(f("windgustmph")), "wind_dir": f("winddir"), "uv": f("uv"),
        "rain_day_mm": inch(f("dailyrainin") if q.get("dailyrainin") not in (None, "") else f("drain_piezo")),
        "rain_rate_mmh": inch(f("rainratein") if q.get("rainratein") not in (None, "") else f("rrain_piezo")),
        "pressure_hpa": None if f("baromrelin") is None else round(f("baromrelin") * 33.8639, 1)}.items() if v is not None}
