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
| Home Assistant | Inverter entities (MQTT discovery), dashboards, alerts | `http://<pi>:8123` |

## Hardware

- Raspberry Pi 4 (2 GB is enough, 4 GB recommended) and the official 5.1 V 3 A power supply.
- A **128 GB SSD** in a USB 3 enclosure with UASP (or a USB SSD such as a Samsung T7 / SanDisk Extreme).
  Five years of history for two inverters is a few GB; the extra space spreads the writes so the drive
  lasts. SATA-in-USB enclosures are safer than NVMe on a Pi 4 (its USB ports share about 1.2 A).
- Wired Ethernet on the same network (subnet) as the inverter dongles.

## First boot

1. Flash the image to the SSD, plug it into the Pi's blue USB 3 port, connect Ethernet, power on.
2. Wait about five minutes, then open `http://solar-monitor.local/` (or the Pi's IP from the router).
3. The start page lists the dongles it found. Click **Finish setup** and enter the location, time zone,
   batteries, panels and generator. Saving applies it to Home Assistant and Grafana.
4. Open Home Assistant from the start page and create your account. The inverter entities are already
   there.

Give each dongle a fixed IP (DHCP reservation) in the router if you can. If a dongle's IP changes anyway,
the hourly scan finds it again by its serial number.

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
- `image/build.sh` builds the image: it takes Raspberry Pi OS Lite (arm64, checksum-verified), grows the
  root partition, installs Docker, avahi (`solar-monitor.local`) and unattended-upgrades in a chroot, runs
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

To do:
- First boot of the image on a real Raspberry Pi 4.
- Home Assistant dashboards and alerts (low battery, inverter faults, dongle offline), the solar forecast,
  and the "start the generator before dark" check for sites with a manual generator.
- EG4 6000XP: confirm the bridge's register map against a real 6000XP.
- lxp-bridge-go: in read-only mode, 18 settings entities are announced as sensors with
  `entity_category: config`, which Home Assistant rejects (should be `diagnostic`).
