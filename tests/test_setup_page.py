"""Tests for the setup page's messages: field errors, notices, scrolling to what happened.
Run: python3 -m unittest discover -s tests -v"""
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import solar_web  # noqa: E402


class SetupPage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.tmp.name, "state"))
        self.old, solar_web.STACK = solar_web.STACK, self.tmp.name

    def tearDown(self):
        solar_web.STACK = self.old
        self.tmp.cleanup()

    def form(self, **kw):
        f = {"name": "Test", "time_zone": "America/Denver", "latitude": "39.7", "longitude": "-105", "battery_count": "6",
             "battery_kwh_each": "5.12", "low_soc_alert": "25", "a1_panels": "24", "a1_panel_watts": "285", "a1_tilt": "30",
             "a1_azimuth": "180"}
        f.update(kw)
        return f

    def test_field_errors_name_the_field(self):
        for kw, field, text in [({"latitude": ""}, "latitude", "Latitude is needed"),
                                ({"latitude": "abc"}, "latitude", "'abc' is not a number"),
                                ({"battery_count": "99"}, "battery_count", "Number of batteries must be between 1 and 64"),
                                ({"a1_panel_watts": "5"}, "a1_panel_watts", "Array 1: watts per panel"),
                                ({"time_zone": "Mars/Base"}, "time_zone", "unknown time zone")]:
            with self.assertRaises(solar_web.FieldError) as cm:
                solar_web.validate(self.form(**kw))
            self.assertEqual(cm.exception.field, field)
            self.assertIn(text, str(cm.exception))

    def test_page_marks_the_field_and_scrolls_to_it(self):
        site = {**solar_web.load_site(), "latitude": ""}
        page = solar_web.setup_page(site, "Latitude is needed", error_field="latitude")
        self.assertIn("id='err-field' class='bad' aria-invalid='true' name='latitude'", page)
        self.assertIn("<span class='fielderr'>Latitude is needed</span>", page)
        self.assertIn("class='toast err'", page)
        self.assertIn('getElementById("err-field")', page)

    def test_section_messages(self):
        page = solar_web.setup_page(solar_web.load_site(), module_msgs={"tunnel": "Not saved: the account ID is the 32 letters."})
        self.assertIn("class='toast err'", page)
        self.assertIn("id=notice class='card notice err'", page)       # in the Remote access section
        self.assertIn('getElementById("notice")', page)
        page = solar_web.setup_page(solar_web.load_site(), module_msgs={"cameras": "connected: 4 camera(s)."})
        self.assertIn("class='toast ok'", page)
        self.assertTrue(solar_web.is_error("2 camera(s) on the screen; camera service restart failed: x"))
        self.assertFalse(solar_web.is_error("remote access on at https://solar.example.com"))

    def test_saved(self):
        page = solar_web.setup_page(solar_web.load_site(), done=["nothing changed"])
        self.assertIn("class='toast ok'", page)
        self.assertIn("Saved: nothing changed.", page)
        self.assertNotIn("getElementById", page)                        # a save stays where it is (the banner is pinned)

    def test_navigation_header(self):
        page = solar_web.page("Battery health", "<p>x</p>")
        self.assertIn("<header class='nav'>", page)
        self.assertIn("<a href='/battery' class=on aria-current=page>Battery</a>", page)
        self.assertEqual(page.count("class=on"), 1)
        for href in ("/", "/setup", "/generator", "/backup", "/advisor", "/display"):
            self.assertIn(f"<a href='{href}'", page)
        self.assertIn("data-port='8123'", page)
        self.assertIn("data-port='3000'", page)
        self.assertNotIn("class=on", solar_web.page("Not found", ""))

    def test_section_list_down_the_side(self):
        """Every section of the setup page is in the list beside it, in page order, and each link has its target."""
        page = solar_web.setup_page(solar_web.load_site())
        nav = re.search(r"<nav class='sidenav'.*?</nav>", page, re.S).group(0)
        links = re.findall(r"<a href='#([\w-]+)'( class=sep)?>([^<]+)</a>", nav)
        names = [n for _h, _c, n in links]
        self.assertEqual(names[:3], ["Optional features", "Location", "Batteries"])
        self.assertIn("Prices", names)                                  # not "Prices (optional, for the advisor's ...)"
        self.assertIn("Second array", names)
        self.assertEqual(names[-1], "SSH access")
        self.assertLess(nav.index("Saved together"), nav.index("Saved separately"))
        self.assertTrue(all(sep for h, sep, n in links if n in ("Remote access", "Remote support", "SSH access")))
        self.assertFalse(any(sep for h, sep, n in links if n in ("Location", "Updates", "Generator")))
        hrefs = [h for h, _c, _n in links]
        self.assertEqual(len(hrefs), len(set(hrefs)))
        for h in hrefs:
            self.assertEqual(len(re.findall(rf"""\bid=["']?{re.escape(h)}["' >]""", page)), 1, h)
        # a main-form section's target is its fieldset, a separately saved one's is its heading
        self.assertIn('<fieldset id="sec-location"><legend>Location</legend>', page)
        self.assertRegex(page, r"<section class='own'><h2 id=\"remote\">")
        # fieldsets inside another section are not sections; ids that were there are kept
        self.assertEqual(page.count('id="screen"'), 1)
        self.assertNotIn("Sound check", nav)
        # the list sits beside the page, after its heading; the form and its save bar are inside the main column
        self.assertRegex(page, r"<h1>Site setup</h1><div class='setup-layout'><nav class='sidenav'")
        self.assertLess(page.index("class='setup-main'"), page.index('id="site-form"'))
        self.assertIn(".sidenav a')", page)                             # the script that follows the scroll position

    def test_sections_of_a_page_fragment(self):
        body = ('<h1>x</h1><form id="site-form"><fieldset id=\'features\'><legend>Optional features</legend></fieldset>'
                '<fieldset><legend>Screen &amp; sound (optional, if fitted)</legend><fieldset><legend>Inner</legend></fieldset></fieldset>'
                '<fieldset><legend>Screen &amp; sound</legend></fieldset><div class="savebar"></div></form>'
                "<section class='own'><h2 id=\"probeboard\">Boiler probe board</h2><form><fieldset><legend>Firmware</legend></fieldset></form></section>")
        out, secs = solar_web.setup_sections(body)
        self.assertEqual(secs, [("form", "features", "Optional features"), ("form", "sec-screen-sound", "Screen & sound"),
                                ("form", "sec-screen-sound-2", "Screen & sound"), ("own", "probeboard", "Boiler probe board")])
        self.assertIn('<fieldset id="sec-screen-sound"><legend>Screen &amp; sound (optional, if fitted)</legend><fieldset><legend>Inner', out)
        self.assertEqual(solar_web.setup_sections("<p>no form</p>"), ("<p>no form</p>", []))
        self.assertEqual(solar_web.setup_sidenav([]), "")
        self.assertIn("<a href='#sec-screen-sound'>Screen &amp; sound</a>", solar_web.setup_sidenav(secs))

    def test_mark_select(self):
        body = "<label>Time zone <select name='time_zone'><option>A</option></select></label>"
        out = solar_web.mark_field(body, "time_zone", "unknown time zone")
        self.assertTrue(out.endswith("</select><span class='fielderr'>unknown time zone</span></label>"))

    def test_forecast_settings_shown_with_saved_values(self):
        page = solar_web.setup_page(solar_web.load_site())
        self.assertIn("<option value='7' selected>7 days</option>", page)          # defaults
        self.assertIn("<option value='8' selected>8 seconds</option>", page)
        solar_web.modules.save(self.tmp.name, "weather", {"days": 4, "page": 0})
        page = solar_web.setup_page(solar_web.load_site())
        self.assertIn("<option value='4' selected>4 days</option>", page)
        self.assertIn("<option value='0' selected>never (show only what fits)</option>", page)


    def test_feature_switches(self):
        page = solar_web.setup_page(solar_web.load_site())
        for mid in ("backup", "generator", "notifications", "tunnel", "support", "ssh", "weather"):
            self.assertIn(f"name='module_{mid}' value='1' style='width:auto' checked>", page)
        self.assertLess(page.index("<legend>Optional features</legend>"), page.index("<legend>Location</legend>"))
        self.assertIn("<legend>Weather on the screen</legend>", page)
        self.assertIn('<h2 id="support">', page)
        self.assertNotIn("display_weather", page)                       # the old checkbox is the switch now
        # switch weather and remote support off: their parts go; the others stay
        f = {"modules_form": "1", "module_backup": "1", "module_generator": "1", "module_tunnel": "1", "module_ssh": "1", "module_notifications": "1", "module_weather_shown": "1",
             "wx_days": "3", "wx_page": "12"}
        msgs = solar_web.modules.form_apply(self.tmp.name, f)
        self.assertEqual(msgs, ["remote support off", "weather off"])
        page = solar_web.setup_page(solar_web.load_site())
        self.assertNotIn("<legend>Weather on the screen</legend>", page)
        self.assertNotIn('<h2 id="support">', page)
        self.assertIn("name='module_weather' value='1' style='width:auto'>", page)   # its switch is still there, off
        self.assertEqual(solar_web.modules.settings(self.tmp.name, "weather")["days"], 7)  # turned off: fields not saved
        # back on: the fields come back with what was saved before
        f = {"modules_form": "1", "module_backup": "1", "module_generator": "1", "module_tunnel": "1", "module_support": "1", "module_ssh": "1", "module_notifications": "1",
             "module_weather": "1"}
        self.assertEqual(solar_web.modules.form_apply(self.tmp.name, f), ["remote support on", "weather on"])
        f.update(module_weather_shown="1", wx_days="3", wx_page="12")
        self.assertEqual(solar_web.modules.form_apply(self.tmp.name, f), [])
        self.assertEqual(solar_web.modules.settings(self.tmp.name, "weather"), {"days": 3, "page": 12})

    def test_a_form_without_switches_changes_none(self):
        self.assertEqual(solar_web.modules.form_apply(self.tmp.name, {"name": "x"}), [])
        self.assertTrue(solar_web.modules.load(self.tmp.name, "weather")["enabled"])


