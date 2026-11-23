"""Tests for notifications (setup/notify.py and setup/modules/notifications): phones, quiet hours, routing by priority,
holding, repeats, clearing, ntfy delivery, the Home Assistant hand-off and the setup form.
Run: python3 -m unittest discover -s tests -v"""
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
os.environ.setdefault("SOLAR_COMPOSE", "true")
os.environ["TZ"] = "UTC"
time.tzset()
import modules  # noqa: E402
import notify  # noqa: E402
import solar_setup as ss  # noqa: E402
from modules import FieldError, notifications  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
def wj(path, d):
    with open(path, "w") as f:
        json.dump(d, f)


NOON = 1795435200            # 2026-11-23 12:00 UTC
NIGHT = NOON + 11 * 3600     # 23:00 UTC


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))
        os.makedirs(os.path.join(self.stack, "homeassistant", ".storage"))
        self.sent = []

    def tearDown(self):
        modules._ALL = None
        self.tmp.cleanup()

    def phones(self, *names, registry=True):
        st = os.path.join(self.stack, "homeassistant", ".storage")
        entries = [{"entry_id": f"e{i}", "domain": "mobile_app", "title": n} for i, n in enumerate(names)]
        wj(os.path.join(st, "core.config_entries"),
           {"data": {"entries": [{"entry_id": "m", "domain": "mqtt", "title": "broker"}] + entries}})
        if registry:
            ents = [{"entity_id": f"device_tracker.{n.lower().replace(' ', '_')}", "platform": "mobile_app",
                     "config_entry_id": f"e{i}"} for i, n in enumerate(names)]
            wj(os.path.join(st, "core.entity_registry"), {"data": {"entities": ents + [{"entity_id": "sensor.x", "platform": "mqtt"}]}})

    def deliver(self, stack, s, a, channels):
        self.sent.append((a["tag"], a["message"], list(channels)))
        return {c: "sent" for c in channels}

    def s(self, **kw):
        return {**notify.DEFAULTS, **kw}


