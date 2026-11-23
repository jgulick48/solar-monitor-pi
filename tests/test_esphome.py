"""Checks on the probe board's ESPHome configuration (esphome/): the entity names the boiler module reads, the
placeholder secrets, and that real secrets stay out of git. ESPHome itself validates the file (esphome config).
Run: python3 -m unittest discover -s tests -v"""
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "esphome")
PROBES = ("Floor supply", "Floor return", "Boiler supply", "Boiler return", "Hot water outlet", "Slab")


class ESPHome(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "boiler-probes.yaml")) as f:
            self.cfg = f.read()

    def test_probe_names_and_addresses(self):
        live = "\n".join(line for line in self.cfg.splitlines() if not line.lstrip().startswith("#"))
        for name in PROBES:
            self.assertIn(f"name: {name}\n", live)
        addrs = re.findall(r"^  (addr_\w+): ", self.cfg, re.M)
        self.assertEqual(len(addrs), len(PROBES))
        for a in addrs:
            self.assertIn("${" + a + "}", live)
        self.assertIn("name: boiler-probes", self.cfg)                       # entity ids: sensor.boiler_probes_...

    def test_secrets_stay_out_of_git(self):
        with open(os.path.join(ROOT, ".gitignore")) as f:
            self.assertIn("secrets.yaml", f.read().split())
        with open(os.path.join(ROOT, "secrets.example.yaml")) as f:
            ex = f.read()
        self.assertIn('api_encryption_key: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="', ex)   # an obvious placeholder
        self.assertFalse(os.path.exists(os.path.join(ROOT, "secrets.yaml")))


if __name__ == "__main__":
    unittest.main()
