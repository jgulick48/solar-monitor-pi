"""Away mode: a switch for when nobody is at the property. While it is on, the freeze, battery and water alerts speak
earlier and the box sends a short report every day (monitoring only). The rules are setup/away.py; this is its
switch on the start page, its settings, and the collector's minute."""
import datetime
import html
import os
import time

import away as engine
import notify
import solar_setup as ss
from modules import Module, Response
from webui import is_error, notice


def _site(stack):
    return ss.load_json(os.path.join(stack, "state", "site.json"), {})


def _us(site):
    return site.get("units", "us_customary") == "us_customary"


def _when(ts):
    return time.strftime("%a %b %-d %H:%M", time.localtime(ts))


def _until(s):
    try:
        return datetime.date.fromisoformat(s["until"]).strftime("%a %b %-d") if s.get("until") else ""
    except ValueError:
        return ""


def _state_text(stack, s=None):
    s = s or engine.settings(stack)
    if not engine.active(stack):
        return "off"
    return f"on since {_when(s['since'])}" + (f", until the end of {_until(s)}" if _until(s) else "")


# --------------------------------------------------------------------------
# Start page and setup page
# --------------------------------------------------------------------------
def card(stack):
    s = engine.settings(stack)
    if engine.active(stack):
        return (f"<div class='card warn'><b>Away mode</b> is {_state_text(stack, s)}: alerts speak earlier"
                + (f" and a report comes every day at {html.escape(s['report_time'])}" if s["report"] else "") + ". "
                "<form method='post' action='/away/set' style='display:inline'><button name='on' value='0'>Somebody is "
                "here again: turn it off</button></form> &nbsp;<a href='/setup#away'>settings</a></div>")
    return ("<div class='card'><b>Away mode</b>: off. <form method='post' action='/away/set' style='display:inline'>"
            "<button name='on' value='1'>Nobody will be here: turn it on</button></form> &nbsp;<a href='/setup#away'>settings</a></div>")


def set_route(stack, form):
    on = form.get("on") == "1"
    was = engine.active(stack)
    engine.set_away(stack, on, engine.settings(stack)["until"] if on else "")
    if was and not on:
        _clear(stack)
    return Response(location="/")


def _clear(stack):
    """Away mode ended: its alerts go, and its state starts fresh for the next time."""
    for tag in engine.TAGS:
        notify.alert(stack, tag, "", "clear", "warning")
    engine.save(stack, {})


def setup_section(stack, msg=""):
    s = engine.settings(stack)
    us = _us(_site(stack))
    note = notice("err" if is_error(msg) else "ok", msg) if msg else ""
    margin = s["freeze_margin_c"] * 9 / 5 if us else s["freeze_margin_c"]
    c = lambda v: " checked" if v else ""  # noqa: E731
    return f"""<h2 id="away">Away mode</h2>{note}
<p>For when nobody is at the property. It is <b>{html.escape(_state_text(stack, s))}</b>. Nothing is switched: the
alerts speak earlier, and a daily report says all is well, so silence is never the only sign.</p>
<form method="post" action="/away/save"><fieldset><legend>Away mode</legend>
<label><input type="checkbox" name="away" value="1" style="width:auto"{c(engine.active(stack))}> Nobody is at the property</label>
<label>Until the end of (optional) <input type="date" name="until" value="{html.escape(s['until'])}"></label>
<small>The last day away; away mode turns itself off after it. Empty: until it is turned off here or on the start page.</small>
<label><input type="checkbox" name="report" value="1" style="width:auto"{c(s['report'])}> Send a report every day at
<input type="time" name="report_time" value="{html.escape(s['report_time'])}" style="width:auto"></label>
<small>The battery, yesterday's solar, the watched temperatures, propane and water, and anything that is wrong.
"All is well" when nothing is.</small>
<div class="row"><label>Freeze alert this much earlier ({'°F' if us else '°C'})
<input name="freeze_margin" type="number" step="any" min="0" value="{margin:.1f}"></label>
<label>Battery warning this many points above the low-battery level
<input name="battery_margin" type="number" step="1" min="0" max="60" value="{float(s['battery_margin']):g}"></label></div>
<small>Freeze protection's "cold inside" level is raised by the first while away (with that module on), and a hard
freeze in the forecast is told even when the heat and the battery look fine. The second warns before the battery is
low, while there is still time to come and start a generator.</small>
<label><input type="checkbox" name="water" value="1" style="width:auto"{c(s['water'])}> Tell me when the water pump runs</label>
<small>With the water module watching the pump. Turn this off if something uses water while you are away (an ice
maker, a softener, a humidifier).</small>
<p><button>Save</button></p></fieldset></form>"""


