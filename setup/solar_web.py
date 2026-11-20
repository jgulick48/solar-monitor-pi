#!/usr/bin/env python3
"""solar-web: the box's start page and setup form (port 80, standard library only).

  /        status: dongles found, links to Home Assistant and Grafana, site summary
  /setup   form for the site details (location, time zone, batteries, panels,
           generator); saving writes state/site.json and applies it
  /rescan  runs solar-setup again (POST)

Applying the site details writes Home Assistant's core settings
(homeassistant/core.yaml: name, location, time zone, units) and a package with
the site's sizes (homeassistant/packages/site.yaml), sets the time zone for
Grafana, then restarts the affected containers.
"""
import argparse
import html
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import zoneinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solar_setup as ss  # noqa: E402

STACK = "/opt/solar-monitor/stack"
SETUP_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "solar_setup.py")
scan_lock = threading.Lock()
apply_log = []

DIRECTIONS = [("180", "South"), ("135", "South-east"), ("225", "South-west"), ("90", "East"),
              ("270", "West"), ("0", "North")]

DEFAULT_SITE = {
    "name": "Solar",
    "time_zone": "",
    "latitude": "",
    "longitude": "",
    "elevation": "",
    "units": "us_customary",
    "battery_count": "6",
    "battery_kwh_each": "5.12",
    "arrays": [{"name": "Array 1", "panels": "24", "panel_watts": "285", "tilt": "30", "azimuth": "180"},
               {"name": "", "panels": "", "panel_watts": "", "tilt": "", "azimuth": "180"}],
    "generator": "manual",
    "generator_kw": "",
    "low_soc_alert": "25",
}


def path(*p):
    return os.path.join(STACK, *p)


def load_site():
    site = ss.load_json(path("state", "site.json"), {})
    merged = json.loads(json.dumps(DEFAULT_SITE))
    merged.update({k: v for k, v in site.items() if k != "arrays"})
    if site.get("arrays"):
        empty = {"name": "", "panels": "", "panel_watts": "", "tilt": "", "azimuth": "180"}
        merged["arrays"] = (site["arrays"] + [empty, empty])[:2]
    merged["_configured"] = bool(site)
    if not merged["time_zone"]:
        merged["time_zone"] = ss.host_timezone()
    return merged


# --------------------------------------------------------------------------
# Validation and apply
# --------------------------------------------------------------------------
def num(form, key, lo, hi, required=True, integer=False):
    v = form.get(key, "").strip()
    if not v:
        if required:
            raise ValueError(f"{key.replace('_', ' ')} is required")
        return ""
    try:
        x = int(v) if integer else float(v)
    except ValueError:
        raise ValueError(f"{key.replace('_', ' ')}: '{v}' is not a number")
    if not lo <= x <= hi:
        raise ValueError(f"{key.replace('_', ' ')} must be between {lo} and {hi}")
    return str(x)


def validate(form):
    site = {
        "name": (form.get("name", "").strip() or "Solar")[:40],
        "time_zone": form.get("time_zone", "").strip(),
        "latitude": num(form, "latitude", -90, 90),
        "longitude": num(form, "longitude", -180, 180),
        "elevation": num(form, "elevation", -500, 9000, required=False, integer=True),
        "units": form.get("units") if form.get("units") in ("us_customary", "metric") else "us_customary",
        "battery_count": num(form, "battery_count", 1, 64, integer=True),
        "battery_kwh_each": num(form, "battery_kwh_each", 0.5, 100),
        "generator": form.get("generator") if form.get("generator") in ("none", "manual", "automatic") else "manual",
        "generator_kw": num(form, "generator_kw", 1, 200, required=False),
        "low_soc_alert": num(form, "low_soc_alert", 5, 80, integer=True),
        "arrays": [],
    }
    if site["time_zone"] not in zoneinfo.available_timezones():
        raise ValueError(f"unknown time zone '{site['time_zone']}'")
    for i in (1, 2):
        p = form.get(f"a{i}_panels", "").strip()
        if i == 2 and not p:
            continue
        site["arrays"].append({
            "name": (form.get(f"a{i}_name", "").strip() or f"Array {i}")[:30],
            "panels": num(form, f"a{i}_panels", 1, 500, integer=True),
            "panel_watts": num(form, f"a{i}_panel_watts", 10, 1000, integer=True),
            "tilt": num(form, f"a{i}_tilt", 0, 90, integer=True),
            "azimuth": num(form, f"a{i}_azimuth", 0, 359, integer=True),
        })
    return site


