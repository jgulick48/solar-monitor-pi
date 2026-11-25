"""Cameras on the wall display: UniFi Protect cameras picked from a list (they come to the front when they see someone,
and can chime), or any RTSP/RTSPS camera by its stream link. This is its switch, setup page and hooks; go2rtc,
detections and the hourly upkeep are setup/cameras.py, Protect's API protect.py.

The camera settings stay in display.json and protect.json (the screen reads them); turning the module off keeps them,
stops go2rtc and the Protect event listener and hides the cameras from the screen.
"""
import html
import subprocess
import threading
import time
import urllib.parse

import cameras as engine  # setup/cameras.py
import notify
import protect
import solar_setup as ss
import tts
from modules import FieldError, Module, Response
from webui import is_error, notice


def _display(stack):
    import display
    return display.load(stack)


def detect(stack):
    """On for a box that already shows cameras or has a Protect console set up."""
    return bool(_display(stack)["cameras"] or protect.settings(stack).get("host"))


def migrate(stack, old, version):
    """No settings of its own (they stay in display.json / protect.json). The first time, on a box without cameras,
    stop the go2rtc that ran before cameras were a module (Compose doesn't stop a service whose profile went away)."""
    if old is None and not detect(stack):
        _compose(stack, "rm", "-s", "-f", "go2rtc")
    return old or {}


def mask_url(url):
    """rtsps://host:7441/TOKEN?x -> rtsps://host:7441/....ab (the token is a password)."""
    u = urllib.parse.urlsplit(url)
    tail = u.path.rsplit("/", 1)[-1]
    return f"{u.scheme}://{u.netloc}/" + ("\u2022" * 4 + tail[-2:] if tail else "")


def _wording(stack, dsp):
    """What the screen says for a camera event: only while voice announcements are on (the voice module)."""
    from modules import voice
    if not voice.is_on(stack):
        return ""
    def inp(name, value):
        return f"<input name='{name}' type='text' value='{html.escape(str(value))}' >"
    return f"""<p style="margin-top:16px"><b>Spoken announcements</b> <small style="display:inline">(Voice announcements says these for
the kinds ticked above)</small></p>
<div class="row"><label>Announcement {inp('speak_template', dsp.get('speak_template', tts.DEFAULT_TEMPLATE))}</label>
<label>Doorbell {inp('speak_doorbell', dsp.get('speak_doorbell', tts.DEFAULT_DOORBELL))}</label></div>
<small>{{object}} is Person, Vehicle, Animal or Package; {{camera}} the camera's name.</small>"""


