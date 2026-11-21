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

    def test_page_uses_the_scale_unit(self):
        css = display.PAGE.split("<script>")[0]
        self.assertIn("--u:min(1vw,1.7778vh)", css)
        import re
        # only the screen split may use raw viewport units
        raw = [m for m in re.findall(r"[a-z-]+:[^;{}]*\d+v[wh]", css) if not m.startswith(("grid-template", "height:100vh", "--u"))]
        self.assertEqual(raw, [], raw)


if __name__ == "__main__":
    unittest.main()
