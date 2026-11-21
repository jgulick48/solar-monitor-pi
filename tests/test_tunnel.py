"""Tests for remote access through a Cloudflare Tunnel, against a fake Cloudflare API.
Run: python3 -m unittest discover -s tests -v"""
import http.server
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import solar_setup as ss  # noqa: E402
import tunnel  # noqa: E402

ACCT = "0123456789abcdef0123456789abcdef"
TOKEN = "test-token-abcd"


class FakeCF:
    """Just enough of the Cloudflare v4 API: zones, cfd_tunnel, configurations, token, dns_records."""

    def __init__(self):
        self.zones = {"zone1": {"id": "zone1", "name": "example.com", "account": {"id": ACCT}}}
        self.tunnels, self.configs, self.dns, self.calls = {}, {}, {}, []
        self.n = 0

    def new_id(self, p):
        self.n += 1
        return f"{p}{self.n}"

    def handle(self, method, path, query, body):
        self.calls.append((method, path))
        a = f"/client/v4/accounts/{ACCT}"
        if path == "/client/v4/zones":
            return [z for z in self.zones.values() if z["name"] == query.get("name") and z["account"]["id"] == query.get("account.id")]
        if path == f"{a}/cfd_tunnel":
            if method == "POST":
                t = {"id": self.new_id("tun"), "name": body["name"], "deleted_at": None, "status": "inactive"}
                self.tunnels[t["id"]] = t
                return t
            return [t for t in self.tunnels.values() if t["name"] == query.get("name") and not t["deleted_at"]]
        m = re.fullmatch(f"{a}/cfd_tunnel/([^/]+)(/.*)?", path)
        if m:
            t = self.tunnels.get(m.group(1))
            if not t:
                raise KeyError("tunnel not found")
            sub = m.group(2) or ""
            if sub == "/configurations":
                self.configs[t["id"]] = body
                return {}
            if sub == "/token":
                return f"run-token-for-{t['id']}"
            if sub == "/connections":
                return {}
            if method == "DELETE":
                t["deleted_at"] = "now"
            return t
        m = re.fullmatch(r"/client/v4/zones/zone1/dns_records(?:/(.+))?", path)
        if m:
            rid = m.group(1)
            if method == "GET":
                return [r for r in self.dns.values() if r["name"] == query.get("name")]
            if method == "POST":
                r = {**body, "id": self.new_id("rec")}
                self.dns[r["id"]] = r
                return r
            if method in ("PUT", "PATCH"):
                self.dns[rid].update(body)
                return self.dns[rid]
            if method == "DELETE":
                return self.dns.pop(rid)
        raise KeyError(f"no route {method} {path}")


