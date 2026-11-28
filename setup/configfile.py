#!/usr/bin/env python3
"""The box's settings as one file: exported here, imported on another box (or this one after a reinstall).

What travels: the site (state/site.json), the wall display, the weather source, the update choice, UniFi Protect's
address and camera choice, and the settings of the modules in PORTABLE. What stays: everything that belongs to one
box (LOCAL: its tunnel, support access, SSH keys, backup stick, radios, the box's own secrets) and all history,
which is what backups are for.

Secrets (API keys, tokens, the cameras' stream links, the ntfy topic) are taken out of the file. They are only
included when asked for, and then encrypted with a passphrase (AES-256, key from the passphrase by PBKDF2, done by
openssl). The web page never exports them: anyone on the network can open it, and it never shows a saved secret
either. With secrets, export from the box itself:

    sudo python3 /opt/solar-monitor/src/setup/configfile.py export --secrets > solar-config.json

The file:
    {"solar_monitor_config": 1, "exported": "...", "version": "v0.2.57", "name": "<site name>",
     "files": {"site.json": {...}, "display.json": {...}, "modules/water.json": {"enabled": true, "settings": {...}}},
     "secrets": null | {"cipher": "aes-256-cbc", "kdf": "pbkdf2-sha256", "iterations": 600000, "data": "<base64>"}}

Import is two steps: plan() says what would change (nothing is written), apply happens in solar_web.py, which
writes the files in `plan["files"]` through the same code the setup page uses.
"""
import argparse
import copy
import getpass
import json
import os
import re
import subprocess
import sys
import time

import solar_setup as ss

FORMAT = 1
ITERATIONS = 600000
# modules whose settings describe the site and travel with it; the rest belong to one box
PORTABLE = ("away", "boiler", "cameras", "freeze", "generator", "notifications", "probeboard", "propane", "voice", "water", "weather")
LOCAL = ("backup", "follow", "hahistory", "share", "ssh", "support", "tunnel", "zigbee", "zwave")
PLAIN = ("site.json", "display.json", "weather.json", "update.json", "protect.json")
# per file: keys that are this box's state, not settings (dotted paths), and keys that are the only ones kept
DROP = {"protect.json": ("ha_added", "applied_quality"), "weather.json": ("probe", "scan", "last_push"),
        "modules/away.json": ("settings.away", "settings.since", "settings.until")}   # being away isn't a setting
ONLY = {"modules/probeboard.json": ("settings.names",)}
# per file: secrets by dotted path; any other key that looks like one (SECRET_RE) is treated the same
SECRET = {"protect.json": ("api_key",), "display.json": ("cameras",),
          "modules/notifications.json": ("settings.ntfy_token", "settings.ntfy_topic")}
SECRET_RE = re.compile(r"(token|api_?key|password|passwd|secret|psk)", re.I)
MAX_BYTES = 512 * 1024
# the fuel on hand is a count on one box at one moment, not a setting
DROP["modules/generator.json"] = ("settings.fuel_gal", "settings.fuel_at")


class ConfigError(Exception):
    pass


def names():
    return list(PLAIN) + [f"modules/{m}.json" for m in PORTABLE]


# --------------------------------------------------------------------------
# Dotted paths
# --------------------------------------------------------------------------
def _get(d, dotted, default=None):
    for k in dotted.split("."):
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


def _pop(d, dotted):
    keys = dotted.split(".")
    for k in keys[:-1]:
        d = d.get(k) if isinstance(d, dict) else None
        if not isinstance(d, dict):
            return None
    return d.pop(keys[-1], None) if isinstance(d, dict) else None


def _set(d, dotted, value):
    keys = dotted.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def secret_paths(name, doc):
    """The dotted paths in doc that hold secrets: the listed ones that are present, and any key named like one."""
    out = [p for p in SECRET.get(name, ()) if _get(doc, p) not in (None, "", [], {})]

    def walk(d, prefix):
        for k, v in d.items():
            p = prefix + k
            if any(p == s or p.startswith(s + ".") for s in out):
                continue
            if isinstance(v, dict):
                walk(v, p + ".")
            elif SECRET_RE.search(k) and v not in (None, "", [], {}) and not isinstance(v, bool):
                out.append(p)
    walk(doc, "")
    return out


def flatten(d, prefix=""):
    """{dotted path: value}; lists are values."""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict) and v:
            out.update(flatten(v, prefix + k + "."))
        else:
            out[prefix + k] = v
    return out


