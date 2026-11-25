"""Freeze protection: an alert when a watched temperature gets close to freezing, and earlier when a hard freeze is
forecast while the heat or the power is in doubt (monitoring only). The work is setup/freeze.py; this is its
switch, settings, card and schedule."""
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request

import freeze as engine
import hastates
import notify
import solar_setup as ss
from modules import FieldError, Module

VM = os.environ.get("SOLAR_VM", "http://127.0.0.1:8428")


def settings(stack):
    import modules
    return {**engine.DEFAULTS, **modules.settings(stack, "freeze")}


def chosen(stack):
    return sorted(set(settings(stack)["sensors"]))


def _site(stack):
    return ss.load_json(os.path.join(stack, "state", "site.json"), {})


def _deg(c, us):
    """A stored °C value as the form shows it."""
    return f"{c * 9 / 5 + 32:.0f}" if us else f"{c:.1f}".rstrip("0").rstrip(".")


# --------------------------------------------------------------------------
# Setup page
# --------------------------------------------------------------------------
def form_section(stack):
    s = settings(stack)
    e = html.escape
    us = engine.us(_site(stack))
    unit = "°F" if us else "°C"
    known = hastates.known(stack, ("sensor",), "temperature") + hastates.known(stack, ("climate",))
    opts = "".join(f"<option value='{e(k['entity_id'])}'>{e(k['name'])}</option>" for k in known)
    now = {r["entity"]: r for r in engine.readings(stack, s, _site(stack))}
    rows = []
    for i in range(engine.MAX_SENSORS):
        eid = s["sensors"][i] if i < len(s["sensors"]) else ""
        r = now.get(eid)
        val = "" if not r else f" <small>now {engine.show(r['c'], us)}</small>" if r["ok"] else " <small>no reading</small>"
        rows.append(f"<div class='row'><label>Temperature {i + 1} <input name='fz_sensor{i + 1}' value='{e(eid)}' "
                    f"list='fz_temps' style='min-width:22em' placeholder='sensor or thermostat entity'></label>{val}</div>")
    return f"""<fieldset id="freeze"><legend>Freeze protection</legend>
<small>Watches up to {engine.MAX_SENSORS} temperatures from Home Assistant (a slab or pipe probe, a room sensor, a
thermostat) and sends a critical alert when one stays below the threshold for 5 minutes. With the weather module on, it
also warns hours earlier: when a hard freeze is forecast in the next 24 hours while the boiler module reports a fault or
the battery is low. Monitoring only: nothing is switched.</small>
<datalist id="fz_temps">{opts}</datalist>
{''.join(rows)}
<div class="row"><label>Alert below ({unit}) <input name="fz_below" type="number" step="any" value="{_deg(s['below_c'], us)}"></label>
<label>Hard freeze: forecast at or below ({unit}) <input name="fz_hard" type="number" step="any" value="{_deg(s['hard_freeze_c'], us)}"></label></div>
<div class="row"><label><input type="checkbox" name="fz_alerts"{' checked' if s.get('alerts') else ''}> Send alerts</label></div>
<small>Changing which entities are watched restarts Home Assistant once (about a minute).</small>
</fieldset>"""


def _parse(stack, form):
    us = engine.us(_site(stack))
    sensors = []
    for i in range(1, engine.MAX_SENSORS + 1):
        v = (form.get(f"fz_sensor{i}") or "").strip().lower()
        if not v:
            continue
        if not hastates.ENTITY_RE.match(v) or v.split(".", 1)[0] not in ("sensor", "climate"):
            raise FieldError(f"fz_sensor{i}", "a sensor or thermostat entity such as sensor.garage_slab_temperature")
        if v not in sensors:
            sensors.append(v)

    def temp(k, default_c, lo_c, hi_c, what):
        v = (form.get(k) or "").strip()
        if not v:
            return default_c
        if not re.match(r"^-?\d+(\.\d+)?$", v):
            raise FieldError(k, what)
        c = round(engine.to_c(float(v), "°F" if us else "°C"), 2)
        if not lo_c <= c <= hi_c:
            raise FieldError(k, what)
        return c
    below = temp("fz_below", engine.DEFAULTS["below_c"], 0.5, 20,
                 "a temperature from 33 to 68 °F" if us else "a temperature from 0.5 to 20 °C")
    hard = temp("fz_hard", engine.DEFAULTS["hard_freeze_c"], -40, 0,
                "a temperature from -40 to 32 °F" if us else "a temperature from -40 to 0 °C")
    return {"sensors": sensors, "below_c": below, "hard_freeze_c": hard, "alerts": bool(form.get("fz_alerts"))}


def form_check(stack, form):
    _parse(stack, form)


def form_save(stack, form):
    import modules
    new = _parse(stack, form)
    before = chosen(stack)
    modules.save(stack, "freeze", new)
    if chosen(stack) != before and hastates.apply(stack):
        return ["Home Assistant restarting for the freeze protection's entities"]
    return []


# --------------------------------------------------------------------------
# Card, Home Assistant, display
# --------------------------------------------------------------------------
def _alert_text(v):
    return ", ".join(engine.RULES[r][2] for r in engine.RULES if r in v["active"])


