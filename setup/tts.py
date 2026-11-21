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


def prewarm(stack, cameras, settings, synth=synthesize):
    """Make every camera's phrases ahead of time (in the background when settings are saved)."""
    from display import SOUND_KINDS
    labels = {"doorbell": "Doorbell", "person": "Person", "vehicle": "Vehicle", "animal": "Animal", "package": "Package"}
    on = [labels[k] for k, _, _ in SOUND_KINDS if (settings.get("sounds") or {}).get(k)]
    made = 0
    for c in cameras:
        for label in on:
            if label == "Doorbell" and not c.get("doorbell"):
                continue
            try:
                wav(stack, phrase(label, c["name"], settings.get("speak_template"), settings.get("speak_doorbell")),
                    settings.get("voice"), synth)
                made += 1
            except (OSError, ValueError):
                return made
    return made


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
