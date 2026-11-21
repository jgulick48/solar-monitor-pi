#!/usr/bin/env python3
"""solar-web: the box's start page and setup form (port 80, standard library only).

  /        status: dongles found, links to Home Assistant and Grafana, site summary
  /setup   form for the site details (location, time zone, batteries, panels,
           generator); saving writes state/site.json and applies it
  /rescan  runs solar-setup again (POST)
  /display the wall display shown on an HDMI screen (setup/display.py)

Applying the site details writes Home Assistant's core settings
(homeassistant/core.yaml: name, location, time zone, units) and a package with
the site's sizes (homeassistant/packages/site.yaml), sets the time zone for
Grafana, then restarts the affected containers.
"""
import argparse
import html
import ipaddress
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
import advisor  # noqa: E402
import backup  # noqa: E402
import display  # noqa: E402
import solar_setup as ss  # noqa: E402
import weather  # noqa: E402

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
    "generator_fuel": "gasoline",
    "future_generator_kw": "28",
    "low_soc_alert": "25",
    "inverter_model": "6000xp",
    "summary_enabled": True,
    "summary_time": "00:00",
    "costs": {"battery_each": "", "panel_per_w": "", "inverter_each": "", "generator": "", "fuel_per_gal": ""},
}
COST_FIELDS = [("battery_each", "Price per battery ($)"), ("panel_per_w", "Panels, $ per watt (installed)"),
               ("inverter_each", "Price per inverter ($)"), ("generator", "Automatic generator, installed ($)"),
               ("fuel_per_gal", "Fuel price ($ per gallon)")]


def path(*p):
    return os.path.join(STACK, *p)


UPDATE_SCRIPT = "/opt/solar-monitor/src/tools/update.sh"


def update_settings():
    d = ss.load_json(path("state", "update.json"), {})
    return {"enabled": d.get("enabled", True), "channel": d.get("channel", "release") if d.get("channel") in ("release", "main") else "release"}


def version():
    try:
        return open(os.path.join(os.path.dirname(STACK), "VERSION")).read().strip()
    except OSError:
        return "unknown"


ADVISOR_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "advisor.py")
weather_lock = threading.Lock()
advisor_lock = threading.Lock()


def weather_settings():
    d = ss.load_json(path("state", "weather.json"), {})
    d.setdefault("source", "openmeteo")
    return d


def save_weather(d):
    ss.write_if_changed(path("state", "weather.json"), json.dumps(d, indent=2) + "\n", 0o644)


def vm_push(solar, temp_c, source):
    lines = []
    ts = int(time.time() * 1000)
    if solar is not None:
        lines.append(f'site_solar_radiation_wm2{{source="{source}"}} {solar} {ts}')
    if temp_c is not None:
        lines.append(f'site_outdoor_temp_c{{source="{source}"}} {temp_c:.2f} {ts}')
    if lines:
        import urllib.request
        req = urllib.request.Request(os.environ.get("SOLAR_VM", "http://127.0.0.1:8428") + "/api/v1/import/prometheus",
                                     data=("\n".join(lines) + "\n").encode(), method="POST")
        urllib.request.urlopen(req, timeout=10).read()
        w = weather_settings()
        w["last_push"] = time.strftime("%Y-%m-%d %H:%M")
        w["last_push_reading"] = {"solar_wm2": solar, "temp_c": temp_c}
        save_weather(w)


def in_background(lock, fn):
    if lock.acquire(blocking=False):
        def run():
            try:
                fn()
            finally:
                lock.release()
        threading.Thread(target=run, daemon=True).start()
        return True
    return False


def probe_station(host):
    w = weather_settings()
    w.update(source="station", station={"host": host, "type": "", "name": ""}, probe=f"checking {host}...")
    save_weather(w)
    r = weather.probe(host)
    w = weather_settings()
    if r:
        w["station"] = {k: r[k] for k in ("type", "host", "name")}
        w["probe"] = f"found {r['type']} at {host}: {fmt_reading(r.get('reading'))}"
    else:
        w["probe"] = (f"no supported station answered at {host} (Ecowitt, WeatherLink Live or Tempest). If it can only push data, "
                      "choose 'the station sends its data here' instead.")
    save_weather(w)


