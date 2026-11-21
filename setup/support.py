#!/usr/bin/env python3
"""Remote support: let a helper reach this box through *their* Cloudflare Tunnel, for troubleshooting.

The helper creates a tunnel in their own Cloudflare account (Zero Trust -> Networks -> Tunnels), adds
public hostnames for what they need (ssh://localhost:22, http://localhost:80 for this setup page,
http://localhost:3000 for Grafana, http://localhost:8123 for Home Assistant), protects them with
Cloudflare Access, and gives the box owner the tunnel's token and an SSH public key. On the setup page
the owner pastes both and picks when support switches itself off. While it is on the box:

  - runs a second cloudflared (compose service "cloudflared-support", profile "support") with that token;
  - adds the key to the `solar` account (a marked block in ~solar/.ssh/authorized_keys);
  - lets `solar` use sudo without a password (/etc/sudoers.d/solar-support);
  - starts SSH if it wasn't running, listening on localhost only and for keys only
    (/etc/ssh/sshd_config.d/solar-support.conf). If the owner had turned SSH on themselves,
    their SSH setup is left as it is.

Turning it off, by hand or when the time is up (checked by the collector every minute), undoes all of
it: the tunnel stops, the key and the sudo rule go, and SSH is stopped again if support started it.
Settings: state/support.json and the run token in state/support.env (both mode 600).
"""
import base64
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solar_setup as ss  # noqa: E402

ROOT = os.environ.get("SOLAR_SUPPORT_ROOT", "/")      # tests point this at a temporary directory
USER = "solar"
MARK_BEGIN = "# --- solar monitor remote support (removed when support is turned off) ---"
MARK_END = "# --- end solar monitor remote support ---"
SSHD_DROPIN = "etc/ssh/sshd_config.d/solar-support.conf"
SUDOERS = "etc/sudoers.d/solar-support"
SSHD_CONF = ("# Written by the solar monitor while remote support is on; removed when it is turned off.\n"
             "ListenAddress 127.0.0.1\nListenAddress ::1\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n")
DAYS = [1, 3, 7, 14, 30, 0]       # 0 = until turned off
DEFAULTS = {"enabled": False, "token": "", "ssh_key": "", "days": 7, "until": 0, "since": 0, "started_ssh": False,
            "tunnel_id": "", "message": ""}


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def p(rel):
    return os.path.join(ROOT, rel)


def settings_path(stack):
    return os.path.join(stack, "state", "support.json")


def env_path(stack):
    return os.path.join(stack, "state", "support.env")


def settings(stack):
    return {**DEFAULTS, **ss.load_json(settings_path(stack), {})}


def save(stack, d):
    ss.write_if_changed(settings_path(stack), json.dumps(d, indent=2) + "\n", 0o600)


def home():
    return p(f"home/{USER}")


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def tunnel_id(token):
    """A Cloudflare tunnel token is base64 JSON {"a": account, "t": tunnel id, "s": secret}. Raises ValueError."""
    t = token.strip()
    try:
        d = json.loads(base64.b64decode(t + "=" * (-len(t) % 4)))
        if not (isinstance(d, dict) and d.get("a") and d.get("t") and d.get("s")):
            raise ValueError
        return str(d["t"])
    except (ValueError, TypeError):
        raise ValueError("that isn't a Cloudflare tunnel token (Zero Trust -> Networks -> Tunnels -> the tunnel -> "
                         "install command: the long string after 'cloudflared service install')")


KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
             "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")


def clean_keys(text):
    """One or more SSH public keys, one per line. Raises ValueError."""
    keys = []
    for line in (text or "").replace("\r", "").split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2 or parts[0] not in KEY_TYPES or len(line) > 2000:
            raise ValueError("an SSH public key looks like 'ssh-ed25519 AAAA... name' (one per line)")
        try:
            base64.b64decode(parts[1], validate=True)
        except ValueError:
            raise ValueError("that SSH public key is damaged (check it was copied whole)")
        if any(c in line for c in "\"'\\") or line.startswith(("command=", "from=")):
            raise ValueError("only plain public keys, without options")
        keys.append(" ".join(parts[:3]))
    if not keys:
        raise ValueError("an SSH public key is needed")
    return "\n".join(keys)


CODE_PREFIX = "SOLAR-SUPPORT-1:"


def make_code(token, keys):
    """The one-paste support code tools/support-helper.py hands out: the tunnel token plus the helper's SSH keys."""
    body = json.dumps({"t": token.strip(), "k": [k for k in clean_keys(keys).split("\n")]}, separators=(",", ":"))
    return CODE_PREFIX + base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")


def read_code(text):
    """(token, keys) from a support code; None if text isn't one. Raises ValueError for a damaged code."""
    t = "".join((text or "").split())             # pasted from a chat or email: line breaks and spaces crept in
    if not t.upper().startswith(CODE_PREFIX):
        return None
    b = t[len(CODE_PREFIX):]
    try:
        d = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
        token, keys = str(d["t"]), "\n".join(d["k"])
    except (ValueError, TypeError, KeyError):
        raise ValueError("that support code is damaged (check it was copied whole)")
    tunnel_id(token)
    return token, clean_keys(keys)


def clean(form, cur):
    code = read_code(form.get("support_token", ""))
    if code:                                      # the support code carries both; keys typed below are added
        token, keys = code
        extra = form.get("support_keys", "").strip()
        if extra:
            keys = clean_keys(keys + "\n" + extra)
        keys = "\n".join(dict.fromkeys(keys.split("\n")))
    else:
        token = form.get("support_token", "").strip() or cur.get("token", "")
        if not token:
            raise ValueError("the support code (or a tunnel token) is needed")
        keys = None
    tid = tunnel_id(token)
    if keys is None:
        keys = clean_keys(form.get("support_keys", "") or cur.get("ssh_key", ""))
    try:
        days = int(form.get("support_days", "7"))
    except ValueError:
        days = 7
    if days not in DAYS:
        days = 7
    return {**cur, "token": token, "tunnel_id": tid, "ssh_key": keys, "days": days}


