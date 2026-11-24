"""Water: the water pump, a storage tank and a water meter, from Home Assistant (monitoring only). The work is
setup/water.py; this is its switch, settings, pages and schedule."""
import html
import re
import time

import hastates
import notify
import solar_setup as ss
import water as engine
from modules import FieldError, Module, Response

NEED = {"recommended": "Recommended", "optional": "Optional"}
L_PER_GAL = 3.78541


def settings(stack):
    import modules
    s = modules.settings(stack, "water")
    return {**s, "roles": {**engine.DEFAULTS["roles"], **(s.get("roles") or {})}}


def chosen(stack):
    return sorted({v for v in settings(stack)["roles"].values() if v})


def _us(stack, site=None):
    site = site if site is not None else ss.load_json(f"{stack}/state/site.json", {})
    return site.get("units", "us_customary") == "us_customary"


def _vol(gal, us, digits=0):
    if gal is None:
        return "&ndash;"
    return f"{gal:,.{digits}f} gal" if us else f"{gal * L_PER_GAL:,.{digits}f} L"


def _mins(sec):
    if sec is None:
        return "&ndash;"
    return f"{sec / 3600:.1f} h" if sec >= 3 * 3600 else f"{sec / 60:.0f} min" if sec >= 120 else f"{sec:.0f} s"


# --------------------------------------------------------------------------
# Setup page
# --------------------------------------------------------------------------
def _value(stack, role, eid, s, us):
    if not eid:
        return ""
    rd = engine.reading(role, hastates.get(stack, eid), engine._f(s.get("tank_gal")))
    if not rd["ok"]:
        return f" <small>no reading{(' (' + html.escape(str(rd['raw'])) + ')') if rd['raw'] else ''}</small>"
    v = rd["value"]
    if role == "pump_power":
        text = f"{v:.0f} W ({'running' if v >= engine.ON_W else 'off'})"
    elif role == "water_meter":
        text = f"total {_vol(v, us)}"
    else:
        text = " / ".join(x for x in (f"{v['percent']:.0f}%" if v["percent"] is not None else "",
                                      _vol(v["gal"], us) if v["gal"] is not None else "") if x)
    return f" <small>now {text}</small>"


def form_section(stack):
    s = settings(stack)
    e = html.escape
    us = _us(stack)
    lists, rows = {}, []
    for role, label, kind, need, gives in engine.ROLES:
        if kind not in lists:
            doms, dc = engine.KINDS[kind]
            opts = "".join(f"<option value='{e(k['entity_id'])}'>{e(k['name'])}</option>"
                           for k in hastates.known(stack, doms, dc))
            lists[kind] = f"<datalist id='wa_{kind}'>{opts}</datalist>"
        eid = s["roles"].get(role, "")
        rows.append(f"<div class='row'><label>{e(label)} ({NEED[need]}) <input name='wa_{role}' value='{e(eid)}' "
                    f"list='wa_{kind}' style='min-width:22em' placeholder='sensor entity'></label>"
                    f"{_value(stack, role, eid, s, us)}<br><small>Gives: {e(gives)}.</small></div>")
    vu, ru = ("gal", "GPM") if us else ("L", "L/min")
    conv = (lambda g: g) if us else (lambda g: g * L_PER_GAL)
    def shown(k, digits=1):
        v = engine._f(s.get(k))
        return "" if v is None else f"{conv(v):.{digits}f}".rstrip("0").rstrip(".")
    nw = engine.load(stack).get("normal_w")
    learned = f"usual running power learned: {nw:.0f} W" if nw else f"its usual running power is learned from its first {engine.LEARN_RUNS} runs"
    return f"""<fieldset id="water"><legend>Water</legend>
<small>Watches the water system from Home Assistant: the pump's metering plug, and a storage tank's level and a water meter
if there are any. Every role is optional; pick the Home Assistant entity for the ones the site has (start typing to choose).
Monitoring only: nothing switches the pump. <a href="/water">Water page</a></small>
{''.join(lists.values())}
{''.join(rows)}
<div class="row"><label><input type="checkbox" name="wa_alerts"{' checked' if s.get('alerts') else ''}> Send alerts (pump running
nonstop, low power, overload, rapid cycling, water used all night, tank low, sensors offline)</label></div>
<p style="margin-top:12px"><b>Pump</b> <small style="display:inline">{e(learned)}</small></p>
<div class="row"><label>Nonstop run alert after (minutes) <input name="wa_max_run" type="number" min="2" max="240" value="{e(str(s.get('max_run_min') or 20))}"></label>
<label>Nameplate current (A) <input name="wa_amps" type="number" step="any" min="0" value="{e(str(s.get('pump_amps') or ''))}" placeholder="for the overload alert"></label>
<label>Pump flow ({ru}) <input name="wa_gpm" type="number" step="any" min="0" value="{shown('pump_gpm')}" placeholder="to estimate use from run time"></label></div>
<small>Without a meter or a tank, use per day is estimated from the pump's run time &times; its flow (a bucket timed at a tap
with the pump running is close enough). The overload alert fires above the nameplate current &times; {engine.VOLTS} V.</small>
<p style="margin-top:12px"><b>Storage tank</b> <small style="display:inline">if there is one</small></p>
<div class="row"><label>Capacity ({vu}) <input name="wa_tank_gal" type="number" step="any" min="0" value="{shown('tank_gal', 0)}"></label>
<label>Warn below (%) <input name="wa_tank_warn" type="number" min="5" max="80" value="{e(str(s.get('tank_warn') or 30))}"></label>
<label>Critical below (%) <input name="wa_tank_critical" type="number" min="2" max="60" value="{e(str(s.get('tank_critical') or 15))}"></label></div>
<small>Changing which entities are watched restarts Home Assistant once (about a minute).</small>
</fieldset>"""


