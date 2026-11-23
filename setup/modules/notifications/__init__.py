"""Notifications: where alerts go, by priority (the HA phone app, ntfy, the wall display), quiet hours and the log
of what was sent. Deciding and delivering is setup/notify.py; this is its switch, settings, pages and schedule.

Turned off, Home Assistant's script.solar_notify sends every alert to every phone with the app, as before this
module existed."""
import html
import os
import re
import secrets
import time

import notify
import solar_setup as ss
from modules import FieldError, Module, Response


def settings(stack):
    import modules
    return modules.settings(stack, "notifications")


def _voice_on(stack):
    from modules import voice
    return voice.is_on(stack)


def migrate(stack, old, version):
    if old is None:
        # A box that was already set up keeps sending at any hour, as before; quiet hours start on new boxes.
        set_up = bool(ss.load_json(os.path.join(stack, "state", "site.json"), {}).get("battery_count"))
        return {**notify.DEFAULTS, **({"quiet_from": "", "quiet_to": ""} if set_up else {})}
    return old


def _tunnel_on(stack):
    try:
        import tunnel
        return bool(tunnel.public_url(stack))
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# Setup page
# --------------------------------------------------------------------------
def form_section(stack):
    s = settings(stack)
    e = html.escape
    def chk(name, on):
        return f"<input type='checkbox' name='{name}' value='1' style='width:auto'{' checked' if on else ''}>"
    ph = notify.phones(stack)
    every = s.get("phones", "all") == "all"
    if ph:
        boxes = "".join(f"<label style='display:inline-block;margin-right:16px'>{chk('nt_phone_' + p['id'], every or p['id'] in s['phones'])} "
                        f"{e(p['name'])}</label>" for p in ph)
        phones = (f"<label>{chk('nt_all_phones', every)} Every phone signed in, including ones added later</label>"
                  f"<div style='margin-left:24px'><small>or only these:</small><br>{boxes}</div>")
    else:
        phones = ("<p><small><b>No phone is signed in yet</b>: install the Home Assistant app, sign in to this box with your "
                  "account and allow notifications; it shows up here.</small></p><input type='hidden' name='nt_all_phones' value='1'>")
    tok = (f"<small>Saved: <code>{e(s['ntfy_token'][:4])}…</code>. Leave empty to keep it; enter <code>-</code> to remove it.</small>"
           if s.get("ntfy_token") else "")
    own = ("<b>Remote access is set up on this box, so a server of your own behind it is the private choice.</b> "
           if _tunnel_on(stack) else "")
    wall_note = "" if _voice_on(stack) else " <small style='display:inline'>(turn on Voice announcements for this)</small>"
    def test(ch, label):
        return f"<button formaction='/notifications/test' formmethod='post' formnovalidate name='channel' value='{ch}'>{label}</button> "
    return f"""<fieldset id="notifications"><legend>Notifications</legend>
<small>Where alerts go. <b>Critical</b> ones (inverter fault, start the generator) go to every channel at any hour and repeat
every 4 hours until they clear. <b>Warnings</b> go to the phones and wait for the end of quiet hours. <b>Information</b> (daily
summary, service reminders) goes to one channel, also not during quiet hours. <a href="/notifications">Alerts sent</a></small>
<label>{chk('nt_ha_app', s['ha_app'])} <b>Home Assistant app</b></label>
<div style="margin-left:24px">{phones}</div>
<label>{chk('nt_ntfy', s['ntfy'])} <b>ntfy</b> <small style='display:inline'>(the ntfy app, without Home Assistant)</small></label>
<div style="margin-left:24px"><div class="row"><label>Server <input name="nt_server" value="{e(s['ntfy_server'])}" placeholder="https://ntfy.sh"></label>
<label>Topic <input name="nt_topic" value="{e(s['ntfy_topic'])}" placeholder="made for you when left empty"></label></div>
<label>Access token <input name="nt_token" type="password" autocomplete="off"></label>{tok}
<small>{own}ntfy.sh is free and public: anyone who knows the topic can read it, so the topic is a long random name; add an access
token if you have an ntfy account. Subscribe to the same server and topic in the ntfy app.</small></div>
<label>{chk('nt_wall', s['wall'])} <b>Wall display</b>: speak critical alerts{wall_note}</label>
<div class="row"><label>Quiet hours from <input name="nt_quiet_from" type="time" value="{e(s['quiet_from'])}"></label>
<label>to <input name="nt_quiet_to" type="time" value="{e(s['quiet_to'])}"></label></div>
<small>Empty: no quiet hours. Box time.</small>
<p>{test('ha_app', 'Test the app')}{test('ntfy', 'Test ntfy')}{test('wall', 'Test the wall display')}
<small style="display:inline">save first if you changed something.</small></p></fieldset>"""


def _url_ok(u):
    return re.match(r"^https?://[A-Za-z0-9.-]+(:\d+)?(/[A-Za-z0-9._~/-]*)?$", u or "") is not None


def form_check(stack, form):
    if form.get("nt_ntfy") == "1":
        if not _url_ok(form.get("nt_server", "").strip()):
            raise FieldError("nt_server", "the ntfy server must be an address like https://ntfy.sh")
        t = form.get("nt_topic", "").strip()
        if t and not notify.TOPIC_RE.match(t):
            raise FieldError("nt_topic", "the topic may only have letters, digits, - and _ (up to 64)")
    for k in ("nt_quiet_from", "nt_quiet_to"):
        v = form.get(k, "").strip()
        if v and not notify.TIME_RE.match(v):
            raise FieldError(k, "quiet hours are times like 22:00")


