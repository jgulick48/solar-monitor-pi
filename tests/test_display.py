"""Tests for the wall display (setup/display.py) and its pages in solar_web.py.
Run: python3 -m unittest discover -s tests -v"""
import datetime as dt
import http.server
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import display  # noqa: E402
import solar_setup as ss  # noqa: E402

DENVER = ZoneInfo("America/Denver")
SITE = {"name": "Test", "latitude": "39.74", "longitude": "-104.99", "time_zone": "America/Denver",
        "battery_count": "6", "battery_kwh_each": "5.12", "low_soc_alert": "25", "generator": "manual"}


class FakeVM(http.server.BaseHTTPRequestHandler):
    values = {"max(lxp_soc)": 62, "sum(lxp_p_pv)": 3400, "sum(lxp_p_load) or sum(lxp_p_eps)": 900,
              "sum(lxp_p_charge) - sum(lxp_p_discharge)": 2300, ss.GEN_POWER: 0, "sum(lxp_e_pv_day)": 12.3,
              ss.LOAD_ENERGY_DAY: 8.1, "sum(lxp_bridge_connected)": 2,
              "max(lxp_fault_code)": 0, "max(lxp_warning_code)": 0, "time() - max(timestamp(lxp_soc))": 12}

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)["query"][0]
        q = re.sub(r"last_over_time\((lxp_[a-z0-9_]+(?:\{[^}]*\})?)\[5m\]\)", r"\1", q)   # the 5-minute hold (solar_setup.hold)
        if u.path.endswith("query_range"):
            a = urllib.parse.parse_qs(u.query)
            pts = [[t, "1000"] for t in range(int(a["start"][0]), int(a["end"][0]) + 1, int(a["step"][0]))]
            res = [{"metric": {}, "values": pts}]
        else:
            v = self.values.get(q, 800 if q.startswith("avg_over_time") else None)
            res = [] if v is None else [{"metric": {}, "value": [0, str(v)]}]
        body = json.dumps({"status": "success", "data": {"resultType": "vector", "result": res}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class Display(unittest.TestCase):
    def test_go2rtc_source(self):
        self.assertEqual(display.go2rtc_source("rtsps://192.168.1.1:7441/AbC123?enableSrtp"), "rtspx://192.168.1.1:7441/AbC123")
        self.assertEqual(display.go2rtc_source("rtsp://10.0.0.5:7447/xyz"), "rtsp://10.0.0.5:7447/xyz")
        for bad in ("http://x/y", "rtsps:///nohost", "garbage"):
            with self.assertRaises(ValueError):
                display.go2rtc_source(bad)

    def test_generator_power_ignores_the_6000xp_gen_registers(self):
        # one guarded expression everywhere (checked against VictoriaMetrics v1.156: 6000XP with 55/64 kW and 0 V
        # on its GEN registers -> 0 W; generator on its AC input -> counted; 18kPV GEN port with voltage -> counted)
        import advisor
        import genlog
        self.assertEqual(display.INSTANT["gen"], ss.hold(ss.GEN_POWER))
        self.assertEqual(genlog.QUERIES["inverter"], ss.GEN_POWER)
        for needle in ("lxp_v_gen > bool 50", "lxp_p_gen < 25000", "lxp_v_ac_r > bool 50"):
            self.assertIn(needle, ss.GEN_POWER)
        self.assertIn("max_over_time(lxp_v_gen[1d]) > bool 50", ss.GEN_ENERGY_DAY)
        self.assertNotIn("sum(lxp_p_gen)", open(advisor.__file__).read())

    def test_bms_current_from_the_inverter_that_has_it(self):
        # checked against VictoriaMetrics v1.156: one BMS -17.1 A + one 0 while discharging 855 W -> about 0, not 855 W
        import advisor
        import genlog
        self.assertEqual(display.INSTANT["ext_charge"], ss.hold(ss.EXT_CHARGE))
        self.assertEqual(genlog.QUERIES["charger"], ss.EXT_CHARGE)
        self.assertIn("min(lxp_batt_current_bms) < 0", ss.BMS_POWER)
        # robust against what isn't a DC charger (ranch, 30 days: 5 phantom runs -> 0; VictoriaMetrics v1.156: a 3 kW
        # charger -> 2980 W, solar charging with the BMS 8% high -> nothing, a generator the inverters see -> nothing)
        for needle in ("avg_over_time((" + ss.BMS_POWER + ")[5m:30s])", "> 0.2 * abs(", "unless on () (max_over_time((max(lxp_v_gen) > 100"):
            self.assertIn(needle, ss.EXT_CHARGE)
        self.assertNotIn("max(lxp_batt_current_bms) * max(lxp_v_bat)", open(advisor.__file__).read())

    def test_house_energy_uses_eps_when_the_load_counter_is_zero(self):
        # checked against VictoriaMetrics v1.156: 6000XP x2 (load 0, eps 7.1 + 6.6) -> 13.7; 18kPV (load 20, eps 15) -> 20
        self.assertEqual(display.INSTANT["used_today"], ss.hold(ss.LOAD_ENERGY_DAY))
        self.assertIn("lxp_e_load_day > lxp_e_eps_day", ss.LOAD_ENERGY_DAY)

    def test_live_queries_hold_last_value_5_minutes(self):
        # a dongle dropping off for a moment: its readings go stale at once; hold them for up to 5 minutes
        self.assertEqual(ss.hold("sum(lxp_p_pv)"), "sum(last_over_time(lxp_p_pv[5m]))")
        self.assertEqual(ss.hold('max(lxp_soc{deploymentID="x"})'), 'max(last_over_time(lxp_soc{deploymentID="x"}[5m]))')
        self.assertEqual(ss.hold("max_over_time(lxp_v_gen[1d])"), "max_over_time(lxp_v_gen[1d])")   # already a range
        self.assertIn("last_over_time(lxp_p_pv[5m])", display.INSTANT["pv"])
        self.assertIn("last_over_time(lxp_e_pv_day[5m])", display.INSTANT["pv_today"])
        self.assertIn("last_over_time(lxp_p_pv[5m])", display.RANGE["pv"])
        self.assertEqual(display.INSTANT["online"], "sum(lxp_bridge_connected)")      # stays live: a gone dongle shows
        self.assertNotIn("last_over_time", display.INSTANT["age"])

    def test_hhmm_and_off_hours(self):
        self.assertEqual(display.parse_hhmm("6:05"), "06:05")
        self.assertEqual(display.parse_hhmm(""), "")
        with self.assertRaises(ValueError):
            display.parse_hhmm("25:00")
        d = {"off_from": "22:00", "off_to": "06:00"}
        at = lambda h, m=0: dt.datetime(2026, 11, 21, h, m)  # noqa: E731
        self.assertTrue(display.screen_off_now(d, at(23)))
        self.assertTrue(display.screen_off_now(d, at(5, 59)))
        self.assertFalse(display.screen_off_now(d, at(6)))
        self.assertFalse(display.screen_off_now(d, at(12)))
        self.assertTrue(display.screen_off_now({"off_from": "01:00", "off_to": "05:00"}, at(3)))
        self.assertFalse(display.screen_off_now({"off_from": "", "off_to": "05:00"}, at(3)))

    def test_sun_times_denver(self):
        rise, sset = display.sun_times(39.74, -104.99, dt.date(2026, 11, 21), DENVER)
        # Denver 2026-11-21: sunrise ~06:52, sunset ~16:38 MST
        self.assertLess(abs((rise.hour * 60 + rise.minute) - (6 * 60 + 52)), 4, rise)
        self.assertLess(abs((sset.hour * 60 + sset.minute) - (16 * 60 + 38)), 4, sset)

    def test_generator_advice(self):
        noon = dt.datetime(2026, 11, 21, 12, 0, tzinfo=DENVER)
        self.assertEqual(display.generator_advice(SITE, {"soc": 50, "load_avg_3d": 800}, noon, DENVER)["level"], "info")
        eve = dt.datetime(2026, 11, 21, 15, 0, tzinfo=DENVER)
        # 30.72 kWh; 50% -> 25% leaves 7.7 kWh, ~9.6 h at 800 W: runs out ~00:36, before ~09:00 relief
        a = display.generator_advice(SITE, {"soc": 50, "load_avg_3d": 800}, eve, DENVER)
        self.assertEqual(a["level"], "warn")
        self.assertIn("before dark", a["text"])
        a = display.generator_advice(SITE, {"soc": 95, "load_avg_3d": 800}, eve, DENVER)
        self.assertEqual(a["level"], "ok")
        # 15:00 to sunrise ~06:53 is ~15.9 h at 800 W = 12.7 kWh = 41% of 30.72 kWh: about 54% at sunrise
        self.assertEqual(a["sunrise_soc"], 54)
        self.assertIn("about 54% at sunrise (6:5", a["text"])
        self.assertIn("AM),", a["text"].replace("\u00a0", " "))
        late = dt.datetime(2026, 11, 21, 20, 0, tzinfo=DENVER)
        self.assertIn("now", display.generator_advice(SITE, {"soc": 40, "load_avg_3d": 800}, late, DENVER)["text"])
        self.assertIsNone(display.generator_advice(SITE, {"soc": None}, eve, DENVER))

    def test_save_writes_go2rtc(self):
        stack = tempfile.mkdtemp()
        os.makedirs(os.path.join(stack, "state"))
        d = {"enabled": True, "off_from": "", "off_to": "",
             "cameras": [{"name": "Drive", "url": "rtsps://h:7441/TOK?enableSrtp"}]}
        self.assertTrue(display.save(stack, d))
        with open(os.path.join(stack, "go2rtc", "go2rtc.yaml")) as f:
            y = f.read()
        self.assertIn('cam1: "rtspx://h:7441/TOK"', y)
        self.assertIn('listen: "127.0.0.1:1984"', y)
        self.assertEqual(os.stat(os.path.join(stack, "state", "display.json")).st_mode & 0o777, 0o600)
        self.assertFalse(display.save(stack, d))
        display.save(stack, dict(d, cameras=[]))
        with open(os.path.join(stack, "go2rtc", "go2rtc.yaml")) as f:
            self.assertIn("streams: {}", f.read())

    def test_data_and_web(self):
        vm = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeVM)
        threading.Thread(target=vm.serve_forever, daemon=True).start()
        display.VM = f"http://127.0.0.1:{vm.server_port}"
        display._cache.clear()
        stack = tempfile.mkdtemp()
        os.makedirs(os.path.join(stack, "state"))
        with open(os.path.join(stack, "state", "inverters.json"), "w") as f:
            json.dump({"a": {}, "b": {}}, f)
        display.save(stack, {"enabled": True, "off_from": "", "off_to": "",
                             "cameras": [{"name": "Drive", "url": "rtsps://h:7441/TOK"}]})
        d = display.data(stack, SITE)
        self.assertEqual(d["now"]["soc"], 62)
        self.assertEqual(d["cameras"], [{"id": "cam1", "name": "Drive", "problem": ""}])
        self.assertNotIn("TOK", json.dumps(d))       # the stream token never reaches the page
        self.assertEqual(d["alerts"], [])
        self.assertEqual(len(d["series"]["pv"]), 145)
        self.assertEqual(d["battery_kwh"], 30.7)
        # web routes (no Docker or systemd here: compose is a no-op)
        os.environ["SOLAR_COMPOSE"] = "true"
        import solar_web
        solar_web.ss.host_timezone = lambda: SITE["time_zone"]  # don't try to set this machine's time zone
        solar_web.STACK = stack
        with open(os.path.join(stack, "state", "site.json"), "w") as f:
            json.dump(dict(SITE, arrays=[{"name": "A", "panels": "24", "panel_watts": "285", "tilt": "30", "azimuth": "180"}]), f)
        web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), solar_web.Handler)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{web.server_port}"
        page = urllib.request.urlopen(base + "/display").read().decode()
        self.assertIn("/display/data", page)
        j = json.loads(urllib.request.urlopen(base + "/display/data").read())
        self.assertEqual(j["name"], "Test")
        setup = urllib.request.urlopen(base + "/setup").read().decode()
        self.assertIn("Screen and cameras", setup)
        self.assertNotIn("TOK", setup)               # saved link is masked
        self.assertIn("rtsps://h:7441/••••OK", setup)
        start = urllib.request.urlopen(base + "/").read().decode()
        self.assertIn("1 camera(s)", start)
        # setup form: keep camera 1 (empty link), add camera 2, screen off hours
        form = dict(SITE, a1_name="A", a1_panels="24", a1_panel_watts="285", a1_tilt="30", a1_azimuth="180",
                    elevation="", inverter_model="6000xp", generator_kw="", future_generator_kw="28",
                    generator_fuel="gasoline", weather_source="openmeteo", display_enabled="1",
                    power_mode="hours", hours1_days="weekdays", hours1_on="6:00", hours1_off="22:00",
                    hours2_days="weekends", hours2_on="08:00", hours2_off="01:00", wake_doorbell="1", cec="1",
                    cam1_name="Drive", cam1_url="",
                    cam2_name="Door", cam2_url="rtsps://10.1.1.1:7441/SECRET2?enableSrtp")
        post = lambda f: urllib.request.urlopen(urllib.request.Request(  # noqa: E731
            base + "/setup", data=urllib.parse.urlencode(f).encode(), method="POST"))
        post(form).read()
        saved = display.load(stack)
        self.assertEqual([c["url"] for c in saved["cameras"]], ["rtsps://h:7441/TOK", "rtsps://10.1.1.1:7441/SECRET2?enableSrtp"])
        self.assertEqual((saved["power_mode"], saved["hours"]), ("hours", [{"days": "weekdays", "on": "06:00", "off": "22:00"},
                                                                           {"days": "weekends", "on": "08:00", "off": "01:00"}]))
        self.assertEqual((saved["wake"]["doorbell"], saved["wake"]["person"], saved["cec"]), (True, False, True))
        # occupancy: needs sensors; with them, Home Assistant gets an automation that reports to /display/presence
        with self.assertRaises(urllib.error.HTTPError) as e:
            post(dict(form, power_mode="occupied")).read()
        self.assertEqual(e.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as e:
            post(dict(form, power_mode="occupied", occupancy="light.kitchen; rm -rf")).read()
        self.assertEqual(e.exception.code, 400)
        post(dict(form, power_mode="occupied", occupancy="binary_sensor.den_motion, person.ann", occupancy_hold="5")).read()
        with open(os.path.join(stack, "homeassistant", "packages", "solar_display.yaml")) as f:
            pkg = f.read()
        self.assertIn("http://127.0.0.1/display/presence", pkg)
        self.assertIn('entity_id: ["binary_sensor.den_motion", "person.ann"]', pkg)
        power = lambda: json.load(urllib.request.urlopen(base + "/display/power"))  # noqa: E731
        self.assertEqual(power()["why"], "no word from Home Assistant's sensors")   # unknown: stays on
        presence = lambda occ: urllib.request.urlopen(urllib.request.Request(  # noqa: E731
            base + "/display/presence", data=json.dumps({"occupied": occ}).encode(), method="POST")).read()
        presence(True)
        self.assertEqual(power()["on"], True)
        sp = display.sp
        sp.PRESENCE["last_on"] -= 600                                   # the room emptied 10 minutes ago
        presence(False)
        self.assertEqual((power()["on"], power()["why"]), (False, "nobody in the room"))
        post(form).read()                                               # back to on-hours: package emptied
        with open(os.path.join(stack, "homeassistant", "packages", "solar_display.yaml")) as f:
            self.assertNotIn("rest_command", f.read())
        with open(os.path.join(stack, "go2rtc", "go2rtc.yaml")) as f:
            self.assertIn('cam2: "rtspx://10.1.1.1:7441/SECRET2"', f.read())
        with self.assertRaises(urllib.error.HTTPError) as e:
            post(dict(form, cam1_url="http://not-a-camera/")).read()
        self.assertEqual(e.exception.code, 400)
        self.assertEqual(len(display.load(stack)["cameras"]), 2)
        form2 = dict(form, cam1_remove="1")
        post(form2).read()
        self.assertEqual([c["name"] for c in display.load(stack)["cameras"]], ["Door"])


if __name__ == "__main__":
    unittest.main()
