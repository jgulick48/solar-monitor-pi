#!/bin/bash
# Runs inside the Raspberry Pi OS image (chroot, arm64 under emulation).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive LC_ALL=C
mv /etc/resolv.conf /etc/resolv.conf.orig 2>/dev/null || true
cp /etc/resolv.conf.build /etc/resolv.conf
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d; chmod +x /usr/sbin/policy-rc.d   # no service starts in the chroot

# Docker Engine + compose plugin from Docker's Debian repository
apt-get update -q
apt-get install -y -q ca-certificates curl gnupg avahi-daemon unattended-upgrades
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -q
apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
# SSD health for the start page's "This box" check (no recommends: they pull in a mail server)
apt-get install -y -q --no-install-recommends smartmontools
# Keep Docker's logs from filling the SSD
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'JSON'
{ "log-driver": "local", "log-opts": { "max-size": "10m", "max-file": "3" } }
JSON
systemctl enable docker.service containerd.service avahi-daemon.service

# HDMI wall display: cage (single-app Wayland compositor) + Chromium in kiosk mode, wlr-randr for the
# screen's off hours. No desktop. Started only when a screen is connected (solar-display.service).
apt-get install -y -q --no-install-recommends cage wlr-randr v4l-utils fonts-dejavu-core libgl1-mesa-dri libegl1 libgles2
apt-get install -y -q --no-install-recommends chromium || apt-get install -y -q --no-install-recommends chromium-browser

# Hostname -> http://solar-monitor.local/ (mDNS via avahi)
echo solar-monitor > /etc/hostname
sed -i 's/^127\.0\.1\.1.*/127.0.1.1\tsolar-monitor/' /etc/hosts
grep -q '127.0.1.1' /etc/hosts || echo -e '127.0.1.1\tsolar-monitor' >> /etc/hosts

# A local admin account with no password (console/SSH login stays off until the owner sets one,
# e.g. with userconf.txt + an empty "ssh" file on the boot partition). Skip the first-boot user wizard.
id solar >/dev/null 2>&1 || useradd -m -s /bin/bash -G sudo,docker,adm solar
passwd -l solar >/dev/null
systemctl disable userconfig.service 2>/dev/null || true
rm -f /etc/systemd/system/getty@tty1.service.d/autologin.conf
# SSH stays disabled (Raspberry Pi OS default); creating "ssh" on the boot partition enables it.

# Default time zone until the setup page sets the site's
ln -sf /usr/share/zoneinfo/UTC /etc/localtime; echo UTC > /etc/timezone

# The solar monitor itself
/opt/solar-monitor/src/tools/install.sh

# Debian security updates for the OS (unattended-upgrades' default: security only, no reboots)
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > /etc/apt/apt.conf.d/20auto-upgrades

# Clean up
apt-get clean
rm -rf /var/lib/apt/lists/* /usr/sbin/policy-rc.d /etc/resolv.conf.build
mv /etc/resolv.conf.orig /etc/resolv.conf 2>/dev/null || true
