# Boiler probes (ESPHome)

`boiler-probes.yaml` turns a **Waveshare ESP32-P4-ETH** (with its PoE module) into the temperature board for the
boiler module: three to seven DS18B20 probes on one or two 1-Wire buses, powered and connected by a single Ethernet cable (PoE). It joins Home Assistant
through Home Assistant's ESPHome integration; the solar monitor box reads the readings from Home Assistant.
The wiki's *Boiler, propane and notifications* page has the design.

## Parts

| Part | Notes |
| --- | --- |
| Waveshare ESP32-P4-ETH with the PoE module | ESP32-P4 with IP101 Ethernet; 2 × 20 header with the free GPIOs |
| PoE switch port or injector | 802.3af |
| 3 to 7 × DS18B20 waterproof probes | Stainless sleeve with **silicone** cable (rated to about 125 °C); 6 mm sleeves fit most wells |
| 1 or 2 × 4.7 kΩ resistors | One 1-Wire pull-up per bus in use (about 2.2 kΩ for a bus over about 30 m in total) |
| Carrier board | `carrier/`: 8 pluggable probe ports with their pull-ups, the heat-call and clamp inputs, OLED header; Gerbers for a through-hole and an SMD version |
| Enclosure (3D printed) | `enclosure/`: box and lid for the carrier and the board, with the ports in its walls and an OLED window (some sizes still to be measured) |
| Thermal paste, pipe clamps, pipe insulation | For the pipe-mounted probes |
| Optional: isolated 24 VAC input module (optocoupler) | Wired call for heat from the Taco SR501 |
| Optional: 2 × SCT-013 current clamps (1 V output), 2 × 10 kΩ, 10 µF | Boiler and floor pump current |

## Probes

Three probes are the minimum; each further probe adds accuracy or a reading. The file ships with probes 1 to 3;
to add one, uncomment its block in `boiler-probes.yaml` (for 6 and 7, the slab bus too), put in its address and
flash again.

| # | Name in Home Assistant | Needed? | What it gives the boiler module | Where | Read every |
| --- | --- | --- | --- | --- | --- |
| 1 | Floor supply | **Required** | With 2: the heat delivered to the floor (the manifold's flow × the temperature difference) | Manifold supply well, with paste | 15 s |
| 2 | Floor return | **Required** | See 1 | Manifold return well, with paste | 15 s |
| 3 | Boiler supply | **Required** | Whether the boiler fires and how hot; the outdoor-reset check; the restart test | Metal fitting at the boiler outlet, clamped with paste, insulated over | 15 s |
| 4 | Boiler return | Optional | Whether the boiler condenses (return under about 130 °F), and the boiler loop's own difference | Boiler return fitting, as probe 3 | 15 s |
| 5 | Hot water outlet | Optional | Tells a hot-water draw from heating straight away; without it, the water pump's plug does | The combi's domestic hot-water outlet pipe, as probe 3 | 5 s |
| 6 | Slab near boiler | Optional (slab bus) | How the floor itself responds and lags | A sensor conduit in the slab if one was poured (mid-floor, between two tubes), else the floor surface in an inside spot out of the sun | 60 s |
| 7 | Slab far | Optional (slab bus) | With 6: how evenly the slab heats | As probe 6, at the far end of the floor's loops | 60 s |

Probes 1 to 5 are on the **boiler bus**, 6 and 7 on the **slab bus**. 1-Wire has no five-probe limit (each probe
has its own 64-bit address); what limits a bus is its total cable length, which is why the long run to the slab
gets a bus of its own.

The names are only what Home Assistant shows. The boiler module's setup has a picker for each role that lists
Home Assistant's temperature sensors, with the three required roles marked; a probe can be renamed, and a sensor
from another device can fill a role. The module shows what each missing optional role would add.

The board also reports **Floor difference** (supply minus return), its uptime, IP address and status. The box
applies its own learned offsets to each pair, zeroed while the pumps are off, since each DS18B20 is ±0.5 °C.

## Wiring

Each 1-Wire bus runs from the board to its probes **in a line** (board → probe → probe …), not a star, and uses
three wires (not "parasite power"). The two data pins are `pin_boiler_bus` (GPIO20) and `pin_slab_bus` (GPIO21) at
the top of the file; any free header GPIO works, so check the board's silkscreen and change them if needed.

| Board pin | Wire | Probe wire |
| --- | --- | --- |
| 3.3 V | Cat5 pair 1 (one wire) | Red (VDD) |
| GND | Cat5 pair 1 (other wire) | Black (GND) |
| GPIO20 (boiler bus) or GPIO21 (slab bus) | Cat5 pair 2 (one wire), with a 4.7 kΩ between that pin and 3.3 V at the board | Yellow (DATA) |

Optional inputs:

- **Call for heat:** the optocoupler module's 24 VAC side across a point that has 24 VAC **only while the
  thermostat calls**; its output between a free header GPIO and GND. Find that point with a meter, thermostat calling and
  not: across the thermostat's own terminals it is the other way round (24 VAC while *not* calling), and the SR501's
  end switch to the boiler carries the boiler's own voltage, not the SR501's. If in doubt, skip it: the pump clamps
  below show the same thing (the pumps run on every call) without touching the controls. Uncomment the
  `Call for heat` block and add a `pin_heat_call` substitution with that GPIO.
- **Pump current:** each clamp's output biased to half the 3.3 V supply (two 10 kΩ from 3.3 V to GND, 10 µF from
  the middle to GND, clamp between the middle and the pin) on two ADC-capable header pins (GPIO16 to GPIO23 on the ESP32-P4; not the bus pins), set as `pin_boiler_pump`
  and `pin_floor_pump` substitutions. Uncomment the clamp blocks under `sensor:` and calibrate against a clamp meter.

## Flashing and naming the probes

1. Copy `secrets.example.yaml` to `secrets.yaml` and replace both values. The key is what Home Assistant asks for
   when it adds the board.
2. Flash once over USB, with the Ethernet cable unplugged (the board isn't isolated, so don't feed it PoE and a
   computer's USB at the same time):
   `esphome run boiler-probes.yaml`.
3. The log lists every probe it finds (`Found 1-Wire device ... 0x...`), and so does the board's own page at
   `http://boiler-probes.local`. Hold one probe in your hand for 20 seconds; the address that rises is that probe.
   Write each address into the `addr_...` substitutions at the top of the file.
4. Flash again (now over the network): `esphome run boiler-probes.yaml --device boiler-probes.local`.
5. Home Assistant finds the board by itself (Settings → Devices & services → *Discovered*: ESPHome) and asks for
   the key from `secrets.yaml`.

Before installing, putting all the probes in one cup of water for a few minutes shows how far apart they read;
the box learns the same offsets on site, but this catches a bad probe early.
