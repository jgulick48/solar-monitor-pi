"""Follow a site: show another solar monitor box's inverters on this one, from that box's share code, without a
second connection to its dongles (read only). The work is setup/sharing.py; this is its switch, form and checks."""
import html
import os
import secrets
import subprocess

import sharing as engine
import solar_setup as ss
from modules import Module, Response
from webui import is_error, notice

DEFAULTS = {"name": "", "host": "", "hosts": [], "port": engine.PORT, "user": engine.USER, "password": "", "ns": engine.NAMESPACE,
            "cert": "", "dongles": [], "client": ""}


def settings(stack):
    import modules
    return {**DEFAULTS, **modules.settings(stack, "follow")}


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def apply(stack, s):
    """Write the mirrors and the dongles' entries for these settings, start or remove the mirrors, and point the
    bridge at them. Returns messages."""
    msgs = []
    before = set(engine.profiles(settings(stack)))
    import modules
    modules.save(stack, "follow", s)
    files_changed, inv_changed = engine.write_follow(stack, s)
    ss.write_env(stack, ss.ensure_secrets(stack))                        # which mirrors run (compose profiles)
    want = engine.profiles(s)
    for p in sorted(before - set(want)):                                 # mirrors no longer needed
        _run(ss.compose_cmd(stack) + ["rm", "-s", "-f", p.replace("follow", "mirror")])
    if want:
        names = [p.replace("follow", "mirror") for p in want]
        r = _run(ss.compose_cmd(stack) + ["up", "-d"] + (["--force-recreate"] if files_changed else []) + names)
        msgs.append(f"{len(names)} mirror{'s' if len(names) != 1 else ''} started" if r.returncode == 0
                    else f"the mirrors couldn't be started: {r.stderr.strip()[-200:]}")
    inv = ss.load_json(os.path.join(stack, "state", "inverters.json"), {})
    if ss.write_bridge_config(stack, inv, ss.ensure_secrets(stack)) or inv_changed:
        r = _run(ss.compose_cmd(stack) + ["restart", "bridge"])
        msgs.append("the inverter bridge restarted" if r.returncode == 0 else f"the bridge restart failed: {r.stderr.strip()[-200:]}")
    return msgs


def save(stack, form):
    cur = settings(stack)
    code = (form.get("share_code") or "").strip()
    try:
        s = {**cur, **engine.read_code(code)} if code else dict(cur)
    except engine.ShareError as e:
        return Response(400, setup_msg=f"Not saved: {e}.")
    if not s["dongles"]:
        return Response(400, setup_msg="Not saved: paste the share code from the other box.")
    host = (form.get("source_host") or "").strip() or (s["hosts"][0] if code and s["hosts"] else s["host"])
    if not host or not engine.HOST_RE.match(host):
        return Response(400, setup_msg="Not saved: the source box's address isn't an address or a name.")
    s["host"] = host
    s["client"] = cur["client"] or secrets.token_hex(4)
    local = [dl for dl, i in ss.load_json(os.path.join(stack, "state", "inverters.json"), {}).items()
             if not i.get("remote") and i.get("present") and dl in {d["datalog"] for d in s["dongles"]}]
    msgs = [f"following {s['name'] or 'the site'} at {host}"] + apply(stack, s)
    if local:
        msgs.append(f"this box had been reading {', '.join(local)} directly and now reads through the other box instead")
    return Response(setup_msg="; ".join(msgs) + ".")


def stop(stack, form):
    cur = settings(stack)
    msgs = ["no longer following"] + apply(stack, {**DEFAULTS, "client": cur["client"]})
    return Response(setup_msg="; ".join(msgs) + ". The followed dongles will be found again by the hourly scan if they are on this network.")


def after_disable(stack):
    cur = settings(stack)
    if cur["dongles"]:
        apply(stack, {**DEFAULTS, "client": cur["client"]})


def on_enable(stack):
    s = settings(stack)
    if s["dongles"] and s["host"]:
        apply(stack, s)


def compose_profiles(stack):
    return engine.profiles(settings(stack))


