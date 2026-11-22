#!/usr/bin/env python3
"""solar-backup: nightly backups to a USB stick, and restoring from one.

The stick is any USB stick with a FAT32, exFAT, ext4 or NTFS file system, chosen on the setup page
(state/modules/backup.json {"uuid": ..., "history": true}). The box never formats it. It is only mounted
while a backup or restore runs, so it can be pulled out safely at any other time.

On the stick, in solar-monitor-backup/:
  repo/   a restic repository (deduplicated; 7 daily, 4 weekly and 6 monthly backups are kept)
  key     the repository password. It is on the stick on purpose: a box whose SSD died can be
          restored from the stick alone. The stick therefore holds the box's passwords and camera
          links in readable form for anyone who has it; keep it in the Pi.
  README.txt

What is backed up: state/ (site settings, passwords, inverters, display and camera settings),
Home Assistant's config (its database as a consistent copy), go2rtc's config and, if "history" is
on, a VictoriaMetrics snapshot (all the inverter history).

restic runs in the `backup` compose service (profile "backup"), which mounts the stack directory,
the VictoriaMetrics volume and the stick.

  backup.py run [--stack DIR]        back up now (solar-backup.timer runs this nightly)
  backup.py restore [--stack DIR]    restore the newest backup on the stick (stops and restarts the stack)
  backup.py sticks                   list USB sticks that can be used
  backup.py status                   what is on the configured stick
"""
import argparse
import datetime as dt
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solar_setup as ss  # noqa: E402

VM = os.environ.get("SOLAR_VM", "http://127.0.0.1:8428")
MOUNT = os.environ.get("SOLAR_BACKUP_MOUNT", "/run/solar-backup")
DIR = "solar-monitor-backup"
FILESYSTEMS = ("vfat", "exfat", "ext4", "ext3", "ntfs", "ntfs3")
KEEP = ["--keep-daily", "7", "--keep-weekly", "4", "--keep-monthly", "6"]
STAGING = "backup-staging"              # under state/: consistent copies of live databases
HA_DB = "home-assistant_v2.db"
VM_PARTS = ("small", "big", "indexdb")    # VictoriaMetrics' storage directories under data/
README = """This USB stick holds backups of a solar monitor (github.com/jgulick48/solar-monitor-pi).

To move them to a new box: flash the solar monitor image onto the new SSD, start it, plug this stick
in, open the box's setup page and choose "Restore" under Backups.

The backups include the box's passwords and camera links, and the key file next to them opens them,
so treat this stick like the box itself.
"""


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def settings(stack):
    """{"uuid", "history", "chosen_at"}: kept by the backup module (state/modules/backup.json)."""
    import modules
    return {"chosen_at": None, **modules.settings(stack, "backup")}


def save_settings(stack, d):
    import modules
    cur = settings(stack)
    if d.get("uuid") != cur["uuid"]:
        d = {**d, "chosen_at": int(time.time()) if d.get("uuid") else None}   # the overdue check counts from here
    modules.save(stack, "backup", d)


def status_path(stack):
    return os.path.join(stack, "state", "backup-status.json")


def record(stack, **kw):
    st = ss.load_json(status_path(stack), {})
    st.update(kw)
    ss.write_if_changed(status_path(stack), json.dumps(st, indent=2) + "\n", 0o644)
    return st


# --------------------------------------------------------------------------
# Sticks
# --------------------------------------------------------------------------
def root_disk(blk):
    """Name of the disk that holds / (the box's own SSD is on USB too, so it must never be offered)."""
    def walk(devs, top):
        for d in devs:
            t = top or d["name"]
            if "/" in (d.get("mountpoints") or []):
                return t
            r = walk(d.get("children") or [], t)
            if r:
                return r
        return None
    return walk(blk.get("blockdevices") or [], None)


def sticks_from(blk):
    """USB file systems that can hold backups, from `lsblk -J -O`-style output."""
    root = root_disk(blk)
    out = []
    for disk in blk.get("blockdevices") or []:
        if disk.get("type") != "disk" or disk["name"] == root or disk.get("tran") != "usb":
            continue
        for part in [disk] + (disk.get("children") or []):
            if part.get("fstype") in FILESYSTEMS and part.get("uuid"):
                mps = [m for m in (part.get("mountpoints") or []) if m]
                out.append({"uuid": part["uuid"], "path": part.get("path") or "/dev/" + part["name"],
                            "label": part.get("label") or "", "fstype": part["fstype"],
                            "size": part.get("size") or disk.get("size") or "",
                            "model": (disk.get("vendor") or "").strip() + " " + (disk.get("model") or "").strip(),
                            "mounted": mps})
    return out