def scan_stations():
    save_weather({**weather_settings(), "scan": {"status": "scanning", "found": []}})
    found = weather.scan(ss.local_networks())
    save_weather({**weather_settings(), "scan": {"status": "done", "when": time.strftime("%Y-%m-%d %H:%M"), "found": found}})


def fmt_reading(r):
    if not r:
        return "no reading"
    if r.get("error"):
        return "error: " + r["error"]
    parts = []
    if r.get("solar_wm2") is not None:
        parts.append(f"sun {r['solar_wm2']:.0f} W/m²")
    if r.get("temp_c") is not None:
        parts.append(f"{r['temp_c']:.1f} °C")
    return ", ".join(parts) or "no solar reading"


def load_site():
    site = ss.load_json(path("state", "site.json"), {})
    merged = json.loads(json.dumps(DEFAULT_SITE))
    merged.update({k: v for k, v in site.items() if k not in ("arrays", "costs")})
    merged["costs"].update(site.get("costs") or {})
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
        "generator_fuel": form.get("generator_fuel") if form.get("generator_fuel") in ("gasoline", "propane", "diesel") else "gasoline",
        "future_generator_kw": num(form, "future_generator_kw", 1, 200, required=False),
        "low_soc_alert": num(form, "low_soc_alert", 5, 80, integer=True),
        "inverter_model": form.get("inverter_model") if form.get("inverter_model") in advisor.INVERTERS else "6000xp",
        "costs": {k: num(form, f"cost_{k}", 0, 100000, required=False) for k, _ in COST_FIELDS},
        "arrays": [],
        "summary_enabled": bool(form.get("summary_enabled")),
        "summary_time": display.parse_hhmm(form.get("summary_time", "")) or "00:00",
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
    ha_changed = ss.write_if_changed(path("homeassistant", "core.yaml"), "\n".join(core) + "\n")
    ha_changed |= ss.write_if_changed(path("homeassistant", "packages", "site.yaml"), ss.site_package(site))
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


def mask_url(url):
    """rtsps://host:7441/TOKEN?x -> rtsps://host:7441/••••ab (the token is a password)."""
    u = urllib.parse.urlsplit(url)
    tail = u.path.rsplit("/", 1)[-1]
    return f"{u.scheme}://{u.netloc}/" + ("\u2022" * 4 + tail[-2:] if tail else "")


