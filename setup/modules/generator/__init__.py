"""The generator: its switch and settings on the setup page, and its run log and service reminders (setup/genlog.py):
the Generator page, the start-page card, the collector's look at the inverter history every 5 minutes and the
generator_* fields for Home Assistant.

For a generator somebody has to walk out to, it also follows each run as it happens (setup/genwatch.py, every
minute): a notice when it has started and when it has stopped (how long, the energy, the fuel), reminders that
follow the battery down while it is needed and not running, and the fuel on hand: the owner enters the gallons after
filling up (Generator page), the box counts down with each run and warns when few hours of running are left.

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
    return old                           # version 2 only adds settings, which take their defaults


DEFAULTS = {"mode": "manual",
            "notify_runs": True,        # say when the generator has started and when it has stopped
            "reminders": True,          # remind again as the battery falls while it is needed and not running
            "fuel_gal": None, "fuel_at": 0,   # gallons on hand as entered, and when (None: not tracked)
            "fuel_low_h": 2.0}          # warn when fewer hours of running than this are left


def _settings(stack):
    import modules
    return {**DEFAULTS, **modules.settings(stack, "generator")}


def fuel_left(stack, site=None, s=None, st=None):
    """genlog.fuel_on_hand for this site, or None (nothing entered, or no way to work fuel out)."""
    s = s or _settings(stack)
    try:
        return genlog.fuel_on_hand(st or genlog.load(stack), site if site is not None else _site(stack),
                                   s.get("fuel_gal"), s.get("fuel_at"))
    except (TypeError, ValueError):
        return None


def _dur(hours):
    import display
    return display.duration(hours)


def _fuel_txt(f):
    return f"about {f['gal']:g} gal, about {_dur(f['hours'])} of running"


def _gal(v):
    return "&ndash;" if v is None else f"{v:.1f} gal"


def _money(v):
    return "&ndash;" if v is None else f"${v:,.2f}"


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
    f = fuel_left(stack)
    if f:
        low = f["hours"] < float(_settings(stack)["fuel_low_h"])
        parts.append(("<b>fuel low</b>: " if low else "fuel on hand ") + _fuel_txt(f))
    return f"<div class='card{' warn' if any(k['due'] for k in s['kinds']) else ''}'><b>Generator</b>: {'; '.join(parts)} &nbsp;<a href='/generator'>log</a></div>"


def page(stack, msg="", code=200):
    s = genlog.status(stack, _site(stack))
    st = genlog.load(stack)
    cards = ""
    site = _site(stack)

    def fuel30(k):
        if k["fuel_30d"] is None:
            return ""
        return f", about {k['fuel_30d']:g} gal" + (f" ({_money(k['cost_30d'])})" if k["cost_30d"] is not None else "")
    for k in s["kinds"]:
        last = k["last_run"]
        last_txt = (f"{_hm(last['start'])}, {last['hours']:.1f} h, {last['kwh']:g} kWh") if last else "none yet"
        cards += f"""<div class="card{' warn' if k['due'] else ''}"><b>{html.escape(k['name'])}</b><br>
{'<b>Service due.</b> ' if k['due'] else ''}{k['since_service']:g} hours since the last service
({'serviced ' + html.escape(k['serviced_on']) if k['serviced_on'] else 'no service recorded yet: counted from the start of the history'});
service every {k['interval']} hours, {max(k['left'], 0):g} to go.{' <b>Running now.</b>' if k['running'] else ''}<br>
<small>Last run: {last_txt}. Last 30 days: {k['runs_30d']} run(s), {k['hours_30d']:g} h, {k['kwh_30d']:g} kWh{fuel30(k)}.
{k['total_hours']:g} hours in all.</small>
<form method="post" action="/generator/serviced" style="margin-top:8px"><input type="hidden" name="kind" value="{k['kind']}">
<label>Hours it has run since that service (0 if just now) <input name="hours_since" value="0" inputmode="decimal" style="max-width:120px"></label>
<p><button>Serviced (oil changed)</button></p></form></div>"""
    if not cards:
        cards = "<p>No generator runs found in the history yet.</p>"
    cards += _fuel_card(stack, site, st)
    rows = "".join(f"<tr><td>{_hm(r['start'])}</td><td>{r['hours']:.1f} h</td><td>{r['kwh']:g} kWh</td><td>{r['peak_kw']:g} kW</td>"
                   f"<td>{_gal(genlog.fuel(r, site))}</td><td>{_money(genlog.cost(genlog.fuel(r, site), site))}</td>"
                   f"<td>{'GEN input' if r['kind'] == 'inverter' else 'DC charger'}</td></tr>" for r in reversed(st["runs"][-50:]))
    mrows = "".join(f"<tr><td>{time.strftime('%B %Y', time.strptime(m['month'], '%Y-%m'))}</td><td>{m['runs']}</td><td>{m['hours']:g} h</td>"
                    f"<td>{m['kwh']:g} kWh</td><td>{_gal(m['fuel'])}</td><td>{_money(m['cost'])}</td></tr>"
                    for m in genlog.months(st["runs"], site))
    monthly = (f"<h2>By month</h2><table><tr><th>Month</th><th>Runs</th><th>Ran</th><th>Energy</th><th>Fuel</th><th>Cost</th></tr>"
               f"{mrows}</table><p><small>{html.escape(genlog.fuel_note(site))}</small></p>" if mrows else "")
    note = ""
    if s["backfilling"]:
        note = f"<p><small>Still reading the older history (up to {_hm(s['processed_until'])}); this takes a few minutes.</small></p>"
    body = f"""<h1>Generator</h1>{('<div class="card">' + html.escape(msg) + '</div>') if msg else ''}{note}{cards}
