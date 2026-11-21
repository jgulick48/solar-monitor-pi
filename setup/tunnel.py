#!/usr/bin/env python3
"""Remote access to Home Assistant through a Cloudflare Tunnel (optional).

The setup page takes a Cloudflare account ID, a domain that is on that account, a name for
the address (solar -> solar.example.com) and an API token. With those this box:

  1. finds the domain's zone,
  2. creates a remotely managed tunnel (or reuses the one it made before),
  3. points the tunnel at Home Assistant only (http://localhost:8123; everything else 404),
  4. adds a proxied CNAME <name>.<domain> -> <tunnel id>.cfargotunnel.com,
  5. fetches the tunnel's run token and starts the `cloudflared` compose service
     (profile "tunnel", turned on through COMPOSE_PROFILES in .env).

The setup page, Grafana and the cameras are never published. The API token needs:
Account > Cloudflare Tunnel > Edit, Zone > DNS > Edit and Zone > Zone > Read.

Home Assistant is told to trust X-Forwarded-For from localhost (its .storage/http; YAML `http:` is
ignored since 2026.11) and gets the address as external_url; solar_web restarts it for both.

The settings (with the API token) are kept in state/tunnel.json and the run token in
state/tunnel.env, both mode 600.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solar_setup as ss  # noqa: E402

API = "https://api.cloudflare.com/client/v4"
SERVICE = "http://localhost:8123"
DEFAULTS = {"enabled": False, "account_id": "", "domain": "", "name": "solar", "api_token": "",
            "tunnel_id": "", "zone_id": "", "message": ""}


class CFError(Exception):
    pass


def settings_path(stack):
    return os.path.join(stack, "state", "tunnel.json")


def env_path(stack):
    return os.path.join(stack, "state", "tunnel.env")


def settings(stack):
    return {**DEFAULTS, **ss.load_json(settings_path(stack), {})}


def save(stack, d):
    ss.write_if_changed(settings_path(stack), json.dumps(d, indent=2) + "\n", 0o600)


def hostname(d):
    return f"{d['name']}.{d['domain']}" if d.get("name") else d.get("domain", "")


def public_url(stack):
    d = settings(stack)
    return f"https://{hostname(d)}" if d["enabled"] and d["domain"] else ""


def mask(token):
    return "•" * 6 + token[-4:] if token else ""


LOCAL_PROXIES = ["127.0.0.1/32", "::1/128"]


def _http_store(stack):
    return os.path.join(stack, "homeassistant", ".storage", "http")


def _proxy_slots(doc):
    data = (doc or {}).get("data") or {}
    return [c for c in (data.get("stable"), data.get("pending")) if isinstance(c, dict)]


def proxy_edit_needed(stack):
    """Home Assistant (2026.11+) keeps its HTTP settings in .storage/http, not YAML. cloudflared connects
    from localhost with X-Forwarded-For, which HA answers with 400 unless it trusts localhost as a proxy."""
    slots = _proxy_slots(ss.load_json(_http_store(stack), None))
    return any(not c.get("use_x_forwarded_for") or not set(LOCAL_PROXIES) <= set(c.get("trusted_proxies") or [])
               for c in slots)


def trust_local_proxy(stack):
    """Turn on X-Forwarded-For from localhost in .storage/http (keeping any other trusted proxies).
    Only while Home Assistant is stopped: it would overwrite the file. True when changed."""
    doc = ss.load_json(_http_store(stack), None)
    slots = _proxy_slots(doc)
    for c in slots:
        c["use_x_forwarded_for"] = True
        c["trusted_proxies"] = list(c.get("trusted_proxies") or []) + [p for p in LOCAL_PROXIES if p not in (c.get("trusted_proxies") or [])]
    return bool(slots) and ss.write_if_changed(_http_store(stack), json.dumps(doc, indent=2) + "\n", 0o600)


def ha_has_owner(stack):
    """True once someone has created the Home Assistant owner account (onboarding 'user' step).
    Before that, publishing HA would let anyone on the internet claim it."""
    ob = ss.load_json(os.path.join(stack, "homeassistant", ".storage", "onboarding"), {})
    return "user" in (ob.get("data") or {}).get("done", [])


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def _label_ok(s):
    return 0 < len(s) <= 63 and all(c.isalnum() or c == "-" for c in s) and not s.startswith("-") and not s.endswith("-")


def clean(form, cur):
    """Form fields -> settings; raises ValueError. An empty token field keeps the saved token."""
    acct = form.get("cf_account", "").strip().lower()
    domain = form.get("cf_domain", "").strip().lower().rstrip(".")
    for p in ("https://", "http://"):
        domain = domain.removeprefix(p)
    name = form.get("cf_name", "").strip().lower()
    token = form.get("cf_token", "").strip() or cur.get("api_token", "")
    if not (len(acct) == 32 and all(c in "0123456789abcdef" for c in acct)):
        raise ValueError("the account ID is the 32 letters and digits shown on the Cloudflare dashboard's overview page")
    if domain.count(".") < 1 or not all(_label_ok(p) for p in domain.split(".")):
        raise ValueError(f"'{domain}' doesn't look like a domain (e.g. example.com)")
    if name and not all(_label_ok(p) for p in name.split(".")):
        raise ValueError(f"'{name}' can only have letters, digits and dashes")
    if not token:
        raise ValueError("an API token is needed")
    if any(c.isspace() for c in token) or len(token) > 200:
        raise ValueError("that doesn't look like an API token")
    return {**cur, "account_id": acct, "domain": domain, "name": name, "api_token": token}


# --------------------------------------------------------------------------
# Cloudflare API
# --------------------------------------------------------------------------
def api(token, method, path, body=None, query=None, timeout=20):
    url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            doc = json.load(e)
        except (ValueError, OSError):
            raise CFError(f"Cloudflare said {e.code} {e.reason}")
    except (urllib.error.URLError, OSError) as e:
        raise CFError(f"can't reach Cloudflare: {getattr(e, 'reason', e)}")
    if not doc.get("success"):
        errs = "; ".join(f"{x.get('message')} ({x.get('code')})" for x in doc.get("errors") or []) or "unknown error"
        raise CFError(f"Cloudflare: {errs}")
    return doc.get("result")


def find_zone(token, account_id, domain):
    zones = api(token, "GET", "/zones", query={"name": domain, "account.id": account_id})
    if not zones:
        raise CFError(f"{domain} isn't on this Cloudflare account, or the token can't read it (it needs Zone > Zone > Read)")
    return zones[0]["id"]


def ensure_tunnel(token, acct, tunnel_name, tunnel_id=""):
    if tunnel_id:
        try:
            t = api(token, "GET", f"/accounts/{acct}/cfd_tunnel/{tunnel_id}")
            if t and not t.get("deleted_at"):
                return t["id"]
        except CFError:
            pass
    found = api(token, "GET", f"/accounts/{acct}/cfd_tunnel", query={"name": tunnel_name, "is_deleted": "false"})
    if found:
        return found[0]["id"]
    return api(token, "POST", f"/accounts/{acct}/cfd_tunnel", {"name": tunnel_name, "config_src": "cloudflare"})["id"]


def ingress(host):
    return {"config": {"ingress": [{"hostname": host, "service": SERVICE, "originRequest": {}},
                                   {"service": "http_status:404"}]}}


def target(tunnel_id):
    return f"{tunnel_id}.cfargotunnel.com"


def ensure_dns(token, zone_id, host, tunnel_id, ours=()):
    """CNAME host -> the tunnel. Replaces only a record that already points at this box's tunnel(s)."""
    want = {"type": "CNAME", "name": host, "content": target(tunnel_id), "proxied": True,
            "comment": "solar monitor remote access"}
    recs = api(token, "GET", f"/zones/{zone_id}/dns_records", query={"name": host})
    for r in recs or []:
        if r["type"] == "CNAME" and r["content"] == want["content"]:
            if not r.get("proxied"):
                api(token, "PATCH", f"/zones/{zone_id}/dns_records/{r['id']}", {"proxied": True})
            return r["id"]
        if r["type"] == "CNAME" and r["content"] in {target(t) for t in ours if t}:
            api(token, "PUT", f"/zones/{zone_id}/dns_records/{r['id']}", want)
            return r["id"]
        raise CFError(f"{host} already has a DNS record ({r['type']} {r['content']}); "
                      "choose another name, or delete that record in Cloudflare first")
    return api(token, "POST", f"/zones/{zone_id}/dns_records", want)["id"]


