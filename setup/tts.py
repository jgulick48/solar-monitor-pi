#!/usr/bin/env python3
"""Spoken announcements for the wall display: Piper text-to-speech, like UniFi's Alarm Manager.

Piper runs in the optional `tts` compose service (rhasspy/wyoming-piper, the voice Home Assistant uses for
local voice), on 127.0.0.1:10200. It isn't in the image (about 200 MB): turning speech on pulls it and the
chosen voice once. This module speaks its Wyoming protocol (JSON header lines, optional data and payload) with
the standard library, turns the PCM it sends back into a WAV and caches it in state/tts-cache, so a phrase is
made once (about a second on a Pi 4) and then plays at once. When settings are saved, the phrases for every
camera are made ahead of time.

Wording: "{object} at the {camera}" by default ("Person at the Front door"), the doorbell "Someone's at the
{camera}"; both editable on the setup page. {object} is Person, Vehicle, Animal or Package.
"""
import hashlib
import io
import json
import os
import socket
import sys
import threading
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HOST, PORT = os.environ.get("SOLAR_TTS_HOST", "127.0.0.1"), int(os.environ.get("SOLAR_TTS_PORT", "10200"))
VOICES = [("en_US-lessac-medium", "US English, Lessac (clear, neutral)"), ("en_US-amy-medium", "US English, Amy"),
          ("en_US-ryan-medium", "US English, Ryan"), ("en_GB-alba-medium", "British English, Alba")]
DEFAULT_TEMPLATE = "{object} at the {camera}"
DEFAULT_DOORBELL = "Someone's at the {camera}"
KEEP = 300                          # cached phrases


def cache_dir(stack):
    return os.path.join(stack, "state", "tts-cache")


def phrase(label, camera, template=DEFAULT_TEMPLATE, doorbell=DEFAULT_DOORBELL):
    t = doorbell if label == "Doorbell" else template
    try:
        return (t or DEFAULT_TEMPLATE).format(object=label, camera=camera).strip()[:200]
    except (KeyError, IndexError, ValueError):
        return f"{label} at the {camera}"


def check_template(t):
    """Raises ValueError if a template uses anything but {object} and {camera}."""
    try:
        t.format(object="Person", camera="Front door")
    except (KeyError, IndexError, ValueError):
        raise ValueError("the announcement can only use {object} and {camera}")
    if len(t) > 120:
        raise ValueError("the announcement is too long")


# ---------------------------------------------------------------------- Wyoming protocol
def _send(sock, etype, data=None, payload=b""):
    header = {"type": etype, "version": "1.5.0"}
    body = json.dumps(data or {}).encode()
    header["data_length"] = len(body)
    if payload:
        header["payload_length"] = len(payload)
    sock.sendall(json.dumps(header).encode() + b"\n" + body + payload)


def _events(sock):
    """Yield (type, data, payload) from the server."""
    f = sock.makefile("rb")
    while True:
        line = f.readline()
        if not line:
            return
        h = json.loads(line)
        data = h.get("data") or {}
        if h.get("data_length"):
            data = {**data, **json.loads(f.read(h["data_length"]))}
        payload = f.read(h["payload_length"]) if h.get("payload_length") else b""
        yield h["type"], data, payload


def synthesize(text, voice=None, host=HOST, port=PORT, timeout=60):
    """WAV bytes for text. Raises OSError/ValueError if Piper isn't there or says no."""
    with socket.create_connection((host, port), timeout=timeout) as s:
        req = {"text": text}
        if voice:
            req["voice"] = {"name": voice}
        _send(s, "synthesize", req)
        pcm, fmt = bytearray(), None
        for etype, data, payload in _events(s):
            if etype == "audio-start":
                fmt = (data.get("rate", 22050), data.get("width", 2), data.get("channels", 1))
            elif etype == "audio-chunk":
                fmt = fmt or (data.get("rate", 22050), data.get("width", 2), data.get("channels", 1))
                pcm += payload
            elif etype == "audio-stop":
                break
            elif etype == "error":
                raise ValueError(data.get("text") or "Piper error")
    if not pcm or not fmt:
        raise ValueError("Piper sent no audio")
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setframerate(fmt[0])
        w.setsampwidth(fmt[1])
        w.setnchannels(fmt[2])
        w.writeframes(bytes(pcm))
    return out.getvalue()


# ---------------------------------------------------------------------- cache
_lock = threading.Lock()


def _key(text, voice):
    return hashlib.sha1(f"{voice}|{text}".encode()).hexdigest()[:20]


def wav(stack, text, voice, synth=synthesize):
    """The phrase as WAV bytes, from the cache or made now."""
    d = cache_dir(stack)
    path = os.path.join(d, _key(text, voice) + ".wav")
    try:
        with open(path, "rb") as f:
            os.utime(path)
            return f.read()
    except FileNotFoundError:
        pass
    data = synth(text, voice)
    with _lock:
        os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        files = sorted((os.path.join(d, n) for n in os.listdir(d) if n.endswith(".wav")), key=os.path.getmtime)
        for old in files[:-KEEP]:
            os.remove(old)
    return data


