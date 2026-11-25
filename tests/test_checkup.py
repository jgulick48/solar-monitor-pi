"""Tests for the check-up page (setup/checkup.py) and the modules' own lines on it.
Run: python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import checkup  # noqa: E402
import modules  # noqa: E402

SITE = {"latitude": "39.7", "longitude": "-105", "battery_count": "6", "battery_kwh_each": "5.12", "time_zone": "",
        "arrays": [{"panels": "12", "panel_watts": "400"}]}


class Checkup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))

    def tearDown(self):
        modules._ALL = None
        self.tmp.cleanup()

    def test_site(self):
        self.assertEqual(checkup.site_check(SITE)["state"], "ok")
        c = checkup.site_check({"arrays": [{"panels": "", "panel_watts": ""}]})
        self.assertEqual(c["state"], "bad")
        self.assertIn("the location, the batteries, the solar panels", c["text"])
        self.assertEqual(c["link"], ("/setup", "Setup"))

    def test_dongles(self):
        inv = {"BA1": {}, "BA2": {}}
        self.assertEqual(checkup.dongle_check({}, {})["state"], "bad")
        self.assertEqual(checkup.dongle_check(inv, None)["state"], "warn")
        live = {"BA1": {"connected": True, "age": 4.0, "drops": 0}, "BA2": {"connected": True, "age": 5.0, "drops": 1}}
        c = checkup.dongle_check(inv, live)
        self.assertEqual((c["state"], c["text"]), ("ok", "2 dongles connected and sending data."))
        live["BA2"] = {"connected": True, "age": 900.0, "drops": 0}
        c = checkup.dongle_check(inv, live)
        self.assertEqual(c["state"], "bad")
        self.assertIn("BA2: connected but silent for 15 min", c["text"])
        del live["BA2"]
        self.assertIn("BA2: no connection yet", checkup.dongle_check(inv, live)["text"])

    def test_data_and_home_assistant(self):
        self.assertEqual([checkup.data_check(a)["state"] for a in (None, 900, 12)], ["bad", "bad", "ok"])
        self.assertEqual(checkup.ha_check(self.stack, fetch=lambda url: 502)["state"], "bad")

        def down(url):
            raise OSError("refused")
        self.assertIn("refused", checkup.ha_check(self.stack, fetch=down)["text"])
        c = checkup.ha_check(self.stack, fetch=lambda url: 200)              # running, no owner account yet
        self.assertEqual((c["state"], c["link"]), ("warn", (":8123", "Home Assistant")))
        os.makedirs(os.path.join(self.stack, "homeassistant", ".storage"))
        with open(os.path.join(self.stack, "homeassistant", ".storage", "onboarding"), "w") as f:
            json.dump({"data": {"done": ["user"]}}, f)
        self.assertEqual(checkup.ha_check(self.stack, fetch=lambda url: 200)["state"], "ok")

    def test_forecast(self):
        now = time.time()
        self.assertEqual(checkup.forecast_check(self.stack, {}, now)["state"], "bad")           # no location
        self.assertIn("No forecast yet", checkup.forecast_check(self.stack, SITE, now)["text"])

        def write(**kw):
            with open(os.path.join(self.stack, "state", "forecast.json"), "w") as f:
                json.dump({"generated": now - 600, "hours": [[now + 5 * 86400, 0.0]], "calibration_source": "calibrated", **kw}, f)
        write()
        c = checkup.forecast_check(self.stack, SITE, now)
        self.assertEqual((c["state"], c["text"]), ("ok", "Fetched 10 minutes ago, 5 days ahead, tuned to this site's real output."))
        write(calibration_source="default")
        self.assertEqual(checkup.forecast_check(self.stack, SITE, now)["state"], "warn")
        write(generated=now - 9 * 3600)
        self.assertEqual(checkup.forecast_check(self.stack, SITE, now)["state"], "bad")

    def test_box_and_clock(self):
        ok = {"level": "ok", "findings": [], "cpu_temp": 48.2, "disk": {"free_gb": 180, "free_pct": 80}}
        self.assertEqual(checkup.box_check(ok)["text"], "Temperature, power, disk and services are fine (48 °C, 180 GB free).")
        self.assertEqual(checkup.box_check({**ok, "level": "note", "findings": [("note", "The Pi is warm: 72 °C.")]})["state"], "warn")
        c = checkup.box_check({**ok, "level": "warn", "findings": [("warn", "Not running: grafana.")]})
        self.assertEqual((c["state"], c["text"]), ("bad", "Not running: grafana."))

        class R:
            def __init__(self, out):
                self.returncode, self.stdout = 0, out
        self.assertEqual(checkup.clock_check(SITE, run=lambda cmd: R("yes\n"))["state"], "ok")
        self.assertEqual(checkup.clock_check(SITE, run=lambda cmd: R("no\n"))["state"], "warn")
        self.assertEqual(checkup.clock_check({"time_zone": "Mars/Olympus"}, run=lambda cmd: R("yes\n"))["state"], "warn")

    def test_summary_and_page(self):
        cs = [checkup.check("A", "ok", "fine"), checkup.check("B", "off", "not set up", ("/setup#b", "Setup")),
              checkup.check("C <x>", "bad", "broken & sad", (":8123", "Home Assistant")), checkup.check("D", "warn", "hm")]
        self.assertEqual(checkup.summary(cs), ("bad", "1 thing not working, 1 to look at."))
        self.assertEqual(checkup.summary(cs[:2]), ("ok", "Everything that is set up works."))
        page = checkup.render(cs, "box.local")
        order = [page.index(f"<b>{t}</b>") for t in ("C &lt;x&gt;", "D", "A", "B")]
        self.assertEqual(order, sorted(order))                              # problems first, not set up last
        self.assertIn("broken &amp; sad <a href='http://box.local:8123'>Home Assistant</a>", page)
        self.assertIn("class='ck-bad'", page)

    def test_module_checks(self):
        """Enabled modules add their lines; one that fails gets a line saying so."""
        from modules import backup, freeze, notifications, tunnel
        with open(os.path.join(self.stack, "state", "site.json"), "w") as f:
            json.dump({"units": "us_customary"}, f)
        self.assertEqual(backup.checks(self.stack)[0]["state"], "warn")       # no stick chosen
        self.assertEqual(tunnel.checks(self.stack)[0]["state"], "off")
        self.assertEqual(notifications.checks(self.stack)[0]["state"], "bad")  # nowhere to send
        self.assertEqual(freeze.checks(self.stack)[0]["state"], "bad")        # nothing picked
        modules.save(self.stack, "freeze", {"sensors": ["sensor.slab"]})
        self.assertIn("No reading from Slab", freeze.checks(self.stack)[0]["text"])
        for c in backup.checks(self.stack) + tunnel.checks(self.stack) + notifications.checks(self.stack):
            self.assertEqual(set(c), {"title", "state", "text", "link"})
        d = modules.load(self.stack, "freeze")
        d["enabled"] = True
        with open(os.path.join(self.stack, "state", "modules", "freeze.json"), "w") as f:
            json.dump(d, f)
        titles = [c["title"] for c in modules.checks(self.stack)]
        self.assertIn("Freeze protection", titles)
        old = freeze.MODULE.checks
        try:
            object.__setattr__(freeze.MODULE, "checks", lambda stack: 1 / 0)
            got = [c for c in modules.checks(self.stack) if c["title"] == "Freeze protection"]
            self.assertEqual((got[0]["state"], got[0]["text"]), ("warn", "Couldn't be checked (ZeroDivisionError)."))
        finally:
            object.__setattr__(freeze.MODULE, "checks", old)


if __name__ == "__main__":
    unittest.main()
