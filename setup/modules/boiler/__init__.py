"""Boiler: watches the propane boiler and the floor heating from Home Assistant's probes, plugs and thermostat
(monitoring only for now: faults are logged, nothing is reset). The work is setup/boiler.py; this is its switch,
settings, pages and schedule."""
import html
import re
import time

import boiler as engine
import hastates
import notify
import solar_setup as ss
from modules import FieldError, Module, Response

NEED = {"required": "<b>Required</b>", "recommended": "Recommended", "optional": "Optional"}


def settings(stack):
    import modules
    s = modules.settings(stack, "boiler")
    return {**s, "roles": {**engine.DEFAULTS["roles"], **(s.get("roles") or {})}}


def chosen(stack):
    return sorted({v for v in settings(stack)["roles"].values() if v})


def _us(stack):
    return ss.load_json(f"{stack}/state/site.json", {}).get("units", "us_customary") == "us_customary"


def _t(c, us, digits=1):
    if c is None:
        return "&ndash;"
    return f"{c * 9 / 5 + 32:.{digits}f} °F" if us else f"{c:.{digits}f} °C"


def _dt(c, us):
    """A temperature difference."""
    if c is None:
        return "&ndash;"
    return f"{c * 9 / 5:+.1f} °F" if us else f"{c:+.1f} °C"


def _ago(t, now=None):
    if not t:
        return ""
    s = max(0, int((now or time.time()) - t))
    return f"{s} s ago" if s < 120 else f"{s // 60} min ago" if s < 7200 else f"{s // 3600} h ago"


# --------------------------------------------------------------------------
# Setup page
# --------------------------------------------------------------------------
def _value(stack, role, eid, us):
    if not eid:
        return ""
    rd = engine.reading(role, hastates.get(stack, eid))
    if not rd["ok"]:
        return f" <small>no reading{(' (' + html.escape(str(rd['raw'])) + ')') if rd['raw'] else ''}</small>"
    v, kind = rd["value"], engine.ROLE[role][2]
    text = (_t(v, us) if kind == "temperature" else f"{v:.0f} W" if kind == "power" else f"{v:.2f} A" if kind == "current"
            else ("calling" if v else "not calling") if kind == "climate" and v is not None
            else html.escape(str(rd["raw"])) if kind == "climate" else "on" if v else "off")
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
            lists[kind] = f"<datalist id='bo_{kind}'>{opts}</datalist>"
        eid = s["roles"].get(role, "")
        rows.append(f"<div class='row'><label>{e(label)} ({NEED[need]}) <input name='bo_{role}' value='{e(eid)}' "
                    f"list='bo_{kind}' style='min-width:22em' placeholder='{e(kind.replace('_', ' '))} entity'></label>"
                    f"{_value(stack, role, eid, us)}<br><small>Gives: {e(gives)}.</small></div>")
    missing = [engine.ROLE[r][1] for r in engine.REQUIRED if not s["roles"].get(r)]
    need = (f"<p><b>Still needed:</b> {e(', '.join(missing))}. The module watches nothing until the three required "
            f"temperatures are chosen.</p>" if missing else "")
    unit = "°F" if us else "°C"
    conv = (lambda c: round(c * 9 / 5 + 32)) if us else (lambda c: round(c, 1))
    lv = engine.levels(engine.load(stack), s)
    learned = (f"learned: controls only about {lv.get('idle', '?')} W, pump {lv.get('pump', '?')} W, burner {lv.get('burner', '?')} W"
               if lv and not lv.get("manual") else "learned from the boiler plug's first day of readings")
    return f"""<fieldset id="boiler"><legend>Boiler</legend>
<small>Watches the boiler and the floor heating from Home Assistant: the probe board's temperatures, the thermostat and the
metering plugs. Pick the Home Assistant entity for each role (start typing to choose); the names on the probe board don't
matter. Three temperatures are required; each other role adds a signal. Monitoring only for now: faults are logged,
nothing is reset. <a href="/boiler">Boiler page</a></small>
{need}{''.join(lists.values())}
{''.join(rows)}
<div class="row"><label><input type="checkbox" name="bo_alerts"{' checked' if s.get('alerts') else ''}> Send alerts (floor too hot,
freeze risk, sensors offline)</label></div>
<div class="row"><label>Floor supply too hot above ({unit}) <input name="bo_floor_max" type="number" step="any" value="{conv(float(s['floor_max_c']))}"></label>
<label>Freeze risk below ({unit}) <input name="bo_freeze" type="number" step="any" value="{conv(float(s['freeze_c']))}"></label></div>
<small>The pump panel is rated for 150 °F (66 °C), and with no mixing valve the floor gets the boiler's own water.</small>
<label>Burner on above (W) <input name="bo_burner_w" value="{e(str(s.get('burner_w') or ''))}" placeholder="{e(learned)}"></label>
<small>Changing which entities are watched restarts Home Assistant once (about a minute).</small>
</fieldset>"""


