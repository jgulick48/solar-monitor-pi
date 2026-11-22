"""Tests for the voice module (setup/modules/voice): moving the settings over, the announcement service, turning it
on and off, and the display speaking an announcement through /display/say.
Run: python3 -m unittest discover -s tests -v"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
os.environ.setdefault("SOLAR_COMPOSE", "true")
import modules  # noqa: E402
from modules import voice  # noqa: E402


class Voice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))
        self.started, self.stopped = [], []
        self.old = voice.start, voice.stop, voice.MODULE
        voice.start = lambda stack, changed: self.started.append(changed)
        voice.stop = lambda stack: self.stopped.append(1)
        # the manifest holds the functions themselves: point it at the fakes too
        import dataclasses
        m = dataclasses.replace(voice.MODULE, on_enable=lambda s: voice.start(s, True), on_disable=lambda s: voice.stop(s))
        modules._ALL = [x if x.id != "voice" else m for x in modules.all_modules()]

    def tearDown(self):
        voice.start, voice.stop, _ = self.old
        modules._ALL = None
        self.tmp.cleanup()

    def display_json(self, d):
        with open(os.path.join(self.stack, "state", "display.json"), "w") as f:
            json.dump(d, f)

    def test_off_on_a_new_box(self):
        self.assertFalse(modules.load(self.stack, "voice")["enabled"])
        self.assertIsNone(voice.announce(self.stack, "hello"))

    def test_settings_move_over(self):
        self.display_json({"speak": True, "voice": "en_US-amy-medium", "speak_mode": "only"})
        self.assertTrue(modules.load(self.stack, "voice")["enabled"])
        self.assertEqual(modules.settings(self.stack, "voice"), {"voice": "en_US-amy-medium", "mode": "only"})
        self.assertEqual(modules.display_data(self.stack, {}, None).get("speak_mode"), "only")

    def test_announcement_queue(self):
        modules.set_enabled(self.stack, "voice", True)
        a = voice.announce(self.stack, "  The water   tank is low ", chime="doorbell", now=1000)
        b = voice.announce(self.stack, "Generator can be shut down", chime="nonsense", now=1000)
        self.assertGreater(b, a)                                      # same moment: still in order
        self.assertEqual(voice.pending(self.stack, now=1001), [{"id": a, "chime": "doorbell"}, {"id": b, "chime": None}])
        self.assertEqual(voice.text_of(self.stack, a), "The water tank is low")
        self.assertEqual(voice.pending(self.stack, now=1000 + voice.FRESH_S + 1), [])   # a backlog isn't read out
        self.assertEqual(len(voice.text_of(self.stack, voice.announce(self.stack, "x" * 999, now=1002))), 300)

    def test_switch_starts_and_stops_the_service(self):
        modules.set_enabled(self.stack, "voice", True)
        self.assertEqual(self.started, [True])
        env = open(os.path.join(self.stack, ".env")).read()
        self.assertIn("COMPOSE_PROFILES=tts\n", env)
        self.assertIn("TTS_VOICE=en_US-lessac-medium\n", env)
        modules.set_enabled(self.stack, "voice", False)
        self.assertEqual(self.stopped, [1])
        self.assertNotIn("tts", open(os.path.join(self.stack, ".env")).read())
        self.assertNotIn("speak", modules.display_data(self.stack, {}, None))

    def test_display_speaks_an_announcement(self):
        import display
        import solar_web
        import tts
        solar_web.STACK = self.stack
        old_wav = tts.wav
        tts.wav = lambda stack, text, v: f"RIFF {text} / {v}".encode()
        web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), solar_web.Handler)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{web.server_port}"
        try:
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "/display/say?id=1")
            self.assertEqual(e.exception.code, 503)                   # voice off
            e.exception.close()
            modules.set_enabled(self.stack, "voice", True)
            r = urllib.request.urlopen(urllib.request.Request(base + "/voice/test", data=b"", method="POST")).read().decode()
            self.assertIn("test announcement sent to the screen", r)
            q = voice.pending(self.stack)
            self.assertEqual(len(q), 1)
            self.assertEqual(urllib.request.urlopen(base + f"/display/say?id={q[0]['id']}").read(),
                             f"RIFF {voice.TEST_TEXT} / en_US-lessac-medium".encode())
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "/display/say?id=12345")
            self.assertEqual(e.exception.code, 404)
            e.exception.close()
            self.assertEqual(json.loads(urllib.request.urlopen(base + "/speech/progress").read())["active"], False)
            self.assertIn('"/display/say?id="+a.id', display.PAGE)
        finally:
            tts.wav = old_wav
            web.shutdown()
            web.server_close()

    def test_setup_page(self):
        import solar_web
        solar_web.STACK = self.stack
        page = solar_web.setup_page(solar_web.load_site())
        self.assertNotIn("<legend>Voice announcements</legend>", page)
        self.assertNotIn("name='speak_template'", page)               # the camera wording shows only with voice on
        self.assertIn("name='module_voice' value='1' style='width:auto'>", page)
        modules.set_enabled(self.stack, "voice", True)
        page = solar_web.setup_page(solar_web.load_site())
        self.assertIn("<legend>Voice announcements</legend>", page)
        self.assertNotIn("name='speak_template'", page)               # ... and with cameras: it's the cameras' wording
        modules.set_enabled(self.stack, "cameras", True)
        page = solar_web.setup_page(solar_web.load_site())
        self.assertIn("name='speak_template'", page)
        self.assertIn('formaction="/voice/test"', page)
        # saving the camera settings without the wording on the page keeps it
        import display
        display.save(self.stack, {**display.load(self.stack), "speak_template": "{object} by the {camera}"})
        modules.set_enabled(self.stack, "voice", False)
        orig_run = solar_web.subprocess.run
        solar_web.subprocess.run = lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "", "")   # no compose/systemctl
        try:
            solar_web.apply_display({"display_enabled": "1"})
        finally:
            solar_web.subprocess.run = orig_run
        self.assertEqual(display.load(self.stack)["speak_template"], "{object} by the {camera}")


if __name__ == "__main__":
    unittest.main()
