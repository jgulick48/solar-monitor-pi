"""Tests for the Grafana dashboards (stack/grafana/dashboards) and the per-day samples behind their bars
(setup/vmdays.py, boiler.day_lines, water.day_lines_for). Run: python3 -m unittest discover -s tests -v"""
import glob
import json
import os
import re
import sys
import tempfile
import time
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "setup"))
os.environ["TZ"] = "America/Phoenix"
time.tzset()
import boiler  # noqa: E402
import vmdays  # noqa: E402
import water  # noqa: E402

BOARDS = sorted(glob.glob(os.path.join(ROOT, "stack", "grafana", "dashboards", "*.json")))
# metric names written somewhere in the box's code (and the HA state feed's)
SOURCE = ""
for _p in glob.glob(os.path.join(ROOT, "setup", "**", "*.py"), recursive=True):
    with open(_p) as _f:
        SOURCE += _f.read() + "\n"
FUNCS = {"avg_over_time", "last_over_time", "max_over_time", "min_over_time", "sum_over_time", "increase", "rate", "sum",
         "avg", "max", "min", "on", "and", "by", "label_values", "clamp_min", "deriv", "delta", "count"}


def load(path):
    with open(path) as f:
        return json.load(f)


def exprs(d):
    for p in d["panels"]:
        for tg in p.get("targets", []):
            yield p["title"], tg["expr"]


class Dashboards(unittest.TestCase):
    def test_valid_and_unique(self):
        self.assertTrue({"boiler.json", "water.json"} <= {os.path.basename(b) for b in BOARDS})
        uids = []
        for path in BOARDS:
            d = load(path)
            uids.append(d["uid"])
            ids = [p["id"] for p in d["panels"]]
            self.assertEqual(len(ids), len(set(ids)), path)
            for p in d["panels"]:
                if p["type"] != "row":
                    self.assertEqual(p["datasource"]["uid"], "victoriametrics", (path, p["title"]))
                    self.assertLessEqual(p["gridPos"]["x"] + p["gridPos"]["w"], 24, (path, p["title"]))
        self.assertEqual(len(uids), len(set(uids)))

    def test_metrics_exist(self):
        """Every metric a module dashboard queries is written by the box (a renamed metric breaks a panel silently)."""
        for path in BOARDS:
            if os.path.basename(path) not in ("boiler.json", "water.json"):
                continue
            for title, e in exprs(load(path)):
                names = {m for m in re.findall(r"\b([a-z][a-z0-9_]*)\b(?=\s*(?:\{|\[|$|\)|\s|\*|/|and))", re.sub(r'"[^"]*"', "", e))}
                for m in names - FUNCS - {"tu", "entity_id", "role", "kind", "source", "rule", "pair"}:
                    if m.startswith(("boiler_", "water_", "ha_")):
                        self.assertTrue(m in SOURCE, f"{os.path.basename(path)} / {title}: {m} is not written anywhere")

    def test_day_bars_use_day_samples(self):
        """Per-day bars read the noon-stamped day samples, one per day-wide window."""
        for path in BOARDS:
            for title, e in exprs(load(path)):
                if "_day_" in e:
                    self.assertIn("last_over_time(", e, title)
                    self.assertIn("[1d]", e, title)


class Days(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_noon(self):
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.localtime(vmdays.noon_ms("2026-11-23") / 1000)), "2026-11-23 12:00")

    def test_push_once_each_and_retry(self):
        days = {"2026-11-21": {"v": 1}, "2026-11-22": {"v": 2}, "2026-11-23": {"v": 3}}
        now = time.mktime(time.strptime("2026-11-23 15:00", "%Y-%m-%d %H:%M"))
        sent = []
        def fail(lines):
            raise OSError("down")
        with self.assertRaises(OSError):
            vmdays.push(self.stack, "x", days, lambda d, r: [f"x_day {r['v']}"], now, fail)
        self.assertEqual(vmdays.pending(self.stack, "x", days, now), ["2026-11-21", "2026-11-22"])    # not marked
        self.assertEqual(vmdays.push(self.stack, "x", days, lambda d, r: [f"x_day {r['v']}"], now, sent.extend), 2)
        self.assertEqual(sent, [f"x_day 1 {vmdays.noon_ms('2026-11-21')}", f"x_day 2 {vmdays.noon_ms('2026-11-22')}"])
        self.assertEqual(vmdays.push(self.stack, "x", days, lambda d, r: ["x_day 0"], now, sent.extend), 0)   # today waits
        later = now + 86400
        self.assertEqual(vmdays.push(self.stack, "x", days, lambda d, r: [f"x_day {r['v']}"], later, sent.extend), 1)
        self.assertEqual(sent[-1], f"x_day 3 {vmdays.noon_ms('2026-11-23')}")

    def test_boiler_day_lines(self):
        d = {"burner_min": 90.0, "heat_min": 70.0, "dhw_min": 15.0, "runs": 6, "faults": 1, "heat_kbtu": 210.5,
             "out_sum": 10.0, "out_n": 2}
        lines = boiler.day_lines("2026-11-22", d)
        self.assertIn('boiler_day_burner_minutes{kind="heating"} 70', lines)
        self.assertIn("boiler_day_heat_kbtu 210.5", lines)
        self.assertIn("boiler_day_outdoor_c 5", lines)
        self.assertIn("boiler_day_hdd 24", lines)                  # 65 - 41 °F

    def test_water_day_lines(self):
        s = {**water.DEFAULTS, "roles": {**water.DEFAULTS["roles"], "pump_power": "sensor.p"}, "pump_gpm": 8}
        d = {"runs": 12, "run_s": 600.0, "max_run_s": 120.0, "night_starts": [1, 3]}
        lines = water.day_lines_for(s)("2026-11-22", d)
        self.assertEqual(lines, ['water_day_used_gal{source="pump"} 80', "water_day_pump_run_minutes 10.0",
                                 "water_day_pump_runs 12", "water_day_longest_run_seconds 120", "water_day_night_hours 2"])

    def test_water_role_info(self):
        s = {**water.DEFAULTS, "roles": {**water.DEFAULTS["roles"], "pump_power": "sensor.well_pump_power"}}
        st = water.load(self.stack)
        st["last"] = {"t": time.time(), "pump": None, "tank": None}
        water.save(self.stack, st)
        self.assertIn('water_role_info{role="pump_power",entity_id="sensor.well_pump_power"} 1', water.vm_lines(self.stack, s))


if __name__ == "__main__":
    unittest.main()
