"""Propane: the tank's level, use, days to empty and fill-ups, from a Mopeka Pro Check (the `propane` Compose service,
pro-check-server). The work is setup/propane.py; this is its switch, settings, pages and schedule."""
import html
import re
import subprocess
import threading
import time

import notify
import propane as engine
import solar_setup as ss
from modules import FieldError, Module, Response

ADDR_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def settings(stack):
    import modules
    return modules.settings(stack, "propane")


def _price(stack, s):
    if str(s.get("price") or "").strip():
        return float(s["price"])
    v = (ss.load_json(f"{stack}/state/site.json", {}).get("costs") or {}).get("fuel_per_gal")
    return float(v) if v not in (None, "") else None


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def start(stack):
    """Unblock Bluetooth (Raspberry Pi OS keeps the radios soft-blocked until they're set up, and restores that at boot;
    unblocked here, systemd-rfkill keeps it unblocked), then (re)start the sensor service."""
    def run():
        _run(["rfkill", "unblock", "bluetooth"])
        _run(ss.compose_cmd(stack) + ["up", "-d", "--force-recreate", "propane"])
    threading.Thread(target=run, daemon=True).start()


def stop(stack):
    _run(ss.compose_cmd(stack) + ["rm", "-s", "-f", "propane"])


def env(stack):
    s = settings(stack)
    sec = ss.load_json(f"{stack}/state/secrets.json", {})
    lines = [f"PROPANE_TANK_TYPE={s['tank_type']}", f"PROPANE_MQTT_PASSWORD={sec.get('mqtt_homeassistant', '')}"]
    if s.get("address") and ADDR_RE.match(s["address"]):
        lines.append(f"PROPANE_TANKS={s['address'].upper()}={s['tank_type']}:Propane tank")
    return lines


# --------------------------------------------------------------------------
# Every minute, in the collector
# --------------------------------------------------------------------------
def sample(stack, now=None):
    now = now or time.time()
    s = settings(stack)
    def alert(tag, title, message, priority):
        err = notify.alert(stack, tag, title, message, priority)
        if err:
            raise OSError(f"alert {tag} not sent: {err}")
    try:
        found = engine.sensors()
    except (OSError, ValueError):
        return engine.check_silent(stack, now, alert)
    sensor = engine.pick(found, s.get("address", ""))
    pct = engine.percent_of(sensor, s["tank_type"]) if sensor else None
    if pct is None or sensor.get("sensorReadQuality", 0) < engine.MIN_QUALITY or sensor.get("ageSeconds", 0) > 900:
        return engine.check_silent(stack, now, alert)
    extra = {k: sensor.get(k) for k in ("address", "tempCelsius", "sensorReadQuality", "batteryVoltage", "rssi")}
    msgs = engine.add_reading(stack, s, pct, now, extra, alert, engine.hdd_from_vm)
    try:
        engine.vm_push(s, pct, extra)
    except OSError:
        pass
    return msgs


# --------------------------------------------------------------------------
# Setup page, card, page, Home Assistant
# --------------------------------------------------------------------------
def _fmt(v, unit="", digits=0):
    return "&ndash;" if v is None else f"{v:,.{digits}f}{unit}"


def form_section(stack):
    s = settings(stack)
    e = html.escape
    try:
        found = engine.sensors(timeout=2)
    except (OSError, ValueError):
        found = None
    types = "".join(f"<option value='{k}'{' selected' if s['tank_type'] == k else ''}>{e(t)}</option>" for k, t, _ in engine.TANKS)
    opts = [("", "the one heard best")] + [(x["address"], f"{x['address']} ({x.get('levelPercent') or ''}{'%' if x.get('levelPercent') is not None else ''}, "
                                                          f"signal {x.get('rssi')} dBm)") for x in (found or [])]
    if s.get("address") and s["address"] not in [a for a, _ in opts]:
        opts.append((s["address"], f"{s['address']} (not heard now)"))
    addrs = "".join(f"<option value='{e(a)}'{' selected' if s.get('address', '') == a else ''}>{e(t)}</option>" for a, t in opts)
    heard = ("<small>The sensor service is starting, or Bluetooth isn't working.</small>" if found is None else
             f"<small>Sensors heard: {len(found)}.</small>" if found else
             "<small>No Mopeka sensor heard yet: check it's mounted under the tank and awake (its button), and within about 10 m of the box.</small>")
    return f"""<fieldset id="propane"><legend>Propane</legend>
<small>Tank level from a Mopeka Pro Check under the tank, over Bluetooth. Use per day, days to empty, fill-ups and low-tank
alerts. <a href="/propane">Propane page</a></small>
<div class="row"><label>Tank <select name="pr_tank">{types}</select></label>
<label>Sensor <select name="pr_address">{addrs}</select></label></div>{heard}
<div class="row"><label>Warn below (%) <input name="pr_warn" type="number" min="5" max="60" value="{s['warn']}"></label>
<label>Critical below (%) <input name="pr_critical" type="number" min="2" max="50" value="{s['critical']}"></label>
<label>Keep in reserve (%) <input name="pr_reserve" type="number" min="0" max="40" value="{s['reserve']}"></label></div>
<small>Days to empty count down to the reserve. Most suppliers want an order in by 20%.</small>
<label>Propane price ($ per gallon) <input name="pr_price" value="{e(str(s.get('price') or ''))}" placeholder="the fuel price under Prices"></label>
</fieldset>"""


def _int(form, k, lo, hi):
    v = form.get(k, "").strip()
    if not re.match(r"^\d+$", v) or not lo <= int(v) <= hi:
        raise FieldError(k, f"a whole number from {lo} to {hi}")
    return int(v)


