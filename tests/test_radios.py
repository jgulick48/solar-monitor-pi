"""Tests for the network radios (setup/radios.py, modules zigbee and zwave): addresses, .env lines, the seeded
Zigbee2MQTT configuration, the Z-Wave keys and external settings, the setup form, and the backup paths.
Run: python3 -m unittest discover -s tests -v"""
import json
import os
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
os.environ.setdefault("SOLAR_COMPOSE", "true")
import modules  # noqa: E402
import radios  # noqa: E402
from modules import FieldError, zigbee, zwave  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))
        with open(os.path.join(self.stack, "state", "secrets.json"), "w") as f:
            json.dump({"mqtt_homeassistant": "pw", "mqtt_bridge": "b", "grafana_admin": "g"}, f)
        self.started = []
        self.old = radios.start, radios.stop
        radios.start = lambda stack, service, before=None: (before and before(stack), self.started.append(service))
        radios.stop = lambda stack, service: self.started.append("-" + service)

    def tearDown(self):
        radios.start, radios.stop = self.old
        modules._ALL = None
        self.tmp.cleanup()


class Radios(Base):
    def test_addresses(self):
        self.assertEqual(radios.check_address(" 192.168.1.50 ", "6638"), ("192.168.1.50", 6638))
        self.assertEqual(radios.check_address("zigbee.local", 6638), ("zigbee.local", 6638))
        for host, port in (("", 1), ("bad host", 1), ("-x", 1), ("1.2.3.4", "0"), ("1.2.3.4", "70000"), ("1.2.3.4", "x")):
            with self.assertRaises(ValueError):
                radios.check_address(host, port)
        self.assertEqual(radios.tcp_url({"host": "10.0.0.5", "port": 6638}), "tcp://10.0.0.5:6638")
        self.assertEqual(radios.tcp_url({"host": "", "port": 6638}), "")
        self.assertIn("data-port='8081'", radios.port_link(8081))

    def test_reachable(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        threading.Thread(target=lambda: srv.accept(), daemon=True).start()
        try:
            self.assertTrue(radios.reachable("127.0.0.1", port))
        finally:
            srv.close()
        self.assertFalse(radios.reachable("127.0.0.1", port, timeout=1))


class Zigbee(Base):
    def test_off_until_an_address(self):
        self.assertFalse(modules.load(self.stack, "zigbee")["enabled"])
        modules.set_enabled(self.stack, "zigbee", True)
        self.assertEqual(self.started, [])                              # no address yet: nothing started
        self.assertEqual(zigbee.MODULE.compose_profiles(self.stack), [])
        zigbee.form_check(self.stack, {"zb_host": "", "zb_adapter": "ember"})   # empty is fine

    def test_form_env_and_seed(self):
        modules.set_enabled(self.stack, "zigbee", True)
        form = {"zb_host": "192.168.1.50", "zb_port": "6638", "zb_adapter": "ember", "zb_channel": "20"}
        zigbee.form_check(self.stack, form)
        for bad in ({"zb_host": "no way"}, {"zb_port": "x"}, {"zb_adapter": "xbee"}, {"zb_channel": "13"}):
            with self.assertRaises(FieldError):
                zigbee.form_check(self.stack, {**form, **bad})
        msgs = zigbee.form_save(self.stack, form)
        self.assertEqual(self.started, ["zigbee2mqtt"])
        self.assertIn("restarted", msgs[0])
        self.assertEqual(zigbee.env(self.stack), ["ZIGBEE_PORT=tcp://192.168.1.50:6638", "ZIGBEE_ADAPTER=ember",
                                                  "ZIGBEE_MQTT_PASSWORD=pw"])
        self.assertEqual(zigbee.MODULE.compose_profiles(self.stack), ["zigbee"])
        cfg = json.load(open(os.path.join(self.stack, "zigbee2mqtt", "configuration.yaml")))
        self.assertEqual(cfg["advanced"]["channel"], 20)
        self.assertEqual(cfg["version"], 5)
        self.assertEqual(cfg["advanced"]["network_key"], "GENERATE")
        self.assertTrue(cfg["homeassistant"]["enabled"])
        self.assertEqual(cfg["frontend"]["auth_token"], zigbee.ui_token(self.stack))
        # seeded once: Zigbee2MQTT owns the file afterwards
        with open(os.path.join(self.stack, "zigbee2mqtt", "configuration.yaml"), "w") as f:
            f.write("mine\n")
        zigbee.form_save(self.stack, {**form, "zb_port": "6639"})
        self.assertEqual(open(os.path.join(self.stack, "zigbee2mqtt", "configuration.yaml")).read(), "mine\n")
        # the same settings again: no restart
        n = len(self.started)
        self.assertEqual(zigbee.form_save(self.stack, {**form, "zb_port": "6639"}), [])
        self.assertEqual(len(self.started), n)
        # the address cleared: stopped
        zigbee.form_save(self.stack, {**form, "zb_host": ""})
        self.assertEqual(self.started[-1], "-zigbee2mqtt")

    def test_section(self):
        modules.set_enabled(self.stack, "zigbee", True)
        html = zigbee.form_section(self.stack)
        self.assertIn("<legend>Zigbee</legend>", html)
        self.assertIn("name='zb_channel'", html)                         # before the network is formed
        os.makedirs(os.path.join(self.stack, "zigbee2mqtt"), exist_ok=True)
        open(os.path.join(self.stack, "zigbee2mqtt", "coordinator_backup.json"), "w").close()
        self.assertNotIn("name='zb_channel'", zigbee.form_section(self.stack))


class ZWave(Base):
    def test_keys_settings_and_env(self):
        modules.set_enabled(self.stack, "zwave", True)
        self.assertEqual(self.started, [])
        form = {"zw_host": "192.168.1.51", "zw_port": "6638", "zw_region": "1"}
        zwave.form_check(self.stack, form)
        for bad in ({"zw_host": "a b"}, {"zw_port": ""}, {"zw_region": "4"}):
            with self.assertRaises(FieldError):
                zwave.form_check(self.stack, {**form, **bad})
        zwave.form_save(self.stack, form)
        self.assertEqual(self.started, ["zwave-js-ui"])
        k = zwave.keys(self.stack)
        self.assertEqual(sorted(k["keys"]), sorted(zwave.KEY_NAMES))
        self.assertTrue(all(len(v) == 32 and int(v, 16) >= 0 for v in k["keys"].values()))
        self.assertEqual(len(set(k["keys"].values())), 4)                 # all different
        self.assertEqual(zwave.keys(self.stack), k)                       # made once
        st = json.load(open(os.path.join(self.stack, "zwave", "box-settings.json")))
        self.assertEqual(st["securityKeys"], k["keys"])
        self.assertEqual((st["rf"]["region"], st["serverPort"], st["serverHost"]), (1, 3000, "127.0.0.1"))
        self.assertTrue(st["serverEnabled"])
        env = zwave.env(self.stack)
        self.assertEqual(env[0], "ZWAVE_PORT=tcp://192.168.1.51:6638")
        self.assertTrue(env[1].startswith("ZWAVE_SESSION_SECRET=") and len(env[1]) > 40)
        self.assertEqual(zwave.MODULE.compose_profiles(self.stack), ["zwave"])
        self.assertIn("ws://127.0.0.1:3000", zwave.form_section(self.stack))
        # region change: rewritten and restarted
        zwave.form_save(self.stack, {**form, "zw_region": "9"})
        self.assertEqual(json.load(open(os.path.join(self.stack, "zwave", "box-settings.json")))["rf"]["region"], 9)


class Wiring(unittest.TestCase):
    def test_compose_and_backup(self):
        compose = open(os.path.join(ROOT, "stack", "compose.yaml")).read()
        for needle in ("koenkk/zigbee2mqtt:2.14.3", "profiles: [zigbee]", "ZIGBEE2MQTT_CONFIG_SERIAL_PORT: ${ZIGBEE_PORT:-}",
                       "zwavejs/zwave-js-ui:11.25.2", "profiles: [zwave]", "ZWAVE_EXTERNAL_SETTINGS: /usr/src/app/store/box-settings.json"):
            self.assertIn(needle, compose)
        backup = open(os.path.join(ROOT, "setup", "backup.py")).read()
        self.assertIn('for d in ("zigbee2mqtt", "zwave") if os.path.isdir(os.path.join(stack, d))', backup)


if __name__ == "__main__":
    unittest.main()
