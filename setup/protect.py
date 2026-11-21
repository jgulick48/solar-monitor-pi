#!/usr/bin/env python3
"""UniFi Protect: cameras and detection events for the wall display, through Protect's Integration API.

The setup page takes the Protect console's address and an API key (UniFi OS: Settings -> Control Plane ->
Integrations -> create API key). With those the box:

  - lists the cameras (GET /proxy/protect/integration/v1/cameras) so they can be picked for the screen,
  - gets each picked camera's RTSPS link (GET .../cameras/{id}/rtsps-stream; if there is no "medium"
    stream yet, POST creates one), which go2rtc plays like any other camera link,
  - keeps a WebSocket open to .../subscribe/events and remembers recent detections per camera, which the
    display uses to bring cameras with something happening to the front.

Events: {"type": "add"|"update"|"remove", "item": {"modelKey": "event", "type": ..., "device": <camera id>,
"start": ms, "end": ms|null, "smartDetectTypes": [...]}}. "ring" is the doorbell; smart detections carry
smartDetectTypes (person, vehicle, animal, package, ...); anything else from a camera counts as motion, which
only highlights a camera when "highlight plain motion" is ticked.

Settings, with the API key, in state/protect.json (mode 600). Standard library only (the WebSocket client is
the few lines below). The console's certificate is self-signed, so it isn't checked; the key only goes to the
address entered on the setup page.
"""
import base64
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solar_setup as ss  # noqa: E402

BASE = "/proxy/protect/integration/v1"
DEFAULTS = {"host": "", "api_key": "", "cameras": [], "highlight_motion": False, "quality": "auto", "applied_quality": ""}
HOLD_S = 60            # a camera stays highlighted this long after its last event ended
OPEN_MAX_S = 300       # an event without an end counts as over after this long
LABELS = {"person": "Person", "vehicle": "Vehicle", "animal": "Animal", "package": "Package", "licensePlate": "Vehicle",
          "face": "Person"}
LEVEL = {"Doorbell": 3, "Person": 2, "Vehicle": 2, "Animal": 2, "Package": 2, "Motion": 1}


def path(stack):
    return os.path.join(stack, "state", "protect.json")


def settings(stack):
    return {**DEFAULTS, **ss.load_json(path(stack), {})}


def save(stack, d):
    ss.write_if_changed(path(stack), json.dumps(d, indent=2) + "\n", 0o600)


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE     # UniFi consoles use a self-signed certificate
    return c


class ProtectError(Exception):
    pass


def api(host, key, method, rel, body=None, timeout=15, opener=None):
    url = f"https://{host}{BASE}{rel}"
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-API-Key": key, "Accept": "application/json", "Content-Type": "application/json"})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout, context=_ctx()) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ProtectError("the console didn't accept the API key")
        if e.code == 404:
            raise ProtectError("no UniFi Protect Integration API at that address (is Protect up to date?)")
        raise ProtectError(f"UniFi Protect said {e.code} {e.reason}")
    except (urllib.error.URLError, OSError) as e:
        raise ProtectError(f"can't reach {host}: {getattr(e, 'reason', e)}")
    return json.loads(raw) if raw else None


def clean_host(s):
    s = (s or "").strip()
    for p in ("https://", "http://"):
        s = s.removeprefix(p)
    s = s.split("/")[0]
    if not s or any(c.isspace() for c in s):
        raise ValueError("enter the Protect console's address, e.g. 192.168.1.1")
    return s


def list_cameras(host, key):
    cams = api(host, key, "GET", "/cameras") or []
    out = []
    for c in cams:
        if c.get("modelKey", "camera") != "camera" or not c.get("id"):
            continue
        ff = c.get("featureFlags") or {}
        out.append({"id": c["id"], "name": c.get("name") or "Camera", "state": c.get("state", ""),
                    "smart": ff.get("smartDetectTypes") or [], "doorbell": bool(ff.get("isDoorbell") or c.get("isDoorbell")
                                                                                 or "doorbell" in str(c.get("type", "")).lower())})
    return out


def _token(url):
    """The stream token in an RTSPS link (rtsps://host:7441/<token>?enableSrtp), which identifies the camera stream."""
    return urllib.parse.urlsplit(url or "").path.rsplit("/", 1)[-1]


def stream_tokens(host, key, cam_id):
    """Tokens of all the camera's existing RTSPS streams (any quality)."""
    got = api(host, key, "GET", f"/cameras/{cam_id}/rtsps-stream") or {}
    return {_token(u) for u in got.values() if u}


def stream_url(host, key, cam_id, quality="medium"):
    """The camera's RTSPS link at this quality, creating the stream if Protect hasn't got one."""
    got = api(host, key, "GET", f"/cameras/{cam_id}/rtsps-stream") or {}
    if not got.get(quality):
        got = api(host, key, "POST", f"/cameras/{cam_id}/rtsps-stream", {"qualities": [quality]}) or {}
    url = got.get(quality) or got.get("high") or got.get("low")
    if not url:
        raise ProtectError("Protect gave no stream link for that camera")
    return url