def _parse(stack, form):
    us = _us(stack)
    roles = {}
    for role, label, kind, _, _ in engine.ROLES:
        v = (form.get(f"wa_{role}") or "").strip().lower()
        if v:
            if not hastates.ENTITY_RE.match(v):
                raise FieldError(f"wa_{role}", "an entity id such as sensor.well_pump_power")
            if v.split(".", 1)[0] not in engine.KINDS[kind][0]:
                raise FieldError(f"wa_{role}", "a sensor entity")
        roles[role] = v
    def num(k, lo, hi, what, whole=False):
        v = (form.get(k) or "").strip()
        if not v:
            return ""
        if not re.match(r"^\d+$" if whole else r"^\d+(\.\d+)?$", v) or not lo <= float(v) <= hi:
            raise FieldError(k, what)
        return int(v) if whole else float(v)
    to_gal = (lambda x: x) if us else (lambda x: x / L_PER_GAL)
    gpm = num("wa_gpm", 0.1, 200 if us else 750, "the pump's flow per minute, like 8, or empty")
    cap = num("wa_tank_gal", 10, 1000000 if us else 3800000, "the tank's capacity, or empty")
    out = {"roles": roles, "alerts": bool(form.get("wa_alerts")),
           "max_run_min": num("wa_max_run", 2, 240, "minutes from 2 to 240", True) or engine.DEFAULTS["max_run_min"],
           "pump_amps": num("wa_amps", 0.5, 30, "the nameplate amps, like 10, or empty"),
           "pump_gpm": "" if gpm == "" else round(to_gal(gpm), 3),
           "tank_gal": "" if cap == "" else round(to_gal(cap), 1),
           "tank_warn": num("wa_tank_warn", 5, 80, "a whole number from 5 to 80", True) or engine.DEFAULTS["tank_warn"],
           "tank_critical": num("wa_tank_critical", 2, 60, "a whole number from 2 to 60", True) or engine.DEFAULTS["tank_critical"]}
    if out["tank_critical"] >= out["tank_warn"]:
        raise FieldError("wa_tank_critical", "the critical level must be under the warning level")
    return out


def form_check(stack, form):
    _parse(stack, form)


def form_save(stack, form):
    import modules
    new = _parse(stack, form)
    before = chosen(stack)
    modules.save(stack, "water", new)
    if chosen(stack) != before and hastates.apply(stack):
        return ["Home Assistant restarting for the water module's entities"]
    return []


# --------------------------------------------------------------------------
# Card, page, Home Assistant, display
# --------------------------------------------------------------------------
def _pump_text(v):
    p = v["pump"]
    if not p:
        return ""
    if not p.get("known"):
        return "pump: no recent reading"
    return f"pump running for {_mins(p['for_s'])}" if p["on"] else "pump off"


def card(stack):
    s = settings(stack)
    if not any(s["roles"].values()):
        return "<div class='card'><b>Water</b>: pick the pump plug, tank or meter in <a href='/setup#water'>setup</a>.</div>"
    us = _us(stack)
    v = engine.summary(stack, s)
    bits = [b for b in (_pump_text(v),
                        f"used today {_vol(v['use_today'], us)}" if v["use_today"] is not None else "",
                        f"{v['days_left']} days of water left" if v["days_left"] is not None else "") if b]
    warn = " <b>(" + html.escape(", ".join(engine.RULE[n][2] for n in v["active"])) + ")</b>" if v["active"] else ""
    return f"<div class='card'><b>Water</b>: {', '.join(bits) or 'no reading yet'}.{warn} <a href='/water'>details</a></div>"