<p><small>Runs are found in the inverter history: power on the inverters' generator input, or charging the inverters didn't do
themselves (a generator on a Chargeverter or other DC charger). Service intervals: 200 hours for a generator on the GEN input,
100 hours for one charging through a DC charger. Home Assistant reminds you when a service is due.</small></p>
{monthly}
<h2>Runs</h2><table><tr><th>Started</th><th>Ran</th><th>Energy</th><th>Peak</th><th>Fuel</th><th>Cost</th><th>Connected to</th></tr>{rows or '<tr><td colspan=7>none</td></tr>'}</table>
<p><a href="/">Back</a></p>"""
    return Response(code, "Generator", body)


def _fuel_card(stack, site, st):
    """The Generator page's fuel-on-hand card and its form."""
    s = _settings(stack)
    f = fuel_left(stack, site, s, st)
    if f:
        low = f["hours"] < float(s["fuel_low_h"])
        state = (f"{'<b>Low.</b> ' if low else ''}About <b>{f['gal']:g} gal</b> on hand, about {_dur(f['hours'])} of running "
                 f"at {f['rate']:g} gal an hour. {f['entered']:g} gal entered {_hm(f['at'])}; about {f['used']:g} gal used since.")
    elif s.get("fuel_gal") is not None:
        low = False
        state = f"{float(s['fuel_gal']):g} gal entered, but fuel use can't be worked out: {html.escape(str(genlog.fuel_basis(site)[1]))}."
    else:
        low = False
        state = "Not tracked yet. Enter what is in the cans or the tank and the box counts down with each run."
    return f"""<div class="card{' warn' if low else ''}"><b>Fuel on hand</b><br>{state}
<form method="post" action="/generator/fuel" style="margin-top:8px"><div class="row">
<label>Fuel on hand now, in gallons (after filling up, or after checking) <input name="fuel_gal" inputmode="decimal" style="max-width:120px" placeholder="e.g. 10"></label>
<label>Warn when fewer hours of running are left than <input name="fuel_low_h" value="{float(s['fuel_low_h']):g}" inputmode="decimal" style="max-width:120px"></label></div>
<p><button>Save</button> <small>Leave the gallons empty to change only the warning; enter 0 gallons and the box
says the fuel is out. To stop tracking, press <button name="stop" value="1" style="display:inline">Stop tracking</button>.</small></p></form></div>"""


