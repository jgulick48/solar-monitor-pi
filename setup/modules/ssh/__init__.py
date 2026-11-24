"""SSH access from the local network with SSH keys: its pane on the setup page and a start-page card while it's on.
What it changes on the box is setup/sshaccess.py."""
import html

import solar_setup as ss
import sshaccess as engine  # setup/sshaccess.py
from modules import Module, Response
from webui import is_error, notice

TEXTAREA = ("width:100%;font:inherit;box-sizing:border-box;background:var(--bg);color:var(--fg);"
            "border:1px solid var(--line);border-radius:6px")


def _login(stack):
    ip = ss.lan_ip()
    return f"ssh solar@{ip}" if ip else "ssh solar@solar-monitor.local"


def setup_section(stack, msg=""):
    d = engine.settings(stack)
    note = notice("err" if is_error(msg) else "ok", msg) if msg else ""
    n = len([k for k in d["keys"].split("\n") if k.strip()])
    if d["enabled"]:
        state = (f"<p><b>On</b> for {n} key{'s' if n != 1 else ''}"
                 f"{', with administrator rights' if d['sudo'] else ''}: <code>{html.escape(_login(stack))}</code></p>")
    else:
        state = "<p>Off.</p>" + (f"<p><small>{html.escape(d['message'])}</small></p>" if d["message"] else "")
    off = "" if not d["enabled"] else """<form method="post" action="/ssh/off"><p><button>Turn SSH access off</button></p></form>"""
    return f"""<h2 id="ssh">SSH access (optional)</h2>{note}{state}
<form method="post" action="/ssh"><fieldset><legend>Log in to this box from this network</legend>
<p><small>For whoever looks after the box: paste the <b>public</b> key of each computer that may log in (the contents of
<code>~/.ssh/id_ed25519.pub</code>, starting <code>ssh-ed25519</code>). Logins are with these keys only, never a password,
as the user <code>solar</code>, from this network (nothing is opened on the router). Remove a key here to stop that
computer logging in; turning SSH access off removes them all.</small></p>
<label>SSH public key(s), one per line
<textarea name="ssh_keys" rows="3" style="{TEXTAREA}">{html.escape(d['keys'])}</textarea></label>
<label><input type="checkbox" name="ssh_sudo" value="1" style="width:auto"{' checked' if d['sudo'] else ''}> Administrator rights
(sudo without a password), needed to update or repair the box</label>
<p><button>{'Save SSH access' if d['enabled'] else 'Turn SSH access on'}</button></p></fieldset></form>{off}"""


def card(stack):
    d = engine.settings(stack)
    if not d["enabled"]:
        return ""
    return (f'<div class="card"><b>SSH access</b>: on, <code>{html.escape(_login(stack))}</code> '
            f'&nbsp;<a href="/setup#ssh">change</a></div>')


def turn_on(stack, form):
    try:
        keys = engine.clean_keys(form.get("ssh_keys", ""))
    except ValueError as e:
        return Response(400, setup_msg=f"Not turned on: {e}.")
    msgs = engine.enable(stack, keys, form.get("ssh_sudo") == "1")
    return Response(setup_msg="; ".join([f"on: {_login(stack)}"] + msgs) + ".")


def turn_off(stack, form):
    return Response(setup_msg="; ".join(["off"] + engine.disable(stack)) + ".")


def on_disable(stack):
    if engine.settings(stack)["enabled"]:
        engine.disable(stack, "turned off with its module")


MODULE = Module(
    id="ssh",
    title="SSH access",
    description="log in to this box from your own network with an SSH key",
    order=35,
    default_enabled=True,
    defaults=engine.DEFAULTS,
    on_disable=on_disable,
    card=card,
    card_slot="access",
    setup_section=setup_section,
    routes={("POST", "/ssh"): turn_on, ("POST", "/ssh/off"): turn_off},
)
