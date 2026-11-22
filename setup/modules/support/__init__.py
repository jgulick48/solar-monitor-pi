"""Remote support (a helper's Cloudflare tunnel and SSH key): its setup-page section, the start-page banner while it's
on, and the collector's check that switches it off when its time is up. What it does to the box is setup/support.py."""
import html
import os
import time

import solar_setup as ss
import support as engine  # setup/support.py
from modules import Module, Response
from webui import is_error, notice

LEGACY = ("state", "support.json")       # where the settings were before modules (v0.2.28 and earlier)


def migrate(stack, old, version):
    if old is None:
        return ss.load_json(os.path.join(stack, *LEGACY), {})
    return old


def _until(st):
    if not st["enabled"]:
        return ""
    if not st["until"]:
        return "until it is turned off"
    return "until " + time.strftime("%a %b %-d %H:%M", time.localtime(st["until"]))


def setup_section(stack, msg=""):
    """Remote support (a helper's Cloudflare tunnel + SSH key): its own form on the setup page."""
    st = engine.status(stack)
    days = "".join(f"<option value='{d}'{' selected' if d == st['days'] else ''}>{('after ' + str(d) + (' day' if d == 1 else ' days')) if d else 'only when I turn it off'}</option>"
                   for d in engine.DAYS)
    state = (f"<p><b>On</b>, {_until(st)}. The helper can log in to this box and has full administrator access.</p>"
             if st["enabled"] else "<p>Off.</p>")
    if st["message"] and not st["enabled"]:
        state += f"<p><small>{html.escape(st['message'])}</small></p>"
    saved = "<small>A tunnel token is saved; leave the field empty to keep it.</small>" if st["has_token"] else ""
    off = "" if not st["enabled"] else """<form method="post" action="/support/off"><p><button>Turn remote support off now</button></p></form>"""
    note = notice("err" if is_error(msg) else "ok", msg) if msg else ""
    return f"""<h2 id="support">Remote support (optional)</h2>{note}{state}
<form method="post" action="/support"><fieldset><legend>Let a helper connect to this box</legend>
<p><small>For troubleshooting: someone you trust sets up a Cloudflare Tunnel in <b>their</b> Cloudflare account and gives you a
<b>support code</b> (or its tunnel token and their SSH key). While remote support is on they can reach this box through that tunnel (what exactly, such as SSH, this
setup page or Grafana, is set up in their account, behind their Cloudflare Access login), log in and change anything on it.
Nothing is opened on your router. It switches itself off when the time you choose is up, and you can turn it off here at any time.</small></p>
<label>Support code (starts with SOLAR-SUPPORT-1:) or tunnel token <input name="support_token" type="password" autocomplete="off" style="max-width:100%"></label>{saved}
<label>Their SSH public key(s), one per line <small>(not needed with a support code)</small>
<textarea name="support_keys" rows="3" style="width:100%;font:inherit;box-sizing:border-box;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px">{html.escape(st['keys'])}</textarea></label>
<label>Switch off <select name="support_days">{days}</select></label>
<p><button>{'Save and keep on' if st['enabled'] else 'Turn remote support on'}</button></p></fieldset></form>{off}"""


def banner(stack):
    st = engine.status(stack)
    if not st["enabled"]:
        return ""
    return (f"<div class='card warn'><b>Remote support is on</b>, {_until(st)}: a helper can log in to this box. "
            "<form method='post' action='/support/off' style='display:inline'><button>Turn it off</button></form> "
            "<a href='/setup#support'>settings</a></div>")


def turn_on(stack, form):
    try:
        d = engine.clean(form, engine.settings(stack))
    except ValueError as e:
        return Response(400, setup_msg=f"Not turned on: {e}.")
    msgs = engine.enable(stack, d)
    return Response(setup_msg="; ".join(["on, " + _until(engine.status(stack))] + msgs) + ".")


def turn_off(stack, form):
    msgs = engine.disable(stack)
    return Response(setup_msg="; ".join(["off"] + msgs) + ".")


def on_disable(stack):
    if engine.settings(stack)["enabled"]:
        engine.disable(stack, "turned off with its module")


MODULE = Module(
    id="support",
    title="Remote support",
    order=30,
    default_enabled=True,
    defaults=engine.DEFAULTS,
    migrate=migrate,
    on_disable=on_disable,
    banner=banner,
    setup_section=setup_section,
    routes={("POST", "/support"): turn_on, ("POST", "/support/off"): turn_off},
    every={60: engine.check},          # switch off when the time is up; keep the box matching the setting
    compose_profiles=lambda stack: ["support"] if engine.settings(stack)["enabled"] else [],
)
