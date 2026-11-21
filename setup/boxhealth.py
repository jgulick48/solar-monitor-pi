#!/usr/bin/env python3
"""Health of the box itself: temperature, power supply, disk, SSD wear, containers, dongle drop-outs.

  CPU temperature     /sys/class/thermal: 70 °C or more worth a look, 80 °C or more needs attention
                      (the Pi slows itself down from 80-85 °C).
  Power supply        `vcgencmd get_throttled`: under-voltage or throttling now needs attention; earlier since
                      the last boot is worth a look (a weak power supply or cable corrupts SSDs).
  Disk                free space on the SSD: under 15% worth a look, under 5% (or 2 GB) needs attention.
  SSD                 `smartctl` (also through most USB adapters): NVMe "percentage used" 70% / 90%, media
                      errors, critical warnings; SATA overall result and reallocated sectors. Read at most
                      every 6 hours. Some USB adapters don't pass it through: then it says so.
  Containers          any of the stack's containers not running needs attention.
  Dongle drop-outs    how often each dongle's connection dropped in 24 hours (VictoriaMetrics): 6 or more is
                      worth a look (usually weak Wi-Fi where the inverter is).
"""
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solar_setup as ss  # noqa: E402

SMART_EVERY = 6 * 3600
_cache = {}


def run(cmd, timeout=20):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def cpu_temp(path="/sys/class/thermal/thermal_zone0/temp"):
    try:
        return int(open(path).read().strip()) / 1000
    except (OSError, ValueError):
        return None


THROTTLE_BITS = {0: "under-voltage", 1: "CPU speed capped", 2: "throttled", 3: "temperature limit"}


def throttled(out=None):
    """{'now': [...], 'since_boot': [...]} from vcgencmd, or None when not a Pi."""
    if out is None:
        r = run(["vcgencmd", "get_throttled"], timeout=5)
        if r.returncode != 0:
            return None
        out = r.stdout
    try:
        v = int(out.strip().split("=")[1], 16)
    except (IndexError, ValueError):
        return None
    return {"now": [t for b, t in THROTTLE_BITS.items() if v >> b & 1],
            "since_boot": [t for b, t in THROTTLE_BITS.items() if v >> (b + 16) & 1]}


def disk(path):
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    return {"free_gb": round(u.free / 1e9, 1), "total_gb": round(u.total / 1e9, 1), "free_pct": round(u.free / u.total * 100, 1)}


def root_device():
    r = run(["findmnt", "-n", "-o", "SOURCE", "/"], timeout=5)
    dev = r.stdout.strip()
    if not dev.startswith("/dev/"):
        return None
    r = run(["lsblk", "-n", "-d", "-o", "PKNAME", dev], timeout=5)
    parent = r.stdout.strip()
    return f"/dev/{parent}" if parent else dev


def parse_smart(d):
    """smartctl -j output -> {'kind', 'used_pct', 'problems': [...]}, or None if it has no health data."""
    out = {"model": d.get("model_name") or "", "problems": []}
    nv = d.get("nvme_smart_health_information_log")
    if nv:
        out["kind"] = "NVMe"
        out["used_pct"] = nv.get("percentage_used")
        if nv.get("media_errors"):
            out["problems"].append(f"{nv['media_errors']} media errors")
        if nv.get("critical_warning"):
            out["problems"].append(f"critical warning {nv['critical_warning']}")
        if nv.get("available_spare") is not None and nv.get("available_spare_threshold") is not None \
                and nv["available_spare"] <= nv["available_spare_threshold"]:
            out["problems"].append("spare blocks used up")
        return out
    attrs = {a.get("name"): a for a in (d.get("ata_smart_attributes") or {}).get("table", [])}
    if d.get("smart_status") is None and not attrs:
        return None
    out["kind"] = "SATA"
    if (d.get("smart_status") or {}).get("passed") is False:
        out["problems"].append("SMART overall result: FAILED")
    for name in ("Reallocated_Sector_Ct", "Current_Pending_Sector", "Offline_Uncorrectable"):
        raw = (attrs.get(name) or {}).get("raw", {}).get("value")
        if raw:
            out["problems"].append(f"{name.replace('_', ' ').lower()}: {raw}")
    for name in ("Percent_Lifetime_Remain", "Wear_Leveling_Count", "SSD_Life_Left", "Media_Wearout_Indicator"):
        a = attrs.get(name)
        if a and a.get("value") is not None:
            out["used_pct"] = max(0, 100 - int(a["value"]))
            break
    return out


def smart():
    """SSD health via smartctl, trying the usual USB bridge types. {'error': ...} if not available."""
    dev = root_device()
    if not dev or not shutil.which("smartctl"):
        return {"error": "smartctl not installed" if dev else "no disk found"}
    for extra in ([], ["-d", "sat"], ["-d", "sntjmicron"], ["-d", "sntasmedia"], ["-d", "sntrealtek"]):
        r = run(["smartctl", "-j", "-a", *extra, dev], timeout=30)
        try:
            d = json.loads(r.stdout)
        except ValueError:
            continue
        res = parse_smart(d)
        if res:
            res["device"] = dev
            return res
    return {"error": "the SSD's USB adapter doesn't pass its health data through"}


