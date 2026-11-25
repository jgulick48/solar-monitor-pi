"""Tests for tools/support-helper.py against an in-memory fake of the Cloudflare API.
Run: python3 -m unittest discover -s tests -v"""
import argparse
import base64
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "setup"))
import support  # noqa: E402
import tunnel  # noqa: E402

spec = importlib.util.spec_from_file_location("support_helper", os.path.join(ROOT, "tools", "support-helper.py"))
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

KEY = "ssh-ed25519 " + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"\x07" * 32).decode() + " helper"


class FakeCF:
    """Just enough of the API: tunnels, their config and token, DNS, Access policies, apps and service tokens."""

    def __init__(self):
        self.tunnels, self.configs, self.dns, self.policies, self.apps, self.svc = {}, {}, {}, {}, {}, {}
        self.n = 0

    def new_id(self):
        self.n += 1
        return f"id{self.n}"

    def __call__(self, tok, method, path, body=None, query=None, timeout=20):
        assert tok == "api-token"
        q = query or {}
        parts = path.strip("/").split("/")
        if path == "/zones":
            return [{"id": "zone1"}] if q.get("name") == "example.com" else []
        if parts[0] == "zones":                                        # /zones/z/dns_records[/id]
            if method == "GET":
                return [r for r in self.dns.values() if r["name"] == q.get("name")]
            if method == "POST":
                rid = self.new_id()
                self.dns[rid] = {**body, "id": rid}
                return self.dns[rid]
            if method == "DELETE":
                del self.dns[parts[3]]
                return {}
            self.dns[parts[3]].update(body)
            return self.dns[parts[3]]
        kind = parts[2]
        if kind == "cfd_tunnel":
            if len(parts) == 3 and method == "GET":
                return [t for t in self.tunnels.values() if q.get("name") in (None, t["name"])]
            if len(parts) == 3:
                tid = self.new_id()
                self.tunnels[tid] = {"id": tid, "name": body["name"], "status": "inactive"}
                return self.tunnels[tid]
            tid = parts[3]
            if len(parts) == 5 and parts[4] == "configurations":
                self.configs[tid] = body
                return {}
            if len(parts) == 5 and parts[4] == "token":
                return base64.b64encode(json.dumps({"a": "acct", "t": tid, "s": "c2VjcmV0"}).encode()).decode()
            if len(parts) == 5 and parts[4] == "connections":
                return {}
            if method == "DELETE":
                del self.tunnels[tid]
                return {}
            if method == "PATCH":
                self.tunnels[tid].update(body)
            return self.tunnels.get(tid)
        store = {"policies": self.policies, "apps": self.apps, "service_tokens": self.svc}[parts[3]]
        if len(parts) == 4 and method == "GET":
            return list(store.values())
        if len(parts) == 4:
            oid = self.new_id()
            store[oid] = {**body, "id": oid}
            if store is self.svc:
                store[oid].update(client_id="cid.access", client_secret="svc-secret")
            return store[oid]
        if method == "DELETE":
            del store[parts[4]]
            return {}
        store[parts[4]] = {**body, "id": parts[4]}
        return store[parts[4]]