def form_section(stack):
    dsp = _display(stack)
    def inp(name, value, typ="text", extra=""):
        return f"<input name='{name}' type='{typ}' value='{html.escape(str(value))}' {extra}>"
    cams = ""
    manual = [c for c in dsp["cameras"] if not c.get("protect_id")]
    for i in range(1, min(len(manual) + 1, engine.MAX_CAMERAS - (len(dsp["cameras"]) - len(manual))) + 1):
        c = manual[i - 1] if len(manual) >= i else {}
        saved = (f"<small>Saved: <code>{html.escape(mask_url(c['url']))}</code>. Leave the link empty to keep it. "
                 f"<label style='display:inline'><input type='checkbox' name='cam{i}_remove' value='1' style='width:auto'> remove</label></small>") if c else ""
        cams += (f"<div class='row'><label>Camera {i} name {inp(f'cam{i}_name', c.get('name', ''), 'text', 'placeholder=' + chr(39) + ('Driveway' if i == 1 else 'Front door') + chr(39))}</label>"
                 f"<label style='flex:2 1 300px'>Stream link {inp(f'cam{i}_url', '', 'text', 'placeholder=' + chr(39) + 'rtsps://192.168.1.1:7441/...' + chr(39) + ' style=' + chr(39) + 'max-width:100%' + chr(39) + ' autocomplete=off')}</label></div>{saved}")
    return f"""<fieldset id="cameras"><legend>Cameras</legend>
<p style="margin-top:16px"><b>Sounds</b> <small style="display:inline">through the screen's speakers (HDMI), when a UniFi Protect camera sees:</small></p>
<div class="row">{''.join(f"<label><input type='checkbox' name='sound_{k}' value='1'{' checked' if (dsp.get('sounds') or {}).get(k, on) else ''}> {t}</label>" for k, t, on in engine.SOUND_KINDS)}</div>
<div class="row"><label>Repeat the same alert (same camera, same kind) at most every <select name="sound_cooldown">{''.join(f"<option value='{v}'{' selected' if dsp.get('sound_cooldown', 60) == v else ''}>{t}</option>" for v, t in engine.COOLDOWNS)}</select></label>
<label>Any two sounds at least <select name="sound_gap">{''.join(f"<option value='{v}'{' selected' if dsp.get('sound_gap', 5) == v else ''}>{t}</option>" for v, t in engine.GAPS)}</select></label></div>
<label><input type="checkbox" name="doorbell_always" value="1"{' checked' if dsp.get('doorbell_always', True) else ''}> The doorbell always rings (not held back by these limits)</label>
<small>Limits the sounds and announcements only; the camera still comes to the front of the screen.</small>
{_wording(stack, dsp)}
<p style="margin-top:16px"><b>Phone notifications with the camera's picture</b> <small style="display:inline">through the notifications module (its phones and quiet hours), when a UniFi Protect camera on the screen's list sees:</small></p>
<div class="row">{''.join(f"<label><input type='checkbox' name='notify_{k}' value='1'{' checked' if (dsp.get('notify') or {}).get(k) else ''}> {t}</label>" for k, t, _ in engine.NOTIFY_KINDS)}
<label>The same camera and kind at most every <select name="notify_cooldown">{''.join(f"<option value='{v}'{' selected' if dsp.get('notify_cooldown', 300) == v else ''}>{t}</option>" for v, t in engine.NOTIFY_COOLDOWNS)}</select></label></div>
<small>The picture shows in the Home Assistant app (ntfy gets the text). During quiet hours they wait until morning.</small>
<p><small>Two cameras at a time are shown next to the power figures, taking turns every 15 seconds. To add one by its stream link instead, in UniFi Protect: open the camera &rarr; Settings &rarr; Advanced &rarr;
RTSPS, turn on <b>Medium</b> quality and copy its link (<code>rtsps://...:7441/...</code>). The camera pictures are only shown on the
box's own screen, not to other computers on the network.</small></p>{cams}
<p><small>UniFi Protect cameras can be picked from a list instead (and come to the front when they see someone):
<a href="#protect">UniFi Protect cameras</a> below. Camera entities, motion and doorbell events in Home Assistant: add the UniFi Protect integration there (Settings &rarr;
Devices &amp; services; it's usually found automatically) with a local Protect user.</small></p></fieldset>"""


def form_check(stack, form):
    for i in range(1, engine.MAX_CAMERAS + 1):
        if form.get(f"cam{i}_url", "").strip():
            try:
                engine.go2rtc_source(form[f"cam{i}_url"])
            except ValueError as e:
                raise FieldError(f"cam{i}_url", f"Camera {i}: {e}")
    for k in ("speak_template", "speak_doorbell"):
        if k in form:
            try:
                tts.check_template((form.get(k) or "{object}").strip())
            except ValueError as e:
                raise FieldError(k, f"Announcement: {e}")


def form_save(stack, form):
    import display
    cur = display.load(stack)
    num = lambda k, allowed, dflt: (lambda v: v if v in allowed else dflt)(int(form[k]) if str(form.get(k, "")).isdigit() else dflt)  # noqa: E731
    d = {**cur,
         "sounds": {k: form.get(f"sound_{k}") == "1" for k, _, _ in engine.SOUND_KINDS},
         "sound_cooldown": num("sound_cooldown", dict(engine.COOLDOWNS), 60),
         "sound_gap": num("sound_gap", dict(engine.GAPS), 5),
         "doorbell_always": form.get("doorbell_always") == "1",
         "notify": {k: form.get(f"notify_{k}") == "1" for k, _, _ in engine.NOTIFY_KINDS},
         "notify_cooldown": num("notify_cooldown", dict(engine.NOTIFY_COOLDOWNS), 300),
         # the wording for voice announcements: only on the page while voice is on; kept otherwise
         "speak_template": (form.get("speak_template") or cur.get("speak_template") or tts.DEFAULT_TEMPLATE).strip()[:120],
         "speak_doorbell": (form.get("speak_doorbell") or cur.get("speak_doorbell") or tts.DEFAULT_DOORBELL).strip()[:120]}
    # UniFi Protect's cameras are set in their own section; the fields here are the manual ones
    keep = [c for c in cur["cameras"] if c.get("protect_id")]
    manual = [c for c in cur["cameras"] if not c.get("protect_id")]
    new = []
    for i in range(1, engine.MAX_CAMERAS + 1):
        name = form.get(f"cam{i}_name", "").strip()[:30]
        url = form.get(f"cam{i}_url", "").strip()
        old = manual[i - 1] if len(manual) >= i else None
        if form.get(f"cam{i}_remove") == "1":
            continue
        if not url and old:
            url = old["url"]  # the saved link isn't shown again; empty keeps it
        if url:
            engine.go2rtc_source(url)  # validates
            new.append({"name": name or f"Camera {i}", "url": url})
    d["cameras"] = (keep + new)[:engine.MAX_CAMERAS]
    return [engine.restart_go2rtc(stack)] if display.save(stack, d) else []


