"""Tests for the Home Assistant state feed (setup/hastates.py, setup/mqttsub.py) and the "Home Assistant history"
module: statestream lines in packages/site.yaml, parsing what Home Assistant publishes, the latest-state file, the
VictoriaMetrics lines and their once-a-minute repeat, and the MQTT client against a tiny broker.
Run: python3 -m unittest discover -s tests -v"""
import http.server
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
os.environ.setdefault("SOLAR_COMPOSE", "true")
import hastates  # noqa: E402
import modules  # noqa: E402
import mqttsub  # noqa: E402
import solar_setup as ss  # noqa: E402
from modules import FieldError, hahistory  # noqa: E402

T0 = 1_800_000_000.0


def reset():
    with hastates._lock:
        hastates._latest.clear()
        hastates._sent.clear()
        hastates._queue.clear()
        hastates._status.update(ha="online", dirty=False)


class Base(unittest.TestCase):
    def setUp(self):
        reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))
        os.makedirs(os.path.join(self.stack, "homeassistant", "packages"))
        os.makedirs(os.path.join(self.stack, "homeassistant", ".storage"))

    def tearDown(self):
        reset()
        modules._ALL = None
        self.tmp.cleanup()

    def pub(self, entity, key, value, now):
        d, o = entity.split(".")
        hastates.on_message(f"solar/ha/{d}/{o}/{key}", value.encode(), now=now)


