"""Tests for the wall display's screen mode from EDID (display.py).
Run: python3 -m unittest discover -s tests -v"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import display  # noqa: E402


def edid(name="SAMSUNG", w=3840, h=2160, cm=(121, 68)):
    """A minimal EDID base block: header, size, one detailed timing (preferred) and a name descriptor."""
    b = bytearray(128)
    b[0:8] = bytes.fromhex("00ffffffffffff00")
    b[21], b[22] = cm
    dtd = bytearray(18)
    dtd[0], dtd[1] = 0x08, 0xE8                         # pixel clock (non-zero: a timing)
    dtd[2], dtd[4] = w & 0xFF, (w >> 8) << 4
    dtd[5], dtd[7] = h & 0xFF, (h >> 8) << 4
    b[54:72] = dtd
    desc = bytearray(18)
    desc[3] = 0xFC
    desc[5:18] = (name.encode() + b"\n").ljust(13, b" ")
    b[72:90] = desc
    return bytes(b)


class Screen(unittest.TestCase):
    def test_parse_edid(self):
        e = display.parse_edid(edid())
        self.assertEqual((e["name"], e["preferred"], e["width_cm"]), ("SAMSUNG", (3840, 2160), 121))
        self.assertEqual(display.parse_edid(b"\0" * 128), {})

    def test_choose_mode(self):
        tv4k = ["3840x2160", "2560x1440", "1920x1080", "1920x1080i", "1280x720", "720x480"]
        self.assertEqual(display.choose_mode(tv4k, (3840, 2160)), (1920, 1080))
        self.assertEqual(display.choose_mode(["1366x768", "1280x720", "1024x768"]), (1366, 768))
        self.assertEqual(display.choose_mode(["1920x1200", "1920x1080", "1680x1050"], (1920, 1200)), (1920, 1200))
        self.assertEqual(display.choose_mode(["1024x768", "800x600"]), (1024, 768))
        # a 4K monitor without 1080p: the biggest 16:9 mode under the limit
        self.assertEqual(display.choose_mode(["3840x2160", "2560x1440", "1600x900", "1280x1024"]), (1600, 900))
        self.assertIsNone(display.choose_mode([]))

    def test_screens_from_sysfs(self):
        with tempfile.TemporaryDirectory() as drm:
            def card(name, status, modes, e=None):
                d = os.path.join(drm, name)
                os.makedirs(d)
                open(os.path.join(d, "status"), "w").write(status + "\n")
                open(os.path.join(d, "modes"), "w").write("\n".join(modes) + "\n")
                open(os.path.join(d, "edid"), "wb").write(e or b"")
            card("card1-HDMI-A-1", "connected", ["3840x2160", "1920x1080", "1280x720"], edid())
            card("card1-HDMI-A-2", "disconnected", [])
            [s] = display.screens(drm)
            self.assertEqual((s["output"], s["name"], s["mode"], s["preferred"]), ("HDMI-A-1", "SAMSUNG", (1920, 1080), (3840, 2160)))
            old = display.screens
            display.screens = lambda drm=drm: old(drm)
            try:
                self.assertEqual(display.screen_line(), 'SAMSUNG, 55": shown at 1920x1080 (its own best is 3840x2160)')
            finally:
                display.screens = old

    def test_condition_runs_as_root(self):
        """display.json is root's (mode 600): the start check must run with "+" or the kiosk never starts."""
        unit = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "systemd", "solar-display.service")).read()
        cond = [ln for ln in unit.splitlines() if ln.startswith("ExecCondition=")]
        self.assertEqual(len(cond), 1)
        self.assertTrue(cond[0].startswith("ExecCondition=+"), cond[0])
        self.assertIn("User=solar-display", unit)

    def test_should_run_reports_unreadable_settings(self):
        orig = display.load
        def denied(stack):
            raise PermissionError(13, "Permission denied", "/opt/solar-monitor/stack/state/display.json")
        display.load = denied
        try:
            self.assertFalse(display.should_run("/tmp"))
        finally:
            display.load = orig

    def test_clock_format(self):
        import datetime as dt
        t = dt.datetime(2026, 11, 21, 16, 41)
        self.assertEqual(display.hm(t, {"units": "us_customary"}), "4:41 PM")
        self.assertEqual(display.hm(t, {"units": "metric"}), "16:41")
        self.assertEqual(display.hm(t, {"units": "us_customary", "clock": "24"}), "16:41")
        self.assertEqual(display.hm(t.replace(hour=9, minute=5), {"units": "metric", "clock": "12"}), "9:05 AM")

    def test_advice_uses_the_clock(self):
        import datetime as dt
        from test_display import DENVER, SITE
        eve = dt.datetime(2026, 11, 21, 15, 30, tzinfo=DENVER)
        a12 = display.generator_advice({**SITE, "units": "us_customary"}, {"soc": 50, "load_avg_3d": 800}, eve, DENVER)
        a24 = display.generator_advice({**SITE, "units": "us_customary", "clock": "24"}, {"soc": 50, "load_avg_3d": 800}, eve, DENVER)
        self.assertRegex(a12["text"], r"\d:\d\d PM")
        self.assertNotRegex(a24["text"], r"[AP]M")
        self.assertEqual(a12["sunset"], a24["sunset"])                        # machine value for HA stays 24-hour

    def test_hdmi_sound_card(self):
        cards = ["vc4hdmi0", "vc4hdmi1", "Headphones"]
        self.assertIn('"hdmi:CARD=vc4hdmi1,DEV=0"', display.asoundrc(["HDMI-A-2"], cards))
        self.assertIn("card vc4hdmi0", display.asoundrc(["HDMI-A-1"], cards))
        self.assertIsNone(display.asoundrc(["HDMI-A-1"], ["Headphones"]))
        with tempfile.TemporaryDirectory() as home:
            proc = os.path.join(home, "cards")
            open(proc, "w").write(" 0 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0\n 1 [vc4hdmi1       ]: vc4-hdmi - vc4-hdmi-1\n")
            old = display.screens
            display.screens = lambda: [{"output": "HDMI-A-1"}]
            try:
                display.screen_audio(home=home, proc=proc)
            finally:
                display.screens = old
            self.assertIn("CARD=vc4hdmi0", open(os.path.join(home, ".asoundrc")).read())

    def test_page_has_chimes(self):
        for needle in ("CHIMES={doorbell", "vehicle:\"horn\"", "function horn(", "new AudioContext()", "d.test_sound", "soundCfg.quiet&&soundCfg.off",
                       "actx.decodeAudioData", "createBufferSource"):
            self.assertIn(needle, display.PAGE)

    def test_speech_uses_web_audio_not_an_audio_element(self):
        self.assertNotIn("new Audio(", display.PAGE)        # one output stream: the HDMI device doesn't mix

    def test_page_uses_the_scale_unit(self):
        css = display.PAGE.split("<script>")[0]
        self.assertIn("--u:min(1vw,1.7778vh)", css)
        import re
        # only the screen split may use raw viewport units
        raw = [m for m in re.findall(r"[a-z-]+:[^;{}]*\d+v[wh]", css) if not m.startswith(("grid-template", "height:100vh", "--u"))]
        self.assertEqual(raw, [], raw)


if __name__ == "__main__":
    unittest.main()