def page(stack, form):
    s = settings(stack)
    us = _us(stack)
    v = engine.summary(stack, s)
    roles = s["roles"]
    e = html.escape
    when = lambda t: time.strftime("%b %d %H:%M", time.localtime(t))  # noqa: E731
    last = v["last"] or {}
    tank = last.get("tank") or {}
    head = []
    if roles.get("pump_power"):
        head.append(f"<b>{e(_pump_text(v).capitalize())}</b>; today {_mins(v['run_s_today'])} in {v['starts_today']} runs, "
                    f"{v['starts_last_hour']} starts in the last hour")
    if roles.get("tank_level"):
        head.append(f"tank <b>{tank['percent']:.0f}%</b>" + (f" ({_vol(tank['gal'], us)})" if tank.get("gal") is not None else "")
                    if tank.get("percent") is not None else f"tank {_vol(tank.get('gal'), us)}")
    if v["use_today"] is not None:
        src = {"meter": "the meter", "tank": "the tank's level", "pump": "estimated from the pump's run time"}[v["use_source"]]
        head.append(f"used today {_vol(v['use_today'], us)} ({src})")
    if v["usual"] is not None:
        head.append(f"usual {_vol(v['usual'], us)} a day (last {v['usual_days']} days)")
    if v["days_left"] is not None:
        head.append(f"about <b>{v['days_left']} days</b> of water left at that use")
    nw = v["normal_w"]
    learn = (f"Usual running power {nw:.0f} W." if nw else
             f"Usual running power: learned after {engine.LEARN_RUNS} runs of a minute or more.") if roles.get("pump_power") else ""
    active = "".join(f"<li><b>{e(engine.RULE[n][2])}</b> since {when(t)}</li>" for n, t in v["active"].items())
    log = "".join(f"<tr><td>{when(x['at'])}</td><td>{e(x['title'])}</td><td>{e(x['detail'])}</td><td>{e(x['action'])}"
                  f"{(' &middot; cleared ' + when(x['cleared'])) if x.get('cleared') else ''}</td></tr>"
                  for x in reversed(v["log"][-30:])) or "<tr><td colspan=4>Nothing yet.</td></tr>"
    def day_row(d):
        rec = v["days"][d]
        use, _ = engine.use_of(rec, s, roles)
        night = rec["night_meter"] if roles.get("water_meter") else rec["night_starts"]
        return (f"<tr><td>{d}</td><td>{_vol(use, us, 1) if use is not None else '&ndash;'}</td>"
                + (f"<td>{_mins(rec['run_s'])}</td><td>{rec['runs']}</td><td>{_mins(rec['max_run_s'])}</td>" if roles.get("pump_power") else "")
                + (f"<td>{_vol(rec['tank_filled_gal'], us)}</td>" if roles.get("tank_level") else "")
                + f"<td>{len(night)} of 4</td></tr>")
    days = "".join(day_row(d) for d in sorted(v["days"], reverse=True)[:31]) or "<tr><td colspan=7>No day yet.</td></tr>"
    watts = lambda w: "&ndash;" if w is None else f"{w:.0f} W"  # noqa: E731
    runs = "".join(f"<tr><td>{when(r['start'])}</td><td>{_mins(r['s'])}</td><td>{watts(r['w'])}</td></tr>"
                   for r in reversed(v["runs"][-20:])) or "<tr><td colspan=3>None yet.</td></tr>"
    body = f"""<h1>Water</h1>
<p>{' &middot; '.join(head) or 'No reading yet: pick the entities in <a href="/setup#water">setup</a>.'}</p>
{f'<p><b>Now:</b></p><ul>{active}</ul>' if active else ''}
<p><small>{e(learn)}</small></p>
<h2>Days</h2><table><tr><th>Day</th><th>Used</th>{'<th>Pump run</th><th>Runs</th><th>Longest</th>' if roles.get('pump_power') else ''}{'<th>Filled</th>' if roles.get('tank_level') else ''}<th>Night hours with water use</th></tr>{days}</table>
<p><small>Used: {'the meter' if roles.get('water_meter') else 'the tank&#39;s drops' if roles.get('tank_level') and s.get('tank_gal') else 'the pump&#39;s run time &times; its flow' if s.get('pump_gpm') else 'needs a meter, a tank with its capacity, or the pump&#39;s flow'}.
Night hours: of the four hours from 1 to 5 am, how many had {'water through the meter' if roles.get('water_meter') else 'a pump start'}; all four sends the all-night alert.</small></p>
{f'<h2>Last runs</h2><table><tr><th>Start</th><th>Length</th><th>Power</th></tr>{runs}</table>' if roles.get('pump_power') else ''}
<h2>Alerts</h2><table><tr><th>When</th><th>What</th><th>Detail</th><th>Done</th></tr>{log}</table>"""
    return Response(title="Water", body=body)


