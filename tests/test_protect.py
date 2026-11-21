"""Tests for UniFi Protect cameras and events (protect.py, display.events).
Run: python3 -m unittest discover -s tests -v"""
import json
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import display  # noqa: E402
import protect  # noqa: E402

T = 1_795_300_000.0


def ev(kind, item_id, device, start=T, end=None, smart=None, msg_type="add"):
    item = {"id": item_id, "modelKey": "event", "type": kind, "device": device, "start": int(start * 1000)}
    if end is not None or msg_type == "update":
        item["end"] = None if end is None else int(end * 1000)
    if smart is not None:
        item["smartDetectTypes"] = smart
    return json.dumps({"type": msg_type, "item": item})


class Events(unittest.TestCase):
    def test_labels_levels_and_hold(self):
        e = protect.Events()
        e.handle(ev("smartDetectZone", "e1", "camA", smart=["person"]), now=T)
        e.handle(ev("motion", "e2", "camB"), now=T)
        e.handle(ev("ring", "e3", "camC"), now=T)
        a = e.active(now=T + 5)
        self.assertEqual(a["camA"]["label"], "Person")
        self.assertEqual(a["camC"]["label"], "Doorbell")
        self.assertEqual(a["camC"]["level"], 3)
        self.assertNotIn("camB", a)                                  # plain motion only when asked for
        self.assertEqual(e.active(highlight_motion=True, now=T + 5)["camB"]["label"], "Motion")
        # the person event ends: highlighted for another minute, then not
        e.handle(ev("smartDetectZone", "e1", "camA", end=T + 20, msg_type="update"), now=T + 20)
        self.assertIn("camA", e.active(now=T + 70))
        self.assertNotIn("camA", e.active(now=T + 81))
        # an event that never ends stops counting after 5 minutes
        self.assertNotIn("camC", e.active(now=T + 301))

    def test_update_brings_the_kind_and_remove(self):
        e = protect.Events()
        e.handle(ev("smartDetectZone", "e1", "camA"), now=T)                       # kind not known yet
        self.assertNotIn("camA", e.active(now=T + 1))
        e.handle(ev("smartDetectZone", "e1", "camA", smart=["vehicle"], msg_type="update"), now=T + 1)
        self.assertEqual(e.active(now=T + 2)["camA"]["label"], "Vehicle")
        e.handle(json.dumps({"type": "remove", "item": {"id": "e1", "modelKey": "event"}}), now=T + 3)
        self.assertEqual(e.active(now=T + 3), {})

    def test_ignores_other_models_and_sounds(self):
        e = protect.Events()
        e.handle(json.dumps({"type": "update", "item": {"id": "x", "modelKey": "camera", "state": "CONNECTED"}}), now=T)
        e.handle(ev("smartAudioDetect", "s1", "camA", smart=["alrmSmoke"]), now=T)
        e.handle("not json", now=T)
        self.assertEqual(e.active(highlight_motion=False, now=T + 1), {})


def frame(payload, op=1, fin=True):
    n = len(payload)
    head = bytes([(0x80 if fin else 0) | op]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n))
    return head + payload


class Sock:
    def __init__(self, data):
        self.data, self.sent = bytearray(data), []

    def recv(self, n):
        if not self.data:
            return b""
        out = bytes(self.data[:7])          # dribble the bytes in, to test reassembly
        del self.data[:7]
        return out

    def sendall(self, b):
        self.sent.append(b)


class WebSocket(unittest.TestCase):
    def test_frames(self):
        big = json.dumps({"type": "add", "item": {"id": "e", "pad": "x" * 300}}).encode()
        data = frame(b'{"a":1}') + frame(b"ping!", op=9) + frame(b'{"b":', fin=False) + frame(b"2}", op=0) + frame(big) + frame(b"", op=8)
        s = Sock(data)
        got = []
        with self.assertRaises(ConnectionError):
            for m in protect.ws_frames(s):
                got.append(m)
        self.assertEqual(got, ['{"a":1}', '{"b":2}', big.decode()])
        self.assertEqual(s.sent[0][0], 0x8A)                         # answered the ping with a pong
        self.assertTrue(s.sent[0][1] & 0x80)                         # client frames are masked


class Display(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_cameras_keeps_manual_ones_and_maps_events(self):
        display.save(self.stack, {**display.DEFAULTS, "cameras": [{"name": "Shed", "url": "rtsp://10.0.0.9/live"}]})
        protect.save(self.stack, {**protect.DEFAULTS, "host": "192.0.2.1", "api_key": "k"})
        orig = protect.stream_url
        protect.stream_url = lambda host, key, cid, quality="medium": f"rtsps://192.0.2.1:7441/{cid}token?enableSrtp"
        try:
            cams = [{"id": "aaa", "name": "Driveway", "doorbell": False}, {"id": "bbb", "name": "Front door", "doorbell": True},
                    {"id": "ccc", "name": "Back", "doorbell": False}]
            self.assertTrue(protect.apply_cameras(self.stack, ["bbb", "aaa"], cams))
        finally:
            protect.stream_url = orig
        d = display.load(self.stack)
        self.assertEqual([c["name"] for c in d["cameras"]], ["Front door", "Driveway", "Shed"])  # as picked, then manual
        self.assertEqual(d["cameras"][0]["protect_id"], "bbb")
        y = open(os.path.join(self.stack, "go2rtc", "go2rtc.yaml")).read()
        self.assertIn('cam2: "rtspx://192.0.2.1:7441/aaatoken"', y)                          # SRTP flag dropped, as for manual links
        self.assertIn("cam3:", y)
        # events for the front door show up on cam1
        protect.EVENTS.items.clear()
        protect.EVENTS.handle(ev("ring", "r1", "bbb", start=__import__("time").time()))
        orig_listen = protect.listen
        protect.listen = lambda stack: None
        try:
            got = display.events(self.stack)
        finally:
            protect.listen = orig_listen
            protect.EVENTS.items.clear()
        self.assertEqual(list(got["active"]), ["cam1"])
        self.assertEqual(got["active"]["cam1"]["label"], "Doorbell")
        self.assertEqual(os.stat(protect.path(self.stack)).st_mode & 0o777, 0o600)

    def test_page_rotates_and_highlights(self):
        page = display.PAGE
        for needle in ("/display/events", "ROTATE_S=15", ".cam.hot", ".cam.ring", "ondisconnect()"):
            self.assertIn(needle, page)
        self.assertEqual(display.MAX_CAMERAS, 16)

    def test_clean_host(self):
        self.assertEqual(protect.clean_host("https://192.168.1.1/protect/dashboard"), "192.168.1.1")
        with self.assertRaises(ValueError):
            protect.clean_host("   ")


if __name__ == "__main__":
    unittest.main()
