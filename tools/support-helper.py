#!/usr/bin/env python3
"""Remote support, the helper's side: set up a box's support tunnel in your Cloudflare account in one command.

The box owner then pastes one support code on their setup page (Remote support) and presses the button.

Once:
  1. Cloudflare dashboard -> My Profile -> API Tokens -> Create Token -> Custom token, with:
       Account  Cloudflare Tunnel            Edit
       Account  Access: Apps and Policies    Edit
       Account  Access: Service Tokens       Edit
       Zone     DNS                          Edit   (your domain)
       Zone     Zone                         Read   (your domain)
     Save it in a file only you can read:  install -m 600 /dev/null ~/.cache/cf-solar-support-api  then paste it in.
  2. support-helper.py init --account <account id> --domain example.com --email you@example.com \\
                            --key ~/.ssh/id_ed25519.pub [--key ...]
     Creates (or reuses) two Access policies: the emails, and a service token for logging in without a browser
     (kept in ~/.cache/cf-solar-support-service, mode 600).

Per box:
  support-helper.py new  NAME [--out FILE]   tunnel "solar-support-NAME", hostnames NAME-ssh / NAME-setup /
                                             NAME-grafana / NAME-ha on your domain, DNS, an Access app with
                                             your policies; prints the support code (or writes it to FILE)
  support-helper.py code NAME [--out FILE]   the support code again
  support-helper.py rename OLD NEW         rename a box: its tunnel, hostnames and Access app; the box keeps
                                             its support code
  support-helper.py ha   NAME open|protected   Home Assistant's hostname (NAME-ha) without the Cloudflare login, so
                                             only its own login page stands in front of it (and its phone app
                                             can connect); or back behind Cloudflare Access. New boxes start
                                             protected.
  support-helper.py ssh  NAME [ssh args]     log in as solar through the tunnel (needs cloudflared)
  support-helper.py get  NAME PATH [--app setup|grafana|ha]   fetch a page through Access with the service token
  support-helper.py list                     boxes, their tunnel status and links
  support-helper.py remove NAME              delete the box's Access app, DNS records and tunnel

The support code holds the tunnel's run token: anyone with it can attach a connector to that tunnel (but gets
nothing past Cloudflare Access). Send it privately, and `remove` the box when you're done helping for good.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "setup"))
import support  # noqa: E402
import tunnel  # noqa: E402
from tunnel import CFError, api  # noqa: E402

HOME = os.path.expanduser("~")
TOKEN_FILE = os.environ.get("SOLAR_SUPPORT_API_TOKEN_FILE", os.path.join(HOME, ".cache", "cf-solar-support-api"))
SERVICE_FILE = os.environ.get("SOLAR_SUPPORT_SERVICE_FILE", os.path.join(HOME, ".cache", "cf-solar-support-service"))
CONFIG = os.environ.get("SOLAR_SUPPORT_CONFIG", os.path.join(HOME, ".config", "solar-support", "config.json"))
KNOWN_HOSTS = os.path.join(HOME, ".ssh", "known_hosts.solar-support")
APPS = {"ssh": "ssh://localhost:22", "setup": "http://localhost:80", "grafana": "http://localhost:3000",
        "ha": "http://localhost:8123"}
PREFIX = "solar-support-"


def die(msg):
    print(f"support-helper: {msg}", file=sys.stderr)
    sys.exit(1)


def read_private(path, what):
    try:
        st = os.stat(path)
    except FileNotFoundError:
        die(f"{what} not found: {path} (see --help)")
    if st.st_mode & 0o077:
        die(f"{path} can be read by others; chmod 600 it first")
    with open(path) as f:
        return f.read()


def write_private(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(path + ".tmp", path)


def token():
    t = read_private(TOKEN_FILE, "Cloudflare API token").strip()
    if not t:
        die(f"{TOKEN_FILE} is empty")
    return t


def config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except FileNotFoundError:
        die(f"not set up yet: run `support-helper.py init` first ({CONFIG} is missing)")


def service():
    env = dict(line.split("=", 1) for line in read_private(SERVICE_FILE, "service token").split("\n") if "=" in line)
    return env["CF_ACCESS_CLIENT_ID"].strip(), env["CF_ACCESS_CLIENT_SECRET"].strip()


def check_name(name):
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?", name or ""):
        die("a box name is lowercase letters, digits and dashes (up to 32), e.g. neighbor")
    return name


def host(cfg, name, app):
    return f"{name}-{app}.{cfg['domain']}"


def find_tunnel(tok, acct, name):
    found = api(tok, "GET", f"/accounts/{acct}/cfd_tunnel", query={"name": PREFIX + name, "is_deleted": "false"})
    return found[0] if found else None


def find_app(tok, acct, name):
    for a in api(tok, "GET", f"/accounts/{acct}/access/apps") or []:
        if a.get("name") == PREFIX + name:
            return a
    return None


def find_policy(tok, acct, pname):
    for p in api(tok, "GET", f"/accounts/{acct}/access/policies") or []:
        if p.get("name") == pname:
            return p
    return None


# --------------------------------------------------------------------------
def cmd_init(a):
    tok = token()
    keys = []
    for k in a.key:
        with open(os.path.expanduser(k)) as f:
            keys.append(support.clean_keys(f.read()))
    keys = support.clean_keys("\n".join(keys))
    acct = a.account
    zone = tunnel.find_zone(tok, acct, a.domain)
    include = [{"email": {"email": e}} for e in a.email]
    people = find_policy(tok, acct, "solar support: helpers")
    body = {"name": "solar support: helpers", "decision": "allow", "include": include, "session_duration": "24h"}
    people = (api(tok, "PUT", f"/accounts/{acct}/access/policies/{people['id']}", body) if people
              else api(tok, "POST", f"/accounts/{acct}/access/policies", body))
    # the service token: made once (its secret is only shown when it's created)
    svc_id = None
    if os.path.exists(SERVICE_FILE):
        cid, _ = service()
        for t in api(tok, "GET", f"/accounts/{acct}/access/service_tokens") or []:
            if t.get("client_id") == cid:
                svc_id = t["id"]
    if not svc_id:
        t = api(tok, "POST", f"/accounts/{acct}/access/service_tokens", {"name": "solar-support", "duration": "8760h"})
        write_private(SERVICE_FILE, f"CF_ACCESS_CLIENT_ID={t['client_id']}\nCF_ACCESS_CLIENT_SECRET={t['client_secret']}\n")
        svc_id = t["id"]
        print(f"service token created (good for a year), kept in {SERVICE_FILE}")
    body = {"name": "solar support: service token", "decision": "non_identity",
            "include": [{"service_token": {"token_id": svc_id}}]}
    sp = find_policy(tok, acct, body["name"])
    sp = (api(tok, "PUT", f"/accounts/{acct}/access/policies/{sp['id']}", body) if sp
          else api(tok, "POST", f"/accounts/{acct}/access/policies", body))
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump({"account_id": acct, "domain": a.domain, "zone_id": zone, "emails": a.email, "ssh_keys": keys,
                   "policies": [people["id"], sp["id"]]}, f, indent=2)
    print(f"ready: {a.domain}, {len(a.email)} helper email(s), {len(keys.splitlines())} SSH key(s). Next: support-helper.py new NAME")


def cmd_new(a):
    cfg, tok, name = config(), token(), check_name(a.name)
    tid = tunnel.ensure_tunnel(tok, cfg["account_id"], PREFIX + name)
    _route(cfg, tok, name, tid)
    cmd_code(a)


def _ha_open(app):
    """True when an Access app leaves Home Assistant's hostname out: it has only its own login."""
    return bool(app) and not any(d.get("uri", "").split(".")[0].endswith("-ha") for d in app.get("destinations") or [])


