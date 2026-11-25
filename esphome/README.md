# Boiler probes (ESPHome)

`boiler-probes.yaml` turns a **Waveshare ESP32-P4-ETH** (with its PoE module), plugged into the **carrier board**
(`carrier/`), into the temperature board for the boiler module: up to eight DS18B20 probes, each on its own port,
plus the wired call for heat, two pump current clamps and a small display, all powered and connected by a single
Ethernet cable (PoE). It joins Home Assistant through Home Assistant's ESPHome integration; the solar monitor box
reads the readings from Home Assistant. The wiki's *Boiler, propane and notifications* page has the design.

## Parts

| Part | Notes |
| --- | --- |
| Waveshare ESP32-P4-ETH with the PoE module | ESP32-P4 with IP101 Ethernet; 2 × 20 header pins underneath, which plug into the carrier |
| Carrier board | `carrier/`: 8 pluggable probe ports with their pull-ups, the heat-call and clamp inputs, OLED and button headers; Gerbers for a through-hole and an SMD version, and the through-hole parts list in its README |
| Enclosure (3D printed) | `enclosure/`: base and cover for the carrier and the board, with the ports in its walls and an OLED window (some sizes still to be measured) |
| PoE switch port or injector | 802.3af |
| 3 to 8 × DS18B20 waterproof probes | Stainless sleeve with **silicone** cable (rated to about 125 °C); 6 mm sleeves fit most wells |
| 10 × 3-pin 5.08 mm pluggable terminal blocks | 8 probe ports, HEAT and CT (e.g. uxcell's sets) |
| Optional: 0.96" SSD1306 I²C OLED module and a 12 mm panel-mount momentary button | The display in the top, and the WAKE button beside it |
| Optional: 2 × SCT-013 current clamps (1 V output) | Boiler and floor pump current; the bias parts are on the carrier |
| Thermal paste, pipe clamps, pipe insulation | For the pipe-mounted probes |

## Probes

Each probe plugs into its own port, and each port is its own 1-Wire bus (with its own 4.7 kΩ pull-up on the
carrier), so there are **no addresses to find**: a probe is the port it's plugged into, and a damaged cable only
takes out its own probe. Three probes are the minimum; each further one adds accuracy or a reading. A port with no
probe reads *unknown* in Home Assistant.

| Port | GPIO | Name in Home Assistant | Needed? | What it gives the boiler module | Where | Read every |
| --- | --- | --- | --- | --- | --- | --- |
| P1 | 33 | Floor supply | **Required** | With P2: the heat delivered to the floor (the manifold's flow × the temperature difference) | Manifold supply well, with paste | 15 s |
| P2 | 27 | Floor return | **Required** | See P1 | Manifold return well, with paste | 15 s |
| P3 | 23 | Boiler supply | **Required** | Whether the boiler fires and how hot; the outdoor-reset check; the restart test | Metal fitting at the boiler outlet, clamped with paste, insulated over | 15 s |
| P4 | 21 | Boiler return | Optional | Whether the boiler condenses (return under about 130 °F), and the boiler loop's own difference | Boiler return fitting, as P3 | 15 s |
| P5 | 32 | Hot water outlet | Optional | Tells a hot-water draw from heating straight away; without it, the water pump's plug does | The combi's domestic hot-water outlet pipe, as P3 | 5 s |
| P6 | 26 | Slab near boiler | Optional | How the floor itself responds and lags | A sensor conduit in the slab if one was poured (mid-floor, between two tubes), else the floor surface in an inside spot out of the sun | 60 s |
| P7 | 22 | Slab far | Optional | With P6: how evenly the slab heats | As P6, at the far end of the floor's loops | 60 s |
| P8 | 20 | Probe P8 | Spare | Anything else: an outdoor or room temperature | | 60 s |

P1–P4 are along the enclosure's front (the RESET side), P5–P8 along the back.

The names are only what Home Assistant shows. The boiler module's setup has a picker for each role that lists
Home Assistant's temperature sensors, with the three required roles marked; a probe can go in another port and be
picked there, and a sensor from another device can fill a role. The module shows what each missing optional role
would add.

The board also reports **Floor difference** (supply minus return), its uptime, IP address and status. The box
applies its own learned offsets to each pair, zeroed while the pumps are off, since each DS18B20 is ±0.5 °C.

## Wiring

Every block is a 3-pin pluggable terminal: the screw plug goes on the cable, and it plugs into the port from
outside the enclosure.

| Block | Pin | Wire |
| --- | --- | --- |
| Probe ports P1–P8 | **G** | Black (GND) |
| | **D** | Yellow (DATA) |
| | **V** | Red (3.3 V) |
| HEAT | outer two pins | The 24 VAC call for heat (either way round); the middle pin stays empty |
| CT | **CT1** | One wire of the boiler pump's clamp |
| | **B** (BIAS) | The other wire of **both** clamps |
| | **CT2** | One wire of the floor pump's clamp |

The pins are labelled on the carrier (G D V on top, CT1 BIAS CT2 and 24VAC underneath) and on the enclosure.

- **Probe cables:** three wires, not "parasite power". To extend a probe, Cat5 works for tens of metres: one pair for
  3.3 V and GND, one wire of another pair for DATA. For a run over about 30 m, swap that port's 4.7 kΩ for about
  2.2 kΩ.
- **Call for heat:** connect HEAT across a point that has 24 VAC **only while the thermostat calls**. Find that point
  with a meter, thermostat calling and not: across the thermostat's own terminals it is the other way round (24 VAC
  while *not* calling), and the SR501's end switch to the boiler carries the boiler's own voltage, not the SR501's.
  The carrier's optocoupler keeps the 24 VAC isolated from the board. If in doubt, leave it unconnected: the pump
  clamps show the same thing (the pumps run on every call) without touching the controls.
- **Pump current:** SCT-013 clamps with a 1 V output (built-in burden). Cut off the 3.5 mm plug. The carrier holds
  the bias at half of 3.3 V, so the clamp's AC swings around it on GPIO16 (CT1) and GPIO17 (CT2). Calibrate each
  against a clamp meter: the `calibrate_linear` lines under `sensor:` in `boiler-probes.yaml`.
- **OLED and button:** a 4-wire female-female lead from the carrier's OLED header (G V SCL SDA, the module's pin
  order) to the module in the lid, and the lid button on the BTN header (GPIO47 and GND).