def containers(stack):
    r = run(ss.compose_cmd(stack) + ["ps", "-a", "--format", "{{.Service}} {{.State}}"], timeout=30)
    if r.returncode != 0:
        return None
    return [ln.split()[0] for ln in r.stdout.splitlines() if ln.split() and ln.split()[-1] != "running"]


def dongle_drops():
    """{datalog: drop-outs in 24 h} from the bridge's connection metric."""
    import display
    try:
        r = display._vm("/api/v1/query", {"query": "sum by (deploymentID) (changes(lxp_bridge_connected[24h])) / 2"})
    except (OSError, ValueError, KeyError):
        return None
    out = {}
    for s in r:
        name = s["metric"].get("deploymentID") or "dongle"   # the bridge labels each dongle by its id
        out[name] = round(float(s["value"][1]))
    return out


def status(stack, probe=None):
    """Everything above plus findings [(level, text)] and an overall level ok / note / warn.
    probe: overrides for tests ({'temp':..., 'throttled':..., 'disk':..., 'smart':..., 'containers':..., 'drops':...})."""
    p = probe or {}
    t = p["temp"] if "temp" in p else cpu_temp()
    th = p["throttled"] if "throttled" in p else throttled()
    dk = p["disk"] if "disk" in p else disk(stack)
    sm = p["smart"] if "smart" in p else cached("smart", SMART_EVERY, smart)
    ct = p["containers"] if "containers" in p else cached("containers", 60, lambda: containers(stack))
    dr = p["drops"] if "drops" in p else cached("drops", 300, dongle_drops)
    f = []
    if t is not None:
        if t >= 80:
            f.append(("warn", f"The Pi is at {t:.0f} °C: give it air (a case with a fan, away from the inverters' heat)."))
        elif t >= 70:
            f.append(("note", f"The Pi is warm: {t:.0f} °C."))
    if th:
        if th["now"]:
            f.append(("warn", f"Power supply: {', '.join(th['now'])} now. Use the official 5.1 V 3 A supply and a short cable."))
        elif th["since_boot"]:
            f.append(("note", f"Power supply: {', '.join(th['since_boot'])} since the last start (a weak supply or cable)."))
    if dk:
        if dk["free_pct"] < 5 or dk["free_gb"] < 2:
            f.append(("warn", f"The SSD is nearly full: {dk['free_gb']} GB free."))
        elif dk["free_pct"] < 15:
            f.append(("note", f"The SSD is getting full: {dk['free_gb']} GB ({dk['free_pct']:.0f}%) free."))
    if sm and not sm.get("error"):
        if sm["problems"]:
            f.append(("warn", f"SSD: {'; '.join(sm['problems'])}. Make sure the USB backup is set up and plan to replace it."))
        used = sm.get("used_pct")
        if used is not None and used >= 90:
            f.append(("warn", f"SSD worn: {used}% of its rated life used."))
        elif used is not None and used >= 70:
            f.append(("note", f"SSD wear: {used}% of its rated life used."))
    if ct:
        f.append(("warn", f"Not running: {', '.join(sorted(ct))}."))
    for name, n in sorted((dr or {}).items()):
        if n >= 6:
            f.append(("note", f"Dongle {name} dropped off the network {n} times in 24 hours (often weak Wi-Fi)."))
    level = "warn" if any(x[0] == "warn" for x in f) else "note" if f else "ok"
    return {"level": level, "findings": f, "cpu_temp": t, "throttled": th, "disk": dk, "smart": sm,
            "not_running": ct, "dongle_drops": dr}


def ha_fields(stack):
    try:
        s = status(stack)
    except Exception:  # noqa: BLE001 - never break /api/ha
        return {"box_health": "unknown", "box_health_text": "", "box_cpu_temp": None, "box_disk_free": None,
                "box_ssd_used": None, "box_power_problem": False}
    sm = s["smart"] or {}
    th = s["throttled"] or {}
    return {
        "box_health": s["level"],
        "box_health_text": " ".join(t for _, t in s["findings"]) or "All good.",
        "box_cpu_temp": None if s["cpu_temp"] is None else round(s["cpu_temp"]),
        "box_disk_free": None if not s["disk"] else s["disk"]["free_pct"],
        "box_ssd_used": sm.get("used_pct"),
        "box_power_problem": bool(th.get("now")),
    }


if __name__ == "__main__":
    st = status(sys.argv[1] if len(sys.argv) > 1 else "/opt/solar-monitor/stack")
    print(json.dumps(st, indent=1))