def setup_section(stack, msg=""):
    """UniFi Protect cameras (protect.py): console address and API key, then which cameras the screen shows."""
    d = protect.settings(stack)
    note = notice("err" if is_error(msg) else "ok", msg) if msg else ""
    saved = "<small>An API key is saved; leave the field empty to keep it.</small>" if d["api_key"] else ""
    cams_html = ""
    if d["host"] and d["api_key"]:
        try:
            cams = protect.list_cameras(d["host"], d["api_key"])
            renamed = protect.sync_names(stack, cams)      # renamed in Protect: the screen and voice follow now
            if renamed:
                engine.prewarm_voice(stack, renamed.values())
                d = protect.settings(stack)
            picked = {c["id"] for c in d["cameras"]}
            rows = "".join(
                f"<label><input type='checkbox' name='cam_{html.escape(c['id'])}' value='1' style='width:auto'"
                f"{' checked' if c['id'] in picked else ''}> {html.escape(c['name'])}"
                f"<small> {'doorbell, ' if c['doorbell'] else ''}{html.escape(', '.join(c['smart']) or 'motion only')}"
                f"{'' if c['state'] in ('', 'CONNECTED') else ', ' + html.escape(c['state'].lower())}</small></label>" for c in cams)
            ev = ("events connected" if protect.EVENTS.connected else
                  ("events not connected yet" + (f": {html.escape(protect.EVENTS.error)}" if protect.EVENTS.error else "")))
            probs = "".join(notice("err", t) for t in engine.camera_problems(stack).values())
            cams_html = probs + f"""<form method="post" action="/protect/cameras"><fieldset><legend>Cameras on the screen ({ev})</legend>
{rows or '<p>No cameras on this console.</p>'}
<label><input type="checkbox" name="highlight_motion" value="1" style="width:auto"{' checked' if d['highlight_motion'] else ''}>
 Also bring cameras to the front for plain motion (not only people, vehicles, animals, packages and the doorbell)</label>
<label>Camera picture <select name="protect_quality">{''.join(f"<option value='{v}'{' selected' if d.get('quality', 'auto') == v else ''}>{html.escape(t)}</option>" for v, t in protect.QUALITIES)}</select></label>
<small>Now: {html.escape(d.get('applied_quality') or protect.resolve_quality(d.get('quality', 'auto')))}. Playing the video is most of this box's work; low quality takes about a third less on a Pi 4.
The video also stops while the screen is off.</small>
<p><button>Save cameras</button></p></fieldset></form>
<form method="post" action="/protect/forget"><p><button>Forget this console</button></p></form>"""
        except protect.ProtectError as e:
            cams_html = f"<p class='err'>{html.escape(str(e))}</p>"
    return f"""<h2 id="protect">UniFi Protect cameras (optional)</h2>{note}
<form method="post" action="/protect/connect"><fieldset><legend>UniFi Protect console</legend>
<p><small>Lists the cameras so you can pick them (their stream links are filled in for you), and brings a camera to the
front of the screen when it sees a person, vehicle, animal or package, or the doorbell rings. Make an API key on the console:
UniFi OS &rarr; Settings &rarr; Control Plane &rarr; Integrations &rarr; Create API Key.</small></p>
<div class="row"><label>Console address {f"<input name='protect_host' value='{html.escape(d['host'])}' placeholder='192.168.1.1'>"}</label>
<label>API key <input name="protect_key" type="password" autocomplete="off"></label></div>{saved}
<p><button>Save console and connect</button></p></fieldset></form>{cams_html}"""


