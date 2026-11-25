"""Tests for phone notifications with a camera's picture: Protect events reaching the listeners once per label, the
alert and its cooldown (setup/cameras.py), the kept pictures, and the picture's way through notify.py.
Run: python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import cameras  # noqa: E402
import display  # noqa: E402
import notify  # noqa: E402
import protect  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 100


class Snapshots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))
        os.environ["SOLAR_COMPOSE"] = "true"
        with open(os.path.join(self.stack, "state", "site.json"), "w") as f:
            json.dump({"units": "us_customary"}, f)
        d = display.load(self.stack)
        d.update(cameras=[{"name": "Front door", "url": "rtsps://h:7441/A", "protect_id": "c1", "doorbell": True},
                          {"name": "Driveway", "url": "rtsps://h:7441/B", "protect_id": "c2"}],
                 notify={"doorbell": True, "person": True}, notify_cooldown=300)
        display.save(self.stack, d)
        cameras._last_notified.clear()

    def tearDown(self):
        os.environ.pop("SOLAR_COMPOSE", None)
        self.tmp.cleanup()

    def test_listeners_once_per_label(self):
        ev, got = protect.Events(), []
        ev.listeners.append(lambda *a: got.append(a))
        ev.listeners.append(lambda *a: 1 / 0)                              # a broken listener stops nothing
        msg = lambda **item: json.dumps({"type": "update", "item": {"modelKey": "event", "id": "e1", **item}})  # noqa: E731
        ev.handle(msg(device="c2", type="motion", start=1000_000), now=1000)
        ev.handle(msg(type="smartDetectZone", smartDetectTypes=["person"]), now=1002)
        ev.handle(msg(type="smartDetectZone", smartDetectTypes=["person"]), now=1004)   # an update: not again
        ev.handle(msg(end=1010_000), now=1010)
        self.assertEqual(got, [("c2", "Motion", 1000.0), ("c2", "Person", 1000.0)])
        ev.handle(json.dumps({"type": "add", "item": {"modelKey": "event", "id": "e2", "device": "c1", "type": "ring",
                                                      "start": 1020_000}}), now=1020)
        self.assertEqual(got[-1], ("c1", "Doorbell", 1020.0))
        # an old event replayed after a reconnect isn't news
        ev.handle(json.dumps({"type": "add", "item": {"modelKey": "event", "id": "e3", "device": "c1", "type": "ring",
                                                      "start": 1000_000}}), now=5000)
        self.assertEqual(len(got), 3)

    def test_alert_and_cooldown(self):
        at = time.mktime((2026, 11, 25, 15, 42, 0, 0, 0, -1))
        a = cameras.event_alert(self.stack, "c1", "Doorbell", at, now=at)
        self.assertEqual(a, {"tag": "camera_1", "priority": "warning", "protect_id": "c1", "title": "Doorbell: Front door",
                             "message": "Someone rang the doorbell at 3:42 PM."})
        self.assertIsNone(cameras.event_alert(self.stack, "c1", "Doorbell", at, now=at + 200))      # within 5 minutes
        self.assertIsNotNone(cameras.event_alert(self.stack, "c1", "Person", at, now=at + 200))     # another kind
        self.assertIsNotNone(cameras.event_alert(self.stack, "c1", "Doorbell", at, now=at + 301))
        p = cameras.event_alert(self.stack, "c2", "Person", at, now=at)
        self.assertEqual((p["title"], p["priority"], p["message"]), ("Person at the Driveway", "info", "A person was seen at 3:42 PM."))
        self.assertIsNone(cameras.event_alert(self.stack, "c2", "Vehicle", at, now=at))             # not a notified kind
        self.assertIsNone(cameras.event_alert(self.stack, "gone", "Person", at, now=at))            # not on the screen's list
        d = display.load(self.stack)
        d["notify"] = {"doorbell": True, "person": False}
        display.save(self.stack, d)
        self.assertIsNone(cameras.event_alert(self.stack, "c2", "Person", at, now=at + 9999))       # not chosen
        self.assertEqual(display.DEFAULTS["notify"], {"doorbell": False, "person": False})          # off until chosen

    def test_notify_event_with_and_without_picture(self):
        sent = []
        a = cameras.notify_event(self.stack, "c1", "Doorbell", time.time(), sent.append, fetch=lambda h, k, c: JPEG)
        self.assertEqual(sent, [a])
        self.assertRegex(a["image"], notify.IMAGE_RE)
        self.assertNotIn("protect_id", a)
        f = os.path.join(cameras.snap_dir(self.stack), os.path.basename(a["image"]))
        with open(f, "rb") as fh:
            self.assertEqual(fh.read(), JPEG)

        def down(h, k, c):
            raise protect.ProtectError("no")
        b = cameras.notify_event(self.stack, "c2", "Person", time.time(), sent.append, fetch=down)
        self.assertEqual((len(sent), "image" in b), (2, False))            # the text still goes
        self.assertIsNone(cameras.notify_event(self.stack, "c2", "Person", time.time(), sent.append, fetch=down))

    def test_old_pictures_removed(self):
        now = time.time()
        first = cameras.save_snapshot(self.stack, JPEG, now)
        old = os.path.join(cameras.snap_dir(self.stack), os.path.basename(first))
        os.utime(old, (now - cameras.SNAP_KEEP_S - 60,) * 2)
        second = cameras.save_snapshot(self.stack, JPEG, now)
        self.assertEqual(os.listdir(cameras.snap_dir(self.stack)), [os.path.basename(second)])

    def test_picture_through_notify(self):
        img = "/local/snap/" + "a" * 32 + ".jpg"
        a = notify.clean({"tag": "camera_1", "title": "Doorbell: Front door", "message": "x", "priority": "warning", "image": img})
        self.assertEqual(a["image"], img)
        self.assertEqual(notify.ha_data(a)["image"], img)
        for bad in ("https://evil.example/x.jpg", "/local/snap/../secrets.jpg", "/local/other/" + "a" * 32 + ".jpg", ""):
            self.assertNotIn("image", notify.clean({"tag": "t", "message": "x", "image": bad}))
        self.assertNotIn("image", notify.ha_data(notify.clean({"tag": "t", "message": "x"})))
        # delivered with the picture; one held over quiet hours keeps it
        got = []
        s = {**notify.DEFAULTS, "quiet_from": "22:00", "quiet_to": "07:00"}
        night = time.mktime((2026, 11, 25, 23, 0, 0, 0, 0, -1))
        deliver = lambda stack, s, a, ch: got.append(a) or {"ha_app": "sent"}  # noqa: E731
        self.assertEqual(notify.submit(self.stack, s, {"tag": "camera_1", "title": "T", "message": "m", "priority": "info", "image": img},
                                       now=night, deliver=deliver), "held")
        notify.due(self.stack, s, now=night + 9 * 3600, deliver=deliver)
        self.assertEqual(got[0]["image"], img)


if __name__ == "__main__":
    unittest.main()
