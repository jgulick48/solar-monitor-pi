#!/usr/bin/env python3
"""The check-up page (/check): one line per thing that has to work, green or red, with what to do about it. Made for
the first day of a new box ("is everything set up?") and for any later "is something wrong?".

Each check is {"title", "state", "text", "link"}:
  state  "ok" (works), "warn" (works, but look at it), "bad" (doesn't work), "off" (optional and not set up)
  text   one sentence: what was found, and for warn / bad what to do
  link   (href, label) to where it is fixed, or None

core(stack, site) gives the box's own checks; every enabled module adds its own through Module.checks (modules.checks).
A check that fails to run shows as "couldn't be checked" and never breaks the page.
"""
import html
import os
import subprocess
import time
import urllib.error
import urllib.request

import solar_setup as ss

ORDER = {"bad": 0, "warn": 1, "ok": 2, "off": 3}
MARK = {"ok": ("&#10003;", "Works"), "warn": ("!", "Look at it"), "bad": ("&#10007;", "Not working"), "off": ("&ndash;", "Not set up")}


def check(title, state, text, link=None):
    return {"title": title, "state": state, "text": text, "link": link}


def site_check(site):
    missing = []
    if not site.get("latitude") or not site.get("longitude"):
        missing.append("the location")
    try:
        if int(site.get("battery_count") or 0) * float(site.get("battery_kwh_each") or 0) <= 0:
            missing.append("the batteries")
    except (TypeError, ValueError):
        missing.append("the batteries")
    if not any(str(a.get("panels") or "").strip() and str(a.get("panel_watts") or "").strip() for a in site.get("arrays") or []):
        missing.append("the solar panels")
    if missing:
        return check("Site details", "bad", f"Not entered yet: {', '.join(missing)}. The forecast, the advice and the "
                     "alerts need them.", ("/setup", "Setup"))
    return check("Site details", "ok", "Location, batteries and panels are entered.")


def dongle_check(inverters, live):
    """inverters: state/inverters.json; live: boxhealth.dongles() ({datalog: {connected, age, drops}} or None)."""
    import boxhealth
    if not inverters:
        return check("Inverter dongles", "bad", "No dongle found yet. Check each inverter's WiFi dongle is on this "
                     "network, then press Scan now on the start page.", ("/", "Start page"))
    if live is None:
        return check("Inverter dongles", "warn", "The data store didn't answer, so the dongles couldn't be checked. Try again in a minute.")
    bad = []
    for dl in sorted(inverters):
        txt, wrong = boxhealth.dongle_text(live.get(dl))
        if wrong or dl not in live:
            bad.append(f"{dl}: {txt or 'no connection yet'}")
    n = len(inverters)
    if bad:
        return check("Inverter dongles", "bad", f"{'; '.join(bad)}. Check the dongle's WiFi; if it is connected but "
                     "silent, unplug it and plug it back in.", ("/", "Start page"))
    return check("Inverter dongles", "ok", f"{n} dongle{'s' if n != 1 else ''} connected and sending data.")


def data_check(age):
    """age: seconds since the newest inverter reading in the data store, or None."""
    if age is None:
        return check("Data being recorded", "bad", "No inverter data has been recorded yet. It starts within a few "
                     "minutes of a dongle connecting.")
    if age > 300:
        return check("Data being recorded", "bad", f"The newest inverter reading is {age / 60:.0f} minutes old: nothing is being recorded now.")
    return check("Data being recorded", "ok", f"The newest inverter reading is {age:.0f} seconds old.")


def ha_check(stack, fetch=None):
    import tunnel
    try:
        code = (fetch or _http_code)("http://127.0.0.1:8123/manifest.json")
    except OSError as e:
        code = str(getattr(e, "reason", e))
    if code != 200:
        return check("Home Assistant", "bad", f"Home Assistant isn't answering ({code}). It takes a few minutes after "
                     "a start; if it stays this way, restart the box.")
    if not tunnel.ha_has_owner(stack):
        return check("Home Assistant", "warn", "Running, but nobody has created its account yet. Open Home Assistant "
                     "and create yours: until then anyone on this network could.", (":8123", "Home Assistant"))
    return check("Home Assistant", "ok", "Running, and its owner account exists.")


