"""ntfy on this box: a private alternative to ntfy.sh for the notifications module's ntfy channel.

It runs as the `ntfy` compose service (profile "ntfy"), listens on 127.0.0.1:2586 only, and reaches the phones
through the box's own Cloudflare tunnel (the Remote access module) at <name>-ntfy.<domain>: one level under the
domain, so Cloudflare's free certificate covers it. Without remote access it is not offered.

Nobody can read or post without an account: the server denies everything by default, has no sign-up and no web
app. Its config (stack/ntfy/server.yml, written here) declares two users and their rights on one topic:

  box     writes to the topic, with an access token (the notifications module posts with it, over localhost)
  phone   reads the topic, with a password (entered once in the ntfy app on each phone)

iPhones only get pushes through Apple's service, which ntfy.sh holds: the server forwards a "poll" request to
ntfy.sh for each message (upstream-base-url), and the phone then fetches the message itself from this server.
Only a hash of the topic's address goes to ntfy.sh, never the message.

Secrets (state/secrets.json): ntfy_topic, ntfy_box_token, ntfy_phone_password, and the bcrypt hashes of the two
users' passwords (kept, so the config only changes when a password does).
"""
import json
import os
import secrets
import string
import subprocess

import solar_setup as ss

PORT = 2586
LOCAL_URL = f"http://127.0.0.1:{PORT}"
SERVICE = f"http://localhost:{PORT}"       # what cloudflared (host network) forwards to
UPSTREAM = "https://ntfy.sh"
IMAGE = "binwiederhier/ntfy:v2.37.2"            # the same as stack/compose.yaml
PHONE_USER, BOX_USER = "phone", "box"
_ALNUM = string.ascii_lowercase + string.digits


def _secrets_path(stack):
    return os.path.join(stack, "state", "secrets.json")


def topic(stack):
    return ss.secret(stack, "ntfy_topic", lambda: "solar-" + secrets.token_hex(6))


def box_token(stack):
    """An ntfy access token: tk_ and 29 lowercase letters and digits."""
    return ss.secret(stack, "ntfy_box_token", lambda: "tk_" + "".join(secrets.choice(_ALNUM) for _ in range(29)))


def phone_password(stack):
    return ss.secret(stack, "ntfy_phone_password",
                     lambda: "-".join("".join(secrets.choice(_ALNUM) for _ in range(5)) for _ in range(4)))


def reset_phone_password(stack):
    """A new phone password (phones signed in with the old one stop getting alerts until it is entered again)."""
    path = _secrets_path(stack)
    s = ss.load_json(path, {})
    s.pop("ntfy_phone_password", None)
    s.pop("ntfy_phone_hash", None)
    ss.write_if_changed(path, json.dumps(s, indent=2) + "\n", 0o600)
    return phone_password(stack)


def _bcrypt(password):
    try:
        import bcrypt
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt(10)).decode()
    except ImportError:                    # python3-bcrypt is on the image; ntfy can hash it too
        r = subprocess.run(["docker", "run", "--rm", "-i", IMAGE, "user", "hash"], input=f"{password}\n{password}\n",
                           capture_output=True, text=True, timeout=120)
        h = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        if not h.startswith("$2"):
            raise OSError("could not hash the ntfy password")
        return h


def _matches(password, h):
    try:
        import bcrypt
        return bcrypt.checkpw(password.encode(), h.encode())
    except ImportError:
        return True                        # can't check here: keep the stored hash
    except ValueError:
        return False


def password_hash(stack, key, password):
    """The bcrypt hash of a password, kept in secrets.json while it still matches."""
    path = _secrets_path(stack)
    s = ss.load_json(path, {})
    h = s.get(key, "")
    if not h or not _matches(password, h):
        s[key] = _bcrypt(password)
        ss.write_if_changed(path, json.dumps(s, indent=2) + "\n", 0o600)
        h = s[key]
    return h


# --------------------------------------------------------------------------
# Where it is
# --------------------------------------------------------------------------
def host(stack):
    """<name>-ntfy.<domain> from the remote access settings, or '' without remote access."""
    import tunnel
    d = tunnel.settings(stack)
    if not (d.get("enabled") and d.get("domain")):
        return ""
    return f"{d['name']}-ntfy.{d['domain']}" if d.get("name") else f"ntfy.{d['domain']}"


def public_url(stack):
    h = host(stack)
    return f"https://{h}" if h else ""


def chosen(stack):
    """The notifications module is on and its ntfy channel is on and set to this box."""
    import modules
    if not any(m.id == "notifications" for m in modules.enabled(stack)):
        return False
    s = modules.settings(stack, "notifications")
    return bool(s.get("ntfy") and s.get("ntfy_box"))


def wanted(stack):
    """chosen(), and remote access is set up to reach it."""
    return chosen(stack) and bool(host(stack))


# --------------------------------------------------------------------------
# The server
# --------------------------------------------------------------------------
def _q(v):
    return json.dumps(v)                   # YAML accepts JSON strings


def config(stack):
    """stack/ntfy/server.yml."""
    t = topic(stack)
    phone = password_hash(stack, "ntfy_phone_hash", phone_password(stack))
    box = password_hash(stack, "ntfy_box_hash", ss.secret(stack, "ntfy_box_password", lambda: secrets.token_urlsafe(24)))
    lines = [
        "# Written by the solar monitor (notifications module, ntfy on this box); edits are overwritten.",
        f"base-url: {_q(public_url(stack))}",
        'listen-http: ":80"',
        "behind-proxy: true",
        'cache-file: "/var/cache/ntfy/cache.db"',
        'cache-duration: "24h"',
        'auth-file: "/var/cache/ntfy/user.db"',
        'auth-default-access: "deny-all"',
        "auth-users:",
        f"  - {_q(f'{BOX_USER}:{box}:user')}",
        f"  - {_q(f'{PHONE_USER}:{phone}:user')}",
        "auth-access:",
        f"  - {_q(f'{BOX_USER}:{t}:wo')}",
        f"  - {_q(f'{PHONE_USER}:{t}:ro')}",
        "auth-tokens:",
        f"  - {_q(f'{BOX_USER}:{box_token(stack)}:solar monitor')}",
        "enable-signup: false",
        "enable-login: false",
        'web-root: "disable"',
        f"upstream-base-url: {_q(UPSTREAM)}",
    ]
    return "\n".join(lines) + "\n"


def config_path(stack):
    return os.path.join(stack, "ntfy", "server.yml")


def _compose(stack, *args):
    try:
        return subprocess.run(ss.compose_cmd(stack) + list(args), capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def apply(stack, on=None):
    """Start, restart (new config) or stop the server. on: whether it should run (None: wanted()). Returns messages."""
    on = wanted(stack) if on is None else (on and bool(host(stack)))
    ss.write_env(stack, ss.ensure_secrets(stack))          # COMPOSE_PROFILES gains or loses "ntfy"
    if not on:
        r = _compose(stack, "rm", "-s", "-f", "ntfy")
        return [] if r.returncode == 0 else [f"couldn't stop ntfy: {r.stderr.strip()[-200:]}"]
    os.makedirs(os.path.dirname(config_path(stack)), exist_ok=True)
    changed = ss.write_if_changed(config_path(stack), config(stack), 0o600)
    r = _compose(stack, "up", "-d", *(["--force-recreate"] if changed else []), "ntfy")
    if r.returncode != 0:
        return [f"ntfy didn't start: {r.stderr.strip()[-300:]}"]
    return ["ntfy on this box started" if changed else "ntfy on this box running"]