def prewarm(stack, cameras, settings, synth=synthesize, progress=None):
    """Make every camera's phrases ahead of time (in the background when settings are saved).
    progress(made, total) is called as it goes."""
    from display import SOUND_KINDS
    labels = {"doorbell": "Doorbell", "person": "Person", "vehicle": "Vehicle", "animal": "Animal", "package": "Package"}
    on = [labels[k] for k, _, _ in SOUND_KINDS if (settings.get("sounds") or {}).get(k)]
    todo = [(c, label) for c in cameras for label in on if label != "Doorbell" or c.get("doorbell")]
    made = 0
    for c, label in todo:
        try:
            wav(stack, phrase(label, c["name"], settings.get("speak_template"), settings.get("speak_doorbell")),
                settings.get("voice"), synth)
            made += 1
        except (OSError, ValueError):
            return made
        if progress:
            progress(made, len(todo))
    return made


# ---------------------------------------------------------------------- first-time download, with progress
# Turning speech on the first time downloads the Piper image (about 200 MB) and then Piper downloads the voice
# (about 63 MB) into the tts-data volume. The setup page shows a progress bar from PROGRESS: the image through
# Docker's own API (bytes per layer), the voice by the size of its file as it grows, then the phrases being made.
PROGRESS = {"active": False, "step": "", "pct": None, "detail": "", "error": "", "at": 0}
DOCKER_SOCK = "/var/run/docker.sock"
VOICE_BYTES = 63_201_294          # en_US-lessac-medium.onnx; the other medium voices are about the same


def _docker(method, path, timeout=30):
    import http.client

    class Conn(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect(DOCKER_SOCK)
    c = Conn("localhost", timeout=timeout)
    c.request(method, path)
    return c.getresponse()


def image_ref(stack):
    """The Piper image the stack uses (stack/compose.yaml)."""
    try:
        for line in open(os.path.join(stack, "compose.yaml")):
            if "image:" in line and "wyoming-piper" in line:
                return line.split("image:", 1)[1].strip()
    except OSError:
        pass
    return "rhasspy/wyoming-piper:2.5.4"


def image_present(ref):
    import urllib.parse
    try:
        r = _docker("GET", f"/images/{urllib.parse.quote(ref, safe='')}/json")
        r.read()
        return r.status == 200
    except OSError:
        return False


def pull_image(ref, report):
    """Pull through Docker's API; report(done_bytes, total_bytes, extracting) as layers download."""
    import urllib.parse
    name, _, tag = ref.rpartition(":")
    r = _docker("POST", f"/images/create?fromImage={urllib.parse.quote(name)}&tag={urllib.parse.quote(tag)}", timeout=120)
    if r.status != 200:
        raise OSError(f"Docker said {r.status} {r.read()[:200]!r}")
    layers = {}                                       # id -> [done, total]
    while True:
        line = r.readline()
        if not line:
            break
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("error"):
            raise OSError(m["error"])
        lid, st, pd = m.get("id"), m.get("status", ""), m.get("progressDetail") or {}
        if not lid:
            continue
        if st == "Downloading" and pd.get("total"):
            layers[lid] = [pd.get("current", 0), pd["total"]]
        elif st in ("Download complete", "Verifying Checksum", "Pull complete", "Extracting") and lid in layers:
            layers[lid][0] = layers[lid][1]
        report(sum(v[0] for v in layers.values()), sum(v[1] for v in layers.values()), st in ("Extracting", "Pull complete"))


def voice_bytes(voice):
    """Bytes of the voice downloaded so far in the tts-data volume (Piper writes it while downloading)."""
    try:
        r = _docker("GET", "/volumes")
        vols = json.loads(r.read()).get("Volumes") or []
    except (OSError, ValueError):
        return 0
    total = 0
    for v in vols:
        if v.get("Name", "").endswith("tts-data") and v.get("Mountpoint"):
            try:
                for n in os.listdir(v["Mountpoint"]):
                    if n.startswith(voice or "") and ".onnx" in n and not n.endswith(".json"):
                        total = max(total, os.path.getsize(os.path.join(v["Mountpoint"], n)))
            except OSError:
                pass
    return total


def set_progress(**kw):
    import time
    PROGRESS.update(at=time.time(), **kw)


def status(host=HOST, port=PORT):
    """Is Piper answering? (A describe/info round trip.)"""
    try:
        with socket.create_connection((host, port), timeout=3) as s:
            _send(s, "describe")
            for etype, data, _ in _events(s):
                if etype == "info":
                    voices = [v.get("name") for t in data.get("tts", []) for v in t.get("voices", [])]
                    return {"ok": True, "voices": voices}
    except (OSError, ValueError):
        pass
    return {"ok": False, "voices": []}