# --------------------------------------------------------------------------
# Encryption (openssl)
# --------------------------------------------------------------------------
def _openssl(args, data, passphrase):
    env = dict(os.environ, SOLAR_CONFIG_PASS=passphrase)
    try:
        r = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-md", "sha256", "-iter", str(ITERATIONS),
                            "-a", "-A", "-pass", "env:SOLAR_CONFIG_PASS", *args], input=data, capture_output=True, env=env, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise ConfigError(f"openssl couldn't be run: {e}") from None
    return r


def encrypt(obj, passphrase):
    if len(passphrase) < 8:
        raise ConfigError("the passphrase needs at least 8 characters")
    r = _openssl(["-salt"], json.dumps({"check": "solar", "secrets": obj}).encode(), passphrase)
    if r.returncode != 0:
        raise ConfigError("the secrets couldn't be encrypted")
    return {"cipher": "aes-256-cbc", "kdf": "pbkdf2-sha256", "iterations": ITERATIONS, "data": r.stdout.decode().strip()}


def decrypt(box, passphrase):
    if not isinstance(box, dict) or box.get("cipher") != "aes-256-cbc" or not isinstance(box.get("data"), str):
        raise ConfigError("the file's secrets are in a form this version doesn't know")
    r = _openssl(["-d"], box["data"].encode(), passphrase)
    try:
        doc = json.loads(r.stdout) if r.returncode == 0 else None
    except ValueError:
        doc = None
    if not isinstance(doc, dict) or doc.get("check") != "solar" or not isinstance(doc.get("secrets"), dict):
        raise ConfigError("wrong passphrase for the file's secrets")
    return doc["secrets"]


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------
def current(stack):
    """{file name: its content now} for everything that travels (module files through modules.load, so they exist
    and are up to date)."""
    import modules
    out = {}
    for n in PLAIN:
        d = ss.load_json(os.path.join(stack, "state", n), None)
        if isinstance(d, dict):
            out[n] = d
    for m in PORTABLE:
        try:
            d = modules.load(stack, m)
        except Exception:  # noqa: BLE001 - an unreadable module file is left out
            continue
        out[f"modules/{m}.json"] = {"enabled": bool(d["enabled"]), "settings": d["settings"]}
    return out


def export(stack, version="", passphrase=None, now=None):
    """The config document. Secrets are left out, or with a passphrase encrypted into "secrets"."""
    files, secrets = {}, {}
    for n, doc in current(stack).items():
        doc = copy.deepcopy(doc)
        for p in DROP.get(n, ()):
            _pop(doc, p)
        if n in ONLY:
            kept = {}
            for p in ("enabled",) + ONLY[n]:
                if _get(doc, p) is not None:
                    _set(kept, p, _get(doc, p))
            doc = kept
        for p in secret_paths(n, doc):
            secrets[f"{n}:{p}"] = _pop(doc, p)
        files[n] = doc
    return {"solar_monitor_config": FORMAT, "exported": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
            "version": version, "name": str((files.get("site.json") or {}).get("name") or ""),
            "files": files, "secrets_left_out": len(secrets) if not passphrase else 0,
            "secrets": encrypt(secrets, passphrase) if passphrase and secrets else None}


# --------------------------------------------------------------------------
# Import: reading, checking, planning
# --------------------------------------------------------------------------
def parse(text):
    if len(text.encode()) > MAX_BYTES:
        raise ConfigError("that file is too large to be a settings file")
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise ConfigError(f"that isn't a settings file (not JSON: {e})") from None
    if not isinstance(doc, dict) or "solar_monitor_config" not in doc or not isinstance(doc.get("files"), dict):
        raise ConfigError("that isn't a solar monitor settings file")
    if doc["solar_monitor_config"] != FORMAT:
        raise ConfigError(f"the file is in format {doc['solar_monitor_config']}; this version reads format {FORMAT}. Update the box first.")
    return doc


def _fits(value, like):
    """Does an imported value have the kind the box expects (from its current or default value)?"""
    if like is None or value is None:
        return True
    if isinstance(like, bool):
        return isinstance(value, bool)
    if isinstance(like, (int, float)):
        return isinstance(value, (int, float, str)) and not isinstance(value, bool)
    if isinstance(like, str):
        return isinstance(value, (str, int, float)) and not isinstance(value, bool)
    return isinstance(value, type(like))