def fuel_form(stack, form):
    import modules
    s = _settings(stack)
    if form.get("stop"):
        s.update(fuel_gal=None, fuel_at=0)
        modules.save(stack, "generator", s)
        return page(stack, "Fuel is no longer tracked.")
    try:
        low_h = float(form.get("fuel_low_h") or s["fuel_low_h"])
        gal = form.get("fuel_gal", "").strip()
        gal = None if gal == "" else float(gal)
        if not 0 <= low_h <= 100 or (gal is not None and not 0 <= gal <= 5000):
            raise ValueError
    except ValueError:
        return page(stack, "Fuel on hand must be a number of gallons, and the warning a number of hours.", 400)
    s["fuel_low_h"] = low_h
    if gal is not None:
        s.update(fuel_gal=gal, fuel_at=int(time.time()))
    modules.save(stack, "generator", s)
    return page(stack, "Fuel on hand recorded." if gal is not None else "Saved.")


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
    s = _settings(stack)
    mode = s["mode"]
    modes = "".join(f"<option value='{v}'{' selected' if mode == v else ''}>{t}</option>" for v, t in MODES)
    fuels = "".join(f"<option value='{v}'{' selected' if site.get('generator_fuel') == v else ''}>{v.title()}</option>" for v in FUELS)
    return f"""<fieldset id="generator"><legend>Generator</legend><label>How is the generator started? <select name="generator">{modes}</select></label>
<div class="row"><label>Generator size in kW (optional) <input name='generator_kw' type='text' value='{html.escape(str(site.get('generator_kw', '')))}' inputmode='decimal'></label>
<label>Fuel <select name="generator_fuel">{fuels}</select></label></div>
<label>Measured fuel use, gallons per hour (optional)
<input name='generator_gph' type='text' value='{html.escape(str(site.get('generator_gph', '')))}' inputmode='decimal' placeholder='e.g. 0.6'></label>
<small>The generator log works out each run's fuel and cost (with the fuel price under Costs). Without this it uses a
typical figure for the generator's size and fuel; with it, your own: fill the tank, run it, and divide what it took
to refill by the hours it ran.</small>
<label>Chargeverter (or other DC charger) set to, in amps (optional)
<input name='charger_amps' type='text' value='{html.escape(str(site.get('charger_amps', '')))}' inputmode='decimal' placeholder='e.g. 40'></label>
<small>The current it's set to charge the battery at. With it, the advice to start the generator also says how long to
run it: the kWh needed divided by the charger's power (amps &times; the battery's voltage).</small>
<label><input type="checkbox" name="gen_notify_runs" value="1" style="width:auto"{' checked' if s['notify_runs'] else ''}> Tell me when the generator has started and when it has stopped</label>
<small>The stop notice has how long it ran, the energy, the fuel it used and what is left, and says if it stopped
before the battery had enough.</small>
<label><input type="checkbox" name="gen_reminders" value="1" style="width:auto"{' checked' if s['reminders'] else ''}> Remind me again as the battery falls while the generator is needed and not running</label>
<small>For a generator started by hand: after the first "start the generator", another reminder each time the
battery has fallen 10 more points, and one when it is within 2 hours of the low-battery level. These come through at
night too. The fuel on hand is entered on the <a href="/generator">Generator page</a>.</small></fieldset>"""


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


def _amps(form):
    v = form.get("charger_amps", "").strip().upper().rstrip("A").strip()
    if not v:
        return ""
    try:
        x = float(v)
    except ValueError:
        raise FieldError("charger_amps", f"Charger current: '{form.get('charger_amps')}' is not a number of amps")
    if not 1 <= x <= 300:
        raise FieldError("charger_amps", "Charger current must be between 1 and 300 A")
    return f"{x:g}"


def _gph(form):
    v = form.get("generator_gph", "").strip()
    if not v:
        return ""
    try:
        x = float(v)
    except ValueError:
        raise FieldError("generator_gph", f"Measured fuel use: '{v}' is not a number of gallons per hour")
    if not 0.05 <= x <= 20:
        raise FieldError("generator_gph", "Measured fuel use must be between 0.05 and 20 gallons per hour")
    return f"{x:g}"


def form_check(stack, form):
    _kw(form)
    _amps(form)
    _gph(form)


def form_save(stack, form):
    import modules
    mode = form.get("generator") if form.get("generator") in dict(MODES) else "manual"
    modules.save(stack, "generator", {**_settings(stack), "mode": mode, "notify_runs": form.get("gen_notify_runs") == "1",
                                      "reminders": form.get("gen_reminders") == "1"})
    _set_site(stack, generator=mode, generator_kw=_kw(form), charger_amps=_amps(form), generator_gph=_gph(form),
              generator_fuel=form.get("generator_fuel") if form.get("generator_fuel") in FUELS else "gasoline")
    return []