def _states(s):
    return [(n, d, *engine.mirror_health(n)) for n, d in enumerate(s["dongles"][:engine.MAX_DONGLES], 1)]


def setup_section(stack, msg=""):
    e = html.escape
    s = settings(stack)
    note = notice("err" if is_error(msg) else "ok", msg) if msg else ""
    state, stop_form = "<p>Not following a site yet.</p>", ""
    if s["dongles"] and s["host"]:
        rows = "".join(f"<li><code>{e(d['datalog'])}</code>: {'' if ok else '<b class=\"err\">'}{e(text)}{'' if ok else '</b>'}</li>"
                       for _, d, ok, text in _states(s))
        state = (f"<p><b>Following {e(s['name'] or 'a site')}</b> at <code>{e(s['host'])}</code>, port {s['port']}:</p><ul>{rows}</ul>")
        stop_form = """<form method="post" action="/follow/stop"><p><button>Stop following</button></p></form>"""
    others = [h for h in s["hosts"] if h != s["host"]]
    hint = f" The code also gave {', '.join(f'<code>{e(h)}</code>' for h in others)}." if others else ""
    own = [dl for dl, i in ss.load_json(os.path.join(stack, "state", "inverters.json"), {}).items() if not i.get("remote")]
    warn = (f"<p><small><b>This box also reads {len(own)} dongle{'s' if len(own) != 1 else ''} of its own.</b> A followed site's "
            "inverters are added to them, as if they were one system: the totals, the battery level and the advice mix "
            "both. Following is meant for a box without inverters of its own.</small></p>") if own and not s["dongles"] else ""
    return f"""<h2 id="follow">Follow a site</h2>{note}{state}{warn}
<form method="post" action="/follow"><fieldset><legend>The other box's share code</legend>
<p><small>On the box at the site: Setup &rarr; Optional features &rarr; <i>Share this site</i>, then copy its share code. This box
then shows that site's inverters as if they were connected here, reading only what the other box passes on: it never
connects to the dongles, and it can't change anything on the inverter. The two boxes must reach each other: the same
network, or a VPN between them.</small></p>
<label>Share code <textarea name="share_code" rows="4" style="width:100%;font-family:monospace;font-size:.8em" autocomplete="off"
placeholder="{'Paste a new code to replace the saved one' if s['dongles'] else 'SOLARSHARE1-...'}"></textarea></label>
<label>The other box's address <input name="source_host" value="{e(s['host'])}" placeholder="from the code; or its VPN address" autocomplete="off"></label>
<small>Leave empty to use the first address in the code. Enter another if this box reaches it differently (a VPN address).{hint}</small>
<p><button>{'Save' if s['dongles'] else 'Follow this site'}</button></p></fieldset></form>{stop_form}"""


def checks(stack):
    s = settings(stack)
    link = ("/setup#follow", "Setup")
    if not (s["dongles"] and s["host"]):
        return [{"title": "Follow a site", "state": "warn", "link": link, "text": "On, but no share code has been entered yet."}]
    bad = [f"{d['datalog']}: {text}" for _, d, ok, text in _states(s) if not ok]
    if bad:
        return [{"title": "Follow a site", "state": "bad", "link": link,
                 "text": f"Following {s['name'] or 'a site'} at {s['host']}, but {'; '.join(bad)}. Check this box can reach "
                         "the other one, that sharing is still on there, and that its code hasn't been replaced."}]
    n = len(s["dongles"])
    return [{"title": "Follow a site", "state": "ok", "link": None,
             "text": f"Following {s['name'] or 'a site'} at {s['host']}: {n} dongle{'s' if n != 1 else ''}, getting data."}]


MODULE = Module(
    id="follow",
    title="Follow a site",
    description="show another solar monitor box's inverters here from its share code, without connecting to its dongles (same network or VPN)",
    order=81,
    default_enabled=False,
    defaults=DEFAULTS,
    on_enable=on_enable,
    after_disable=after_disable,
    compose_profiles=compose_profiles,
    setup_section=setup_section,
    routes={("POST", "/follow"): save, ("POST", "/follow/stop"): stop},
    checks=checks,
)
