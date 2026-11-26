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
import re
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
import boxhealth  # noqa: E402
import checkup  # noqa: E402
import configfile  # noqa: E402
import battery  # noqa: E402
import display  # noqa: E402
import protect  # noqa: E402
import screenpower as sp  # noqa: E402
import genlog  # noqa: E402
import history  # noqa: E402
import report  # noqa: E402
import modules  # noqa: E402
import overload  # noqa: E402
import solar_setup as ss  # noqa: E402
import solarcheck  # noqa: E402
import tts  # noqa: E402
import weather  # noqa: E402
import wx  # noqa: E402
from webui import FieldError, is_error, notice  # noqa: E402

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
    "spare_notify": False,
    "inverter_model": "6000xp",
    "summary_enabled": True,
    "summary_time": "00:00",
    "report_enabled": True,
    "clock": "",
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


def vm_push(solar, temp_c, source, wind_ms=None, gust_ms=None):
    lines = []
    ts = int(time.time() * 1000)
    if wind_ms is not None:
        lines.append(f'site_wind_speed_ms{{source="{source}"}} {wind_ms:.2f} {ts}')
    if gust_ms is not None:
        lines.append(f'site_wind_gust_ms{{source="{source}"}} {gust_ms:.2f} {ts}')
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
FIELD_NAMES = {"latitude": "Latitude", "longitude": "Longitude", "elevation": "Elevation", "battery_count": "Number of batteries",
               "battery_kwh_each": "kWh per battery", "generator_kw": "Generator size", "future_generator_kw": "Automatic generator size",
               "low_soc_alert": "Low battery alert", "time_zone": "Time zone"}


def field_name(key):
    m = re.match(r"a(\d)_(\w+)", key)
    if m:
        return f"Array {m.group(1)}: " + {"panels": "number of panels", "panel_watts": "watts per panel", "tilt": "tilt"}.get(m.group(2), m.group(2))
    if key.startswith("cost_"):
        return dict(COST_FIELDS).get(key[5:], key)
    return FIELD_NAMES.get(key, key.replace("_", " ").capitalize())


def num(form, key, lo, hi, required=True, integer=False):
    v = form.get(key, "").strip()
    if not v:
        if required:
            raise FieldError(key, f"{field_name(key)} is needed")
        return ""
    try:
        x = int(v) if integer else float(v)
    except ValueError:
        raise FieldError(key, f"{field_name(key)}: '{v}' is not a number")
    if not lo <= x <= hi:
        raise FieldError(key, f"{field_name(key)} must be between {lo:g} and {hi:g}")
    return str(x)


def validate(form):
    cur = load_site()
    site = {
        "name": (form.get("name", "").strip() or "Solar")[:40],
        "time_zone": form.get("time_zone", "").strip(),
        "latitude": num(form, "latitude", -90, 90),
        "longitude": num(form, "longitude", -180, 180),
        "elevation": num(form, "elevation", -500, 9000, required=False, integer=True),
        "units": form.get("units") if form.get("units") in ("us_customary", "metric") else "us_customary",
        "battery_count": num(form, "battery_count", 1, 64, integer=True),
        "battery_kwh_each": num(form, "battery_kwh_each", 0.5, 100),
        # the generator's own settings: the generator module (kept here, in site.json, where the others read them)
        **{k: cur[k] for k in ("generator", "generator_kw", "generator_fuel")},
        "future_generator_kw": num(form, "future_generator_kw", 1, 200, required=False),
        "low_soc_alert": num(form, "low_soc_alert", 5, 80, integer=True),
        "spare_notify": bool(form.get("spare_notify")),
        "inverter_model": form.get("inverter_model") if form.get("inverter_model") in advisor.INVERTERS else "6000xp",
        "costs": {k: num(form, f"cost_{k}", 0, 100000, required=False) for k, _ in COST_FIELDS},
        "arrays": [],
        "summary_enabled": bool(form.get("summary_enabled")),
        "summary_time": display.parse_hhmm(form.get("summary_time", "")) or "00:00",
        "report_enabled": bool(form.get("report_enabled")),
        "clock": form.get("clock") if form.get("clock") in ("12", "24") else "",
    }
    if site["time_zone"] not in zoneinfo.available_timezones():
        raise FieldError("time_zone", f"unknown time zone '{site['time_zone']}'")
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


def write_core(site):
    """HA core settings (homeassistant/core.yaml); the public address too when remote access is on."""
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
    for k, v in modules.ha_core(STACK).items():            # e.g. external_url from remote access
        core.insert(-1, f"{k}: {yaml_str(v)}")
    return ss.write_if_changed(path("homeassistant", "core.yaml"), "\n".join(core) + "\n")


def write_ha_core():
    """For modules (remote access): rewrite core.yaml from the saved site; True if it changed."""
    site = load_site()
    return bool(site["_configured"] and write_core(site))


modules.provide("write_ha_core", write_ha_core)


def apply_site(site):
    """Write HA core settings + site package and restart what changed."""
    msgs = []
    ha_changed = write_core(site)
    ha_changed |= ss.write_if_changed(path("homeassistant", "packages", "site.yaml"), ss.site_yaml(STACK, site))
    # System time zone (used by Grafana via .env and by the host clock display)
    if site["time_zone"] != ss.host_timezone():
        r = subprocess.run(["timedatectl", "set-timezone", site["time_zone"]], capture_output=True, text=True)
        msgs.append("time zone set" if r.returncode == 0 else f"could not set the system time zone: {r.stderr.strip()}")
    sec = ss.ensure_secrets(STACK)
    env_changed = ss.write_if_changed(path(".env"), ss.load_env_text(sec, site["time_zone"], ss.env_profiles(STACK), ss.env_extra(STACK)), 0o600)
    restart = (["homeassistant"] if ha_changed else []) + (["grafana"] if env_changed else [])
    if restart:
        r = subprocess.run(ss.compose_cmd(STACK) + ["up", "-d", "--force-recreate", *restart], capture_output=True, text=True)
        msgs.append(f"restarted {', '.join(restart)}" if r.returncode == 0 else f"restart failed: {r.stderr.strip()[-300:]}")
    else:
        msgs.append("nothing changed")
    return msgs


def power_fields(dsp):
    """The setup form's "when is the screen on" part."""
    def sel(name, opts, cur):
        return f"<select name='{name}'>" + "".join(f"<option value='{v}'{' selected' if v == cur else ''}>{html.escape(t)}</option>" for v, t in opts) + "</select>"
    rules = list(dsp.get("hours") or [])
    rows = ""
    for i in range(1, sp.MAX_RULES + 1):
        r = rules[i - 1] if len(rules) >= i else {}
        if i > max(2, len(rules) + 1):
            break
        rows += (f"<div class='row'><label>{'On-hours' if i == 1 else 'and'} {sel(f'hours{i}_days', sp.DAYS, r.get('days', 'daily'))}</label>"
                 f"<label>on at <input type='time' name='hours{i}_on' value='{html.escape(r.get('on', ''))}'></label>"
                 f"<label>off at <input type='time' name='hours{i}_off' value='{html.escape(r.get('off', ''))}'></label></div>")
    wake = dsp.get("wake") or {}
    return f"""<p style="margin-top:16px"><b>When the screen is on</b></p>
<label>Screen on {sel('power_mode', sp.MODES, dsp.get('power_mode', 'always'))}</label>
{rows}<small>An off time earlier than the on time runs past midnight (on 17:00, off 01:00). Save to get another line (up to {sp.MAX_RULES}).</small>
<label>Occupancy sensors in Home Assistant (motion, occupancy or presence; comma separated)
<input name="occupancy" value="{html.escape(', '.join(dsp.get('occupancy') or []))}" placeholder="binary_sensor.living_room_motion" style="max-width:100%"></label>
<div class="row"><label>Stay on after the room is empty for <select name="occupancy_hold">{''.join(f"<option value='{v}'{' selected' if dsp.get('occupancy_hold', 10) == v else ''}>{v} minutes</option>" for v in (0, 2, 5, 10, 15, 30, 60))}</select></label></div>
<small>Someone is in the room while any of them is on (or a person is home). Home Assistant tells this box when they change.</small>
<p style="margin:14px 0 0">Also turn the screen on, for <select name="wake_minutes" style="width:auto;display:inline">{''.join(f"<option value='{v}'{' selected' if dsp.get('wake_minutes', 3) == v else ''}>{v} minutes</option>" for v in (1, 3, 5, 10))}</select>, when a camera sees:</p>
<div class="row">{''.join(f"<label><input type='checkbox' name='wake_{k}' value='1'{' checked' if wake.get(k) else ''}> {t}</label>" for k, t in sp.WAKE_KINDS)}</div>
<label><input type="checkbox" name="cec" value="1"{' checked' if dsp.get('cec', True) else ''}> Turn the TV itself off and on (HDMI-CEC; on the TV it may be called SimpLink, Anynet+, Bravia Sync or CEC)</label>
<small>When the screen is off, the HDMI picture is switched off too; most monitors and many TVs then go to standby by themselves.
{html.escape(sp.status_line(dsp))}</small>
<p><button formaction="/display/test-power" formmethod="post" formnovalidate>Test: screen off for 15 seconds</button></p>"""