def ha_fields(stack, site):
    s = settings(stack)
    v = engine.summary(stack, s)
    p = v["pump"] or {}
    tank = (v["last"] or {}).get("tank") or {}
    return {"water_pump_running": bool(p.get("on")) if p else None,
            "water_pump_run_today_min": round(v["run_s_today"] / 60, 1) if p else None,
            "water_pump_starts_today": v["starts_today"] if p else None,
            "water_used_today": v["use_today"], "water_days_left": v["days_left"],
            "water_tank_percent": None if tank.get("percent") is None else round(tank["percent"], 1),
            "water_alert": ", ".join(engine.RULE[n][2] for n in v["active"]) or "none"}


def display_panel(stack, site):
    s = settings(stack)
    if not any(s["roles"].values()):
        return {"title": "Water", "level": "warn", "note": "Pick the water entities in setup", "tiles": []}
    us = _us(stack, site)
    v = engine.summary(stack, s)
    if v["age"] is None or v["age"] > 300:
        return {"title": "Water", "level": "warn", "note": "No water readings for 5 minutes", "tiles": []}
    crit = {"long_run", "tank_critical"}
    level = "bad" if crit & set(v["active"]) else "warn" if v["active"] else ""
    tiles = []
    p = v["pump"]
    if p:
        tiles.append({"k": "Pump", "v": ("Running" if p["on"] else "Off") if p.get("known") else "--",
                      "tone": "bad" if "long_run" in v["active"] else "load" if p["on"] else "fg",
                      "s": f"for {_mins(p['for_s'])}" + (f", {p['w']:.0f} W" if p.get("w") else "") if p["on"]
                      else f"{v['starts_last_hour']} starts in the last hour"})
        tiles.append({"k": "Pump today", "v": _mins(v["run_s_today"]), "s": f"{v['starts_today']} runs"})
    tank = v["last"].get("tank") or {}
    if tank.get("percent") is not None:
        tone = "bad" if "tank_critical" in v["active"] else "warn" if "tank_low" in v["active"] else "fg"
        tiles.append({"k": "Tank", "v": f"{tank['percent']:.0f}%", "tone": tone, "bar": tank["percent"],
                      "s": _vol(tank["gal"], us) if tank.get("gal") is not None else ""})
    if v["days_left"] is not None:
        tiles.append({"k": "Water left", "v": f"{v['days_left']} days", "s": f"at {_vol(v['usual'], us)} a day"})
    if v["use_today"] is not None:
        tiles.append({"k": "Used today", "v": _vol(v["use_today"], us),
                      "s": ("estimated, " if v["use_source"] == "pump" else "")
                      + (f"usual {_vol(v['usual'], us)}" if v["usual"] is not None else "")})
    note = "; ".join(engine.RULE[n][2] for n in v["active"])
    return {"title": "Water", "level": level, "note": note, "tiles": tiles}


def every_minute(stack):
    s = settings(stack)
    if not any(s["roles"].values()):
        return []
    def alert(tag, title, message, priority):
        return notify.alert(stack, tag, title, message, priority)
    msgs = engine.step(stack, s, engine.readings(stack, s), time.time(), alert)
    try:
        engine.vm_push(stack, s)
    except OSError:
        pass
    return msgs


MODULE = Module(
    id="water",
    title="Water",
    description="the water pump, a storage tank and a water meter from Home Assistant: run time, use, leaks, days left",
    order=61,
    default_enabled=False,
    defaults=engine.DEFAULTS,
    form_section=form_section,
    form_check=form_check,
    form_save=form_save,
    card=card,
    nav=("/water", "Water", "Water"),
    routes={("GET", "/water"): page},
    ha_fields=ha_fields,
    ha_entities=chosen,
    display_panel=display_panel,
    every={60: (every_minute, 60)},
)
