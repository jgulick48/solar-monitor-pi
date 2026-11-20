#!/bin/bash
# solar-update: install a newer version of the solar monitor from GitHub.
#
# Follows the latest GitHub *release* by default (channel "release"); channel
# "main" follows every commit on main (for test boxes). Settings in
# /opt/solar-monitor/stack/state/update.json: {"enabled": true, "channel": "release"}.
# The box's generated config, passwords and data are never touched. If the
# stack doesn't come back up after an update, the previous version is restored.
#
#   tools/update.sh [--check-only] [--manual] [--force]
#   --manual: run even if automatic updates are off (the setup page's button)
#   --force:  reinstall even if already on the latest version
set -uo pipefail
REPO=${SOLAR_REPO:-jgulick48/solar-monitor-pi}
BASE=${SOLAR_BASE:-/opt/solar-monitor}
STATE=$BASE/stack/state
RESULT=$STATE/update-status.json
CHECK_ONLY=0; FORCE=0; MANUAL=0
for a in "$@"; do case "$a" in --check-only) CHECK_ONLY=1 ;; --force) FORCE=1 ;; --manual) MANUAL=1 ;; esac; done

exec 9>"${SOLAR_LOCK:-/run/solar-update.lock}"
flock -n 9 || { echo "another update is running"; exit 0; }

now() { date '+%Y-%m-%d %H:%M'; }
setting() { python3 -c "import json,sys;d=json.load(open('$STATE/update.json')) if __import__('os').path.exists('$STATE/update.json') else {};print(d.get('$1','$2'))"; }
record() {  # status, message, latest
  python3 - "$RESULT" "$1" "$2" "${3:-}" "$(cat $BASE/VERSION 2>/dev/null)" "$(now)" <<'PY'
import json, sys
path, status, msg, latest, cur, when = sys.argv[1:7]
json.dump({"status": status, "message": msg, "latest": latest, "current": cur, "checked": when}, open(path, "w"), indent=2)
PY
  echo "$(now) $1: $2"
}

ENABLED=$(setting enabled True); CHANNEL=$(setting channel release)
if [ "$ENABLED" != "True" ] && [ "$FORCE" = 0 ] && [ "$MANUAL" = 0 ]; then record disabled "automatic updates are off"; exit 0; fi
CURRENT=$(cat $BASE/VERSION 2>/dev/null || echo unknown)

if [ "$CHANNEL" = main ]; then
  LATEST=$(curl -fsS --max-time 30 2>/dev/null "https://api.github.com/repos/$REPO/commits/main" | python3 -c 'import json,sys;print(json.load(sys.stdin)["sha"][:7])' 2>/dev/null) || { record error "could not reach GitHub"; exit 1; }
  URL="https://codeload.github.com/$REPO/tar.gz/$LATEST"
else
  LATEST=$(curl -fsS --max-time 30 2>/dev/null "https://api.github.com/repos/$REPO/releases/latest" | python3 -c 'import json,sys;print(json.load(sys.stdin)["tag_name"])' 2>/dev/null) || { record error "could not reach GitHub or no release published yet"; exit 1; }
  URL="https://codeload.github.com/$REPO/tar.gz/refs/tags/$LATEST"
fi
if [ "$LATEST" = "$CURRENT" ] && [ "$FORCE" = 0 ]; then record up-to-date "running the latest version" "$LATEST"; exit 0; fi
if [ "$CHECK_ONLY" = 1 ]; then record available "version $LATEST is available" "$LATEST"; exit 0; fi

echo "$(now) updating $CURRENT -> $LATEST"
rm -rf $BASE/src.new; mkdir -p $BASE/src.new
if ! curl -fsSL --max-time 300 "$URL" | tar xz -C $BASE/src.new --strip-components=1 || [ ! -x $BASE/src.new/tools/install.sh ]; then
  rm -rf $BASE/src.new; record error "download of $LATEST failed" "$LATEST"; exit 1
fi

compose() { docker compose --project-directory $BASE/stack "$@"; }
install_from() {  # dir version
  "$1/tools/install.sh" >/dev/null && echo "$2" > $BASE/VERSION && systemctl restart solar-web.service &&
  /usr/bin/python3 $BASE/setup/solar_setup.py --stack $BASE/stack --no-scan >/dev/null &&
  compose pull -q && compose up -d --remove-orphans
}
healthy() {  # every service running for 2 minutes, and the start page answers
  sleep "${SOLAR_HEALTH_WAIT:-120}"
  local n_bad
  n_bad=$(compose ps -a --format '{{.State}}' | grep -vc '^running$')
  [ "$n_bad" = 0 ] && curl -fsS --max-time 10 -o /dev/null ${SOLAR_HEALTH_URL:-http://127.0.0.1/}
}

rm -rf $BASE/src.prev; [ -d $BASE/src ] && mv $BASE/src $BASE/src.prev
mv $BASE/src.new $BASE/src
if install_from $BASE/src "$LATEST" && healthy; then
  record updated "updated from $CURRENT to $LATEST" "$LATEST"; exit 0
fi
echo "$(now) $LATEST is not healthy, rolling back to $CURRENT"
rm -rf $BASE/src.failed; mv $BASE/src $BASE/src.failed
if [ -d $BASE/src.prev ]; then
  mv $BASE/src.prev $BASE/src
  install_from $BASE/src "$CURRENT"
fi
record rolled-back "$LATEST failed its health check; kept $CURRENT" "$LATEST"
exit 1