def form_save(stack, form):
    import modules
    cur = settings(stack)
    ids = [p["id"] for p in notify.phones(stack)]
    every = form.get("nt_all_phones") == "1"
    new = {
        "ha_app": form.get("nt_ha_app") == "1",
        "phones": "all" if every else [i for i in ids if form.get("nt_phone_" + i) == "1"],
        "ntfy": form.get("nt_ntfy") == "1",
        "ntfy_server": form.get("nt_server", "").strip() or "https://ntfy.sh",
        "ntfy_topic": form.get("nt_topic", "").strip() or cur.get("ntfy_topic", ""),
        "ntfy_token": cur.get("ntfy_token", ""),
        "wall": form.get("nt_wall") == "1",
        "quiet_from": form.get("nt_quiet_from", "").strip(),
        "quiet_to": form.get("nt_quiet_to", "").strip(),
    }
    tok = form.get("nt_token", "").strip()
    if tok == "-":
        new["ntfy_token"] = ""
    elif tok:
        new["ntfy_token"] = tok
    if new["ntfy"] and not new["ntfy_topic"]:
        new["ntfy_topic"] = "solar-" + secrets.token_urlsafe(18).replace("_", "-")
    modules.save(stack, "notifications", new)
    msgs = []
    if new["ha_app"] and not every and not new["phones"] and ids:
        msgs.append("no phone is ticked: the Home Assistant app gets no alerts")
    return msgs


def test(stack, form):
    ch = form.get("channel")
    if ch not in dict(notify.CHANNELS):
        return Response(400, setup_msg="Not sent: no such channel.")
    s = settings(stack)
    if ch == "ha_app" and not notify.chosen_phones(stack, s):
        return Response(setup_msg="Not sent: no phone is chosen (or none is signed in to Home Assistant yet).")
    if ch == "ntfy" and not s.get("ntfy_topic"):
        return Response(setup_msg="Not sent: turn ntfy on and save first (that makes the topic).")
    if ch == "wall" and not _voice_on(stack):
        return Response(setup_msg="Not sent: turn on Voice announcements first.")
    a = {"tag": "test", "title": "Test from the solar monitor", "priority": "info",
         "message": "If you can read this, alerts from the solar monitor reach you here."}
    r = notify.send(stack, s, a, [ch])
    notify._log(stack, {"at": time.time(), **a, "result": r})
    word = r.get(ch, "")
    return Response(setup_msg=("Test " + word + "." if not word.startswith("failed") else f"Test {word}. Check the settings above."))


# --------------------------------------------------------------------------
# Alerts from Home Assistant (script.solar_notify) and the alerts page
# --------------------------------------------------------------------------
def receive(stack, form):
    """POST /notify/send from Home Assistant: answers "ok" at once and delivers in the background."""
    if not secrets.compare_digest(form.get("token", ""), notify.box_token(stack)):
        return Response(403, body="forbidden", ctype="text/plain")
    import threading
    s, voice_on = settings(stack), _voice_on(stack)
    alert = {k: form.get(k, "") for k in ("tag", "title", "message", "priority")}
    threading.Thread(target=lambda: notify.submit(stack, s, alert, voice_on=voice_on), daemon=True).start()
    return Response(body="ok", ctype="text/plain")


def page(stack, form):
    e = html.escape
    def when(t):
        return time.strftime("%b %d %H:%M", time.localtime(t))
    def res(r):
        return "; ".join(f"{e(dict(notify.CHANNELS).get(k, ''))}{': ' if k in dict(notify.CHANNELS) else ''}{e(str(v))}"
                         for k, v in r.items() if k != "why") + (f" <small>({e(r['why'])})</small>" if r.get("why") else "")
    rows = "".join(f"<tr><td>{when(x['at'])}</td><td>{e(x['priority'])}</td><td><b>{e(x['title'])}</b> {e(x['message'][:200])}</td>"
                   f"<td>{res(x.get('result', {}))}</td></tr>" for x in notify.log(stack)) or "<tr><td colspan=4>None yet.</td></tr>"
    held = "".join(f"<li>{e(h['title'] or h['tag'])} <small>({when(h['at'])})</small></li>" for h in notify.held(stack))
    act = "".join(f"<li>{e(a['title'] or t)} <small>(since {when(a['first'])})</small></li>" for t, a in notify.active(stack).items())
    body = f"""<h1>Alerts sent</h1>
{'<h2>Waiting for the end of quiet hours</h2><ul>' + held + '</ul>' if held else ''}
{'<h2>Critical, repeated every 4 hours until cleared</h2><ul>' + act + '</ul>' if act else ''}
<table><tr><th>When</th><th>Priority</th><th>Alert</th><th>Sent to</th></tr>{rows}</table>
<p><small>The last {notify.LOG_MAX}. Where alerts go is under <a href="/setup#notifications">Notifications</a> on the setup page.</small></p>"""
    return Response(title="Alerts sent", body=body)


def due(stack):
    return notify.due(stack, settings(stack), voice_on=_voice_on(stack))


MODULE = Module(
    id="notifications",
    title="Notifications",
    description="where alerts go: the Home Assistant app, ntfy, the wall display; quiet hours",
    order=15,
    default_enabled=True,
    defaults=notify.DEFAULTS,
    migrate=migrate,
    form_section=form_section,
    form_check=form_check,
    form_save=form_save,
    nav=("/notifications", "Alerts", "Alerts sent"),
    routes={("POST", "/notify/send"): receive, ("POST", "/notifications/test"): test, ("GET", "/notifications"): page},
    every={60: due},
)
