"""Backups to a USB stick: the setup page for them, the start page card and the Home Assistant fields.
The backup and restore themselves are setup/backup.py (run by solar-backup.timer and from here)."""
import datetime as dt
import html
import os
import subprocess
import sys
import time

import backup as engine  # setup/backup.py
import solar_setup as ss
from modules import Module, Response

SETUP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEGACY = ("state", "backup.json")        # where the settings were before modules (v0.2.26 and earlier)


def migrate(stack, old, version):
    if old is None:                      # first time: take over state/backup.json
        f = os.path.join(stack, *LEGACY)
        legacy = ss.load_json(f, {})
        out = {k: legacy[k] for k in ("uuid", "history") if k in legacy}
        if legacy.get("uuid"):
            out["chosen_at"] = int(os.path.getmtime(f))   # the overdue check counted from the file's last change
        return out
    return old


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "never"


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def ha_fields(stack, site=None, now=None):
    """Backup state for HA: overdue once a chosen stick has had no good backup for 2 days."""
    now = now or time.time()
    cfg = engine.settings(stack)
    st = ss.load_json(engine.status_path(stack), {})
    last = st.get("last_ok")
    since = last or (cfg.get("chosen_at") if cfg["uuid"] else None)
    return {
        "backup_configured": bool(cfg["uuid"]),
        "backup_last_ok": dt.datetime.fromtimestamp(last, dt.timezone.utc).isoformat() if last else None,
        "backup_overdue": bool(cfg["uuid"]) and since is not None and now - since > 2 * 86400,
        "backup_message": st.get("message") or "",
    }


def status_line(stack):
    """One line for the start page."""
    cfg, st = engine.settings(stack), ss.load_json(engine.status_path(stack), {})
    if not cfg["uuid"]:
        return "<b>not set up</b>: plug a USB stick into the Pi and choose it."
    line = f"last backup {_when(st.get('last_ok'))}"
    if st.get("snapshots"):
        line += f", {st['snapshots']} on the stick, {st.get('free_gb', '?')} GB free"
    if st.get("result") == "error":
        line += f". <b>Last attempt failed</b>: {html.escape(st.get('message', ''))}"
    elif st.get("result") in ("running", "restoring"):
        line += f". {html.escape(st.get('message', ''))}..."
    return line


def card(stack):
    return f'<div class="card"><b>Backups</b>: {status_line(stack)} &nbsp;<a href="/backup">change</a></div>'


def page(stack, msg="", code=200):
    cfg, st = engine.settings(stack), ss.load_json(engine.status_path(stack), {})
    sticks = engine.list_sticks()
    known = {k["uuid"] for k in sticks}
    opts = []
    for k in sticks:
        desc = f"{k['model'].strip() or 'USB stick'}, {k['size']}, {k['fstype']}" + (f", label {k['label']}" if k["label"] else "")
        opts.append(f"<label><input type='radio' name='uuid' value='{html.escape(k['uuid'])}' style='width:auto'"
                    f"{' checked' if k['uuid'] == cfg['uuid'] else ''}> {html.escape(desc)}</label>")
    if cfg["uuid"] and cfg["uuid"] not in known:
        opts.append(f"<label><input type='radio' name='uuid' value='{html.escape(cfg['uuid'])}' style='width:auto' checked>"
                    " The chosen stick (not plugged in right now)</label>")
    opts.append(f"<label><input type='radio' name='uuid' value='' style='width:auto'{'' if cfg['uuid'] else ' checked'}>"
                " No backups</label>")
    found = "" if sticks else "<p><small>No USB stick found. Plug one in (FAT32, exFAT, ext4 or NTFS; 16 GB or more) and reload this page.</small></p>"
    status = html.escape(st.get("message", "no backup yet"))
    restore = ""
    if cfg["uuid"]:
        restore = """<h2>Restore</h2><form method="post" action="/backup/restore">
<p>Replaces this box's settings, passwords, Home Assistant (accounts, dashboards, history) and the inverter history with the
newest backup on the stick. Use it after replacing the SSD: flash the image, start the box, choose the stick above, then restore.
It stops everything for a few minutes.</p>
<label><input type="checkbox" name="confirm" value="1" style="width:auto"> Yes, replace everything on this box with the backup</label>
<p><button>Restore from the stick</button></p></form>"""
    body = f"""<h1>Backups</h1>{('<div class="card">' + html.escape(msg) + '</div>') if msg else ''}
<div class="card"><b>Status</b>: {status_line(stack)}<br><small>Last result: {status}</small>
<form method="post" action="/backup/run" style="margin-top:8px"><button{'' if cfg['uuid'] else ' disabled'}>Back up now</button></form></div>
<form method="post" action="/backup/settings"><fieldset><legend>USB stick</legend>{''.join(opts)}{found}
<label><input type="checkbox" name="history" value="1" style="width:auto"{' checked' if cfg['history'] else ''}> Include the inverter history (VictoriaMetrics)</label>
<small>Every night at about 01:30; 7 daily, 4 weekly and 6 monthly backups are kept. The stick is only in use during a backup,
so it can be pulled out at any other time. It is never formatted: the backups go in a <code>solar-monitor-backup</code> folder.
<b>The key to the backups is on the stick too</b> (so a new box can restore from the stick alone): whoever has the stick can read
this box's passwords and camera links. Keep it in the Pi.</small></fieldset>
<p><button>Save</button> &nbsp;<a href="/">Back</a></p></form>{restore}"""
    return Response(code, "Backups", body)


def save_settings(stack, form):
    uuid = form.get("uuid", "").strip()
    if uuid and uuid not in {k["uuid"] for k in engine.list_sticks()} | {engine.settings(stack)["uuid"]}:
        return page(stack, "That stick isn't plugged in.", 400)
    engine.save_settings(stack, {**engine.settings(stack), "uuid": uuid, "history": form.get("history") == "1"})
    return page(stack, "Saved." + (" The first backup runs tonight; or use Back up now." if uuid else ""))


def run_now(stack, form):
    _run(["systemctl", "start", "--no-block", "solar-backup.service"])
    return Response(location="/backup")


def restore(stack, form):
    if form.get("confirm") != "1":
        return page(stack, "Tick the box to confirm the restore.", 400)
    # Its own unit: the restore restarts the stack, and must outlive this request.
    _run(["systemd-run", "--unit=solar-restore", "--collect", "--no-block", sys.executable,
          os.path.join(SETUP_DIR, "backup.py"), "restore", "--stack", stack])
    return page(stack, "Restoring: this takes a few minutes. Reload this page to see when it's done.")


MODULE = Module(
    id="backup",
    title="Backups",
    order=10,
    default_enabled=True,
    defaults={"uuid": "", "history": True, "chosen_at": None},
    migrate=migrate,
    ha_fields=ha_fields,
    nav=("/backup", "Backups", "Backups"),
    card=card,
    routes={("GET", "/backup"): lambda stack, form: page(stack), ("POST", "/backup/settings"): save_settings,
            ("POST", "/backup/run"): run_now, ("POST", "/backup/restore"): restore},
)