class Helper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.saved = (helper.TOKEN_FILE, helper.SERVICE_FILE, helper.CONFIG, helper.api, tunnel.api)
        helper.TOKEN_FILE, helper.SERVICE_FILE = os.path.join(d, "api"), os.path.join(d, "svc")
        helper.CONFIG = os.path.join(d, "cfg", "config.json")
        with open(helper.TOKEN_FILE, "w") as f:
            f.write("api-token\n")
        os.chmod(helper.TOKEN_FILE, 0o600)
        self.keyfile = os.path.join(d, "k.pub")
        with open(self.keyfile, "w") as f:
            f.write(KEY + "\n")
        self.cf = FakeCF()
        helper.api = tunnel.api = self.cf

    def tearDown(self):
        helper.TOKEN_FILE, helper.SERVICE_FILE, helper.CONFIG, helper.api, tunnel.api = self.saved
        self.tmp.cleanup()

    def run_cmd(self, cmd, **kw):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            getattr(helper, "cmd_" + cmd)(argparse.Namespace(**kw))
        return out.getvalue(), err.getvalue()

    def init(self):
        return self.run_cmd("init", account="acct", domain="example.com", email=["me@example.com"], key=[self.keyfile])

    def test_init_new_code_remove(self):
        self.init()
        self.assertEqual(os.stat(helper.SERVICE_FILE).st_mode & 0o777, 0o600)
        self.assertEqual(helper.service(), ("cid.access", "svc-secret"))
        self.init()                                                       # again: nothing duplicated
        self.assertEqual(len(self.cf.svc), 1)
        self.assertEqual(sorted(p["name"] for p in self.cf.policies.values()),
                         ["solar support: helpers", "solar support: service token"])
        out, _ = self.run_cmd("new", name="neighbor", out=None)
        token, keys = support.read_code(out.strip())                     # the code the box owner pastes
        tid = support.tunnel_id(token)
        self.assertEqual(keys, KEY)
        self.assertEqual(self.cf.tunnels[tid]["name"], "solar-support-neighbor")
        ing = self.cf.configs[tid]["config"]["ingress"]
        self.assertIn({"hostname": "neighbor-ssh.example.com", "service": "ssh://localhost:22", "originRequest": {}}, ing)
        self.assertEqual(ing[-1], {"service": "http_status:404"})
        self.assertEqual(sorted(r["name"] for r in self.cf.dns.values()),
                         sorted(f"neighbor-{a}.example.com" for a in helper.APPS))
        app, = self.cf.apps.values()
        self.assertEqual(len(app["destinations"]), 4)
        self.assertEqual(len(app["policies"]), 2)
        self.run_cmd("new", name="neighbor", out=None)                   # again: same tunnel, app and records
        self.assertEqual((len(self.cf.tunnels), len(self.cf.apps), len(self.cf.dns)), (1, 1, 4))
        f = os.path.join(self.tmp.name, "code")
        out, _ = self.run_cmd("code", name="neighbor", out=f)
        self.assertEqual(out, "")                                         # with --out nothing secret is printed
        self.assertEqual(os.stat(f).st_mode & 0o777, 0o600)
        with open(f) as fh:
            self.assertEqual(support.read_code(fh.read())[0], token)
        # a record someone else owns is never touched
        self.cf.dns["foreign"] = {"id": "foreign", "type": "A", "name": "other-ssh.example.com", "content": "1.2.3.4"}
        self.run_cmd("remove", name="neighbor", yes=True)
        self.assertEqual((self.cf.tunnels, self.cf.apps), ({}, {}))
        self.assertEqual(list(self.cf.dns), ["foreign"])

    def test_rename(self):
        """A box renamed: the same tunnel (so its support code keeps working), its new hostnames in the routes, DNS
        and Access app, the old hostnames' records gone, another box's untouched."""
        self.init()
        out, _ = self.run_cmd("new", name="debleed", out=None)
        tid = support.tunnel_id(support.read_code(out.strip())[0])
        self.run_cmd("new", name="other", out=None)
        self.run_cmd("rename", old="debleed", new="debeeld")
        self.assertEqual(self.cf.tunnels[tid]["name"], "solar-support-debeeld")
        out, _ = self.run_cmd("code", name="debeeld", out=None)
        self.assertEqual(support.tunnel_id(support.read_code(out.strip())[0]), tid)      # the same tunnel and token
        hosts = {i["hostname"] for i in self.cf.configs[tid]["config"]["ingress"] if "hostname" in i}
        self.assertEqual(hosts, {f"debeeld-{a}.example.com" for a in helper.APPS})
        names = sorted(r["name"] for r in self.cf.dns.values())
        self.assertEqual(names, sorted([f"debeeld-{a}.example.com" for a in helper.APPS] +
                                       [f"other-{a}.example.com" for a in helper.APPS]))
        apps = sorted(a["name"] for a in self.cf.apps.values())
        self.assertEqual(apps, ["solar-support-debeeld", "solar-support-other"])
        app = [a for a in self.cf.apps.values() if a["name"] == "solar-support-debeeld"][0]
        self.assertEqual({d["uri"] for d in app["destinations"]}, hosts)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            self.run_cmd("rename", old="debeeld", new="other")                            # taken
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            self.run_cmd("rename", old="debleed", new="x")                                # gone

    def test_refuses_readable_token_and_bad_names(self):
        os.chmod(helper.TOKEN_FILE, 0o644)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            helper.token()
        for bad in ("Neighbor", "a.b", "-x", ""):
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                helper.check_name(bad)


if __name__ == "__main__":
    unittest.main()
