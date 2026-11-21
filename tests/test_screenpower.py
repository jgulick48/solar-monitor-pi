"""Tests for when the screen is on (screenpower.py).
Run: python3 -m unittest discover -s tests -v"""
import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import screenpower as sp  # noqa: E402

# 2026-11-20 is a Friday
at = lambda day, h, m=0: dt.datetime(2026, 11, day, h, m)  # noqa: E731
T = 1_795_300_000.0


class Rules(unittest.TestCase):
    def test_days_and_past_midnight(self):
        rules = [{"days": "weekdays", "on": "06:30", "off": "22:00"}, {"days": "weekends", "on": "08:00", "off": "01:00"}]
        self.assertFalse(sp.in_hours(rules, at(20, 6, 29)))
        self.assertTrue(sp.in_hours(rules, at(20, 6, 30)))
        self.assertFalse(sp.in_hours(rules, at(20, 23)))          # Friday night: the weekday rule ended
        self.assertFalse(sp.in_hours(rules, at(21, 0, 30)))       # Sat 00:30: Friday isn't a weekend day
        self.assertFalse(sp.in_hours(rules, at(21, 7)))
        self.assertTrue(sp.in_hours(rules, at(21, 23)))           # Saturday evening
        self.assertTrue(sp.in_hours(rules, at(22, 0, 59)))        # Sunday 00:59, from Saturday's rule
        self.assertTrue(sp.in_hours(rules, at(23, 0, 30)))        # Monday 00:30, from Sunday's rule
        self.assertFalse(sp.in_hours(rules, at(23, 1)))
        self.assertEqual(sp.next_change(rules, at(20, 12)), at(20, 22))
        self.assertEqual(sp.next_change(rules, at(20, 22, 30)), at(21, 8))
        self.assertIsNone(sp.next_change([], at(20, 12)))

    def test_clean(self):
        self.assertEqual(sp.clean_rules([("weekdays", "06:00", "22:00"), ("daily", "", ""), ("bogus", "07:00", "08:00")]),
                         [{"days": "weekdays", "on": "06:00", "off": "22:00"}, {"days": "daily", "on": "07:00", "off": "08:00"}])
        for bad in ([("daily", "06:00", "")], [("daily", "06:00", "06:00")]):
            with self.assertRaises(ValueError):
                sp.clean_rules(bad)
        self.assertEqual(sp.clean_entities(" Binary_Sensor.Den_Motion,person.ann\nperson.ann "),
                         ["binary_sensor.den_motion", "person.ann"])
        for bad in ("light.kitchen", "binary_sensor.x; rm", "binary_sensor."):
            with self.assertRaises(ValueError):
                sp.clean_entities(bad)

    def test_old_off_hours_become_a_rule(self):
        d = sp.migrate({"off_from": "22:00", "off_to": "06:00"})
        self.assertEqual((d["power_mode"], d["hours"]), ("hours", [{"days": "daily", "on": "06:00", "off": "22:00"}]))
        self.assertEqual(sp.migrate({})["power_mode"], "always")
        self.assertEqual(sp.migrate({"power_mode": "always", "off_from": "22:00", "off_to": "06:00"})["power_mode"], "always")


class Wanted(unittest.TestCase):
    def setUp(self):
        sp.PRESENCE.update(occupied=None, at=0.0, last_on=0.0)
        sp.WAKE.update(until=0.0, why="")
        sp.TEST["until"] = 0.0

    def test_hours_and_wake(self):
        d = {**sp.DEFAULTS, "power_mode": "hours", "hours": [{"days": "daily", "on": "06:00", "off": "22:00"}],
             "wake": {"doorbell": True, "person": False}, "wake_minutes": 3}
        self.assertEqual(sp.wanted(d, at(20, 12), T), (True, "on-hours"))
        self.assertEqual(sp.wanted(d, at(20, 23), T), (False, "outside the on-hours"))
        sp.note_events(d, {"cam1": {"label": "Person"}}, {"cam1": "Driveway"}, T)          # not a wake kind
        self.assertFalse(sp.wanted(d, at(20, 23), T)[0])
        sp.note_events(d, {"cam2": {"label": "Doorbell"}}, {"cam2": "Front door"}, T)
        self.assertEqual(sp.wanted(d, at(20, 23), T + 60), (True, "woken by a camera: Doorbell at the Front door"))
        self.assertFalse(sp.wanted(d, at(20, 23), T + 181)[0])
        self.assertEqual(sp.wanted({**d, "enabled": False}, at(20, 12), T)[0], False)
        sp.TEST["until"] = T + 15
        self.assertEqual(sp.wanted(d, at(20, 12), T)[0], False)

    def test_occupancy(self):
        d = {**sp.DEFAULTS, "power_mode": "hours_occupied", "hours": [{"days": "daily", "on": "06:00", "off": "22:00"}],
             "occupancy": ["binary_sensor.den"], "occupancy_hold": 10}
        self.assertEqual(sp.wanted(d, at(20, 12), T), (True, "no word from Home Assistant's sensors"))
        sp.report_presence(True, T)
        self.assertEqual(sp.wanted(d, at(20, 12), T + 1), (True, "on-hours and someone's in the room"))
        self.assertEqual(sp.wanted(d, at(20, 23), T + 1), (False, "outside the on-hours"))
        sp.report_presence(False, T + 120)
        self.assertTrue(sp.wanted(d, at(20, 12), T + 500)[0])                              # within the 10 minutes
        self.assertEqual(sp.wanted(d, at(20, 12), T + 601), (False, "nobody in the room"))
        self.assertTrue(sp.wanted(d, at(20, 12), T + 120 + sp.STALE_S + 1)[0])              # HA silent: stays on

    def test_ha_package(self):
        d = {"power_mode": "occupied", "occupancy": ["binary_sensor.den", "person.ann"]}
        pkg = sp.ha_package(d)
        line = next(ln for ln in pkg.splitlines() if ln.strip().startswith("payload:"))
        payload = json.loads(line.split("payload:", 1)[1])
        self.assertIn("expand(['binary_sensor.den', 'person.ann'])", payload)
        self.assertIn('"occupied": {{', payload)
        self.assertNotIn("rest_command", sp.ha_package({**d, "power_mode": "hours"}))


class CEC(unittest.TestCase):
    def test_parse_power(self):
        nack = "Transmit from Playback Device 1 to TV (4 to 0):\nGIVE_DEVICE_POWER_STATUS (0x8f)\n\tTx, Not Acknowledged (4), Max Retries\n"
        self.assertEqual(sp.parse_power(nack), "no answer")                    # as the test Pi's LG monitor answered
        self.assertEqual(sp.parse_power("Received from TV to Playback Device 1 (0 to 4):\nREPORT_POWER_STATUS (0x90):\n\tpwr-state: on (0x00)\n"), "on")
        self.assertEqual(sp.parse_power("\tpwr-state: standby (0x01)\n"), "standby")
        self.assertEqual(sp.parse_power("\tpwr-state: to-standby (0x03)\n"), "standby")
        self.assertEqual(sp.parse_power(""), "")


if __name__ == "__main__":
    unittest.main()