def _delete_dns(d, tunnel_id):
    """Delete d's CNAME if (and only if) it points at this tunnel. True when one was deleted."""
    if not d.get("zone_id"):
        return False
    gone = False
    for r in api(d["api_token"], "GET", f"/zones/{d['zone_id']}/dns_records", query={"name": hostname(d)}) or []:
        if r["type"] == "CNAME" and r["content"] == target(tunnel_id):
            api(d["api_token"], "DELETE", f"/zones/{d['zone_id']}/dns_records/{r['id']}")
            gone = True
    return gone


def tunnel_status(stack, timeout=5):
    """Cloudflare's view of the tunnel: healthy, degraded, down or inactive; '' when unknown."""
    d = settings(stack)
    if not (d["enabled"] and d["tunnel_id"]):
        return ""
    try:
        return api(d["api_token"], "GET", f"/accounts/{d['account_id']}/cfd_tunnel/{d['tunnel_id']}", timeout=timeout).get("status", "")
    except CFError:
        return ""


# --------------------------------------------------------------------------
# Turning it on and off
# --------------------------------------------------------------------------
def compose(stack, *args):
    try:
        return subprocess.run(ss.compose_cmd(stack) + list(args), capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def enable(stack, d, prev=None):
    """Set up the tunnel and start cloudflared. Returns the settings saved; raises CFError.
    prev: the settings before this change (its DNS record is removed when the address changed)."""
    if not ha_has_owner(stack):
        raise CFError("create your Home Assistant account first (open Home Assistant on this network); "
                      "until then anyone who found the address could claim it")
    token, acct, host = d["api_token"], d["account_id"], hostname(d)
    zone = find_zone(token, acct, d["domain"])
    old = d.get("tunnel_id", "")
    tid = ensure_tunnel(token, acct, f"solar-monitor {host}", old)
    api(token, "PUT", f"/accounts/{acct}/cfd_tunnel/{tid}/configurations", ingress(host))
    ensure_dns(token, zone, host, tid, ours=(old,))
    if prev and prev.get("enabled") and prev.get("zone_id") and hostname(prev) != host:
        _delete_dns(prev, tid)
    run_token = api(token, "GET", f"/accounts/{acct}/cfd_tunnel/{tid}/token")
    ss.write_if_changed(env_path(stack), f"TUNNEL_TOKEN={run_token}\n", 0o600)
    d = {**d, "enabled": True, "zone_id": zone, "tunnel_id": tid, "message": ""}
    save(stack, d)
    ss.write_env(stack, ss.ensure_secrets(stack))
    r = compose(stack, "up", "-d", "--force-recreate", "cloudflared")
    if r.returncode != 0:
        d["message"] = "the tunnel is set up but cloudflared didn't start: " + r.stderr.strip()[-300:]
        save(stack, d)
    return d


def disable(stack, remove=True):
    """Stop cloudflared; with remove, also delete the DNS record and the tunnel at Cloudflare.
    Returns a list of messages (Cloudflare errors are reported, not raised: it is off locally either way)."""
    d = settings(stack)
    msgs = []
    d["enabled"] = False
    save(stack, d)
    ss.write_env(stack, ss.ensure_secrets(stack))
    compose(stack, "rm", "-s", "-f", "cloudflared")
    try:
        os.remove(env_path(stack))
    except FileNotFoundError:
        pass
    msgs.append("remote access off")
    if remove and d["tunnel_id"] and d["api_token"]:
        token, acct, tid = d["api_token"], d["account_id"], d["tunnel_id"]
        try:
            if _delete_dns(d, tid):
                msgs.append(f"DNS record {hostname(d)} deleted")
            api(token, "DELETE", f"/accounts/{acct}/cfd_tunnel/{tid}/connections")
            api(token, "DELETE", f"/accounts/{acct}/cfd_tunnel/{tid}")
            msgs.append("tunnel deleted at Cloudflare")
            d["tunnel_id"] = d["zone_id"] = ""
        except CFError as e:
            msgs.append(f"couldn't clean up at Cloudflare ({e}); delete the tunnel there by hand")
        save(stack, d)
    return msgs
