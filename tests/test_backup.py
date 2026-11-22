"""Tests for USB backups: which disks are offered, the overdue logic and the /backup page.
(The backup and restore themselves were tested against real VictoriaMetrics and restic containers.)
Run: python3 -m unittest discover -s tests -v"""
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import backup  # noqa: E402
import modules  # noqa: E402
from modules import backup as backup_module  # noqa: E402

# A Pi booted from a USB SSD (sda, root on sda2) with a USB stick (sdb) and an SD card reader.
LSBLK = {"blockdevices": [
    {"name": "sda", "path": "/dev/sda", "type": "disk", "tran": "usb", "size": "119.2G", "vendor": "Samsung ", "model": "PSSD T7",
     "children": [
         {"name": "sda1", "path": "/dev/sda1", "type": "part", "fstype": "vfat", "label": "bootfs", "uuid": "4B1C-11F2",
          "mountpoints": ["/boot/firmware"]},
         {"name": "sda2", "path": "/dev/sda2", "type": "part", "fstype": "ext4", "label": "rootfs", "uuid": "root-uuid",
          "mountpoints": ["/"]}]},
    {"name": "sdb", "path": "/dev/sdb", "type": "disk", "tran": "usb", "size": "28.9G", "vendor": "SanDisk ", "model": "Ultra Fit",
     "children": [{"name": "sdb1", "path": "/dev/sdb1", "type": "part", "fstype": "exfat", "label": "", "uuid": "62A1-77F0",
                   "mountpoints": [None]}]},
    {"name": "sdc", "path": "/dev/sdc", "type": "disk", "tran": "usb", "size": "8G", "model": "Card reader",
     "children": [{"name": "sdc1", "path": "/dev/sdc1", "type": "part", "fstype": "swap", "uuid": "swap-uuid"}]},
    {"name": "mmcblk0", "path": "/dev/mmcblk0", "type": "disk", "tran": None, "size": "29.7G",
     "children": [{"name": "mmcblk0p1", "type": "part", "fstype": "vfat", "uuid": "SDCARD-1"}]},
]}


class Backup(unittest.TestCase):
    def test_sticks_exclude_the_boot_ssd(self):
        self.assertEqual(backup.root_disk(LSBLK), "sda")
        s = backup.sticks_from(LSBLK)
        self.assertEqual([k["uuid"] for k in s], ["62A1-77F0"])   # not the SSD, swap or the SD card
        self.assertEqual(s[0]["model"], "SanDisk Ultra Fit")
        self.assertEqual(s[0]["mounted"], [])

    def test_overdue(self):
        stack = tempfile.mkdtemp()
        os.makedirs(os.path.join(stack, "state"))
        now = time.time()
        self.assertEqual(backup_module.ha_fields(stack, None, now)["backup_configured"], False)
        backup.save_settings(stack, {"uuid": "62A1-77F0", "history": True})
        self.assertFalse(backup_module.ha_fields(stack, None, now)["backup_overdue"])            # just chosen
        self.assertTrue(backup_module.ha_fields(stack, None, now + 3 * 86400)["backup_overdue"])  # never worked since
        backup.record(stack, result="ok", last_ok=int(now))
        f = backup_module.ha_fields(stack, None, now + 86400)
        self.assertFalse(f["backup_overdue"])
        self.assertTrue(f["backup_last_ok"].endswith("+00:00"))
        self.assertTrue(backup_module.ha_fields(stack, None, now + 2.5 * 86400)["backup_overdue"])

    def test_page_and_settings(self):
        os.environ["SOLAR_COMPOSE"] = "true"
        import solar_web
        stack = tempfile.mkdtemp()
        os.makedirs(os.path.join(stack, "state"))
        solar_web.STACK = stack
        backup.list_sticks = lambda: backup.sticks_from(LSBLK)
        web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), solar_web.Handler)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{web.server_port}"
        page = urllib.request.urlopen(base + "/backup").read().decode()
        self.assertIn("SanDisk Ultra Fit, 28.9G, exfat", page)
        self.assertNotIn("PSSD", page)
        self.assertNotIn("Restore from the stick", page)          # no stick chosen yet
        post = lambda p, f: urllib.request.urlopen(urllib.request.Request(  # noqa: E731
            base + p, data=urllib.parse.urlencode(f).encode(), method="POST"))
        with self.assertRaises(urllib.error.HTTPError) as e:
            post("/backup/settings", {"uuid": "root-uuid"}).read()  # the boot SSD can't be chosen
        e.exception.close()
        page = post("/backup/settings", {"uuid": "62A1-77F0", "history": "1"}).read().decode()
        self.assertIn("Restore from the stick", page)
        cfg = backup.settings(stack)
        self.assertEqual((cfg["uuid"], cfg["history"]), ("62A1-77F0", True))
        self.assertTrue(os.path.exists(modules.path(stack, "backup")))
        with self.assertRaises(urllib.error.HTTPError) as e:
            post("/backup/restore", {}).read()                     # needs the confirmation tick
        e.exception.close()
        self.assertEqual(e.exception.code, 400)
        start = urllib.request.urlopen(base + "/").read().decode()
        self.assertIn("last backup never", start)
        web.shutdown()
        web.server_close()


if __name__ == "__main__":
    unittest.main()
