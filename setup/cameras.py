#!/usr/bin/env python3
"""Cameras on the wall display: go2rtc's config, the cameras' detections (UniFi Protect, protect.py) for the screen,
and the hourly upkeep (Protect's changing stream links, each camera's codec). The "cameras" module
(setup/modules/cameras) is its setup page and switch.

The camera settings stay in state/display.json (the screen reads them): "cameras" [{name, url, protect_id?, ...}],
"sounds", "sound_cooldown", "sound_gap", "doorbell_always" and the voice wording "speak_template"/"speak_doorbell".
go2rtc (compose service "go2rtc", profile "cameras") turns the cameras' RTSP(S) streams into MSE for the kiosk; it
listens on 127.0.0.1 only.
"""
import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request

import solar_setup as ss

COOLDOWNS = [(0, "every time"), (30, "30 seconds"), (60, "1 minute"), (120, "2 minutes"), (300, "5 minutes"), (600, "10 minutes"),
             (1800, "30 minutes")]
GAPS = [(0, "no limit"), (5, "5 seconds"), (10, "10 seconds"), (30, "30 seconds"), (60, "1 minute")]
SOUND_KINDS = [("doorbell", "Doorbell", True), ("person", "Person", True), ("vehicle", "Vehicle", False),
               ("animal", "Animal", False), ("package", "Package", False)]
MAX_CAMERAS = 16   # all of them rotate through the two places on screen


def go2rtc_source(url):
    """Camera link -> go2rtc source. UniFi Protect gives rtsps://host:7441/TOKEN?enableSrtp with a
    self-signed certificate; go2rtc's rtspx:// is RTSPS without the certificate check."""
    u = urllib.parse.urlsplit(url.strip())
    if u.scheme not in ("rtsp", "rtsps", "rtspx") or not u.hostname:
        raise ValueError("a camera link must start with rtsp:// or rtsps:// and include the camera's address")
    if u.scheme == "rtsps":
        q = "&".join(p for p in u.query.split("&") if p and p.lower() != "enablesrtp")
        u = u._replace(scheme="rtspx", query=q)
    return urllib.parse.urlunsplit(u)


def write_go2rtc(stack, cameras):
    """go2rtc's config for these cameras. Returns True if it changed (go2rtc then needs a restart)."""
    lines = ["# Written by the solar monitor setup page (cameras for the HDMI display); edits are overwritten.",
             "api:", "  listen: \"127.0.0.1:1984\"", "  origin: \"*\"",
             "rtsp:", "  listen: \"\"", "webrtc:", "  listen: \"\"", "log:", "  level: warn", "streams:"]
    for i, c in enumerate(cameras, start=1):
        lines.append(f"  cam{i}: {json.dumps(go2rtc_source(c['url']))}")
    if not cameras:
        lines[-1] = "streams: {}"
    os.makedirs(os.path.join(stack, "go2rtc"), exist_ok=True)
    return ss.write_if_changed(os.path.join(stack, "go2rtc", "go2rtc.yaml"), "\n".join(lines) + "\n", 0o600)


def restart_go2rtc(stack):
    """Returns a message for the page."""
    try:
        r = subprocess.run(ss.compose_cmd(stack) + ["up", "-d", "--force-recreate", "go2rtc"], capture_output=True, text=True)
    except OSError as e:
        r = subprocess.CompletedProcess([], 1, "", str(e))
    return "cameras updated" if r.returncode == 0 else f"camera service restart failed: {r.stderr.strip()[-200:]}"


def _cameras(stack):
    import display
    return display.load(stack)


def detections(stack):
    """Cameras with a detection now (UniFi Protect), keyed by the display's camera ids (cam1...); notes the ones
    that wake the screen (screenpower.py)."""
    import protect
    import screenpower as sp
    protect.listen(stack)                 # (re)starts the event listener if the settings changed
    d = _cameras(stack)
    active = protect.EVENTS.active(protect.settings(stack).get("highlight_motion", False))
    out = {}
    for i, c in enumerate(d["cameras"], 1):
        if c.get("protect_id") in active:
            out[f"cam{i}"] = active[c["protect_id"]]
    sp.note_events(d, out, {f"cam{i}": c.get("name") or f"Camera {i}" for i, c in enumerate(d["cameras"], 1)})
    return {"active": out, "connected": protect.EVENTS.connected}