def serve(fake):
    class H(http.server.BaseHTTPRequestHandler):
        def go(self):
            u = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                code, doc = 403, {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}
            else:
                try:
                    code, doc = 200, {"success": True, "errors": [], "result": fake.handle(self.command, u.path, q, body)}
                except KeyError as e:
                    code, doc = 404, {"success": False, "errors": [{"code": 1003, "message": str(e)}]}
            data = json.dumps(doc).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = go

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Tunnel(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCF()
        self.srv = serve(self.fake)
        self.old_api = tunnel.API
        tunnel.API = f"http://127.0.0.1:{self.srv.server_port}/client/v4"
        os.environ["SOLAR_COMPOSE"] = "true"  # compose calls succeed and do nothing
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.tmp.name
        os.makedirs(os.path.join(self.stack, "state"))
        self.own_ha(True)

    def tearDown(self):
        tunnel.API = self.old_api
        self.srv.shutdown()
        self.tmp.cleanup()
        os.environ.pop("SOLAR_COMPOSE", None)

    def own_ha(self, done):
        p = os.path.join(self.stack, "homeassistant", ".storage")
        os.makedirs(p, exist_ok=True)
        with open(os.path.join(p, "onboarding"), "w") as f:
            json.dump({"data": {"done": ["user", "core_config"] if done else []}}, f)

    def form(self, **kw):
        f = {"cf_account": ACCT, "cf_domain": "example.com", "cf_name": "solar", "cf_token": TOKEN}
        f.update(kw)
        return tunnel.clean(f, tunnel.settings(self.stack))

    def test_enable_creates_tunnel_dns_and_token(self):
        d = tunnel.enable(self.stack, self.form())
        tid = d["tunnel_id"]
        self.assertTrue(d["enabled"])
        self.assertEqual(tunnel.public_url(self.stack), "https://solar.example.com")
        ing = self.fake.configs[tid]["config"]["ingress"]
        self.assertEqual(ing[0], {"hostname": "solar.example.com", "service": "http://localhost:8123", "originRequest": {}})
        self.assertEqual(ing[-1], {"service": "http_status:404"})
        [rec] = self.fake.dns.values()
        self.assertEqual((rec["type"], rec["name"], rec["content"], rec["proxied"]),
                         ("CNAME", "solar.example.com", f"{tid}.cfargotunnel.com", True))
        env = os.path.join(self.stack, "state", "tunnel.env")
        self.assertEqual(open(env).read(), f"TUNNEL_TOKEN=run-token-for-{tid}\n")
        self.assertEqual(os.stat(env).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(tunnel.settings_path(self.stack)).st_mode & 0o777, 0o600)
        self.assertIn("COMPOSE_PROFILES=tunnel\n", open(os.path.join(self.stack, ".env")).read())

    def test_enable_again_reuses_the_tunnel(self):
        tid = tunnel.enable(self.stack, self.form())["tunnel_id"]
        d = tunnel.enable(self.stack, self.form(cf_token=""))  # empty token field keeps the saved one
        self.assertEqual(d["tunnel_id"], tid)
        self.assertEqual(len(self.fake.tunnels), 1)
        self.assertEqual(len(self.fake.dns), 1)

    def test_renaming_moves_the_dns_record(self):
        prev = tunnel.enable(self.stack, self.form())
        d = tunnel.enable(self.stack, self.form(cf_name="power"), prev=prev)
        self.assertEqual([r["name"] for r in self.fake.dns.values()], ["power.example.com"])
        self.assertEqual(self.fake.configs[d["tunnel_id"]]["config"]["ingress"][0]["hostname"], "power.example.com")

    def test_never_replaces_someone_elses_record(self):
        self.fake.dns["x"] = {"id": "x", "type": "A", "name": "solar.example.com", "content": "203.0.113.5"}
        with self.assertRaisesRegex(tunnel.CFError, "already has a DNS record"):
            tunnel.enable(self.stack, self.form())
        self.assertEqual(self.fake.dns["x"]["content"], "203.0.113.5")
        self.assertFalse(tunnel.settings(self.stack)["enabled"])

    def test_refuses_before_ha_has_an_owner(self):
        self.own_ha(False)
        with self.assertRaisesRegex(tunnel.CFError, "Home Assistant account first"):
            tunnel.enable(self.stack, self.form())
        self.assertEqual(self.fake.calls, [])

    def test_wrong_domain_and_bad_token(self):
        with self.assertRaisesRegex(tunnel.CFError, "isn't on this Cloudflare account"):
            tunnel.enable(self.stack, self.form(cf_domain="example.org"))
        with self.assertRaisesRegex(tunnel.CFError, "Authentication error"):
            tunnel.enable(self.stack, self.form(cf_token="wrong"))

    def test_disable_cleans_up(self):
        tid = tunnel.enable(self.stack, self.form())["tunnel_id"]
        msgs = tunnel.disable(self.stack, remove=True)
        self.assertIn("tunnel deleted at Cloudflare", msgs)
        self.assertEqual(self.fake.dns, {})
        self.assertTrue(self.fake.tunnels[tid]["deleted_at"])
        self.assertFalse(os.path.exists(os.path.join(self.stack, "state", "tunnel.env")))
        self.assertEqual(tunnel.public_url(self.stack), "")
        self.assertNotIn("COMPOSE_PROFILES", open(os.path.join(self.stack, ".env")).read())

    def test_disable_keep_at_cloudflare(self):
        tunnel.enable(self.stack, self.form())
        tunnel.disable(self.stack, remove=False)
        self.assertEqual(len(self.fake.dns), 1)
        self.assertTrue(tunnel.settings(self.stack)["tunnel_id"])

    def test_validation(self):
        for bad, msg in [({"cf_account": "abc"}, "account ID"), ({"cf_domain": "localhost"}, "domain"),
                         ({"cf_name": "my site"}, "letters, digits"), ({"cf_token": ""}, "API token")]:
            with self.assertRaisesRegex(ValueError, msg):
                self.form(**bad)
        self.assertEqual(self.form(cf_domain="https://Example.COM/".rstrip("/"))["domain"], "example.com")


class Page(unittest.TestCase):
    def test_setup_page_has_remote_section_and_core_gets_external_url(self):
        import solar_web
        with tempfile.TemporaryDirectory() as stack:
            os.makedirs(os.path.join(stack, "state"))
            os.makedirs(os.path.join(stack, "homeassistant"))
            old = solar_web.STACK
            solar_web.STACK = stack
            try:
                site = {**solar_web.load_site(), "latitude": "40", "longitude": "-105"}
                html = solar_web.setup_page(site)
                self.assertIn('action="/tunnel"', html)
                self.assertIn("Create your Home Assistant account first", html)
                solar_web.write_core(site)
                self.assertNotIn("external_url", open(os.path.join(stack, "homeassistant", "core.yaml")).read())
                tunnel.save(stack, {**tunnel.DEFAULTS, "enabled": True, "domain": "example.com", "name": "solar",
                                    "api_token": "secret-token-wxyz"})
                self.assertTrue(solar_web.write_core(site))
                core = open(os.path.join(stack, "homeassistant", "core.yaml")).read()
                self.assertIn('external_url: "https://solar.example.com"\npackages:', core)
                html = solar_web.setup_page(site)
                self.assertNotIn("secret-token", html)
                self.assertIn("•" * 6 + "wxyz", html)
            finally:
                solar_web.STACK = old


if __name__ == "__main__":
    unittest.main()