def _route(cfg, tok, name, tid, app=None, ha_open=None):
    """Point NAME's hostnames at the tunnel (its ingress and DNS) and put them behind the Access app (app: the
    existing one to update, else found by NAME or made). ha_open: leave Home Assistant's hostname out of the
    Access app (its own login only); None keeps what the app has now (a new box: protected)."""
    acct = cfg["account_id"]
    app = app or find_app(tok, acct, name)
    ha_open = _ha_open(app) if ha_open is None else ha_open
    hosts = {app: host(cfg, name, app) for app in APPS}
    ingress = [{"hostname": h, "service": APPS[app], "originRequest": {}} for app, h in hosts.items()]
    api(tok, "PUT", f"/accounts/{acct}/cfd_tunnel/{tid}/configurations",
        {"config": {"ingress": ingress + [{"service": "http_status:404"}]}})
    for h in hosts.values():
        tunnel.ensure_dns(tok, cfg["zone_id"], h, tid)
    body = {"name": PREFIX + name, "type": "self_hosted", "domain": hosts["setup"],
            "destinations": [{"type": "public", "uri": h} for k, h in hosts.items() if not (ha_open and k == "ha")],
            "session_duration": "24h", "app_launcher_visible": False,
            "policies": [{"id": pid, "precedence": i + 1} for i, pid in enumerate(cfg["policies"])]}
    if app:
        api(tok, "PUT", f"/accounts/{acct}/access/apps/{app['id']}", body)
    else:
        api(tok, "POST", f"/accounts/{acct}/access/apps", body)
    print(f"box {name}: tunnel {tid}, {', '.join(hosts.values())}, behind Cloudflare Access"
          + (f" except {hosts['ha']} (Home Assistant's own login)" if ha_open else ""), file=sys.stderr)