def yaml_str(s):
    return json.dumps(s)  # a JSON string is a valid YAML scalar


def apply_site(site):
    """Write HA core settings + site package and restart what changed."""
    msgs = []
    core = [
        "# Written by the solar monitor setup page (http://<this box>/setup); edits are overwritten.",
        f"name: {yaml_str(site['name'])}",
        f"latitude: {site['latitude']}",
        f"longitude: {site['longitude']}",
        f"time_zone: {yaml_str(site['time_zone'])}",
        f"unit_system: {site['units']}",
        "packages: !include_dir_named packages",
    ]
    if site["elevation"]:
        core.insert(4, f"elevation: {site['elevation']}")
    battery_kwh = round(int(site["battery_count"]) * float(site["battery_kwh_each"]), 2)
    arrays_kwp = [round(int(a["panels"]) * int(a["panel_watts"]) / 1000, 3) for a in site["arrays"]]
    pkg = [
        "# Written by the solar monitor setup page; edits are overwritten.",
        "# Site sizes for dashboards and automations.",
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
        f"          arrays: {yaml_str(json.dumps([dict(a, kwp=k) for a, k in zip(site['arrays'], arrays_kwp)]))}",
        "      - name: Site generator mode",
        "        unique_id: site_generator_mode",
        "        icon: mdi:engine",
        f"        state: {yaml_str(site['generator'])}",
        "        attributes:",
        f"          kw: \"{site['generator_kw']}\"",
        "      - name: Site low battery alert level",
        "        unique_id: site_low_battery_alert_level",
        "        unit_of_measurement: \"%\"",
        f"        state: \"{site['low_soc_alert']}\"",
    ]
    ha_changed = ss.write_if_changed(path("homeassistant", "core.yaml"), "\n".join(core) + "\n")
    ha_changed |= ss.write_if_changed(path("homeassistant", "packages", "site.yaml"), "\n".join(pkg) + "\n")
    # System time zone (used by Grafana via .env and by the host clock display)
    if site["time_zone"] != ss.host_timezone():
        r = subprocess.run(["timedatectl", "set-timezone", site["time_zone"]], capture_output=True, text=True)
        msgs.append("time zone set" if r.returncode == 0 else f"could not set the system time zone: {r.stderr.strip()}")
    sec = ss.ensure_secrets(STACK)
    env_changed = ss.write_if_changed(path(".env"), ss.load_env_text(sec, site["time_zone"]), 0o600)
    restart = (["homeassistant"] if ha_changed else []) + (["grafana"] if env_changed else [])
    if restart:
        r = subprocess.run(ss.compose_cmd(STACK) + ["up", "-d", "--force-recreate", *restart], capture_output=True, text=True)
        msgs.append(f"restarted {', '.join(restart)}" if r.returncode == 0 else f"restart failed: {r.stderr.strip()[-300:]}")
    else:
        msgs.append("nothing changed")
    return msgs


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
CSS = """:root{--bg:#fff;--fg:#1b1b1b;--muted:#666;--line:#ddd;--accent:#0a6;--warn:#b60}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#eee;--muted:#aaa;--line:#333;--accent:#3c9;--warn:#e93}}
body{background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;margin:0 auto;max-width:900px;padding:16px}
a{color:var(--accent)} table{border-collapse:collapse;width:100%;display:block;overflow-x:auto}
td,th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;white-space:nowrap}
.card{border:1px solid var(--line);border-radius:8px;padding:12px 16px;margin:12px 0}
.warn{border-color:var(--warn)} small{color:var(--muted)} code{font-size:.95em}
fieldset{border:1px solid var(--line);border-radius:8px;margin:12px 0;padding:8px 16px 16px}
label{display:block;margin-top:10px} input,select{font:inherit;padding:6px;width:100%;max-width:320px;box-sizing:border-box;
background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px}
.row{display:flex;flex-wrap:wrap;gap:0 16px} .row label{flex:1 1 140px}
button{font:inherit;padding:8px 18px;border-radius:6px;border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer}
.err{color:#c33;font-weight:600}"""


def page(title, body):
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>{body}</body></html>")


def host_of(handler):
    return (handler.headers.get("Host") or ss.lan_ip()).split(":")[0]