# --------------------------------------------------------------------------
# Events: a minimal WebSocket client and what it learns
# --------------------------------------------------------------------------
class Events:
    def __init__(self):
        self.lock = threading.Lock()
        self.items = {}          # event id -> {device, label, start, end}
        self.connected = False
        self.error = ""
        self.last_message = 0.0

    def handle(self, msg, now=None):
        now = now or time.time()
        try:
            m = json.loads(msg)
        except ValueError:
            return
        item = m.get("item") or {}
        if item.get("modelKey") != "event" or not item.get("id"):
            return
        with self.lock:
            self.last_message = now
            if m.get("type") == "remove":
                self.items.pop(item["id"], None)
                return
            cur = self.items.setdefault(item["id"], {"device": None, "label": "Motion", "start": now, "end": None, "seen": now})
            if item.get("device"):
                cur["device"] = item["device"]
            label = event_label(item)
            if label and LEVEL.get(label, 0) >= LEVEL.get(cur["label"], 0):
                cur["label"] = label
            if item.get("start"):
                cur["start"] = item["start"] / 1000
            if "end" in item:
                cur["end"] = item["end"] / 1000 if item["end"] else None
            cur["seen"] = now
            if len(self.items) > 300:   # forget the oldest
                for k in sorted(self.items, key=lambda k: self.items[k]["seen"])[:100]:
                    self.items.pop(k)

    def active(self, highlight_motion=False, now=None):
        """{camera id: {"label", "level", "since"}} for cameras with something going on now or in the last minute."""
        now = now or time.time()
        out = {}
        with self.lock:
            for e in self.items.values():
                if not e["device"]:
                    continue
                live = (e["end"] is None and now - e["start"] < OPEN_MAX_S) or (e["end"] is not None and now - e["end"] < HOLD_S)
                if not live or (e["label"] == "Motion" and not highlight_motion):
                    continue
                lvl = LEVEL.get(e["label"], 1)
                best = out.get(e["device"])
                if not best or (lvl, e["start"]) > (best["level"], best["since"]):
                    out[e["device"]] = {"label": e["label"], "level": lvl, "since": e["start"]}
        return out


def event_label(item):
    t = str(item.get("type") or "")
    if t == "ring":
        return "Doorbell"
    for s in item.get("smartDetectTypes") or []:
        if s in LABELS:
            return LABELS[s]
    if t.startswith("smartAudio"):
        return None                      # sounds (smoke alarm, glass) don't move a camera to the front
    return "Motion"                      # motion, or a smart event whose kind comes in a later update


def ws_frames(sock):
    """Yield text messages from a WebSocket (server frames are unmasked); answers pings."""
    buf = bytearray()
    def need(n):
        while len(buf) < n:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed")
            buf.extend(chunk)
    parts, opcode0 = [], None
    while True:
        need(2)
        b0, b1 = buf[0], buf[1]
        fin, op, n, off = b0 & 0x80, b0 & 0x0F, b1 & 0x7F, 2
        if n == 126:
            need(4)
            n, off = struct.unpack(">H", buf[2:4])[0], 4
        elif n == 127:
            need(10)
            n, off = struct.unpack(">Q", buf[2:10])[0], 10
        if b1 & 0x80:
            off += 4                      # masked (servers don't, but be safe)
        need(off + n)
        payload = bytes(buf[off:off + n])
        if b1 & 0x80:
            mask = buf[off - 4:off]
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        del buf[:off + n]
        if op == 9:
            ws_send(sock, payload, 0xA)
            continue
        if op == 8:
            raise ConnectionError("closed by the console")
        if op in (1, 2):
            parts, opcode0 = [payload], op
        elif op == 0:
            parts.append(payload)
        else:
            continue
        if fin:
            yield b"".join(parts).decode("utf-8", "replace")
            parts = []


def ws_send(sock, payload, opcode=1):
    mask = os.urandom(4)
    n = len(payload)
    head = bytes([0x80 | opcode]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n))
    sock.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))


