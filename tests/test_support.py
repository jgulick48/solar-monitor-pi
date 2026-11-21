"""Tests for remote support (support.py) on a fake root filesystem with fake systemctl/compose.
Run: python3 -m unittest discover -s tests -v"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import solar_setup as ss  # noqa: E402
import support  # noqa: E402

TOKEN = base64.b64encode(json.dumps({"a": "acct", "t": "1234-tunnel", "s": "c2VjcmV0"}).encode()).decode().rstrip("=")


def ed25519(seed, comment):
    """A well-formed ssh-ed25519 public key line (32 made-up key bytes)."""
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([seed]) * 32
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}"


KEY = ed25519(1, "helper@laptop")
OWNER_KEY = ed25519(2, "owner")


class Support(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "root")
        self.stack = os.path.join(self.tmp.name, "stack")
        os.makedirs(os.path.join(self.stack, "state"))
        os.makedirs(os.path.join(self.root, "home", "solar"))
        self.old_root = support.ROOT
        support.ROOT = self.root
        self.ssh_on = False
        self.tunnel_up = False
        self.calls = []
        self.old_run = support.run
        support.run = self.fake_run
        os.environ["SOLAR_COMPOSE"] = "fake-compose"

    def tearDown(self):
        support.ROOT = self.old_root
        support.run = self.old_run
        os.environ.pop("SOLAR_COMPOSE", None)
        self.tmp.cleanup()

    def fake_run(self, cmd):
        self.calls.append(" ".join(cmd))
        if cmd[:3] == ["systemctl", "is-active", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 0 if self.ssh_on else 3, "", "")
        if "ps" in cmd and "cloudflared-support" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "c0ffee\n" if self.tunnel_up else "", "")
        if "up" in cmd and "cloudflared-support" in cmd:
            self.tunnel_up = True
        if cmd[:2] == ["systemctl", "restart"]:
            self.ssh_on = True
        if cmd[:2] == ["systemctl", "stop"]:
            self.ssh_on = False
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def f(self, rel):
        return os.path.join(self.root, rel)

    def form(self, **kw):
        f = {"support_token": TOKEN, "support_keys": KEY, "support_days": "3"}
        f.update(kw)
        return support.clean(f, support.settings(self.stack))

    def test_on_then_off_starts_and_stops_ssh(self):
        ak = self.f("home/solar/.ssh/authorized_keys")
        os.makedirs(os.path.dirname(ak))
        open(ak, "w").write(OWNER_KEY + "\n")
        msgs = support.enable(self.stack, self.form(), now=1000)
        self.assertIn("SSH started (this box only, keys only)", msgs)
        self.assertTrue(self.ssh_on)
        self.assertEqual(open(ak).read(), f"{OWNER_KEY}\n{support.MARK_BEGIN}\n{KEY}\n{support.MARK_END}\n")
        self.assertIn("NOPASSWD:ALL", open(self.f(support.SUDOERS)).read())
        self.assertEqual(os.stat(self.f(support.SUDOERS)).st_mode & 0o777, 0o440)
        self.assertIn("ListenAddress 127.0.0.1", open(self.f(support.SSHD_DROPIN)).read())
        self.assertEqual(open(support.env_path(self.stack)).read(), f"TUNNEL_TOKEN={TOKEN}\n")
        self.assertEqual(os.stat(support.env_path(self.stack)).st_mode & 0o777, 0o600)
        self.assertIn("COMPOSE_PROFILES=support\n", open(os.path.join(self.stack, ".env")).read())
        self.assertTrue(any("up -d cloudflared-support" in c for c in self.calls))
        st = support.status(self.stack, now=1000)
        self.assertEqual((st["enabled"], st["until"], st["tunnel_id"]), (True, 1000 + 3 * 86400, "1234-tunnel"))

        msgs = support.disable(self.stack)
        self.assertIn("SSH stopped", msgs)
        self.assertFalse(self.ssh_on)
        self.assertEqual(open(ak).read(), OWNER_KEY + "\n")       # the owner's own key stays
        for rel in (support.SUDOERS, support.SSHD_DROPIN):
            self.assertFalse(os.path.exists(self.f(rel)), rel)
        self.assertFalse(os.path.exists(support.env_path(self.stack)))
        self.assertNotIn("COMPOSE_PROFILES", open(os.path.join(self.stack, ".env")).read())
        self.assertTrue(any("rm -s -f cloudflared-support" in c for c in self.calls))

    def test_owner_ssh_left_alone(self):
        self.ssh_on = True                       # the owner turned SSH on themselves
        support.enable(self.stack, self.form())
        self.assertFalse(os.path.exists(self.f(support.SSHD_DROPIN)))
        self.assertFalse(any("systemctl restart" in c for c in self.calls))
        support.disable(self.stack)
        self.assertTrue(self.ssh_on)              # still on afterwards
        self.assertFalse(any("systemctl stop" in c for c in self.calls))

    def test_expiry_and_reboot(self):
        support.enable(self.stack, self.form(support_days="1"), now=1000)
        self.ssh_on = False                       # after a reboot: SSH started by support isn't running
        msgs = support.check(self.stack, now=1000 + 3600)
        self.assertTrue(self.ssh_on, msgs)
        self.assertTrue(support.status(self.stack)["enabled"])
        msgs = support.check(self.stack, now=1000 + 86400)
        self.assertIn("SSH stopped", msgs)
        st = support.status(self.stack)
        self.assertFalse(st["enabled"])
        self.assertIn("switched itself off", st["message"])
        self.assertEqual(support.check(self.stack, now=1000 + 2 * 86400), [])

    def test_until_turned_off(self):
        support.enable(self.stack, self.form(support_days="0"), now=1000)
        self.assertEqual(support.status(self.stack)["until"], 0)
        support.check(self.stack, now=1000 + 400 * 86400)
        self.assertTrue(support.status(self.stack)["enabled"])

    def test_validation(self):
        with self.assertRaisesRegex(ValueError, "isn't a Cloudflare tunnel token"):
            self.form(support_token="not-a-token")
        with self.assertRaisesRegex(ValueError, "looks like"):
            self.form(support_keys="hello world")
        for bad in ('command="rm -rf /" ' + KEY, 'from="*" ' + KEY, KEY + ' "quoted"'):
            with self.assertRaises(ValueError):     # no options in front, no quoting tricks
                self.form(support_keys=bad)
        with self.assertRaisesRegex(ValueError, "damaged"):
            self.form(support_keys="ssh-ed25519 not*base64 x")
        two = KEY + "\n\n  " + OWNER_KEY + "  \r\n"
        self.assertEqual(self.form(support_keys=two)["ssh_key"], KEY + "\n" + OWNER_KEY)
        self.assertEqual(self.form(support_days="99")["days"], 7)
        # an empty token field keeps the saved token
        support.save(self.stack, {**support.DEFAULTS, "token": TOKEN, "ssh_key": KEY})
        self.assertEqual(support.clean({"support_token": "", "support_keys": ""}, support.settings(self.stack))["token"], TOKEN)

    def test_new_token_while_on_switches_the_tunnel(self):
        support.enable(self.stack, self.form())
        self.assertTrue(self.tunnel_up)
        self.calls.clear()
        support.enable(self.stack, self.form())                       # saved again, same token: left running
        self.assertFalse([c for c in self.calls if " up " in f" {c} "])
        other = base64.b64encode(json.dumps({"a": "acct", "t": "5678-tunnel", "s": "c2VjcmV0"}).encode()).decode()
        msgs = support.enable(self.stack, self.form(support_token=other))
        self.assertTrue(any("--force-recreate" in c and c.endswith("cloudflared-support") for c in self.calls))
        self.assertIn("support tunnel switched to the new token", msgs)

    def test_support_code(self):
        code = support.make_code(TOKEN, KEY)
        self.assertTrue(code.startswith("SOLAR-SUPPORT-1:"))
        # pasted from an email: wrapped over lines, nothing in the keys box
        wrapped = "\n".join(code[i:i + 40] for i in range(0, len(code), 40))
        d = support.clean({"support_token": "  " + wrapped + " ", "support_keys": "", "support_days": "3"}, support.DEFAULTS)
        self.assertEqual((d["token"], d["tunnel_id"], d["ssh_key"], d["days"]), (TOKEN, "1234-tunnel", KEY, 3))
        # keys typed in the box are added once
        d = support.clean({"support_token": code, "support_keys": OWNER_KEY + "\n" + KEY}, support.DEFAULTS)
        self.assertEqual(d["ssh_key"], KEY + "\n" + OWNER_KEY)
        with self.assertRaisesRegex(ValueError, "support code is damaged"):
            support.clean({"support_token": code[:-10]}, support.DEFAULTS)
        bad = support.CODE_PREFIX + base64.urlsafe_b64encode(json.dumps({"t": "nope", "k": [KEY]}).encode()).decode()
        with self.assertRaisesRegex(ValueError, "isn't a Cloudflare tunnel token"):
            support.clean({"support_token": bad}, support.DEFAULTS)

    def test_profiles_combine_with_remote_access(self):
        ss.write_if_changed(os.path.join(self.stack, "state", "tunnel.json"), json.dumps({"enabled": True}), 0o600)
        support.enable(self.stack, self.form())
        self.assertIn("COMPOSE_PROFILES=tunnel,support\n", open(os.path.join(self.stack, ".env")).read())


class Page(unittest.TestCase):
    def test_setup_page_and_banner(self):
        import solar_web
        with tempfile.TemporaryDirectory() as stack:
            os.makedirs(os.path.join(stack, "state"))
            old = solar_web.STACK
            solar_web.STACK = stack
            try:
                html = solar_web.setup_page(solar_web.load_site())
                self.assertIn('action="/support"', html)
                self.assertEqual(solar_web.support_banner(), "")
                support.save(stack, {**support.DEFAULTS, "enabled": True, "until": 2_000_000_000, "token": TOKEN, "ssh_key": KEY})
                self.assertIn("Remote support is on", solar_web.support_banner())
                html = solar_web.setup_page(solar_web.load_site())
                self.assertNotIn(TOKEN, html)
                self.assertIn("Turn remote support off now", html)
            finally:
                solar_web.STACK = old


if __name__ == "__main__":
    unittest.main()
