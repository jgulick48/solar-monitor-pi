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
        self.assertEqual(modules.settings(self.stack, "generator"), mod.DEFAULTS)
        self.assertEqual(mod.DEFAULTS["mode"], "manual")

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
        self.assertEqual(modules.settings(self.stack, "generator")["mode"], "automatic")
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
        self.assertEqual(gen.MODULE.every[60], gen.every_minute)                  # which calls announce_stop first

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

    # ---------------------------------------------------------------- following a run: notices, reminders, fuel
    def minute(self, t, gen=0, soc=50, age=10, **advice):
        """One collector minute with the box seeing this; returns what was sent: [(tag, title, text, priority)]."""
        sent = []
        mod.every_minute(self.stack, {"now": {"gen": gen, "soc": soc, "age": age}, "advice": advice or None}, now=t,
                         send=lambda *a: sent.append(a) or "")
        return sent

    def log_run(self, start, hours, kwh):
        st = genlog.load(self.stack)
        st["runs"].append({"kind": "charger", "start": start, "end": start + int(hours * 3600), "hours": hours, "kwh": kwh, "peak_kw": 4.8})
        st["processed_until"] = start + int(hours * 3600)
        genlog.save(self.stack, st)

    def test_started_and_stopped_notices(self):
        self.site(generator="manual", generator_gph="0.6", costs={"fuel_per_gal": "4"})
        t = 1_795_000_000
        run = dict(level="warn", phase="run", need_kwh=3.1, run_hours=0.75, text="Keep the generator running")
        self.assertEqual(self.minute(t), [])
        self.assertEqual(self.minute(t + 60, 4800, 40, **run), [])              # a minute of power isn't a start yet
        self.assertEqual(self.minute(t + 120, 4800, 40, **run), [])
        self.assertEqual(self.minute(t + 180, 4800, 41, **run), [])
        (tag, title, text, prio), = self.minute(t + 240, 4800, 41, **run)
        self.assertEqual((tag, title, prio), ("generator_run", "Generator started", "info"))
        self.assertIn("giving about 4.8 kW. Battery 41%. It needs to run about 45 min", text)
        for m in range(5, 65):
            self.assertEqual(self.minute(t + m * 60, 4800, 41 + m // 3, **run), [])
        self.assertEqual(self.minute(t + 65 * 60, 0, 61), [])                   # a dip under 5 minutes doesn't end it
        self.assertEqual(self.minute(t + 68 * 60, 0, 61), [])
        (tag, title, text, prio), = self.minute(t + 70 * 60, 0, 60)
        self.assertEqual((tag, title, prio), ("generator_run", "Generator stopped early", "warning"))
        self.assertIn("It ran 1 h 03 min", text)
        self.assertIn("gave 5.1 kWh, using about 0.6 gal ($2.52)", text)        # 64 looks at 4.8 kW; 0.6 gal/h, $4 a gallon
        self.assertIn("Battery 40% at the start, 60% now.", text)
        self.assertIn("about 3.1 kWh more is needed", text)
        self.assertNotIn("Fuel on hand", text)                                  # not tracked
        self.assertEqual(self.minute(t + 71 * 60, 0, 60), [])                   # said once
        # a run that reached "it can be shut down" isn't "early", and data that has gone stale decides nothing
        t += 86400
        stop = dict(level="ok", phase="stop", need_kwh=0.0, run_hours=0.0)
        sent = [x for m in range(0, 30) for x in self.minute(t + m * 60, 4800, 70, **stop)]
        self.assertIn("The battery already has enough", sent[0][2])
        self.assertEqual(self.minute(t + 31 * 60, 0, 80, age=4000), [])
        self.assertEqual(self.minute(t + 40 * 60, 0, 80, age=4000), [])
        (tag, title, text, prio), = self.minute(t + 41 * 60, 0, 80)
        self.assertEqual((title, prio), ("Generator stopped", "info"))

    def test_notices_can_be_turned_off_and_auto_start_has_no_reminders(self):
        self.site(generator="automatic")
        modules.form_apply(self.stack, {"module_generator_shown": "1", "generator": "automatic", "gen_reminders": "1"})
        s = modules.settings(self.stack, "generator")
        self.assertEqual((s["notify_runs"], s["reminders"]), (False, True))
        t = 1_795_000_000
        self.assertEqual([x for m in range(0, 20) for x in self.minute(t + m * 60, 9000 if m < 12 else 0, 50)], [])
        html = modules.form_sections(self.stack)
        self.assertRegex(html, r"name=\"gen_reminders\" value=\"1\"[^>]* checked")
        self.assertNotRegex(html, r"name=\"gen_notify_runs\" value=\"1\"[^>]* checked")

    def test_reminders_follow_the_battery_down(self):
        self.site(generator="manual", generator_gph="0.5")
        t = 1_795_000_000
        sent = []
        for m in range(0, 400, 5):                                              # 52% falling a point every 8 minutes
            adv = dict(level="warn", need_kwh=6.2, run_hours=2.5, empty_at="01:40", empty_in_h=(330 - m) / 60,
                       text="Start the generator now")
            sent += [(m, *x) for x in self.minute(t + m * 60, 0, 52 - m / 8, **adv)]
        self.assertEqual([(m, title) for m, _tag, title, _x, _p in sent],
                         [(80, "Generator still not started"), (160, "Generator still not started"),
                          (210, "Start the generator now"), (290, "Generator still not started"),
                          (370, "Generator still not started")])
        self.assertTrue(all(tag == "generator" and prio == "critical" for _m, tag, _t, _x, prio in sent))
        self.assertIn("The battery has fallen to 42% (it was 52% when the generator was first needed) and reaches 25% about 01:40. "
                      "About 6.2 kWh is needed, about 2 h 30 min of generator.", sent[0][3])
        self.assertIn("The battery is at 26% and reaches 25% in about 2 h (01:40).", sent[2][3])
        # once it runs the reminders stop, and the next need starts again from its own level
        self.assertEqual([x[1] for m in range(400, 420) for x in self.minute(t + m * 60, 4800, 5)], ["Generator started"])
        # turned off on the setup page: none
        modules.form_apply(self.stack, {"module_generator_shown": "1", "generator": "manual", "gen_notify_runs": "1"})
        t += 5 * 86400
        sent = [x for m in range(0, 400, 5) for x in self.minute(t + m * 60, 0, 52 - m / 8, level="warn", empty_in_h=1.0)]
        self.assertEqual([x[1] for x in sent], ["Generator stopped"])           # the run above ending; no reminders

    def test_fuel_on_hand(self):
        self.site(generator="manual", generator_gph="0.5")
        t = 1_795_000_000
        m, fuel = modules.route(self.stack, "POST", "/generator/fuel")
        self.assertIsNone(mod.fuel_left(self.stack))
        self.assertIn("Not tracked yet", mod.page(self.stack).body)
        self.assertEqual(fuel(self.stack, {"fuel_gal": "plenty"}).code, 400)
        import time as _time
        real = _time.time
        _time.time = lambda: t
        try:
            self.assertIn("Fuel on hand recorded.", fuel(self.stack, {"fuel_gal": "5", "fuel_low_h": "3"}).body)
        finally:
            _time.time = real
        self.assertEqual(mod.fuel_left(self.stack)["gal"], 5.0)
        self.log_run(t - 7200, 1.0, 4.5)                                        # before the fill: not counted
        self.log_run(t + 3600, 2.0, 9.0)                                        # 2 h at 0.5 gal/h
        f = mod.fuel_left(self.stack)
        self.assertEqual((f["gal"], f["hours"], f["used"]), (4.0, 8.0, 1.0))
        self.assertIn("About <b>4 gal</b> on hand, about 8 h of running", mod.page(self.stack).body)
        self.assertIn("fuel on hand about 4 gal, about 8 h of running", mod.card(self.stack))
        ha = modules.ha_fields(self.stack, self.site())
        self.assertEqual((ha["generator_fuel_left"], ha["generator_fuel_hours"]), (4.0, 8.0))
        self.assertEqual(self.minute(t + 20000), [])                            # 8 h left, warning under 3
        # a run that straddles the fill counts only for its part after it; the run going now counts too
        self.log_run(t + 30000, 5.5, 25.0)
        (tag, title, text, prio), = self.minute(t + 60000)
        self.assertEqual((tag, title, prio), ("generator_fuel", "Generator fuel is low", "warning"))
        self.assertIn("About 1.2 gal, about 2 h 30 min of running is left (5 gal entered", text)
        self.assertIn("fuel low", mod.card(self.stack))
        self.assertEqual(self.minute(t + 60060), [])                            # said once
        # the reminder says when the fuel on hand is less than the run that is needed
        adv = dict(level="warn", need_kwh=14.0, run_hours=3.0, empty_at="23:10", empty_in_h=1.5)
        self.minute(t + 60120, 0, 50, **adv)
        self.assertEqual(self.minute(t + 60120 + 660, 0, 49, **adv), [])        # Home Assistant has only just said so
        (tag, title, text, prio), = self.minute(t + 60120 + 1860, 0, 49, **adv)
        self.assertEqual(title, "Start the generator now")
        self.assertIn("Only about 1.2 gal, about 2 h 30 min of running is on hand: less than that run.", text)
        # filled up: cleared
        self.assertIn("Fuel on hand recorded.", fuel(self.stack, {"fuel_gal": "10"}).body)
        self.assertEqual([x[:3] for x in self.minute(t + 61000, 0, 49)], [("generator_fuel", "", "clear")])
        self.assertEqual(modules.settings(self.stack, "generator")["fuel_low_h"], 3.0)   # kept
        self.assertIn("no longer tracked", fuel(self.stack, {"stop": "1"}).body)
        self.assertIsNone(modules.ha_fields(self.stack, self.site())["generator_fuel_left"])
        # a config export leaves the count out: it belongs to this box at this moment
        import configfile
        self.assertEqual(configfile.DROP["modules/generator.json"], ("settings.fuel_gal", "settings.fuel_at"))
        # saving the setup page keeps the fuel that was entered
        fuel(self.stack, {"fuel_gal": "7"})
        modules.form_apply(self.stack, {"module_generator_shown": "1", "generator": "manual"})
        self.assertEqual(modules.settings(self.stack, "generator")["fuel_gal"], 7.0)

    def test_fuel_without_a_measured_figure(self):
        """No gallons-per-hour entered: the typical figure for the generator's size, at the load the last run carried."""
        self.site(generator="manual", generator_kw="5.0", generator_gph="")
        st = genlog.load(self.stack)
        self.assertIsNone(genlog.fuel_on_hand(st, self.site(), None, 0))
        first = genlog.fuel_on_hand(st, self.site(), 10, 1000)
        self.assertEqual(first["gal"], 10.0)
        self.assertGreater(first["hours"], 5)
        self.log_run(5000, 2.0, 9.2)
        st = genlog.load(self.stack)
        used = genlog.fuel(st["runs"][0], self.site())
        f = genlog.fuel_on_hand(st, self.site(), 10, 1000)
        self.assertAlmostEqual(f["gal"], round(10 - used, 1))
        self.assertAlmostEqual(f["rate"], round(used / 2, 2))
        st["open"]["charger"] = {"start": 20000, "last_on": 23540, "wh": 4600.0, "peak": 4700.0}   # running now, an hour in
        self.assertLess(genlog.fuel_on_hand(st, self.site(), 10, 1000)["gal"], f["gal"])
        self.site(generator="manual", generator_kw="", generator_gph="")
        self.assertIsNone(genlog.fuel_on_hand(st, self.site(), 10, 1000))            # nothing to work fuel out from
        self.assertIn("fuel use can't be worked out", self._fuel_page(10))

    def _fuel_page(self, gal):
        s = mod._settings(self.stack)
        s.update(fuel_gal=gal, fuel_at=1000)
        modules.save(self.stack, "generator", s)
        return mod.page(self.stack).body

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