## The display

The OLED shows the readings a few at a time, changing every 4 seconds: floor supply and return with the call for
heat; boiler supply and return with hot water; the slab and P8; the pump currents and the board's IP address.
Temperatures are in °F (`oled_unit: C` at the top of the file for Celsius). It turns off after 5 minutes
(`oled_on_minutes`), because an OLED left on burns in; the lid button, or a restart, turns it on again.

## Flashing

1. Copy `secrets.example.yaml` to `secrets.yaml` and replace both values (`openssl rand -base64 32` makes a key).
   The key is what Home Assistant asks for when it adds the board.
2. Flash once over USB, with the Ethernet cable unplugged (the board isn't isolated, so don't feed it PoE and a
   computer's USB at the same time): hold BOOT, press RESET, then `esphome run boiler-probes.yaml`. In the enclosure,
   both buttons are behind the holes marked BOOT and RESET, and the USB-C jack is 26 mm in, so it needs a plug with
   a slim, long body.
3. After that it updates over the network: `esphome run boiler-probes.yaml --device boiler-probes.local`.
4. Home Assistant finds the board by itself (Settings → Devices & services → *Discovered*: ESPHome) and asks for
   the key from `secrets.yaml`.
5. Plug each probe into its port. The board's own page, `http://boiler-probes.local`, shows every reading.

The configuration was checked with ESPHome 2026.11.0 (`esphome config`, and a full `esphome compile`). It hasn't
run on the hardware yet.

Before installing, putting all the probes in one cup of water for a few minutes shows how far apart they read;
the box learns the same offsets on site, but this catches a bad probe early.