def ha_owner(cfg, name):
    """Whether the box's Home Assistant has its owner account, asked through the tunnel (/api/onboarding, which
    needs no login): True, False, or None when it can't be told (the box isn't connected)."""
    cid, secret = service()
    req = urllib.request.Request(f"https://{host(cfg, name, 'ha')}/api/onboarding",
                                 headers={"CF-Access-Client-Id": cid, "CF-Access-Client-Secret": secret,
                                          "User-Agent": "solar-support-helper/1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            steps = json.loads(r.read())
        return any(x.get("step") == "user" and x.get("done") for x in steps)
    except urllib.error.HTTPError as e:
        return True if e.code == 404 else None          # 404: onboarding is finished and its pages are gone
    except (OSError, ValueError, AttributeError):
        return None


def cmd_ha(a):
    """Home Assistant's hostname in or out of the Access app. Opening it is refused while Home Assistant has no
    owner account: without the Cloudflare login, the first visitor could create it."""
    cfg, tok, name = config(), token(), check_name(a.name)
    t = find_tunnel(tok, cfg["account_id"], name)
    if not t:
        die(f"no box {name}")
    if a.state == "open" and not a.force:
        owner = ha_owner(cfg, name)
        if owner is False:
            die(f"{name}'s Home Assistant has no owner account yet: anyone could claim it. Create the account first.")
        if owner is None:
            die(f"couldn't reach {name}'s Home Assistant to check it has an owner account (is the box connected?). "
                "Try again, or --force if you know it has one.")
    _route(cfg, tok, name, t["id"], ha_open=a.state == "open")


def cmd_rename(a):
    """Rename a box: the same tunnel (its token, so the box's support code keeps working), renamed, with the new
    hostnames in its routes, DNS and Access app; the old hostnames' DNS records go."""
    cfg, tok = config(), token()
    old, new = check_name(a.old), check_name(a.new)
    acct = cfg["account_id"]
    t = find_tunnel(tok, acct, old)
    if not t:
        die(f"no box {old}")
    if find_tunnel(tok, acct, new):
        die(f"there's already a box {new}")
    app = find_app(tok, acct, old)
    api(tok, "PATCH", f"/accounts/{acct}/cfd_tunnel/{t['id']}", {"name": PREFIX + new})
    _route(cfg, tok, new, t["id"], app)
    for app_name in APPS:                  # the old names: only records pointing at this box's tunnel
        for r in api(tok, "GET", f"/zones/{cfg['zone_id']}/dns_records", query={"name": host(cfg, old, app_name)}) or []:
            if r["type"] == "CNAME" and r["content"] == tunnel.target(t["id"]):
                api(tok, "DELETE", f"/zones/{cfg['zone_id']}/dns_records/{r['id']}")
    print(f"box {old} is now {new}; the box needs nothing (same tunnel and support code)", file=sys.stderr)


def cmd_code(a):
    cfg, tok, name = config(), token(), check_name(a.name)
    t = find_tunnel(tok, cfg["account_id"], name)
    if not t:
        die(f"no box {name}: run `support-helper.py new {name}`")
    run_token = api(tok, "GET", f"/accounts/{cfg['account_id']}/cfd_tunnel/{t['id']}/token")
    code = support.make_code(run_token, cfg["ssh_keys"])
    if a.out:
        write_private(os.path.expanduser(a.out), code + "\n")
        print(f"support code written to {a.out} (mode 600). Send it privately; it goes in Setup -> Remote support.", file=sys.stderr)
    else:
        print("Support code (paste in Setup -> Remote support on the box; send it privately):", file=sys.stderr)
        print(code)


def cmd_ssh(a):
    cfg, name = config(), check_name(a.name)
    cf = shutil.which("cloudflared") or (os.path.join(HOME, ".local", "bin", "cloudflared")
                                         if os.path.exists(os.path.join(HOME, ".local", "bin", "cloudflared")) else None)
    if not cf:
        die("cloudflared isn't installed (https://github.com/cloudflare/cloudflared/releases)")
    cid, secret = service()
    proxy = f"{cf} access ssh --hostname %h --service-token-id {cid} --service-token-secret {secret}"
    args = ["ssh", "-o", f"ProxyCommand={proxy}", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
            "-o", "StrictHostKeyChecking=accept-new", f"solar@{host(cfg, name, 'ssh')}", *a.rest]
    os.execvp("ssh", args)


def cmd_get(a):
    cfg, name = config(), check_name(a.name)
    cid, secret = service()
    url = f"https://{host(cfg, name, a.app)}{a.path if a.path.startswith('/') else '/' + a.path}"
    # Cloudflare refuses Python's default User-Agent (error 1010, "browser signature banned")
    req = urllib.request.Request(url, headers={"CF-Access-Client-Id": cid, "CF-Access-Client-Secret": secret,
                                               "User-Agent": "solar-support-helper/1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            sys.stdout.buffer.write(r.read())
    except urllib.error.HTTPError as e:
        die(f"{url}: {e.code} {e.reason}")
    except OSError as e:
        die(f"{url}: {e}")


def cmd_list(a):
    cfg, tok = config(), token()
    found = api(tok, "GET", f"/accounts/{cfg['account_id']}/cfd_tunnel", query={"is_deleted": "false"}) or []
    boxes = sorted((t for t in found if t["name"].startswith(PREFIX)), key=lambda t: t["name"])
    apps = {x.get("name"): x for x in api(tok, "GET", f"/accounts/{cfg['account_id']}/access/apps") or []}
    if not boxes:
        print("no boxes yet")
    for t in boxes:
        name = t["name"][len(PREFIX):]
        state = {"healthy": "connected", "inactive": "never connected", "down": "not connected"}.get(t.get("status"), t.get("status"))
        ha = "  HA: own login only" if _ha_open(apps.get(t["name"])) else ""
        print(f"{name:20s} {state:16s} https://{host(cfg, name, 'setup')}  ssh: support-helper.py ssh {name}{ha}")


def cmd_remove(a):
    cfg, tok, name = config(), token(), check_name(a.name)
    acct = cfg["account_id"]
    if not a.yes:
        if input(f"Delete box {name}'s Access app, DNS records and tunnel? Its support code stops working. [y/N] ").strip().lower() != "y":
            die("left as it is")
    app = find_app(tok, acct, name)
    if app:
        api(tok, "DELETE", f"/accounts/{acct}/access/apps/{app['id']}")
    t = find_tunnel(tok, acct, name)
    if t:
        for app_name in APPS:              # only records pointing at this box's tunnel
            for r in api(tok, "GET", f"/zones/{cfg['zone_id']}/dns_records", query={"name": host(cfg, name, app_name)}) or []:
                if r["type"] == "CNAME" and r["content"] == tunnel.target(t["id"]):
                    api(tok, "DELETE", f"/zones/{cfg['zone_id']}/dns_records/{r['id']}")
        try:
            api(tok, "DELETE", f"/accounts/{acct}/cfd_tunnel/{t['id']}/connections")
        except CFError:
            pass
        api(tok, "DELETE", f"/accounts/{acct}/cfd_tunnel/{t['id']}")
    print(f"box {name} removed" if (app or t) else f"no box {name}")


def main():
    ap = argparse.ArgumentParser(prog="support-helper.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--account", required=True, help="Cloudflare account id (dashboard URL or Overview page)")
    p.add_argument("--domain", required=True)
    p.add_argument("--email", action="append", required=True, help="who may log in (repeatable)")
    p.add_argument("--key", action="append", required=True, help="SSH public key file (repeatable)")
    for c in ("new", "code"):
        p = sub.add_parser(c)
        p.add_argument("name")
        p.add_argument("--out", help="write the support code to this file (mode 600) instead of printing it")
    p = sub.add_parser("ssh")
    p.add_argument("name")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p = sub.add_parser("get")
    p.add_argument("name")
    p.add_argument("path")
    p.add_argument("--app", choices=["setup", "grafana", "ha"], default="setup")
    p = sub.add_parser("ha")
    p.add_argument("name")
    p.add_argument("state", choices=["open", "protected"])
    p.add_argument("--force", action="store_true", help="open without checking Home Assistant has an owner account")
    sub.add_parser("list")
    p = sub.add_parser("rename")
    p.add_argument("old")
    p.add_argument("new")
    p = sub.add_parser("remove")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    try:
        globals()["cmd_" + a.cmd](a)
    except CFError as e:
        die(str(e))
    except ValueError as e:
        die(str(e))


if __name__ == "__main__":
    main()