def list_sticks():
    try:
        r = subprocess.run(["lsblk", "-J", "-o", "NAME,PATH,TYPE,TRAN,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,VENDOR,MODEL"],
                           capture_output=True, text=True, timeout=20)
        return sticks_from(json.loads(r.stdout or "{}"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


class Stick:
    """Mounts the stick with this UUID at MOUNT for the duration of a `with` block."""

    def __init__(self, uuid):
        self.uuid = uuid
        self.mounted_here = False

    def __enter__(self):
        os.makedirs(MOUNT, exist_ok=True)
        if not os.path.ismount(MOUNT):
            dev = f"/dev/disk/by-uuid/{self.uuid}"
            if not os.path.exists(dev):
                raise RuntimeError("the backup stick isn't plugged in")
            r = subprocess.run(["mount", "-o", "noatime", dev, MOUNT], capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError(f"could not mount the backup stick: {r.stderr.strip()}")
            self.mounted_here = True
        return os.path.join(MOUNT, DIR)

    def __exit__(self, *exc):
        if self.mounted_here:
            subprocess.run(["sync"])
            subprocess.run(["umount", MOUNT], capture_output=True)


def disk_free(path):
    try:
        u = shutil.disk_usage(path)
        return {"free_gb": round(u.free / 1e9, 1), "total_gb": round(u.total / 1e9, 1)}
    except OSError:
        return {}


# --------------------------------------------------------------------------
# restic in the backup container
# --------------------------------------------------------------------------
def restic(stack, *args, capture=True, entrypoint=None):
    cmd = ss.compose_cmd(stack) + ["--profile", "backup", "run", "--rm", "-T"]
    if entrypoint:
        cmd += ["--entrypoint", entrypoint]
    cmd += ["backup", *args]
    log("$", " ".join(args[:3]), "...")
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if r.returncode:
        raise RuntimeError(f"restic {args[0]} failed: {(r.stderr or r.stdout or '').strip()[-400:]}")
    return r.stdout


def ensure_repo(stack, root):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "README.txt"), "w") as f:
        f.write(README)
    key = os.path.join(root, "key")
    if not os.path.exists(os.path.join(root, "repo", "config")):
        if not os.path.exists(key):
            with open(key, "w") as f:
                f.write(secrets.token_urlsafe(32) + "\n")
        restic(stack, "init")
        log("backup repository created on the stick")
    elif not os.path.exists(key):
        raise RuntimeError("the stick has backups but no key file; it can't be used")


# --------------------------------------------------------------------------
# Consistent copies of live data
# --------------------------------------------------------------------------
def stage_ha_db(stack):
    """SQLite online backup of Home Assistant's database (safe while HA writes to it)."""
    src = os.path.join(stack, "homeassistant", HA_DB)
    dst_dir = os.path.join(stack, "state", STAGING)
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, HA_DB)
    if not os.path.exists(src):
        return None
    tmp = dst + ".tmp"
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
    d = sqlite3.connect(tmp)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    os.replace(tmp, dst)
    return dst


