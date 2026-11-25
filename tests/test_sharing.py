"""Tests for following another site (setup/sharing.py, the "share" and "follow" modules): the source's listener
files and share code, the follower's mirrors and dongle entries, and the scan leaving followed dongles alone.
Run: python3 -m unittest discover -s tests -v"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import modules  # noqa: E402
import sharing  # noqa: E402
import solar_setup as ss  # noqa: E402
import yaml  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
CERT = "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"


def box(tmp, name, inverters):
    stack = os.path.join(tmp, name)
    os.makedirs(os.path.join(stack, "state"))
    os.makedirs(os.path.join(stack, "mosquitto"))
    os.makedirs(os.path.join(stack, "bridge"))
    with open(os.path.join(stack, "state", "inverters.json"), "w") as f:
        json.dump(inverters, f)
    with open(os.path.join(stack, "state", "site.json"), "w") as f:
        json.dump({"name": "Ranch", "time_zone": "America/Denver"}, f)
    return stack


def turn_on(stack, module):
    d = modules.load(stack, module)
    d["enabled"] = True
    with open(modules.path(stack, module), "w") as f:
        json.dump(d, f)


class Sharing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SOLAR_COMPOSE"] = "true"
        self.src = box(self.tmp.name, "src", {"BA11111111": {"host": "10.0.0.5", "serial": "1234567890", "present": True},
                                              "BA22222222": {"host": "10.0.0.6", "serial": "", "present": True}})
        self.dst = box(self.tmp.name, "dst", {})

    def tearDown(self):
        os.environ.pop("SOLAR_COMPOSE", None)
        modules._ALL = None
        self.tmp.cleanup()

    def fake_cert(self, stack):
        os.makedirs(sharing.conf_dir(stack), exist_ok=True)
        for f in ("share.crt", "share.key"):
            with open(os.path.join(sharing.conf_dir(stack), f), "w") as fh:
                fh.write(CERT)

    def code(self):
        self.fake_cert(self.src)
        return sharing.make_code(self.src, "Ranch", ["192.0.2.10", "solar-monitor.local"])

    def test_source_files(self):
        self.fake_cert(self.src)
        self.assertTrue(sharing.write_source(self.src, True))
        self.assertFalse(sharing.write_source(self.src, True))            # unchanged
        d = sharing.conf_dir(self.src)
        conf = open(os.path.join(d, "share.conf")).read()
        self.assertIn("listener 8883", conf)
        self.assertIn("certfile /mosquitto/config/conf.d/share.crt", conf)
        acl = open(os.path.join(d, "acl")).read()
        self.assertIn("user share\ntopic read lxp/+/raw/#", acl)           # a follower reads raw frames, nothing else
        self.assertEqual(acl.count("readwrite"), 2)                       # the box's own accounts keep full access
        self.assertNotIn("write", acl.split("user share")[1])
        self.assertTrue(sharing.write_source(self.src, False))            # off: the listener and the list go
        self.assertEqual(sorted(os.listdir(d)), ["share.crt", "share.key"])   # the certificate stays: the code still fits
        self.assertFalse(sharing.write_source(self.src, False))

    @unittest.skipUnless(shutil.which("openssl"), "needs openssl")
    def test_certificate(self):
        pem = sharing.ensure_cert(self.src)
        self.assertIn("BEGIN CERTIFICATE", pem)
        self.assertEqual(sharing.ensure_cert(self.src), pem)              # made once
        self.assertEqual(os.stat(os.path.join(sharing.conf_dir(self.src), "share.key")).st_mode & 0o777, 0o600)
        import subprocess
        out = subprocess.run(["openssl", "x509", "-in", os.path.join(sharing.conf_dir(self.src), "share.crt"), "-noout", "-ext",
                              "subjectAltName"], capture_output=True, text=True).stdout
        self.assertIn("DNS:solar-share", out)

    def test_password_and_accounts(self):
        sec = ss.ensure_secrets(self.src)
        self.assertNotIn("share:", open(os.path.join(self.src, "mosquitto", "passwd")).read())
        p = sharing.password(self.src)
        self.assertEqual(sharing.password(self.src), p)
        ss.write_mosquitto_passwd(self.src, ss.ensure_secrets(self.src))
        lines = open(os.path.join(self.src, "mosquitto", "passwd")).read().split()
        self.assertEqual([ln.split(":")[0] for ln in lines], ["bridge", "homeassistant", "share"])
        self.assertNotIn(p, " ".join(lines))                              # hashed
        self.assertNotEqual(sharing.password(self.src, new=True), p)      # "new code"
        self.assertEqual(ss.ensure_secrets(self.src)["mqtt_bridge"], sec["mqtt_bridge"])

    def test_code_round_trip(self):
        code = self.code()
        self.assertTrue(code.startswith("SOLARSHARE1-"))
        s = sharing.read_code("  " + code[:40] + "\n" + code[40:] + " ")   # pasted with a line break
        self.assertEqual((s["name"], s["host"], s["hosts"], s["port"], s["user"], s["ns"]),
                         ("Ranch", "192.0.2.10", ["192.0.2.10", "solar-monitor.local"], 8883, "share", "lxp"))
        self.assertEqual(s["password"], sharing.password(self.src))
        self.assertEqual(s["dongles"], [{"datalog": "BA11111111", "serial": "1234567890"}, {"datalog": "BA22222222", "serial": ""}])
        self.assertEqual(s["cert"], CERT)
        for bad in ("", "hello", "SOLARSHARE1-!!!", "SOLARSHARE1-e30", "SOLARSHARE2-abc"):
            with self.assertRaises(sharing.ShareError):
                sharing.read_code(bad)
        import base64
        doc = {"v": 1, "hosts": ["a b", "ok.example"], "port": 8883, "pass": "x", "cert": CERT, "dongles": [{"datalog": "../etc"}]}
        with self.assertRaises(sharing.ShareError):                       # no usable dongle id
            sharing.read_code("SOLARSHARE1-" + base64.urlsafe_b64encode(json.dumps(doc).encode()).decode())

    def test_bridge_publishes_raw_frames_only_while_sharing(self):
        inv = ss.load_json(os.path.join(self.src, "state", "inverters.json"), {})
        sec = ss.ensure_secrets(self.src)
        ss.write_bridge_config(self.src, inv, sec)
        cfg = yaml.safe_load(open(os.path.join(self.src, "bridge", "config.yml")))
        self.assertNotIn("publish_raw_frames", cfg["mqtt"])
        turn_on(self.src, "share")
        self.assertTrue(ss.write_bridge_config(self.src, inv, sec))
        cfg = yaml.safe_load(open(os.path.join(self.src, "bridge", "config.yml")))
        self.assertIs(cfg["mqtt"]["publish_raw_frames"], True)
        self.assertEqual(modules.get("share").env(self.src), ["SHARE_BIND=0.0.0.0"])

    def test_follower_mirrors_and_dongles(self):
        s = {**sharing.read_code(self.code()), "client": "abcd1234"}
        self.assertEqual(sharing.write_follow(self.dst, s), (True, True))
        self.assertEqual(sharing.write_follow(self.dst, s), (False, False))
        m = yaml.safe_load(open(os.path.join(self.dst, "mirror", "mirror2.yml")))["mirror"]
        self.assertEqual((m["datalog"], m["listen"]), ("BA22222222", "0.0.0.0:8000"))
        self.assertEqual(m["mqtt"]["tls"], {"enabled": True, "ca_file": "/config/ca.crt", "server_name": "solar-share"})
        self.assertEqual((m["mqtt"]["host"], m["mqtt"]["port"], m["mqtt"]["username"], m["mqtt"]["clientName"]),
                         ("192.0.2.10", 8883, "share", "solar-follow-abcd1234-2"))
        self.assertEqual(open(os.path.join(self.dst, "mirror", "ca.crt")).read(), CERT)
        inv = ss.load_json(os.path.join(self.dst, "state", "inverters.json"), {})
        self.assertEqual(inv["BA11111111"], {"host": "mirror1", "serial": "1234567890", "present": True, "remote": True,
                                             "site": "Ranch", "last_seen": ""})
        self.assertEqual(sharing.profiles(s), ["follow1", "follow2"])
        # this box's bridge reads them at their mirrors
        ss.write_bridge_config(self.dst, inv, ss.ensure_secrets(self.dst))
        cfg = yaml.safe_load(open(os.path.join(self.dst, "bridge", "config.yml")))
        self.assertEqual([(i["datalog"], i["host"], i["port"]) for i in cfg["inverters"]],
                         [("BA11111111", "mirror1", 8000), ("BA22222222", "mirror2", 8000)])
        self.assertIs(cfg["read_only"], True)
        # a followed site doesn't share them on (only its own dongles go in a share code)
        self.assertEqual(sharing.local_dongles(self.dst), [])
        # stop following: the mirrors' files and the entries go, a dongle of this box's own stays
        inv["BA99999999"] = {"host": "10.9.9.9", "present": True}
        json.dump(inv, open(os.path.join(self.dst, "state", "inverters.json"), "w"))
        self.assertEqual(sharing.write_follow(self.dst, {"dongles": [], "host": ""}), (True, True))
        self.assertEqual(list(ss.load_json(os.path.join(self.dst, "state", "inverters.json"), {})), ["BA99999999"])
        self.assertEqual([f for f in os.listdir(os.path.join(self.dst, "mirror")) if f.endswith(".yml")], [])
        self.assertEqual(sharing.profiles({"dongles": [], "host": ""}), [])

    def test_a_dongle_read_directly_becomes_followed(self):
        """On the same network this box may already read the dongle itself: following takes it over."""
        json.dump({"BA11111111": {"host": "10.0.0.5", "serial": "1234567890", "present": True, "last_seen": "2026-11-25 10:00"}},
                  open(os.path.join(self.dst, "state", "inverters.json"), "w"))
        sharing.write_follow(self.dst, {**sharing.read_code(self.code()), "client": "c"})
        inv = ss.load_json(os.path.join(self.dst, "state", "inverters.json"), {})
        self.assertEqual((inv["BA11111111"]["host"], inv["BA11111111"]["remote"], inv["BA11111111"]["last_seen"]),
                         ("mirror1", True, "2026-11-25 10:00"))

    def test_mirror_health(self):
        self.assertEqual(sharing.mirror_health(1, fetch=lambda n: (204, "")), (True, "getting data from the source"))
        ok, text = sharing.mirror_health(1, fetch=lambda n: (503, "not connected to the MQTT broker"))
        self.assertEqual((ok, text), (False, "not connected to the MQTT broker"))

        def down(n):
            raise OSError("refused")
        self.assertEqual(sharing.mirror_health(2, fetch=down), (False, "its mirror isn't running (refused)"))

    def test_modules(self):
        from modules import follow, share
        self.fake_cert(self.src)
        turn_on(self.src, "share")
        sharing.write_source(self.src, True)
        page = share.setup_section(self.src)
        self.assertIn("SOLARSHARE1-", page)
        self.assertIn("sharing 2 dongles (BA11111111, BA22222222)", page)
        self.assertEqual(share.checks(self.src)[0]["state"], "ok")
        self.assertEqual(share.checks(self.dst)[0]["state"], "bad")        # nothing of its own to share
        self.assertIn("nothing to share", share.setup_section(self.dst))
        # the follower: a bad code, then the real one
        self.assertEqual(follow.checks(self.dst)[0]["state"], "warn")
        r = follow.save(self.dst, {"share_code": "nonsense"})
        self.assertEqual(r.code, 400)
        self.assertIn("isn't a share code", r.setup_msg)
        code = sharing.make_code(self.src, "Ranch", ["192.0.2.10"])
        r = follow.save(self.dst, {"share_code": code, "source_host": "10.8.0.1"})    # reached over a VPN
        self.assertIn("following Ranch at 10.8.0.1", r.setup_msg)
        s = follow.settings(self.dst)
        self.assertEqual((s["host"], len(s["dongles"]), len(s["client"])), ("10.8.0.1", 2, 8))
        self.assertEqual(follow.compose_profiles(self.dst), ["follow1", "follow2"])
        self.assertIn("Following Ranch", follow.setup_section(self.dst))
        self.assertNotIn(s["password"], follow.setup_section(self.dst))    # the code's password is never shown again
        self.assertEqual(follow.save(self.dst, {"share_code": "", "source_host": "bad host"}).code, 400)
        r = follow.stop(self.dst, {})
        self.assertIn("no longer following", r.setup_msg)
        self.assertEqual(ss.load_json(os.path.join(self.dst, "state", "inverters.json"), {}), {})
        self.assertEqual(follow.compose_profiles(self.dst), [])

    def test_stack_files(self):
        compose = yaml.safe_load(open(os.path.join(ROOT, "stack", "compose.yaml")))["services"]
        image = compose["bridge"]["image"]
        for n in range(1, sharing.MAX_DONGLES + 1):
            m = compose[f"mirror{n}"]
            self.assertEqual((m["image"], m["profiles"], m["command"][-1]), (image, [f"follow{n}"], f"/config/mirror{n}.yml"))
            self.assertEqual(m["ports"], [f"127.0.0.1:{sharing.HEALTH_PORT + n}:8080"])
        self.assertIn("${SHARE_BIND:-127.0.0.1}:8883:8883", compose["mosquitto"]["ports"])
        self.assertIn("127.0.0.1:1883:1883", compose["mosquitto"]["ports"])       # the box's own listener stays local
        self.assertIn("include_dir /mosquitto/config/conf.d", open(os.path.join(ROOT, "stack", "mosquitto", "mosquitto.conf")).read())


if __name__ == "__main__":
    unittest.main()
