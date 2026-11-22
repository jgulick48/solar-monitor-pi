"""Tests for the cameras module (setup/modules/cameras): turned on for boxes that had cameras, what it adds to the
screen, its fields in the main form, and turning it off.
Run: python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
os.environ.setdefault("SOLAR_COMPOSE", "true")
import display  # noqa: E402
import modules  # noqa: E402
import protect  # noqa: E402
from modules import cameras as mod  # noqa: E402


class CamerasModule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))
        self.compose = []
        self.old = mod._compose
        mod._compose = lambda stack, *a: self.compose.append(" ".join(a))

    def tearDown(self):
        mod._compose = self.old
        self.tmp.cleanup()

    def test_off_on_a_new_box(self):
        self.assertFalse(modules.load(self.stack, "cameras")["enabled"])
        self.assertEqual(self.compose, ["rm -s -f go2rtc"])                      # the old always-on go2rtc stops, once
        modules.load(self.stack, "cameras")
        self.assertEqual(len(self.compose), 1)
        self.assertNotIn("cameras", modules.compose_profiles(self.stack))
        self.assertEqual(display.events(self.stack)["active"], {})

    def test_on_for_a_box_with_cameras(self):
        display.save(self.stack, {**display.DEFAULTS, "cameras": [{"name": "Shed", "url": "rtsp://10.0.0.9/live"}]})
        self.assertTrue(modules.load(self.stack, "cameras")["enabled"])
        self.assertIn("cameras", modules.compose_profiles(self.stack))
        d = modules.display_data(self.stack, {}, None)
        self.assertEqual(d["cameras"], [{"id": "cam1", "name": "Shed", "problem": ""}])
        self.assertEqual(d["sounds"]["doorbell"], True)

    def test_on_for_a_box_with_a_protect_console(self):
        protect.save(self.stack, {**protect.DEFAULTS, "host": "192.168.1.1", "api_key": "k"})
        self.assertTrue(modules.load(self.stack, "cameras")["enabled"])

    def test_turning_off_stops_go2rtc_and_hides_the_cameras(self):
        display.save(self.stack, {**display.DEFAULTS, "cameras": [{"name": "Shed", "url": "rtsp://10.0.0.9/live"}]})
        modules.set_enabled(self.stack, "cameras", False)
        self.assertEqual(self.compose, ["rm -s -f go2rtc"])
        self.assertNotIn("cameras", modules.display_data(self.stack, {}, None))
        self.assertEqual(len(display.load(self.stack)["cameras"]), 1)          # the settings stay
        self.assertNotIn("COMPOSE_PROFILES=cameras", open(os.path.join(self.stack, ".env")).read())
        modules.set_enabled(self.stack, "cameras", True)
        self.assertEqual(self.compose[-1], "up -d go2rtc")
        self.assertIn("cameras", open(os.path.join(self.stack, ".env")).read())

    def test_form_fields(self):
        modules.set_enabled(self.stack, "cameras", True)
        html = modules.form_sections(self.stack)
        self.assertIn("<legend>Cameras</legend>", html)
        self.assertIn("name='sound_doorbell'", html)
        self.assertIn("name='cam1_url'", html)
        with self.assertRaises(modules.FieldError) as e:
            modules.form_check(self.stack, {"module_cameras_shown": "1", "cam1_url": "http://nope/"})
        self.assertEqual(e.exception.field, "cam1_url")
        msgs = modules.form_apply(self.stack, {"module_cameras_shown": "1", "sound_person": "1", "sound_gap": "10",
                                               "cam1_name": "Gate", "cam1_url": "rtsps://10.0.0.5:7441/abc?enableSrtp"})
        self.assertEqual(msgs, ["cameras updated"])                             # go2rtc's config changed
        d = display.load(self.stack)
        self.assertEqual((d["cameras"][0]["name"], d["sounds"]["person"], d["sounds"]["doorbell"], d["sound_gap"]),
                         ("Gate", True, False, 10))
        with open(os.path.join(self.stack, "go2rtc", "go2rtc.yaml")) as f:
            self.assertIn('cam1: "rtspx://10.0.0.5:7441/abc"', f.read())
        self.assertIn("rtsps://10.0.0.5:7441/••••bc", modules.form_sections(self.stack))   # the link is masked

    def test_setup_section_and_upkeep_hook(self):
        modules.set_enabled(self.stack, "cameras", True)
        self.assertIn('<h2 id="protect">', modules.setup_sections(self.stack))
        self.assertIn(3600, mod.MODULE.every)
        self.assertEqual(mod.MODULE.every[3600](self.stack), [])                  # no Protect, no cameras: nothing to do


if __name__ == "__main__":
    unittest.main()
