"""Share this site: let another solar monitor box follow this one's inverters, without a second connection to the
dongles (monitoring only; a follower can never change the inverter). The work is setup/sharing.py; this is its
switch, the share code and its buttons."""
import html
import os
import subprocess

import sharing as engine
import solar_setup as ss
from modules import Module, Response
from webui import is_error, notice


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def _site_name(stack):
    return ss.load_json(os.path.join(stack, "state", "site.json"), {}).get("name") or "Solar"


def apply(stack, on, new_password=False):
    """Mosquitto's listener and account, the bridge's raw frames, and the restarts they need. Returns messages."""
    msgs = []
    try:
        changed = engine.write_source(stack, on)
    except engine.ShareError as e:
        return [f"not turned on: {e}"]
    sec = ss.ensure_secrets(stack)
    if on and (new_password or not sec.get("mqtt_share")):
        engine.password(stack, new=True)
        ss.write_mosquitto_passwd(stack, ss.ensure_secrets(stack))
        changed = True
    env_changed = ss.write_env(stack, ss.ensure_secrets(stack))          # SHARE_BIND: the listener's port on the network
    if changed or env_changed:
        r = _run(ss.compose_cmd(stack) + ["up", "-d", "--force-recreate", "mosquitto"])
        msgs.append("the message broker restarted" if r.returncode == 0 else f"the message broker restart failed: {r.stderr.strip()[-200:]}")
    inv = ss.load_json(os.path.join(stack, "state", "inverters.json"), {})
    if ss.write_bridge_config(stack, inv, ss.ensure_secrets(stack)) or changed or env_changed:
        r = _run(ss.compose_cmd(stack) + ["restart", "bridge"])
        msgs.append("the inverter bridge restarted" if r.returncode == 0 else f"the bridge restart failed: {r.stderr.strip()[-200:]}")
    return msgs


def on_enable(stack):
    apply(stack, True)


def after_disable(stack):
    """Off: the listener's files go, the broker comes back without its network port, the bridge stops publishing
    raw frames. The certificate and the password are kept, so the same code works if it is turned on again."""
    changed = engine.write_source(stack, False)
    if changed:
        _run(ss.compose_cmd(stack) + ["up", "-d", "--force-recreate", "mosquitto"])
    inv = ss.load_json(os.path.join(stack, "state", "inverters.json"), {})
    if ss.write_bridge_config(stack, inv, ss.ensure_secrets(stack)) or changed:
        _run(ss.compose_cmd(stack) + ["restart", "bridge"])


def env(stack):
    return ["SHARE_BIND=0.0.0.0"]


def setup_section(stack, msg=""):
    e = html.escape
    note = notice("err" if is_error(msg) else "ok", msg) if msg else ""
    dongles = engine.local_dongles(stack)
    try:
        code = engine.make_code(stack, _site_name(stack)) if dongles else ""
    except OSError:
        code = ""
    if not dongles:
        body = "<p class='err'>This box hasn't found an inverter dongle of its own yet, so there is nothing to share.</p>"
    elif not code:
        body = "<p class='err'>The share code isn't ready: turn the module off and on again.</p>"
    else:
        addrs = " or ".join(f"<code>{e(a)}</code>" for a in engine.addresses())
        body = f"""<p><b>On</b>: sharing {len(dongles)} dongle{'s' if len(dongles) != 1 else ''} ({e(', '.join(d['datalog'] for d in dongles))})
on port {engine.PORT} at {addrs}.</p>
<label>Share code <textarea id="share-code" rows="5" readonly style="width:100%;font-family:monospace;font-size:.8em">{e(code)}</textarea></label>
<p><button type="button" onclick="navigator.clipboard.writeText(document.getElementById('share-code').value).then(()=>{{this.textContent='Copied'}})">Copy the code</button></p>
<small>Paste it on the other box: Setup &rarr; Optional features &rarr; <i>Follow a site</i>. Treat it like a password and send it
privately: with it, a box that can reach this one reads this site's inverter data (and nothing else; it can't change
anything). The other box must be able to reach this box's address: the same network, or a VPN between the two.</small>
<form method="post" action="/share/new-code" style="margin-top:10px"><button>Make a new code</button>
<small>Changes the password: every box using the old code stops getting data until it has the new one.</small></form>"""
    return f"""<h2 id="share">Share this site</h2>{note}
<p><small>Lets another solar monitor box show this site's inverters as its own, without connecting to the dongles a second time
(two programs polling a WiFi dongle make it drop out). This box keeps reading the dongles and passes every reading on.</small></p>
{body}"""


def new_code(stack, form):
    msgs = apply(stack, True, new_password=True)
    return Response(setup_msg="; ".join(["new share code made"] + msgs) + ".")


def checks(stack):
    link = ("/setup#share", "Setup")
    n = len(engine.local_dongles(stack))
    if not n:
        return [{"title": "Share this site", "state": "bad", "link": link, "text": "On, but this box has no dongle of its own to share."}]
    if not os.path.exists(os.path.join(engine.conf_dir(stack), "share.conf")):
        return [{"title": "Share this site", "state": "bad", "link": link,
                 "text": "On, but its network listener isn't set up: turn the module off and on again."}]
    return [{"title": "Share this site", "state": "ok", "link": None,
             "text": f"Sharing {n} dongle{'s' if n != 1 else ''} with boxes that have the share code (port {engine.PORT}, read only)."}]


MODULE = Module(
    id="share",
    title="Share this site",
    description="let another solar monitor box follow this site's inverters with a share code (read only, same network or VPN)",
    order=80,
    default_enabled=False,
    on_enable=on_enable,
    after_disable=after_disable,
    env=env,
    setup_section=setup_section,
    routes={("POST", "/share/new-code"): new_code},
    checks=checks,
)