def screen_power_form(form):
    """When the screen is on (screenpower.py) from the setup form. Raises FieldError."""
    rows = []
    for i in range(1, sp.MAX_RULES + 1):
        try:
            rows.append((form.get(f"hours{i}_days", "daily"), display.parse_hhmm(form.get(f"hours{i}_on")),
                         display.parse_hhmm(form.get(f"hours{i}_off"))))
        except ValueError as e:
            raise FieldError(f"hours{i}_on", f"Screen on-hours, line {i}: {e}")
    try:
        hours = sp.clean_rules(rows)
    except ValueError as e:
        raise FieldError("hours1_on", f"Screen on-hours: {e}")
    try:
        occ = sp.clean_entities(form.get("occupancy", ""))
    except ValueError as e:
        raise FieldError("occupancy", f"Occupancy sensors: {e}")
    mode = form.get("power_mode") if form.get("power_mode") in dict(sp.MODES) else "always"
    if mode in ("hours", "hours_occupied") and not hours:
        raise FieldError("hours1_on", "Screen: add at least one line of on-hours, or choose another way to turn it on")
    if mode in ("occupied", "hours_occupied") and not occ:
        raise FieldError("occupancy", "Screen: add the Home Assistant occupancy sensors, or choose another way to turn it on")
    num = lambda k, lo, hi, dflt: max(lo, min(hi, int(form[k]))) if str(form.get(k, "")).isdigit() else dflt  # noqa: E731
    return {"power_mode": mode, "hours": hours, "occupancy": occ, "occupancy_hold": num("occupancy_hold", 0, 240, 10),
            "wake": {k: form.get(f"wake_{k}") == "1" for k, _ in sp.WAKE_KINDS}, "wake_minutes": num("wake_minutes", 1, 60, 3),
            "cec": form.get("cec") == "1", "off_from": "", "off_to": ""}


def main_choices():
    """What the wall display's main panel can be: solar, or an enabled module with a display panel."""
    return [("solar", "Solar")] + [(m.id, m.title) for m in modules.enabled(STACK) if m.display_panel]


