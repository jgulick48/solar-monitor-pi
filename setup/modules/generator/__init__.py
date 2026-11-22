"""The generator: its switch and settings on the setup page, and its run log and service reminders (setup/genlog.py):
the Generator page, the start-page card, the collector's look at the inverter history every 5 minutes and the
generator_* fields for Home Assistant.

How the generator is started, its size and fuel stay in state/site.json, where the advisor, the wall display (the
generator tile and "start the generator" advice), the daily summary and Home Assistant's site package read them:
turning the module off sets "generator" there to "none" (what "No generator" did), turning it on puts back how it is
started (kept in state/modules/generator.json).
"""
import html
import json
import os
import time

import genlog
import solar_setup as ss
from modules import FieldError, Module, Response

MODES = (("manual", "Started by hand (e.g. portable generator + Chargeverter)"),
         ("automatic", "Auto-start from the inverter's generator dry contact"))
FUELS = ("gasoline", "propane", "diesel")


def _site_path(stack):
    return os.path.join(stack, "state", "site.json")


def _site(stack):
    return ss.load_json(_site_path(stack), {})


def _set_site(stack, **kw):
    site = _site(stack)
    if not site:                     # not set up yet: the setup page's first save writes it
        return
    site.update(kw)
    ss.write_if_changed(_site_path(stack), json.dumps(site, indent=2) + "\n", 0o644)


def detect(stack):
    return _site(stack).get("generator", "manual") != "none"


def migrate(stack, old, version):
    if old is None:
        mode = _site(stack).get("generator", "manual")
        return {"mode": mode if mode in dict(MODES) else "manual"}
    return old


def _hm(ts):
    return time.strftime("%a %b %-d %H:%M", time.localtime(ts))


def card(stack):
    """Start page: hours since the generator's last service."""
    s = genlog.status(stack, _site(stack))
    if not s["kinds"]:
        return ""
    parts = []
    for k in s["kinds"]:
        line = f"{k['since_service']:g} of {k['interval']} hours since the last service"
        if k["due"]:
            line = f"<b>service due</b>: {line}"
        if k["running"]:
            line += " (running now)"
        parts.append(line)
    return f"<div class='card{' warn' if any(k['due'] for k in s['kinds']) else ''}'><b>Generator</b>: {'; '.join(parts)} &nbsp;<a href='/generator'>log</a></div>"


def page(stack, msg="", code=200):
    s = genlog.status(stack, _site(stack))
    st = genlog.load(stack)
    cards = ""
    for k in s["kinds"]:
        last = k["last_run"]
        last_txt = (f"{_hm(last['start'])}, {last['hours']:.1f} h, {last['kwh']:g} kWh") if last else "none yet"
        cards += f"""<div class="card{' warn' if k['due'] else ''}"><b>{html.escape(k['name'])}</b><br>
{'<b>Service due.</b> ' if k['due'] else ''}{k['since_service']:g} hours since the last service
({'serviced ' + html.escape(k['serviced_on']) if k['serviced_on'] else 'no service recorded yet: counted from the start of the history'});
service every {k['interval']} hours, {max(k['left'], 0):g} to go.{' <b>Running now.</b>' if k['running'] else ''}<br>
<small>Last run: {last_txt}. Last 30 days: {k['runs_30d']} run(s), {k['hours_30d']:g} h, {k['kwh_30d']:g} kWh.
{k['total_hours']:g} hours in all.</small>
<form method="post" action="/generator/serviced" style="margin-top:8px"><input type="hidden" name="kind" value="{k['kind']}">
<label>Hours it has run since that service (0 if just now) <input name="hours_since" value="0" inputmode="decimal" style="max-width:120px"></label>
<p><button>Serviced (oil changed)</button></p></form></div>"""
    if not cards:
        cards = "<p>No generator runs found in the history yet.</p>"
    rows = "".join(f"<tr><td>{_hm(r['start'])}</td><td>{r['hours']:.1f} h</td><td>{r['kwh']:g} kWh</td><td>{r['peak_kw']:g} kW</td>"
                   f"<td>{'GEN input' if r['kind'] == 'inverter' else 'DC charger'}</td></tr>" for r in reversed(st["runs"][-50:]))
    note = ""
    if s["backfilling"]:
        note = f"<p><small>Still reading the older history (up to {_hm(s['processed_until'])}); this takes a few minutes.</small></p>"
    body = f"""<h1>Generator</h1>{('<div class="card">' + html.escape(msg) + '</div>') if msg else ''}{note}{cards}
<p><small>Runs are found in the inverter history: power on the inverters' generator input, or charging the inverters didn't do
themselves (a generator on a Chargeverter or other DC charger). Service intervals: 200 hours for a generator on the GEN input,
100 hours for one charging through a DC charger. Home Assistant reminds you when a service is due.</small></p>
<h2>Runs</h2><table><tr><th>Started</th><th>Ran</th><th>Energy</th><th>Peak</th><th>Connected to</th></tr>{rows or '<tr><td colspan=5>none</td></tr>'}</table>
<p><a href="/">Back</a></p>"""
    return Response(code, "Generator", body)


