# Roadmap

Ideas for after the first box is running at the neighbor's. Nothing here is started. Done so far (v0.2.4): solar
forecast, daily summary, USB backups, remote access to Home Assistant, generator log and service reminder, battery
health, remote support.

## Before relying on it

1. **Field test on the real Pi**: both 6000XP dongles found and read; a real USB stick (backup and a test restore);
   phone notifications from Home Assistant; the HDMI display and cameras; remote access and remote support with real
   tunnel tokens. So far these were only tested on the build server or in containers.
2. **Upgrade test**: a box on an older release updating itself to the newest one (new container images, the generator
   log reading back through a year of history on its first run).

## Planned (decided 2026-11-25)

Eleven features in four phases, each its own PR and release. The design, with the reasons, is on the wiki:
[Planned features](https://github.com/jgulick48/solar-monitor-pi/wiki/Planned-features).

**Phase 1: foundations**

- **Weekly usage prediction.** Expected load for each hour of the week (the median of the last 4 to 8 weeks, scaled
  by how the last 3 days compare with their usual level) instead of a flat 3-day average. Used by the generator
  advice, "lasts about" and the Phase 2 features. The box backtests it against the flat average on the site's own
  history and uses whichever predicts better; flat until there are 2 weeks of data.
- **Dongle health.** Per dongle: time since its last data and reconnects per day, on the setup page and in Home
  Assistant, with an alert for "connected but quiet", which the offline alert misses.

**Phase 2: advice**

- **"Stop the generator".** While it runs, the overnight simulation is repeated from the current battery level:
  "about 40 minutes more" (from the measured charge rate), then "the generator can be shut down", announced once
  per run on the display, by voice and by phone.
- **"Good time to run big loads".** With the battery nearly full and more solar forecast than the expected use:
  "about 2 kW spare until 3 PM" on the display and as a Home Assistant sensor. Phone notification off by default.
- **Storm and dull-spell outlook.** A daily look 3 to 5 days ahead (forecast solar against expected use): "three dull
  days ahead: expect about 6 generator hours; propane is at 34%". Weather warnings add a short checklist. Needs the
  solar forecast extended from 2 days to 5.
- **Freeze protection.** A critical alert when a chosen temperature (slab, probe, room) falls below a threshold
  (40 °F by default), and earlier when a hard freeze is forecast while the boiler is faulted or the battery is low.
  Works without the boiler module, with any Home Assistant temperature sensor.

**Phase 3: convenience**

- **Snapshot notifications.** Doorbell and person alerts with the camera's picture, through the notifications
  module's quiet hours, with a cooldown per camera. Home Assistant app first, then ntfy.
- **First-run check.** One page with a green or red line per item (dongles, data being recorded, Home Assistant,
  forecast, notifications, cameras, backup, remote access, disk and temperature); each module adds its own checks.
- **Config export and import.** The settings as one file, imported on another box with a preview. Passwords and API
  keys are left out unless asked for, and then encrypted with a passphrase. History stays with backups.
- **Year-over-year comparison.** A History page: each month against the same month last year (solar, use, generator
  hours, propane, water), and a Grafana dashboard.

**Phase 4: follow another site** (same network or VPN: built; through the tunnel: later)

- A "Share this site" switch on the source box (the bridge's raw-frame feed and a read-only share code); the
  following box pastes the code and runs the bridge's mirror, so the remote inverter appears like a local one
  without a second connection to the dongle. Same network or VPN first; through the source's remote-access tunnel
  later (needs MQTT over WebSocket in the bridge).

## Feature ideas

Most useful first.

### 1. Inverter and panel-string checks
With two inverters, one can stop working while the other carries everything, which looks normal at a glance.
- Alert when one inverter's output, solar or communication drops out while the other's doesn't.
- Compare each PV string with its siblings (same direction and size): one making much less than the others points
  to a tripped breaker, shading or a failed connector.
- Data is already there: `lxp_p_pv_1/2/3` per inverter, `lxp_fault_code`, `lxp_bridge_connected`.

### 2. Health of the box itself
Today a dying SSD would only show up as missing data.
- SSD: wear (SMART / NVMe health, if the USB bridge passes it through) and free space.
- Pi: temperature, under-voltage and throttling (`vcgencmd get_throttled`).
- Dongles: Wi-Fi signal, reconnects per day.
- Alerts through Home Assistant like the others; a "Box health" card on the start page.

### 3. Solar underperformance alert
Actual solar well below the forecast (or the weather station's irradiance) for a few hours while the battery isn't
full, so the inverters aren't cutting it back: snow, dirt, a failed string. The ranch's "solar panels snow alert" is a
working example (actual < 40% of potential for 2 hours, potential > 3 kW, battery below 95%).

### 4. Overload warning
Alert when the house load comes close to what the inverters can supply (2 × 6000XP: about 12 kW combined, less per
leg), before a big load trips them. Show peak load per day on the dashboard.

### 5. Generator fuel and cost
- Fuel per run from the run's kWh and the generator type and fuel (setup page already asks for both; typical
  gallons per kWh by size and load, or a measured figure the owner enters).
- Monthly fuel and cost (fuel price is already on the setup page for the advisor).

### 6. Monthly report
One notification (and a page) per month: solar made, power used, generator hours and fuel, lowest battery, battery
health, backups, compared with the same month last year once there is a year of data.

## Smaller items

- Grafana: a dashboard for the battery health and generator log figures.
- Advisor: use the generator log's runs instead of recomputing generator time from raw history.
- Setup page: a "test notification" button to check that phones are signed in.
