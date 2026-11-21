#!/usr/bin/env python3
"""solar-collector: sunlight data for the advisor, written to VictoriaMetrics.

  * Every minute, if a local weather station is configured (state/weather.json
    {"source": "station", "station": {...}}), its solar radiation and outdoor
    temperature: site_solar_radiation_wm2, site_outdoor_temp_c.
    (Stations that push instead are handled by solar_web.py.)
  * Every hour, Open-Meteo's irradiance on the plane of each solar array
    (global_tilted_irradiance for the array's tilt and direction, hourly means)
    and the air temperature, for the last two days:
    site_openmeteo_gti_wm2{array="1"}, site_openmeteo_temp_c. On the first run
    it backfills 92 days. Timestamps are the end of each hour (Open-Meteo
    reports the mean of the preceding hour). This sends the site's location to
    Open-Meteo (open-meteo.com, free API, no key).
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solar_setup as ss  # noqa: E402
import weather  # noqa: E402

VM = os.environ.get("SOLAR_VM", "http://127.0.0.1:8428")
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def vm_import(lines):
    if not lines:
        return
    req = urllib.request.Request(f"{VM}/api/v1/import/prometheus", data=("\n".join(lines) + "\n").encode(), method="POST")
    urllib.request.urlopen(req, timeout=20).read()


def station_sample(stack):
    cfg = ss.load_json(os.path.join(stack, "state", "weather.json"), {})
    if cfg.get("source") != "station" or not cfg.get("station"):
        return
    r = weather.read(cfg["station"])
    ts = int(time.time() * 1000)
    lines = []
    if r.get("solar_wm2") is not None:
        lines.append(f'site_solar_radiation_wm2{{source="station"}} {r["solar_wm2"]} {ts}')
    if r.get("temp_c") is not None:
        lines.append(f'site_outdoor_temp_c{{source="station"}} {r["temp_c"]:.2f} {ts}')
    vm_import(lines)


def open_meteo_fetch(stack, past_days):
    site = ss.load_json(os.path.join(stack, "state", "site.json"), {})
    if not site.get("latitude") or not site.get("arrays"):
        return 0
    n = 0
    temp_done = False
    for i, a in enumerate(site["arrays"], start=1):
        q = {
            "latitude": site["latitude"], "longitude": site["longitude"],
            "hourly": "global_tilted_irradiance,temperature_2m",
            "tilt": a["tilt"],
            "azimuth": (int(a["azimuth"]) - 180 + 540) % 360 - 180,  # Open-Meteo: 0 = south, -90 = east, 90 = west
            "past_days": past_days, "forecast_days": 1, "timezone": "UTC", "timeformat": "unixtime",
        }
        with urllib.request.urlopen(f"{OPEN_METEO}?{urllib.parse.urlencode(q)}", timeout=60) as r:
            d = json.loads(r.read())
        h = d["hourly"]
        now = time.time()
        lines = []
        for t, gti, temp in zip(h["time"], h["global_tilted_irradiance"], h["temperature_2m"]):
            if t > now or gti is None:
                continue  # only past hours (value at t = mean of the hour ending at t)
            lines.append(f'site_openmeteo_gti_wm2{{array="{i}"}} {gti} {t * 1000}')
            if not temp_done and temp is not None:
                lines.append(f"site_openmeteo_temp_c {temp} {t * 1000}")
        temp_done = True
        vm_import(lines)
        n += len(lines)
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stack", default="/opt/solar-monitor/stack")
    ap.add_argument("--once", action="store_true", help="one station sample and one Open-Meteo fetch, then exit")
    args = ap.parse_args()
    marker = os.path.join(args.stack, "state", "openmeteo-backfilled")
    last_om = 0.0
    while True:
        try:
            station_sample(args.stack)
        except Exception as e:  # noqa: BLE001 - keep collecting
            log("station read failed:", e)
        if time.time() - last_om >= 3600:
            try:
                backfill = not os.path.exists(marker)
                n = open_meteo_fetch(args.stack, 92 if backfill else 2)
                if n and backfill:
                    open(marker, "w").write(time.strftime("%Y-%m-%d %H:%M\n"))
                if n:
                    log(f"Open-Meteo: {n} samples")
                last_om = time.time()
            except Exception as e:  # noqa: BLE001
                log("Open-Meteo fetch failed:", e)
                last_om = time.time() - 3000  # retry in ~10 minutes
        if args.once:
            return
        time.sleep(60)


if __name__ == "__main__":
    main()