def apply_display(form):
    """Screen and camera settings from the setup form. Returns messages; raises ValueError."""
    cur = display.load(STACK)
    d = {"enabled": form.get("display_enabled") == "1",
         "off_from": display.parse_hhmm(form.get("display_off_from")),
         "off_to": display.parse_hhmm(form.get("display_off_to")), "cameras": []}
    for i in range(1, display.MAX_CAMERAS + 1):
        name = form.get(f"cam{i}_name", "").strip()[:30]
        url = form.get(f"cam{i}_url", "").strip()
        old = cur["cameras"][i - 1] if len(cur["cameras"]) >= i else None
        if form.get(f"cam{i}_remove") == "1":
            continue
        if not url and old:
            url = old["url"]  # the saved link isn't shown again; empty keeps it
        if url:
            display.go2rtc_source(url)  # validates
            d["cameras"].append({"name": name or f"Camera {i}", "url": url})
    def run(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True)
        except OSError as e:
            return subprocess.CompletedProcess(cmd, 1, "", str(e))
    msgs = []
    if display.save(STACK, d):
        r = run(ss.compose_cmd(STACK) + ["up", "-d", "--force-recreate", "go2rtc"])
        msgs.append("cameras updated" if r.returncode == 0 else f"camera service restart failed: {r.stderr.strip()[-200:]}")
    if d["enabled"] != cur.get("enabled", True):
        run(["systemctl", "start" if d["enabled"] else "stop", "solar-display.service"])
        msgs.append("screen " + ("on" if d["enabled"] else "off"))
    run(["systemctl", "start", "--no-block", "solar-display-power.service"])
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
    upd = update_settings()
    ust = ss.load_json(path("state", "update-status.json"), {})
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
    wth = weather_settings()
    st = wth.get("station") or {}
    weather_line = {
        "openmeteo": "Open-Meteo (online).",
        "station": f"weather station {html.escape(st.get('type') or '?')} at {html.escape(st.get('host', '?'))}. {html.escape(wth.get('probe', ''))}",
        "push": "the weather station sends its data here. " + (f"Last received {html.escape(wth['last_push'])}." if wth.get("last_push") else "Nothing received yet."),
    }[wth["source"]] + " <a href='/setup'>change</a>"
    sc = wth.get("scan") or {}
    scan_rows = ""
    if sc.get("status") == "scanning":
        scan_rows = "<p><small>Scanning... refresh in a minute.</small></p>"
    elif sc.get("status") == "done":
        scan_rows = "".join(
            f"<form method='post' action='/weather/use'><p>{html.escape(r.get('name') or r['type'])} at {html.escape(r['host'])} "
            f"({html.escape(fmt_reading(r.get('reading')))}) <input type='hidden' name='host' value='{html.escape(r['host'])}'>"
            f"<button>Use this station</button></p></form>" for r in sc.get("found", [])
        ) or (f"<p><small>No station found ({html.escape(sc.get('when', ''))}). It may not have a local network interface; "
              "enter its IP on the setup page, or set it to send its data here.</small></p>")
    dsp = display.load(STACK)
    display_line = (("shown on a screen plugged into the HDMI port" if dsp["enabled"] else "turned off")
                    + (f", {len(dsp['cameras'])} camera(s)" if dsp["cameras"] else ", no cameras") + ".")
    adv = ss.load_json(path("state", "advisor.json"), {})
    adv_note = (f"last run {adv.get('generated')}: " + (adv.get("message") or (adv.get("recommendation") or [""])[0])) if adv else "not run yet (runs weekly)"
    body = f"""<h1>Solar monitor</h1>{banner}
<div class="card"><b>Home Assistant</b>: <a href="http://{host}:8123">http://{host}:8123</a><br>
<small>First visit: create your own account. The inverter data appears by itself (the MQTT integration is pre-configured).</small></div>
<div class="card"><b>Grafana</b>: <a href="http://{host}:3000">http://{host}:3000</a> &nbsp;<small>dashboards are visible without logging in</small><br>
<small>Admin login (only to change dashboards): user <code>admin</code>, password <code>{html.escape(sec.get('grafana_admin', ''))}</code></small></div>
<div class="card"><b>Site</b>: {summary} &nbsp;<a href="/setup">change</a></div>
<div class="card"><b>Version</b> {html.escape(version())} &nbsp;<small>automatic updates {'on' if upd['enabled'] else '<b>off</b>'}
({'releases' if upd['channel'] == 'release' else 'every change on main'}). {html.escape(ust.get('message', 'not checked yet'))}
{('(' + html.escape(ust.get('checked', '')) + ')') if ust.get('checked') else ''}</small>
<form method="post" action="/update" style="margin-top:8px"><button>Check for updates now</button></form></div>
<div class="card"><b>Advisor</b>: <a href="/advisor">what would help most &mdash; batteries, panels, an inverter or an automatic generator</a>
<br><small>{html.escape(adv_note)}</small></div>
<div class="card"><b>Backups</b>: {backup_line()} &nbsp;<a href="/backup">change</a></div>
<div class="card"><b>Screen</b>: {display_line} <a href="/display">preview</a> &nbsp;<a href="/setup#screen">change</a></div>
<div class="card" id="weather"><b>Sunlight data</b>: {weather_line}
<form method="post" action="/weather/scan" style="margin-top:8px"><button>Scan for weather stations</button>
<small>Looks for Ecowitt gateways and WeatherFlow Tempest hubs (about a minute).</small></form>{scan_rows}</div>
<h2>Inverter dongles</h2>
<table><tr><th>Datalog</th><th>Inverter serial</th><th>IP address</th><th>Last seen</th><th>Present</th></tr>{rows}</table>
<form method="post" action="/rescan"><p><button>Scan now</button> <small>Last scan: {html.escape(scan.get('last_scan', 'never'))}.
Scanning takes up to two minutes. Give each dongle a fixed IP (DHCP reservation) in the router if you can.</small></p></form>
<p><small>The monitor only reads from the inverters; it never changes their settings.</small></p>"""
    return page("Solar monitor", body)