if __name__ == "__main__":
    unittest.main()


class SavingIsClear(unittest.TestCase):
    """What the main Save covers, what saves on its own, and buttons that act without throwing edits away."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.tmp.name, "state"))
        self.old, solar_web.STACK = solar_web.STACK, self.tmp.name
        os.environ["SOLAR_COMPOSE"] = "true"            # modules that start a container: compose succeeds and does nothing
        self.old_run, solar_web.run_quietly = solar_web.run_quietly, lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")

    def tearDown(self):
        solar_web.run_quietly = self.old_run
        os.environ.pop("SOLAR_COMPOSE", None)
        solar_web.STACK = self.old
        solar_web.modules._ALL = None
        self.tmp.cleanup()

    def page(self):
        return solar_web.setup_page(solar_web.load_site())

    def test_one_save_bar_for_the_main_form(self):
        page = self.page()
        start, end = page.index("<form method=\"post\" action=\"/setup\" id=\"site-form\">"), page.index("</form>", page.index("id=\"savebar\""))
        main = page[start:end]
        self.assertIn("<div class=\"savebar\" id=\"savebar\"><button>Save site settings</button>", main)
        self.assertEqual(page.count("Save site settings</button>"), 1)
        self.assertNotIn("<button>Save</button>", page)                  # every save button says what it saves
        self.assertIn("press Save site settings to turn it off", main)  # beside each module switch
        # the sections with their own forms come after the main form, under their own heading, each in a box
        own = page[end:]
        self.assertIn("<h2>Saved separately</h2>", own)
        for h2 in ('<h2 id="support">', '<h2 id="ssh">', '<h2 id="remote">'):
            self.assertNotIn(h2, main)
            self.assertIn("<section class='own'>" + h2, own)
        self.assertIn("Save site settings</b> doesn't include them", own)

    def test_action_buttons_answer_without_a_new_page(self):
        solar_web.modules.set_enabled(self.tmp.name, "voice", True)
        page = self.page()
        main = page[page.index("id=\"site-form\""):page.index("id=\"savebar\"")]
        buttons = []
        for tag in re.findall(r"<button formaction=[^>]*>", main):
            nv = re.search(r"name='(\w+)' value='(\w+)'", tag)
            buttons.append((re.search(r"formaction=['\"]([^'\"]+)", tag).group(1), *(nv.groups() if nv else ("", ""))))
        actions = {b[0] for b in buttons}
        self.assertIn(("/notifications/test", "channel", "wall"), buttons)
        for a in ("/display/test-sound", "/display/restart", "/display/test-power", "/voice/test", "/notifications/test"):
            self.assertIn(a, actions)
        self.assertIn("button[formaction]", page)                        # the page's script handles them
        web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), solar_web.Handler)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        try:
            for url, name, value in buttons:
                data = urllib.parse.urlencode({"name": "Edited but not saved", **({name: value} if name else {})}).encode()
                req = urllib.request.Request(f"http://127.0.0.1:{web.server_port}{url}", data=data, method="POST",
                                             headers={"X-Solar-Action": "1"})
                try:
                    r = urllib.request.urlopen(req)
                except urllib.error.HTTPError as e:
                    r = e
                body = r.read().decode()
                self.assertEqual(r.headers["Content-Type"], "application/json", url)
                d = json.loads(body)
                self.assertTrue(d["msg"] and d["msg"][0].isupper(), (url, d))
                self.assertIsInstance(d["ok"], bool)
                r.close()
            ok = json.loads(urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{web.server_port}/voice/test", data=b"", method="POST", headers={"X-Solar-Action": "1"})).read())
            self.assertEqual(ok, {"ok": True, "msg": "Test announcement sent to the screen.", "set": {}})
            no = json.loads(urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{web.server_port}/notifications/test", data=b"channel=ha_app", method="POST",
                headers={"X-Solar-Action": "1"})).read())
            self.assertFalse(no["ok"])                                    # "Not sent: no phone is chosen ..."
            # nothing above saved the edited site name
            self.assertNotEqual(solar_web.load_site()["name"], "Edited but not saved")
            # without the script (no header) the old way still works, and says Done, not Saved
            html_page = urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{web.server_port}/display/test-sound", data=b"", method="POST")).read().decode()
            self.assertIn("Done: test sound sent", html_page)
            self.assertNotIn("Saved: test sound", html_page)
        finally:
            web.shutdown()
            web.server_close()