class Deciding(Base):
    def test_phones_from_the_registry_or_the_entries(self):
        self.phones("Pixel 8", "iPad")
        self.assertEqual(notify.phones(self.stack), [{"id": "ipad", "name": "iPad", "registered": True},
                                                     {"id": "pixel_8", "name": "Pixel 8", "registered": True}])
        self.assertEqual(notify.chosen_phones(self.stack, self.s()), ["ipad", "pixel_8"])
        self.assertEqual(notify.chosen_phones(self.stack, self.s(phones=["pixel_8", "gone"])), ["pixel_8"])
        self.phones("Jane's iPhone", registry=False)
        os.remove(os.path.join(self.stack, "homeassistant", ".storage", "core.entity_registry"))
        self.assertEqual(notify.phones(self.stack), [{"id": "jane_s_iphone", "name": "Jane's iPhone"}])
        self.assertEqual(notify.chosen_phones(self.stack, self.s()), [])          # shown, but no known notify action yet

    def test_quiet_hours(self):
        s = self.s()                                     # 22:00 to 07:00, over midnight
        self.assertFalse(notify.in_quiet(s, NOON))
        self.assertTrue(notify.in_quiet(s, NIGHT))
        self.assertTrue(notify.in_quiet(s, NOON - 6 * 3600))          # 06:00
        self.assertFalse(notify.in_quiet(s, NOON - 5 * 3600))         # 07:00
        self.assertTrue(notify.in_quiet(self.s(quiet_from="12:00", quiet_to="13:00"), NOON))
        self.assertFalse(notify.in_quiet(self.s(quiet_from="", quiet_to=""), NIGHT))
        self.assertFalse(notify.in_quiet(self.s(quiet_from="22:00", quiet_to="22:00"), NIGHT))

    def test_priorities_and_channels(self):
        self.phones("Pixel 8")
        s = self.s(ntfy=True, ntfy_topic="t")
        self.assertEqual(notify.priority_of({"tag": "generator"}), "critical")
        self.assertEqual(notify.priority_of({"tag": "summary"}), "info")
        self.assertEqual(notify.priority_of({"tag": "boiler", "priority": "critical"}), "critical")
        self.assertEqual(notify.priority_of({"tag": "something_new"}), "warning")
        self.assertEqual(notify.channels_for(self.stack, s, "critical", True), ["ha_app", "ntfy", "wall"])
        self.assertEqual(notify.channels_for(self.stack, s, "critical", False), ["ha_app", "ntfy"])
        self.assertEqual(notify.channels_for(self.stack, s, "warning", True), ["ha_app", "ntfy"])
        self.assertEqual(notify.channels_for(self.stack, s, "info", True), ["ha_app"])
        self.assertEqual(notify.channels_for(self.stack, self.s(ha_app=False, ntfy=True, ntfy_topic="t"), "info", True), ["ntfy"])
        self.assertEqual(notify.channels_for(self.stack, self.s(phones=[]), "warning", True), [])

    def test_send_hold_and_release(self):
        self.phones("Pixel 8")
        s = self.s()
        self.assertEqual(notify.submit(self.stack, s, {"tag": "battery_low", "title": "Battery low", "message": "18%"},
                                       now=NOON, deliver=self.deliver), "sent")
        self.assertEqual(self.sent, [("battery_low", "18%", ["ha_app"])])
        # the same again within 30 minutes: not sent; after: sent
        self.assertEqual(notify.submit(self.stack, s, {"tag": "battery_low", "message": "18%"}, now=NOON + 60, deliver=self.deliver),
                         "repeat, not sent again")
        notify.submit(self.stack, s, {"tag": "battery_low", "message": "18%"}, now=NOON + 1900, deliver=self.deliver)
        self.assertEqual(len(self.sent), 2)
        # at night: warnings wait, critical goes
        self.sent.clear()
        self.assertEqual(notify.submit(self.stack, s, {"tag": "no_data", "message": "x"}, now=NIGHT, deliver=self.deliver), "held")
        notify.submit(self.stack, s, {"tag": "summary", "message": "day"}, now=NIGHT, deliver=self.deliver)
        notify.submit(self.stack, s, {"tag": "generator", "message": "start it"}, now=NIGHT, deliver=self.deliver)
        self.assertEqual(self.sent, [("generator", "start it", ["ha_app"])])
        self.assertEqual([h["tag"] for h in notify.held(self.stack)], ["no_data", "summary"])
        self.sent.clear()
        self.assertEqual(notify.due(self.stack, s, now=NIGHT + 3600, deliver=self.deliver), [])     # still quiet
        msgs = notify.due(self.stack, s, now=NIGHT + 9 * 3600, deliver=self.deliver)                # 08:00
        self.assertEqual([x[0] for x in self.sent], ["no_data", "summary", "generator"])            # + the 4-hour repeat
        self.assertIn("generator: repeat, not cleared yet", msgs)
        self.assertEqual(notify.held(self.stack), [])
        log = notify.log(self.stack)
        self.assertEqual(log[0]["result"]["why"], "repeat, not cleared yet")
        self.assertIn("held for the end of quiet hours", log[-3]["result"]["-"])

    def test_clear(self):
        self.phones("Pixel 8")
        s = self.s()
        notify.submit(self.stack, s, {"tag": "generator", "message": "start it"}, now=NOON, deliver=self.deliver)
        notify.submit(self.stack, s, {"tag": "backup", "message": "none"}, now=NIGHT, deliver=self.deliver)
        self.sent.clear()
        self.assertEqual(notify.submit(self.stack, s, {"tag": "generator", "message": "clear"}, now=NOON + 60,
                                       deliver=self.deliver), "cleared")
        notify.submit(self.stack, s, {"tag": "backup", "message": "clear"}, now=NIGHT + 60, deliver=self.deliver)
        self.assertEqual(self.sent, [("generator", "clear_notification", ["ha_app"]), ("backup", "clear_notification", ["ha_app"])])
        self.assertEqual((notify.active(self.stack), notify.held(self.stack)), ({}, []))
        # cleared: the same alert may come again at once
        self.sent.clear()
        notify.submit(self.stack, s, {"tag": "generator", "message": "start it"}, now=NOON + 120, deliver=self.deliver)
        self.assertEqual(len(self.sent), 1)

    def test_test_alert_goes_everywhere_at_night(self):
        self.phones("Pixel 8")
        notify.submit(self.stack, self.s(ntfy=True, ntfy_topic="t"), {"tag": "test", "message": "hi"}, now=NIGHT,
                      voice_on=True, deliver=self.deliver)
        self.assertEqual(self.sent, [("test", "hi", ["ha_app", "ntfy", "wall"])])


