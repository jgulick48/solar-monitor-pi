"""The enclosure (esphome/enclosure/boiler-probes-box.scad) against the carrier board it holds
(esphome/carrier/build_board.py): the standoffs under the mounting holes, the port windows, the jacks' holes, HEAT and
the ESP board's place. Both are read as source (OpenSCAD and KiCad's Python aren't needed).

The board's y runs from its back (BOOT) edge; the case's carrier y from its front (RESET) edge: y_case = 60 - y_board.
Run: python3 -m unittest discover -s tests -v"""
import ast
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "esphome")


def board():
    with open(os.path.join(ROOT, "carrier", "build_board.py")) as f:
        src = f.read()
    consts = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    try:
                        consts[t.id] = ast.literal_eval(node.value)
                    except ValueError:
                        pass
                elif isinstance(t, ast.Tuple) and isinstance(node.value, ast.Tuple):
                    for n, v in zip(t.elts, node.value.elts):
                        try:
                            consts[n.id] = ast.literal_eval(v)
                        except ValueError:
                            pass
    return src, consts


def scad(name):
    with open(os.path.join(ROOT, "enclosure", "boiler-probes-box.scad")) as f:
        m = re.search(rf"^{name} = (.+?);", f.read(), re.M)
    assert m, name
    return ast.literal_eval(m.group(1))


class EnclosureFitsCarrier(unittest.TestCase):
    def setUp(self):
        self.src, self.c = board()
        self.H = self.c["H"]

    def test_size_and_esp_board(self):
        self.assertEqual((scad("car_l"), scad("car_w")), (self.c["W"], self.H))
        self.assertEqual(scad("esp_x"), self.c["ESP_X0"])
        self.assertAlmostEqual(scad("esp_y"), self.H - (self.c["ESP_Y0"] + self.c["ESP_W"]))   # its front edge

    def test_standoffs_under_the_mounting_holes(self):
        want = sorted([x, round(self.H - y, 3)] for x, y in self.c["HOLES"])
        self.assertEqual(sorted(scad("car_holes")), want)

    def test_port_windows(self):
        self.assertEqual(scad("port_x"), sorted({x for _n, _s, x, _g in self.c["PORTS"]}))

    def test_jacks_and_heat(self):
        box = self.src[self.src.index("def build_box"):]          # the DIN layout has its own jacks (DIN_JACKS)
        jacks = re.findall(r'\("(J1[26])", "(CT[12])", ([\d.]+), "', box)
        self.assertEqual(scad("ct_jack_y"), [self.H - float(y) for _r, _l, y in sorted(jacks, key=lambda j: j[1])])
        pin1 = float(re.search(r'self\.terminal\("J11", "HEAT", [\d.]+, ([\d.]+),', box).group(1))
        self.assertEqual([round(v, 2) for v in scad("heat_y")], [round(self.H - pin1, 2), round(self.H - pin1 + 2 * 5.08, 2)])


if __name__ == "__main__":
    unittest.main()