def ws_connect(host, key, rel="/subscribe/events", timeout=15):
    h, _, port = host.partition(":")
    raw = socket.create_connection((h, int(port or 443)), timeout=timeout)
    sock = _ctx().wrap_socket(raw, server_hostname=h)
    k = base64.b64encode(os.urandom(16)).decode()
    sock.sendall((f"GET {BASE}{rel} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {k}\r\nSec-WebSocket-Version: 13\r\nX-API-Key: {key}\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("no answer")
        head += chunk
    status = head.split(b"\r\n", 1)[0].decode("latin-1")
    if " 101 " not in status + " ":
        sock.close()
        raise ProtectError(f"event stream refused: {status}")
    rest = head.split(b"\r\n\r\n", 1)[1]
    return sock, rest


EVENTS = Events()
_listener = {"thread": None, "key": None, "stop": None}


def listen(stack):
    """Start (or restart, after a settings change) the background event listener. Safe to call repeatedly."""
    d = settings(stack)
    want = (d["host"], d["api_key"]) if d["host"] and d["api_key"] and d["cameras"] else None
    if _listener["key"] == want and (want is None or (_listener["thread"] and _listener["thread"].is_alive())):
        return
    if _listener["stop"]:
        _listener["stop"].set()
    _listener["key"] = want
    if not want:
        EVENTS.connected = False
        return
    stop = threading.Event()
    _listener["stop"] = stop

    def run():
        delay = 5
        while not stop.is_set():
            try:
                sock, rest = ws_connect(*want)
                sock.settimeout(30)
                EVENTS.connected, EVENTS.error, delay = True, "", 5

                class Conn:
                    """The socket for ws_frames: bytes that came with the handshake answer first; when the
                    stream is quiet for 30 s, a ping; three quiet spells in a row mean the connection is gone."""
                    def __init__(self, s, pre):
                        self.s, self.pre, self.quiet = s, pre, 0

                    def recv(self, n):
                        if self.pre:
                            out, self.pre = self.pre, b""
                            return out
                        while True:
                            if stop.is_set():
                                raise ConnectionError("stopped")
                            try:
                                data = self.s.recv(n)
                                self.quiet = 0
                                return data
                            except socket.timeout:
                                self.quiet += 1
                                if self.quiet >= 3:
                                    raise ConnectionError("no answer to pings")
                                ws_send(self.s, b"", 9)

                    def sendall(self, b):
                        return self.s.sendall(b)
                try:
                    for msg in ws_frames(Conn(sock, rest)):
                        EVENTS.handle(msg)
                finally:
                    sock.close()
            except Exception as e:  # noqa: BLE001 - keep trying
                EVENTS.connected, EVENTS.error = False, str(e)[:200]
            stop.wait(delay)
            delay = min(delay * 2, 300)
    t = threading.Thread(target=run, daemon=True, name="protect-events")
    _listener["thread"] = t
    t.start()


QUALITIES = [("auto", "Automatic (low on a Pi 4, medium on a Pi 5)"), ("medium", "Medium (sharper)"),
             ("low", "Low (much lighter on the box)")]


def board():
    try:
        with open("/proc/device-tree/model") as f:
            return f.read().strip("\x00 \n")
    except OSError:
        return ""


def resolve_quality(setting, model=None):
    """The stream quality to use. A Pi 4 spends about a third of its CPU moving medium-quality video around
    (measured: whole box 41% -> 27% with low); a Pi 5 has room for medium."""
    if setting in ("medium", "low"):
        return setting
    model = board() if model is None else model
    return "low" if "Raspberry Pi" in model and "Raspberry Pi 5" not in model else "medium"


def refresh_quality(stack):
    """After an update or a quality change: re-fetch the picked cameras' links if they were made at another
    quality. Returns True when display.json changed (go2rtc then needs a restart)."""
    d = settings(stack)
    if not (d.get("host") and d.get("api_key") and d.get("cameras")):
        return False
    want = resolve_quality(d.get("quality", "auto"))
    if d.get("applied_quality") == want:
        return False
    cams = list_cameras(d["host"], d["api_key"])
    return apply_cameras(stack, [c["id"] for c in d["cameras"]], cams)


def apply_cameras(stack, picked_ids, cams, quality=None):
    """Store the picked Protect cameras (in this order) with fresh stream links in display.json.
    Manual (non-Protect) cameras stay as they are, after the Protect ones. Returns messages; raises ProtectError."""
    import display
    d = settings(stack)
    quality = quality or resolve_quality(d.get("quality", "auto"))
    by_id = {c["id"]: c for c in cams}
    out = []
    for cid in picked_ids:
        c = by_id.get(cid)
        if not c:
            continue
        out.append({"name": c["name"][:30], "url": stream_url(d["host"], d["api_key"], cid, quality), "protect_id": cid,
                    "doorbell": c.get("doorbell", False)})
    dsp = display.load(stack)
    manual = [c for c in dsp["cameras"] if not c.get("protect_id")]
    # A camera added earlier by its stream link and now picked from Protect would be on the screen twice:
    # drop the manual one (same stream token as any of the Protect camera's streams, or the same name).
    tokens, names = set(), {c["name"].strip().lower() for c in out}
    for c in out:
        try:
            tokens |= stream_tokens(d["host"], d["api_key"], c["protect_id"])
        except ProtectError:
            pass
        tokens.add(_token(c["url"]))
    dupes = [c for c in manual if _token(c["url"]) in tokens or c["name"].strip().lower() in names]
    manual = [c for c in manual if c not in dupes]
    dsp["cameras"] = (out + manual)[:display.MAX_CAMERAS]
    changed = display.save(stack, dsp)
    d["cameras"] = [{"id": c["protect_id"], "name": c["name"]} for c in out]
    d["applied_quality"] = quality
    save(stack, d)
    LAST_DUPES[:] = [c["name"] for c in dupes]
    return changed


LAST_DUPES = []   # manual cameras the last apply_cameras dropped as duplicates (for the setup page's message)