def status_page(handler):
    host = host_of(handler)
    site = load_site()
    inv = ss.load_json(path("state", "inverters.json"), {})
    scan = ss.load_json(path("state", "scan.json"), {})
    sec = ss.load_json(path("state", "secrets.json"), {})
    rows = "".join(
        f"<tr><td>{html.escape(dl)}</td><td>{html.escape(i.get('serial') or 'not seen yet')}</td>"
        f"<td>{html.escape(i.get('host', ''))}</td><td>{html.escape(i.get('last_seen', ''))}</td>"
        f"<td>{'yes' if i.get('present') else '<b>not found in last scan</b>'}</td></tr>" for dl, i in sorted(inv.items()))
    rows = rows or "<tr><td colspan=5>No dongles found yet. They're scanned for every hour, or press <i>Scan now</i>.</td></tr>"
    banner = "" if site["_configured"] else (
        "<div class='card warn'><b>Finish setup:</b> enter the site's location, batteries and panels on the "
        "<a href='/setup'>setup page</a> so the forecast, dashboards and alerts are right.</div>")
    kwh = round(int(site["battery_count"] or 0) * float(site["battery_kwh_each"] or 0), 1)
    kwp = round(sum(int(a["panels"] or 0) * int(a["panel_watts"] or 0) for a in site["arrays"]) / 1000, 2)
    summary = (f"{html.escape(site['name'])}: {kwh} kWh battery, {kwp} kW of panels, generator {html.escape(site['generator'])}, "
               f"time zone {html.escape(site['time_zone'])}") if site["_configured"] else "not set up yet"
    body = f"""<h1>Solar monitor</h1>{banner}
<div class="card"><b>Home Assistant</b>: <a href="http://{host}:8123">http://{host}:8123</a><br>
<small>First visit: create your own account. The inverter data appears by itself (the MQTT integration is pre-configured).</small></div>
<div class="card"><b>Grafana</b>: <a href="http://{host}:3000">http://{host}:3000</a> &nbsp;<small>dashboards are visible without logging in</small><br>
<small>Admin login (only to change dashboards): user <code>admin</code>, password <code>{html.escape(sec.get('grafana_admin', ''))}</code></small></div>
<div class="card"><b>Site</b>: {summary} &nbsp;<a href="/setup">change</a></div>
<h2>Inverter dongles</h2>
<table><tr><th>Datalog</th><th>Inverter serial</th><th>IP address</th><th>Last seen</th><th>Present</th></tr>{rows}</table>
<form method="post" action="/rescan"><p><button>Scan now</button> <small>Last scan: {html.escape(scan.get('last_scan', 'never'))}.
Scanning takes up to two minutes. Give each dongle a fixed IP (DHCP reservation) in the router if you can.</small></p></form>
<p><small>The monitor only reads from the inverters; it never changes their settings.</small></p>"""
    return page("Solar monitor", body)