class Delivering(Base):
    def serve(self):
        got = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                got.append((self.path, dict(self.headers), body))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_port}", got

    def test_ntfy(self):
        url, got = self.serve()
        s = self.s(ntfy=True, ntfy_server=url + "/", ntfy_topic="solar-abc", ntfy_token="tk_1")
        r = notify.send(self.stack, s, {"tag": "generator", "title": "Start the generator", "message": "Battery 22%",
                                        "priority": "critical"}, ["ntfy"])
        self.assertEqual(r, {"ntfy": "sent"})
        path, hdr, body = got[0]
        self.assertEqual(path, "/")
        self.assertEqual(hdr["Authorization"], "Bearer tk_1")
        self.assertEqual(body, {"topic": "solar-abc", "title": "Start the generator", "message": "Battery 22%",
                                "priority": 5, "tags": ["generator"]})
        r = notify.send(self.stack, self.s(ntfy_server="http://127.0.0.1:9", ntfy_topic="t"),
                        {"tag": "x", "title": "", "message": "m", "priority": "info"}, ["ntfy"])
        self.assertTrue(r["ntfy"].startswith("failed:"))

    def test_ha_app_options(self):
        self.assertEqual(notify.ha_data({"tag": "summary", "priority": "info"}), {"tag": "solar_summary", "group": "solar"})
        self.assertEqual(notify.ha_data({"tag": "generator", "priority": "critical"}),
                         {"tag": "solar_generator", "group": "solar", "ttl": 0, "priority": "high",
                          "push": {"interruption-level": "time-sensitive"}})


class HomeAssistantHandOff(Base):
    def test_site_package(self):
        pkg = ss.site_package({}, "solar-test-id", ss.notify_package(self.stack))
        self.assertEqual(pkg.count("\nautomation:\n"), 1)
        self.assertIn("rest_command:\n  solar_notify_box:\n    url: \"http://127.0.0.1:80/notify/send\"", pkg)
        self.assertIn("token=" + notify.box_token(self.stack) + "&tag={{ tag | urlencode }}", pkg)
        self.assertIn(f"webhook_id: \"{notify.ha_webhook(self.stack)}\"", pkg)
        self.assertIn("- id: solar_test_notification", pkg)
        self.assertIn("{{ trigger.json.phones | select('in', integration_entities('mobile_app')", pkg)
        self.assertEqual(notify.box_token(self.stack), notify.box_token(self.stack))         # kept, not remade
        try:
            import yaml
        except ImportError:
            return
        d = yaml.safe_load(pkg)
        self.assertEqual([a["id"] for a in d["automation"]], ["solar_notify_deliver", "solar_test_notification"])

    def test_script_falls_back_to_every_phone(self):
        with open(os.path.join(ROOT, "stack", "homeassistant-seed", "packages", "solar_monitor.yaml")) as f:
            pkg = f.read()
        script = pkg[pkg.index("  solar_notify:"):pkg.index("automation:")]
        self.assertIn("action: rest_command.solar_notify_box", script)
        self.assertIn("(box.content | string) != 'ok'", script)
        self.assertIn("integration_entities('mobile_app')", script)

    def test_receive(self):
        got = []
        old = notify.submit
        notify.submit = lambda stack, s, a, voice_on=False: got.append(a)
        try:
            r = notifications.receive(self.stack, {"token": "wrong", "tag": "x"})
            self.assertEqual(r.code, 403)
            r = notifications.receive(self.stack, {"token": notify.box_token(self.stack), "tag": "battery_low",
                                                   "title": "Battery low", "message": "18%", "priority": ""})
            self.assertEqual((r.code, r.body), (200, "ok"))
            for _ in range(50):
                if got:
                    break
                time.sleep(0.02)
            self.assertEqual(got, [{"tag": "battery_low", "title": "Battery low", "message": "18%", "priority": ""}])
        finally:
            notify.submit = old


