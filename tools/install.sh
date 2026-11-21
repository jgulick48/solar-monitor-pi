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
  groups=$(for g in video render input audio; do getent group "$g" >/dev/null && printf '%s,' "$g"; done)
  useradd -r -m -d /var/lib/solar-display -s /usr/sbin/nologin ${groups:+-G "${groups%,}"} solar-display
fi
# sound through HDMI (camera chimes): older installs made the account without the audio group
getent group audio >/dev/null && usermod -aG audio solar-display 2>/dev/null || true
install -m 0644 "$SRC/system/pam-solar-display" /etc/pam.d/solar-display
install -m 0644 "$SRC/system/90-solar-display.rules" /etc/udev/rules.d/90-solar-display.rules
# the screen power check was a one-minute timer before v0.2.16; now a service that follows occupancy and camera events
systemctl disable --now solar-display-power.timer 2>/dev/null || true
rm -f /etc/systemd/system/solar-display-power.timer
systemctl daemon-reload || true   # no-op in a chroot
systemctl enable solar-images.service solar-setup.service solar-setup.timer solar-stack.service solar-web.service solar-update.timer solar-collector.service solar-advisor.timer solar-display.service solar-display-power.service solar-backup.timer
# On a running system (not the image build's chroot): start new timers/services and restart the
# long-running ones so they use the new code. solar-web is restarted by the updater itself.
if [ -d /run/systemd/system ]; then
  systemctl start solar-setup.timer solar-update.timer solar-advisor.timer solar-backup.timer || true
  systemctl restart solar-display-power.service || true
  udevadm control --reload 2>/dev/null || true
  # The display needs cage + chromium (in the image since v0.2; on older installs:
  # sudo apt-get install cage chromium wlr-randr). It starts only if a screen is connected.
  # restart a running dashboard so a new kiosk.sh (screen mode, sound) and group changes take effect;
  # start it if it isn't running (it checks by itself whether a screen is connected)
  systemctl try-restart --no-block solar-display.service || true
  systemctl start --no-block solar-display.service || true
  systemctl restart solar-collector.service || true
  # SSD health (boxhealth.py); in the image since v0.2.7, added once on boxes updated from older versions
  # (the image ships without apt's package lists, so refresh them first)
  # HDMI-CEC (turning the TV on and off): cec-ctl from v4l-utils, in Raspberry Pi OS; added if missing
  command -v cec-ctl >/dev/null || { timeout 300 apt-get update -q >/dev/null 2>&1; timeout 300 apt-get install -y -q --no-install-recommends v4l-utils >/dev/null 2>&1; } || true
  command -v smartctl >/dev/null || { timeout 300 apt-get update -q >/dev/null 2>&1; timeout 300 apt-get install -y -q --no-install-recommends smartmontools >/dev/null 2>&1; } || true
fi
echo "installed to $DEST; reboot or: systemctl start solar-setup solar-stack solar-web"
