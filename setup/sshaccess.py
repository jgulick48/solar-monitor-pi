#!/usr/bin/env python3
"""SSH access from the local network with SSH keys (the "SSH access" module; its pane is on the setup page).

Turning it on:
  - puts the keys in a marked block of ~solar/.ssh/authorized_keys (other keys there stay);
  - with "administrator rights", lets `solar` use sudo without a password (/etc/sudoers.d/solar-ssh-access);
  - makes SSH listen on the network for keys only (/etc/ssh/sshd_config.d/01-solar-ssh-access.conf) and starts it,
    also at boot, if it wasn't running already.
Turning it off undoes all of that. If SSH was running before (the owner turned it on themselves), it is left
running with their own settings. Remote support (support.py) also uses SSH, limited to the box itself; the two
share it: whichever is turned off last stops it.

Settings (state/modules/ssh.json): {"enabled": bool, "keys": "...", "sudo": bool, "started_ssh": bool}.
"""
import os
import time

import support
from support import clean_keys  # noqa: F401 - the same key check as remote support

MARK_BEGIN = "# --- solar monitor SSH access (removed when SSH access is turned off) ---"
MARK_END = "# --- end solar monitor SSH access ---"
SSHD_DROPIN = "etc/ssh/sshd_config.d/01-solar-ssh-access.conf"   # read first, so its settings win
SUDOERS = "etc/sudoers.d/solar-ssh-access"
SSHD_CONF = ("# Written by the solar monitor while SSH access is on; removed when it is turned off.\n"
             "ListenAddress 0.0.0.0\nListenAddress ::\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
             "PermitRootLogin no\n")
DEFAULTS = {"enabled": False, "keys": "", "sudo": True, "started_ssh": False, "since": 0, "message": ""}


def settings(stack):
    import modules
    return {**DEFAULTS, **modules.settings(stack, "ssh")}


def save(stack, d):
    import modules
    modules.save(stack, "ssh", d)


def is_on(stack):
    try:
        import modules
        return modules.load(stack, "ssh")["enabled"] and settings(stack)["enabled"]
    except Exception:  # noqa: BLE001 - unreadable settings: treat as off
        return False


def _ssh_enabled_at_boot():
    return support.run(["systemctl", "is-enabled", "--quiet", "ssh"]).returncode == 0


def enable(stack, keys, sudo, now=None):
    """Turn SSH access on (or update its keys). Returns messages."""
    d = settings(stack)
    if not d["enabled"]:
        # SSH already running or enabled (the owner's own, or remote support's): leave starting/stopping it to them
        d["started_ssh"] = not (support.ssh_active() or _ssh_enabled_at_boot())
        if support.settings(stack)["enabled"] and support.settings(stack).get("started_ssh"):
            d["started_ssh"] = True       # remote support started it; once both are off it should stop
    d.update(enabled=True, keys=keys, sudo=bool(sudo), since=d["since"] or int(now or time.time()), message="")
    save(stack, d)
    msgs = []
    support._set_keys(keys, MARK_BEGIN, MARK_END)
    if sudo:
        support._write(support.p(SUDOERS), f"# solar monitor SSH access (removed when it is turned off)\n{support.USER} ALL=(ALL) NOPASSWD:ALL\n", 0o440)
    else:
        support._remove(support.p(SUDOERS))
    changed = support._write(support.p(SSHD_DROPIN), SSHD_CONF, 0o644)
    if d["started_ssh"]:
        support.run(["systemctl", "enable", "ssh"])
    if changed or not support.ssh_active():
        r = support.run(["systemctl", "restart", "ssh"])
        msgs.append("SSH started (keys only)" if r.returncode == 0 else f"SSH didn't start: {r.stderr.strip()[-200:]}")
    return msgs


def disable(stack, reason="turned off"):
    """Undo everything enable() did. Returns messages."""
    d = settings(stack)
    msgs = []
    support._set_keys("", MARK_BEGIN, MARK_END)
    support._remove(support.p(SUDOERS))
    support._remove(support.p(SSHD_DROPIN))
    sup = support.settings(stack)
    if sup["enabled"]:
        # remote support still needs SSH, limited to the box itself: hand stopping it over to support
        if d["started_ssh"] and not sup.get("started_ssh"):
            sup["started_ssh"] = True
            support.save(stack, sup)
            support._write(support.p(support.SSHD_DROPIN), support.SSHD_CONF, 0o644)
            support.run(["systemctl", "disable", "ssh"])
        support.run(["systemctl", "restart", "ssh"])
        msgs.append("SSH from the network off (remote support still uses it)")
    elif d["started_ssh"]:
        support.run(["systemctl", "disable", "ssh"])
        support.run(["systemctl", "stop", "ssh"])
        msgs.append("SSH stopped")
    else:
        support.run(["systemctl", "restart", "ssh"])     # back to the owner's own SSH settings
        msgs.append("SSH back to how it was set up before")
    d.update(enabled=False, started_ssh=False, since=0, message=f"SSH access {reason} {time.strftime('%Y-%m-%d %H:%M')}.")
    save(stack, d)
    return msgs