def save_route(stack, form):
    import modules
    s = engine.settings(stack)
    us = _us(_site(stack))
    try:
        margin = float(form.get("freeze_margin") or 0)
        batt = float(form.get("battery_margin") or 0)
        until = (form.get("until") or "").strip()
        if until:
            datetime.date.fromisoformat(until)
        rt = (form.get("report_time") or "08:00").strip()[:5]
        if not notify.TIME_RE.match(rt) or not 0 <= margin <= 30 or not 0 <= batt <= 60:
            raise ValueError
    except ValueError:
        return Response(400, setup_msg="Not saved: the end date, the report time or a margin isn't right.")
    was = engine.active(stack)
    s.update(report=form.get("report") == "1", report_time=rt, water=form.get("water") == "1",
             freeze_margin_c=round(margin * 5 / 9 if us else margin, 2), battery_margin=batt)
    modules.save(stack, "away", s)
    on = form.get("away") == "1"
    s = engine.set_away(stack, on, until if on else "")
    if on and engine.expired(s):
        engine.set_away(stack, False)
        return Response(400, setup_msg="Not turned on: the end date is in the past.")
    if was and not on:
        _clear(stack)
    return Response(setup_msg=f"saved: away mode is {_state_text(stack)}.")


# --------------------------------------------------------------------------
# The daily report
# --------------------------------------------------------------------------
def _temp(c, us):
    return f"{c * 9 / 5 + 32:.0f} °F" if us else f"{c:.1f} °C"


def _try(parts, fn):
    try:
        got = fn()
    except Exception:  # noqa: BLE001 - one part that fails is left out of the report
        return
    if got:
        parts.append(got)


def report(stack, site, now=None, data=None):
    """(title, text): the daily report. data: display.data() (tests pass their own)."""
    import checkup
    import display
    import modules
    now = now or time.time()
    us = _us(site)
    parts, problems = [], []
    if data is None:
        try:
            data = display.data(stack, site)
        except Exception:  # noqa: BLE001
            data = {}
    vals = data.get("now") or {}

    def battery():
        if vals.get("soc") is None:
            problems.append("No battery reading.")
            return ""
        day0 = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        y = display.day_stats(day0 - 86400, day0 - 1)
        extra = []
        if y.get("low_soc") is not None:
            extra.append(f"lowest yesterday {y['low_soc']:.0f}%")
        if y.get("solar") is not None:
            extra.append(f"{y['solar']:.0f} kWh of solar yesterday")
        if (y.get("gen_hours") or 0) >= 0.05:
            extra.append(f"generator {y['gen_hours']:.1f} h")
        return f"Battery {vals['soc']:.0f}%" + (f" ({', '.join(extra)})" if extra else "") + "."

    def temps():
        if not modules.load(stack, "freeze")["enabled"]:
            return ""
        import freeze
        fs = {**freeze.DEFAULTS, **modules.settings(stack, "freeze")}
        rd = freeze.readings(stack, fs, site, now)
        ok = [r for r in rd if r["ok"]]
        return ("Inside: " + ", ".join(f"{r['name']} {_temp(r['c'], us)}" for r in ok) + ".") if ok else ""

    def propane():
        if not modules.load(stack, "propane")["enabled"]:
            return ""
        f = modules.get("propane").ha_fields(stack, site)
        pct, days = f.get("propane_level_percent"), f.get("propane_days_left")
        if pct is None:
            return ""
        return f"Propane {pct:.0f}%" + (f", about {days:.0f} days left" if days is not None else "") + "."

    def water():
        if not modules.load(stack, "water")["enabled"]:
            return ""
        import water as w
        st = w.load(stack)
        if not st.get("pump"):
            return ""
        n = w.starts_since(st, now - 86400)
        return "The water pump didn't run in the last 24 hours." if not n else f"The water pump ran {n} time{'s' if n != 1 else ''} in the last 24 hours."

    for fn in (battery, temps, propane, water):
        _try(parts, fn)
    try:
        checks = checkup.core(stack, site) + modules.checks(stack)
        for c in checks:
            if c["state"] == "bad":
                problems.append(f"{c['title']}: {c['text']}")
    except Exception:  # noqa: BLE001
        pass
    try:
        for tag, a in notify.active(stack).items():
            if not any(a.get("title") and a["title"] in p for p in problems):
                problems.append(f"Alert not cleared: {a.get('title') or tag}.")
    except Exception:  # noqa: BLE001
        pass
    for a in data.get("alerts") or []:
        if a.get("text") and a["text"] not in problems:
            problems.append(a["text"])
    return engine.report_text(site.get("name") or "", parts, problems)