def _parse(stack, form):
    us = _us(stack)
    roles = {}
    for role, label, kind, _, _ in engine.ROLES:
        v = (form.get(f"bo_{role}") or "").strip().lower()
        if v:
            if not hastates.ENTITY_RE.match(v):
                raise FieldError(f"bo_{role}", "an entity id such as sensor.boiler_probes_floor_supply")
            dom = v.split(".", 1)[0]
            if dom not in engine.KINDS[kind][0]:
                raise FieldError(f"bo_{role}", f"a {' or '.join(engine.KINDS[kind][0])} entity")
        roles[role] = v
    def temp(k, lo_c, hi_c):
        v = (form.get(k) or "").strip()
        if not re.match(r"^-?\d+(\.\d+)?$", v):
            raise FieldError(k, "a temperature")
        c = (float(v) - 32) * 5 / 9 if us else float(v)
        if not lo_c <= c <= hi_c:
            raise FieldError(k, f"between {_t(lo_c, us, 0)} and {_t(hi_c, us, 0)}")
        return round(c, 2)
    out = {"roles": roles, "alerts": bool(form.get("bo_alerts")), "floor_max_c": temp("bo_floor_max", 30, 70),
           "freeze_c": temp("bo_freeze", 0, 20), "burner_w": (form.get("bo_burner_w") or "").strip()}
    if out["burner_w"] and not re.match(r"^\d+(\.\d+)?$", out["burner_w"]):
        raise FieldError("bo_burner_w", "watts, like 120, or empty to learn it")
    return out


def form_check(stack, form):
    _parse(stack, form)


def form_save(stack, form):
    import modules
    new = _parse(stack, form)
    before = chosen(stack)
    modules.save(stack, "boiler", new)
    if chosen(stack) != before and hastates.apply(stack):
        return ["Home Assistant restarting to publish the boiler's entities"]
    return []


# --------------------------------------------------------------------------
# Card, page, Home Assistant
# --------------------------------------------------------------------------
def _headline(stack, s, v, us):
    last = v["last"]
    missing = [engine.ROLE[r][1] for r in engine.REQUIRED if not s["roles"].get(r)]
    if missing:
        return f"pick {html.escape(', '.join(missing))} in <a href='/setup#boiler'>setup</a>"
    if not last:
        return "no reading yet"
    t = last["temps"]
    parts = [f"<b>{html.escape(last['state'])}</b>"]
    if v["fault"]:
        parts.append(f"<b>fault</b>: {html.escape(v['fault'])}")
    if "floor_supply" in t and "floor_return" in t:
        parts.append(f"floor {_t(t['floor_supply'], us, 0)} / {_t(t['floor_return'], us, 0)}")
    if "boiler_supply" in t:
        parts.append(f"boiler {_t(t['boiler_supply'], us, 0)}")
    if v["today"]:
        parts.append(f"burner {v['today'].get('burner_min', 0):.0f} min today")
    return ", ".join(parts)


def card(stack):
    s = settings(stack)
    v = engine.summary(stack, s)
    return f"<div class='card'><b>Boiler</b>: {_headline(stack, s, v, _us(stack))}. <a href='/boiler'>details</a></div>"


