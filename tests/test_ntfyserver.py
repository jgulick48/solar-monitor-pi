"""Tests for ntfy on this box (setup/ntfyserver.py): its config and accounts, the tunnel route and DNS record, the
notifications module's choice and the channel posting to it. Cloudflare is the fake API from test_tunnel.
Run: python3 -m unittest discover -s tests -v"""
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import modules  # noqa: E402
import notify  # noqa: E402
import ntfyserver  # noqa: E402
import tunnel  # noqa: E402
import yaml  # noqa: E402
from modules import FieldError  # noqa: E402
from modules import notifications as mod  # noqa: E402
from test_tunnel import ACCT, TOKEN, FakeCF, serve  # noqa: E402

try:
    import bcrypt
except ImportError:          # pragma: no cover - python3-bcrypt is on the image
    bcrypt = None


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCF()
        self.srv = serve(self.fake)
        self.old_api = tunnel.API
        tunnel.API = f"http://127.0.0.1:{self.srv.server_port}/client/v4"
        os.environ["SOLAR_COMPOSE"] = "true"           # compose calls succeed and do nothing
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))
        p = os.path.join(self.stack, "homeassistant", ".storage")
        os.makedirs(p)
        with open(os.path.join(p, "onboarding"), "w") as f:
            json.dump({"data": {"done": ["user", "core_config"]}}, f)

    def tearDown(self):
        tunnel.API = self.old_api
        self.srv.shutdown()
        self.tmp.cleanup()
        os.environ.pop("SOLAR_COMPOSE", None)
        modules._ALL = None

    def remote_access(self):
        f = {"cf_account": ACCT, "cf_domain": "example.com", "cf_name": "solar", "cf_token": TOKEN}
        return tunnel.enable(self.stack, tunnel.clean(f, tunnel.settings(self.stack)))

    def form(self, **kw):
        f = {"nt_ha_app": "1", "nt_all_phones": "1", "nt_ntfy": "1", "nt_where": "box", "nt_server": "https://ntfy.sh",
             "nt_topic": "", "nt_token": "", "nt_wall": "1", "nt_quiet_from": "", "nt_quiet_to": ""}
        f.update(kw)
        return f

    def ingress(self, tid):
        return [r.get("hostname") for r in self.fake.configs[tid]["config"]["ingress"]]


class Config(Base):
    def test_config_accounts_and_stability(self):
        self.remote_access()
        cfg = ntfyserver.config(self.stack)
        d = yaml.safe_load(cfg)
        self.assertEqual(d["base-url"], "https://solar-ntfy.example.com")
        self.assertEqual(d["auth-default-access"], "deny-all")
        self.assertEqual((d["enable-signup"], d["enable-login"], d["web-root"]), (False, False, "disable"))
        self.assertEqual(d["upstream-base-url"], "https://ntfy.sh")
        t = ntfyserver.topic(self.stack)
        self.assertRegex(t, r"^solar-[0-9a-f]{12}$")
        self.assertEqual(sorted(d["auth-access"]), sorted([f"box:{t}:wo", f"phone:{t}:ro"]))
        tok = ntfyserver.box_token(self.stack)
        self.assertRegex(tok, r"^tk_[a-z0-9]{29}$")
        self.assertEqual(d["auth-tokens"], [f"box:{tok}:solar monitor"])
        users = {u.split(":")[0]: u for u in d["auth-users"]}
        self.assertEqual(set(users), {"box", "phone"})
        self.assertTrue(users["phone"].endswith(":user"))
        if bcrypt:
            h = users["phone"][len("phone:"):-len(":user")]
            self.assertTrue(bcrypt.checkpw(ntfyserver.phone_password(self.stack).encode(), h.encode()))
        self.assertEqual(ntfyserver.config(self.stack), cfg)           # stable: hashes kept
        old = ntfyserver.phone_password(self.stack)
        new = ntfyserver.reset_phone_password(self.stack)
        self.assertNotEqual(old, new)
        self.assertNotEqual(ntfyserver.config(self.stack), cfg)
        self.assertEqual(os.stat(os.path.join(self.stack, "state", "secrets.json")).st_mode & 0o777, 0o600)

    def test_host_needs_remote_access(self):
        self.assertEqual(ntfyserver.host(self.stack), "")
        self.remote_access()
        self.assertEqual(ntfyserver.host(self.stack), "solar-ntfy.example.com")


