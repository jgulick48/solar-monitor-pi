#!/bin/bash
# Build the solar monitor SD/SSD image from Raspberry Pi OS Lite (arm64).
#
# Runs as root on a Linux host (or a --privileged container with /dev mounted)
# that can execute arm64 binaries (binfmt_misc qemu-aarch64 with the F flag).
# Needs: curl xz-utils parted e2fsprogs util-linux skopeo rsync.
#
#   sudo image/build.sh [output-dir]
#
# Output: solar-monitor-pi-<date>.img.xz and .sha256 in output-dir (default ./out).
set -euo pipefail

RASPIOS_VERSION=${RASPIOS_VERSION:-2026-10-06}
RASPIOS_FILE=${RASPIOS_FILE:-${RASPIOS_VERSION}-raspios-trixie-arm64-lite.img.xz}
RASPIOS_URL=${RASPIOS_URL:-https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-${RASPIOS_VERSION}/${RASPIOS_FILE}}
GROW_MB=${GROW_MB:-6144}          # extra room for Docker and the pre-loaded container images
REPO=$(cd "$(dirname "$0")/.." && pwd)
OUT=$(mkdir -p "${1:-$REPO/out}" && cd "${1:-$REPO/out}" && pwd)
WORK=$(mktemp -d /var/tmp/solar-image.XXXXXX)
NAME=solar-monitor-pi-$(date +%Y-%m-%d)
ROOT=$WORK/root
LOOP=""

cleanup() {
  set +e
  for m in boot/firmware dev/pts dev proc sys run; do mountpoint -q "$ROOT/$m" && umount -l "$ROOT/$m"; done
  mountpoint -q "$ROOT" && umount -l "$ROOT"
  [ -n "$LOOP" ] && losetup -d "$LOOP"
  rm -rf "$WORK"
}
trap cleanup EXIT

# arm64 emulation: in a container binfmt_misc may not be mounted even though the host registered qemu-aarch64 (F flag)
[ -e /proc/sys/fs/binfmt_misc/status ] || mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc 2>/dev/null || true
[ -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ] || { echo "arm64 emulation (binfmt qemu-aarch64) is not available" >&2; exit 1; }

echo "== Raspberry Pi OS Lite $RASPIOS_VERSION"
mkdir -p "$REPO/.cache"
CACHED=$REPO/.cache/$RASPIOS_FILE
if [ ! -s "$CACHED" ]; then curl -fL --retry 3 -o "$CACHED.part" "$RASPIOS_URL" && mv "$CACHED.part" "$CACHED"; fi
curl -fsL "$RASPIOS_URL.sha256" -o "$WORK/sha256"
(cd "$(dirname "$CACHED")" && sha256sum -c <(awk -v f="$RASPIOS_FILE" '{print $1"  "f}' "$WORK/sha256"))
xz -dc "$CACHED" > "$WORK/$NAME.img"

echo "== grow the root partition by ${GROW_MB} MB"
truncate -s +"${GROW_MB}M" "$WORK/$NAME.img"
parted -s "$WORK/$NAME.img" resizepart 2 100%
LOOP=$(losetup -fP --show "$WORK/$NAME.img")
e2fsck -fy "${LOOP}p2" >/dev/null || [ $? -le 1 ]
resize2fs "${LOOP}p2" >/dev/null

echo "== mount"
mkdir -p "$ROOT"
mount "${LOOP}p2" "$ROOT"
mount "${LOOP}p1" "$ROOT/boot/firmware"
mount -t proc proc "$ROOT/proc"; mount -t sysfs sys "$ROOT/sys"
mount --bind /dev "$ROOT/dev"; mount --bind /dev/pts "$ROOT/dev/pts"; mount -t tmpfs tmpfs "$ROOT/run"

echo "== copy the solar monitor"
mkdir -p "$ROOT/opt/solar-monitor/src"
rsync -a --delete --exclude .git --exclude .cache --exclude out --exclude '__pycache__' \
      --exclude 'stack/.env' --exclude 'stack/state' --exclude 'stack/homeassistant/' \
      --exclude 'stack/bridge/config.yml' --exclude 'stack/mosquitto/passwd' \
      "$REPO/" "$ROOT/opt/solar-monitor/src/"
# Version: the release tag when building a tag, else the commit
VERSION=${SOLAR_VERSION:-$(git -C "$REPO" describe --tags --exact-match 2>/dev/null || git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)}
echo "$VERSION" > "$ROOT/opt/solar-monitor/VERSION"; echo "$VERSION" > "$ROOT/opt/solar-monitor/src/VERSION"
echo "   version $VERSION"

echo "== configure the OS (chroot)"
cp /etc/resolv.conf "$ROOT/etc/resolv.conf.build"
chroot "$ROOT" /bin/bash /opt/solar-monitor/src/image/chroot-setup.sh

echo "== container images (arm64) for an offline first boot"
mkdir -p "$ROOT/opt/solar-monitor/images"
grep -E '^\s+image:' "$REPO/stack/compose.yaml" | awk '{print $2}' | while read -r img; do
  ref=$img; case "$img" in */*/*) ;; */*) ref=docker.io/$img ;; *) ref=docker.io/library/$img ;; esac
  file=$ROOT/opt/solar-monitor/images/$(echo "$img" | tr '/:' '__').tar
  echo "   $img"
  skopeo copy -q --override-os linux --override-arch arm64 "docker://$ref" "docker-archive:$file:$img"
done
du -sh "$ROOT/opt/solar-monitor/images"

echo "== finish"
sync
for m in boot/firmware dev/pts dev proc sys run; do umount "$ROOT/$m"; done
umount "$ROOT"
e2fsck -fy "${LOOP}p2" >/dev/null || [ $? -le 1 ]
losetup -d "$LOOP"; LOOP=""
xz -T0 -6 -c "$WORK/$NAME.img" > "$OUT/$NAME.img.xz"
(cd "$OUT" && sha256sum "$NAME.img.xz" > "$NAME.img.xz.sha256")
ls -lh "$OUT/$NAME.img.xz"