def on_enable(stack):
    import modules
    _set_site(stack, generator=modules.settings(stack, "generator")["mode"])


def on_disable(stack):
    _set_site(stack, generator="none")


def update_log(stack):
    n = genlog.update(stack)
    try:                                            # its counts, for Grafana's Battery and generator dashboard
        import vmdays
        lines = genlog.vm_lines(genlog.status(stack, _site(stack)))
        if lines:
            vmdays.post(lines)
    except (OSError, ValueError):
        pass                                        # VictoriaMetrics is restarting: the next pass writes them
    return [f"generator log: {n} new run(s)"] if n else []


def announce_stop(stack, advice=None, now=None):
    """Every minute: when a hand-started generator has run long enough (display.generator_stop_advice says "stop"),
    have the wall display say so, once per run. state/generator-stop.json remembers that this run was announced;
    it is cleared when the generator is off again. Returns messages to log."""
    import display
    from modules import voice
    path = os.path.join(stack, "state", "generator-stop.json")
    if advice is None:
        try:
            advice = display.data(stack, _site(stack)).get("advice") or {}
        except Exception:  # noqa: BLE001 - never stop the collector's other jobs
            return []
    phase = advice.get("phase")
    said = ss.load_json(path, {}).get("announced")
    if phase is None:                               # not running (any more): ready for the next run
        if said:
            ss.write_if_changed(path, "{}\n", 0o644)
        return []
    if phase != "stop" or said:
        return []
    ss.write_if_changed(path, json.dumps({"announced": int(now or time.time())}) + "\n", 0o644)
    voice.announce(stack, "The generator can be shut down. The battery will last until the sun is back.")
    return ["generator: enough charge, it can be shut down"]


def _hhmm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def _pct(v):
    return "unknown" if v is None else f"{v:.0f}%"


def notices(events, site, fuel, s):
    """genwatch's events as alerts: [(tag, title, message, priority)]. fuel: fuel_left() or None."""
    out = []
    fuel_line = f" Fuel on hand: {_fuel_txt(fuel)}." if fuel else ""
    for kind, e in events:
        if kind == "started" and s["notify_runs"]:
            if e["can_stop"]:
                more = " The battery already has enough to last until the sun is back."
            elif e["run_hours"]:
                more = f" It needs to run about {_dur(e['run_hours'])} for the battery to last until the sun is back."
            else:
                more = ""
            out.append(("generator_run", "Generator started",
                        f"Running since {_hhmm(e['start'])}, giving about {e['kw']:g} kW. Battery {_pct(e['soc'])}.{more}", "info"))
        elif kind == "stopped" and s["notify_runs"]:
            run = {"kind": "inverter" if site.get("generator") == "automatic" else "charger", "hours": e["hours"], "kwh": e["kwh"]}
            gal = genlog.fuel(run, site)
            cost = genlog.cost(gal, site)
            used = "" if gal is None else f", using about {gal:.1f} gal" + ("" if cost is None else f" (${cost:,.2f})")
            text = (f"It ran {_dur(e['hours'])} until {_hhmm(e['end'])} and gave {e['kwh']:.1f} kWh{used}. "
                    f"Battery {_pct(e['soc_start'])} at the start, {_pct(e['soc'])} now.")
            early = e["early"] and site.get("generator") == "manual" and (e.get("need_kwh") or 0) > 0
            if early:
                text += (f" It stopped before the battery had enough: about {e['need_kwh']:.1f} kWh more is needed to "
                         "last until the sun is back.")
            out.append(("generator_run", "Generator stopped early" if early else "Generator stopped", text + fuel_line,
                        "warning" if early else "info"))
        elif kind == "remind" and s["reminders"] and site.get("generator") == "manual":
            floor = f"{float(e['floor']):.0f}%" if e.get("floor") is not None else "the low-battery level"
            if e["urgent"]:
                title = "Start the generator now"
                text = (f"The battery is at {_pct(e['soc'])} and reaches {floor} in about {_dur(e['empty_in_h'])}"
                        + (f" ({e['empty_at']})" if e.get("empty_at") else "") + ".")
            else:
                title = "Generator still not started"
                text = (f"The battery has fallen to {_pct(e['soc'])}"
                        + (f" (it was {_pct(e['first_soc'])} when the generator was first needed)" if e.get("first_soc") is not None else "")
                        + (f" and reaches {floor} about {e['empty_at']}" if e.get("empty_at") else "") + ".")
            if e.get("need_kwh"):
                text += f" About {e['need_kwh']:.1f} kWh is needed" + (f", about {_dur(e['run_hours'])} of generator" if e.get("run_hours") else "") + "."
            if fuel and e.get("run_hours") and fuel["hours"] < e["run_hours"]:
                text += f" Only {_fuel_txt(fuel)} is on hand: less than that run."
            else:
                text += fuel_line
            out.append(("generator", title, text, "critical"))
    return out