def card(stack):
    s = settings(stack)
    if not s["sensors"]:
        return "<div class='card'><b>Freeze protection</b>: pick the temperatures to watch in <a href='/setup#freeze'>setup</a>.</div>"
    site = _site(stack)
    us = engine.us(site)
    v = engine.summary(stack, s, site)
    temps = ", ".join(f"{html.escape(r['name'])} {engine.show(r['c'], us) if r['ok'] else 'no reading'}" for r in v["readings"])
    low = v["forecast_low"]
    fc = f" Forecast low in the next 24 hours: {engine.show(low[0], us)}." if low else ""
    warn = "".join(f"<br><b>{html.escape(engine.RULES[r][2])}</b>: {html.escape(v['text'].get(r, ''))}"
                   for r in engine.RULES if r in v["active"])
    return (f"<div class='card'><b>Freeze protection</b>: {temps}; alert below {engine.show(s['below_c'], us)}.{fc}{warn}</div>")


def ha_fields(stack, site):
    s = settings(stack)
    v = engine.summary(stack, s, site)
    low, fc = v["lowest"], v["forecast_low"]
    return {"freeze_alert": _alert_text(v) or "none",
            "freeze_alert_text": " ".join(v["text"].get(r, "") for r in engine.RULES if r in v["active"]),
            "freeze_cold": "cold" in v["active"], "freeze_risk": "risk" in v["active"],
            "freeze_lowest_c": low and low["c"], "freeze_lowest_name": low["name"] if low else "",
            "freeze_forecast_low_c": fc and fc[0], "freeze_below_c": s["below_c"]}


def display_panel(stack, site):
    s = settings(stack)
    if not s["sensors"]:
        return {"title": "Freeze protection", "level": "warn", "note": "Pick the temperatures to watch in setup", "tiles": []}
    us = engine.us(site)
    v = engine.summary(stack, s, site)
    level = "bad" if "cold" in v["active"] else "warn" if v["active"] else ""
    tiles = [{"k": r["name"], "v": engine.show(r["c"], us) if r["ok"] else "--",
              "tone": "muted" if not r["ok"] else "bad" if r["c"] < s["below_c"] else "fg",
              "s": f"alert below {engine.show(s['below_c'], us)}" if r["ok"] else "no reading"} for r in v["readings"]]
    if v["forecast_low"]:
        c, at = v["forecast_low"]
        tiles.append({"k": "Forecast low", "v": engine.show(c, us), "tone": "warn" if c <= s["hard_freeze_c"] else "fg",
                      "s": time.strftime("%a %-I %p" if us else "%a %H:%M", time.localtime(at))})
    note = " ".join(v["text"].get(r, "") for r in engine.RULES if r in v["active"])
    return {"title": "Freeze protection", "level": level, "note": note, "tiles": tiles}


def _soc():
    """The battery's level now (%), or None."""
    try:
        q = urllib.parse.urlencode({"query": ss.hold("max(lxp_soc)")})
        with urllib.request.urlopen(f"{VM}/api/v1/query?{q}", timeout=5) as r:
            res = json.loads(r.read())["data"]["result"]
        return float(res[0]["value"][1]) if res else None
    except (OSError, ValueError, KeyError, IndexError):
        return None


def _boiler_fault(stack, site):
    import modules
    try:
        if modules.load(stack, "boiler")["enabled"]:
            return modules.get("boiler").ha_fields(stack, site).get("boiler_fault") or ""
    except Exception:  # noqa: BLE001 - the boiler module's trouble never stops the freeze check
        pass
    return ""


def every_minute(stack):
    s = settings(stack)
    if not s["sensors"]:
        return []
    site = _site(stack)

    def alert(tag, title, message, priority):
        return notify.alert(stack, tag, title, message, priority)
    return engine.step(stack, s, engine.readings(stack, s, site), site, time.time(), alert, soc=_soc(),
                       boiler_fault=_boiler_fault(stack, site), low=engine.forecast_low(stack))


def checks(stack):
    s = settings(stack)
    link = ("/setup#freeze", "Setup")
    if not s["sensors"]:
        return [{"title": "Freeze protection", "state": "bad", "link": link,
                 "text": "On, but no temperature is picked, so it watches nothing."}]
    site = _site(stack)
    rd = engine.readings(stack, s, site)
    silent = [r["name"] for r in rd if not r["ok"]]
    if silent:
        return [{"title": "Freeze protection", "state": "bad", "link": link,
                 "text": f"No reading from {', '.join(silent)}: check the sensor in Home Assistant."}]
    us = engine.us(site)
    return [{"title": "Freeze protection", "state": "ok", "link": None,
             "text": ", ".join(f"{r['name']} {engine.show(r['c'], us)}" for r in rd) + f"; alert below {engine.show(s['below_c'], us)}."}]


MODULE = Module(
    id="freeze",
    checks=checks,
    title="Freeze protection",
    description="an alert when a slab, pipe or room temperature nears freezing, and earlier when a hard freeze is forecast with the heat or battery in doubt",
    order=63,
    default_enabled=False,
    defaults=engine.DEFAULTS,
    form_section=form_section,
    form_check=form_check,
    form_save=form_save,
    card=card,
    ha_fields=ha_fields,
    ha_entities=chosen,
    display_panel=display_panel,
    every={60: (every_minute, 60)},
)