def form_check(stack, form):
    if form.get("pr_tank") not in engine.CAPACITY:
        raise FieldError("pr_tank", "pick a tank")
    a = form.get("pr_address", "").strip()
    if a and not ADDR_RE.match(a):
        raise FieldError("pr_address", "a sensor address like E4:C4:15:4B:B6:9E")
    warn, crit = _int(form, "pr_warn", 5, 60), _int(form, "pr_critical", 2, 50)
    _int(form, "pr_reserve", 0, 40)
    if crit >= warn:
        raise FieldError("pr_critical", "the critical level must be under the warning level")
    p = form.get("pr_price", "").strip()
    if p and not re.match(r"^\d+(\.\d+)?$", p):
        raise FieldError("pr_price", "a price like 2.49")


def form_save(stack, form):
    import modules
    cur = settings(stack)
    new = {"tank_type": form["pr_tank"], "address": form.get("pr_address", "").strip().upper(),
           "warn": _int(form, "pr_warn", 5, 60), "critical": _int(form, "pr_critical", 2, 50),
           "reserve": _int(form, "pr_reserve", 0, 40), "price": form.get("pr_price", "").strip()}
    modules.save(stack, "propane", new)
    if (new["tank_type"], new["address"]) != (cur["tank_type"], cur["address"]):
        ss.write_env(stack, ss.ensure_secrets(stack))
        start(stack)
        return ["propane sensor service restarted with the new tank"]
    return []


def card(stack):
    s = settings(stack)
    v = engine.summary(stack, s)
    if v["percent"] is None:
        return "<div class='card'><b>Propane</b>: no reading yet. <a href='/setup#propane'>settings</a></div>"
    left = f", about {v['days_left']} days to the reserve" if v["days_left"] is not None else ""
    warn = " <b>(low)</b>" if v["alert"] else ""
    return (f"<div class='card'><b>Propane</b>: {v['percent']:.0f}%{warn} ({_fmt(v['gallons'], ' gal')}){left}. "
            f"<a href='/propane'>use and fill-ups</a></div>")


def page(stack, form):
    s = settings(stack)
    v = engine.summary(stack, s)
    price = _price(stack, s)
    def cost(g):
        return "" if price is None or g is None else f" (${g * price:,.2f})"
    when = lambda t: time.strftime("%b %d %H:%M", time.localtime(t))  # noqa: E731
    lr = v["last_reading"]
    days = "".join(f"<tr><td>{d['date']}</td><td>{d['used']:g} gal{cost(d['used'])}</td><td>{d['filled']:g}</td>"
                   f"<td>{_fmt(d.get('hdd'), '', 1)}</td></tr>" for d in reversed(v["days"][-31:])) or "<tr><td colspan=4>No full day yet.</td></tr>"
    fills = "".join(f"<tr><td>{when(f['at'])}</td><td>+{f['added']:g} gal{cost(f['added'])}</td><td>{f['before']:g} &rarr; {f['after']:g} gal "
                    f"({f['percent_after']:g}%)</td></tr>" for f in reversed(v["fills"])) or "<tr><td colspan=3>None yet.</td></tr>"
    month = sum(d["used"] for d in v["days"][-30:])
    body = f"""<h1>Propane</h1>
<p><b>{_fmt(v['percent'], '%')}</b> ({_fmt(v['gallons'], ' gal')}) &middot; {('about <b>' + str(v['days_left']) + ' days</b> to the ' + str(s['reserve']) + '% reserve') if v['days_left'] is not None else 'days to empty: after a week of readings'}
&middot; today {_fmt(v['used_today'], ' gal', 1)}, yesterday {_fmt(v['used_yesterday'], ' gal', 1)} &middot; last 30 days {month:,.0f} gal{cost(month)}</p>
<p><small>Sensor {html.escape(str(lr.get('address') or ''))}: read quality {lr.get('sensorReadQuality', '&ndash;')} of 3,
tank {_fmt(lr.get('tempCelsius'), ' &deg;C')}, battery {_fmt(lr.get('batteryVoltage'), ' V', 2)},
last reading {('%d s ago' % v['reading_age']) if v['reading_age'] is not None else 'none'}.</small></p>
<h2>Fill-ups</h2><table><tr><th>When</th><th>Added</th><th>Level</th></tr>{fills}</table>
<h2>Use per day</h2><table><tr><th>Day</th><th>Used</th><th>Filled (gal)</th><th>Degree days</th></tr>{days}</table>
<p><small>Use per day is the drop in level plus what was filled that day. Days to empty use heating degree days (65 &deg;F)
once there are 14 days to learn from: use = a base amount + an amount per degree day, against the coming week's forecast.</small></p>"""
    return Response(title="Propane", body=body)


def ha_fields(stack, site):
    v = engine.summary(stack, settings(stack))
    return {"propane_level_percent": v["percent"], "propane_gallons": v["gallons"], "propane_days_left": v["days_left"],
            "propane_used_today": v["used_today"], "propane_used_yesterday": v["used_yesterday"],
            "propane_low": bool(v["alert"]), "propane_last_fill": (time.strftime("%Y-%m-%d", time.localtime(v["last_fill"]["at"]))
                                                                  if v["last_fill"] else None)}


def every_minute(stack):
    return sample(stack)


MODULE = Module(
    id="propane",
    title="Propane",
    description="tank level from a Mopeka sensor: use, days to empty, fill-ups, low-tank alerts",
    order=60,
    default_enabled=False,
    defaults=engine.DEFAULTS,
    on_enable=start,
    on_disable=stop,
    form_section=form_section,
    form_check=form_check,
    form_save=form_save,
    card=card,
    nav=("/propane", "Propane", "Propane"),
    routes={("GET", "/propane"): page},
    ha_fields=ha_fields,
    compose_profiles=lambda stack: ["propane"],
    env=env,
    every={60: (every_minute, 60)},
)
