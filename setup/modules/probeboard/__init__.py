"""The boiler probe board on the setup page: name its eight ports, install its firmware over USB the first time, update
it over the network afterwards, and add it to Home Assistant; and its lines on the check-up page (is it on the
network, which ports have a probe, what the clamps and the heat call read). What it does is setup/probeboard.py."""
import html
import json
import time

import probeboard as engine  # setup/probeboard.py
import solar_setup as ss
from modules import Module, Response
from webui import is_error, notice


def _when(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "never"


def setup_section(stack, msg=""):
    d = engine.settings(stack)
    names = [engine.clean_name(n, engine.NAMES[i]) for i, n in enumerate((d["names"] + engine.NAMES)[:8])]
    note = notice("err" if is_error(msg) else "ok", msg) if msg else ""
    boards = engine.usb_boards()
    j = engine.job(stack)
    busy = j.get("status") == "running"
    pending = d["installed"] and d["built_names"] and d["built_names"] != names
    state = (f"<p>Firmware installed {_when(d['installed'])}"
             + (" &middot; in Home Assistant" if d["ha_added"] else "") + "."
             + (" <b>The port names changed since: update the board to use them.</b>" if pending else "") + "</p>"
             if d["installed"] else "<p>Not installed yet.</p>")
    usb = (f"<small>ESP32 found on USB: <code>{html.escape(boards[0])}</code>.</small>" if boards else
           "<small>No ESP32 on the box's USB ports. For the first install, plug the board's USB-C into one of the "
           "Pi's USB ports, <b>with its PoE (network) cable unplugged</b>: it isn't isolated, so it takes one or "
           "the other.</small>")
    rows = "".join(
        f"<label>P{i} {'(front)' if i <= 4 else '(back)'} <input name='p{i}' value='{html.escape(n)}' maxlength='{engine.NAME_MAX}'></label>"
        for i, n in enumerate(names, 1))
    progress = ""
    if j:
        status = {"running": "Working", "ok": "Done", "error": "Failed"}.get(j.get("status"), "")
        progress = (f"<div id='pb-job' data-running='{'1' if busy else ''}'><p><b>{status}</b>: "
                    f"<span id='pb-step'>{html.escape(j.get('message') or j.get('step', ''))}</span></p>"
                    f"<details{' open' if busy or j.get('status') == 'error' else ''}><summary>Log</summary>"
                    f"<pre id='pb-log' style='white-space:pre-wrap;font-size:12px;max-height:300px;overflow:auto'>"
                    f"{html.escape(chr(10).join(j.get('log', [])[-40:]))}</pre></details></div>")
    dis = " disabled" if busy else ""
    seen = ("ports with a probe so far: " + ", ".join(f"P{p}" for p in engine.seen(stack))) if engine.seen(stack) else "no port has had a probe yet"
    return f"""<h2 id="probeboard">Boiler probe board</h2>{note}{state}
<form method="post" action="/probeboard/names"><fieldset><legend>Port names and chip</legend>
<p><small>Each probe port's name in Home Assistant and on the board's display. The boiler module's setup picks which
sensor is which, so these are only names. They take effect when the board is next installed or updated.</small></p>
<div class="row">{rows}</div>
<label><input type="checkbox" name="old_chip" value="1"{' checked' if d.get('old_chip') else ''}> The board has an ESP32-P4
chip older than revision 3</label>
<small>Leave this off for boards bought now. Older chips need a different build: with the wrong one the board never
starts. The install log names the chip ("ESP32-P4 (revision v3.2)") when it connects.</small>
<p><button>Save</button></p></fieldset></form>
<form method="post" action="/probeboard/install"><fieldset><legend>Firmware</legend>
<p><small>The box builds the board's firmware itself (ESPHome; the first build downloads about 2 GB and takes a while)
with its own encryption key, and adds the board to Home Assistant after the first install.</small></p>
{usb}
<p><button name="kind" value="usb"{dis}{'' if boards else ' disabled'}>Install over USB</button>
<button name="kind" value="network"{dis}{'' if d['installed'] else ' disabled'}>Update over the network</button>
<button formaction="/probeboard/ha" formmethod="post"{dis}{'' if d['installed'] else ' disabled'}>Add to Home Assistant</button></p>
{progress}</fieldset></form>
<form method="post" action="/probeboard/forget"><p><small>The <a href="/check">check-up page</a> shows what each port
reads, straight from the board, and reports a port that had a probe and has none now ({seen}).</small>
<button{'' if engine.seen(stack) else ' disabled'}>Forget missing probes</button></p></form>
<script>(()=>{{const j=document.getElementById('pb-job'); if(!j||!j.dataset.running) return;
// when the job ends, show the result with a fresh GET: this page may be the answer to the "Install" POST, and
// reloading that would send the POST again and start the install over, for ever (seen on the first real board)
const t=setInterval(async()=>{{try{{const r=await fetch('/probeboard/job'); const d=await r.json();
document.getElementById('pb-step').textContent=d.message||d.step||''; const l=document.getElementById('pb-log');
l.textContent=(d.log||[]).slice(-40).join('\\n'); l.scrollTop=l.scrollHeight;
if(d.status!=='running'){{clearInterval(t); if(location.pathname==='/setup'){{location.hash='probeboard'; location.reload();}}else{{location.replace('/setup#probeboard');}}}}}}catch(e){{}}}},3000);}})();</script>"""


def save_names(stack, form):
    d = engine.settings(stack)
    d["names"] = [engine.clean_name(form.get(f"p{i}", ""), engine.NAMES[i - 1]) for i in range(1, 9)]
    d["old_chip"] = bool(form.get("old_chip"))
    engine.save(stack, d)
    return Response(setup_msg="port names saved" + ("; update the board to use them" if d["installed"] else "") + ".")


def install(stack, form):
    kind = form.get("kind")
    if kind not in ("usb", "network"):
        return Response(400, setup_msg="Not started: choose USB or the network.")
    return Response(setup_msg=engine.start(stack, kind))


def add_ha(stack, form):
    return Response(setup_msg="; ".join(engine.add_to_ha(stack)) + ".")


def forget(stack, form):
    engine.forget(stack)
    return Response(setup_msg="forgotten: only ports with a probe now count as used.")


def _temp(c, us):
    return f"{c * 9 / 5 + 32:.1f} °F" if us else f"{c:.1f} °C"


def checks(stack, fetch=None):
    """Check-up page: the board on the network, its probe ports, and its clamps and heat call."""
    d = engine.settings(stack)
    link = ("/setup#probeboard", "Setup")
    if not d["installed"]:
        return [{"title": "Boiler probe board", "state": "off", "link": link,
                 "text": "Its firmware isn't installed yet."}]
    try:
        got = engine.live(fetch)
    except OSError as e:
        return [{"title": "Boiler probe board", "state": "bad", "link": link,
                 "text": f"It doesn't answer at {engine.HOST} ({getattr(e, 'reason', e)}). Check its network (PoE) "
                         "cable and the switch port's light; its display shows its address when it is connected."}]
    us = ss.load_json(f"{stack}/state/site.json", {}).get("units", "us_customary") == "us_customary"
    pp = engine.ports(stack, got)
    have = [p for p in pp if p["c"] is not None]
    missing = [p for p in pp if p["missing"]]
    out = [{"title": "Boiler probe board", "state": "ok", "link": None,
            "text": f"On the network; {len(have)} of 8 ports have a probe."}]
    readings = ", ".join(f"P{p['port']} {p['name']} {_temp(p['c'], us)}" for p in have)
    unused = ", ".join(f"P{p['port']}" for p in pp if p["c"] is None and not p["missing"])
    if missing:
        text = ("No probe answers on " + ", ".join(f"P{p['port']} ({p['name']})" for p in missing)
                + ", which had one before: check the plug is pushed in and its three wires are tight. "
                + (f"Reading: {readings}. " if readings else "")
                + "If it was taken out on purpose, press Forget missing probes on the setup page.")
        out.append({"title": "Probe ports", "state": "bad", "link": link, "text": text})
    elif not have:
        out.append({"title": "Probe ports", "state": "warn", "link": link,
                    "text": "No port has a probe yet. A probe shows here within 15 seconds of being plugged in."})
    else:
        out.append({"title": "Probe ports", "state": "ok", "link": None,
                    "text": readings + (f". No probe on {unused}." if unused else ".")})
    clamps = [f"{label} ({name.lower().replace(' current', '')}) {got[('sensor', engine._norm(name))]:.2f} A"
              for label, name in engine.CLAMPS if got.get(("sensor", engine._norm(name))) is not None]
    heat = got.get(("binary_sensor", engine._norm(engine.HEAT_CALL)))
    parts = clamps + ([f"call for heat {'on' if heat else 'off'}"] if heat is not None else [])
    if parts:
        out.append({"title": "Pump clamps and heat call", "state": "ok", "link": None,
                    "text": "; ".join(parts)[0].upper() + "; ".join(parts)[1:]
                            + ". A clamp that isn't plugged in reads about 0 A, the same as a pump that is off."})
    return out


def job_status(stack, form):
    return Response(body=json.dumps(engine.job(stack)), ctype="application/json")


MODULE = Module(
    id="probeboard",
    title="Boiler probe board",
    description="build and install the ESP32 probe board's firmware from here, name its ports, add it to Home Assistant",
    order=56,
    defaults=dict(engine.DEFAULTS),
    setup_section=setup_section,
    routes={("POST", "/probeboard/names"): save_names, ("POST", "/probeboard/install"): install,
            ("POST", "/probeboard/ha"): add_ha, ("GET", "/probeboard/job"): job_status,
            ("POST", "/probeboard/forget"): forget},
    checks=checks,
    every={300: engine.remember},       # which ports have a probe, so one that goes missing is noticed
)
