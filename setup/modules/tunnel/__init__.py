"""Remote access to Home Assistant through a Cloudflare Tunnel: its setup-page section, start-page card and
Home Assistant's external_url. Setting the tunnel up at Cloudflare is setup/tunnel.py."""
import html
import os
import subprocess

import modules
import solar_setup as ss
import tunnel as engine  # setup/tunnel.py
from modules import Module, Response
from webui import is_error, notice

LEGACY = ("state", "tunnel.json")        # where the settings were before modules (v0.2.28 and earlier)


def migrate(stack, old, version):
    if old is None:
        return ss.load_json(os.path.join(stack, *LEGACY), {})
    return old


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def ha_core(stack):
    url = engine.public_url(stack)
    return {"external_url": url} if url else {}


def status_line(stack):
    url = engine.public_url(stack)
    return (f"Home Assistant at <a href='{html.escape(url)}'>{html.escape(url)}</a> (Cloudflare Tunnel)" if url
            else "off. Home Assistant is only reachable on this network.")


def card(stack):
    return f'<div class="card"><b>Remote access</b>: {status_line(stack)} &nbsp;<a href="/setup#remote">change</a></div>'


def setup_section(stack, msg=""):
    """Remote access (Cloudflare Tunnel) on the setup page: its own form, since it talks to Cloudflare."""
    d = engine.settings(stack)
    def inp(name, value, extra=""):
        return f"<input name='{name}' value='{html.escape(value)}' autocomplete='off' {extra}>"
    note = notice("err" if is_error(msg) else "ok", msg) if msg else ""
    if d["message"]:
        note += notice("err", d["message"], anchor=False)
    state = "<p>Off.</p>"
    if d["enabled"]:
        url = engine.public_url(stack)
        st = engine.tunnel_status(stack) or "unknown"
        state = (f"<p><b>On</b>: Home Assistant is at <a href='{html.escape(url)}'>{html.escape(url)}</a> "
                 f"<small>(Cloudflare says the tunnel is <b>{html.escape(st)}</b>)</small></p>")
    saved = f"<small>Saved: <code>{engine.mask(d['api_token'])}</code>. Leave empty to keep it.</small>" if d["api_token"] else ""
    off = "" if not d["enabled"] else """<form method="post" action="/tunnel/off">
<label><input type="checkbox" name="remove" value="1" style="width:auto" checked> Also delete the tunnel and its DNS record at Cloudflare</label>
<p><button>Turn remote access off</button></p></form>"""
    owner = "" if engine.ha_has_owner(stack) else (
        "<p class='err'>Create your Home Assistant account first (open Home Assistant on this network): "
        "until then, anyone who found the address could claim it.</p>")
    return f"""<h2 id="remote">Remote access (optional)</h2>{note}{state}{owner}
<form method="post" action="/tunnel"><fieldset><legend>Cloudflare Tunnel</legend>
<p><small>Reach Home Assistant, and the Home Assistant phone app, away from home through your own Cloudflare account and domain,
without opening anything on the router. Only Home Assistant is published (it has its own login; turn on two-factor authentication
in your Home Assistant profile). This setup page, Grafana and the cameras stay on the local network.</small></p>
<label>Cloudflare account ID {inp('cf_account', d['account_id'], "placeholder='32 letters and digits'")}</label>
<small>On the Cloudflare dashboard: the domain's overview page, right-hand column, <i>Account ID</i>.</small>
<div class="row"><label>Name {inp('cf_name', d['name'], "placeholder='solar'")}</label>
<label>Domain {inp('cf_domain', d['domain'], "placeholder='example.com'")}</label></div>
<small>Home Assistant will be at <i>name.domain</i>, e.g. solar.example.com. The domain must already be on that Cloudflare account.</small>
<label>API token <input name="cf_token" type="password" autocomplete="off" style="max-width:100%"></label>{saved}
<p><small>Make one at Cloudflare &rarr; My Profile &rarr; API Tokens &rarr; Create Custom Token with the permissions
<b>Account &middot; Cloudflare Tunnel &middot; Edit</b>, <b>Zone &middot; DNS &middot; Edit</b> and <b>Zone &middot; Zone &middot; Read</b>
(limit it to that account and domain). This box keeps the token to update or remove the tunnel later.</small></p>
<p><button>{'Save and update' if d['enabled'] else 'Turn remote access on'}</button></p></fieldset></form>{off}"""


def apply_core_change(stack):
    """Rewrite core.yaml (the public address) and, with remote access on, let HA trust cloudflared on
    localhost (.storage/http, edited while HA is stopped); restart HA when either changed."""
    core_changed = modules.service("write_ha_core")()
    if engine.public_url(stack) and engine.proxy_edit_needed(stack):
        r = _run(ss.compose_cmd(stack) + ["stop", "homeassistant"])
        if r.returncode != 0:
            return [f"couldn't stop Home Assistant to change its proxy setting: {r.stderr.strip()[-200:]}"]
        engine.trust_local_proxy(stack)
        r = _run(ss.compose_cmd(stack) + ["up", "-d", "homeassistant"])
    elif core_changed:
        r = _run(ss.compose_cmd(stack) + ["restart", "homeassistant"])
    else:
        return []
    return ["Home Assistant restarted"] if r.returncode == 0 else [f"Home Assistant restart failed: {r.stderr.strip()[-200:]}"]


def turn_on(stack, form):
    cur = engine.settings(stack)
    try:
        d = engine.clean(form, cur)
    except ValueError as e:
        return Response(400, setup_msg=f"Not saved: {e}.")
    try:
        d = engine.enable(stack, d, prev=cur)
        msgs = ["remote access on at " + engine.public_url(stack)] + apply_core_change(stack)
        if d["message"]:
            msgs.append(d["message"])
    except engine.CFError as e:
        engine.save(stack, {**d, "enabled": cur["enabled"], "message": ""})
        msgs = [f"Not turned on: {e}"]
    return Response(setup_msg="; ".join(msgs) + ".")


def turn_off(stack, form):
    msgs = engine.disable(stack, remove=form.get("remove") == "1") + apply_core_change(stack)
    return Response(setup_msg="; ".join(msgs) + ".")


def on_disable(stack):
    if engine.settings(stack)["enabled"]:
        engine.disable(stack, remove=False)
        apply_core_change(stack)


MODULE = Module(
    id="tunnel",
    title="Remote access",
    description="Home Assistant away from home, through your own Cloudflare Tunnel",
    order=20,
    default_enabled=True,
    defaults=engine.DEFAULTS,
    migrate=migrate,
    on_disable=on_disable,
    ha_core=ha_core,
    card=card,
    card_slot="access",
    setup_section=setup_section,
    routes={("POST", "/tunnel"): turn_on, ("POST", "/tunnel/off"): turn_off},
    compose_profiles=lambda stack: ["tunnel"] if engine.settings(stack)["enabled"] else [],
)