# --------------------------------------------------------------------------
# The changes on the box
# --------------------------------------------------------------------------
def _write(path, text, mode):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return ss.write_if_changed(path, text, mode)


def _remove(path):
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


def _set_keys(keys):
    """Replace the marked block in ~solar/.ssh/authorized_keys (keys = '' removes it). Other keys stay."""
    ak = os.path.join(home(), ".ssh", "authorized_keys")
    try:
        lines = open(ak).read().split("\n")
    except FileNotFoundError:
        lines = []
    out, skip = [], False
    for line in lines:
        if line == MARK_BEGIN:
            skip = True
        elif line == MARK_END:
            skip = False
        elif not skip:
            out.append(line)
    while out and out[-1] == "":
        out.pop()
    if keys:
        out += [MARK_BEGIN, *keys.split("\n"), MARK_END]
    if not out and not os.path.exists(ak):
        return
    os.makedirs(os.path.dirname(ak), mode=0o700, exist_ok=True)
    _write(ak, "\n".join(out) + "\n" if out else "", 0o600)
    if os.geteuid() == 0 and ROOT == "/":
        import pwd
        pw = pwd.getpwnam(USER)
        for path in (os.path.dirname(ak), ak):
            os.chown(path, pw.pw_uid, pw.pw_gid)


def ssh_active():
    return run(["systemctl", "is-active", "--quiet", "ssh"]).returncode == 0


def apply_on(stack, d):
    """Make the box match 'support on'. Returns messages."""
    msgs = []
    _set_keys(d["ssh_key"])
    _write(p(SUDOERS), f"# solar monitor remote support (removed when it is turned off)\n{USER} ALL=(ALL) NOPASSWD:ALL\n", 0o440)
    if not d.get("started_ssh") and not ssh_active():
        d["started_ssh"] = True       # support starts SSH: localhost-only and keys-only, undone when it ends
    if d.get("started_ssh"):
        changed = _write(p(SSHD_DROPIN), SSHD_CONF, 0o644)
        if changed or not ssh_active():
            r = run(["systemctl", "restart", "ssh"])
            msgs.append("SSH started (this box only, keys only)" if r.returncode == 0 else f"SSH didn't start: {r.stderr.strip()[-200:]}")
    ss.write_if_changed(env_path(stack), f"TUNNEL_TOKEN={d['token']}\n", 0o600)
    ss.write_env(stack, ss.ensure_secrets(stack))
    if run(ss.compose_cmd(stack) + ["ps", "-q", "--status", "running", "cloudflared-support"]).stdout.strip() == "" or msgs:
        r = run(ss.compose_cmd(stack) + ["up", "-d", "cloudflared-support"])
        msgs.append("support tunnel started" if r.returncode == 0 else f"support tunnel didn't start: {r.stderr.strip()[-300:]}")
    return msgs


def apply_off(stack, d):
    """Undo everything 'support on' did. Returns messages."""
    msgs = []
    r = run(ss.compose_cmd(stack) + ["rm", "-s", "-f", "cloudflared-support"])
    if r.returncode == 0:
        msgs.append("support tunnel stopped")
    _remove(env_path(stack))
    ss.write_env(stack, ss.ensure_secrets(stack))
    _set_keys("")
    _remove(p(SUDOERS))
    if d.get("started_ssh"):
        _remove(p(SSHD_DROPIN))
        run(["systemctl", "stop", "ssh"])
        d["started_ssh"] = False
        msgs.append("SSH stopped")
    return msgs


def enable(stack, d, now=None):
    now = now or time.time()
    d = {**d, "enabled": True, "since": int(now), "until": int(now + d["days"] * 86400) if d["days"] else 0, "message": ""}
    save(stack, d)          # before apply: .env's COMPOSE_PROFILES reads it
    msgs = apply_on(stack, d)
    save(stack, d)
    return msgs


def disable(stack, reason="turned off"):
    d = settings(stack)
    d["enabled"] = False
    save(stack, d)
    msgs = apply_off(stack, d)
    d["message"] = f"Remote support {reason} {time.strftime('%Y-%m-%d %H:%M')}."
    save(stack, d)
    return msgs


def check(stack, now=None):
    """Collector, every minute: switch off when the time is up; otherwise keep the box matching the setting
    (after a reboot SSH started by support isn't running). Returns messages."""
    d = settings(stack)
    if not d["enabled"]:
        return []
    if d["until"] and (now or time.time()) >= d["until"]:
        return disable(stack, "switched itself off at the end of its time,")
    return apply_on(stack, d)


def status(stack, now=None):
    d = settings(stack)
    now = now or time.time()
    left = None
    if d["enabled"] and d["until"]:
        left = max(0, d["until"] - now)
    return {"enabled": d["enabled"], "until": d["until"], "since": d["since"], "left_s": left, "days": d["days"],
            "tunnel_id": d["tunnel_id"], "has_token": bool(d["token"]), "keys": d["ssh_key"], "message": d["message"],
            "started_ssh": d["started_ssh"]}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stack", default="/opt/solar-monitor/stack")
    ap.add_argument("cmd", choices=["status", "off", "check"])
    a = ap.parse_args()
    if a.cmd == "off":
        print("\n".join(disable(a.stack)))
    elif a.cmd == "check":
        print("\n".join(check(a.stack)))
    st = status(a.stack)
    st.pop("keys", None)
    print(json.dumps(st, indent=1))