def setup_page(site, error="", done=None):
    def inp(name, value, typ="text", extra=""):
        return f"<input name='{name}' type='{typ}' value='{html.escape(str(value))}' {extra}>"
    tzs = "".join(f"<option{' selected' if z == site['time_zone'] else ''}>{z}</option>"
                  for z in sorted(zoneinfo.available_timezones()) if "/" in z and not z.startswith(("Etc/", "posix/", "right/")))
    def direction(name, val):
        opts = "".join(f"<option value='{v}'{' selected' if str(val) == v else ''}>{t} ({v}°)</option>" for v, t in DIRECTIONS)
        return f"<select name='{name}'>{opts}</select>"
    arrays = ""
    for i, a in enumerate(site["arrays"], start=1):
        arrays += f"""<fieldset><legend>{'Solar array' if i == 1 else 'Second array (optional, if some panels face another way)'}</legend>
<div class="row"><label>Name {inp(f'a{i}_name', a['name'])}</label>
<label>Number of panels {inp(f'a{i}_panels', a['panels'], 'number', 'min=1')}</label>
<label>Watts per panel {inp(f'a{i}_panel_watts', a['panel_watts'], 'number', 'min=10')}</label></div>
<div class="row"><label>Tilt from flat (degrees) {inp(f'a{i}_tilt', a['tilt'], 'number', 'min=0 max=90')}</label>
<label>Facing {direction(f'a{i}_azimuth', a['azimuth'])}</label></div></fieldset>"""
    gen = "".join(f"<option value='{v}'{' selected' if site['generator'] == v else ''}>{t}</option>" for v, t in
                  (("none", "No generator"), ("manual", "Started by hand (e.g. portable generator + Chargeverter)"),
                   ("automatic", "Auto-start from the inverter's generator dry contact")))
    msg = f"<p class='err'>{html.escape(error)}</p>" if error else ""
    if done:
        msg = "<div class='card'>Saved: " + html.escape("; ".join(done)) + ". <a href='/'>Back to the start page</a></div>"
    body = f"""<h1>Site setup</h1>{msg}
<form method="post" action="/setup">
<fieldset><legend>Location</legend>
<label>Site name {inp('name', site['name'])}</label>
<div class="row"><label>Latitude {inp('latitude', site['latitude'], 'text', "inputmode='decimal' placeholder='e.g. 39.7392'")}</label>
<label>Longitude {inp('longitude', site['longitude'], 'text', "inputmode='decimal' placeholder='e.g. -104.9903'")}</label>
<label>Elevation in metres (optional) {inp('elevation', site['elevation'], 'number')}</label></div>
<small>Tip: right-click the house in Google Maps; the first line is latitude, longitude. Used for sunrise/sunset and the solar forecast.</small>
<div class="row"><label>Time zone <select name="time_zone">{tzs}</select></label>
<label>Units <select name="units"><option value="us_customary"{' selected' if site['units'] == 'us_customary' else ''}>US (°F, miles)</option>
<option value="metric"{' selected' if site['units'] == 'metric' else ''}>Metric (°C, km)</option></select></label></div></fieldset>
<fieldset><legend>Batteries</legend><div class="row">
<label>Number of batteries {inp('battery_count', site['battery_count'], 'number', 'min=1')}</label>
<label>kWh per battery {inp('battery_kwh_each', site['battery_kwh_each'], 'text', "inputmode='decimal'")}</label></div>
<small>EG4 LifePower4 48V 100Ah = 5.12 kWh; EG4 LL-S = 5.12 kWh; EG4 WallMount 280Ah = 14.3 kWh.</small>
<label>Low battery alert at (%) {inp('low_soc_alert', site['low_soc_alert'], 'number', 'min=5 max=80')}</label></fieldset>
{arrays}
<fieldset><legend>Generator</legend><label>How is the generator started? <select name="generator">{gen}</select></label>
<label>Generator size in kW (optional) {inp('generator_kw', site['generator_kw'], 'text', "inputmode='decimal'")}</label></fieldset>
<p><button>Save</button> &nbsp;<a href="/">Cancel</a></p></form>
<p><small>Saving restarts Home Assistant (about a minute) when the location, time zone or sizes changed.</small></p>"""
    return page("Site setup", body)


class Handler(BaseHTTPRequestHandler):
    server_version = "solar-web"

    def send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def form(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, 65536)).decode()
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def do_GET(self):
        if self.path == "/":
            self.send(200, status_page(self))
        elif self.path == "/setup":
            self.send(200, setup_page(load_site()))
        elif self.path == "/api/status":
            self.send(200, json.dumps({"site": load_site(), "inverters": ss.load_json(path("state", "inverters.json"), {})}),
                      "application/json")
        else:
            self.send(404, page("Not found", "<p>Not found. <a href='/'>Start page</a></p>"))

    def do_POST(self):
        if self.path == "/setup":
            f = self.form()
            try:
                site = validate(f)
            except ValueError as e:
                cur = load_site()
                cur.update({k: v for k, v in f.items() if k in DEFAULT_SITE and k != "arrays"})
                self.send(400, setup_page(cur, str(e)))
                return
            ss.write_if_changed(path("state", "site.json"), json.dumps(site, indent=2) + "\n", 0o644)
            msgs = apply_site(site)
            self.send(200, setup_page(load_site(), done=msgs))
        elif self.path == "/rescan":
            if scan_lock.acquire(blocking=False):
                def run():
                    try:
                        subprocess.run([sys.executable, SETUP_SCRIPT, "--stack", STACK, "--apply"], timeout=900)
                    finally:
                        scan_lock.release()
                threading.Thread(target=run, daemon=True).start()
            self.send(303, "", extra={"Location": "/"})
        else:
            self.send(404, "not found", "text/plain")

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))


def main():
    global STACK
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stack", default=STACK)
    ap.add_argument("--port", type=int, default=80)
    args = ap.parse_args()
    STACK = args.stack
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
