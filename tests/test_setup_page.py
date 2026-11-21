"""Tests for the setup page's messages: field errors, notices, scrolling to what happened.
Run: python3 -m unittest discover -s tests -v"""
import os
import sys
import tempfile
import unittest

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
        page = solar_web.setup_page(solar_web.load_site(), remote_msg="Not saved: the account ID is the 32 letters.")
        self.assertIn("class='toast err'", page)
        self.assertIn("id=notice class='card notice err'", page)       # in the Remote access section
        self.assertIn('getElementById("notice")', page)
        page = solar_web.setup_page(solar_web.load_site(), protect_msg="connected: 4 camera(s).")
        self.assertIn("class='toast ok'", page)
        self.assertTrue(solar_web.is_error("2 camera(s) on the screen; camera service restart failed: x"))
        self.assertFalse(solar_web.is_error("remote access on at https://solar.example.com"))

    def test_saved(self):
        page = solar_web.setup_page(solar_web.load_site(), done=["nothing changed"])
        self.assertIn("class='toast ok'", page)
        self.assertIn("Saved: nothing changed.", page)
        self.assertNotIn("getElementById", page)                        # a save stays where it is (the banner is pinned)

    def test_mark_select(self):
        body = "<label>Time zone <select name='time_zone'><option>A</option></select></label>"
        out = solar_web.mark_field(body, "time_zone", "unknown time zone")
        self.assertTrue(out.endswith("</select><span class='fielderr'>unknown time zone</span></label>"))


if __name__ == "__main__":
    unittest.main()