def setup_page(site, error="", done=None):
    upd = update_settings()
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
    inv_opts = "".join(f"<option value='{k}'{' selected' if site.get('inverter_model') == k else ''}>{v['name']} "
                       f"({v['ac_kw']:g} kW out, {v['pv_kw']:g} kW solar in)</option>" for k, v in advisor.INVERTERS.items())
    fuel_opts = "".join(f"<option value='{v}'{' selected' if site.get('generator_fuel') == v else ''}>{v.title()}</option>"
                        for v in ("gasoline", "propane", "diesel"))
    n_inv = len(ss.load_json(path("state", "inverters.json"), {}))
    wth = weather_settings()
    cost_inputs = "".join(f"<label>{t} {inp('cost_' + k, site['costs'].get(k, ''), 'text', chr(32) + 'inputmode=decimal')}</label>"
                          for k, t in COST_FIELDS)
    station_note = html.escape(wth.get("probe", "")) or "To find a station, use <i>Scan for weather stations</i> on the start page."
    if wth["source"] == "push":
        station_note = ("Set the station's custom server to this box: Ecowitt (WS View &rarr; Customized) protocol Ecowitt, "
                        "path <code>/weather/ecowitt</code>, port 80; Ambient Weather path <code>/weather/ambient?</code>. "
                        + (f"Last data received {html.escape(wth['last_push'])}." if wth.get("last_push") else "Nothing received yet."))
    dsp = display.load(STACK)
    cams = ""
    for i in range(1, display.MAX_CAMERAS + 1):
        c = dsp["cameras"][i - 1] if len(dsp["cameras"]) >= i else {}
        saved = (f"<small>Saved: <code>{html.escape(mask_url(c['url']))}</code>. Leave the link empty to keep it. "
                 f"<label style='display:inline'><input type='checkbox' name='cam{i}_remove' value='1' style='width:auto'> remove</label></small>") if c else ""
        cams += (f"<div class='row'><label>Camera {i} name {inp(f'cam{i}_name', c.get('name', ''), 'text', 'placeholder=' + chr(39) + ('Driveway' if i == 1 else 'Front door') + chr(39))}</label>"
                 f"<label style='flex:2 1 300px'>Stream link {inp(f'cam{i}_url', '', 'text', 'placeholder=' + chr(39) + 'rtsps://192.168.1.1:7441/...' + chr(39) + ' style=' + chr(39) + 'max-width:100%' + chr(39) + ' autocomplete=off')}</label></div>{saved}")
    screen_fields = f"""<fieldset id="screen"><legend>Screen and cameras</legend>
<label><input type="checkbox" name="display_enabled" value="1" style="width:auto"{' checked' if dsp['enabled'] else ''}> Show the dashboard on a screen plugged into the HDMI port</label>
<div class="row"><label>Screen off from (optional) {inp('display_off_from', dsp['off_from'], 'time')}</label>
<label>Screen on again at {inp('display_off_to', dsp['off_to'], 'time')}</label></div>
<small>For example 22:00 until 06:00, to save power and the screen.</small>
<p><small>Up to two cameras are shown next to the power figures. UniFi Protect: open the camera &rarr; Settings &rarr; Advanced &rarr;
RTSPS, turn on <b>Medium</b> quality and copy its link (<code>rtsps://...:7441/...</code>). The camera pictures are only shown on the
box's own screen, not to other computers on the network.</small></p>{cams}
<p><small>Camera entities, motion and doorbell events in Home Assistant: add the UniFi Protect integration there (Settings &rarr;
Devices &amp; services; it's usually found automatically) with a local Protect user.</small></p></fieldset>"""
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
<fieldset><legend>Daily summary</legend>
<label><input type="checkbox" name="summary_enabled" value="1" style="width:auto"{' checked' if site.get('summary_enabled', True) else ''}> Send a daily summary to the Home Assistant app</label>
<label>At {inp('summary_time', site.get('summary_time') or '00:00', 'time', "style='max-width:140px'")}</label>
<small>Solar made, power used, the lowest battery level and generator time for the day, plus the solar forecast; on
Sundays also the week and the advisor's latest finding. Sent before 06:00 it covers the day before.</small></fieldset>
{arrays}
<fieldset><legend>Inverters</legend><label>Model <select name="inverter_model">{inv_opts}</select></label>
<small>{n_inv} inverter dongle(s) found on the network. Used by the advisor for output, solar input and charging limits.</small></fieldset>
<fieldset><legend>Generator</legend><label>How is the generator started? <select name="generator">{gen}</select></label>
<div class="row"><label>Generator size in kW (optional) {inp('generator_kw', site['generator_kw'], 'text', "inputmode='decimal'")}</label>
<label>Fuel <select name="generator_fuel">{fuel_opts}</select></label></div>
<label>Size of an automatic generator to compare against, kW {inp('future_generator_kw', site['future_generator_kw'], 'text', "inputmode='decimal'")}</label></fieldset>
<fieldset><legend>Sunlight data (for the advisor)</legend>
<label><input type="radio" name="weather_source" value="openmeteo" style="width:auto"{' checked' if wth['source'] == 'openmeteo' else ''}>
 Open-Meteo (free online weather service; sends the site location)</label>
