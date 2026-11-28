"""Tests for the probe board on the setup page (setup/probeboard.py, the "probeboard" module): port names, the build
files, finding the ESP32 on USB, the install/update job (with a fake docker), and adding it to Home Assistant.
Run: python3 -m unittest discover -s tests -v"""
import base64
import json
import os
import stat
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import modules  # noqa: E402
import probeboard  # noqa: E402
import yaml  # noqa: E402
from modules import probeboard as mod  # noqa: E402

FAKE_DOCKER = """#!/bin/sh
echo "$@" >> "{log}"
case "$1" in
  image) exit {inspect} ;;
  pull) echo "pulling"; exit 0 ;;
  run) echo "INFO Successfully compiled program."; echo "INFO Successfully uploaded program."; exit {run} ;;
esac
"""


class ProbeBoard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = os.path.join(self.tmp.name, "stack")
        os.makedirs(os.path.join(self.stack, "state"))
        self.root = os.path.join(self.tmp.name, "root")
        os.makedirs(os.path.join(self.root, "dev", "serial", "by-id"))
        self.old = probeboard.ROOT, probeboard.DOCKER
        probeboard.ROOT = self.root
        os.environ["SOLAR_COMPOSE"] = "true"              # compose calls succeed and do nothing
        self.docker_log = os.path.join(self.tmp.name, "docker.log")

    def tearDown(self):
        probeboard.ROOT, probeboard.DOCKER = self.old
        os.environ.pop("SOLAR_COMPOSE", None)
        modules._ALL = None
        self.tmp.cleanup()

    def fake_docker(self, inspect=0, run=0):
        p = os.path.join(self.tmp.name, "docker")
        with open(p, "w") as f:
            f.write(FAKE_DOCKER.format(log=self.docker_log, inspect=inspect, run=run))
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        probeboard.DOCKER = [p]

    def calls(self):
        with open(self.docker_log) as f:
            return f.read().splitlines()

    def plug_in(self):
        tty = os.path.join(self.root, "dev", "ttyACM0")
        open(tty, "w").close()
        os.symlink(tty, os.path.join(self.root, "dev", "serial", "by-id", "usb-Espressif_USB_JTAG_serial_debug_unit_60:55:F9:00:00:01-if00"))
        os.symlink(tty, os.path.join(self.root, "dev", "serial", "by-id", "usb-FTDI_FT232R_USB_UART_A1B2-if00-port0"))
        return tty

    def ha_storage(self):
        p = probeboard.entries_path(self.stack)
        os.makedirs(os.path.dirname(p))
        with open(p, "w") as f:
            json.dump({"version": 1, "minor_version": 5, "key": "core.config_entries",
                       "data": {"entries": [{"domain": "mqtt", "entry_id": "m1", "data": {}}]}}, f)
        return p

    def test_names(self):
        self.assertEqual(probeboard.clean_name('  Garage "west" ${x} \\n ', "P1"), "Garage west x n")
        self.assertEqual(probeboard.clean_name("", "Floor supply"), "Floor supply")
        self.assertEqual(len(probeboard.clean_name("x" * 40, "d")), probeboard.NAME_MAX)
        r = mod.save_names(self.stack, {"p1": "Kitchen loop", "p2": "", "p8": 'Out"side'})
        self.assertIn("port names saved", r.setup_msg)
        names = probeboard.settings(self.stack)["names"]
        self.assertEqual(names[:2], ["Kitchen loop", "Floor return"])
        self.assertEqual(names[7], "Outside")

    def test_build_files(self):
        names = list(probeboard.NAMES)
        names[0] = "Kitchen loop"
        wd = probeboard.write_build_files(self.stack, names)
        box = yaml.safe_load(open(os.path.join(wd, "box.yaml")).read().replace("!include", "include"))
        self.assertEqual(box["substitutions"]["p1_name"], "Kitchen loop")
        self.assertEqual(box["substitutions"]["p8_name"], "Probe P8")
        self.assertEqual(box["packages"]["board"], "include boiler-probes.yaml")
        self.assertTrue(os.path.exists(os.path.join(wd, "boiler-probes.yaml")))
        sec = yaml.safe_load(open(os.path.join(wd, "secrets.yaml")))
        self.assertEqual(len(base64.b64decode(sec["api_encryption_key"])), 32)
        self.assertEqual(sec["api_encryption_key"], probeboard.api_key(self.stack))     # kept: the same each build
        self.assertEqual(os.stat(os.path.join(wd, "secrets.yaml")).st_mode & 0o777, 0o600)
        # the board's own file names each port from these substitutions
        src = open(os.path.join(wd, "boiler-probes.yaml")).read()
        for i in range(1, 9):
            self.assertIn(f"name: ${{p{i}_name}}", src)
        # the chip's revision decides the build: revision 3 and later by default, older ones on request
        self.assertIn('old_chip: "false"', open(os.path.join(wd, "box.yaml")).read())
        probeboard.write_build_files(self.stack, names, old_chip=True)
        self.assertIn('old_chip: "true"', open(os.path.join(wd, "box.yaml")).read())
        self.assertIn("engineering_sample: ${old_chip}", src)
        self.assertNotIn("esp32-p4-evboard", src)                  # that board name forces the build for old chips


    def test_usb(self):
        self.assertEqual(probeboard.usb_boards(), [])
        self.assertIn("no ESP32 found", probeboard.start(self.stack, "usb"))
        tty = self.plug_in()
        self.assertEqual(probeboard.usb_boards(), [tty])                 # the Espressif one, not the FTDI adapter
        # the ESP32-P4-ETH's USB-C is a WCH CH343 bridge, which doesn't say "Espressif"
        by_id = os.path.join(self.root, "dev", "serial", "by-id")
        for n in os.listdir(by_id):
            os.remove(os.path.join(by_id, n))
        self.assertEqual(probeboard.usb_boards(), [])
        os.symlink(tty, os.path.join(by_id, "usb-1a86_USB_Single_Serial_5C93018101-if00"))
        self.assertEqual(probeboard.usb_boards(), [tty])

    def test_install_over_usb(self):
        self.fake_docker(inspect=1)                                      # no ESPHome image yet: pulled first
        self.ha_storage()
        tty = self.plug_in()
        probeboard.run_job(self.stack, "usb")
        j = probeboard.job(self.stack)
        self.assertEqual(j["status"], "ok", j)
        self.assertIn("installed over USB", j["message"])
        self.assertIn("added to Home Assistant", j["message"])
        calls = self.calls()
        self.assertTrue(calls[0].startswith("image inspect"))
        self.assertTrue(calls[1].startswith("pull " + probeboard.IMAGE))
        self.assertIn(f"--device {tty}:{tty}", calls[2])
        self.assertTrue(calls[2].endswith(f"{probeboard.IMAGE} run box.yaml --device {tty} --no-logs"))
        d = probeboard.settings(self.stack)
        self.assertTrue(d["installed"] and d["ha_added"])
        self.assertEqual(d["built_names"], probeboard.NAMES)
        e = probeboard.ha_entry(self.stack)
        self.assertEqual(e["data"], {"host": probeboard.HOST, "port": 6053, "password": "",
                                     "noise_psk": probeboard.api_key(self.stack), "device_name": "boiler-probes"})
        self.assertIsNone(e["unique_id"])                                # Home Assistant sets the MAC on connecting

    def test_update_over_the_network(self):
        self.fake_docker()
        self.ha_storage()
        probeboard.run_job(self.stack, "network")
        calls = self.calls()
        self.assertEqual(len(calls), 2)                                  # image present: no pull
        self.assertNotIn("--device /dev", calls[1])
        self.assertTrue(calls[1].endswith(f"run box.yaml --device {probeboard.HOST} --no-logs"))

    def test_failed_build(self):
        self.fake_docker(run=1)
        probeboard.run_job(self.stack, "network")
        j = probeboard.job(self.stack)
        self.assertEqual(j["status"], "error")
        self.assertIn("the build or the install failed", j["message"])
        self.assertIn("INFO Successfully compiled program.", j["log"])  # its output kept for the page
        self.assertFalse(probeboard.settings(self.stack)["installed"])

    def test_interrupted_job(self):
        with open(probeboard.job_path(self.stack), "w") as f:
            json.dump({"status": "running", "step": "Building"}, f)
        self.assertEqual(probeboard.job(self.stack)["status"], "error")  # no job in this process: cut short

    def test_ha_entry(self):
        self.ha_storage()
        self.assertTrue(probeboard.write_ha_entry(self.stack))
        self.assertFalse(probeboard.write_ha_entry(self.stack))          # already there, same key
        doc = json.load(open(probeboard.entries_path(self.stack)))
        self.assertEqual([e["domain"] for e in doc["data"]["entries"]], ["mqtt", "esphome"])
        # Home Assistant has since set the MAC; a new key (secrets reset) is put in the same entry
        e = doc["data"]["entries"][1]
        e["unique_id"], e["data"]["noise_psk"] = "60:55:f9:00:00:01", "old"
        json.dump(doc, open(probeboard.entries_path(self.stack), "w"))
        self.assertTrue(probeboard.write_ha_entry(self.stack))
        doc = json.load(open(probeboard.entries_path(self.stack)))
        self.assertEqual(len(doc["data"]["entries"]), 2)
        self.assertEqual(doc["data"]["entries"][1]["unique_id"], "60:55:f9:00:00:01")
        self.assertEqual(doc["data"]["entries"][1]["data"]["noise_psk"], probeboard.api_key(self.stack))
        self.assertEqual(probeboard.add_to_ha(self.stack), ["already in Home Assistant"])

    def test_page(self):
        html = mod.setup_section(self.stack)
        self.assertIn("Not installed yet", html)
        self.assertIn("No ESP32 on the box's USB ports", html)
        self.assertIn("name='p1' value='Floor supply'", html)
        self.assertRegex(html, r'value="usb"[^>]*disabled')                  # nothing on USB yet
        self.assertRegex(html, r'value="network"[^>]*disabled')              # nothing installed yet
        self.plug_in()
        d = probeboard.settings(self.stack)
        d.update(installed=int(time.time()), built_names=list(probeboard.NAMES), names=["Kitchen"] + probeboard.NAMES[1:])
        probeboard.save(self.stack, d)
        html = mod.setup_section(self.stack)
        self.assertNotRegex(html, r'value="usb"[^>]*disabled')
        self.assertIn("The port names changed since", html)
        # when a job ends the page is fetched again with a GET: reloading the answer to the "Install" POST would
        # send the POST again and start the install over (it flashed the first real board in a loop)
        self.assertIn("location.replace('/setup#probeboard')", html)
        self.assertNotIn("location.hash='probeboard'; location.reload();}}catch", html)

    # ---------------------------------------------------------------- the check-up page
    def events(self, **readings):
        """The board's event stream: every probe port by its default name, the clamps and the heat call."""
        vals = {"floor_supply": 41.25, "floor_return": 35.0, "boiler_supply": 60.5, "boiler_return": None,
                "hot_water_outlet": 48.0, "slab_near_boiler": None, "slab_far": None, "probe_p8": None, **readings}
        lines = ["retry: 30000", "event: ping", 'data: {"title":"Boiler probes","ota":true}', ""]
        for k, v in vals.items():
            lines += ["event: state", "data: " + json.dumps({"id": f"sensor-{k}", "value": v, "state": "NA" if v is None else f"{v} °C"}), ""]
        lines += ["event: state", 'data: {"id":"sensor-boiler_pump_current","value":0.42,"state":"0.42 A"}', "",
                  "event: state", 'data: {"name_id":"sensor/Floor pump current","name":"Floor pump current","value":0,"state":"0.00 A"}', "",
                  "event: state", 'data: {"id":"binary_sensor-call_for_heat","value":true,"state":"ON"}', "",
                  "event: log", "data: [I][sensor:094]: 'Floor supply': Sending state 41.25", "",
                  "event: state", "data: not json", ""]
        return lambda: lines

    def installed(self, **kw):
        d = probeboard.settings(self.stack)
        d.update(installed=int(time.time()), built_names=list(probeboard.NAMES), **kw)
        probeboard.save(self.stack, d)

    def test_events(self):
        got = probeboard.live(self.events())
        self.assertEqual(got[("sensor", "floor_supply")], 41.25)
        self.assertIsNone(got[("sensor", "probe_p8")])                       # a port with no probe
        self.assertEqual(got[("sensor", "floor_pump_current")], 0.0)         # the newer id form, found by name
        self.assertIs(got[("binary_sensor", "call_for_heat")], True)
        self.assertNotIn(("log", ""), got)
        with self.assertRaises(OSError):
            probeboard.live(lambda: ["event: ping", "data: {}"])            # answered, but no readings

    def test_fetch_events_from_a_stream_that_stays_open(self):
        """The board keeps the connection open after its readings: fetch_events stops when it goes quiet."""
        import http.server
        import threading
        lines = self.events()()
        release = threading.Event()

        class Board(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/events" else 404)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(("\r\n".join(lines) + "\r\n").encode())
                self.wfile.flush()
                release.wait(10)                                             # ...and says nothing more

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Board)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            t0 = time.time()
            got = probeboard.parse_events(probeboard.fetch_events("127.0.0.1", srv.server_address[1]))
            self.assertLess(time.time() - t0, probeboard.EVENTS_WAIT + 1)
            self.assertEqual(got[("sensor", "hot_water_outlet")], 48.0)
            self.assertIs(got[("binary_sensor", "call_for_heat")], True)
        finally:
            release.set()
            srv.shutdown()
            srv.server_close()
        with self.assertRaises(OSError):
            probeboard.fetch_events("127.0.0.1", srv.server_address[1])     # nothing listening any more

    def test_checks(self):
        self.assertEqual([c["state"] for c in mod.checks(self.stack)], ["off"])            # not installed: optional
        self.installed()

        def down():
            raise OSError("Name or service not known")
        c = mod.checks(self.stack, down)
        self.assertEqual((len(c), c[0]["state"]), (1, "bad"))
        self.assertIn("boiler-probes.local", c[0]["text"])
        self.assertEqual(probeboard.seen(self.stack), [])
        board, ports, inputs = mod.checks(self.stack, self.events())
        self.assertEqual((board["state"], ports["state"], inputs["state"]), ("ok", "ok", "ok"))
        self.assertIn("4 of 8 ports", board["text"])
        self.assertIn("P1 Floor supply 106.2 °F", ports["text"])              # the site's units (US by default)
        self.assertIn("No probe on P4, P6, P7, P8.", ports["text"])
        self.assertIn("CT1 (boiler pump) 0.42 A; CT2 (floor pump) 0.00 A; call for heat on", inputs["text"])
        self.assertEqual(probeboard.seen(self.stack), [1, 2, 3, 5])
        # P3's plug is pulled: it had a probe, so that is a fault; P4 never had one and still isn't
        _, ports, _ = mod.checks(self.stack, self.events(boiler_supply=None))
        self.assertEqual(ports["state"], "bad")
        self.assertIn("No probe answers on P3 (Boiler supply)", ports["text"])
        self.assertNotIn("P4", ports["text"])
        self.assertEqual(probeboard.seen(self.stack), [1, 2, 3, 5])           # still remembered
        # taken out on purpose: forgotten, and the page says so
        self.assertIn("P1, P2, P3, P5", mod.setup_section(self.stack))
        mod.forget(self.stack, {})
        _, ports, _ = mod.checks(self.stack, self.events(boiler_supply=None))
        self.assertEqual(ports["state"], "ok")
        self.assertIn("No probe on P3, P4", ports["text"])
        # nothing plugged in yet
        mod.forget(self.stack, {})
        empty = {k: None for k in ("floor_supply", "floor_return", "boiler_supply", "hot_water_outlet")}
        self.assertEqual(mod.checks(self.stack, self.events(**empty))[1]["state"], "warn")

    def test_ports_follow_the_built_names_and_metric(self):
        self.installed()
        d = probeboard.settings(self.stack)
        d.update(built_names=["Kitchen loop"] + probeboard.NAMES[1:], names=["Renamed since"] + probeboard.NAMES[1:])
        probeboard.save(self.stack, d)
        json.dump({"units": "metric"}, open(os.path.join(self.stack, "state", "site.json"), "w"))
        lines = self.events()() + ["event: state", 'data: {"id":"sensor-kitchen_loop","value":21.5,"state":"21.5 °C"}']
        ports = mod.checks(self.stack, lambda: lines)[1]
        self.assertIn("P1 Kitchen loop 21.5 °C", ports["text"])              # the name the firmware has, not the new one

    def test_the_collector_remembers_ports(self):
        probeboard.remember(self.stack, self.events())                        # not installed: nothing
        self.assertEqual(probeboard.seen(self.stack), [])
        self.installed()

        def down():
            raise OSError("no route")
        probeboard.remember(self.stack, down)                                 # offline: nothing, and no error
        probeboard.remember(self.stack, self.events())
        self.assertEqual(probeboard.seen(self.stack), [1, 2, 3, 5])
        self.assertIn(300, mod.MODULE.every)


if __name__ == "__main__":
    unittest.main()
