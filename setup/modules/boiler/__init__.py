"""Boiler: watches the propane boiler and the floor heating from Home Assistant's probes, plugs and thermostat
(monitoring only for now: faults are logged, nothing is reset). The work is setup/boiler.py; this is its switch,
settings, pages and schedule."""
import html
import json
import os
import re
import secrets
import subprocess
import threading
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
    s = settings(stack)
    ents = {v for v in s["roles"].values() if v}
    if s.get("resets"):
        ents.add(engine.SEASON)              # Home Assistant's "Boiler automatic resets" switch: the box shows it
    return sorted(ents)


# --------------------------------------------------------------------------
# The reset automation (Home Assistant: packages/solar_boiler.yaml)
# --------------------------------------------------------------------------
def token(stack):
    return ss.secret(stack, "boiler_token", lambda: secrets.token_urlsafe(24))


def package(stack, s, port=80):
    """packages/solar_boiler.yaml. Home Assistant does the switching: when the box says a reset may run
    (binary_sensor.site_boiler_reset_allowed) and the "Boiler automatic resets" switch is on, it asks the box to
    start (the box records it, so it counts towards the limits, or refuses), turns the boiler's plug off for 30
    seconds and on again, waits for the box's verdict and says how it went. A second automation turns the plug
    back on if it is ever left off during a reset (Home Assistant restarted in the middle)."""
    js = json.dumps
    sw = (s.get("roles") or {}).get("boiler_switch") or ""
    head = "# Written by the solar monitor (boiler module); edits are overwritten.\n"
    if not s.get("resets") or not hastates.ENTITY_RE.match(sw) or not sw.startswith("switch."):
        return head + "# Automatic boiler resets are off (Setup > Boiler).\n"
    off = engine.RESET_OFF_S
    wait = engine.RESET_OFF_S + engine.HEAT_BACK_S + 5 * 60
    lines = [
        "input_boolean:",
        "  solar_boiler_resets:",
        "    name: Boiler automatic resets",
        "    icon: mdi:restart",
        "rest_command:",
        "  solar_boiler_reset_started:",
        f"    url: \"http://127.0.0.1:{int(port)}/boiler/reset-started\"",
        "    method: POST",
        "    headers:",
        f"      Authorization: {js('Bearer ' + token(stack))}",
        "    timeout: 10",
        "automation:",
        "  - id: solar_boiler_reset",
        "    alias: \"Solar: boiler reset\"",
        "    description: \"Power-cycles the boiler when the solar monitor finds a fault it may reset (limits: 1 per 6 h, 2 per 24 h).\"",
        "    mode: single",
        "    max_exceeded: silent",
        "    triggers:",
        "      - trigger: state",
        "        entity_id: binary_sensor.site_boiler_reset_allowed",
        "        to: \"on\"",
        "    conditions:",
        "      - condition: state",
        "        entity_id: input_boolean.solar_boiler_resets",
        "        state: \"on\"",
        "    actions:",
        "      - action: rest_command.solar_boiler_reset_started",
        "        response_variable: started",
        "        continue_on_error: true",
        "      - condition: template",
        "        value_template: \"{{ started is defined and started.status == 200 }}\"",
        "      - action: script.solar_notify",
        "        data:",
        "          tag: boiler_reset",
        "          title: Boiler reset",
        f"          message: \"{{{{ states('sensor.site_boiler_fault') }}}}: turning the boiler off for {off} seconds and on again.\"",
        "          priority: warning",
        "      - action: switch.turn_off",
        f"        target: {{entity_id: {sw}}}",
        f"      - delay: {{seconds: {off}}}",
        "      - action: switch.turn_on",
        f"        target: {{entity_id: {sw}}}",
        "      - wait_template: \"{{ states('sensor.site_boiler_reset_state') in ['heat back', 'failed'] }}\"",
        f"        timeout: {{seconds: {wait}}}",
        "      - choose:",
        "          - conditions: \"{{ is_state('sensor.site_boiler_reset_state', 'heat back') }}\"",
        "            sequence:",
        "              - action: script.solar_notify",
        "                data:",
        "                  tag: boiler_reset",
        "                  title: Boiler running again",
        "                  message: \"The boiler is heating again after the reset.\"",
        "                  priority: info",
        "        default:",
        "          - action: script.solar_notify",
        "            data:",
        "              tag: boiler_reset",
        "              title: Boiler did not restart",
        "              message: \"No heat came back after the reset. Check the boiler (its display shows the fault code).\"",
        "              priority: critical",
        "  - id: solar_boiler_reset_plug_back_on",
        "    alias: \"Solar: boiler plug back on after a reset\"",
        "    mode: single",
        "    triggers:",
        "      - trigger: state",
        f"        entity_id: {sw}",
        "        to: \"off\"",
        "        for: {minutes: 2}",
        "      - trigger: time_pattern",          # also after a Home Assistant restart in the middle of a reset
        "        minutes: \"/2\"",
        "    conditions:",
        "      - condition: state",
        "        entity_id: sensor.site_boiler_reset_state",
        "        state: resetting",
        "      - condition: state",
        f"        entity_id: {sw}",
        "        state: \"off\"",
        "    actions:",
        "      - action: switch.turn_on",
        f"        target: {{entity_id: {sw}}}",
    ]
    return head + "\n".join(lines) + "\n"


