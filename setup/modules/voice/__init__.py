"""Voice announcements through the screen's speakers: Piper text-to-speech on this box (setup/tts.py), as a service
other modules use. A module calls announce(stack, text) and the wall display speaks it (after a chime, unless the
setup page says "only the announcement"). Camera events are spoken from the camera settings (their wording is
there), through the same voice.

  announce(stack, "The water tank is below 20%", chime="doorbell")

The queue is state/voice-queue.json, so announcements can come from any process (the collector, the web server);
the display picks up new ones within a couple of seconds and announcements older than two minutes are dropped
(a screen that was off doesn't read out a backlog).
"""
import html
import json
import os
import subprocess
import threading
import time

import solar_setup as ss
import tts
from modules import Module, Response

LEGACY = ("state", "display.json")       # where the settings were before modules (v0.2.31 and earlier)
QUEUE = ("state", "voice-queue.json")
FRESH_S = 120
MODES = (("after", "Chime, then the announcement"), ("only", "Only the announcement"))
CHIMES = ("doorbell", "person", "vehicle", "animal", "package")
TEST_TEXT = "This is a test announcement from the solar monitor."
_queue_lock = threading.Lock()           # the queue file, within one process
_start_lock = threading.Lock()           # one speech start at a time (web server)


def _legacy(stack):
    return ss.load_json(os.path.join(stack, *LEGACY), {})


def detect(stack):
    return bool(_legacy(stack).get("speak", False))


def migrate(stack, old, version):
    if old is None:
        d = _legacy(stack)
        return {"voice": d.get("voice") or "en_US-lessac-medium", "mode": "only" if d.get("speak_mode") == "only" else "after"}
    return old


def settings(stack):
    import modules
    return modules.settings(stack, "voice")


def is_on(stack):
    try:
        import modules
        return modules.load(stack, "voice")["enabled"]
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# The announcement service
# --------------------------------------------------------------------------
def _queue_path(stack):
    return os.path.join(stack, *QUEUE)


def announce(stack, text, chime=None, now=None):
    """Have the wall display say `text` (after the chime `chime`, one of CHIMES, if the setup page wants chimes).
    Does nothing while voice announcements are off. Returns the announcement's id, or None."""
    if not is_on(stack):
        return None
    text = " ".join(str(text).split())[:300]
    if not text:
        return None
    now = now or time.time()
    with _queue_lock:
        q = [a for a in ss.load_json(_queue_path(stack), []) if now - a["at"] < FRESH_S]
        aid = max([int(now * 1000)] + [a["id"] + 1 for a in q])
        q.append({"id": aid, "text": text, "chime": chime if chime in CHIMES else None, "at": now})
        ss.write_if_changed(_queue_path(stack), json.dumps(q[-20:]) + "\n", 0o644)
    return aid


def pending(stack, now=None):
    """Announcements for the display: [{id, chime}] (the text is fetched as speech by id)."""
    now = now or time.time()
    return [{"id": a["id"], "chime": a["chime"]} for a in ss.load_json(_queue_path(stack), []) if now - a["at"] < FRESH_S]


def text_of(stack, aid):
    for a in ss.load_json(_queue_path(stack), []):
        if a["id"] == aid:
            return a["text"]
    return None


# --------------------------------------------------------------------------
# The speech service (Piper): started when voice is turned on or the voice changes
# --------------------------------------------------------------------------
def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def _camera_settings(stack):
    """The cameras and their wording (camera settings, display.json), for making their phrases ahead of time."""
    import display
    return display.load(stack)


def start(stack, changed):
    """In the background: start Piper (downloading it the first time, with progress for the setup page), wait for
    the voice, then make the cameras' phrases ahead of time."""
    def mb(b):
        return f"{b / 1e6:.0f} MB"

    def run():
        with _start_lock:
            voice = settings(stack)["voice"]
            try:
                if changed:
                    ref = tts.image_ref(stack)
                    if not tts.image_present(ref):
                        tts.set_progress(active=True, step="Downloading the speech service", pct=0, detail="", error="")
                        best = [0]

                        def report(done, total, extracting):
                            pct = int(done * 100 / total) if total else 0
                            best[0] = max(best[0], pct)          # layer sizes arrive one by one: never go back
                            tts.set_progress(step="Unpacking the speech service" if extracting and best[0] >= 99
                                             else "Downloading the speech service", pct=best[0],
                                             detail=f"{mb(done)} of {mb(total)}" if total else "")
                        tts.pull_image(ref, report)
                    tts.set_progress(active=True, step="Starting the speech service", pct=None, detail="", error="")
                    _run(ss.compose_cmd(stack) + ["up", "-d", "--force-recreate", "tts"])
                for _ in range(360):         # wait for Piper and its first voice download (up to an hour)
                    if tts.status()["ok"]:
                        break
                    got = tts.voice_bytes(voice)
                    if got and got < tts.VOICE_BYTES * 1.05:
                        tts.set_progress(active=True, step="Downloading the voice", pct=min(99, int(got * 100 / tts.VOICE_BYTES)),
                                         detail=f"{mb(got)} of about {mb(tts.VOICE_BYTES)}", error="")
                    elif tts.PROGRESS["active"]:
                        tts.set_progress(step="Starting the speech service", pct=None, detail="")
                    time.sleep(5 if tts.PROGRESS["active"] else 10)
                else:
                    raise OSError("the speech service didn't answer within an hour")
                if tts.PROGRESS["active"]:
                    tts.set_progress(step="Preparing the announcements", pct=0, detail="")
                cams = _camera_settings(stack)
                tts.prewarm(stack, cams["cameras"], {**cams, "voice": voice},
                            progress=lambda made, total: tts.PROGRESS["active"] and tts.set_progress(
                                pct=int(made * 100 / total), detail=f"{made} of {total} phrases"))
                tts.set_progress(active=False, step="", pct=None, detail="", error="")
            except OSError as e:
                tts.set_progress(active=False, error=str(e)[:200])
    threading.Thread(target=run, daemon=True).start()