def vm_call(path, timeout=300):
    with urllib.request.urlopen(f"{VM}{path}", timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def vm_snapshot():
    r = vm_call("/snapshot/create")
    if r.get("status") != "ok":
        raise RuntimeError(f"VictoriaMetrics snapshot failed: {r}")
    return r["snapshot"]


def vm_snapshot_delete(name):
    try:
        vm_call(f"/snapshot/delete?snapshot={name}")
    except OSError as e:
        log("could not delete the VictoriaMetrics snapshot", name, e)


# --------------------------------------------------------------------------
def run(stack):
    cfg = settings(stack)
    if not cfg["uuid"]:
        record(stack, result="off", message="no backup stick chosen")
        return 0
    started = time.time()
    record(stack, result="running", message="backing up", started=int(started))
    snap = None
    try:
        with Stick(cfg["uuid"]) as root:
            ensure_repo(stack, root)
            stage_ha_db(stack)
            paths = ["/data/stack/state", "/data/stack/homeassistant", "/data/stack/go2rtc"]
            if cfg["history"]:
                snap = vm_snapshot()
                # The snapshot directory itself only holds symlinks (which restic stores as links); the
                # files are in each part directory's snapshots/<name>.
                paths += [f"/data/vm/data/{p}/snapshots/{snap}" for p in VM_PARTS] + [f"/data/vm/snapshots/{snap}/metadata"]
            restic(stack, "backup", "--host", "solar-monitor", "--tag", "nightly",
                   "--exclude", f"/data/stack/homeassistant/{HA_DB}*", "--exclude", "/data/stack/homeassistant/tts",
                   "--exclude", "/data/stack/homeassistant/backups", "--exclude", "/data/stack/state/restore",
                   *paths)
            restic(stack, "forget", "--host", "solar-monitor", "--prune", *KEEP)
            snaps = json.loads(restic(stack, "snapshots", "--json") or "[]")
            free = disk_free(MOUNT)
        st = record(stack, result="ok", message=f"backed up in {time.time() - started:.0f} s",
                    last_ok=int(time.time()), snapshots=len(snaps), history=cfg["history"], **free)
        log("backup ok:", st["message"], f"{len(snaps)} backups on the stick")
        return 0
    except (RuntimeError, OSError, sqlite3.Error, ValueError) as e:
        record(stack, result="error", message=str(e), last_error=int(time.time()))
        log("backup failed:", e)
        return 1
    finally:
        if snap:
            vm_snapshot_delete(snap)
        shutil.rmtree(os.path.join(stack, "state", STAGING), ignore_errors=True)


def stick_status(stack, uuid):
    """Backups on a stick: [{time, id, paths}], newest last."""
    with Stick(uuid) as root:
        if not os.path.exists(os.path.join(root, "repo", "config")):
            return []
        snaps = json.loads(restic(stack, "snapshots", "--json") or "[]")
        return [{"time": s["time"][:19].replace("T", " "), "id": s["short_id"],
                 "history": any("/data/vm/" in p for p in s.get("paths", []))} for s in snaps]


def restore(stack):
    """Replace this box's settings, Home Assistant config and history with the newest backup."""
    cfg = settings(stack)
    if not cfg["uuid"]:
        raise SystemExit("no backup stick chosen")
    record(stack, result="restoring", message="restoring from the stick", started=int(time.time()))
    target = os.path.join(stack, "state", "restore")
    try:
        with Stick(cfg["uuid"]) as root:
            if not os.path.exists(os.path.join(root, "key")):
                raise RuntimeError("no backups on this stick")
            shutil.rmtree(target, ignore_errors=True)
            subprocess.run(ss.compose_cmd(stack) + ["stop"], capture_output=True)
            restic(stack, "restore", "latest", "--host", "solar-monitor", "--target", "/data/stack/state/restore")
        src = os.path.join(target, "data", "stack")
        keep = {k: cfg[k] for k in ("uuid", "history")}   # the stick in use now, not the one in the backup
        # settings and Home Assistant: replace
        for sub in ("homeassistant", "go2rtc"):
            if os.path.isdir(os.path.join(src, sub)):
                shutil.rmtree(os.path.join(stack, sub), ignore_errors=True)
                shutil.copytree(os.path.join(src, sub), os.path.join(stack, sub), symlinks=True)
        st_src = os.path.join(src, "state")
        for name in os.listdir(st_src):
            if name in ("restore", "backup-status.json"):
                continue
            s, d = os.path.join(st_src, name), os.path.join(stack, "state", name)
            if os.path.isdir(s):
                if name == STAGING:
                    continue
                shutil.rmtree(d, ignore_errors=True)
                shutil.copytree(s, d, symlinks=True)
            else:
                shutil.copy2(s, d)
        ha_db = os.path.join(st_src, STAGING, HA_DB)
        if os.path.exists(ha_db):
            for ext in ("", "-wal", "-shm"):
                p = os.path.join(stack, "homeassistant", HA_DB + ext)
                if os.path.exists(p):
                    os.remove(p)
            shutil.copy2(ha_db, os.path.join(stack, "homeassistant", HA_DB))
        save_settings(stack, {**settings(stack), **keep})   # keep using the stick it was restored from
        # history: replace VictoriaMetrics' data with the snapshot's part directories
        vm_src = os.path.join(target, "data", "vm", "snapshots")
        if os.path.isdir(vm_src) and os.listdir(vm_src):
            snap = sorted(os.listdir(vm_src))[-1]
            r = "/data/stack/state/restore/data/vm"
            restic(stack, "-c", "set -e; rm -rf /data/vm/data /data/vm/indexdb /data/vm/snapshots /data/vm/metadata "
                   "/data/vm/cache /data/vm/tmp; mkdir -p /data/vm/data; "
                   + " ".join(f"cp -a {r}/data/{p}/snapshots/{snap} /data/vm/data/{p};" for p in VM_PARTS)
                   + f" cp -a {r}/snapshots/{snap}/metadata /data/vm/metadata", entrypoint="sh")
        shutil.rmtree(target, ignore_errors=True)
        # rebuild the generated config from the restored settings, then start everything
        subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "solar_setup.py"),
                        "--stack", stack, "--no-scan"], capture_output=True)
        r = subprocess.run(ss.compose_cmd(stack) + ["up", "-d"], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"restored, but the stack didn't start: {r.stderr.strip()[-300:]}")
        record(stack, result="restored", message="restored from the newest backup on the stick",
               last_restore=int(time.time()))
        log("restore done")
        return 0
    except (RuntimeError, OSError) as e:
        record(stack, result="error", message=f"restore failed: {e}", last_error=int(time.time()))
        log("restore failed:", e)
        subprocess.run(ss.compose_cmd(stack) + ["up", "-d"], capture_output=True)
        return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["run", "restore", "sticks", "status"])
    ap.add_argument("--stack", default="/opt/solar-monitor/stack")
    a = ap.parse_args()
    if a.action == "run":
        return run(a.stack)
    if a.action == "restore":
        return restore(a.stack)
    if a.action == "sticks":
        print(json.dumps(list_sticks(), indent=2))
        return 0
    print(json.dumps(stick_status(a.stack, settings(a.stack)["uuid"]), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