def apply_package(stack, on=None):
    """Write packages/solar_boiler.yaml; restart Home Assistant (in the background) if it changed. True if so.
    on: whether the module is on (None: as it is now; on_disable runs while it still is)."""
    import modules
    s = settings(stack)
    if on is None:
        on = any(m.id == "boiler" for m in modules.enabled(stack))
    p = os.path.join(stack, "homeassistant", "packages", "solar_boiler.yaml")
    if not os.path.isdir(os.path.dirname(p)):
        return False
    if not ss.write_if_changed(p, package(stack, s if on else {**s, "resets": False}), 0o644):
        return False
    def run():
        try:
            subprocess.run(ss.compose_cmd(stack) + ["restart", "homeassistant"], capture_output=True, text=True)
        except OSError:
            pass
    threading.Thread(target=run, daemon=True).start()
    return True


def reset_started(stack, form):
    """POST /boiler/reset-started from Home Assistant's reset automation (its token in the Authorization header)."""
    if not secrets.compare_digest(form.get("_bearer", ""), token(stack)):
        return Response(403, body="forbidden", ctype="text/plain")
    why = engine.start_reset(stack)
    return Response(409, body=why, ctype="text/plain") if why else Response(body="ok", ctype="text/plain")


def clear(stack, form):
    """POST /boiler/clear: the reset limit is cleared (the boiler has been checked)."""
    engine.note_request(stack, "cleared")
    return Response(location="/boiler")


def _on_enable(stack):
    apply_package(stack, True)


def _on_disable(stack):
    if os.path.exists(os.path.join(stack, "homeassistant", "packages", "solar_boiler.yaml")):
        apply_package(stack, False)


def _us(stack, site=None):
    site = site if site is not None else ss.load_json(f"{stack}/state/site.json", {})
    return site.get("units", "us_customary") == "us_customary"