def page(stack, form):
    s = settings(stack)
    us = _us(stack)
    v = engine.summary(stack, s)
    e = html.escape
    now = time.time()
    when = lambda t: time.strftime("%b %d %H:%M", time.localtime(t))  # noqa: E731
    last = v["last"] or {}
    temps = last.get("temps") or {}
    rows = ""
    for role, label, kind, need, gives in engine.ROLES:
        eid = s["roles"].get(role)
        cur = hastates.get(stack, eid) if eid else None
        shown = _value(stack, role, eid, us) if eid else " <small>not chosen</small>"
        if role in temps and role in ("floor_return", "boiler_return"):
            shown += f" <small>(corrected {_t(temps[role], us)})</small>"
        rows += (f"<tr><td>{e(label)}</td><td>{NEED[need]}</td><td>{e(eid or '')}</td><td>{shown}</td>"
                 f"<td><small>{_ago((cur or {}).get('at'), now) and 'changed ' + _ago(cur.get('at'), now)}</small></td></tr>")
    offs = ""
    for pair, sup, ret in engine.PAIRS:
        o = v["offsets"].get(pair)
        if not o:
            offs += f"<li>{pair.title()} pair: not learned yet (needs 30 quiet minutes: no call, burner off, temperatures steady)</li>"
            continue
        wide = (f" <b>At rest they last differed by {_dt(o['wide'], us)}, more than a probe's error: check both are in "
                f"place.</b>" if o.get("wide") is not None else "")
        offs += (f"<li>{pair.title()} pair: the return probe reads {_dt(-o['c'], us)} against the supply, from {o['n']} quiet "
                 f"minutes{(', last ' + _ago(o['at'], now)) if o.get('at') else ''}; corrected for.{wide}</li>")
    lv, learned = v["levels"], v["learned_levels"]
    if lv and lv.get("manual"):
        lvl = f"Burner on above {lv['burner_on']:g} W (set in setup)."
    elif learned:
        lvl = (f"Learned from {learned['samples']:,} readings: controls only about {learned['idle']} W, pump {learned['pump']} W, "
               f"pump + fan + burner {learned['burner']} W; burner on above {learned['burner_on']} W.")
    elif s["roles"].get("boiler_power"):
        lvl = (f"Learning: {v['power_samples']:,} of {engine.LEARN_SAMPLES:,} readings (a day), and the burner has to have run "
               f"a few times. Until then, burner runs and the fault rules wait.")
    else:
        lvl = "No boiler plug chosen: burner runs are not known, and the fault rules can't run."
    curve = "".join(f"<tr><td>{_t(float(b), us, 0)} to {_t(float(b) + engine.CURVE_BIN_C, us, 0)}</td><td>{_t(c['c'], us)}</td>"
                    f"<td>{c['n']}</td></tr>" for b, c in sorted(v["curve"].items(), key=lambda x: float(x[0]))) \
        or "<tr><td colspan=3>Nothing yet: needs the boiler plug, the thermostat and 10 minutes of heating.</td></tr>"
    runs = "".join(f"<tr><td>{when(r['start'])}</td><td>{r['minutes']:g} min</td><td>{e(r['class'])}</td>"
                   f"<td>{_t(r.get('supply_start'), us, 0)} &rarr; {_t(r.get('supply_max'), us, 0)}</td>"
                   f"<td>{_t(r.get('outdoor'), us, 0)}</td></tr>" for r in reversed(v["runs"][-40:])) \
        or "<tr><td colspan=5>None yet.</td></tr>"
    days = "".join(f"<tr><td>{d}</td><td>{x['runs']}</td><td>{x['burner_min']:.0f}</td><td>{x['heat_min']:.0f}</td>"
                   f"<td>{x['dhw_min']:.0f}</td><td>{x['faults']}</td></tr>" for d, x in reversed(list(v["days"].items())[-14:])) \
        or "<tr><td colspan=6>None yet.</td></tr>"
    log = "".join(f"<tr><td>{when(x['at'])}</td><td>{e(x['kind'])}</td><td>{e(x['title'])}<br><small>{e(x.get('detail') or '')}</small></td>"
                  f"<td>{e(x['action'])}</td><td>{when(x['cleared']) if x.get('cleared') else '<b>active</b>'}</td></tr>"
                  for x in reversed(v["log"][-50:])) or "<tr><td colspan=5>Nothing yet.</td></tr>"
    since = f"Watching since {when(v['since'])}. " if v["since"] else ""
    body = f"""<h1>Boiler</h1>
<p>{_headline(stack, s, v, us)}{(' &middot; floor difference ' + _dt(last['floor_diff'], us)) if last.get('floor_diff') is not None else ''}
{(' &middot; outdoor ' + _t(last['outdoor'], us, 0)) if last.get('outdoor') is not None else ''}
&middot; call for heat: {'yes' if last.get('calling') else 'no' if last.get('calling') is False else 'unknown'}
&middot; updated {_ago(last.get('t'), now) or 'never'}</p>
<p><small>{since}Monitoring only: the fault rules are logged here and nothing is reset; the reset automation comes after
a week of this has been checked for false faults.</small></p>
<h2>Sensors</h2><table><tr><th>Role</th><th></th><th>Home Assistant entity</th><th>Now</th><th></th></tr>{rows}</table>
<p><small><a href="/setup#boiler">Choose them in setup</a></small></p>
<h2>Learned</h2><ul>{offs}<li>Boiler plug: {lvl}</li></ul>
<h3>Outdoor reset (boiler supply while heating)</h3><table><tr><th>Outdoor</th><th>Boiler supply</th><th>Minutes</th></tr>{curve}</table>
<h2>Burner runs</h2><table><tr><th>Started</th><th>Length</th><th>Kind</th><th>Boiler supply</th><th>Outdoor</th></tr>{runs}</table>
<h2>Per day</h2><table><tr><th>Day</th><th>Runs</th><th>Burner (min)</th><th>Heating (min)</th><th>Hot water (min)</th><th>Faults</th></tr>{days}</table>
<h2>Faults and alerts</h2><table><tr><th>When</th><th>Kind</th><th>What</th><th>Done</th><th>Cleared</th></tr>{log}</table>
<p><small>Fault rules: not running on demand (the thermostat calls, no hot-water draw, the plug at controls only for 15
minutes); running but no heat (burner on 10 minutes, boiler supply up less than 5 °C); stopped during a call (the
burner ran, stopped, and the boiler supply fell to the floor return's temperature for 10 minutes). All of them wait
10 minutes after a hot-water draw.</small></p>"""
    return Response(title="Boiler", body=body)