class Values(Base):
    def test_numbers(self):
        for text, want in (("41.25", 41.25), ("on", 1), ("off", 0), ("heating", 1), ("idle", 0), ("cooling", -1),
                           (True, 1), (3, 3), ('"7.5"', 7.5)):
            self.assertEqual(hastates.number(text), want, text)
        for text in ("unavailable", "unknown", "heat", "nan", "inf", ""):
            self.assertIsNone(hastates.number(text), text)

    def test_states_attributes_and_repeat(self):
        self.pub("sensor.floor_supply", "state", "41.25", T0)
        self.pub("sensor.floor_supply", "unit_of_measurement", '"°C"', T0)
        self.pub("climate.t6", "state", "heat", T0)                       # text: kept, not recorded
        self.pub("climate.t6", "hvac_action", '"heating"', T0)
        self.pub("climate.t6", "current_temperature", "20.5", T0)
        self.pub("climate.t6", "friendly_name", '"T6"', T0)               # not an attribute we keep
        hastates.on_message("other/topic/x/y/state", b"1", now=T0)        # not the statestream: ignored
        ms = int(T0 * 1000)
        self.assertEqual(hastates._queue, [
            f'ha_state{{entity_id="sensor.floor_supply"}} 41.25 {ms}',
            f'ha_attribute{{entity_id="climate.t6",attribute="hvac_action"}} 1 {ms}',
            f'ha_attribute{{entity_id="climate.t6",attribute="current_temperature"}} 20.5 {ms}'])
        self.assertTrue(hastates.flush(self.stack))
        got = hastates.get(self.stack, "sensor.floor_supply")
        self.assertEqual((got["state"], got["unit"], got["at"]), ("41.25", "°C", T0))
        self.assertEqual(hastates.get(self.stack, "climate.t6")["attributes"], {"hvac_action": "heating", "current_temperature": 20.5})
        self.assertFalse(hastates.flush(self.stack))                      # nothing new
        # the same value again later: "at" stays the time it changed
        self.pub("sensor.floor_supply", "state", "41.25", T0 + 5)
        self.assertEqual(hastates._latest["sensor.floor_supply"]["at"], T0)
        hastates._queue.clear()
        # the once-a-minute repeat: only what wasn't sent in the last minute
        self.assertEqual(hastates.repeat_due(T0 + 30), 0)
        self.assertEqual(hastates.repeat_due(T0 + 66), 3)
        self.assertIn(f'ha_state{{entity_id="sensor.floor_supply"}} 41.25 {int((T0 + 66) * 1000)}', hastates._queue)
        # Home Assistant offline: no repeats (its gap shows as a gap)
        hastates.on_message("homeassistant/status", b"offline")
        self.assertEqual(hastates.repeat_due(T0 + 200), 0)
        hastates.on_message("homeassistant/status", b"online")
        self.assertEqual(hastates.repeat_due(T0 + 200), 3)
        # unavailable: no longer repeated
        self.pub("sensor.floor_supply", "state", "unavailable", T0 + 210)
        self.assertEqual(hastates.repeat_due(T0 + 300), 2)

    def test_restore_fallback_and_known(self):
        st = os.path.join(self.stack, "homeassistant", ".storage")
        with open(os.path.join(st, "core.restore_state"), "w") as f:
            json.dump({"data": [{"state": {"entity_id": "sensor.slab", "state": "22.1", "last_changed": "2027-01-15T08:00:00+00:00",
                                           "attributes": {"unit_of_measurement": "°C", "icon": "x"}}}]}, f)
        got = hastates.get(self.stack, "sensor.slab")
        self.assertEqual((got["state"], got["unit"], got["attributes"]), ("22.1", "°C", {}))
        self.assertIsNone(hastates.get(self.stack, "sensor.none"))
        with open(os.path.join(st, "core.entity_registry"), "w") as f:
            json.dump({"data": {"entities": [
                {"entity_id": "sensor.slab", "original_name": "Slab", "original_device_class": "temperature", "unit_of_measurement": "°C"},
                {"entity_id": "sensor.boiler_power", "original_name": "Boiler power", "original_device_class": "power"},
                {"entity_id": "sensor.old", "disabled_by": "user"},
                {"entity_id": "light.porch"}]}}, f)
        self.assertEqual([k["entity_id"] for k in hastates.known(self.stack)], ["sensor.boiler_power", "sensor.slab"])
        self.assertEqual([k["entity_id"] for k in hastates.known(self.stack, device_class="temperature")], ["sensor.slab"])

    def test_vm_push_keeps_lines_when_unreachable(self):
        got = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                got.append((self.path, self.rfile.read(int(self.headers["Content-Length"])).decode()))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old = hastates.VM
        try:
            self.pub("sensor.a", "state", "1", T0)
            hastates.VM = "http://127.0.0.1:1"                                # nothing listens
            with self.assertRaises(OSError):
                hastates.vm_push()
            self.assertEqual(len(hastates._queue), 1)
            hastates.VM = f"http://127.0.0.1:{srv.server_address[1]}"
            self.assertEqual(hastates.vm_push(), 1)
            self.assertEqual(got, [("/api/v1/import/prometheus", f'ha_state{{entity_id="sensor.a"}} 1 {int(T0 * 1000)}\n')])
            self.assertEqual(hastates._queue, [])
        finally:
            hastates.VM = old
            srv.shutdown()


class Package(Base):
    def test_statestream_lines(self):
        self.assertEqual(hastates.package([]), ([], []))
        top, auto = hastates.package(["sensor.b", "sensor.a", "sensor.b"])
        self.assertEqual(top[:4], ["mqtt_statestream:", "  base_topic: solar/ha", "  publish_attributes: true", "  include:"])
        self.assertEqual(top[-2:], ["      - sensor.a", "      - sensor.b"])
        pkg = ss.site_package({}, "wid", statestream=(top, auto))
        self.assertIn("mqtt_statestream:\n  base_topic: solar/ha", pkg)
        self.assertEqual(pkg.count("\nautomation:\n"), 1)                # one automation key, shared
        self.assertIn("  - id: solar_statestream_refresh", pkg)
        self.assertIn("            - sensor.a\n            - sensor.b\n", pkg)
        self.assertIn('topic: "solar/ha/{{ repeat.item | replace(\'.\', \'/\') }}/state"', pkg)
        self.assertNotIn("mqtt_statestream", ss.site_package({}))
        import yaml
        doc = yaml.safe_load(pkg)
        self.assertEqual(doc["mqtt_statestream"]["include"]["entities"], ["sensor.a", "sensor.b"])
        self.assertEqual([a["id"] for a in doc["automation"]], ["solar_statestream_refresh", "solar_test_notification"])


