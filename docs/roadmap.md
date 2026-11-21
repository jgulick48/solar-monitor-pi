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