def stream_codec(cam_id, secs=6, base="http://127.0.0.1:1984"):
    """The video codec go2rtc gets from one of the screen's cameras ("H264", "H265"...), or "" if no video arrived.
    Pulls the stream for a few seconds so go2rtc connects, and reads what the camera sent."""
    def pull():
        try:
            with urllib.request.urlopen(f"{base}/api/stream.mp4?src={urllib.parse.quote(cam_id)}", timeout=secs + 2) as r:
                end = time.time() + secs
                while time.time() < end and r.read(16384):
                    pass
        except OSError:
            pass
    t = threading.Thread(target=pull, daemon=True)
    t.start()
    codec = ""
    for _ in range(secs * 2):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(f"{base}/api/streams?src={urllib.parse.quote(cam_id)}", timeout=3) as r:
                s = json.load(r)
        except (OSError, ValueError):
            continue
        for p in s.get("producers") or []:
            for m in p.get("medias") or []:
                if m.startswith("video"):
                    codec = m.split(",")[-1].strip().split("/")[0].upper()
        if codec:
            break
    t.join(secs + 3)
    return codec


def check_cameras(stack):
    """Each camera's codec, kept in state/camera-check.json for the screen and the setup page. The screen's Chromium
    plays H.264 only: a camera sending H.265 (UniFi Protect's Enhanced Encoding) stays black (seen on site)."""
    out = {}
    for i, c in enumerate(_cameras(stack)["cameras"], 1):
        out[f"cam{i}"] = {"name": c.get("name") or f"Camera {i}", "codec": stream_codec(f"cam{i}"), "at": int(time.time())}
    ss.write_if_changed(os.path.join(stack, "state", "camera-check.json"), json.dumps(out, indent=2) + "\n", 0o644)
    return out


def camera_problems(stack):
    """{camN: text} for cameras the screen can't show, from the last check."""
    chk = ss.load_json(os.path.join(stack, "state", "camera-check.json"), {})
    names = {f"cam{i}": c.get("name") for i, c in enumerate(_cameras(stack)["cameras"], 1)}
    out = {}
    for cam, r in chk.items():
        if names.get(cam) != r.get("name"):
            continue                                  # cameras changed since the check
        if r.get("codec") in ("H265", "HEVC"):
            out[cam] = (f"{r['name']} sends H.265 video, which this screen can't play. In UniFi Protect, open the camera's "
                        "settings and turn off Enhanced Encoding.")
        elif r.get("codec") == "":
            out[cam] = f"{r['name']} sent no video when checked."
    return out


def sync_names(stack):
    """Every 5 minutes: Protect in Home Assistant (added once), cameras renamed in UniFi Protect take the new name
    on the screen and in the announcements (whose phrases are made ahead of time again when the voice is on), and
    the Home dashboard follows Home Assistant's camera entities. Returns messages to log."""
    import hadashboard
    import protect
    msgs = protect.ensure_in_ha(stack)      # the cameras in Home Assistant too (once; restarts it then)
    try:
        renamed = protect.sync_names(stack)
    except (OSError, ValueError, protect.ProtectError) as e:
        renamed = {}
        msgs.append(f"camera name check failed: {e}")
    if renamed:
        prewarm_voice(stack, renamed.values())
        msgs += [f"camera renamed in Protect: {old} -> {new}" for old, new in renamed.items()]
    if hadashboard.write(stack):            # new camera entities in Home Assistant, or renamed ones
        msgs.append("Home dashboard updated")
    return msgs


def prewarm_voice(stack, names):
    """With the voice on, make the phrases for these cameras now, so the first announcement isn't late (they are
    made on demand otherwise)."""
    import tts
    from modules import voice
    try:
        if not voice.is_on(stack):
            return
        d = _cameras(stack)
        tts.prewarm(stack, [c for c in d["cameras"] if c.get("name") in set(names)],
                    {**d, "voice": voice.settings(stack)["voice"]})
    except (OSError, ValueError, KeyError):
        pass


def refresh(stack, check=True):
    """Protect cameras at the quality the setting asks for, with the links Protect currently hands out (it changes
    them; a stale link left a camera black on site), then each camera's codec (state/camera-check.json).
    Returns messages to log."""
    import protect
    msgs = []
    try:
        if protect.refresh_quality(stack):
            restart_go2rtc(stack)
            msgs.append(f"camera links refreshed at {protect.settings(stack).get('applied_quality')}")
            time.sleep(5)
    except (OSError, ValueError, protect.ProtectError) as e:
        msgs.append(f"camera link refresh failed: {e}")
    if check and _cameras(stack)["cameras"]:
        try:
            r = check_cameras(stack)
            msgs.append(f"camera check: { {k: v['codec'] or 'no video' for k, v in r.items()} }")
        except OSError as e:
            msgs.append(f"camera check failed: {e}")
    return msgs
