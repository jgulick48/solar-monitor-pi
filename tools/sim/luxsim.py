#!/usr/bin/env python3
"""Simulated LuxPower/EG4 WiFi dongle + inverter, for testing the solar monitor without real hardware.

Speaks the dongle's TCP protocol on port 8000 like a real one: heartbeats (0xC1) every 15 s carrying the
datalog serial, and translated (0xC2) replies to read-input (4) and read-holding (3) requests with the
inverter serial, so the box's discovery (solar_setup.identify) and lxp-bridge-go both work unchanged.
Writes (6, 0x10) are acknowledged and ignored (the box runs the bridge read-only anyway).

The inverter behind it is a small simulation, stepped every 5 seconds:
  sun       clear-sky curve for the latitude and date, with drifting clouds
  house     a base load, an evening peak and random appliance spikes
  battery   SoC from the power balance (solar is cut back when it's full), cell voltages from SoC
  generator scenario "generator": on the inverter's GEN input; "chargeverter": a DC charger the
            inverter doesn't see (only the BMS current shows it)
  counters  today's and lifetime energies, reset at local midnight

Scenarios (control page http://<ip>:8080/, or ?scenario=NAME):
  normal, generator, chargeverter, low-battery, fault, bms-fault, imbalance, cold, full, offline
  "offline" stops answering on port 8000 (like a dongle that dropped off Wi-Fi).

  python3 luxsim.py --datalog BA00000001 --serial 0000000001 --pv-kw 8 --battery-kwh 15.36 --lat 39.7 --lon -105
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

SCENARIOS = ["normal", "generator", "chargeverter", "low-battery", "fault", "bms-fault", "imbalance", "cold", "full", "offline"]
STEP = 5.0


def crc16_modbus(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def frame(func, datalog, payload=b"", proto=2):
    body = bytes([1, func]) + datalog + payload
    return bytes([0xA1, 0x1A, proto, 0]) + struct.pack("<H", len(body)) + body


def heartbeat(datalog):
    return frame(0xC1, datalog, b"\x00")


def reply(datalog, serial, dev_func, start, values):
    """Translated reply: [len][addr 1][func][serial 10][start u16][byte count][values u16 LE...][crc]."""
    data = bytes([1, dev_func]) + serial + struct.pack("<H", start) + bytes([2 * len(values)])
    data += b"".join(struct.pack("<H", v & 0xFFFF) for v in values)
    data += struct.pack("<H", crc16_modbus(data))
    return frame(0xC2, datalog, struct.pack("<H", len(data)) + data)


def parse_request(buf):
    """Yield (consumed_until, dev_func, start, count) for complete translated requests in buf."""
    i = 0
    while i + 6 <= len(buf):
        if buf[i:i + 2] != b"\xa1\x1a":
            i += 1
            continue
        length = struct.unpack("<H", buf[i + 4:i + 6])[0]
        if i + 6 + length > len(buf):
            return
        f = buf[i:i + 6 + length]
        i += 6 + length
        if len(f) >= 38 and f[7] == 0xC2:
            data = f[18:]
            dev_func = data[3]
            start = struct.unpack("<H", data[14:16])[0]
            count = struct.unpack("<H", data[16:18])[0]
            yield i, dev_func, start, count
        else:
            yield i, None, 0, 0


class Inverter:
    def __init__(self, a):
        self.a = a
        self.lock = threading.Lock()
        self.scenario = a.scenario
        self.cap_wh = a.battery_kwh * 1000
        self.soc = a.soc
        self.cloud = 1.0
        self.appliance, self.appliance_until = 0.0, 0.0
        self.pv = self.load = self.gen = self.ext = self.charge = self.discharge = 0.0
        self.day = None
        self.e = {k: 0.0 for k in ("pv", "chg", "dis", "load", "gen")}          # Wh today
        self.e_all = {"pv": 3.2e6, "chg": 1.9e6, "dis": 1.7e6, "load": 4.1e6, "gen": 0.4e6}   # Wh lifetime
        self.runtime = 3600 * 24 * 400
        self.cycles = 312
        self.tz = dt.timezone(dt.timedelta(hours=a.utc_offset))

    # ---------------------------------------------------------------- physics
    def sun(self, now):
        """Clear-sky fraction of panel output (0..1) at this moment."""
        t = dt.datetime.fromtimestamp(now, dt.timezone.utc)
        doy = t.timetuple().tm_yday
        decl = math.radians(23.44) * math.sin(2 * math.pi * (284 + doy) / 365)
        solar_hour = (t.hour + t.minute / 60 + self.a.lon / 15) % 24
        ha = math.radians(15 * (solar_hour - 12))
        lat = math.radians(self.a.lat)
        el = math.asin(math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha))
        return max(0.0, math.sin(el)) ** 1.2

    def step(self, now):
        with self.lock:
            local = dt.datetime.fromtimestamp(now, self.tz)
            if self.day != local.date():
                self.day = local.date()
                self.e = {k: 0.0 for k in self.e}
            sc = self.scenario
            if sc == "low-battery" and self.soc > 18:
                self.soc = 18.0
            if sc == "full":
                self.soc = min(100.0, self.soc + 2)
            self.cloud = min(1.0, max(0.35, self.cloud + random.uniform(-0.05, 0.05)))
            pv_avail = self.a.pv_kw * 1000 * self.sun(now) * self.cloud
            h = local.hour + local.minute / 60
            base = self.a.load_w * (1.6 if 17 <= h < 22 else 1.0)
            # appliances run for minutes at a time (a kettle, the well pump, a dryer), a few times an hour
            if now >= self.appliance_until:
                self.appliance = 0.0
                if random.random() < STEP / 900:
                    self.appliance = random.choice([800, 1500, 2500])
                    self.appliance_until = now + random.uniform(120, 600)
            self.load = base * random.uniform(0.95, 1.05) + self.appliance
            self.gen = 4500.0 if sc == "generator" else 0.0
            self.ext = 2800.0 if sc == "chargeverter" else 0.0
            # solar first, then the generator, then the battery covers the rest
            room_wh = (100 - self.soc) / 100 * self.cap_wh
            max_charge = min(self.a.max_charge_w, room_wh * 3600 / STEP)
            surplus = pv_avail + self.gen - self.load
            if surplus >= 0:
                self.charge, self.discharge = min(surplus, max_charge), 0.0
                self.pv = min(pv_avail, self.load + self.charge - self.gen) if self.soc >= 99.5 else pv_avail
                self.pv = max(self.pv, 0.0)
            else:
                self.pv, self.charge = pv_avail, 0.0
                self.discharge = min(-surplus, self.a.max_charge_w) if self.soc > 5 else 0.0
            net = self.charge - self.discharge + self.ext * 0.95
            self.soc = min(100.0, max(1.0, self.soc + net * STEP / 3600 / self.cap_wh * 100))
            for k, w in (("pv", self.pv), ("chg", self.charge), ("dis", self.discharge), ("load", self.load), ("gen", self.gen)):
                self.e[k] += w * STEP / 3600
                self.e_all[k] += w * STEP / 3600
            self.runtime += STEP

    # ---------------------------------------------------------------- registers
    def cells(self):
        s = self.soc
        v = 3.10 + 0.0022 * s if s < 90 else 3.30 + (s - 90) * 0.014        # LiFePO4-ish curve
        spread = 0.008 + (0.004 if s > 95 else 0.0)
        if self.scenario == "imbalance" and s > 90:
            spread = 0.145
        return round((v + spread / 2) * 1000), round((v - spread / 2) * 1000)

    def inputs(self):
        with self.lock:
            r = [0] * 256
            sc = self.scenario
            vbat = 16 * sum(self.cells()) / 2 / 1000
            pv1, pv2 = self.pv * 0.55, self.pv * 0.45
            def u32(i, v):
                v = int(v) & 0xFFFFFFFF
                r[i], r[i + 1] = v & 0xFFFF, v >> 16
            r[0] = 0x10 if self.gen == 0 else 0x40                       # state
            r[1], r[2], r[3] = (3800 if pv1 > 10 else 120), (3700 if pv2 > 10 else 110), 0
            r[4] = round(vbat * 10)
            r[5] = int(self.soc) | (self.a.soh << 8)
            r[7], r[8], r[9] = round(pv1), round(pv2), 0
            r[10], r[11] = round(self.charge), round(self.discharge)
            r[12] = r[13] = r[14] = 2400
            r[15] = 6000
            r[16] = round(max(self.load - self.gen, 0))
            r[20] = r[21] = r[22] = 2400
            r[23] = 6000
            r[24] = round(self.load)
            r[25] = round(self.load * 1.05)
            r[28], r[29] = round(self.e["pv"] * 0.55 / 100), round(self.e["pv"] * 0.45 / 100)
            r[31] = round(self.e["load"] / 100)
            r[33], r[34] = round(self.e["chg"] / 100), round(self.e["dis"] / 100)
            r[35] = round(self.e["load"] / 100)
            r[38], r[39] = 3900, 3850
            u32(40, self.e_all["pv"] * 0.55 / 100)
            u32(42, self.e_all["pv"] * 0.45 / 100)
            u32(46, self.e_all["load"] / 100)
            u32(50, self.e_all["chg"] / 100)
            u32(52, self.e_all["dis"] / 100)
            u32(54, self.e_all["load"] / 100)
            u32(60, 0x00000010 if sc == "fault" else 0)                  # inverter fault code
            u32(62, 0)
            r[64], r[65], r[66] = 38, 41, 40
            r[67] = 21 if sc != "cold" else -2 & 0xFFFF
            u32(69, self.runtime)
            r[81], r[82] = 2000, 2000                                      # max charge/discharge current (0.1 A)
            r[83], r[84] = 560, 480
            r[96] = 1
            r[97] = round(self.cap_wh / 51.2)                              # Ah
            bms_a = (self.charge - self.discharge + self.ext) / max(vbat, 40)
            r[98] = round(bms_a * 10) & 0xFFFF
            r[99] = 4 if sc == "bms-fault" else 0
            r[100] = 0
            r[101], r[102] = self.cells()
            t_hi, t_lo = (-1.0, -3.0) if sc == "cold" else (23.0, 20.0)
            r[103], r[104] = round(t_hi * 10) & 0xFFFF, round(t_lo * 10) & 0xFFFF
            r[105] = (1 << 3) if self.gen else 0                           # generator dry contact
            r[106] = self.cycles
            r[107] = r[4]
            r[114] = round(self.load)
            r[121] = 2400 if self.gen else 0
            r[122] = 6000 if self.gen else 0
            r[123] = round(self.gen)
            r[124] = round(self.e["gen"] / 100)
            u32(125, self.e_all["gen"] / 100)
            r[127], r[128] = 1200, 1200
            r[129], r[130] = round(self.load / 2), round(self.load / 2)
            r[170] = round(self.load)
            r[171] = round(self.e["load"] / 100)
            u32(172, self.e_all["load"] / 100)
            r[193] = r[194] = 1200
            r[195] = r[196] = 1200 if self.gen else 0
            return r

    def holdings(self):
        r = [0] * 256
        r[2:7] = [ord(c) for c in "SIMUL"]                                # placeholder model text
        r[19] = self.a.device_type                                         # device type code: 44 = 18kPV, 38 = 6000XP (lxp-bridge-go model auto-detection)
        r[64] = 100                                                        # charge power %
        r[194], r[195] = 20, 90                                            # gen start/end SoC
        return r

    def state(self):
        with self.lock:
            return {"scenario": self.scenario, "soc": round(self.soc, 1), "pv_w": round(self.pv), "load_w": round(self.load),
                    "charge_w": round(self.charge), "discharge_w": round(self.discharge), "gen_w": round(self.gen),
                    "dc_charger_w": round(self.ext), "cells_mv": self.cells(), "today_kwh": {k: round(v / 1000, 2) for k, v in self.e.items()},
                    "datalog": self.a.datalog, "serial": self.a.serial}


# -------------------------------------------------------------------- the dongle's TCP side
class Dongle:
    def __init__(self, inv, a):
        self.inv, self.a = inv, a
        self.datalog, self.serial = a.datalog.encode(), a.serial.encode()
        self.clients = set()
        self.srv = None

    def serve(self):
        while True:
            if self.inv.scenario == "offline":
                if self.srv:
                    self.srv.close()
                    self.srv = None
                    for c in list(self.clients):
                        try:
                            c.close()
                        except OSError:
                            pass
                time.sleep(1)
                continue
            if not self.srv:
                self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.srv.bind(("0.0.0.0", self.a.port))
                self.srv.listen(8)
                self.srv.settimeout(1)
            try:
                c, addr = self.srv.accept()
            except (socket.timeout, OSError):
                continue
            print(f"{time.strftime('%H:%M:%S')} {self.a.datalog}: connection from {addr[0]}", flush=True)
            threading.Thread(target=self.client, args=(c,), daemon=True).start()

    def client(self, c):
        self.clients.add(c)
        c.settimeout(1)
        buf, last_hb = b"", 0.0
        try:
            while self.inv.scenario != "offline":
                if time.time() - last_hb > 15:
                    c.sendall(heartbeat(self.datalog))
                    last_hb = time.time()
                try:
                    chunk = c.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                except socket.timeout:
                    continue
                used = 0
                for used, func, start, count in parse_request(buf):
                    if func == 4:
                        regs = self.inv.inputs()
                    elif func == 3:
                        regs = self.inv.holdings()
                    elif func in (6, 0x10):
                        c.sendall(reply(self.datalog, self.serial, func, start, []))   # acknowledged, ignored
                        continue
                    else:
                        continue
                    count = max(1, min(count, 127))
                    c.sendall(reply(self.datalog, self.serial, func, start, (regs + [0] * 256)[start:start + count]))
                buf = buf[used:] if used else buf[-4096:]
        except OSError:
            pass
        finally:
            self.clients.discard(c)
            c.close()


# -------------------------------------------------------------------- control page
def control(inv, port):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            if q.get("scenario") in SCENARIOS:
                with inv.lock:
                    inv.scenario = q["scenario"]
                print(f"{time.strftime('%H:%M:%S')} scenario -> {inv.scenario}", flush=True)
            if "soc" in q:
                with inv.lock:
                    inv.soc = max(1.0, min(100.0, float(q["soc"])))
            st = inv.state()
            if self.path.startswith("/state"):
                body, ctype = json.dumps(st, indent=1), "application/json"
            else:
                links = " ".join(f"<a href='/?scenario={s}'>{'<b>' + s + '</b>' if s == st['scenario'] else s}</a>" for s in SCENARIOS)
                body = (f"<!doctype html><meta name=viewport content='width=device-width'><title>luxsim {st['datalog']}</title>"
                        f"<body style='font:16px system-ui;max-width:700px;margin:auto;padding:12px'><h2>Simulated dongle {st['datalog']}</h2>"
                        f"<p>Scenario: {links}</p><p>Set SoC: " + " ".join(f"<a href='/?soc={s}'>{s}%</a>" for s in (10, 25, 50, 80, 99)) +
                        f"</p><pre>{json.dumps(st, indent=1)}</pre><p><small>Refresh for current values. /state gives JSON.</small></p>")
                ctype = "text/html; charset=utf-8"
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    e = os.environ.get
    ap.add_argument("--datalog", default=e("DATALOG", "BA00000001"))
    ap.add_argument("--serial", default=e("SERIAL", "0000000001"))
    ap.add_argument("--device-type", type=int, default=int(e("DEVICE_TYPE", "44")), help="holding 19: 44 = 18kPV (default), 38 = 6000XP")
    ap.add_argument("--port", type=int, default=int(e("PORT", "8000")))
    ap.add_argument("--control-port", type=int, default=int(e("CONTROL_PORT", "8080")))
    ap.add_argument("--scenario", default=e("SCENARIO", "normal"), choices=SCENARIOS)
    ap.add_argument("--pv-kw", type=float, default=float(e("PV_KW", "7.5")))
    ap.add_argument("--battery-kwh", type=float, default=float(e("BATTERY_KWH", "15.36")))
    ap.add_argument("--load-w", type=float, default=float(e("LOAD_W", "500")))
    ap.add_argument("--max-charge-w", type=float, default=float(e("MAX_CHARGE_W", "6000")))
    ap.add_argument("--soc", type=float, default=float(e("SOC", "60")))
    ap.add_argument("--soh", type=int, default=int(e("SOH", "98")))
    ap.add_argument("--lat", type=float, default=float(e("LAT", "39.7")))
    ap.add_argument("--lon", type=float, default=float(e("LON", "-105.0")))
    ap.add_argument("--utc-offset", type=float, default=float(e("UTC_OFFSET", "-7")))
    a = ap.parse_args()
    if len(a.datalog) != 10 or len(a.serial) != 10:
        ap.error("datalog and serial must be 10 characters")
    inv = Inverter(a)
    def stepper():
        while True:
            inv.step(time.time())
            time.sleep(STEP)
    threading.Thread(target=stepper, daemon=True).start()
    threading.Thread(target=control, args=(inv, a.control_port), daemon=True).start()
    print(f"luxsim {a.datalog}/{a.serial} on :{a.port}, control on :{a.control_port}, scenario {a.scenario}", flush=True)
    Dongle(inv, a).serve()


if __name__ == "__main__":
    main()
