#!/bin/sh
# Install the solar monitor onto a Debian/Raspberry Pi OS host that already
# has Docker Engine + the compose plugin. Used by the image build and for
# manual installs: sudo tools/install.sh
set -eu
SRC=$(cd "$(dirname "$0")/.." && pwd)
DEST=/opt/solar-monitor
install -d "$DEST" "$DEST/stack" "$DEST/setup"
cp -a "$SRC/setup/." "$DEST/setup/"
# keep generated/runtime files (state, .env, passwd, bridge config, HA data) on reinstall
cp -a --no-preserve=ownership "$SRC/stack/." "$DEST/stack/"
install -m 0644 "$SRC"/systemd/solar-*.service "$SRC"/systemd/solar-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable solar-setup.service solar-setup.timer solar-stack.service solar-web.service
echo "installed to $DEST; reboot or: systemctl start solar-setup solar-stack solar-web"