def ha_fields(stack, site):
    s = settings(stack)
    v = engine.summary(stack, s)
    last = v["last"] or {}
    fresh = v["age"] is not None and v["age"] < 300
    return {"boiler_state": last.get("state") if fresh else None, "boiler_fault": v["fault"] or "",
            "boiler_floor_difference": last.get("floor_diff") if fresh else None,
            "boiler_burner_minutes_today": round(v["today"].get("burner_min", 0)) if v["today"] else 0}


def every_minute(stack):
    s = settings(stack)
    if not any(s["roles"].get(r) for r in engine.REQUIRED):
        return []
    def alert(tag, title, message, priority):
        return notify.alert(stack, tag, title, message, priority)
    now = time.time()
    msgs = engine.step(stack, s, engine.readings(stack, s), now, engine.outdoor_c(stack, now), alert)
    try:
        engine.vm_push(stack)
    except OSError:
        pass
    return msgs


MODULE = Module(
    id="boiler",
    title="Boiler",
    description="watches the boiler and floor heating from Home Assistant's probes, plugs and thermostat",
    order=62,
    default_enabled=False,
    defaults=engine.DEFAULTS,
    form_section=form_section,
    form_check=form_check,
    form_save=form_save,
    card=card,
    nav=("/boiler", "Boiler", "Boiler"),
    routes={("GET", "/boiler"): page},
    ha_fields=ha_fields,
    ha_entities=chosen,
    every={60: (every_minute, 60)},
)
