#!/usr/bin/env python3
"""Simulated Ecowitt gateway (GW1100/GW2000 style), for testing the solar monitor's weather station support.

  UDP 46000   answers the CMD_BROADCAST discovery packet with its MAC, IP, port and name
  HTTP 80     /get_livedata_info with outdoor temperature (0x02, °F) and solar radiation (0x15, W/m2)

Sunlight follows a clear-sky curve for the latitude with drifting clouds; temperature follows the day.
?clouds=0.3 (0..1) on the HTTP port sets the cloud cover, / shows the current values.

  python3 ecowittsim.py --ip 192.168.12.133 --lat 39.7 --lon -105
"""
import argparse
import datetime as dt
import json
import math
import os
import random
import socket
import struct
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

state = {"clouds": 0.2}


def sun(now, lat, lon):
    t = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    decl = math.radians(23.44) * math.sin(2 * math.pi * (284 + t.timetuple().tm_yday) / 365)
    ha = math.radians(15 * (((t.hour + t.minute / 60 + lon / 15) % 24) - 12))
    la = math.radians(lat)
    el = math.asin(math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha))
    return max(0.0, math.sin(el))


def reading(a):
    now = time.time()
    s = sun(now, a.lat, a.lon)
    ghi = 1000 * s ** 1.15 * (1 - 0.75 * state["clouds"] * random.uniform(0.7, 1.0))
    temp_f = 38 + 22 * s + random.uniform(-0.5, 0.5)
    return round(ghi, 1), round(temp_f, 1)


def discovery_reply(a):
    mac = bytes.fromhex(a.mac.replace(":", ""))
    name = a.name.encode()
    body = mac + socket.inet_aton(a.ip) + struct.pack(">H", 45000) + bytes([len(name)]) + name
    pkt = bytes([0x12]) + struct.pack(">H", len(body) + 4) + body
    return b"\xff\xff" + pkt + bytes([sum(pkt) & 0xFF])


def udp(a):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", 46000))
    while True:
        data, addr = s.recvfrom(1024)
        if data[:3] == b"\xff\xff\x12":
            s.sendto(discovery_reply(a), addr)
            print(f"{time.strftime('%H:%M:%S')} discovery from {addr[0]}", flush=True)


def http(a):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            if "clouds" in q:
                state["clouds"] = max(0.0, min(1.0, float(q["clouds"])))
            ghi, tf = reading(a)
            if u.path == "/get_livedata_info":
                body = json.dumps({"common_list": [{"id": "0x02", "val": f"{tf}", "unit": "F"},
                                                   {"id": "0x07", "val": "45%"},
                                                   {"id": "0x15", "val": f"{ghi} W/m2"},
                                                   {"id": "0x17", "val": "2"}],
                                   "wh25": [{"intemp": "70.1", "unit": "F", "inhumi": "38%"}]})
                ctype = "application/json"
            else:
                body = (f"<!doctype html><title>ecowittsim</title><body style='font:16px system-ui;padding:12px'>"
                        f"<h2>Simulated Ecowitt gateway {a.name}</h2><p>Sun {ghi} W/m², {tf} °F, clouds {state['clouds']}</p>"
                        "<p>Clouds: " + " ".join(f"<a href='/?clouds={c}'>{c}</a>" for c in (0, 0.3, 0.6, 1)) + "</p>")
                ctype = "text/html; charset=utf-8"
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *x):
            pass
    ThreadingHTTPServer(("0.0.0.0", a.http_port), H).serve_forever()


def main():
    e = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", default=e("IP") or socket.gethostbyname(socket.gethostname()), help="the address it announces")
    ap.add_argument("--mac", default=e("MAC", "48:3f:da:00:00:01"))
    ap.add_argument("--name", default=e("NAME", "GW1100A-WIFI0001"))
    ap.add_argument("--http-port", type=int, default=int(e("HTTP_PORT", "80")))
    ap.add_argument("--lat", type=float, default=float(e("LAT", "39.7")))
    ap.add_argument("--lon", type=float, default=float(e("LON", "-105.0")))
    a = ap.parse_args()
    threading.Thread(target=udp, args=(a,), daemon=True).start()
    print(f"ecowittsim {a.name} at {a.ip}: discovery on udp 46000, http on :{a.http_port}", flush=True)
    http(a)


if __name__ == "__main__":
    main()