def shapes(stack, site_defaults=None):
    """{file name: a document of the expected kinds} to check imported values against: the defaults under the
    box's current content."""
    import display
    import modules
    import protect
    out = {"site.json": site_defaults or {}, "display.json": dict(display.DEFAULTS), "protect.json": dict(protect.DEFAULTS),
           "weather.json": {"source": "", "station": {}}, "update.json": {"enabled": True, "channel": ""}}
    for m in PORTABLE:
        try:
            out[f"modules/{m}.json"] = {"enabled": True, "settings": dict(modules.get(m).defaults)}
        except KeyError:
            pass
    cur = current(stack)
    for n in out:
        merged = copy.deepcopy(out[n])
        for p, v in flatten(cur.get(n) or {}).items():
            if _get(merged, p) is None:
                _set(merged, p, v)
        out[n] = merged
    return out


def plan(stack, doc, passphrase="", site_defaults=None):
    """What importing doc would do, without writing anything:
    {"files": {name: the new content}, "changes": [(name, path, old, new, secret)], "notes": [text], "same": n}.
    Values of the wrong kind and files this version doesn't know are skipped, each with a note. Secrets are applied
    when the file has them and the passphrase opens them; otherwise the box keeps its own."""
    notes, changes, files, same = [], [], {}, 0
    secrets = {}
    if doc.get("secrets"):
        if passphrase:
            secrets = decrypt(doc["secrets"], passphrase)
        else:
            notes.append("The file has secrets (API keys, camera links), but no passphrase was given: they are skipped, "
                         "and this box keeps its own.")
    elif doc.get("secrets_left_out"):
        notes.append(f"The file was exported without its {doc['secrets_left_out']} secrets (API keys, camera links): "
                     "this box keeps its own, or they are entered again on the setup page.")
    cur, like = current(stack), shapes(stack, site_defaults)
    known = names()
    for n in sorted(doc["files"]):
        if n not in known:
            notes.append(f"{n}: not something this version imports; skipped.")
            continue
        got = doc["files"][n]
        if not isinstance(got, dict):
            notes.append(f"{n}: not in the expected form; skipped.")
            continue
        new = copy.deepcopy(cur.get(n) or {})
        incoming = flatten(got)
        for key, v in secrets.items():
            if key.startswith(n + ":"):
                incoming[key[len(n) + 1:]] = v
        hidden = set(secret_paths(n, cur.get(n) or {})) | {k[len(n) + 1:] for k in secrets if k.startswith(n + ":")}
        for p, v in sorted(incoming.items()):
            if any(p == d or p.startswith(d + ".") for d in DROP.get(n, ())):
                continue
            if not _fits(v, _get(like[n], p)):
                notes.append(f"{n}: {p} isn't the kind of value expected; skipped.")
                continue
            old = _get(new, p)
            if old == v:
                same += 1
                continue
            _set(new, p, v)
            changes.append((n, p, old, v, p in hidden or bool(SECRET_RE.search(p.split(".")[-1]))))
        if new != (cur.get(n) or {}):
            files[n] = new
    if "site.json" in files:
        tz = files["site.json"].get("time_zone")
        try:
            import zoneinfo
            if tz and tz not in zoneinfo.available_timezones():
                raise ConfigError(f"the file's time zone '{tz}' isn't one this box knows")
        except ImportError:
            pass
    return {"files": files, "changes": changes, "notes": notes, "same": same}


def show(value, secret=False):
    """A value for the preview table."""
    if secret:
        return "(hidden)" if value not in (None, "", [], {}) else "(none)"
    if value is None:
        return "(not set)"
    if isinstance(value, bool):
        return "on" if value else "off"
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return s if len(s) <= 80 else s[:77] + "..."


# --------------------------------------------------------------------------
# Command line (for an export with secrets, from the box itself)
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Export this box's settings as one file (stdout).")
    ap.add_argument("cmd", choices=["export"])
    ap.add_argument("--stack", default="/opt/solar-monitor/stack")
    ap.add_argument("--secrets", action="store_true", help="include API keys, tokens and camera links, encrypted with a passphrase")
    a = ap.parse_args(argv)
    passphrase = None
    if a.secrets:
        passphrase = os.environ.get("SOLAR_CONFIG_PASS") or getpass.getpass("Passphrase for the secrets (8 characters or more): ")
        if not os.environ.get("SOLAR_CONFIG_PASS") and getpass.getpass("Again: ") != passphrase:
            sys.exit("the two passphrases differ")
    try:
        version = open(os.path.join(os.path.dirname(a.stack), "VERSION")).read().strip()
    except OSError:
        version = ""
    try:
        doc = export(a.stack, version, passphrase)
    except ConfigError as e:
        sys.exit(str(e))
    json.dump(doc, sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
