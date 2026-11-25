"""Tests for UniFi Protect cameras and events (protect.py, display.events).
Run: python3 -m unittest discover -s tests -v"""
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import cameras  # noqa: E402
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
        orig, orig_t = protect.stream_url, protect.stream_tokens
        protect.stream_url = lambda host, key, cid, quality="medium": f"rtsps://192.0.2.1:7441/{cid}token?enableSrtp"
        protect.stream_tokens = lambda host, key, cid: {f"{cid}token"}
        try:
            cams = [{"id": "aaa", "name": "Driveway", "doorbell": False}, {"id": "bbb", "name": "Front door", "doorbell": True},
                    {"id": "ccc", "name": "Back", "doorbell": False}]
            self.assertTrue(protect.apply_cameras(self.stack, ["bbb", "aaa"], cams))
        finally:
            protect.stream_url, protect.stream_tokens = orig, orig_t
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

    def test_apply_cameras_drops_manual_duplicates(self):
        display.save(self.stack, {**display.DEFAULTS, "cameras": [
            {"name": "Driveway", "url": "rtsps://192.0.2.1:7441/abcTOKEN?enableSrtp"},       # same camera, by name
            {"name": "Old porch", "url": "rtsps://192.0.2.1:7441/bbbHIGH?enableSrtp"},       # same camera, by token
            {"name": "Shed", "url": "rtsp://10.0.0.9/live"}]})
        protect.save(self.stack, {**protect.DEFAULTS, "host": "192.0.2.1", "api_key": "k"})
        o1, o2 = protect.stream_url, protect.stream_tokens
        protect.stream_url = lambda host, key, cid, quality="medium": f"rtsps://192.0.2.1:7441/{cid}MED?enableSrtp"
        protect.stream_tokens = lambda host, key, cid: {f"{cid}MED", f"{cid}HIGH"}
        try:
            protect.apply_cameras(self.stack, ["aaa", "bbb"], [{"id": "aaa", "name": "Driveway"}, {"id": "bbb", "name": "Porch"}])
        finally:
            protect.stream_url, protect.stream_tokens = o1, o2
        self.assertEqual([c["name"] for c in display.load(self.stack)["cameras"]], ["Driveway", "Porch", "Shed"])
        self.assertEqual(sorted(protect.LAST_DUPES), ["Driveway", "Old porch"])

    def test_page_never_shows_a_camera_twice(self):
        self.assertIn("a.name.trim().toLowerCase()===b.name.trim().toLowerCase()", display.PAGE)

    def test_page_rotates_and_highlights(self):
        page = display.PAGE
        for needle in ("/display/events", "ROTATE_S=15", ".cam.hot", ".cam.ring", "ondisconnect()"):
            self.assertIn(needle, page)
        self.assertEqual(display.MAX_CAMERAS, 16)

    def test_quality(self):
        r = protect.resolve_quality
        self.assertEqual(r("auto", "Raspberry Pi 4 Model B Rev 1.5"), "low")
        self.assertEqual(r("auto", "Raspberry Pi 5 Model B Rev 1.1"), "medium")
        self.assertEqual(r("auto", ""), "medium")                      # not a Pi (tests, a PC)
        self.assertEqual(r("medium", "Raspberry Pi 4 Model B"), "medium")
        # after an update, cameras made at medium are re-fetched at the quality the setting asks for
        display.save(self.stack, {**display.DEFAULTS, "cameras": []})
        protect.save(self.stack, {**protect.DEFAULTS, "host": "192.0.2.1", "api_key": "k", "quality": "low",
                                  "cameras": [{"id": "aaa", "name": "Driveway"}], "applied_quality": "medium"})
        asked = []
        o = (protect.stream_url, protect.stream_tokens, protect.list_cameras)
        protect.stream_url = lambda host, key, cid, quality="medium": asked.append(quality) or f"rtsps://192.0.2.1:7441/{cid}{quality}?enableSrtp"
        protect.stream_tokens = lambda host, key, cid: {f"{cid}low"}     # what Protect hands out now
        protect.list_cameras = lambda host, key: [{"id": "aaa", "name": "Driveway"}]
        try:
            self.assertTrue(protect.refresh_quality(self.stack))
            self.assertFalse(protect.refresh_quality(self.stack))      # done: nothing to do the next time
        finally:
            protect.stream_url, protect.stream_tokens, protect.list_cameras = o
        self.assertEqual(asked, ["low"])
        self.assertEqual(display.load(self.stack)["cameras"][0]["url"], "rtsps://192.0.2.1:7441/aaalow?enableSrtp")

    def test_stale_links_are_refreshed(self):
        display.save(self.stack, {**display.DEFAULTS, "cameras": []})
        protect.save(self.stack, {**protect.DEFAULTS, "host": "192.0.2.1", "api_key": "k", "quality": "medium",
                                  "cameras": [{"id": "aaa", "name": "Front Yard"}], "applied_quality": "medium"})
        o = (protect.stream_url, protect.stream_tokens, protect.list_cameras)
        issued = {"aaa": "OLD"}
        protect.stream_url = lambda host, key, cid, quality="medium": f"rtsps://192.0.2.1:7441/{issued[cid]}?enableSrtp"
        protect.stream_tokens = lambda host, key, cid: {issued[cid]}
        protect.list_cameras = lambda host, key: [{"id": "aaa", "name": "Front Yard"}]
        try:
            protect.apply_cameras(self.stack, ["aaa"], protect.list_cameras("", ""))
            self.assertEqual(protect.stale_links(self.stack), [])
            self.assertFalse(protect.refresh_quality(self.stack))
            issued["aaa"] = "NEW"                                       # Protect hands out another link
            self.assertEqual(protect.stale_links(self.stack), ["Front Yard"])
            self.assertTrue(protect.refresh_quality(self.stack))
        finally:
            protect.stream_url, protect.stream_tokens, protect.list_cameras = o
        self.assertIn("/NEW?", display.load(self.stack)["cameras"][0]["url"])

    def test_renamed_in_protect(self):
        """A camera renamed in UniFi Protect gets the new name on the screen and in the announcements (display.json),
        keeping its stream link; manual cameras are left alone."""
        display.save(self.stack, {**display.DEFAULTS, "cameras": [{"name": "Shed", "url": "rtsp://10.0.0.9/live"}]})
        protect.save(self.stack, {**protect.DEFAULTS, "host": "192.0.2.1", "api_key": "k"})
        o = (protect.stream_url, protect.stream_tokens)
        protect.stream_url = lambda host, key, cid, quality="medium": f"rtsps://192.0.2.1:7441/T{cid}?enableSrtp"
        protect.stream_tokens = lambda host, key, cid: {f"T{cid}"}
        try:
            protect.apply_cameras(self.stack, ["aaa", "bbb"], [{"id": "aaa", "name": "Front Yard"}, {"id": "bbb", "name": "Gate"}])
        finally:
            protect.stream_url, protect.stream_tokens = o
        self.assertEqual(protect.sync_names(self.stack, [{"id": "aaa", "name": "Front Yard"}, {"id": "bbb", "name": "Gate"}]), {})
        renamed = protect.sync_names(self.stack, [{"id": "aaa", "name": "Driveway"}, {"id": "bbb", "name": "Gate"},
                                                  {"id": "ccc", "name": "Not picked"}])
        self.assertEqual(renamed, {"Front Yard": "Driveway"})
        cams = display.load(self.stack)["cameras"]
        self.assertEqual([c["name"] for c in cams], ["Driveway", "Gate", "Shed"])
        self.assertEqual(cams[0]["url"], "rtsps://192.0.2.1:7441/Taaa?enableSrtp")     # same link: no go2rtc restart
        self.assertEqual(protect.settings(self.stack)["cameras"], [{"id": "aaa", "name": "Driveway"}, {"id": "bbb", "name": "Gate"}])
        # a camera missing from Protect's list (offline, or deleted) keeps its name
        self.assertEqual(protect.sync_names(self.stack, [{"id": "bbb", "name": "Gate"}]), {})
        self.assertEqual(display.load(self.stack)["cameras"][0]["name"], "Driveway")
        # the collector's 5-minute job fetches the list itself, and logs the rename
        o = protect.list_cameras
        protect.list_cameras = lambda host, key: [{"id": "aaa", "name": "Driveway"}, {"id": "bbb", "name": "Back gate"}]
        try:
            self.assertEqual(cameras.sync_names(self.stack)[0], "camera renamed in Protect: Gate -> Back gate")
            self.assertEqual(cameras.sync_names(self.stack), [])
        finally:
            protect.list_cameras = o

    def test_added_to_home_assistant(self):
        """Protect goes into Home Assistant with the box's API key (API-key-only mode), once: an integration the
        owner added, or one they deleted, is left alone; a new key on the box is put in the box's entry."""
        store = os.path.join(self.stack, "homeassistant", ".storage", "core.config_entries")
        calls = []
        run = lambda cmd: (calls.append(cmd[-2:]), subprocess.CompletedProcess(cmd, 0, "", ""))[1]  # noqa: E731
        os.environ["SOLAR_COMPOSE"] = "fake-compose"
        try:
            protect.save(self.stack, {**protect.DEFAULTS, "host": "192.0.2.1", "api_key": "k1"})
            self.assertEqual(protect.ensure_in_ha(self.stack, run), [])          # Home Assistant not set up yet
            os.makedirs(os.path.dirname(store))
            with open(store, "w") as f:
                json.dump({"version": 1, "minor_version": 5, "key": "core.config_entries",
                           "data": {"entries": [{"domain": "mqtt", "entry_id": "m", "data": {}}]}}, f)
            self.assertEqual(protect.ensure_in_ha(self.stack, run), ["cameras added to Home Assistant (UniFi Protect integration)"])
            self.assertEqual(calls, [["stop", "homeassistant"], ["-d", "homeassistant"]])
            e = protect.ha_entries(self.stack)[0]
            self.assertEqual(e["data"], {"host": "192.0.2.1", "port": 443, "verify_ssl": False, "api_key": "k1",
                                         "connection_mode": "api_key_only", "id": "UniFi Protect"})
            self.assertEqual((e["version"], e["unique_id"]), (2, None))
            self.assertEqual(oct(os.stat(store).st_mode & 0o777), "0o600")
            calls.clear()
            self.assertEqual(protect.ensure_in_ha(self.stack, run), [])          # there: nothing to do
            self.assertEqual(calls, [])
            protect.save(self.stack, {**protect.settings(self.stack), "api_key": "k2"})
            self.assertEqual(protect.ensure_in_ha(self.stack, run), ["Home Assistant's UniFi Protect integration has the new API key"])
            self.assertEqual([x["data"]["api_key"] for x in protect.ha_entries(self.stack)], ["k2"])
            # deleted by the owner in Home Assistant: stays deleted
            with open(store) as f:
                doc = json.load(f)
            doc["data"]["entries"] = [x for x in doc["data"]["entries"] if x["domain"] != "unifiprotect"]
            with open(store, "w") as f:
                json.dump(doc, f)
            calls.clear()
            self.assertEqual(protect.ensure_in_ha(self.stack, run), [])
            self.assertEqual(calls, [])
            # the owner's own integration (a Protect login) on a box that never added one: left alone
            protect.save(self.stack, {**protect.DEFAULTS, "host": "192.0.2.1", "api_key": "k2"})
            doc["data"]["entries"].append({"domain": "unifiprotect", "data": {"host": "192.0.2.1", "username": "u"}})
            with open(store, "w") as f:
                json.dump(doc, f)
            self.assertEqual(protect.ha_action(self.stack), "")
        finally:
            os.environ.pop("SOLAR_COMPOSE", None)

    def test_h265_camera_explained(self):
        display.save(self.stack, {**display.DEFAULTS, "cameras": [{"name": "Driveway", "url": "rtsp://a/1"}, {"name": "Front Yard", "url": "rtsp://a/2"}]})
        with open(os.path.join(self.stack, "state", "camera-check.json"), "w") as f:
            json.dump({"cam1": {"name": "Driveway", "codec": "H264"}, "cam2": {"name": "Front Yard", "codec": "H265"}}, f)
        probs = cameras.camera_problems(self.stack)
        self.assertEqual(list(probs), ["cam2"])
        self.assertIn("Enhanced Encoding", probs["cam2"])
        self.assertIn("if(c.problem&&/H\\.265/.test(c.problem))", display.PAGE)

    def test_page_stops_video_when_dark(self):
        self.assertIn("if(screenDark){ dropPlayer(s); s.cam=null; continue; }", display.PAGE)

    def test_clean_host(self):
        self.assertEqual(protect.clean_host("https://192.168.1.1/protect/dashboard"), "192.168.1.1")
        with self.assertRaises(ValueError):
            protect.clean_host("   ")


if __name__ == "__main__":
    unittest.main()
