"""Tests for the generator module (setup/modules/generator): its switch writing site.json, its fields, its page and
card, its Home Assistant fields, and a setup-page save that turns it off.
Run: python3 -m unittest discover -s tests -v"""
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
os.environ.setdefault("SOLAR_COMPOSE", "true")
import genlog  # noqa: E402
import modules  # noqa: E402
from modules import generator as mod  # noqa: E402

SITE = {"name": "Test", "time_zone": "America/Denver", "latitude": "39.7", "longitude": "-105", "elevation": "",
        "units": "us_customary", "battery_count": "6", "battery_kwh_each": "5.12", "low_soc_alert": "25",
        "generator": "manual", "generator_kw": "5.0", "generator_fuel": "gasoline", "future_generator_kw": "28",
        "inverter_model": "6000xp", "costs": {}, "arrays": [{"name": "A", "panels": "24", "panel_watts": "285", "tilt": "30", "azimuth": "180"}]}


class GeneratorModule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))

    def tearDown(self):
        self.tmp.cleanup()

    def site(self, **kw):
        path = os.path.join(self.stack, "state", "site.json")
        if kw:
            with open(path, "w") as f:
                json.dump({**SITE, **kw}, f)
        with open(path) as f:
            return json.load(f)

    def test_off_for_a_site_without_a_generator(self):
        self.site(generator="none")
        self.assertFalse(modules.load(self.stack, "generator")["enabled"])
        self.assertEqual(modules.settings(self.stack, "generator"), {"mode": "manual"})

    def test_switch_writes_site_json(self):
        self.site(generator="automatic")
        self.assertTrue(modules.load(self.stack, "generator")["enabled"])
        modules.set_enabled(self.stack, "generator", False)
        self.assertEqual(self.site()["generator"], "none")                      # what "No generator" did
        self.assertNotIn("generator_running", modules.ha_fields(self.stack, self.site()))
        modules.set_enabled(self.stack, "generator", True)
        self.assertEqual(self.site()["generator"], "automatic")                 # how it was started comes back
        self.assertIn("generator_running", modules.ha_fields(self.stack, self.site()))

    def test_fields(self):
        self.site(generator="manual")
        html = modules.form_sections(self.stack)
        self.assertIn("<legend>Generator</legend>", html)
        self.assertIn("<option value='manual' selected>", html)
        with self.assertRaises(modules.FieldError) as e:
            modules.form_check(self.stack, {"module_generator_shown": "1", "generator_kw": "500"})
        self.assertEqual(e.exception.field, "generator_kw")
        modules.form_apply(self.stack, {"module_generator_shown": "1", "generator": "automatic", "generator_kw": "22",
                                        "generator_fuel": "propane"})
        s = self.site()
        self.assertEqual((s["generator"], s["generator_kw"], s["generator_fuel"]), ("automatic", "22.0", "propane"))
        self.assertEqual(modules.settings(self.stack, "generator"), {"mode": "automatic"})
        # the Chargeverter's current: "40", "40 A" and "40a" all mean 40; nonsense and 0 are refused; empty clears it
        self.assertIn("name='charger_amps'", modules.form_sections(self.stack))
        for bad in ("lots", "0", "500"):
            with self.assertRaises(modules.FieldError) as e:
                modules.form_check(self.stack, {"module_generator_shown": "1", "charger_amps": bad})
            self.assertEqual(e.exception.field, "charger_amps")
        for given in ("40", "40 A", "40a"):
            modules.form_apply(self.stack, {"module_generator_shown": "1", "generator": "manual", "charger_amps": given})
            self.assertEqual(self.site()["charger_amps"], "40")
        modules.form_apply(self.stack, {"module_generator_shown": "1", "generator": "manual", "charger_amps": ""})
        self.assertEqual(self.site()["charger_amps"], "")

    def test_stop_is_announced_once_per_run(self):
        from modules import voice
        gen = modules.all_modules() and __import__("modules.generator", fromlist=["x"])
        d = modules.load(self.stack, "voice")
        d["enabled"] = True
        os.makedirs(os.path.join(self.stack, "state", "modules"), exist_ok=True)
        with open(os.path.join(self.stack, "state", "modules", "voice.json"), "w") as f:
            json.dump(d, f)
        queue = lambda: [a["text"] for a in modules.ss.load_json(voice._queue_path(self.stack), [])]  # noqa: E731
        self.assertEqual(gen.announce_stop(self.stack, {"phase": "run", "level": "warn"}, now=1000), [])
        self.assertEqual(queue(), [])
        self.assertEqual(len(gen.announce_stop(self.stack, {"phase": "stop", "level": "ok"}, now=1060)), 1)
        self.assertEqual(len(queue()), 1)
        self.assertIn("The generator can be shut down", queue()[0])
        self.assertEqual(gen.announce_stop(self.stack, {"phase": "stop", "level": "ok"}, now=1120), [])   # once
        self.assertEqual(gen.announce_stop(self.stack, {"phase": "run", "level": "warn"}, now=1180), [])  # a dip: still once
        self.assertEqual(gen.announce_stop(self.stack, {"phase": "stop", "level": "ok"}, now=1240), [])
        self.assertEqual(len(queue()), 1)
        gen.announce_stop(self.stack, {"level": "ok"}, now=5000)                    # generator off: the next run is new
        self.assertEqual(len(gen.announce_stop(self.stack, {"phase": "stop", "level": "ok"}, now=90000)), 1)
        self.assertEqual(gen.MODULE.every[60], gen.announce_stop)

    def test_page_card_and_log_hook(self):
        self.site(generator="manual")
        self.assertIn(("/generator", "Generator", "Generator"), modules.nav(self.stack))
        m, fn = modules.route(self.stack, "GET", "/generator")
        self.assertIn("<h1>Generator</h1>", fn(self.stack, {}).body)
        m, fn = modules.route(self.stack, "POST", "/generator/serviced")
        self.assertEqual(fn(self.stack, {"kind": "inverter", "hours_since": "abc"}).code, 400)
        self.assertIn("Service recorded.", fn(self.stack, {"kind": "inverter", "hours_since": "2"}).body)
        # a run on the page with its fuel and cost, and the month's line
        st = genlog.load(self.stack)
        st["runs"] = [{"kind": "charger", "start": 1_795_000_000, "end": 1_795_007_200, "hours": 2.0, "kwh": 9.2, "peak_kw": 4.8}]
        st["hours"]["charger"] = 2.0
        genlog.save(self.stack, st)
        m, fn = modules.route(self.stack, "GET", "/generator")
        self.site(generator="manual", generator_gph="0.5", costs={"fuel_per_gal": "4"})
        body = fn(self.stack, {}).body
        self.assertIn("<td>1.0 gal</td><td>$4.00</td><td>DC charger</td>", body)
        self.assertIn("<h2>By month</h2>", body)
        self.assertIn("0.5 gallons an hour, as measured", body)
        self.site(generator="manual", generator_gph="", generator_kw="", costs={})
        self.assertIn("<td>&ndash;</td><td>&ndash;</td><td>DC charger</td>", fn(self.stack, {}).body)
        # the measured figure on the setup page
        self.assertIn("name='generator_gph'", modules.form_sections(self.stack))
        with self.assertRaises(modules.FieldError) as e:
            modules.form_check(self.stack, {"module_generator_shown": "1", "generator_gph": "lots"})
        self.assertEqual(e.exception.field, "generator_gph")
        modules.form_apply(self.stack, {"module_generator_shown": "1", "generator": "manual", "generator_gph": "0.60"})
        self.assertEqual(self.site()["generator_gph"], "0.6")
        old = genlog.update
        genlog.update = lambda stack: 2
        try:
            self.assertEqual(mod.update_log(self.stack), ["generator log: 2 new run(s)"])
        finally:
            genlog.update = old

    def test_setup_save_turning_it_off_reaches_home_assistant(self):
        import solar_web
        self.site(generator="manual")
        solar_web.STACK = self.stack
        solar_web.ss.host_timezone = lambda: SITE["time_zone"]
        web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), solar_web.Handler)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        try:
            form = {k: v for k, v in SITE.items() if isinstance(v, str)}
            form.update(a1_name="A", a1_panels="24", a1_panel_watts="285", a1_tilt="30", a1_azimuth="180",
                        weather_source="openmeteo", display_enabled="1", modules_form="1", module_backup="1",
                        module_weather="1")                                     # the generator's switch left off
            page = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{web.server_port}/setup",
                                                                 data=urllib.parse.urlencode(form).encode(), method="POST")).read().decode()
            self.assertIn("generator off", page)
            self.assertEqual(self.site()["generator"], "none")
            with open(os.path.join(self.stack, "homeassistant", "packages", "site.yaml")) as f:
                self.assertIn('state: "none"', f.read())                         # Home Assistant's "Site generator mode"
            self.assertNotIn("<legend>Generator</legend>", solar_web.setup_page(solar_web.load_site()))
            self.assertEqual(self.site()["generator_kw"], "5.0")                 # its settings are kept
        finally:
            web.shutdown()
            web.server_close()


if __name__ == "__main__":
    unittest.main()