def _protect(stack, action, f):
    d = protect.settings(stack)
    try:
        if action == "/protect/connect":
            host = protect.clean_host(f.get("protect_host"))
            key = f.get("protect_key", "").strip() or (d["api_key"] if host == d["host"] else "")
            if not key:
                return "the API key is needed"
            cams = protect.list_cameras(host, key)
            protect.save(stack, {**d, "host": host, "api_key": key})
            if protect.ha_action(stack):            # into Home Assistant too, in the background (it restarts once)
                threading.Thread(target=engine.sync_names, args=(stack,), daemon=True).start()
                return (f"connected: {len(cams)} camera(s). Tick the ones for the screen. They're being added to "
                        "Home Assistant as well (it restarts once).")
            return f"connected: {len(cams)} camera(s). Tick the ones for the screen."
        if action == "/protect/forget":
            changed = protect.apply_cameras(stack, [], [])
            protect.save(stack, dict(protect.DEFAULTS))
            msgs = ["forgotten"]
        else:
            cams = protect.list_cameras(d["host"], d["api_key"])
            picked = [c["id"] for c in cams if f.get(f"cam_{c['id']}") == "1"]
            q = f.get("protect_quality") if f.get("protect_quality") in dict(protect.QUALITIES) else "auto"
            protect.save(stack, {**protect.settings(stack), "highlight_motion": f.get("highlight_motion") == "1", "quality": q})
            changed = protect.apply_cameras(stack, picked, cams)
            msgs = [f"{len(picked)} camera(s) on the screen"]
            if protect.LAST_DUPES:
                msgs.append("removed the stream link for " + ", ".join(protect.LAST_DUPES)
                            + ": the same camera as one picked from UniFi Protect")
    except (ValueError, protect.ProtectError) as e:
        return f"not saved: {e}"
    if changed:
        msgs.append(engine.restart_go2rtc(stack))
    if action != "/protect/forget":
        threading.Thread(target=lambda: (time.sleep(5), engine.refresh(stack)), daemon=True).start()
        msgs.append("checking each camera's video in the background (reload this page in a minute)")
    protect.listen(stack)
    return "; ".join(msgs) + "."


def watch_events(stack):
    """Phone notifications for doorbell rings and people (once per process): each Protect event goes to
    cameras.notify_event in its own thread, and from there to the notifications module while that is on."""
    if getattr(watch_events, "on", False):
        return
    watch_events.on = True

    def submit(alert):
        import modules
        from modules import notifications
        if modules.load(stack, "notifications")["enabled"]:
            notify.submit(stack, notifications.settings(stack), alert, voice_on=False)

    def on_event(device, label, at):
        threading.Thread(target=lambda: engine.notify_event(stack, device, label, at, submit), daemon=True).start()
    protect.EVENTS.listeners.append(on_event)


def _protect_route(action):
    return lambda stack, form: Response(setup_msg=_protect(stack, action, form))


def display_data(stack, site, now):
    d = _display(stack)
    probs = engine.camera_problems(stack)
    return {"cameras": [{"id": f"cam{i}", "name": c.get("name") or f"Camera {i}", "problem": probs.get(f"cam{i}", "")}
                        for i, c in enumerate(d["cameras"], 1)],
            "sounds": d.get("sounds") or {}, "sound_cooldown": d.get("sound_cooldown", 60),
            "sound_gap": d.get("sound_gap", 5), "doorbell_always": d.get("doorbell_always", True)}


def _compose(stack, *args):
    try:
        subprocess.run(ss.compose_cmd(stack) + list(args), capture_output=True, text=True)
    except OSError:
        pass


def on_enable(stack):
    import display
    display.save(stack, display.load(stack))           # go2rtc's config
    _compose(stack, "up", "-d", "go2rtc")
    watch_events(stack)
    protect.listen(stack)


def on_disable(stack):
    _compose(stack, "rm", "-s", "-f", "go2rtc")
    protect.stop()


MODULE = Module(
    id="cameras",
    title="Cameras",
    description="UniFi Protect or other cameras next to the power figures, with chimes when they see someone",
    order=15,
    default_enabled=False,
    detect=detect,
    migrate=migrate,
    on_enable=on_enable,
    on_disable=on_disable,
    form_section=form_section,
    form_check=form_check,
    form_save=form_save,
    setup_section=setup_section,
    routes={("POST", a): _protect_route(a) for a in ("/protect/connect", "/protect/cameras", "/protect/forget")},
    display_data=display_data,
    display_events=engine.detections,
    every={3600: engine.refresh,        # Protect's stream links (they change) and each camera's codec
           300: engine.sync_names},     # cameras renamed in Protect
    compose_profiles=lambda stack: ["cameras"],
)
