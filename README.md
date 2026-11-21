# solar-monitor-pi

A Raspberry Pi image for monitoring a solar installation that uses EG4 / LuxPower inverters with the
WiFi datalogger dongle. Plug the Pi into the same network as the dongles and it finds them, starts
collecting data and serves dashboards. Nothing is sent to the internet, and the inverters are only
read, never changed.

## What's on it

| Part | What it does | Where |
|---|---|---|
| Setup page | Start page: dongles found, links, site setup form | `http://<pi>/` (port 80) |
| [lxp-bridge-go](https://github.com/jgulick48/lxp-bridge-go) | Reads every inverter's dongle (read-only mode) and publishes the values | internal |
| Mosquitto | MQTT broker between the bridge and Home Assistant | localhost only |
| VictoriaMetrics | Long-term history (5 years) of every inverter value | localhost only |
| Grafana | Dashboards, viewable without logging in | `http://<pi>:3000` |
| Home Assistant | Inverter entities (MQTT discovery), the Solar dashboard, alerts to phones | `http://<pi>:8123` |
| Advisor | Weekly analysis: batteries, panels, inverter or generator? | `http://<pi>/advisor` |
| Backups | Nightly backup to a USB stick, and restore | `http://<pi>/backup` |
| Generator log | Every generator run, hours since the last service, service reminder | `http://<pi>/generator` |
| Wall display | Dashboard and up to two cameras on a screen plugged into the Pi's HDMI port | `http://<pi>/display` |
| go2rtc | Turns the cameras' RTSP(S) streams into video the display's browser can play | localhost only |

## Hardware

- Raspberry Pi 4 (2 GB is enough, 4 GB recommended) and the official 5.1 V 3 A power supply.
- A **128 GB SSD** in a USB 3 enclosure with UASP (or a USB SSD such as a Samsung T7 / SanDisk Extreme).
  Five years of history for two inverters is a few GB; the extra space spreads the writes so the drive
  lasts. SATA-in-USB enclosures are safer than NVMe on a Pi 4 (its USB ports share about 1.2 A).
- Wired Ethernet on the same network (subnet) as the inverter dongles.
- Optional: a 1080p monitor or TV on the Pi's HDMI port (micro-HDMI 0, next to the power socket) for the
  wall display. Plugging it in, even later, starts the display.

## First boot

1. Flash the image to the SSD, plug it into the Pi's blue USB 3 port, connect Ethernet, power on.
2. Wait about five minutes, then open `http://solar-monitor.local/` (or the Pi's IP from the router).
3. The start page lists the dongles it found. Click **Finish setup** and enter the location, time zone,
   batteries, panels and generator. Saving applies it to Home Assistant and Grafana.
4. Open Home Assistant from the start page and create your account. The inverter entities are already
   there.

Give each dongle a fixed IP (DHCP reservation) in the router if you can. If a dongle's IP changes anyway,
the hourly scan finds it again by its serial number.

## Wall display

![Wall display at 1920x1080 (test data and test-pattern cameras)](docs/display.png)

With a screen on the HDMI port the Pi shows a dashboard, laid out for 1920x1080:

- **Cameras** (left half, two stacked 960x540 tiles; without cameras the power figures use the whole screen).
- **Battery** (%, kWh stored, how long it lasts at the usual use), **solar**, **house load**, **battery power**
  (charging/discharging) and **generator**, with today's totals and the last 24 hours as a chart.
- **Notes**: dongles offline, inverter faults, low battery and, for a hand-started generator, from mid-afternoon on
  whether the battery lasts until the sun is back ("Start the generator before dark: ... about 4.2 kWh more is
  needed"), from the average use over the last three days and the solar forecast: a dull day tomorrow counts too
  ("tomorrow looks dull (about 6 kWh of solar expected) ...").

### Solar forecast

`solar-collector` keeps Open-Meteo's irradiance forecast for each array (the same hourly request it already makes)
and turns it into expected output per hour for today and the next two days (`state/forecast.json`), scaled by the
advisor's calibration of this site's panels once it has one (0.8 of the model until then) and capped at the
inverters' solar input. It shows on the display's solar tile, as `sensor.site_solar_forecast_today` / `_tomorrow` in
Home Assistant, and drives the generator advice, which steps the battery through tonight and tomorrow in 15-minute
steps (forecast solar against the usual use) instead of assuming the sun takes over two hours after sunrise. With no
fresh forecast (no internet) the advice falls back to that rule.

It runs Chromium in kiosk mode inside `cage` (a one-app Wayland compositor; no desktop) as the locked
`solar-display` account (`solar-display.service`). It only starts when the display is turned on on the setup page,
the kiosk software is installed and a screen is connected; plugging a screen in starts it (udev rule). The browser's
profile lives in RAM. The setup page can turn the screen off for set hours (`solar-display-power.timer`,
via `wlr-randr`). The same page is at `http://<pi>/display` from other computers, without the cameras.

### Cameras (UniFi Protect)

Setup page, *Screen and cameras*: name each camera and paste its stream link. In UniFi Protect: open the camera
-> Settings -> Advanced -> RTSPS, turn on **Medium** quality (720p: sharp enough for a 960x540 tile and light on
the Pi; the Pi 4 decodes H.264 in hardware) and copy the `rtsps://<console>:7441/<token>?enableSrtp` link.
`go2rtc` reads the streams (RTSPS without checking Protect's self-signed certificate) and serves them to the
display over MSE on 127.0.0.1 only, so the pictures aren't reachable from the network. The links are kept in
`state/display.json` and `go2rtc/go2rtc.yaml` (mode 600) and aren't shown again on the setup page (the token in
the link is effectively a password). Any camera with an RTSP/RTSPS H.264 stream works the same way.

For camera entities, motion and doorbell events in Home Assistant, add the **UniFi Protect** integration in
Home Assistant (Settings -> Devices & services; it's usually discovered automatically) with a local Protect user.

## Home Assistant: Solar dashboard and alerts

![Solar dashboard in Home Assistant (live data, fresh install)](docs/ha-dashboard.png)

Home Assistant comes with a **Solar** dashboard in the sidebar and alerts, from `packages/solar_monitor.yaml`:

- **Site sensors** (`sensor.site_*`, `binary_sensor.site_*`): battery level, stored kWh and how long it lasts at the
  usual use, solar, house, battery and generator power, today's energy, dongles connected, fault code, alerts and the
  generator advice. They come from the box's `/api/ha`, the same totals and alerts as the wall display, added up over
  all inverters, so they don't depend on the inverters' own entity names.
- **Alerts**: battery low (5 min), inverter fault, dongle offline (10 min), no data (10 min) and, for a hand-started
  generator, *start the generator* (mid-afternoon, again 30 minutes before sunset if it hasn't been started; cleared
  when it runs). Each one shows in Home Assistant's notifications and on every phone signed in with the **Home
  Assistant app** (the alert is withdrawn from the phone once it clears). They can be switched off on the dashboard.
- **Daily summary** (setup page, *Daily summary*: on/off and time, default midnight): "24 kWh solar, 21 kWh used,
  battery down to 38%, generator 1.5 h (6.0 kWh). Today: about 13 kWh of solar expected." Sent before 06:00 it covers
  the day before, later the day so far; on Sundays it adds the week's totals and the advisor's latest finding.
- **Energy dashboard**: solar and battery are filled in on the first start; changes made in Home Assistant are kept.
  The generator isn't in it (Home Assistant has no off-grid generator source).

The package and the dashboard are replaced on every update (and Home Assistant restarted when they change), so edit
copies, not the originals: make your own dashboard in Settings -> Dashboards and your own automations.

## Backups (USB stick)

Plug a USB stick into the Pi (FAT32, exFAT, ext4 or NTFS, as most sticks come; 16 GB or more) and choose it on the
**Backups** page (`http://<pi>/backup`). The box never formats it. Every night at about 01:30 it backs up the settings
and passwords (`state/`), Home Assistant (config, accounts, dashboards, and its database as a consistent copy), the camera
config and, unless turned off, the whole inverter history (a VictoriaMetrics snapshot). The backups are
[restic](https://restic.net) snapshots in a `solar-monitor-backup` folder (deduplicated, so each night only adds what
changed); 7 daily, 4 weekly and 6 monthly are kept. The stick is mounted only while a backup runs, so it can be pulled
out at any other time.

**The key to the backups is on the stick, next to them**, so a box whose SSD died can be restored from the stick alone.
That means whoever has the stick can read the box's passwords and camera links: keep it in the Pi.

To restore (for example onto a new SSD): flash the image, start the box, open the Backups page, choose the stick and
**Restore from the stick**. It stops everything, puts back the newest backup, rebuilds the generated config and starts
again (a few minutes). Home Assistant alerts (and the start page shows it) when there has been no good backup for two
days.

restic runs in a container (`backup` service in `stack/compose.yaml`, profile `backup`), so boxes updated from older
versions download it on the first backup.

## Generator log and service reminder

The box finds every generator run in the inverter history, a minute at a time, two ways:

- **On the inverters' GEN input** (usually auto-started by the dry contact): generator input power above 200 W.
  Service every **200 hours**.
- **Through a DC charger** such as a Chargeverter: the battery BMS's charging power more than 500 W above what the
  inverters put into the battery. Service every **100 hours**.

Dips under 5 minutes don't end a run; runs under 3 minutes (10 for the charger estimate) are ignored. The first time,
it works through the existing history (up to 400 days). The **Generator** page (`http://<pi>/generator`) lists the runs
(start, hours, energy, peak) and the hours since the last service, with a **Serviced** button (enter the hours it has
already run if the oil was changed earlier). Home Assistant gets *Site generator hours since service*, *last run*,
*running* and *service due* sensors and sends a reminder when a service is due. The generator power everywhere (wall
display, Home Assistant, daily summary) now includes a generator charging through a DC charger.

State: `state/generator.json`; `setup/genlog.py` (run by the collector every 5 minutes; `genlog.py update` by hand).

## Advisor

The start page's **Advisor** answers "what would help this system most?" from the site's own history. It
runs every week (Sunday night) or on demand:

1. **Solar that could have been made**: sunlight on each array's tilt and direction, from a local weather
   station or Open-Meteo, calibrated against the inverters' actual output while the batteries weren't
   full. That shows the solar lost because the batteries were full, which the inverters' own readings hide.
2. **What happened**: load, solar, lost solar, low battery days and generator charging by month, plus
   charging the inverters didn't do themselves (a Chargeverter or other DC charger shows up as the
   battery's BMS current being higher than the inverters' battery power).
3. **Options compared**: the period is replayed with +1/+2/+4 batteries, +25/50/100% panels (capped at
   the inverters' solar input, with an extra inverter where that limits it) and an automatic generator.
   For each it shows the energy the batteries would have lacked to stay above 20% without a generator,
   the days that happened, generator hours and fuel to make it up, wasted solar, and with prices from
   the setup page, cost and payback.
4. **Capacity**: peak house load against inverter output, and the array against the inverters' solar input.

It needs a week of data for a first rough answer and a full winter before the batteries vs. panels
question is reliable; the page says how confident it is.

### Sunlight data

Set on the setup page:
- **Open-Meteo** (default): hourly irradiance for each array's tilt and direction from the free
  Open-Meteo API, fetched by `solar-collector`. It sends the site's location to Open-Meteo and tends to
  see more sun than a local meter on cloudy days.
- **A weather station on the network**: *Scan for weather stations* on the start page finds Ecowitt
  gateways/consoles (UDP discovery) and WeatherFlow Tempest hubs (UDP broadcasts); or enter its IP
  (Ecowitt local HTTP API, Davis WeatherLink Live). Read every minute.
- **A station that pushes its data**: point the station's custom server at the box. Ecowitt (WS View,
  *Customized*, protocol Ecowitt): server = the box's IP, path `/weather/ecowitt`, port 80. Ambient
  Weather: path `/weather/ambient?`.

Tested against the ranch's year of data: the advisor's monthly figures match a hand analysis, and on
90 days Open-Meteo found ~20-30% more possible solar than the local meter in Oct-Nov (same conclusion).

## Logging in to the Pi itself

Not needed for normal use. The image has an admin account `solar` with no password and SSH turned off.
To get a shell, power off, put the SSD in a computer and create two files on the `bootfs` partition:
an empty file named `ssh`, and `userconf.txt` containing `solar:<hash>` where the hash comes from
`openssl passwd -6`. On the next boot SSH is on and `solar` has that password.

## Updates

- **The solar monitor** checks GitHub once a day (between 03:30 and 05:30) and installs a newer
  **release**. Updates only follow published releases, not every commit, so nothing reaches a box until
  you publish one. It downloads the release, reinstalls the files and units, pulls any new container
  images and restarts the stack. If every container isn't running and the start page isn't answering
  two minutes later, it puts the previous version back. The box's own settings, passwords and history
  are never touched.
- The start page shows the version and the last update result, and has a **Check for updates now**
  button. Automatic updates can be turned off, or switched to follow every commit on `main` (for a
  test box), on the setup page.
- **The operating system** installs Debian security updates by itself (`unattended-upgrades`).

### Publishing a release

1. Merge the changes to `main`.
2. Create a release with a new tag, e.g. `v0.2.0`:
   `gh release create v0.2.0 --repo jgulick48/solar-monitor-pi --generate-notes`
3. Boxes pick it up within a day. To build a fresh image for it, check out the tag and run
   `sudo image/build.sh`; the image reports the tag as its version.

Container versions are pinned in `stack/compose.yaml`; bump them there (and release) to update
Home Assistant, Grafana and the rest.

## How it works

- `setup/solar_setup.py` runs at boot (`solar-setup.service`) and hourly (`solar-setup.timer`). It sweeps
  the local network for TCP port 8000 and identifies LuxPower dataloggers by listening to the frames they
  send: heartbeats carry the datalog serial, and replies to the EG4 cloud's polls carry the inverter
  serial. If nothing is polling a dongle, one read-only request (input registers 0-39 with a placeholder
  inverter serial) gets the inverter's reply. It then writes the bridge config (one entry per dongle,
  `read_only: true`), generates per-box passwords (MQTT, Grafana admin) and, on first run, seeds Home
  Assistant with the MQTT integration already configured.
- `setup/solar_web.py` (`solar-web.service`) serves the start page and the setup form. Saving writes
  `state/site.json`, Home Assistant's core settings (`homeassistant/core.yaml`) and the
  `packages/site.yaml` sensors (battery capacity, array size, generator mode, low battery level), sets
  the system and Grafana time zone, and restarts what changed.
- `stack/compose.yaml` runs the containers (`solar-stack.service`).
- `tools/install.sh` installs everything under `/opt/solar-monitor` on a Debian / Raspberry Pi OS host
  with Docker; the image build and the updater use it.
- `tools/update.sh` (`solar-update.timer`) is the updater described above.
- `setup/weather.py` finds and reads weather stations; `setup/collector.py` (`solar-collector.service`)
  stores station and Open-Meteo sunlight in VictoriaMetrics; `setup/advisor.py` (`solar-advisor.timer`)
  is the advisor.
- `stack/homeassistant-seed/` is Home Assistant's first-run config; its `packages/solar_monitor.yaml` and
  `dashboards/solar.yaml` are kept current by `solar_setup.py` on every run, which also fills in the Energy
  dashboard once.
- `setup/backup.py` (`solar-backup.timer`) backs up to the USB stick and restores from it.
- `setup/display.py` is the wall display (page, data, settings, go2rtc config, screen schedule);
  `setup/kiosk.sh` starts Chromium; `system/` has the kiosk PAM session and the HDMI hotplug rule.
- `image/build.sh` builds the image: it takes Raspberry Pi OS Lite (arm64, checksum-verified), grows the
  root partition, installs Docker, avahi (`solar-monitor.local`), unattended-upgrades and the kiosk (cage, Chromium) in a chroot, runs
  the installer and stores the arm64 container images in the image so the first boot works offline
  (`solar-images.service` loads them). It needs root and arm64 emulation (binfmt `qemu-aarch64`).

## Development

Run the stack on any Docker host against a real dongle:

```sh
python3 setup/solar_setup.py --stack "$PWD/stack" --no-scan --host <dongle-ip>   # or omit --no-scan to sweep the LAN
docker compose --project-directory stack up -d
python3 setup/solar_web.py --stack "$PWD/stack" --port 8088
```

`SOLAR_COMPOSE` overrides the compose command (for example a containerised `docker:cli compose`).
`tools/gen_dashboards.py` regenerates the Grafana dashboards.

## Status

Tested on an amd64 Docker host against an EG4 18kPV dongle: discovery, bridge, broker, VictoriaMetrics,
Grafana, Home Assistant (185 inverter entities by MQTT discovery) and the setup form all work.

Solar dashboard and alerts: tested in Home Assistant 2026.11.2 against the 18kPV's live data: the package loads, all
22 site entities and the 6 automations come up, the dashboard and Energy dashboard render, and the generator alert
was raised and withdrawn.

Wall display: tested with Google Chrome at 1920x1080 against test data and two go2rtc H.264 test streams
(`python3 -m unittest discover -s tests`). The kiosk itself (cage + Chromium on the Pi, hardware video decoding,
screen off hours) needs a real Pi and screen.

To do:
- First boot of the image on a real Raspberry Pi 4, with a screen and the UniFi Protect cameras.
- Alerts on a phone: tested in Home Assistant's own notifications; the push through the Home Assistant app still
  needs a real phone.
- EG4 6000XP: confirm the bridge's register map against a real 6000XP.
