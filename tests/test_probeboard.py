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


if __name__ == "__main__":
    unittest.main()
