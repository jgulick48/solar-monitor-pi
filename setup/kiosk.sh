#!/bin/sh
# Chromium in kiosk mode on the wall display page, run inside cage by solar-display.service.
# Profile and cache live in the user's runtime directory (RAM), so nothing is written to the SSD
# and there is no "restore pages" prompt after a power cut.
# Screen mode from the TV's EDID (its preferred mode; 1080p on 4K TVs). Never stops the kiosk.
/usr/bin/python3 /opt/solar-monitor/setup/display.py screen-mode 2>&1 || true
# Sound (camera chimes) to the HDMI port the screen is on.
/usr/bin/python3 /opt/solar-monitor/setup/display.py screen-audio 2>&1 || true
B=/usr/bin/chromium; [ -x "$B" ] || B=/usr/bin/chromium-browser
exec "$B" --kiosk --ozone-platform=wayland --noerrdialogs --disable-infobars --no-first-run \
  --disable-session-crashed-bubble --disable-features=Translate,TranslateUI --disable-pinch \
  --overscroll-history-navigation=0 --check-for-update-interval=31536000 \
  --autoplay-policy=no-user-gesture-required --password-store=basic \
  --user-data-dir="${XDG_RUNTIME_DIR:-/tmp}/chromium" --disk-cache-size=33554432 \
  http://127.0.0.1/display