def serviced(stack, form):
    kind = form.get("kind")
    try:
        hours = float(form.get("hours_since") or 0)
        if kind not in genlog.QUERIES or not 0 <= hours <= 10000:
            raise ValueError
    except ValueError:
        return page(stack, "Hours since the service must be a number of hours.", 400)
    genlog.mark_serviced(stack, kind, hours)
    return page(stack, "Service recorded.")


def form_section(stack):
    import modules
    site = _site(stack)
    mode = modules.settings(stack, "generator")["mode"]
    modes = "".join(f"<option value='{v}'{' selected' if mode == v else ''}>{t}</option>" for v, t in MODES)
    fuels = "".join(f"<option value='{v}'{' selected' if site.get('generator_fuel') == v else ''}>{v.title()}</option>" for v in FUELS)
    return f"""<fieldset id="generator"><legend>Generator</legend><label>How is the generator started? <select name="generator">{modes}</select></label>
<div class="row"><label>Generator size in kW (optional) <input name='generator_kw' type='text' value='{html.escape(str(site.get('generator_kw', '')))}' inputmode='decimal'></label>
<label>Fuel <select name="generator_fuel">{fuels}</select></label></div></fieldset>"""


def _kw(form):
    v = form.get("generator_kw", "").strip()
    if not v:
        return ""
    try:
        x = float(v)
    except ValueError:
        raise FieldError("generator_kw", f"Generator size: '{v}' is not a number")
    if not 1 <= x <= 200:
        raise FieldError("generator_kw", "Generator size must be between 1 and 200")
    return str(x)


def form_check(stack, form):
    _kw(form)


def form_save(stack, form):
    import modules
    mode = form.get("generator") if form.get("generator") in dict(MODES) else "manual"
    modules.save(stack, "generator", {"mode": mode})
    _set_site(stack, generator=mode, generator_kw=_kw(form),
              generator_fuel=form.get("generator_fuel") if form.get("generator_fuel") in FUELS else "gasoline")
    return []


def on_enable(stack):
    import modules
    _set_site(stack, generator=modules.settings(stack, "generator")["mode"])


def on_disable(stack):
    _set_site(stack, generator="none")


def update_log(stack):
    n = genlog.update(stack)
    return [f"generator log: {n} new run(s)"] if n else []


MODULE = Module(
    id="generator",
    title="Generator",
    description="start-the-generator advice, its run log and service reminders",
    order=12,
    default_enabled=True,
    detect=detect,
    defaults={"mode": "manual"},
    migrate=migrate,
    on_enable=on_enable,
    on_disable=on_disable,
    form_section=form_section,
    form_check=form_check,
    form_save=form_save,
    ha_fields=lambda stack, site: genlog.ha_fields(stack, site),
    nav=("/generator", "Generator", "Generator"),
    card=card,
    card_slot="access",                 # right after the battery card, as before
    routes={("GET", "/generator"): lambda stack, form: page(stack), ("POST", "/generator/serviced"): serviced},
    every={300: update_log},            # runs found in the inverter history
)
