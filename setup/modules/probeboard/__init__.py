"""The boiler probe board on the setup page: name its eight ports, install its firmware over USB the first time, update
it over the network afterwards, and add it to Home Assistant. What it does is setup/probeboard.py."""
import html
import json
import time

import probeboard as engine  # setup/probeboard.py
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
    return f"""<h2 id="probeboard">Boiler probe board</h2>{note}{state}
<form method="post" action="/probeboard/names"><fieldset><legend>Port names</legend>
<p><small>Each probe port's name in Home Assistant and on the board's display. The boiler module's setup picks which
sensor is which, so these are only names. They take effect when the board is next installed or updated.</small></p>
<div class="row">{rows}</div>
<p><button>Save port names</button></p></fieldset></form>
<form method="post" action="/probeboard/install"><fieldset><legend>Firmware</legend>
<p><small>The box builds the board's firmware itself (ESPHome; the first build downloads about 2 GB and takes a while)
with its own encryption key, and adds the board to Home Assistant after the first install.</small></p>
{usb}
<p><button name="kind" value="usb"{dis}{'' if boards else ' disabled'}>Install over USB</button>
<button name="kind" value="network"{dis}{'' if d['installed'] else ' disabled'}>Update over the network</button>
<button formaction="/probeboard/ha" formmethod="post"{dis}{'' if d['installed'] else ' disabled'}>Add to Home Assistant</button></p>
{progress}</fieldset></form>
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
    engine.save(stack, d)
    return Response(setup_msg="port names saved" + ("; update the board to use them" if d["installed"] else "") + ".")


def install(stack, form):
    kind = form.get("kind")
    if kind not in ("usb", "network"):
        return Response(400, setup_msg="Not started: choose USB or the network.")
    return Response(setup_msg=engine.start(stack, kind))


def add_ha(stack, form):
    return Response(setup_msg="; ".join(engine.add_to_ha(stack)) + ".")


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
            ("POST", "/probeboard/ha"): add_ha, ("GET", "/probeboard/job"): job_status},
)
