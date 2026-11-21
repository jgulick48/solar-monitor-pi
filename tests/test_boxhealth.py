"""Tests for the box's own health (boxhealth.py) and the test notification.
Run: python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import boxhealth  # noqa: E402
import solar_setup as ss  # noqa: E402

OK = {"temp": 52.0, "throttled": {"now": [], "since_boot": []}, "disk": {"free_gb": 180.0, "total_gb": 250.0, "free_pct": 72.0},
      "smart": {"kind": "NVMe", "used_pct": 3, "problems": []}, "containers": [], "drops": {"BA31101550": 0}}

NVME = {"model_name": "Samsung SSD 980 1TB", "nvme_smart_health_information_log": {
    "critical_warning": 0, "percentage_used": 12, "media_errors": 0, "available_spare": 100, "available_spare_threshold": 10}}
SATA = {"model_name": "CT500MX500SSD1", "smart_status": {"passed": True}, "ata_smart_attributes": {"table": [
    {"name": "Reallocated_Sector_Ct", "value": 100, "raw": {"value": 0}},
    {"name": "Percent_Lifetime_Remain", "value": 81, "raw": {"value": 19}}]}}


class BoxHealth(unittest.TestCase):
    def st(self, **kw):
        return boxhealth.status("/tmp", probe={**OK, **kw})

    def test_all_good(self):
        s = self.st()
        self.assertEqual((s["level"], s["findings"]), ("ok", []))

    def test_thresholds(self):
        self.assertEqual(self.st(temp=72)["level"], "note")
        self.assertIn("give it air", self.st(temp=83)["findings"][0][1])
        self.assertEqual(self.st(throttled={"now": [], "since_boot": ["under-voltage"]})["level"], "note")
        s = self.st(throttled={"now": ["under-voltage"], "since_boot": ["under-voltage"]})
        self.assertEqual(s["level"], "warn")
        self.assertIn("5.1 V", s["findings"][0][1])
        self.assertEqual(self.st(disk={"free_gb": 30.0, "total_gb": 250.0, "free_pct": 12.0})["level"], "note")
        self.assertEqual(self.st(disk={"free_gb": 1.5, "total_gb": 250.0, "free_pct": 0.6})["level"], "warn")
        self.assertEqual(self.st(smart={"kind": "NVMe", "used_pct": 75, "problems": []})["level"], "note")
        self.assertEqual(self.st(smart={"kind": "NVMe", "used_pct": 20, "problems": ["3 media errors"]})["level"], "warn")
        self.assertEqual(self.st(containers=["homeassistant"])["findings"], [("warn", "Not running: homeassistant.")])
        self.assertIn("dropped off the network 9 times", self.st(drops={"BA31101550": 9})["findings"][0][1])
        # nothing known (not a Pi, no smartctl, compose not answering): nothing to report
        self.assertEqual(self.st(temp=None, throttled=None, smart={"error": "smartctl not installed"}, containers=None, drops=None)["level"], "ok")

    def test_throttled_bits(self):
        self.assertEqual(boxhealth.throttled("throttled=0x0"), {"now": [], "since_boot": []})
        self.assertEqual(boxhealth.throttled("throttled=0x50000"), {"now": [], "since_boot": ["under-voltage", "throttled"]})
        self.assertEqual(boxhealth.throttled("throttled=0x1")["now"], ["under-voltage"])
        self.assertIsNone(boxhealth.throttled("garbage"))

    def test_parse_smart(self):
        n = boxhealth.parse_smart(NVME)
        self.assertEqual((n["kind"], n["used_pct"], n["problems"]), ("NVMe", 12, []))
        bad = json.loads(json.dumps(NVME))
        bad["nvme_smart_health_information_log"].update(media_errors=4, available_spare=5)
        self.assertEqual(boxhealth.parse_smart(bad)["problems"], ["4 media errors", "spare blocks used up"])
        s = boxhealth.parse_smart(SATA)
        self.assertEqual((s["kind"], s["used_pct"], s["problems"]), ("SATA", 19, []))
        failed = json.loads(json.dumps(SATA))
        failed["smart_status"]["passed"] = False
        failed["ata_smart_attributes"]["table"][0]["raw"]["value"] = 12
        self.assertEqual(boxhealth.parse_smart(failed)["problems"], ["SMART overall result: FAILED", "reallocated sector ct: 12"])
        self.assertIsNone(boxhealth.parse_smart({"smartctl": {"exit_status": 1}}))   # adapter passes nothing through

    def test_ha_fields(self):
        orig = boxhealth.status
        boxhealth.status = lambda stack: orig(stack, probe={**OK, "temp": 81.4})
        try:
            f = boxhealth.ha_fields("/tmp")
        finally:
            boxhealth.status = orig
        self.assertEqual((f["box_health"], f["box_cpu_temp"], f["box_disk_free"], f["box_ssd_used"]), ("warn", 81, 72.0, 3))
        self.assertIn("81", f["box_health_text"])


class TestNotification(unittest.TestCase):
    def test_webhook_automation_in_site_package(self):
        with tempfile.TemporaryDirectory() as stack:
            os.makedirs(os.path.join(stack, "state"))
            wid = ss.ha_webhook_id(stack)
            self.assertEqual(ss.ha_webhook_id(stack), wid)                      # kept
            self.assertEqual(os.stat(os.path.join(stack, "state", "secrets.json")).st_mode & 0o777, 0o600)
            pkg = ss.site_package({}, wid)
            self.assertIn(f'webhook_id: "{wid}"', pkg)
            self.assertIn("local_only: true", pkg)
            self.assertIn("action: script.solar_notify", pkg)
            self.assertNotIn("webhook", ss.site_package({}))

    def test_phones(self):
        import solar_web
        with tempfile.TemporaryDirectory() as stack:
            os.makedirs(os.path.join(stack, "homeassistant", ".storage"))
            old = solar_web.STACK
            solar_web.STACK = stack
            try:
                self.assertIn("No phone is signed in", solar_web.phones_line())
                json.dump({"data": {"entries": [{"domain": "mqtt", "title": "broker"}, {"domain": "mobile_app", "title": "Pixel 8"},
                                                {"domain": "mobile_app", "title": "Jane's iPhone"}]}},
                          open(os.path.join(stack, "homeassistant", ".storage", "core.config_entries"), "w"))
                self.assertEqual(solar_web.phones(), ["Pixel 8", "Jane's iPhone"])
                self.assertIn("Jane&#x27;s iPhone", solar_web.phones_line())
            finally:
                solar_web.STACK = old


if __name__ == "__main__":
    unittest.main()
