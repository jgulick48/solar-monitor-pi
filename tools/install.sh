#!/bin/sh
# Install the solar monitor onto a Debian/Raspberry Pi OS host that already
# has Docker Engine + the compose plugin. Used by the image build and for
# manual installs: sudo tools/install.sh
set -eu
SRC=$(cd "$(dirname "$0")/.." && pwd)
DEST=/opt/solar-monitor
install -d "$DEST" "$DEST/stack" "$DEST/setup"
cp -a "$SRC/setup/." "$DEST/setup/"
# version shipped in the image / installed by tools/update.sh
[ -f "$DEST/VERSION" ] || echo "$(cat "$SRC/VERSION" 2>/dev/null || echo unknown)" > "$DEST/VERSION"
# keep generated/runtime files (state, .env, passwd, bridge config, HA data) on reinstall
cp -a --no-preserve=ownership "$SRC/stack/." "$DEST/stack/"
install -m 0644 "$SRC"/systemd/solar-*.service "$SRC"/systemd/solar-*.timer /etc/systemd/system/
# HDMI wall display: kiosk account (no password, no shell), its PAM session, hotplug rule
if ! id solar-display >/dev/null 2>&1; then
  groups=$(for g in video render input; do getent group "$g" >/dev/null && printf '%s,' "$g"; done)
  useradd -r -m -d /var/lib/solar-display -s /usr/sbin/nologin ${groups:+-G "${groups%,}"} solar-display
fi
install -m 0644 "$SRC/system/pam-solar-display" /etc/pam.d/solar-display
install -m 0644 "$SRC/system/90-solar-display.rules" /etc/udev/rules.d/90-solar-display.rules
systemctl daemon-reload || true   # no-op in a chroot
systemctl enable solar-images.service solar-setup.service solar-setup.timer solar-stack.service solar-web.service solar-update.timer solar-collector.service solar-advisor.timer solar-display.service solar-display-power.timer solar-backup.timer
# On a running system (not the image build's chroot): start new timers/services and restart the
# long-running ones so they use the new code. solar-web is restarted by the updater itself.
if [ -d /run/systemd/system ]; then
  systemctl start solar-setup.timer solar-update.timer solar-advisor.timer solar-display-power.timer solar-backup.timer || true
  udevadm control --reload 2>/dev/null || true
  # The display needs cage + chromium (in the image since v0.2; on older installs:
  # sudo apt-get install cage chromium wlr-randr). It starts only if a screen is connected.
  systemctl start --no-block solar-display.service || true
  systemctl restart solar-collector.service || true
fi
echo "installed to $DEST; reboot or: systemctl start solar-setup solar-stack solar-web"