def every_minute(stack, data=None, now=None, send=None):
    """The collector, every minute: the wall display's "it can be shut down" (announce_stop), and the run's notices,
    the reminders and the low-fuel warning. data: display.data() (tests pass their own). Returns messages to log."""
    import display
    import genwatch
    import notify
    send = send or (lambda *a: notify.alert(stack, *a))
    site = _site(stack)
    if data is None:
        try:
            data = display.data(stack, site)
        except Exception:  # noqa: BLE001 - never stop the collector's other jobs
            return []
    advice = data.get("advice") or {}
    msgs = announce_stop(stack, advice, now)
    now = now or time.time()
    s = _settings(stack)
    vals = data.get("now") or {}
    fresh = vals.get("age") is not None and vals["age"] <= 300
    obs = {"gen_w": (vals.get("gen") or 0) if fresh else None, "soc": vals.get("soc") if fresh else None,
           "needed": advice.get("level") == "warn" and advice.get("phase") != "run" and site.get("generator") == "manual",
           "phase": advice.get("phase") or "", "need_kwh": advice.get("need_kwh"), "run_hours": advice.get("run_hours"),
           "empty_in_h": advice.get("empty_in_h"), "empty_at": advice.get("empty_at"), "floor": site.get("low_soc_alert") or 25}
    st = genwatch.load(stack)
    events = genwatch.step(st, obs, now)
    fuel = fuel_left(stack, site, s)
    for tag, title, text, priority in notices(events, site, fuel, s):
        err = send(tag, title, text, priority)
        msgs.append(f"generator: {title.lower()}" + (f" (not sent: {err})" if err else ""))
    low = bool(fuel) and fuel["hours"] < float(s["fuel_low_h"])
    if low and not st.get("fuel_low"):
        err = send("generator_fuel", "Generator fuel is low" if fuel["gal"] > 0 else "Generator fuel is out",
                   f"{_fuel_txt(fuel)[0].upper()}{_fuel_txt(fuel)[1:]} is left ({fuel['entered']:g} gal entered {_hm(fuel['at'])}, "
                   f"about {fuel['used']:g} gal used since). After filling up, enter the new amount on the box's Generator page.", "warning")
        msgs.append("generator: fuel low" + (f" (not sent: {err})" if err else ""))
        st["fuel_low"] = True
    elif not low and st.get("fuel_low"):
        send("generator_fuel", "", "clear", "warning")
        st["fuel_low"] = False
    genwatch.save(stack, st)
    return msgs


def ha_fields(stack, site):
    f = fuel_left(stack, site)
    return {**genlog.ha_fields(stack, site), "generator_fuel_left": f and f["gal"], "generator_fuel_hours": f and f["hours"]}


MODULE = Module(
    id="generator",
    title="Generator",
    description="start-the-generator advice, its run log and service reminders",
    order=12,
    default_enabled=True,
    detect=detect,
    defaults=dict(DEFAULTS),
    migrate=migrate,
    on_enable=on_enable,
    on_disable=on_disable,
    form_section=form_section,
    form_check=form_check,
    form_save=form_save,
    ha_fields=ha_fields,
    nav=("/generator", "Generator", "Generator"),
    card=card,
    card_slot="access",                 # right after the battery card, as before
    routes={("GET", "/generator"): lambda stack, form: page(stack), ("POST", "/generator/serviced"): serviced,
            ("POST", "/generator/fuel"): fuel_form},
    every={300: update_log,             # runs found in the inverter history
           60: every_minute},           # "it can be shut down" once per run; started / stopped; reminders; low fuel
)