def _http_code(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def forecast_check(stack, site, now=None):
    now = now or time.time()
    if not site.get("latitude"):
        return check("Solar forecast", "bad", "Needs the site's location.", ("/setup", "Setup"))
    fc = ss.load_json(os.path.join(stack, "state", "forecast.json"), {})
    if not fc.get("hours"):
        return check("Solar forecast", "bad", "No forecast yet. It is fetched from Open-Meteo every hour: check the box's internet connection.")
    age_h = (now - fc.get("generated", 0)) / 3600
    if age_h > 6:
        return check("Solar forecast", "bad", f"The forecast is {age_h:.0f} hours old: check the box's internet connection.")
    days = (fc["hours"][-1][0] - now) / 86400
    if fc.get("calibration_source") != "calibrated":
        return check("Solar forecast", "warn", f"Fetched, {days:.0f} days ahead, but not yet tuned to this site: it uses a "
                     "standard loss figure until the advisor has compared a few weeks of real output.", ("/advisor", "Advisor"))
    return check("Solar forecast", "ok", f"Fetched {age_h * 60:.0f} minutes ago, {days:.0f} days ahead, tuned to this site's real output.")


def box_check(status):
    """status: boxhealth.status()."""
    bits = []
    if status.get("cpu_temp") is not None:
        bits.append(f"{status['cpu_temp']:.0f} °C")
    if status.get("disk"):
        bits.append(f"{status['disk']['free_gb']} GB free")
    summary = ", ".join(bits)
    texts = " ".join(t for _, t in status["findings"])
    if status["level"] == "warn":
        return check("The box itself", "bad", texts)
    if status["level"] == "note":
        return check("The box itself", "warn", texts)
    return check("The box itself", "ok", f"Temperature, power, disk and services are fine{' (' + summary + ')' if summary else ''}.")


def clock_check(site, run=None):
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=10))
    try:
        r = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
        synced = r.returncode == 0 and r.stdout.strip() == "yes"
    except (OSError, subprocess.SubprocessError):
        synced = None
    tz = site.get("time_zone") or ""
    if synced is False:
        return check("Clock", "warn", "The clock isn't set from the internet yet. It usually is within minutes of "
                     "starting; if this stays, check the internet connection (times in the history would be wrong).")
    if tz and tz != ss.host_timezone():
        return check("Clock", "warn", f"The site's time zone is {tz} but the box runs in {ss.host_timezone()}: save the setup page again.",
                     ("/setup", "Setup"))
    return check("Clock", "ok", f"Set from the internet, time zone {tz or ss.host_timezone()}.")


def core(stack, site):
    """The box's own checks, each guarded: one that can't run says so."""
    import boxhealth
    import display

    def guard(title, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - never let one check break the page
            return check(title, "warn", f"Couldn't be checked ({type(e).__name__}).")
    inv = ss.load_json(os.path.join(stack, "state", "inverters.json"), {})
    return [
        guard("Site details", lambda: site_check(site)),
        guard("Inverter dongles", lambda: dongle_check(inv, boxhealth.dongles())),
        guard("Data being recorded", lambda: data_check(display.vm_instant(display.INSTANT["age"]))),
        guard("Home Assistant", lambda: ha_check(stack)),
        guard("Solar forecast", lambda: forecast_check(stack, site)),
        guard("The box itself", lambda: box_check(boxhealth.status(stack))),
        guard("Clock", lambda: clock_check(site)),
    ]


def summary(checks):
    """("bad" | "warn" | "ok", "2 things aren't working, 1 to look at")."""
    bad = sum(c["state"] == "bad" for c in checks)
    warn = sum(c["state"] == "warn" for c in checks)
    if not bad and not warn:
        return "ok", "Everything that is set up works."
    parts = ([f"{bad} thing{'s' if bad != 1 else ''} not working"] if bad else []) + ([f"{warn} to look at"] if warn else [])
    return ("bad" if bad else "warn"), ", ".join(parts) + "."


def render(checks, host=""):
    """The page's body. Problems first, then what works, then what isn't set up."""
    e = html.escape
    level, text = summary(checks)
    rows = []
    for c in sorted(checks, key=lambda c: ORDER.get(c["state"], 1)):      # stable: the given order within a state
        mark, word = MARK.get(c["state"], MARK["warn"])
        link = ""
        if c.get("link"):
            href, label = c["link"]
            href = f"http://{host}{href}" if href.startswith(":") and host else href
            link = f" <a href='{e(href)}'>{e(label)}</a>"
        rows.append(f"<tr class='ck-{e(c['state'])}'><td class='ck-mark' title='{word}'><span aria-hidden='true'>{mark}</span>"
                    f"<span class='sr'>{word}</span></td><td><b>{e(c['title'])}</b></td><td>{e(c['text'])}{link}</td></tr>")
    return (f"<h1>Check-up</h1><div class='card ck-sum ck-{level}'><b>{e(text)}</b> "
            f"<small>Checked {time.strftime('%H:%M:%S')}. <a href='/check'>Check again</a></small></div>"
            f"<table class='checks'>{''.join(rows)}</table>"
            "<p><small>Green works. Yellow works but deserves a look. Red doesn't work. Grey is optional and not set up.</small></p>")