<label><input type="radio" name="weather_source" value="station" style="width:auto"{' checked' if wth['source'] == 'station' else ''}>
 Our weather station at IP address {inp('station_ip', (wth.get('station') or {}).get('host', ''), 'text', "placeholder='e.g. 192.168.1.40' style='max-width:200px'")}</label>
<label><input type="radio" name="weather_source" value="push" style="width:auto"{' checked' if wth['source'] == 'push' else ''}>
 The station sends its data here (Ecowitt "customized" upload or Ambient Weather custom server)</label>
<small>{station_note}</small></fieldset>
{screen_fields}
<fieldset><legend>Prices (optional, for the advisor's cost comparison)</legend><div class="row">{cost_inputs}</div></fieldset>
<fieldset><legend>Updates</legend>
<label><input type="checkbox" name="auto_update" value="1" style="width:auto"{' checked' if upd['enabled'] else ''}> Install new versions automatically (checked daily, early morning)</label>
<label>Follow <select name="update_channel"><option value="release"{' selected' if upd['channel'] == 'release' else ''}>Releases (recommended)</option>
<option value="main"{' selected' if upd['channel'] == 'main' else ''}>Every change on main (test boxes)</option></select></label></fieldset>
<p><button>Save</button> &nbsp;<a href="/">Cancel</a></p></form>
<p><small>Saving restarts Home Assistant (about a minute) when the location, time zone or sizes changed.</small></p>"""
    return page("Site setup", body)


def run_quietly(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "never"


def backup_line():
    """One line for the start page."""
    cfg, st = backup.settings(STACK), ss.load_json(backup.status_path(STACK), {})
    if not cfg["uuid"]:
        return "<b>not set up</b>: plug a USB stick into the Pi and choose it."
    line = f"last backup {_when(st.get('last_ok'))}"
    if st.get("snapshots"):
        line += f", {st['snapshots']} on the stick, {st.get('free_gb', '?')} GB free"
    if st.get("result") == "error":
        line += f". <b>Last attempt failed</b>: {html.escape(st.get('message', ''))}"
    elif st.get("result") in ("running", "restoring"):
        line += f". {html.escape(st.get('message', ''))}..."
    return line


def backup_page(msg=""):
    cfg, st = backup.settings(STACK), ss.load_json(backup.status_path(STACK), {})
    sticks = backup.list_sticks()
    known = {k["uuid"] for k in sticks}
    opts = []
    for k in sticks:
        desc = f"{k['model'].strip() or 'USB stick'}, {k['size']}, {k['fstype']}" + (f", label {k['label']}" if k["label"] else "")
        opts.append(f"<label><input type='radio' name='uuid' value='{html.escape(k['uuid'])}' style='width:auto'"
                    f"{' checked' if k['uuid'] == cfg['uuid'] else ''}> {html.escape(desc)}</label>")
    if cfg["uuid"] and cfg["uuid"] not in known:
        opts.append(f"<label><input type='radio' name='uuid' value='{html.escape(cfg['uuid'])}' style='width:auto' checked>"
                    " The chosen stick (not plugged in right now)</label>")
    opts.append(f"<label><input type='radio' name='uuid' value='' style='width:auto'{'' if cfg['uuid'] else ' checked'}>"
                " No backups</label>")
    found = "" if sticks else "<p><small>No USB stick found. Plug one in (FAT32, exFAT, ext4 or NTFS; 16 GB or more) and reload this page.</small></p>"
    status = html.escape(st.get("message", "no backup yet"))
    restore = ""
    if cfg["uuid"]:
        restore = """<h2>Restore</h2><form method="post" action="/backup/restore">
<p>Replaces this box's settings, passwords, Home Assistant (accounts, dashboards, history) and the inverter history with the
newest backup on the stick. Use it after replacing the SSD: flash the image, start the box, choose the stick above, then restore.
It stops everything for a few minutes.</p>
<label><input type="checkbox" name="confirm" value="1" style="width:auto"> Yes, replace everything on this box with the backup</label>
<p><button>Restore from the stick</button></p></form>"""
    body = f"""<h1>Backups</h1>{('<div class="card">' + html.escape(msg) + '</div>') if msg else ''}
<div class="card"><b>Status</b>: {backup_line()}<br><small>Last result: {status}</small>
<form method="post" action="/backup/run" style="margin-top:8px"><button{'' if cfg['uuid'] else ' disabled'}>Back up now</button></form></div>
<form method="post" action="/backup/settings"><fieldset><legend>USB stick</legend>{''.join(opts)}{found}
<label><input type="checkbox" name="history" value="1" style="width:auto"{' checked' if cfg['history'] else ''}> Include the inverter history (VictoriaMetrics)</label>
<small>Every night at about 01:30; 7 daily, 4 weekly and 6 monthly backups are kept. The stick is only in use during a backup,
so it can be pulled out at any other time. It is never formatted: the backups go in a <code>solar-monitor-backup</code> folder.
<b>The key to the backups is on the stick too</b> (so a new box can restore from the stick alone): whoever has the stick can read
this box's passwords and camera links. Keep it in the Pi.</small></fieldset>
<p><button>Save</button> &nbsp;<a href="/">Back</a></p></form>{restore}"""
    return page("Backups", body)


def advisor_page():
    a = ss.load_json(path("state", "advisor.json"), {})
    running = advisor_lock.locked()
    btn = ("<p><small>Analysis running... refresh in a minute.</small></p>" if running else
           "<form method='post' action='/advisor/run'><p><button>Run the analysis now</button> <small>It also runs every week.</small></p></form>")
    e = html.escape
    if not a:
        return page("Advisor", f"<h1>Advisor</h1><p>Not run yet.</p>{btn}<p><a href='/'>Start page</a></p>")
    if a.get("status") != "ok":
        return page("Advisor", f"<h1>Advisor</h1><div class='card warn'>{e(a.get('message', a.get('status', '')))}</div>{btn}<p><a href='/'>Start page</a></p>")
    d = a["data"]
    rec = "".join(f"<li>{e(x)}</li>" for x in a.get("recommendation", []))
    def v(x, unit=""):
        return "&ndash;" if x is None else f"{x:g}{unit}" if isinstance(x, (int, float)) else e(str(x))
    sc = "".join(
        f"<tr><td>{e(r['name'])}</td><td>{v(r.get('shortfall', 0))}</td><td>{v(r.get('short_days', 0))}</td><td>{v(r.get('hours', 0))}</td>"
        f"<td>{v(r.get('fuel', 0))}</td><td>{v(r.get('wasted', 0))}</td><td>{('$' + format(r['price'], ',')) if r.get('price') else '&ndash;'}</td>"
        f"<td>{v(r.get('payback_years'))}</td></tr>" for r in a.get("scenarios", []))
    mo = "".join(
        f"<tr><td>{e(m['month'])}</td><td>{v(m['days'])}</td><td>{v(m['load_per_day'])}</td><td>{v(m['pv_per_day'])}</td>"
        f"<td>{v(m['potential_per_day'])}</td><td>{v(m['lost_to_full_per_day'])}</td><td>{v(m['generator_kwh'])}</td>"
        f"<td>{v(m['generator_starts'])}</td><td>{v(m['low_battery_days'])}</td></tr>" for m in a.get("months", []))
    h = a.get("headroom", {})
    ag = a.get("automatic_generator")
    body = f"""<h1>Advisor</h1><p><small>Analysed {e(a['generated'])}: {v(d['days'])} days ({e(d['from'])} to {e(d['to'])}),
sunlight from {e(a['source'])}{', calibrated ' + format(d['calibration'], '.2f') + ' on ' + str(d['calibration_samples']) + ' samples' if d.get('calibration') else ' (not calibrated yet)'}.
Confidence: <b>{e(d['confidence'])}</b>.</small></p>
<div class="card"><ul>{rec}</ul></div>{btn}
<h2>Options compared</h2>
<p><small>{e(a.get('period_note', ''))} "Short" is the energy the batteries would have lacked to stay above 20% without a generator;
generator hours and fuel are what it takes to make that up ({e(a['site']['inverter'])} x{a['site']['inverters']}).</small></p>
<table><tr><th>Option</th><th>Short kWh</th><th>Days short</th><th>Gen hours</th><th>Fuel gal</th><th>Solar wasted kWh</th><th>Cost</th><th>Payback yrs</th></tr>{sc}</table>
{('<p><small>An automatic ' + format(ag['kw'], 'g') + ' kW generator would make up the shortfall in about ' + str(ag['hours']) + ' h (' + format(ag['starts'], 'g') + ' starts, ' + str(ag['fuel']) + ' gal propane), charging at ' + str(ag['charge_kw']) + ' kW.</small></p>') if ag else ''}
<h2>Capacity</h2>
<table><tr><td>House load</td><td>peak {v(h.get('peak_load_kw'))} kW, 99% of the time below {v(h.get('p99_load_kw'))} kW, inverter output {v(h.get('inverter_output_kw'))} kW</td></tr>
<tr><td>Solar</td><td>array {v(h.get('array_kwp'))} kW, peak seen {v(h.get('peak_pv_kw'))} kW, inverters accept up to {v(h.get('pv_input_limit_kw'))} kW</td></tr></table>
<h2>By month (per day averages)</h2>
<table><tr><th>Month</th><th>Days</th><th>Load kWh</th><th>Solar used</th><th>Solar possible</th><th>Lost (full)</th><th>Generator kWh</th><th>Gen runs</th><th>Low days</th></tr>{mo}</table>
<p><a href="/">Start page</a></p>"""
    return page("Advisor", body)


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
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/weather/ambient":
            # Ambient Weather "custom server": GET <path>?...&solarradiation=..&tempf=..
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            self.weather_push(q, "ambient")
            return
        if self.path == "/":
            self.send(200, status_page(self))
        elif self.path == "/advisor":
            self.send(200, advisor_page())
        elif self.path == "/display":
            self.send(200, display.PAGE)
        elif self.path == "/display/data":
            self.send(200, json.dumps(display.data(STACK, load_site())), "application/json")
        elif self.path == "/api/ha":
            self.send(200, json.dumps(display.ha_status(STACK, load_site())), "application/json")
        elif self.path == "/backup":
            self.send(200, backup_page())
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
                for i in range(1, display.MAX_CAMERAS + 1):  # check the screen settings before saving anything
                    if f.get(f"cam{i}_url", "").strip():
                        display.go2rtc_source(f[f"cam{i}_url"])
                for k in ("display_off_from", "display_off_to"):
                    display.parse_hhmm(f.get(k))
            except ValueError as e:
                cur = load_site()
                cur.update({k: v for k, v in f.items() if k in DEFAULT_SITE and k != "arrays"})
                self.send(400, setup_page(cur, str(e)))
                return
            ss.write_if_changed(path("state", "site.json"), json.dumps(site, indent=2) + "\n", 0o644)
            ss.write_if_changed(path("state", "update.json"), json.dumps(
                {"enabled": f.get("auto_update") == "1", "channel": "main" if f.get("update_channel") == "main" else "release"}, indent=2) + "\n", 0o644)
            msgs = apply_site(site)
            msgs += apply_display(f)
            src = f.get("weather_source") if f.get("weather_source") in ("openmeteo", "station", "push") else "openmeteo"
            ip = f.get("station_ip", "").strip()
            w = weather_settings()
            if src == "station" and ip and ip != (w.get("station") or {}).get("host"):
                try:
                    ipaddress.ip_address(ip)
                    in_background(weather_lock, lambda: probe_station(ip))
                    msgs.append(f"checking the weather station at {ip}")
                except ValueError:
                    msgs.append(f"'{ip}' is not an IP address; weather station not changed")
            elif src != w["source"]:
                w["source"] = src
                save_weather(w)
            self.send(200, setup_page(load_site(), done=msgs))
        elif self.path == "/backup/settings":
            f = self.form()
            uuid = f.get("uuid", "").strip()
            if uuid and uuid not in {k["uuid"] for k in backup.list_sticks()} | {backup.settings(STACK)["uuid"]}:
                self.send(400, backup_page("That stick isn't plugged in."))
                return
            backup.save_settings(STACK, {**backup.settings(STACK), "uuid": uuid, "history": f.get("history") == "1"})
            self.send(200, backup_page("Saved." + (" The first backup runs tonight; or use Back up now." if uuid else "")))
        elif self.path == "/backup/run":
            run_quietly(["systemctl", "start", "--no-block", "solar-backup.service"])
            self.send(303, "", extra={"Location": "/backup"})
        elif self.path == "/backup/restore":
            if self.form().get("confirm") != "1":
                self.send(400, backup_page("Tick the box to confirm the restore."))
                return
            # Its own unit: the restore restarts the stack, and must outlive this request.
            run_quietly(["systemd-run", "--unit=solar-restore", "--collect", "--no-block", sys.executable,
                         os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup.py"), "restore", "--stack", STACK])
            self.send(200, backup_page("Restoring: this takes a few minutes. Reload this page to see when it's done."))
        elif self.path == "/update":
            if os.path.exists(UPDATE_SCRIPT):
                # Its own systemd unit: the update restarts this web service, which must not take the update with it.
                subprocess.run(["systemd-run", "--unit=solar-update-manual", "--collect", "--no-block",
                                UPDATE_SCRIPT, "--manual"], capture_output=True)
            self.send(303, "", extra={"Location": "/"})
        elif self.path == "/weather/ecowitt":
            self.weather_push(self.form(), "ecowitt")  # Ecowitt "customized" upload, protocol Ecowitt (form POST)
        elif self.path == "/weather/scan":
            in_background(weather_lock, scan_stations)
            self.send(303, "", extra={"Location": "/#weather"})
        elif self.path == "/weather/use":
            f = self.form()
            host = f.get("host", "")
            try:
                ipaddress.ip_address(host)
                in_background(weather_lock, lambda: probe_station(host))
            except ValueError:
                pass
            self.send(303, "", extra={"Location": "/#weather"})
        elif self.path == "/advisor/run":
            in_background(advisor_lock, lambda: subprocess.run([sys.executable, ADVISOR_SCRIPT, "--stack", STACK], timeout=1800,
                                                                 capture_output=True))
            self.send(303, "", extra={"Location": "/advisor"})
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

    def weather_push(self, q, kind):
        try:
            solar = float(q["solarradiation"]) if q.get("solarradiation") not in (None, "") else None
            tf = float(q["tempf"]) if q.get("tempf") not in (None, "") else None
            if weather_settings().get("source") == "push":
                vm_push(solar, weather.f_to_c(tf), kind)
            self.send(200, "OK", "text/plain")
        except (ValueError, OSError) as e:
            self.send(400, f"bad data: {e}", "text/plain")

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
