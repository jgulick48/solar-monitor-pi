"""Tests for SSH access from the local network (sshaccess.py, the "ssh" module) on a fake root filesystem with a fake
systemctl, including sharing SSH with remote support.
Run: python3 -m unittest discover -s tests -v"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup"))
import modules  # noqa: E402
import sshaccess  # noqa: E402
import support  # noqa: E402
from tests.test_support import KEY, OWNER_KEY, Support  # noqa: E402


class SSHAccess(Support):
    """Reuses remote support's fake root, systemctl and compose."""

    def fake_run(self, cmd):
        if cmd[:3] == ["systemctl", "is-enabled", "--quiet"]:
            self.calls.append(" ".join(cmd))
            return support.subprocess.CompletedProcess(cmd, 0 if getattr(self, "ssh_boot", False) else 1, "", "")
        if cmd[:2] == ["systemctl", "enable"]:
            self.ssh_boot = True
        if cmd[:2] == ["systemctl", "disable"]:
            self.ssh_boot = False
        return super().fake_run(cmd)

    def ak(self):
        return open(self.f("home/solar/.ssh/authorized_keys")).read()

    def test_put_back_after_a_restore(self):
        sshaccess.enable(self.stack, KEY, sudo=True)
        # a backup restored onto a new drive: the settings are back, nothing outside them is, SSH is off
        os.remove(self.f("home/solar/.ssh/authorized_keys"))
        os.remove(self.f(sshaccess.SUDOERS))
        os.remove(self.f(sshaccess.SSHD_DROPIN))
        self.ssh_on = self.ssh_boot = False
        msgs = sshaccess.check(self.stack)
        self.assertEqual(msgs, ["SSH access keys put back", "administrator rights put back", "SSH started (keys only)"])
        self.assertIn(f"{sshaccess.MARK_BEGIN}\n{KEY}\n{sshaccess.MARK_END}", self.ak())
        self.assertEqual(open(self.f(sshaccess.SUDOERS)).read(), sshaccess.SUDO_RULE)
        self.assertEqual(open(self.f(sshaccess.SSHD_DROPIN)).read(), sshaccess.SSHD_CONF)
        self.assertTrue(self.ssh_on and self.ssh_boot)                      # running, and again after a reboot
        # nothing missing: nothing touched, SSH not restarted
        self.calls.clear()
        self.assertEqual(sshaccess.check(self.stack), [])
        self.assertFalse([c for c in self.calls if "restart" in c or c.startswith("systemctl enable")], self.calls)

    def test_check_keeps_the_owners_other_keys_and_settings(self):
        os.makedirs(self.f("home/solar/.ssh"))
        with open(self.f("home/solar/.ssh/authorized_keys"), "w") as f:
            f.write(OWNER_KEY + "\n")
        sshaccess.enable(self.stack, KEY, sudo=False)
        os.remove(self.f(sshaccess.SSHD_DROPIN))
        self.assertEqual(sshaccess.check(self.stack), ["SSH started (keys only)"])
        self.assertTrue(self.ak().startswith(OWNER_KEY + "\n"))              # the owner's own key stays
        self.assertFalse(os.path.exists(self.f(sshaccess.SUDOERS)))          # no administrator rights: none added
        sshaccess.disable(self.stack)
        self.assertEqual(sshaccess.check(self.stack), [])                   # off: leaves everything alone

    def test_on_and_off(self):
        msgs = sshaccess.enable(self.stack, KEY, sudo=True)
        self.assertEqual(msgs, ["SSH started (keys only)"])
        self.assertTrue(self.ssh_on and self.ssh_boot)                        # running, and again after a reboot
        self.assertIn(f"{sshaccess.MARK_BEGIN}\n{KEY}\n{sshaccess.MARK_END}", self.ak())
        conf = open(self.f(sshaccess.SSHD_DROPIN)).read()
        self.assertIn("ListenAddress 0.0.0.0", conf)
        self.assertIn("PasswordAuthentication no", conf)
        self.assertIn("NOPASSWD:ALL", open(self.f(sshaccess.SUDOERS)).read())
        self.assertTrue(sshaccess.is_on(self.stack))
        self.assertEqual(sshaccess.disable(self.stack), ["SSH stopped"])
        self.assertFalse(self.ssh_on or self.ssh_boot)
        self.assertNotIn(KEY, self.ak())
        self.assertFalse(os.path.exists(self.f(sshaccess.SSHD_DROPIN)) or os.path.exists(self.f(sshaccess.SUDOERS)))
        self.assertFalse(sshaccess.is_on(self.stack))

    def test_without_sudo_and_keys_updated(self):
        sshaccess.enable(self.stack, KEY, sudo=True)
        sshaccess.enable(self.stack, OWNER_KEY, sudo=False)
        self.assertNotIn(KEY, self.ak())
        self.assertIn(OWNER_KEY, self.ak())
        self.assertFalse(os.path.exists(self.f(sshaccess.SUDOERS)))

    def test_owners_own_ssh_is_left_running(self):
        self.ssh_on = self.ssh_boot = True
        os.makedirs(self.f("home/solar/.ssh"))
        with open(self.f("home/solar/.ssh/authorized_keys"), "w") as f:
            f.write(OWNER_KEY + "\n")
        sshaccess.enable(self.stack, KEY, sudo=False)
        self.assertEqual(sshaccess.disable(self.stack), ["SSH back to how it was set up before"])
        self.assertTrue(self.ssh_on and self.ssh_boot)
        self.assertEqual(self.ak(), OWNER_KEY + "\n")                          # the owner's key stays

    def test_shared_with_remote_support(self):
        # support on first (starts SSH, localhost only), then SSH access; support off leaves SSH running
        support.enable(self.stack, self.form(), now=1000)
        sshaccess.enable(self.stack, OWNER_KEY, sudo=True)
        self.assertTrue(os.path.exists(self.f(support.SSHD_DROPIN)) and os.path.exists(self.f(sshaccess.SSHD_DROPIN)))
        msgs = support.disable(self.stack)
        self.assertIn("SSH left on for SSH access", msgs)
        self.assertTrue(self.ssh_on)
        self.assertIn(OWNER_KEY, self.ak())
        self.assertNotIn(KEY, self.ak())                                      # the helper's key went with support
        self.assertEqual(sshaccess.disable(self.stack), ["SSH stopped"])       # last one off stops it
        self.assertFalse(self.ssh_on)

    def test_ssh_access_off_hands_ssh_back_to_support(self):
        sshaccess.enable(self.stack, OWNER_KEY, sudo=True)                    # starts SSH
        support.enable(self.stack, self.form(), now=1000)                     # SSH already running: support didn't start it
        self.assertEqual(sshaccess.disable(self.stack), ["SSH from the network off (remote support still uses it)"])
        self.assertTrue(self.ssh_on)
        self.assertTrue(os.path.exists(self.f(support.SSHD_DROPIN)))          # back to localhost only, for the tunnel
        self.assertFalse(os.path.exists(self.f(sshaccess.SSHD_DROPIN)))
        support.disable(self.stack)
        self.assertFalse(self.ssh_on)                                          # and support stops it in the end

    def test_module_switch_off_turns_it_off(self):
        sshaccess.enable(self.stack, KEY, sudo=True)
        modules.set_enabled(self.stack, "ssh", False)
        self.assertFalse(self.ssh_on)
        self.assertNotIn(KEY, self.ak())

    def test_pane(self):
        from modules import ssh as mod
        html = mod.setup_section(self.stack)
        self.assertIn('action="/ssh"', html)
        self.assertIn("Turn SSH access on", html)
        self.assertEqual(mod.card(self.stack), "")
        self.assertEqual(mod.turn_on(self.stack, {"ssh_keys": "hello"}).code, 400)
        r = mod.turn_on(self.stack, {"ssh_keys": KEY, "ssh_sudo": "1"})
        self.assertTrue(r.setup_msg.startswith("on: ssh solar@"), r.setup_msg)
        self.assertIn("with administrator rights", mod.setup_section(self.stack))
        self.assertIn("Turn SSH access off", mod.setup_section(self.stack))
        self.assertIn("SSH access</b>: on", mod.card(self.stack))


# Support is the base for its fixtures only: its own tests run from test_support, not again here
for _name in [n for n in dir(Support) if n.startswith("test_") and n not in vars(SSHAccess)]:
    setattr(SSHAccess, _name, None)
del Support


if __name__ == "__main__":
    unittest.main()