def apply_display(form):
    """The screen's settings from the setup form. Returns messages; raises ValueError."""
    cur = display.load(STACK)
    d = {**cur, "enabled": form.get("display_enabled") == "1", **screen_power_form(form),
         "volume": max(0, min(100, int(form.get("volume") or 70) if str(form.get("volume") or "70").isdigit() else 70)),
         "sound_quiet": form.get("sound_quiet") == "1",       # the camera settings: the cameras module
         "main": form.get("display_main") if form.get("display_main") in [k for k, _ in main_choices()] else "solar",
         "panel_s": int(form["panel_s"]) if str(form.get("panel_s", "")).isdigit() and int(form["panel_s"]) in display.PANEL_SECONDS else 15}
    def run(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True)
        except OSError as e:
            return subprocess.CompletedProcess(cmd, 1, "", str(e))
    msgs = []
    display.save(STACK, d)
    if d["enabled"] != cur.get("enabled", True):
        run(["systemctl", "start" if d["enabled"] else "stop", "solar-display.service"])
        msgs.append("screen " + ("on" if d["enabled"] else "off"))
    run(["systemctl", "restart", "--no-block", "solar-display-power.service"])
    # occupancy from Home Assistant: an automation tells this box when the sensors change
    if ss.write_if_changed(path("homeassistant", "packages", "solar_display.yaml"), sp.ha_package(d)):
        r = run(ss.compose_cmd(STACK) + ["restart", "homeassistant"])
        msgs.append("Home Assistant restarted for the occupancy sensors" if r.returncode == 0 else
                    f"Home Assistant didn't restart: {r.stderr.strip()[-200:]}")
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
label{display:block;margin-top:14px} input,select,textarea{font:inherit;padding:7px 8px;width:100%;max-width:340px;box-sizing:border-box;
background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px}
/* every label above its box, all boxes the same width; checkboxes and radio buttons stay beside their text */
label>input:not([type=checkbox]):not([type=radio]),label>select,label>textarea{display:block;margin-top:5px}
label:has(>input[type=checkbox]),label:has(>input[type=radio]){display:flex;gap:8px;align-items:baseline}
input[type=checkbox],input[type=radio]{width:auto;flex:none}
.row{display:flex;flex-wrap:wrap;gap:0 20px;align-items:flex-end} .row label{flex:1 1 150px} .row label>input,.row label>select{max-width:none}
select{max-width:560px}
fieldset small{display:block;margin-top:6px;line-height:1.45} fieldset>p{margin:12px 0 0}
legend{font-weight:600;padding:0 6px}
h2{margin:48px 0 8px;padding-top:20px;border-top:2px solid var(--line)}
button{font:inherit;padding:8px 18px;border-radius:6px;border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer}
.err{color:#c33;font-weight:600}
.hist td.n,.hist th.n{text-align:right;font-variant-numeric:tabular-nums}.hist tr.total td{border-top:2px solid var(--muted)}
.checks td{vertical-align:top}.ck-mark{width:2em;text-align:center;font-weight:700}.sr{position:absolute;left:-999em}
.ck-ok .ck-mark{color:#0a6}.ck-warn .ck-mark{color:#b80}.ck-bad .ck-mark{color:#c33}.ck-off{color:var(--muted)}
.ck-sum.ck-ok{border-color:#0a6}.ck-sum.ck-warn{border-color:#b80}.ck-sum.ck-bad{border-color:#c33}
.notice{border-width:1px 1px 1px 6px}.notice.ok{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 8%,var(--bg))}
.notice.err{border-color:#c33;background:color-mix(in srgb,#c33 9%,var(--bg));font-weight:600}
header.nav{position:sticky;top:0;z-index:15;display:flex;align-items:center;gap:12px;margin:-16px -16px 12px;padding:10px 16px;
background:var(--bg);border-bottom:1px solid var(--line);overflow-x:auto;white-space:nowrap;scrollbar-width:none}
header.nav nav,header.nav .apps{display:flex;gap:4px}header.nav .apps{margin-left:auto}
header.nav a{text-decoration:none;color:var(--fg);padding:6px 10px;border-radius:6px;font-size:15px}
header.nav a:hover{background:color-mix(in srgb,var(--accent) 12%,var(--bg))}
header.nav a.on{background:var(--accent);color:#fff}header.nav a.app{color:var(--accent)}
[id]{scroll-margin-top:72px}
@media (max-width:600px){header.nav{margin:-12px -12px 10px;padding:8px 12px}}
.toast{position:fixed;top:60px;left:50%;transform:translateX(-50%);z-index:20;width:min(860px,calc(100% - 32px));box-sizing:border-box;
padding:12px 44px 12px 16px;border-radius:8px;box-shadow:0 6px 24px rgba(0,0,0,.25);color:#fff;font-weight:600}
.toast.ok{background:#0a6}.toast.err{background:#c33}
.toast .x{position:absolute;right:8px;top:6px;background:none;border:0;color:#fff;font-size:22px;padding:2px 8px}
.bad{border:2px solid #c33!important;box-shadow:0 0 0 3px color-mix(in srgb,#c33 25%,transparent)}
.fielderr{display:block;color:#c33;font-weight:600;font-size:.9em;margin-top:4px}
/* setup page: the main form's save bar, action results, and the sections saved on their own */
.savebar{position:sticky;bottom:0;z-index:10;display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;margin:16px -16px 0;padding:10px 16px;
background:var(--bg);border-top:1px solid var(--line);box-shadow:0 -4px 12px rgba(0,0,0,.08)}
.savebar .state{color:var(--muted)} .savebar.dirty{border-top:2px solid var(--warn)} .savebar.dirty .state{color:var(--warn);font-weight:600}
.actmsg{display:inline-block;margin-left:8px;font-size:.9em;font-weight:600}.actmsg.ok{color:var(--accent)}.actmsg.err{color:#c33}
.pending{display:none;color:var(--warn);font-weight:600;font-size:.9em}.pending.on{display:inline}
.own-intro{margin:56px 0 0;padding-top:20px;border-top:2px solid var(--line)} .own-intro h2{margin:0;padding:0;border:0}
section.own{border:1px solid var(--line);border-radius:10px;margin:16px 0;padding:4px 16px 12px;
background:color-mix(in srgb,var(--fg) 3%,var(--bg))} section.own h2{margin:12px 0 4px;padding:0;border:0}
@media (max-width:600px){body{padding:12px} fieldset{padding:6px 12px 14px} .row label{flex-basis:100%} .savebar{margin:16px -12px 0;padding:8px 12px}}"""


NAV = [("/", "Home", "Solar monitor"), ("/check", "Check-up", "Check-up"), ("/setup", "Setup", "Site setup"),
       ("/battery", "Battery", "Battery health"), ("/panels", "Panels", "Panels and inverters"),
       ("/history", "History", "History"), ("/report", "Report", "Monthly report"),
       ("/advisor", "Advisor", "Advisor"), ("/display", "Screen", None)]
NAV_MODULES_AT = 7          # the enabled modules' pages (backups, generator) go before the Advisor
APPS = [("8123", "Home Assistant"), ("3000", "Grafana")]


def nav(title):
    """The header on every page: this box's pages, then Home Assistant and Grafana on the same host (set in the
    browser from the address used, so it works by IP and by solar-monitor.local alike)."""
    entries = NAV[:NAV_MODULES_AT] + modules.nav(STACK) + NAV[NAV_MODULES_AT:]
    links = "".join(f"<a href='{href}'{' class=on aria-current=page' if t == title else ''}>{name}</a>" for href, name, t in entries)
    apps = "".join(f"<a class='app' data-port='{port}' href='#' target='_blank' rel='noopener'>{name} &#8599;</a>" for port, name in APPS)
    return (f"<header class='nav'><nav aria-label='Pages'>{links}</nav><span class='apps'>{apps}</span></header>"
            "<script>document.querySelectorAll('a[data-port]').forEach(a=>{a.href=location.protocol+'//'+location.hostname+':'+a.dataset.port+'/'})</script>")


def page(title, body):
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>{nav(title)}{body}</body></html>")


def host_of(handler):
    return (handler.headers.get("Host") or ss.lan_ip()).split(":")[0]


def display_problem(dsp):
    """When the display should be on but the kiosk isn't running: its state and last log lines, so a box
    without SSH can still say why the screen is black."""
    if not dsp["enabled"] or not display.screen_connected():
        return ""
    st = run_quietly(["systemctl", "show", "solar-display.service", "-p", "ActiveState,SubState,Result,NRestarts,ExecMainStatus"])
    props = dict(ln.split("=", 1) for ln in st.stdout.splitlines() if "=" in ln)
    if props.get("ActiveState") == "active" and props.get("SubState") == "running":
        return ""
    log = run_quietly(["journalctl", "-u", "solar-display.service", "-b", "-n", "40", "--no-pager", "-o", "short-iso"])
    state = ", ".join(f"{k} {props.get(k, '?')}" for k in ("ActiveState", "SubState", "Result", "NRestarts", "ExecMainStatus"))
    return (f"<br><b>The screen is connected but the dashboard isn't running</b> ({html.escape(state)}). "
            "<form method='post' action='/display/restart' style='display:inline'><button>Try again</button></form>"
            f"<details><summary>Details</summary><pre style='white-space:pre-wrap;font-size:12px'>"
            f"{html.escape((log.stdout or log.stderr)[-6000:])}</pre></details>")


def status_page(handler):
    host = host_of(handler)
    site = load_site()
    inv = ss.load_json(path("state", "inverters.json"), {})
    scan = ss.load_json(path("state", "scan.json"), {})
    sec = ss.load_json(path("state", "secrets.json"), {})
    upd = update_settings()
    ust = ss.load_json(path("state", "update-status.json"), {})
    live = boxhealth.cached("dongles", 20, boxhealth.dongles) or {}

    def live_cell(dl):
        txt, bad = boxhealth.dongle_text(live.get(dl))
        return f"<b class='err'>{html.escape(txt)}</b>" if bad else html.escape(txt)
    rows = "".join(
        f"<tr><td>{html.escape(dl)}</td><td>{html.escape(i.get('serial') or 'not seen yet')}</td>"
        f"<td>{html.escape('followed from ' + (i.get('site') or 'another site') if i.get('remote') else i.get('host', ''))}</td>"
        f"<td>{html.escape(i.get('last_seen', ''))}</td>"
        f"<td>{live_cell(dl)}</td>"
        f"<td>{'yes' if i.get('present') else '<b>not found in last scan</b>'}"
        + ("" if i.get("present") else
           f"<form method='post' action='/inverter/forget' style='display:inline;margin-left:8px'><input type='hidden' name='datalog' value='{html.escape(dl)}'>"
           f"<button title='Remove this dongle from the monitor and Home Assistant (if it comes back, the next scan finds it again)'>Forget</button></form>")
        + "</td></tr>" for dl, i in sorted(inv.items()))
    rows = rows or "<tr><td colspan=6>No dongles found yet. They're scanned for every hour, or press <i>Scan now</i>.</td></tr>"
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
    body = f"""<h1>Solar monitor</h1>{modules.banners(STACK)}{banner}
<div class="card"><b>Home Assistant</b>: <a href="http://{host}:8123">http://{host}:8123</a><br>
<small>First visit: create your own account. The inverter data appears by itself (the MQTT integration is pre-configured).</small>
<br><small>{phones_line()}</small>
<form method="post" action="/notify/test" style="margin-top:8px"><button>Send a test notification</button>
<small>to every phone with the Home Assistant app signed in to this box</small></form></div>
<div class="card"><b>Grafana</b>: <a href="http://{host}:3000">http://{host}:3000</a> &nbsp;<small>dashboards are visible without logging in</small><br>
<small>Admin login (only to change dashboards): user <code>admin</code>, password <code>{html.escape(sec.get('grafana_admin', ''))}</code></small></div>
<div class="card"><b>Site</b>: {summary} &nbsp;<a href="/setup">change</a></div>
<div class="card"><b>Version</b> {html.escape(version())} &nbsp;<small>automatic updates {'on' if upd['enabled'] else '<b>off</b>'}
({'releases' if upd['channel'] == 'release' else 'every change on main'}). {html.escape(ust.get('message', 'not checked yet'))}
{('(' + html.escape(ust.get('checked', '')) + ')') if ust.get('checked') else ''}</small>
<form method="post" action="/update" style="margin-top:8px"><button>Check for updates now</button></form></div>
<div class="card"><b>Advisor</b>: <a href="/advisor">what would help most &mdash; batteries, panels, an inverter or an automatic generator</a>
<br><small>{html.escape(adv_note)}</small></div>
{modules.cards(STACK)}
{box_card()}
{battery_card(site)}
{solarcheck.card(solarcheck.status(STACK))}
{overload.card(overload.status(site))}
{modules.cards(STACK, "access")}
<div class="card"><b>Screen</b>: {display_line} <a href="/display">preview</a> &nbsp;<a href="/setup#screen">change</a>{display_problem(dsp)}</div>
<div class="card" id="weather"><b>Sunlight data</b>: {weather_line}
<form method="post" action="/weather/scan" style="margin-top:8px"><button>Scan for weather stations</button>
<small>Looks for Ecowitt gateways and WeatherFlow Tempest hubs (about a minute).</small></form>{scan_rows}</div>
<h2>Inverter dongles</h2>
<table><tr><th>Datalog</th><th>Inverter serial</th><th>IP address</th><th>Last seen</th><th>Data</th><th>Present</th></tr>{rows}</table>
<form method="post" action="/rescan"><p><button>Scan now</button> <small>Last scan: {html.escape(scan.get('last_scan', 'never'))}.
Scanning takes up to two minutes. Give each dongle a fixed IP (DHCP reservation) in the router if you can.</small></p></form>
<p><small>The monitor only reads from the inverters; it never changes their settings.</small></p>"""
    return page("Solar monitor", body)


def scroll_to(target):
    if not target:
        return ""
    return ("<script>addEventListener('load',()=>{const t=document.getElementById(" + json.dumps(target) + ");"
            "if(t){t.scrollIntoView({block:'center'});if(t.focus)t.focus({preventScroll:true});}})</script>")


def mark_field(body, name, error):
    """Outline the input named `name` in red, with the message under it."""
    m = re.search(r"<(input|select|textarea)\b[^>]*\bname=['\"]" + re.escape(name) + r"['\"][^>]*>", body)
    if not m:
        return body
    tag = m.group(0)
    bad = tag.replace("<" + m.group(1), "<" + m.group(1) + " id='err-field' class='bad' aria-invalid='true'", 1)
    end = m.end()
    if m.group(1) in ("select", "textarea"):
        close = body.find("</" + m.group(1) + ">", end)
        end = close + len(m.group(1)) + 3 if close >= 0 else end
    return body[:m.start()] + bad + body[m.end():end] + f"<span class='fielderr'>{html.escape(error)}</span>" + body[end:]


SETUP_JS = """<script>(()=>{
const f=document.querySelector('#site-form'); if(!f) return;
const bar=document.querySelector('#savebar'), st=bar.querySelector('.state'); let dirty=false, sending=false;
const mark=()=>{dirty=true; bar.classList.add('dirty'); st.textContent='Unsaved changes';};
f.addEventListener('input',e=>{if(!e.target.closest('[data-nosave]')) mark();});
f.addEventListener('change',e=>{if(!e.target.closest('[data-nosave]')) mark();
  const c=e.target; if(c.matches('input[type=checkbox][name^=module_]')){const p=c.closest('label').querySelector('.pending');
  if(p) p.classList.toggle('on', c.checked!==c.defaultChecked);}});
f.addEventListener('submit',e=>{const b=e.submitter; if(b&&b.hasAttribute('formaction')) return; sending=true;});
addEventListener('beforeunload',e=>{if(dirty&&!sending){e.preventDefault(); e.returnValue='';}});
// Test, restart and similar buttons inside the form: run them without leaving the page, so edits stay
f.querySelectorAll('button[formaction]').forEach(b=>b.addEventListener('click',async e=>{
  e.preventDefault(); let m=b.nextElementSibling;
  if(!m||!m.classList.contains('actmsg')){m=document.createElement('span'); m.setAttribute('aria-live','polite'); b.after(m);}
  m.className='actmsg'; m.textContent='Working...'; b.disabled=true;
  const body=new URLSearchParams(new FormData(f)); if(b.name) body.set(b.name,b.value);
  try{const r=await fetch(b.formAction,{method:'POST',body,headers:{'X-Solar-Action':'1'}}); const j=await r.json();
    m.className='actmsg '+(j.ok?'ok':'err'); m.textContent=j.msg+(dirty&&j.ok?' (with the saved settings: your changes here are not saved yet)':'');
    for(const [k,v] of Object.entries(j.set||{})) document.querySelectorAll('[data-set="'+k+'"]').forEach(x=>x.textContent=v);
  }catch(err){m.className='actmsg err'; m.textContent='No answer from the box: '+err;}
  b.disabled=false;}));
})();</script>"""


def setup_page(site, error="", done=None, error_field="", form=None, module_msgs=None, done_prefix="Saved"):
    """done: what a save (or, with done_prefix="Done", an action button) did; shown in the banner."""
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
    inv_opts = "".join(f"<option value='{k}'{' selected' if site.get('inverter_model') == k else ''}>{v['name']} "
                       f"({v['ac_kw']:g} kW out, {v['pv_kw']:g} kW solar in)</option>" for k, v in advisor.INVERTERS.items())
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
    screen_fields = f"""<fieldset id="screen"><legend>Screen</legend>
<p><small>Screen: {html.escape(display.screen_line())}. The picture size comes from the screen itself (its EDID);
4K TVs are run at 1920x1080, which they scale up.</small></p>
<label><input type="checkbox" name="display_enabled" value="1" style="width:auto"{' checked' if dsp['enabled'] else ''}> Show the dashboard on a screen plugged into the HDMI port</label>
{power_fields(dsp)}
<p style="margin-top:16px"><b>Panels</b> <small style="display:inline">the main panel's tiles stay on screen; the other panels
(the solar chart, and modules such as Boiler and Propane) take turns in the chart's place, and one with an alert comes first</small></p>
<div class="row"><label>Main panel <select name="display_main">{''.join(f"<option value='{k}'{' selected' if dsp.get('main', 'solar') == k else ''}>{html.escape(t)}</option>" for k, t in main_choices())}</select></label>
<label>Next panel every <select name="panel_s">{''.join(f"<option value='{n}'{' selected' if dsp.get('panel_s', 15) == n else ''}>{n} seconds</option>" for n in display.PANEL_SECONDS)}</select></label></div>
<p style="margin-top:16px"><b>Sound</b> <small style="display:inline">through the screen's speakers (HDMI): camera chimes and voice announcements</small></p>
<div class="row"><label>Volume <input type="range" name="volume" min="0" max="100" step="5" value="{dsp.get('volume', 70)}"></label>
<label><input type="checkbox" name="sound_quiet" value="1"{' checked' if dsp.get('sound_quiet', True) else ''}> Not during the screen-off hours</label></div>
<p><button formaction="/display/test-sound" formmethod="post" formnovalidate>Play a test sound</button>
<button formaction="/display/restart" formmethod="post" formnovalidate>Restart the dashboard</button>
<small style="display:inline">on the screen (save first if you changed something). The TV's own volume applies too.</small></p>
<details><summary>Sound check</summary><pre style="white-space:pre-wrap;font-size:12px">{html.escape(sound_check())}</pre></details></fieldset>"""
    # One message per response: a banner pinned to the top of the window, the same message where it belongs
    # (next to the field, or in the section whose form was sent) and the page scrolled there.
    msg, toast, target = "", "", ""
    if error:
        toast, target = notice("err", "Not saved: " + error, toast=True), ("err-field" if error_field else "notice")
        if not error_field:
            msg = notice("err", "Not saved: " + error)
    elif done:
        text = f"{done_prefix}: " + "; ".join(done) + "."
        toast, msg = notice("ok", text, toast=True), notice("ok", text, link=("/", "Back to the start page"))
    sections = [(msg, modules.get(mid).title) for mid, msg in (module_msgs or {}).items()]
    for sec_msg, title in sections:
        if sec_msg:
            kind = "err" if is_error(sec_msg) else "ok"
            toast, target = notice(kind, f"{title}: {sec_msg}", toast=True), "notice"
    own = modules.setup_sections(STACK, module_msgs)
    own_note = ("The sections under <a href='#own'>Saved separately</a> have their own buttons and aren't part of it."
                if own else "")
    if own:
        own = ("<div class='own-intro' id='own'><h2>Saved separately</h2><p><small>Each of these has its own button, which saves "
               "only that section, straight away. <b>Save site settings</b> doesn't include them, and they don't save "
               "changes made above.</small></p></div>" + own)
    body = f"""<h1>Site setup</h1>{toast}{msg}
<p><small>Everything from <i>Optional features</i> to <i>Updates</i> is saved together with the <b>Save site settings</b>
button at the bottom of the window. The test and restart buttons among them act straight away, with the saved settings.
{own_note} To move these settings to another box, or bring another box's here: <a href="/config">Settings file</a>.</small></p>
<form method="post" action="/setup" id="site-form">
{modules.switches(STACK)}
<fieldset><legend>Location</legend>
<label>Site name {inp('name', site['name'])}</label>
<div class="row"><label>Latitude {inp('latitude', site['latitude'], 'text', "inputmode='decimal' placeholder='e.g. 39.7392'")}</label>
<label>Longitude {inp('longitude', site['longitude'], 'text', "inputmode='decimal' placeholder='e.g. -104.9903'")}</label>
<label>Elevation in metres (optional) {inp('elevation', site['elevation'], 'number')}</label></div>
<small>Tip: right-click the house in Google Maps; the first line is latitude, longitude. Used for sunrise/sunset and the solar forecast.</small>
<div class="row"><label>Time zone <select name="time_zone">{tzs}</select></label>
<label>Units <select name="units"><option value="us_customary"{' selected' if site['units'] == 'us_customary' else ''}>US (°F, miles)</option>
<option value="metric"{' selected' if site['units'] == 'metric' else ''}>Metric (°C, km)</option></select></label>
<label>Clock <select name="clock"><option value=""{' selected' if not site.get('clock') else ''}>As the units (12-hour for US)</option>
<option value="12"{' selected' if site.get('clock') == '12' else ''}>12-hour (4:41 PM)</option>
<option value="24"{' selected' if site.get('clock') == '24' else ''}>24-hour (16:41)</option></select></label></div></fieldset>
<fieldset><legend>Batteries</legend><div class="row">
<label>Number of batteries {inp('battery_count', site['battery_count'], 'number', 'min=1')}</label>
<label>kWh per battery {inp('battery_kwh_each', site['battery_kwh_each'], 'text', "inputmode='decimal'")}</label></div>
<small>EG4 LifePower4 48V 100Ah = 5.12 kWh; EG4 LL-S = 5.12 kWh; EG4 WallMount 280Ah = 14.3 kWh.</small>
<label>Low battery alert at (%) {inp('low_soc_alert', site['low_soc_alert'], 'number', 'min=5 max=80')}</label>
<label><input type="checkbox" name="spare_notify" value="1" style="width:auto"{' checked' if site.get('spare_notify') else ''}>
 Notify when there is solar to spare (the battery is full and the panels could make at least 1 kW more than the house uses, for an hour or more; at most once a day)</label>
<small>The wall display and Home Assistant (<i>Site spare power</i>) show it either way.</small></fieldset>
<fieldset><legend>Daily summary and monthly report</legend>
<label><input type="checkbox" name="summary_enabled" value="1" style="width:auto"{' checked' if site.get('summary_enabled', True) else ''}> Send a daily summary to the Home Assistant app</label>
<label>At {inp('summary_time', site.get('summary_time') or '00:00', 'time', "style='max-width:140px'")}</label>
<small>Solar made, power used, the lowest battery level and generator time for the day, plus the solar forecast; on
Sundays also the week and the advisor's latest finding. Sent before 06:00 it covers the day before.</small>
<label><input type="checkbox" name="report_enabled" value="1" style="width:auto"{' checked' if site.get('report_enabled', True) else ''}> Send a monthly report</label>
<small>Once a month, on the 1st after 8:00: last month's solar, use, generator time and fuel, the lowest battery level,
battery health and backups, beside the same month a year earlier. The <a href="/report">Report</a> page has it either way.</small></fieldset>
{arrays}
<fieldset><legend>Inverters</legend><label>Model <select name="inverter_model">{inv_opts}</select></label>
<small>{n_inv} inverter dongle(s) found on the network. Used by the advisor for output, solar input and charging limits.</small></fieldset>
<fieldset><legend>Weather data</legend>
<small>With a weather station, its readings are what the box uses now: the screen's temperature, wind, rain and the
like, and the sunlight for the advisor. Forecasts always come from Open-Meteo (a station only measures the present):
the screen's forecast and the solar forecast, which also sends the site location.</small>
<label><input type="radio" name="weather_source" value="openmeteo" style="width:auto"{' checked' if wth['source'] == 'openmeteo' else ''}>
 Open-Meteo (free online weather service; sends the site location)</label>
<label><input type="radio" name="weather_source" value="station" style="width:auto"{' checked' if wth['source'] == 'station' else ''}>
 Our weather station at IP address {inp('station_ip', (wth.get('station') or {}).get('host', ''), 'text', "placeholder='e.g. 192.168.1.40' style='max-width:200px'")}</label>
<label><input type="radio" name="weather_source" value="push" style="width:auto"{' checked' if wth['source'] == 'push' else ''}>
 The station sends its data here (Ecowitt "customized" upload or Ambient Weather custom server)</label>
<small>{station_note}</small></fieldset>
{screen_fields}
{modules.form_sections(STACK)}
<fieldset><legend>Prices (optional, for the advisor's cost comparison)</legend><div class="row">{cost_inputs}</div>
<label>Size of an automatic generator to compare against, kW {inp('future_generator_kw', site['future_generator_kw'], 'text', "inputmode='decimal'")}</label></fieldset>
<fieldset><legend>Updates</legend>
<label><input type="checkbox" name="auto_update" value="1" style="width:auto"{' checked' if upd['enabled'] else ''}> Install new versions automatically (checked daily, early morning)</label>
<label>Follow <select name="update_channel"><option value="release"{' selected' if upd['channel'] == 'release' else ''}>Releases (recommended)</option>
<option value="main"{' selected' if upd['channel'] == 'main' else ''}>Every change on main (test boxes)</option></select></label></fieldset>
<div class="savebar" id="savebar"><button>Save site settings</button><span class="state" aria-live="polite">No unsaved changes</span>
<a href="/setup">Undo changes</a></div></form>
<p><small>Saving restarts Home Assistant (about a minute) when the location, time zone or sizes changed.</small></p>
{own}{SETUP_JS}{scroll_to(target)}"""
    if error_field:
        body = mark_field(body, error_field, error)
    return page("Site setup", body)


def sound_check():
    """What the Pi's sound looks like from here: its sound cards, the card the kiosk chose (~/.asoundrc) and the
    kiosk's own sound line from this boot. Shown on the setup page, to see why the screen is silent."""
    try:
        cards = [ln.strip() for ln in open("/proc/asound/cards") if ln.strip() and ln.strip()[0].isdigit()]
    except OSError:
        cards = []
    try:
        chosen = [ln.strip() for ln in open("/var/lib/solar-display/.asoundrc") if ln.startswith("pcm.")]
    except OSError:
        chosen = []
    log = run_quietly(["journalctl", "-u", "solar-display.service", "-b", "--no-pager", "-o", "cat", "-n", "400"]).stdout
    said = [ln for ln in log.splitlines() if ln.startswith("sound:") or "ALSA" in ln or "alsa" in ln.lower()][-6:]
    lines = ["Sound cards: " + ("; ".join(cards) or "none found"),
             "The kiosk's sound goes to: " + ("; ".join(chosen) or "the default card (no choice written yet: restart the dashboard)")]
    lines += said
    return "\n".join(lines)


def run_quietly(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "never"


LEVEL_WORD = {"ok": "good", "note": "worth a look", "warn": "<b>needs attention</b>", "unknown": "no data yet"}


def battery_card(site):
    try:
        b = battery.status(STACK, site)
    except Exception:  # noqa: BLE001
        return ""
    bits = [f"cells {b['spread_top_mv']} mV apart when full" if b["spread_top_mv"] is not None else None,
            f"SOH {b['soh']:.0f}%" if b["soh"] is not None else None,
            f"{b['cycles']:.0f} cycles" if b["cycles"] is not None else None,
            f"BMS on {', '.join(b['bms_inverters'])}" if b["bms_inverters"] and b["other_inverters"] else None]
    return (f"<div class='card{' warn' if b['level'] == 'warn' else ''}'><b>Battery health</b>: {LEVEL_WORD[b['level']]}"
            f"{(': ' + ', '.join(x for x in bits if x)) if any(bits) else ''} &nbsp;<a href='/battery'>details</a></div>")


def battery_page(site):
    b = battery.status(STACK, site)
    def f(v, fmt, unit=""):
        return "?" if v is None else (fmt.format(v) + unit)
    finds = "".join(f"<li{' style=\"color:var(--warn)\"' if lvl == 'warn' else ''}>{'<b>' if lvl == 'warn' else ''}{html.escape(t)}"
                    f"{'</b>' if lvl == 'warn' else ''}</li>" for lvl, t in b["findings"]) or "<li>Nothing to report.</li>"
    days = "".join(f"<tr><td>{d}</td><td>{'' if v is None else str(v) + ' mV'}</td></tr>"
                   for d, v in sorted(b["spread_days"].items(), reverse=True))
    full = (time.strftime("%a %b %-d %H:%M", time.localtime(b["last_full"])) if b["last_full"] else
            "not in the last 60 days" if not b["history_start"] or time.time() - b["history_start"] > 59 * 86400 else
            "not since the history starts (" + time.strftime("%b %-d", time.localtime(b["history_start"])) + ")")
    cap = (f"{b['capacity_kwh']} kWh (from {b['capacity_samples']} long discharge(s))" if b["capacity_kwh"] else
           "not yet: needs a discharge of 30% or more without charging")
    body = f"""<h1>Battery health</h1>
<div class="card{' warn' if b['level'] == 'warn' else ''}"><b>Overall</b>: {LEVEL_WORD[b['level']]}<ul>{finds}</ul></div>
<table>
<tr><td>BMS data from</td><td>{html.escape(battery.bms_line(b))}</td></tr>
<tr><td>Cells now</td><td>{f(b['min_cell_v'], '{:.3f}', ' V')} to {f(b['max_cell_v'], '{:.3f}', ' V')} ({f(b['spread_now_mv'], '{}', ' mV')} apart; only meaningful near full)</td></tr>
<tr><td>Spread when last full</td><td>{f(b['spread_top_mv'], '{}', ' mV')} {('(' + b['spread_top_day'] + ')') if b['spread_top_day'] else ''}; good under 50, watch 50-100, over 100 drifting</td></tr>
<tr><td>Last full charge</td><td>{full}</td></tr>
<tr><td>Cell temperatures</td><td>{f(b['temp_min'], '{:.0f}', ' °C')} to {f(b['temp_max'], '{:.0f}', ' °C')} now; {f(b['temp_min_24h'], '{:.0f}')} to {f(b['temp_max_24h'], '{:.0f}', ' °C')} in the last 24 hours</td></tr>
<tr><td>State of health</td><td>{f(b['soh'], '{:.0f}', '%')} (lowest the BMS reports)</td></tr>
<tr><td>Cycles</td><td>{f(b['cycles'], '{:.0f}')}</td></tr>
<tr><td>Capacity estimate</td><td>{cap}; {f(b['nameplate_kwh'], '{}', ' kWh')} entered on the setup page</td></tr>
<tr><td>BMS fault / warning</td><td>{f(b['bms_fault'], '{:.0f}')} / {f(b['bms_warning'], '{:.0f}')}</td></tr>
</table>
<h2>Cell spread when full, per day</h2><table><tr><th>Day</th><th>Spread at 98% or more</th></tr>{days}</table>
<p><small>LiFePO4 cells all sit near 3.3 V for most of the charge, so a weak or unbalanced cell only shows near full,
where the BMS balances them. The capacity estimate is the energy taken out per 1% of charge on long discharges, times 100:
it follows the BMS's own charge count, so it checks the BMS against the size entered on the setup page rather than
measuring the cells. The figures are for the whole bank (the highest and lowest cells of all batteries), from the inverter
connected to the battery's BMS: in a parallel system the other inverters report zeros, which are left out.</small></p>
<p><a href="/">Back</a></p>"""
    return page("Battery health", body)


def phones():
    """Phones with the Home Assistant app signed in to this box (mobile_app config entries)."""
    d = ss.load_json(path("homeassistant", ".storage", "core.config_entries"), {})
    return [e.get("title") or "phone" for e in (d.get("data") or {}).get("entries", []) if e.get("domain") == "mobile_app"]


def phones_line():
    p = phones()
    if not p:
        return ("<b>No phone is signed in yet</b>: install the Home Assistant app, sign in to this box with your account "
                "and allow notifications. Alerts and the daily summary go to every phone signed in.")
    if any(m.id == "notifications" for m in modules.enabled(STACK)):
        import notify
        s = modules.settings(STACK, "notifications")
        names = {x["id"]: x["name"] for x in notify.phones(STACK)}
        chosen = [names[i] for i in notify.chosen_phones(STACK, s)] if s.get("ha_app") else []
        extra = " and ntfy" if s.get("ntfy") and s.get("ntfy_topic") else ""
        return ("Alerts go to: " + (", ".join(html.escape(x) for x in chosen) or "no phone") + extra
                + ' (<a href="/setup#notifications">change</a>).')
    return "Alerts go to: " + ", ".join(html.escape(x) for x in p) + "."


def send_test_notification():
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:8123/api/webhook/{ss.ha_webhook_id(STACK)}", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except OSError as e:
        return f"Home Assistant didn't answer ({getattr(e, 'reason', e)}); is it running?"
    p = phones()
    if not p:
        return "Sent to Home Assistant's notifications, but no phone is signed in yet (see the Home Assistant card)."
    return (f"Sent to {', '.join(p)}. It should arrive within a minute; if not, check the app's notification permission "
            "and that the phone has internet.")


BOX_WORD = {"ok": "good", "note": "worth a look", "warn": "<b>needs attention</b>"}


def box_card():
    try:
        b = boxhealth.status(STACK)
    except Exception:  # noqa: BLE001
        return ""
    bits = []
    if b["cpu_temp"] is not None:
        bits.append(f"{b['cpu_temp']:.0f} °C")
    if b["disk"]:
        bits.append(f"{b['disk']['free_gb']:g} GB free")
    sm = b["smart"] or {}
    if sm.get("used_pct") is not None:
        bits.append(f"SSD {sm['used_pct']}% worn")
    finds = "".join(f"<li>{html.escape(t)}</li>" for _, t in b["findings"])
    note = "" if sm.get("used_pct") is not None or not sm.get("error") else f"<br><small>SSD health: {html.escape(sm['error'])}.</small>"
    return (f"<div class='card{' warn' if b['level'] == 'warn' else ''}'><b>This box</b>: {BOX_WORD[b['level']]}"
            f"{(' (' + ', '.join(bits) + ')') if bits else ''}{('<ul>' + finds + '</ul>') if finds else ''}{note}</div>")


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
    gen_note = {"log": "<p><small>Generator energy and runs: the <a href='/generator'>generator log</a>'s.</small></p>",
                "history": "<p><small>Generator energy and runs are counted from the 15-minute history: the generator log is "
                           "off or still working through the history.</small></p>"}.get(d.get("generator_from"), "")
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
{gen_note}
<p><a href="/">Start page</a></p>"""
    return page("Advisor", body)


# --------------------------------------------------------------------------
# Settings as one file: export, and import with a preview (configfile.py)
# --------------------------------------------------------------------------
CONFIG_FORM_MAX = 3 * configfile.MAX_BYTES + 4096      # the file, URL-encoded


def config_page(msg="", kind="ok"):
    note = notice(kind, msg) if msg else ""
    body = f"""<h1>Settings file</h1>{note}
<div class="card"><b>Export</b>: this box's settings as one file, to move them to another box or keep them somewhere safe.
<form method="get" action="/config/export" style="margin-top:8px"><button>Download the settings file</button></form>
<small>It holds the site details, the wall display, weather, updates, the UniFi Protect address and camera choice, and
the settings of the generator, notifications, cameras, voice, weather, propane, water, boiler, freeze protection and
probe board modules. Secrets (API keys, tokens, camera stream links, the ntfy topic) are left out. It does not hold
history: that is what backups are for. Remote access, remote support, SSH keys, the backup stick and the radios belong
to one box and stay here.</small>
<p><small>To include the secrets, encrypted with a passphrase, export on the box itself (over SSH or its console):<br>
<code>sudo python3 /opt/solar-monitor/src/setup/configfile.py export --secrets &gt; solar-config.json</code></small></p></div>
<div class="card"><b>Import</b>: choose a settings file. You see what would change before anything is saved.
<form method="post" action="/config/preview" style="margin-top:8px">
<p><input type="file" id="cfgfile" accept=".json,application/json"></p>
<textarea name="config" id="cfgtext" rows="6" style="width:100%;font-family:monospace" placeholder="...or paste the file's text here" required></textarea>
<div class="row"><label>Passphrase <input type="password" name="passphrase" autocomplete="off" placeholder="only if the file has secrets"></label></div>
<button>Show what would change</button></form></div>
<script>document.getElementById('cfgfile').addEventListener('change',e=>{{const f=e.target.files[0];if(!f)return;
f.text().then(t=>{{document.getElementById('cfgtext').value=t}})}})</script>"""
    return page("Settings file", body)


def config_preview(form):
    """The preview of an import, or the settings page again with what is wrong with the file."""
    e = html.escape
    try:
        doc = configfile.parse(form.get("config", ""))
        plan = configfile.plan(STACK, doc, form.get("passphrase", ""), DEFAULT_SITE)
    except configfile.ConfigError as err:
        return config_page(f"Nothing imported: {err}.", "err")
    notes = "".join(f"<li>{e(n)}</li>" for n in plan["notes"])
    rows = "".join(f"<tr><td>{e(n)}</td><td>{e(p)}</td><td>{e(configfile.show(old, sec))}</td><td><b>{e(configfile.show(new, sec))}</b></td></tr>"
                   for n, p, old, new, sec in plan["changes"])
    head = (f"From <b>{e(doc.get('name') or 'a box')}</b>, exported {e(str(doc.get('exported', ''))[:16].replace('T', ' '))}"
            f" by version {e(doc.get('version') or 'unknown')} (this box: {e(version())}).")
    if not plan["changes"]:
        return page("Settings file", f"<h1>Settings file</h1><p>{head}</p>{f'<ul>{notes}</ul>' if notes else ''}"
                    "<div class='card'><b>Nothing would change</b>: this box already has these settings.</div>"
                    "<p><a href='/config'>Back</a></p>")
    body = f"""<h1>Import: what would change</h1><p>{head}</p>{f'<ul>{notes}</ul>' if notes else ''}
<p>{len(plan['changes'])} settings would change; {plan['same']} are already the same. Nothing is saved yet.</p>
<table><tr><th>Where</th><th>Setting</th><th>Now</th><th>After the import</th></tr>{rows}</table>
<form method="post" action="/config/import" style="margin-top:12px">
<textarea name="config" hidden>{e(form.get('config', ''))}</textarea>
<input type="hidden" name="passphrase" value="{e(form.get('passphrase', ''))}">
<button>Import these settings</button> <a href="/config">Cancel</a>
<p><small>The settings as they are now are kept in <code>state/config-before-import.json</code> on the box. Home Assistant
and other services restart where their settings changed (a minute or two).</small></p></form>"""
    return page("Settings file", body)


def config_import(form):
    try:
        doc = configfile.parse(form.get("config", ""))
        plan = configfile.plan(STACK, doc, form.get("passphrase", ""), DEFAULT_SITE)
    except configfile.ConfigError as err:
        return config_page(f"Nothing imported: {err}.", "err")
    if not plan["files"]:
        return config_page("Nothing to import: this box already has these settings.")
    msgs = apply_config(plan)
    bad = [m for m in msgs if is_error(m)]
    return config_page(f"Imported {len(plan['changes'])} settings. " + "; ".join(msgs) + ".", "err" if bad else "ok")


def apply_config(plan):
    """Write an import's files the way the setup page would, and restart what they affect. Returns messages."""
    msgs = []
    ss.write_if_changed(path("state", "config-before-import.json"),
                        json.dumps({"saved": time.strftime("%Y-%m-%d %H:%M:%S"), "files": configfile.current(STACK)}, indent=1) + "\n", 0o600)
    files = plan["files"]
    if "site.json" in files:
        ss.write_if_changed(path("state", "site.json"), json.dumps(files["site.json"], indent=2) + "\n", 0o644)
    if "weather.json" in files:
        save_weather(files["weather.json"])
    if "update.json" in files:
        ss.write_if_changed(path("state", "update.json"), json.dumps(files["update.json"], indent=2) + "\n", 0o644)
    if "protect.json" in files:
        protect.save(STACK, {**protect.settings(STACK), **files["protect.json"]})
    # over the loaded settings: a box that never saved its display has no file, and the defaults fill it in
    cams_changed = "display.json" in files and display.save(STACK, {**display.load(STACK), **files["display.json"]})
    for name, new in files.items():
        if not name.startswith("modules/"):
            continue
        mid = name[len("modules/"):-len(".json")]
        try:
            if isinstance(new.get("settings"), dict):
                modules.save(STACK, mid, new["settings"])
            if "enabled" in new:
                modules.set_enabled(STACK, mid, bool(new["enabled"]))
        except Exception as e:  # noqa: BLE001 - one module's trouble doesn't stop the rest of the import
            msgs.append(f"failed: the {mid} module's settings ({type(e).__name__}: {e})")
    try:
        ss.write_env(STACK, ss.ensure_secrets(STACK))
        if cams_changed:
            import cameras
            msgs.append(cameras.restart_go2rtc(STACK))
        import hastates
        hastates.apply(STACK)
        msgs += apply_site(load_site())
        if modules.load(STACK, "cameras")["enabled"]:
            protect.listen(STACK)
    except Exception as e:  # noqa: BLE001
        msgs.append(f"failed: applying the settings ({type(e).__name__}: {e})")
    return msgs


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

    def loopback(self):
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def form(self, limit=65536):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, limit)).decode()
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def action(self):
        """A button inside the setup form, sent by the page's script: answer with JSON instead of a new page."""
        return self.headers.get("X-Solar-Action") == "1"

    def action_reply(self, msg, ok=None, code=200, fields=None):
        ok = (not is_error(msg)) and code < 400 if ok is None else ok
        self.send(code if code >= 400 else 200, json.dumps({"ok": ok, "msg": msg, "set": fields or {}}), "application/json")

    def module_route(self, method, url_path):
        """An enabled module's page (modules/): True if one answered."""
        mod, fn = modules.route(STACK, method, url_path)
        if not fn:
            return False
        form = self.form() if method == "POST" else {}
        auth = self.headers.get("Authorization") or ""
        form.pop("_bearer", None)                 # only ever from the header
        if auth.startswith("Bearer "):
            form["_bearer"] = auth[7:].strip()    # a token from Home Assistant's rest_command (not in the logged payload)
        r = fn(STACK, form)
        if self.action() and (r.done is not None or r.setup_msg is not None):
            text = ("; ".join(r.done) + ".") if r.done is not None else r.setup_msg
            self.action_reply(text[:1].upper() + text[1:], code=r.code, fields=r.fields)
        elif r.location:
            self.send(303, "", extra={"Location": r.location})
        elif r.ctype:
            self.send(r.code, r.body, r.ctype)
        elif r.done is not None:
            self.send(r.code, setup_page(load_site(), done=r.done, done_prefix="Done"))
        elif r.setup_msg is not None:
            self.send(r.code, setup_page(load_site(), module_msgs={mod.id: r.setup_msg}))
        else:
            self.send(r.code, page(r.title, r.body))
        return True

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/weather/ambient":
            # Ambient Weather "custom server": GET <path>?...&solarradiation=..&tempf=..
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            self.weather_push(q, "ambient")
            return
        if self.module_route("GET", url.path):
            return
        if self.path == "/":
            self.send(200, status_page(self))
        elif self.path == "/advisor":
            self.send(200, advisor_page())
        elif url.path == "/display":  # ?layout=N pins a layout (burn-in protection, see display.py)
            self.send(200, display.PAGE)
        elif url.path == "/display/say":
            self.say(dict(urllib.parse.parse_qsl(url.query)))
        elif self.path == "/display/events":
            self.send(200, json.dumps(display.events(STACK)), "application/json")
        elif self.path == "/display/power":                  # for solar-display-power.service
            if not self.loopback():
                return self.send(403, "local only")
            self.send(200, json.dumps(display.power_state(STACK)), "application/json")
        elif self.path == "/display/data":
            self.send(200, json.dumps(display.data(STACK, load_site())), "application/json")
        elif self.path == "/api/ha":
            self.send(200, json.dumps(display.ha_status(STACK, load_site())), "application/json")
        elif self.path == "/history":
            try:
                body = history.render(boxhealth.cached("history", 900, lambda: history.build(STACK, load_site())))
            except (OSError, ValueError) as e:
                body = f"<h1>History</h1><div class='card warn'>The history couldn't be read just now ({html.escape(str(e))}). Try again in a minute.</div>"
            self.send(200, page("History", body))
        elif self.path.split("?")[0] == "/report":
            want = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("m", [""])[0]
            try:
                body = report.render(STACK, load_site(), want if report.MONTH_RE.match(want) else None, cache=boxhealth.cached)
            except (OSError, ValueError) as e:
                body = f"<h1>Monthly report</h1><div class='card warn'>The report couldn't be read just now ({html.escape(str(e))}). Try again in a minute.</div>"
            self.send(200, page("Monthly report", body))
        elif self.path == "/config":
            self.send(200, config_page())
        elif self.path == "/config/export":
            doc = configfile.export(STACK, version())
            fname = "solar-config-" + (re.sub(r"[^A-Za-z0-9]+", "-", doc["name"]).strip("-").lower() or "box") + time.strftime("-%Y%m%d") + ".json"
            self.send(200, json.dumps(doc, indent=1) + "\n", "application/json",
                      extra={"Content-Disposition": f'attachment; filename="{fname}"'})
        elif self.path == "/check":
            site = load_site()
            self.send(200, page("Check-up", checkup.render(checkup.core(STACK, site) + modules.checks(STACK), host_of(self))))
        elif self.path == "/battery":
            self.send(200, battery_page(load_site()))
        elif self.path == "/panels":
            self.send(200, page("Panels and inverters", solarcheck.render(solarcheck.status(STACK))))
        elif self.path == "/setup":
            self.send(200, setup_page(load_site()))
        elif self.path == "/api/status":
            self.send(200, json.dumps({"site": load_site(), "inverters": ss.load_json(path("state", "inverters.json"), {})}),
                      "application/json")
        else:
            self.send(404, page("Not found", "<p>Not found. <a href='/'>Start page</a></p>"))

    def do_POST(self):
        if self.module_route("POST", self.path):
            return
        if self.path == "/config/preview":
            return self.send(200, config_preview(self.form(CONFIG_FORM_MAX)))
        if self.path == "/config/import":
            return self.send(200, config_import(self.form(CONFIG_FORM_MAX)))
        if self.path == "/setup":
            f = self.form()
            try:
                site = validate(f)
                screen_power_form(f)
                modules.form_check(STACK, f)
            except ValueError as e:
                cur = load_site()
                cur.update({k: v for k, v in f.items() if k in DEFAULT_SITE and k != "arrays"})
                self.send(400, setup_page(cur, str(e), error_field=getattr(e, "field", ""), form=f))
                return
            ss.write_if_changed(path("state", "site.json"), json.dumps(site, indent=2) + "\n", 0o644)
            ss.write_if_changed(path("state", "update.json"), json.dumps(
                {"enabled": f.get("auto_update") == "1", "channel": "main" if f.get("update_channel") == "main" else "release"}, indent=2) + "\n", 0o644)
            mod_msgs = modules.form_apply(STACK, f)   # first: the generator module writes its part of site.json
            saved = ss.load_json(path("state", "site.json"), {})
            site.update({k: saved[k] for k in ("generator", "generator_kw", "generator_fuel") if k in saved})
            msgs = apply_site(site)
            msgs += apply_display(f)
            msgs += mod_msgs
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
        elif self.path in ("/display/presence", "/display/power-report"):
            # from Home Assistant's automation (host network) / the power service: this box only
            if not self.loopback():
                return self.send(403, "local only")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
            except ValueError:
                return self.send(400, "bad json")
            if self.path == "/display/presence":
                sp.report_presence(bool(body.get("occupied")))
            else:
                sp.REPORT.update(at=time.time(), **{k: body[k] for k in ("on", "tv", "signal") if k in body})
            self.send(200, "{}", "application/json")
        elif self.path == "/display/test-power":
            sp.TEST["until"] = time.time() + 15
            done = "the screen turns off for 15 seconds, then back on"
            if self.action():
                return self.action_reply(done[:1].upper() + done[1:] + ".")
            self.send(200, setup_page(load_site(), done=[done], done_prefix="Done"))
        elif self.path == "/inverter/forget":
            try:
                msgs = ss.forget_dongle(STACK, self.form().get("datalog", ""))
            except (ValueError, OSError, subprocess.SubprocessError) as e:
                msgs = [f"not forgotten: {e}"]
            self.send(200, status_page(self).replace("<h1>Solar monitor</h1>", "<h1>Solar monitor</h1>" + notice("err" if is_error(msgs[0]) else "ok", "; ".join(msgs) + "."), 1))
        elif self.path == "/display/test-sound":
            display.TEST_SOUND["at"] = time.time()
            ok = display.screen_connected() and run_quietly(["systemctl", "is-active", "--quiet", "solar-display.service"]).returncode == 0
            done = "test sound sent to the screen" if ok else "test sound sent, but the screen's dashboard isn't running"
            if self.action():
                return self.action_reply(done[:1].upper() + done[1:] + ".")
            self.send(200, setup_page(load_site(), done=[done], done_prefix="Done"))
        elif self.path == "/display/restart":
            run_quietly(["systemctl", "reset-failed", "solar-display.service"])
            run_quietly(["systemctl", "restart", "--no-block", "solar-display.service"])
            if self.action():
                return self.action_reply("The dashboard on the screen is restarting.")
            self.send(303, "", extra={"Location": "/"})
        elif self.path == "/notify/test":
            msg = send_test_notification()
            self.send(200, page("Test notification", f"<h1>Test notification</h1><div class='card'>{html.escape(msg)}</div>"
                                                      "<p><a href='/'>Back</a></p>"))
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

    def say(self, q):
        """The announcement for a camera event (or the test) as WAV, from Piper via the cache."""
        from modules import voice
        if not voice.is_on(STACK):
            self.send(503, "voice announcements are off", "text/plain")
            return
        d = display.load(STACK)
        if q.get("id"):
            text = voice.text_of(STACK, int(q["id"])) if q["id"].isdigit() else None
            if not text:
                self.send(404, "no such announcement", "text/plain")
                return
        else:
            try:
                cam = d["cameras"][int(q.get("cam", "cam0")[3:]) - 1]
            except (ValueError, IndexError):
                self.send(404, "no such camera", "text/plain")
                return
            label = q.get("label", "")
            if label not in ("Doorbell", "Person", "Vehicle", "Animal", "Package", "Motion"):
                self.send(400, "bad label", "text/plain")
                return
            text = tts.phrase(label, cam["name"], d.get("speak_template"), d.get("speak_doorbell"))
        try:
            data = tts.wav(STACK, text, voice.settings(STACK)["voice"])
        except (OSError, ValueError) as e:
            self.send(503, f"speech not available: {e}", "text/plain")
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def weather_push(self, q, kind):
        try:
            solar = float(q["solarradiation"]) if q.get("solarradiation") not in (None, "") else None
            tf = float(q["tempf"]) if q.get("tempf") not in (None, "") else None
            if weather_settings().get("source") == "push":
                fields = weather.push_fields(q)
                vm_push(solar, weather.f_to_c(tf), kind, fields.get("wind_ms"), fields.get("gust_ms"))
                wx.save_station(STACK, fields, kind)
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
    if modules.load(STACK, "cameras")["enabled"]:
        from modules import cameras as cameras_module
        cameras_module.watch_events(STACK)     # phone notifications with the camera's picture
        protect.listen(STACK)      # UniFi Protect events for the wall display, if set up (the cameras module)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
