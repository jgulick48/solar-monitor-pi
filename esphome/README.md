# Boiler probes (ESPHome)

`boiler-probes.yaml` turns an **Olimex ESP32-POE-ISO** into the temperature board for the boiler module: six
DS18B20 probes on one wire, powered and connected by a single Ethernet cable (PoE). It joins Home Assistant
through Home Assistant's ESPHome integration; the solar monitor box reads the readings from Home Assistant.
The wiki's *Boiler, propane and notifications* page has the design.

## Parts

| Part | Notes |
| --- | --- |
| Olimex ESP32-POE-ISO | The isolated (ISO) version: safe on PoE and USB at the same time, and isolated from the plumbing's and boiler's ground |
| PoE switch port or injector | 802.3af |
| 6 × DS18B20 waterproof probes | Stainless sleeve with **silicone** cable (rated to about 125 °C); 6 mm sleeves fit most wells |
| 4.7 kΩ resistor | 1-Wire pull-up (about 2.2 kΩ for runs over about 30 m in total) |
| Cat5/Cat6 cable, screw terminal block | For the probe bus |
| Thermal paste, pipe clamps, pipe insulation | For the pipe-mounted probes |
| Optional: isolated 24 VAC input module (optocoupler) | Wired call for heat from the Taco SR501 |
| Optional: 2 × SCT-013 current clamps (1 V output), 2 × 10 kΩ, 10 µF | Boiler and floor pump current |

## Probes

| # | Name in Home Assistant | Where | Read every |
| --- | --- | --- | --- |
| 1 | Floor supply | Manifold supply well, with paste | 15 s |
| 2 | Floor return | Manifold return well, with paste | 15 s |
| 3 | Boiler supply | Metal fitting at the boiler outlet, clamped with paste, insulated over | 15 s |
| 4 | Boiler return | Boiler return fitting, as probe 3 | 15 s |
| 5 | Hot water outlet | The combi's domestic hot-water outlet pipe, as probe 3 | 5 s |
| 6 | Slab | A sensor conduit in the slab if one was poured (mid-floor, between two tubes), else the floor surface in an inside spot out of the sun | 60 s |

The board also reports **Floor difference** (supply minus return), its uptime, IP address and status. The box
applies its own learned offsets to each pair, zeroed while the pumps are off, since each DS18B20 is ±0.5 °C.

## Wiring

The 1-Wire bus runs from the board to each probe **in a line** (board → probe → probe …), not a star, and uses
three wires (not "parasite power"):

| Board pin | Wire | Probe wire |
| --- | --- | --- |
| 3.3 V | Cat5 pair 1 (one wire) | Red (VDD) |
| GND | Cat5 pair 1 (other wire) | Black (GND) |
| GPIO4 | Cat5 pair 2 (one wire), with the 4.7 kΩ between GPIO4 and 3.3 V at the board | Yellow (DATA) |

Optional inputs:

- **Call for heat:** the optocoupler module's 24 VAC side across a point that has 24 VAC **only while the
  thermostat calls**; its output between **GPIO32** and GND. Find that point with a meter, thermostat calling and
  not: across the thermostat's own terminals it is the other way round (24 VAC while *not* calling), and the SR501's
  end switch to the boiler carries the boiler's own voltage, not the SR501's. If in doubt, skip it: the pump clamps
  below show the same thing (the pumps run on every call) without touching the controls. Uncomment the
  `Call for heat` block.
- **Pump current:** each clamp's output biased to half the 3.3 V supply (two 10 kΩ from 3.3 V to GND, 10 µF from
  the middle to GND, clamp between the middle and the pin) on **GPIO34** (boiler pump) and **GPIO35** (floor pump).
  Uncomment the clamp blocks under `sensor:` and calibrate against a clamp meter.

## Flashing and naming the probes

1. Copy `secrets.example.yaml` to `secrets.yaml` and replace both values. The key is what Home Assistant asks for
   when it adds the board.
2. Flash once over USB (the ISO board's USB is isolated from PoE, so both can stay connected):
   `esphome run boiler-probes.yaml`.
3. The log lists every probe it finds (`Found 1-Wire device ... 0x...`), and so does the board's own page at
   `http://boiler-probes.local`. Hold one probe in your hand for 20 seconds; the address that rises is that probe.
   Write each address into the `addr_...` substitutions at the top of the file.
4. Flash again (now over the network): `esphome run boiler-probes.yaml --device boiler-probes.local`.
5. Home Assistant finds the board by itself (Settings → Devices & services → *Discovered*: ESPHome) and asks for
   the key from `secrets.yaml`.

Before installing, putting all six probes in one cup of water for a few minutes shows how far apart they read;
the box learns the same offsets on site, but this catches a bad probe early.