# --------------------------------------------------------------------------
# The collector, every minute
# --------------------------------------------------------------------------
def every_minute(stack, data=None, now=None, send=None):
    import display
    import modules
    now = now or time.time()
    send = send or (lambda *a: notify.alert(stack, *a))
    s = engine.settings(stack)
    if not s["away"]:
        return []
    if engine.expired(s, now):
        engine.set_away(stack, False)
        for tag in engine.TAGS:
            send(tag, "", "clear", "warning")
        engine.save(stack, {})
        send("away_ended", "Away mode has ended", f"Its end date ({_until(s)}) has passed, so the alerts are back to "
             "their usual levels and the daily report stops. Turn it on again on the box's start page if nobody is there yet.", "info")
        return ["away mode ended: its end date has passed"]
    site = _site(stack)
    if data is None:
        try:
            data = display.data(stack, site)
        except Exception:  # noqa: BLE001 - never stop the collector's other jobs
            data = {}
    msgs = []
    st = engine.load(stack)
    vals = data.get("now") or {}
    fresh = vals.get("age") is not None and vals["age"] <= 300
    try:
        floor = float(site.get("low_soc_alert") or 25)
    except (TypeError, ValueError):
        floor = 25.0
    adv = data.get("advice") or {}
    got = engine.battery_rule(st, s, vals.get("soc") if fresh else None, floor, now,
                              adv.get("text", "") if adv.get("level") == "warn" else "")
    if got:
        send("away_battery", "Battery getting low, nobody there", got[1] if got[0] == "raise" else "clear", "warning")
        msgs.append("away: battery warning" if got[0] == "raise" else "away: battery warning cleared")
    if s["water"]:
        try:
            if modules.load(stack, "water")["enabled"]:
                import water as w
                ws = w.load(stack)
                pump = ws.get("pump") or {}
                text = engine.water_rule(st, ws.get("runs") or [], pump.get("since") if pump.get("on") else None, s["since"], now)
                if text:
                    send("away_water", "Water used while away", text, "warning")
                    msgs.append("away: the water pump ran")
        except Exception:  # noqa: BLE001 - the water module's trouble never stops the rest
            pass
    if engine.report_due(st, s, now):
        st["report_day"] = time.strftime("%Y-%m-%d", time.localtime(now))
        title, text = report(stack, site, now, data)
        send("away_report", title, text, "info")
        msgs.append(f"away: daily report sent ({title})")
    engine.save(stack, st)
    return msgs


def ha_fields(stack, site):
    return {"away_mode": engine.active(stack)}


def checks(stack):
    s = engine.settings(stack)
    if not engine.active(stack):
        return [{"title": "Away mode", "state": "ok", "link": None, "text": "Off: somebody is at the property."}]
    return [{"title": "Away mode", "state": "ok", "link": ("/setup#away", "Setup"),
             "text": f"{_state_text(stack, s)[0].upper()}{_state_text(stack, s)[1:]}. Alerts speak earlier"
                     + (f"; a report comes every day at {s['report_time']}." if s["report"] else "; the daily report is off.")}]


MODULE = Module(
    id="away",
    title="Away mode",
    description="a switch for when nobody is at the property: earlier freeze, battery and water alerts, and a daily all-is-well report",
    order=58,
    default_enabled=False,
    defaults=dict(engine.DEFAULTS),
    card=card,
    setup_section=setup_section,
    routes={("POST", "/away/set"): set_route, ("POST", "/away/save"): save_route},
    ha_fields=ha_fields,
    checks=checks,
    every={60: every_minute},
)
