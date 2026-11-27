"""The DIN-rail case (esphome/enclosure/boiler-probes-din.scad) against the DIN carrier board it holds
(esphome/carrier/build_board.py, the DIN_* layout): standoffs under the mounting holes, ports, jacks, HEAT, the ESP
board's place, and the case's pillars inside the board's notches. Both are read as source.

The case's sizes are in board coordinates (x along the rail, y down from the board's top edge), so they compare
directly. Run: python3 -m unittest discover -s tests -v"""
import ast
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "esphome")


def board():
    """The board script's module-level constants, worked out in order (they refer to each other)."""
    with open(os.path.join(ROOT, "carrier", "build_board.py")) as f:
        tree = ast.parse(f.read())
    consts = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        try:
            value = eval(compile(ast.Expression(node.value), "build_board.py", "eval"), {"__builtins__": {}}, dict(consts))
        except Exception:
            continue                      # needs KiCad (pcbnew.FromMM and the like): not a size
        t = node.targets[0]
        if isinstance(t, ast.Name):
            consts[t.id] = value
        elif isinstance(t, ast.Tuple):
            for n, v in zip(t.elts, value):
                consts[n.id] = v
    return consts


def scad(name):
    with open(os.path.join(ROOT, "enclosure", "boiler-probes-din.scad")) as f:
        m = re.search(rf"^{name} = (.+?);", f.read(), re.M)
    assert m, name
    return ast.literal_eval(m.group(1))


class DinCaseFitsDinCarrier(unittest.TestCase):
    def setUp(self):
        self.c = board()

    def test_size_and_esp_board(self):
        c = self.c
        self.assertEqual((scad("car_l"), scad("car_w")), (c["DIN_W"], c["DIN_H"]))
        self.assertEqual(scad("esp_x"), c["DIN_ESP_X0"])
        self.assertAlmostEqual(scad("esp_end"), c["DIN_ESP_END"] - c["DIN_H"])
        # the ESP board and the PoE module's overhang (6 mm past its BOOT-side edge) stay on the carrier
        self.assertGreaterEqual(c["DIN_ESP_X0"] - 6, 0)

    def test_standoffs_under_the_mounting_holes(self):
        self.assertEqual(sorted(scad("car_holes")), sorted([x, y] for x, y in self.c["DIN_HOLES"]))

    def test_ports_jacks_and_heat(self):
        c = self.c
        self.assertEqual(scad("port_top_x"), [x for _n, s, x, _g in c["DIN_PORTS"] if s == "top"])
        self.assertEqual(scad("port_bottom_x"), [x for _n, s, x, _g in c["DIN_PORTS"] if s == "bottom"])
        self.assertEqual(scad("heat_x"), c["DIN_HEAT_X"])
        self.assertEqual(scad("ct_jack_x"), [x for _r, _l, x, _g in c["DIN_JACKS"]])

    def test_same_pins_as_the_box_board(self):
        """One firmware for both boards: every port is on the GPIO it has on the box layout."""
        self.assertEqual({n: g for n, _s, _x, g in self.c["DIN_PORTS"]}, {n: g for n, _s, _x, g in self.c["PORTS"]})

    def test_pillars_stand_in_the_notches(self):
        notches = sorted(self.c["DIN_NOTCHES"])
        pillars = sorted(scad("pillars"))
        self.assertEqual(len(pillars), len(notches))
        W, H = self.c["DIN_W"], self.c["DIN_H"]
        for (nx0, ny0, nx1, ny1), (px0, py0, px1, py1), (sx, sy) in zip(notches, pillars, sorted(scad("pillar_screw"))):
            for edge, n, p in ((0, nx0, px0), (0, ny0, py0), (W, nx1, px1), (H, ny1, py1)):
                if n == edge:                       # open side: the pillar runs on into the wall
                    self.assertEqual(p, edge)
                else:                               # 0.5 mm of room to the notch's side
                    self.assertAlmostEqual(abs(p - n), 0.5)
            self.assertTrue(px0 + 2 <= sx <= px1 - 2 and py0 + 2 <= sy <= py1 - 2, (sx, sy))

    def test_nothing_sits_in_a_notch(self):
        """Ports, jacks, HEAT and mounting holes stay clear of the cut-outs."""
        c = self.c
        things = [(x - 7.8, 0, x + 7.8, 12.5) for _n, s, x, _g in c["DIN_PORTS"] if s == "top"]
        things += [(x - 7.8, c["DIN_H"] - 12.5, x + 7.8, c["DIN_H"]) for _n, s, x, _g in c["DIN_PORTS"] if s == "bottom"]
        things += [(c["DIN_HEAT_X"] - 7.8, c["DIN_H"] - 12.5, c["DIN_HEAT_X"] + 7.8, c["DIN_H"])]
        things += [(x - 6, 0, x + 6, 14) for _r, _l, x, _g in c["DIN_JACKS"]]
        things += [(x - 2.8, y - 2.8, x + 2.8, y + 2.8) for x, y in c["DIN_HOLES"]]
        for ax0, ay0, ax1, ay1 in things:
            self.assertTrue(0 <= ax0 and ax1 <= c["DIN_W"] and 0 <= ay0 and ay1 <= c["DIN_H"], (ax0, ay0, ax1, ay1))
            for nx0, ny0, nx1, ny1 in c["DIN_NOTCHES"]:
                self.assertFalse(ax0 < nx1 and nx0 < ax1 and ay0 < ny1 and ny0 < ay1, ((ax0, ay0, ax1, ay1), (nx0, ny0)))


if __name__ == "__main__":
    unittest.main()