class Routes(Base):
    def test_turn_on_and_off_from_the_setup_page(self):
        d = self.remote_access()
        tid = d["tunnel_id"]
        self.assertEqual(self.ingress(tid), ["solar.example.com", None])
        mod.form_check(self.stack, self.form())
        msgs = mod.form_save(self.stack, self.form())
        self.assertIn("solar-ntfy.example.com added to the tunnel", msgs)
        self.assertEqual(self.ingress(tid), ["solar.example.com", "solar-ntfy.example.com", None])
        rule = self.fake.configs[tid]["config"]["ingress"][1]
        self.assertEqual(rule["service"], "http://localhost:2586")
        names = sorted(r["name"] for r in self.fake.dns.values())
        self.assertEqual(names, ["solar-ntfy.example.com", "solar.example.com"])
        self.assertTrue(ntfyserver.wanted(self.stack))
        self.assertIn("ntfy", modules.compose_profiles(self.stack))
        with open(os.path.join(self.stack, ".env")) as f:
            env = f.read()
        self.assertRegex(env, r"COMPOSE_PROFILES=[a-z,]*ntfy")
        self.assertTrue(os.path.exists(ntfyserver.config_path(self.stack)))
        # the setup page tells the phones what to enter
        page = mod.form_section(self.stack)
        self.assertIn("https://solar-ntfy.example.com", page)
        self.assertIn(ntfyserver.phone_password(self.stack), page)
        self.assertIn(ntfyserver.topic(self.stack), page)
        # back to a public server: route and record go
        msgs = mod.form_save(self.stack, self.form(nt_where="server"))
        self.assertIn("solar-ntfy.example.com removed from the tunnel", msgs)
        self.assertEqual(self.ingress(tid), ["solar.example.com", None])
        self.assertEqual([r["name"] for r in self.fake.dns.values()], ["solar.example.com"])
        self.assertNotIn("ntfy", modules.compose_profiles(self.stack))

    def test_refused_without_remote_access(self):
        with self.assertRaises(FieldError) as e:
            mod.form_check(self.stack, self.form())
        self.assertIn("Remote access", str(e.exception))
        self.assertIn("disabled", mod.form_section(self.stack).split("value='box'")[1].split(">")[0])

    def test_renaming_remote_access_moves_ntfy(self):
        self.remote_access()
        mod.form_save(self.stack, self.form())
        f = {"cf_account": ACCT, "cf_domain": "example.com", "cf_name": "home", "cf_token": ""}
        d = tunnel.enable(self.stack, tunnel.clean(f, tunnel.settings(self.stack)), prev=tunnel.settings(self.stack))
        self.assertEqual(self.ingress(d["tunnel_id"]), ["home.example.com", "home-ntfy.example.com", None])
        self.assertEqual(sorted(r["name"] for r in self.fake.dns.values()), ["home-ntfy.example.com", "home.example.com"])

    def test_turning_remote_access_off_removes_both_records(self):
        self.remote_access()
        mod.form_save(self.stack, self.form())
        msgs = tunnel.disable(self.stack, remove=True)
        self.assertIn("DNS record solar-ntfy.example.com deleted", msgs)
        self.assertEqual(self.fake.dns, {})
        self.assertFalse(ntfyserver.wanted(self.stack))            # still chosen, but nothing reaches it

    def test_module_off_stops_it(self):
        d = self.remote_access()
        mod.form_save(self.stack, self.form())
        modules.set_enabled(self.stack, "notifications", False)
        self.assertEqual(self.ingress(d["tunnel_id"]), ["solar.example.com", None])
        self.assertNotIn("ntfy", modules.compose_profiles(self.stack))
        modules.set_enabled(self.stack, "notifications", True)
        self.assertIn("solar-ntfy.example.com", self.ingress(d["tunnel_id"]))


class Sending(Base):
    def test_posts_to_the_box_with_its_token(self):
        self.remote_access()
        mod.form_save(self.stack, self.form())
        s = mod.settings(self.stack)
        self.assertEqual(notify.ntfy_target(self.stack, s),
                         ("http://127.0.0.1:2586", ntfyserver.topic(self.stack), ntfyserver.box_token(self.stack)))
        self.assertIn("ntfy", notify.channels_for(self.stack, s, "warning", False))
        with mock.patch.object(notify, "_post") as post:
            r = notify.send(self.stack, s, {"tag": "t", "title": "T", "message": "m", "priority": "critical"}, ["ntfy"])
        self.assertEqual(r, {"ntfy": "sent"})
        url, body, hdr = post.call_args[0]
        self.assertEqual(url, "http://127.0.0.1:2586")
        self.assertEqual((body["topic"], body["priority"]), (ntfyserver.topic(self.stack), 5))
        self.assertEqual(hdr, {"Authorization": f"Bearer {ntfyserver.box_token(self.stack)}"})

    def test_not_ready_without_remote_access(self):
        s = {**notify.DEFAULTS, "ntfy": True, "ntfy_box": True}
        self.assertIsNone(notify.ntfy_target(self.stack, s))
        self.assertNotIn("ntfy", notify.channels_for(self.stack, s, "critical", False))
        self.assertTrue(notify.send(self.stack, s, {"tag": "t", "title": "", "message": "m", "priority": "info"},
                                    ["ntfy"])["ntfy"].startswith("failed"))

    def test_public_server_unchanged(self):
        s = {**notify.DEFAULTS, "ntfy": True, "ntfy_topic": "solar-abc", "ntfy_token": "tk_x"}
        self.assertEqual(notify.ntfy_target(self.stack, s), ("https://ntfy.sh", "solar-abc", "tk_x"))

    def test_new_phone_password_route(self):
        self.remote_access()
        mod.form_save(self.stack, self.form())
        old = ntfyserver.phone_password(self.stack)
        r = mod.ntfy_password(self.stack, {})
        self.assertNotEqual(ntfyserver.phone_password(self.stack), old)
        self.assertIn("New ntfy phone password", r.setup_msg)
        self.assertTrue(re.match(r"^[a-z0-9]{5}(-[a-z0-9]{5}){3}$", ntfyserver.phone_password(self.stack)))


if __name__ == "__main__":
    unittest.main()
