#!/usr/bin/env python3
"""Simulated UniFi Protect console (Integration API), for testing the wall display's Protect cameras and events.

HTTPS (self-signed, like a real console) on --port:
  GET  /proxy/protect/integration/v1/cameras                     the cameras
  GET  /proxy/protect/integration/v1/cameras/{id}/rtsps-stream   their stream links (POST creates "medium")
  GET  /proxy/protect/integration/v1/subscribe/events            WebSocket with event messages
All need the header X-API-Key: <--key>.

Control page (plain HTTP) on --control-port: fire an event on a camera, e.g. /fire?cam=cam2&kind=person
(kinds: person, vehicle, animal, package, motion, ring); it starts now and ends after &seconds=20.
The stream links point at --rtsp-host, so they only play if something is serving RTSPS there.

  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj /CN=protect -keyout key.pem -out cert.pem
  python3 protectsim.py --cert cert.pem --certkey key.pem --key test-api-key --cameras "Driveway,Front door:doorbell,Back yard,Shed"
"""
import argparse
import base64
import hashlib
import json
import os
import ssl
import struct
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = "/proxy/protect/integration/v1"
clients, lock = [], threading.Lock()


def ws_frame(text):
    b = text.encode()
    n = len(b)
    head = bytes([0x81]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536 else bytes([127]) + struct.pack(">Q", n))
    return head + b


def broadcast(msg):
    data = ws_frame(json.dumps(msg))
    with lock:
        for c in list(clients):
            try:
                c.sendall(data)
            except OSError:
                clients.remove(c)


def main():
    e = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(e("PORT", "443")))
    ap.add_argument("--control-port", type=int, default=int(e("CONTROL_PORT", "8080")))
    ap.add_argument("--cert", default=e("CERT", "cert.pem"))
    ap.add_argument("--certkey", default=e("CERTKEY", "key.pem"))
    ap.add_argument("--key", default=e("API_KEY", "test-api-key"))
    ap.add_argument("--rtsp-host", default=e("RTSP_HOST", "127.0.0.1"))
    ap.add_argument("--cameras", default=e("CAMERAS", "Driveway,Front door:doorbell,Back yard,Shed"))
    a = ap.parse_args()
    cams = []
    for i, spec in enumerate(a.cameras.split(","), 1):
        name, _, kind = spec.partition(":")
        cams.append({"id": f"6620{i:020x}", "modelKey": "camera", "state": "CONNECTED", "name": name.strip(),
                     "featureFlags": {"smartDetectTypes": ["person", "vehicle", "animal", "package"], "hasMic": True,
                                      "isDoorbell": kind == "doorbell"}})
    streams = {c["id"]: {"high": f"rtsps://{a.rtsp_host}:7441/{c['id'][-8:]}H?enableSrtp", "medium": None, "low": None, "package": None}
               for c in cams}

    class API(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def reply(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authed(self):
            if self.headers.get("X-API-Key") != a.key:
                self.reply(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):
            if not self.authed():
                return
            p = urllib.parse.urlsplit(self.path).path
            if p == BASE + "/cameras":
                return self.reply(200, cams)
            if p.startswith(BASE + "/cameras/") and p.endswith("/rtsps-stream"):
                cid = p.split("/")[-2]
                return self.reply(200, streams[cid]) if cid in streams else self.reply(404, {})
            if p == BASE + "/subscribe/events" and self.headers.get("Upgrade", "").lower() == "websocket":
                accept = base64.b64encode(hashlib.sha1((self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                self.wfile.flush()
                conn = self.connection
                with lock:
                    clients.append(conn)
                conn.settimeout(None)
                try:
                    while True:                     # read (and answer pings) until the client goes
                        h = conn.recv(2)
                        if len(h) < 2:
                            break
                        n = h[1] & 0x7F
                        ext = conn.recv(2 if n == 126 else 8) if n >= 126 else b""
                        n = struct.unpack(">H", ext)[0] if n == 126 else struct.unpack(">Q", ext)[0] if n == 127 else n
                        mask = conn.recv(4) if h[1] & 0x80 else b""
                        payload = conn.recv(n) if n else b""
                        if h[0] & 0x0F == 9:
                            conn.sendall(bytes([0x8A, 0]))
                        if h[0] & 0x0F == 8:
                            break
                except OSError:
                    pass
                with lock:
                    if conn in clients:
                        clients.remove(conn)
                self.close_connection = True
                return
            self.reply(404, {})

        def do_POST(self):
            if not self.authed():
                return
            p = urllib.parse.urlsplit(self.path).path
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            cid = p.split("/")[-2]
            if cid not in streams:
                return self.reply(404, {})
            for q in body.get("qualities", []):
                streams[cid][q] = f"rtsps://{a.rtsp_host}:7441/{cid[-8:]}{q[0].upper()}?enableSrtp"
            self.reply(200, {q: streams[cid][q] for q in body.get("qualities", [])})

        def log_message(self, *x):
            pass

    class Control(BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            msg = ""
            if "cam" in q:
                idx = int(q["cam"].removeprefix("cam")) - 1
                cam = cams[idx]
                kind, secs = q.get("kind", "person"), float(q.get("seconds", "20"))
                now = int(time.time() * 1000)
                eid = f"ev{now}"
                item = {"id": eid, "modelKey": "event", "device": cam["id"], "start": now, "end": None}
                if kind == "ring":
                    item["type"] = "ring"
                elif kind == "motion":
                    item["type"] = "motion"
                else:
                    item.update(type="smartDetectZone", smartDetectTypes=[kind])
                broadcast({"type": "add", "item": item})
                threading.Timer(secs, broadcast, args=({"type": "update", "item": {"id": eid, "modelKey": "event", "end": now + int(secs * 1000)}},)).start()
                msg = f"fired {kind} on {cam['name']} for {secs:g} s ({len(clients)} listener(s))"
            links = "".join(f"<li>{c['name']}: " + " ".join(f"<a href='/?cam=cam{i}&kind={k}'>{k}</a>" for k in ("person", "vehicle", "animal", "package", "motion", "ring")) + "</li>"
                            for i, c in enumerate(cams, 1))
            data = f"<!doctype html><title>protectsim</title><body style='font:16px system-ui;padding:12px'><h2>Simulated UniFi Protect</h2><p>{msg}</p><ul>{links}</ul>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *x):
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", a.port), API)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(a.cert, a.certkey)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", a.control_port), Control).serve_forever, daemon=True).start()
    print(f"protectsim: {len(cams)} cameras, API on https://:{a.port}, control on :{a.control_port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