class Settings(Base):
    def test_new_box_has_quiet_hours_a_set_up_box_keeps_none(self):
        s = modules.settings(self.stack, "notifications")
        self.assertEqual((s["quiet_from"], s["quiet_to"], s["ha_app"], s["phones"]), ("22:00", "07:00", True, "all"))
        self.assertTrue(modules.load(self.stack, "notifications")["enabled"])
        with tempfile.TemporaryDirectory() as other:
            os.makedirs(os.path.join(other, "state"))
            wj(os.path.join(other, "state", "site.json"), {"battery_count": 6})
            self.assertEqual(modules.settings(other, "notifications")["quiet_from"], "")

    def test_form(self):
        self.phones("Pixel 8", "iPad")
        with self.assertRaises(FieldError):
            notifications.form_check(self.stack, {"nt_ntfy": "1", "nt_server": "ftp://x"})
        with self.assertRaises(FieldError):
            notifications.form_check(self.stack, {"nt_ntfy": "1", "nt_server": "https://ntfy.sh", "nt_topic": "a b"})
        with self.assertRaises(FieldError):
            notifications.form_check(self.stack, {"nt_quiet_from": "25:00"})
        notifications.form_check(self.stack, {"nt_ntfy": "1", "nt_server": "https://ntfy.example.com:8443/", "nt_topic": ""})
        notifications.form_save(self.stack, {"nt_ha_app": "1", "nt_phone_pixel_8": "1", "nt_ntfy": "1",
                                             "nt_server": "https://ntfy.sh", "nt_token": "tk_secret", "nt_quiet_from": "21:30",
                                             "nt_quiet_to": "06:00"})
        s = modules.settings(self.stack, "notifications")
        self.assertEqual(s["phones"], ["pixel_8"])
        self.assertRegex(s["ntfy_topic"], r"^solar-[A-Za-z0-9-]{20,}$")
        self.assertEqual((s["ntfy_token"], s["quiet_from"], s["wall"]), ("tk_secret", "21:30", False))
        topic = s["ntfy_topic"]
        notifications.form_save(self.stack, {"nt_ha_app": "1", "nt_all_phones": "1", "nt_ntfy": "1", "nt_server": "https://ntfy.sh"})
        s = modules.settings(self.stack, "notifications")
        self.assertEqual((s["phones"], s["ntfy_topic"], s["ntfy_token"]), ("all", topic, "tk_secret"))   # kept
        notifications.form_save(self.stack, {"nt_ntfy": "1", "nt_server": "https://ntfy.sh", "nt_token": "-"})
        self.assertEqual(modules.settings(self.stack, "notifications")["ntfy_token"], "")
        html = notifications.form_section(self.stack)
        self.assertIn("<legend>Notifications</legend>", html)
        self.assertNotIn("tk_secret", html)
        self.assertIn("value='ntfy'>Test ntfy</button>", html)

    def test_alerts_page(self):
        notify._log(self.stack, {"at": NOON, "tag": "x", "title": "<b>T</b>", "message": "m", "priority": "info",
                                 "result": {"ha_app": "sent to Pixel 8"}})
        body = notifications.page(self.stack, {}).body
        self.assertIn("&lt;b&gt;T&lt;/b&gt;", body)
        self.assertIn("Home Assistant app: sent to Pixel 8", body)


if __name__ == "__main__":
    unittest.main()
