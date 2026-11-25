"""Checks on the probe board's ESPHome configuration (esphome/): each probe port is its own 1-Wire bus on the GPIO
the carrier board wires it to, the other inputs match the board too, the placeholder secrets, and that real secrets
stay out of git. ESPHome itself validates and compiles the file (esphome config / compile).
Run: python3 -m unittest discover -s tests -v"""
import ast
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "esphome")
REQUIRED = ("Floor supply", "Floor return", "Boiler supply")
# the probe on each port, as the config and README name them
PROBES = {"P1": "Floor supply", "P2": "Floor return", "P3": "Boiler supply", "P4": "Boiler return",
          "P5": "Hot water outlet", "P6": "Slab near boiler", "P7": "Slab far", "P8": "Probe P8"}


def carrier_ports():
    """PORTS from carrier/build_board.py (read as source: it needs KiCad's Python to import): {port: GPIO number}."""
    with open(os.path.join(ROOT, "carrier", "build_board.py")) as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "PORTS" for t in node.targets):
            return {name: int(gpio[4:]) for name, _side, _x, gpio in ast.literal_eval(node.value)}
    raise AssertionError("PORTS not found in build_board.py")


class ESPHome(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "boiler-probes.yaml")) as f:
            self.cfg = f.read()
        self.live = "\n".join(line for line in self.cfg.splitlines() if not line.lstrip().startswith("#"))

    def test_one_bus_per_port_on_the_carriers_gpios(self):
        buses = {m[1].upper(): int(m[0]) for m in re.findall(r"pin: GPIO(\d+), id: (p\d)", self.live)}
        self.assertEqual(buses, carrier_ports())                     # P1-P8, each on the GPIO the board wires it to
        for port, name in PROBES.items():
            # each port named by its substitution (the setup page builds it with the box's own names), defaulting to
            # the probe the README lists
            block = re.search(rf"one_wire_id: {port.lower()}\n\s+name: \$\{{{port.lower()}_name\}}\n", self.live)
            self.assertTrue(block, f"{port}: {name}")
            self.assertIn(f"  {port.lower()}_name: {name}\n", self.cfg)
        for block in self.live.split("- platform: dallas_temp")[1:]:  # one probe per bus: no addresses to find
            self.assertNotIn("address:", block.split("- platform:")[0])
        self.assertEqual(self.live.count("platform: dallas_temp"), 8)
        with open(os.path.join(ROOT, "README.md")) as f:
            readme = f.read()
        for port, name in PROBES.items():
            row = f"| {port} | {carrier_ports()[port]} | {name} |"
            self.assertIn(row, readme)
        for name in REQUIRED:
            self.assertRegex(readme, rf"\| {name} \| \*\*Required\*\* \|")

    def test_other_inputs_match_the_carrier(self):
        with open(os.path.join(ROOT, "carrier", "build_board.py")) as f:
            board = f.read()
        for net, pin in (("HEAT_GPIO46", "number: GPIO46"), ("CT1_GPIO16", "pin: GPIO16"), ("CT2_GPIO17", "pin: GPIO17"),
                         ("SDA_GPIO7", "sda: GPIO7"), ("SCL_GPIO8", "scl: GPIO8"), ("BTN_GPIO47", "number: GPIO47")):
            self.assertIn(net, board)
            self.assertIn(pin, self.live)
        self.assertIn("platform: ssd1306_i2c", self.live)
        self.assertIn("name: boiler-probes", self.cfg)

    def test_secrets_stay_out_of_git(self):
        with open(os.path.join(ROOT, ".gitignore")) as f:
            self.assertIn("secrets.yaml", f.read().split())
        with open(os.path.join(ROOT, "secrets.example.yaml")) as f:
            ex = f.read()
        self.assertIn('api_encryption_key: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="', ex)   # an obvious placeholder
        self.assertFalse(os.path.exists(os.path.join(ROOT, "secrets.yaml")))


if __name__ == "__main__":
    unittest.main()