def _btu(v):
    if v is None:
        return "&ndash;"
    return f"{v / 1000:,.1f}k BTU/h" if v < 100000 else f"{v / 1000:,.0f}k BTU/h"


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
    days = (time.time() - (engine.load(stack).get("since") or time.time())) / 86400
    resets = (f"<div class='row'><label><input type='checkbox' name='bo_resets'{' checked' if s.get('resets') else ''}> "
              f"Automatic resets: Home Assistant turns the boiler's plug off and on when it finds a fault (at most 1 per 6 hours, "
              f"2 per 24 hours)</label></div><small>Needs the boiler plug's switch and power, and the thermostat. In Home "
              f"Assistant, the <i>Boiler automatic resets</i> switch turns them off for the summer. "
              + (f"<b>The boiler has been watched for {days:.1f} days: check a week of the fault log on the boiler page for "
                 f"false faults before turning this on.</b>" if days < 7 else f"Watched for {days:.0f} days.")
              + "</small>")
    fluids = "".join(f"<option value='{k}'{' selected' if s.get('fluid', 'water') == k else ''}>{e(t)} ({f})</option>"
                     for k, t, f in engine.FLUIDS)
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
{resets}
<div class="row"><label><input type="checkbox" name="bo_alerts"{' checked' if s.get('alerts') else ''}> Send alerts (floor too hot,
freeze risk, sensors offline)</label></div>
<div class="row"><label>Floor supply too hot above ({unit}) <input name="bo_floor_max" type="number" step="any" value="{conv(float(s['floor_max_c']))}"></label>
<label>Freeze risk below ({unit}) <input name="bo_freeze" type="number" step="any" value="{conv(float(s['freeze_c']))}"></label></div>
<small>The pump panel is rated for 150 °F (66 °C), and with no mixing valve the floor gets the boiler's own water.</small>
<label>Burner on above (W) <input name="bo_burner_w" value="{e(str(s.get('burner_w') or ''))}" placeholder="{e(learned)}"></label>
<div class="row"><label>Floor flow (GPM) <input name="bo_floor_gpm" type="number" step="any" min="0" value="{e(str(s.get('floor_gpm') or ''))}" placeholder="the manifold's flow meters added up"></label>
<label>Heating fluid <select name="bo_fluid">{fluids}</select></label></div>
<small>With the floor flow (the total of the manifold's loop flow meters while the floors heat), the box estimates the heat
delivered to the floor: factor &times; GPM &times; the floor difference in °F (water 500; glycol less).</small>
<p style="margin-top:12px"><b>Efficiency and sizing</b> <small style="display:inline">entered once; the rest is measured</small></p>
<div class="row"><label>Boiler heating input, lowest (BTU/h) <input name="bo_min_btuh" type="number" min="0" value="{e(str(s.get('boiler_min_btuh') or ''))}"></label>
<label>highest (BTU/h) <input name="bo_max_btuh" type="number" min="0" value="{e(str(s.get('boiler_max_btuh') or ''))}"></label></div>
<div class="row"><label>Heated floor area (ft²) <input name="bo_area" type="number" min="0" value="{e(str(s.get('floor_area_ft2') or ''))}"></label>
<label>Indoor temperature aimed for ({unit}) <input name="bo_indoor" type="number" step="any" value="{conv(float(s.get('indoor_c') or 20))}"></label>
<label>Design outdoor temperature ({unit}) <input name="bo_design" type="number" step="any" value="{'' if s.get('design_c') in (None, '') else conv(float(s['design_c']))}" placeholder="the local heating design temperature"></label></div>
<small>The design temperature is the cold the heating is sized for (the local code's figure, about the coldest 1% of
hours). With it, the boiler page compares the heat the house needs then with the boiler and the slab.</small>
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
    gpm = (form.get("bo_floor_gpm") or "").strip()
    if gpm and (not re.match(r"^\d+(\.\d+)?$", gpm) or not 0.1 <= float(gpm) <= 100):
        raise FieldError("bo_floor_gpm", "gallons per minute, like 4.5, or empty")
    fluid = form.get("bo_fluid") or "water"
    if fluid not in engine.FLUID:
        raise FieldError("bo_fluid", "pick the heating fluid")
    out.update(floor_gpm=gpm, fluid=fluid, resets=bool(form.get("bo_resets")))
    def whole(k, lo, hi, blank_ok=True):
        v = (form.get(k) or "").strip()
        if not v and blank_ok:
            return ""
        if not re.match(r"^\d+$", v) or not lo <= int(v) <= hi:
            raise FieldError(k, f"a whole number from {lo:,} to {hi:,}")
        return int(v)
    out.update(boiler_min_btuh=whole("bo_min_btuh", 1000, 1000000), boiler_max_btuh=whole("bo_max_btuh", 1000, 1000000),
               floor_area_ft2=whole("bo_area", 50, 100000),
               indoor_c=temp("bo_indoor", 10, 30) if (form.get("bo_indoor") or "").strip() else engine.DEFAULTS["indoor_c"],
               design_c=temp("bo_design", -50, 15) if (form.get("bo_design") or "").strip() else "")
    if out["boiler_min_btuh"] and out["boiler_max_btuh"] and out["boiler_min_btuh"] > out["boiler_max_btuh"]:
        raise FieldError("bo_min_btuh", "the lowest input must be under the highest")
    if out["resets"]:
        missing = [engine.ROLE[k][1] for k in ("boiler_switch", "boiler_power") if not roles.get(k)]
        if not roles.get("thermostat") and not roles.get("heat_call"):
            missing.append("Thermostat (or the wired call for heat)")
        if missing:
            raise FieldError("bo_resets", "automatic resets need: " + ", ".join(missing))
    return out


def form_check(stack, form):
    _parse(stack, form)


def form_save(stack, form):
    import modules
    new = _parse(stack, form)
    before = chosen(stack)
    modules.save(stack, "boiler", new)
    restarted = chosen(stack) != before and hastates.apply(stack)
    if apply_package(stack) or restarted:
        return ["Home Assistant restarting for the boiler's entities and reset automation"]
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
{(' &middot; heat to the floor about ' + _btu(last['output']) + ', ' + format(v['today'].get('heat_kbtu', 0), ',.0f') + 'k BTU today') if last.get('output') is not None else ''}
&middot; burner {v['burner_last_hour']:g} min in the last hour
&middot; updated {_ago(last.get('t'), now) or 'never'}</p>
<p><small>{since}Monitoring only: the fault rules are logged here and nothing is reset; the reset automation comes after
a week of this has been checked for false faults.</small></p>
<h2>Sensors</h2><table><tr><th>Role</th><th></th><th>Home Assistant entity</th><th>Now</th><th></th></tr>{rows}</table>
<p><small><a href="/setup#boiler">Choose them in setup</a></small></p>
<h2>Learned</h2><ul>{offs}<li>Boiler plug: {lvl}</li></ul>
<h3>Outdoor reset (boiler supply while heating)</h3><table><tr><th>Outdoor</th><th>Boiler supply</th><th>Minutes</th></tr>{curve}</table>
{_resets_html(s, v, us, when)}
{_efficiency_html(stack, s, us)}
<h2>Burner runs</h2><table><tr><th>Started</th><th>Length</th><th>Kind</th><th>Boiler supply</th><th>Outdoor</th></tr>{runs}</table>
<h2>Per day</h2><table><tr><th>Day</th><th>Runs</th><th>Burner (min)</th><th>Heating (min)</th><th>Hot water (min)</th><th>Faults</th></tr>{days}</table>
<h2>Faults and alerts</h2><table><tr><th>When</th><th>Kind</th><th>What</th><th>Done</th><th>Cleared</th></tr>{log}</table>
<p><small>Fault rules: not running on demand (the thermostat calls, no hot-water draw, the plug at controls only for 15
minutes); running but no heat (burner on 10 minutes, boiler supply up less than 5 °C); stopped during a call (the
burner ran, stopped, and the boiler supply fell to the floor return's temperature for 10 minutes). All of them wait
10 minutes after a hot-water draw.</small></p>"""
    return Response(title="Boiler", body=body)


def _resets_html(s, v, us, when):
    e = html.escape
    rs, last = v["resets"], v["last"] or {}
    if not s.get("resets"):
        return "<h2>Resets</h2><p>Off: faults are logged only. <a href='/setup#boiler'>Turn on in setup</a>.</p>"
    status = ("<b>The reset limit was reached</b>: faults only send alerts until the boiler has been checked. "
              "<form method='post' action='/boiler/clear' style='display:inline'><button>Clear</button></form>"
              if rs.get("locked") else
              f"Waiting for Home Assistant to reset: {e(last.get('reset_fault', ''))}" if last.get("reset_allowed") else
              f"Not resetting: {e(last['reset_why'])}" if last.get("reset_why") else "No fault.")
    rows = "".join(f"<tr><td>{when(a['at'])}</td><td>{e(a.get('fault') or '')}</td><td>{e(a.get('result') or 'running')}</td>"
                   f"<td>{_t(a.get('supply0'), us, 0)} &rarr; {_t((a.get('after') or {}).get('boiler_supply'), us, 0)}</td></tr>"
                   for a in reversed(rs["attempts"][-20:])) or "<tr><td colspan=4>None yet.</td></tr>"
    return (f"<h2>Resets</h2><p>{status} State: {e(last.get('reset_state', 'idle'))}.</p>"
            f"<table><tr><th>When</th><th>Fault</th><th>Result</th><th>Boiler supply</th></tr>{rows}</table>"
            f"<p><small>At most {engine.LIMITS[0][1]} reset per 6 hours and {engine.LIMITS[1][1]} per 24 hours; once the limit is "
            f"reached, faults only alert until <i>Clear</i> or 24 hours without a fault. Heat is back when the burner runs, the "
            f"boiler supply has risen 5 °C and the floor supply is climbing, within 20 minutes. Never while propane is under "
            f"10%, a sensor is offline or Home Assistant's <i>Boiler automatic resets</i> switch is off.</small></p>")


def ha_fields(stack, site):
    s = settings(stack)
    v = engine.summary(stack, s)
    last = v["last"] or {}
    fresh = v["age"] is not None and v["age"] < 300
    return {"boiler_state": last.get("state") if fresh else None, "boiler_fault": v["fault"] or "",
            "boiler_floor_difference": last.get("floor_diff") if fresh else None,
            "boiler_burner_minutes_today": round(v["today"].get("burner_min", 0)) if v["today"] else 0,
            "boiler_heat_output": last.get("output") if fresh else None,
            # the reset automation's inputs (packages/solar_boiler.yaml); never allowed from a stale state
            "boiler_reset_allowed": bool(fresh and last.get("reset_allowed")
                                         and time.time() - engine.reset_requests(stack).get("started", 0)
                                         > engine.RESET_OFF_S + engine.HEAT_BACK_S),
            "boiler_reset_state": last.get("reset_state", "idle") if fresh else "idle",
            "boiler_reset_blocked": last.get("reset_why", "") if fresh else "",
            "boiler_heat_today": round(v["today"].get("heat_kbtu", 0)) if v["today"] and str(s.get("floor_gpm") or "").strip() else None,
            **_week_fields(stack, s)}


def _week_fields(stack, s):
    w = analysis(stack, s)["week"]
    return {"boiler_heat_loss": w["heat_loss_btuh_f"], "boiler_efficiency": w["efficiency_pct"]}


def _price(stack):
    import modules
    from modules import propane as pmod
    try:
        return pmod._price(stack, modules.settings(stack, "propane"))
    except (ValueError, TypeError):
        return None


def analysis(stack, s, now=None):
    """Finished days (with the propane split when the propane module is on), the last 7 and 28 days' figures,
    the sizing check and short cycling."""
    now = now or time.time()
    st = engine.load(stack)
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    pr = _propane(stack)
    used = {d["date"]: d["used"] for d in (pr or {}).get("days") or []}
    days = [engine.day_figures(k, d, used.get(k)) for k, d in sorted(st["days"].items()) if k < today]
    price = _price(stack) if pr is not None else None
    week, month = engine.efficiency(days, s, 7, price), engine.efficiency(days, s, 28, price)
    ua = month["heat_loss_btuh_f"] or week["heat_loss_btuh_f"]
    return {"days": days, "week": week, "month": month, "sizing": engine.sizing(s, ua, month["efficiency_pct"]),
            "cycling": engine.short_cycling(st["runs"], now), "propane_on": pr is not None, "price": price}


def _efficiency_html(stack, s, us):
    a = analysis(stack, s)
    e = html.escape
    dd = lambda c: "&ndash;" if c is None else (f"{c * 9 / 5 + 32:.0f}" if us else f"{c:.1f}")  # noqa: E731
    def fig(w):
        loss = (f"{w['heat_loss_btuh_f']:,} BTU/h per °F" if w["heat_loss_btuh_f"] is not None else
                f"&ndash; <small>({w['loss_days']} of 3 days)</small>")
        eff = (f"{w['efficiency_pct']:g}%" if w["efficiency_pct"] is not None else
               "&ndash; <small>(needs the propane module)</small>" if not a["propane_on"] else
               f"&ndash; <small>({w['burned_days']} of 3 days)</small>")
        cost = f", ${w['heating_cost_per_day']:,.2f} a day" if w.get("heating_cost_per_day") is not None else ""
        gal = f"{w['heating_gal']:g} gal{cost}" if w["heating_gal"] is not None else "&ndash;"
        hdd = f"{w['gal_per_hdd']:g}" if w["gal_per_hdd"] is not None else "&ndash;"
        return f"<td>{w['heat_mbtu']:,.2f}M BTU</td><td>{loss}</td><td>{eff}</td><td>{gal}</td><td>{hdd}</td>"
    if not str(s.get("floor_gpm") or "").strip():
        return ("<h2>Efficiency and heat output</h2><p>Enter the floor flow (GPM) in <a href='/setup#boiler'>setup</a>: "
                "the heat delivered, the house's heat loss and the boiler's efficiency are all measured from it.</p>")
    sz = a["sizing"]
    if sz:
        slab = (f"; the slab delivers about {sz['slab_btuh'][0]:,} to {sz['slab_btuh'][1]:,} BTU/h" if sz["slab_btuh"] else "")
        verdict = (f"<b>Short: {e(sz['first'])} runs out first</b>" if sz["short"] else
                   f"{e(sz['first']).capitalize()} runs out first, with {sz['margin_pct']}% to spare")
        sizing = (f"<p>On the design day ({_t(float(s['design_c']), us, 0)} outside, {_t(float(s['indoor_c']), us, 0)} inside) "
                  f"the house needs about <b>{sz['need_btuh']:,} BTU/h</b>; the boiler can put out about {sz['boiler_btuh']:,} "
                  f"BTU/h{slab}. {verdict}.</p>")
    else:
        sizing = ("<p><small>Sizing check: needs the heat loss (3 days of heating) and the design outdoor temperature "
                  "(setup).</small></p>")
    cy = a["cycling"]
    cycling = (f"<p>Heating runs in the last 7 days: {cy['runs']}, median {cy['median_min']:g} min, {cy['short_pct']}% under "
               f"10 minutes{' <b>(short cycling: the boiler may be oversized for mild days, or its settings too tight)</b>' if cy['short_pct'] >= 50 else ''}.</p>"
               if cy else "")
    rows = "".join(f"<tr><td>{d['date']}</td><td>{dd(d['outdoor_c'])} / {dd(d['indoor_c'])}</td><td>{'&ndash;' if d['hdd'] is None else d['hdd']}</td>"
                   f"<td>{d['heat_kbtu']:,.0f}k</td><td>{d['heat_min']:.0f}</td>"
                   f"<td>{'&ndash;' if not d['propane'] else format(d['propane']['heating'], 'g')}</td></tr>"
                   for d in reversed(a["days"][-14:])) or "<tr><td colspan=6>No finished day yet.</td></tr>"
    return f"""<h2>Efficiency and heat output</h2>
<table><tr><th></th><th>Heat to the floor</th><th>Heat loss of the house</th><th>Boiler heating efficiency</th><th>Propane for heating</th><th>Gal per degree day</th></tr>
<tr><td>Last 7 days</td>{fig(a['week'])}</tr><tr><td>Last 28 days</td>{fig(a['month'])}</tr></table>
{sizing}{cycling}
<table><tr><th>Day</th><th>Outdoor / indoor ({'°F' if us else '°C'})</th><th>Degree days (65 °F)</th><th>Heat (BTU)</th><th>Heating (min)</th><th>Propane for heating (gal)</th></tr>{rows}</table>
<p><small>Heat loss: the heat delivered over the indoor-outdoor difference, on days at least 5 °F apart; sun and people
add heat the boiler didn't, so it reads a little low on sunny days. Efficiency: the heat delivered over the propane
burned for heating (the day's tank drop split by burner time: heating, hot water, the rest; {engine.PROPANE_BTU_PER_GAL:,}
BTU per gallon); it includes the pipe losses between the boiler and the manifold. Expect &plusmn;10 to 15% from the
uncalibrated flow meters and probes; good for following trends.</small></p>"""


def _propane(stack):
    """The propane module's days left and level, when it is on."""
    import modules
    if not any(m.id == "propane" for m in modules.enabled(stack)):
        return None
    import propane
    return propane.summary(stack, modules.settings(stack, "propane"))


def display_panel(stack, site):
    s = settings(stack)
    us = _us(stack, site)
    t = lambda c: "--" if c is None else (f"{c * 9 / 5 + 32:.0f}°" if us else f"{c:.0f}°")  # noqa: E731
    d = lambda c: "" if c is None else (f"{c * 9 / 5:+.0f}°F" if us else f"{c:+.1f}°C")       # noqa: E731
    missing = [engine.ROLE[r][1] for r in engine.REQUIRED if not s["roles"].get(r)]
    if missing:
        return {"title": "Boiler", "level": "warn", "note": "Pick the boiler's sensors in setup: " + ", ".join(missing), "tiles": []}
    v = engine.summary(stack, s)
    last = v["last"] or {}
    if not last or v["age"] is None or v["age"] > 300:
        return {"title": "Boiler", "level": "warn", "note": "No boiler readings for 5 minutes", "tiles": []}
    temps, today = last.get("temps") or {}, v["today"] or {}
    state = last.get("state", "")
    level = "bad" if v["fault"] or "freeze" in v["active"] else "warn" if state in ("offline", "unclear") or v["active"] else ""
    tiles = [{"k": "Boiler", "v": state.capitalize(), "tone": "bad" if v["fault"] else "warn" if state == "heating"
              else "load" if state == "hot water" else "fg",
              "s": f"{today.get('runs', 0)} runs, {today.get('burner_min', 0):.0f} min today"},
             {"k": "Burner, last hour", "v": f"{v['burner_last_hour'] / 60 * 100:.0f}%", "bar": v["burner_last_hour"] / 60 * 100,
              "s": f"today: heat {today.get('heat_min', 0):.0f}, hot water {today.get('dhw_min', 0):.0f} min"},
             {"k": "Floor supply / return", "v": f"{t(temps.get('floor_supply'))} / {t(temps.get('floor_return'))}",
              "s": f"difference {d(last.get('floor_diff'))}" if last.get("floor_diff") is not None else ""}]
    if last.get("output") is not None:
        tiles.append({"k": "Heat to the floor", "v": f"{last['output'] / 1000:,.0f}k BTU/h" if last["output"] else "Off",
                      "tone": "solar" if last["output"] else "fg", "s": f"{today.get('heat_kbtu', 0):,.0f}k BTU today"})
    tiles.append({"k": "Boiler supply", "v": t(temps.get("boiler_supply")),
                  "s": (f"outdoor {t(last['outdoor'])}" if last.get("outdoor") is not None else "")})
    pr = _propane(stack)
    if pr and pr.get("percent") is not None:
        tiles.append({"k": "Propane", "v": f"{pr['days_left']} days" if pr.get("days_left") is not None else f"{pr['percent']:.0f}%",
                      "tone": "bad" if pr.get("alert") else "fg", "bar": pr["percent"],
                      "s": f"{pr['percent']:.0f}%, {pr['gallons']:,.0f} gal left" if pr.get("gallons") is not None else ""})
    note = f"Fault: {v['fault']}" if v["fault"] else ""
    return {"title": "Boiler", "level": level, "note": note, "tiles": tiles}


def every_minute(stack):
    s = settings(stack)
    if not any(s["roles"].get(r) for r in engine.REQUIRED):
        return []
    def alert(tag, title, message, priority):
        return notify.alert(stack, tag, title, message, priority)
    now = time.time()
    season = None
    if s.get("resets"):
        cur = hastates.get(stack, engine.SEASON)
        season = None if not cur or cur.get("state") not in ("on", "off") else cur["state"] == "on"
    ctx = {"propane": _propane(stack), "season": season}
    msgs = engine.step(stack, s, engine.readings(stack, s), now, engine.outdoor_c(stack, now), alert, ctx)
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
    routes={("GET", "/boiler"): page, ("POST", "/boiler/reset-started"): reset_started, ("POST", "/boiler/clear"): clear},
    on_enable=_on_enable,
    on_disable=_on_disable,
    ha_fields=ha_fields,
    ha_entities=chosen,
    display_panel=display_panel,
    every={60: (every_minute, 60)},
)
