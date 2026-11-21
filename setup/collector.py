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
  * From the same requests, the solar forecast: expected output per hour for
    today and the next two days in state/forecast.json, scaled by the
    advisor's calibration (actual output / irradiance model) when it has one.
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
GAMMA = -0.0035          # PV power change per degree C of cell temperature (as in advisor.py)
DEFAULT_DERATE = 0.8     # actual / model before the advisor has calibrated this site
INVERTER_PV_KW = {"6000xp": 8.0, "12000xp": 15.0, "12kpv": 15.0, "18kpv": 18.0}
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


def calibration(stack):
    """(factor, source): the advisor's actual/model ratio if it was calibrated on Open-Meteo, else a default."""
    adv = ss.load_json(os.path.join(stack, "state", "advisor.json"), {})
    k = (adv.get("data") or {}).get("calibration")
    if k and adv.get("source") == "Open-Meteo" and 0.3 <= k <= 1.3:
        return k, "calibrated"
    return DEFAULT_DERATE, "default"


def forecast_hours(site, gti_by_array, temps, k, n_inverters):
    """[[hour_end_unix, kW]]: expected average output in each hour, capped at the inverters' PV input."""
    kwp = [int(a["panels"]) * int(a["panel_watts"]) / 1000 for a in site["arrays"]]
    cap = n_inverters * INVERTER_PV_KW.get(site.get("inverter_model", "6000xp"), 8.0)
    out = []
    for t in sorted(gti_by_array[0]):
        vals = [g.get(t) for g in gti_by_array]
        if any(v is None for v in vals):
            continue
        temp = temps.get(t, 15.0)
        kw = sum(p * x / 1000 * (1 + GAMMA * (temp + x / 800 * 20 - 25)) for p, x in zip(kwp, vals)) * k
        out.append([t, round(min(max(kw, 0.0), cap), 3)])
    return out


def open_meteo_fetch(stack, past_days):
    site = ss.load_json(os.path.join(stack, "state", "site.json"), {})
    if not site.get("latitude") or not site.get("arrays"):
        return 0
    n = 0
    temp_done = False
    gti_by_array, temps = [], {}
    for i, a in enumerate(site["arrays"], start=1):
        q = {
            "latitude": site["latitude"], "longitude": site["longitude"],
            "hourly": "global_tilted_irradiance,temperature_2m",
            "tilt": a["tilt"],
            "azimuth": (int(a["azimuth"]) - 180 + 540) % 360 - 180,  # Open-Meteo: 0 = south, -90 = east, 90 = west
            "past_days": past_days, "forecast_days": 3, "timezone": "UTC", "timeformat": "unixtime",
        }
        with urllib.request.urlopen(f"{OPEN_METEO}?{urllib.parse.urlencode(q)}", timeout=60) as r:
            d = json.loads(r.read())
        h = d["hourly"]
        now = time.time()
        lines = []
        gti_by_array.append({t: g for t, g in zip(h["time"], h["global_tilted_irradiance"]) if t > now - 86400 * 1.5})
        temps.update({t: x for t, x in zip(h["time"], h["temperature_2m"]) if x is not None})
        for t, gti, temp in zip(h["time"], h["global_tilted_irradiance"], h["temperature_2m"]):
            if t > now or gti is None:
                continue  # only past hours (value at t = mean of the hour ending at t)
            lines.append(f'site_openmeteo_gti_wm2{{array="{i}"}} {gti} {t * 1000}')
            if not temp_done and temp is not None:
                lines.append(f"site_openmeteo_temp_c {temp} {t * 1000}")
        temp_done = True
        vm_import(lines)
        n += len(lines)
    k, source = calibration(stack)
    n_inv = max(len(ss.load_json(os.path.join(stack, "state", "inverters.json"), {})), 1)
    fc = {"generated": int(time.time()), "calibration": round(k, 3), "calibration_source": source,
          "hours": forecast_hours(site, gti_by_array, temps, k, n_inv)}
    ss.write_if_changed(os.path.join(stack, "state", "forecast.json"), json.dumps(fc) + "\n", 0o644)
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