def stop(stack):
    _run(ss.compose_cmd(stack) + ["rm", "-s", "-f", "tts"])


# --------------------------------------------------------------------------
# Setup page
# --------------------------------------------------------------------------
def status_html(stack):
    """The speech service's state: with a progress bar while it is downloading (refreshes itself)."""
    p = tts.PROGRESS
    if p["error"]:
        return f"<span id='speechprog' class='err'>Speech setup stopped: {html.escape(p['error'])}</span>"
    if not p["active"]:
        if tts.status()["ok"]:
            return "The speech service is running."
        return html.escape("The speech service is starting (the first time it downloads about 200 MB and the voice; "
                           "it then works offline).")
    bar = (f"<progress max='100' value='{p['pct']}' style='width:100%;max-width:340px;height:14px'></progress>"
           if p["pct"] is not None else "<progress style='width:100%;max-width:340px;height:14px'></progress>")
    return (f"<span id='speechprog' style='display:block;margin-top:6px'>{html.escape(p['step'])}"
            f"{(' ' + str(p['pct']) + '%') if p['pct'] is not None else ''}<br>{bar}<br>{html.escape(p['detail'])}</span>"
            """<script>(function(){const t=setInterval(async()=>{try{const r=await fetch('/speech/progress',{cache:'no-store'});
const p=await r.json(),e=document.getElementById('speechprog');if(!e)return clearInterval(t);
if(!p.active){clearInterval(t);e.textContent=p.error?('Speech setup stopped: '+p.error):'The speech service is running.';return}
const b=e.querySelector('progress');if(p.pct===null)b.removeAttribute('value');else b.value=p.pct;
e.firstChild.textContent=p.step+(p.pct===null?'':' '+p.pct+'%');e.lastChild.textContent=p.detail}catch(x){}},2000)})()</script>""")


def form_section(stack):
    s = settings(stack)
    modes = "".join(f"<option value='{v}'{' selected' if s['mode'] == v else ''}>{t}</option>" for v, t in MODES)
    voices = "".join(f"<option value='{v}'{' selected' if s['voice'] == v else ''}>{html.escape(t)}</option>" for v, t in tts.VOICES)
    return f"""<fieldset id="voice"><legend>Voice announcements</legend>
<small>Spoken through the screen's speakers (HDMI) by Piper text-to-speech on this box: what a camera saw (its wording is
under Cameras), and alerts from other features. The screen's volume and quiet hours apply.</small>
<div class="row"><label>How <select name="speak_mode">{modes}</select></label>
<label>Voice <select name="voice">{voices}</select></label></div>
<small>{status_html(stack)}</small>
<p><button formaction="/voice/test" formmethod="post" formnovalidate>Play a test announcement</button>
<small style="display:inline">on the screen (save first if you changed something).</small></p></fieldset>"""


def form_save(stack, form):
    import modules
    cur = settings(stack)
    new = {"voice": form.get("voice") if form.get("voice") in dict(tts.VOICES) else "en_US-lessac-medium",
           "mode": "only" if form.get("speak_mode") == "only" else "after"}
    modules.save(stack, "voice", new)
    changed = new["voice"] != cur["voice"]
    if changed:
        ss.write_env(stack, ss.ensure_secrets(stack))      # TTS_VOICE
    start(stack, changed)                                   # also makes new camera phrases ahead of time
    return ["voice changed (the new voice downloads first, a few minutes)"] if changed else []


def on_enable(stack):
    start(stack, True)


def test(stack, form):
    announce(stack, TEST_TEXT)
    return Response(done=["test announcement sent to the screen"])


def progress(stack, form):
    return Response(body=json.dumps(tts.PROGRESS), ctype="application/json")


MODULE = Module(
    id="voice",
    title="Voice announcements",
    description="spoken alerts through the screen's speakers (Piper text-to-speech, on this box)",
    order=50,
    default_enabled=False,
    detect=detect,
    defaults={"voice": "en_US-lessac-medium", "mode": "after"},
    migrate=migrate,
    on_enable=on_enable,
    on_disable=stop,
    form_section=form_section,
    form_save=form_save,
    display_data=lambda stack, site, now: {"speak": True, "speak_mode": settings(stack)["mode"]},
    display_events=lambda stack: {"say": pending(stack)},
    routes={("POST", "/voice/test"): test, ("GET", "/speech/progress"): progress},
    compose_profiles=lambda stack: ["tts"],
    env=lambda stack: ([f"TTS_VOICE={settings(stack)['voice']}"]
                       if all(c.isalnum() or c in "_-" for c in settings(stack)["voice"]) else []),
)