class History(Base):
    def site_yaml(self):
        with open(os.path.join(self.stack, "homeassistant", "packages", "site.yaml")) as f:
            return f.read()

    def test_form_and_site_yaml(self):
        self.assertFalse(modules.load(self.stack, "hahistory")["enabled"])
        modules.set_enabled(self.stack, "hahistory", True)
        self.assertNotIn("mqtt_statestream", self.site_yaml())            # nothing chosen yet
        form = {"hh_entity0": " Sensor.Boiler_Power ", "hh_entity1": "", "hh_entity2": "climate.t6", "hh_entity3": "climate.t6"}
        hahistory.form_check(self.stack, form)
        with self.assertRaises(FieldError):
            hahistory.form_check(self.stack, {"hh_entity0": "not an entity"})
        self.assertEqual(hahistory.form_save(self.stack, form), ["Home Assistant restarting to publish the new list of entities"])
        self.assertEqual(hahistory.chosen(self.stack), ["sensor.boiler_power", "climate.t6"])
        self.assertEqual(hastates.entities(self.stack), ["climate.t6", "sensor.boiler_power"])
        y = self.site_yaml()
        self.assertIn("      - climate.t6\n      - sensor.boiler_power\n", y)
        self.assertEqual(hahistory.form_save(self.stack, form), [])        # unchanged: no restart
        html = hahistory.form_section(self.stack)
        self.assertIn("value='climate.t6'", html)
        self.assertIn("name='hh_entity4'", html)                           # three empty lines after the two
        # turned off: Home Assistant stops publishing them
        modules.set_enabled(self.stack, "hahistory", False)
        self.assertNotIn("mqtt_statestream", self.site_yaml())


class Broker(unittest.TestCase):
    def test_subscribe_and_receive(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        seen = {}

        def broker():
            c, _ = srv.accept()
            first, body = mqttsub.read_packet(c)
            seen["connect"] = (first, body)
            c.sendall(b"\x20\x02\x00\x00")                                  # CONNACK, accepted
            first, body = mqttsub.read_packet(c)
            seen["subscribe"] = body
            c.sendall(b"\x90\x04\x00\x01\x00\x00")                          # SUBACK
            topic = b"solar/ha/sensor/a/state"
            c.sendall(mqttsub._packet(0x31, struct.pack("!H", len(topic)) + topic + b"12.5"))   # retained PUBLISH
            c.close()
        threading.Thread(target=broker, daemon=True).start()
        got = []
        with self.assertRaises(mqttsub.Closed):
            mqttsub.run("127.0.0.1", srv.getsockname()[1], "homeassistant", "pw", ["solar/ha/#", "homeassistant/status"],
                        lambda t, p: got.append((t, p)), client_id="t")
        srv.close()
        self.assertEqual(got, [("solar/ha/sensor/a/state", b"12.5")])
        self.assertEqual(seen["connect"][0], 0x10)
        self.assertIn(b"homeassistant", seen["connect"][1])
        self.assertIn(b"solar/ha/#", seen["subscribe"])
        self.assertIn(b"homeassistant/status", seen["subscribe"])

    def test_refused(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)

        def broker():
            c, _ = srv.accept()
            mqttsub.read_packet(c)
            c.sendall(b"\x20\x02\x00\x05")                                  # not authorised
            c.close()
        threading.Thread(target=broker, daemon=True).start()
        with self.assertRaises(mqttsub.Closed) as cm:
            mqttsub.run("127.0.0.1", srv.getsockname()[1], "u", "bad", ["x"], lambda t, p: None)
        srv.close()
        self.assertIn("code 5", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
