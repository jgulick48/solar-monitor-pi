# Test simulators

Stand-ins for the hardware a solar monitor box talks to, for testing a box (or the image on a spare Pi) without
inverters or a weather station. Standard library only: they run on a stock `python:3-alpine` image.

## luxsim.py: a LuxPower/EG4 WiFi dongle and its inverter

Listens on TCP 8000 like a real dongle: heartbeats with the datalog serial every 15 s and translated replies to
read-input and read-holding requests with the inverter serial, so the box's discovery finds it and lxp-bridge-go reads
it unchanged (checked with lxp-bridge-go 2026.11.21-0538; 2026.11.22-0513 detects the model from holding 19, which the simulator answers with 44 (18kPV) or `--device-type 38` (6000XP): solar, load, battery, SoC/SOH, cells, temperatures, BMS
current, cycles, faults, generator). Writes are acknowledged and ignored.

The inverter is a small simulation: a clear-sky sun for the latitude with drifting clouds, a house load with an evening
peak and appliances that run for minutes, a battery whose SoC follows the balance (solar cut back when full), and
today's/lifetime counters that reset at local midnight.

Settings (flags or environment): `DATALOG`, `SERIAL` (10 characters each), `PV_KW`, `BATTERY_KWH`, `LOAD_W`,
`MAX_CHARGE_W`, `SOC`, `SOH`, `LAT`, `LON`, `UTC_OFFSET`, `SCENARIO`, `PORT` (8000), `CONTROL_PORT` (8080).

Control page `http://<ip>:8080/`: switch scenario, jump the SoC; `/state` is JSON.

| scenario | what the box should show |
|---|---|
| `normal` | a normal day |
| `generator` | 4.5 kW on the GEN input, dry contact on: generator power, generator log run (GEN input, 200 h service) |
| `chargeverter` | 2.8 kW into the battery the inverter doesn't see: generator log run (DC charger, 100 h service) after 10 min |
| `low-battery` | SoC 18%: low battery alert |
| `fault` | inverter fault code 16 |
| `bms-fault` | BMS fault code 4: battery health "needs attention", wall display alert |
| `imbalance` | 145 mV cell spread above 90%: battery health warning once it has been full |
| `cold` | cells below 0 °C: battery health warning |
| `full` | charges to 100% quickly (cell spread near full gets measured) |
| `offline` | stops answering: dongle offline alert |

Each inverter needs its own IP address (the box looks for one dongle per address on port 8000): run one container
per inverter on a macvlan network.

## ecowittsim.py: an Ecowitt gateway

Answers the Ecowitt discovery broadcast (UDP 46000) and serves `/get_livedata_info` on port 80 with solar radiation
and outdoor temperature following the sun. Settings: `IP` (the address it announces), `MAC`, `NAME`, `LAT`, `LON`.
`http://<ip>/?clouds=0.6` sets the cloud cover.

## protectsim.py: a UniFi Protect console

The Integration API parts the box uses, over HTTPS with a self-signed certificate: the camera list, RTSPS stream links
(POST creates one) and the `subscribe/events` WebSocket, all behind `X-API-Key`. Control page (plain HTTP):
`/?cam=cam2&kind=ring` fires an event (`person, vehicle, animal, package, motion, ring`), ending after `&seconds=20`.
The stream links point at `--rtsp-host`, so the pictures stay black unless something serves RTSPS there.

    openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj /CN=protect -keyout key.pem -out cert.pem
    python3 protectsim.py --cert cert.pem --certkey key.pem --key test-api-key --cameras "Driveway,Front door:doorbell,Back yard"
