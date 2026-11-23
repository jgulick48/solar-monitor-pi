"""Zigbee: Zigbee2MQTT on the box for a network (PoE/Ethernet) coordinator, such as a SONOFF Dongle Max. Devices
paired in Zigbee2MQTT's web page (port 8081) appear in Home Assistant through MQTT discovery.

The coordinator's address, the adapter type and the MQTT password reach Zigbee2MQTT through .env, so changing them
never touches its configuration.yaml, which it keeps writing to (devices, names). That file is seeded once with the
network's channel, a web page token and GENERATE for the network key and IDs, which Zigbee2MQTT replaces with random
values on its first start. The zigbee2mqtt folder holds the network key and coordinator backup and is in the box's
USB backup."""
import html
import json
import os
import secrets

import radios
import solar_setup as ss
from modules import FieldError, Module, Response

ADAPTERS = (("ember", "Silicon Labs EFR32 (ember): SONOFF Dongle Max, SONOFF Dongle-E, SMLIGHT SLZB-06M/MG24"),
            ("zstack", "Texas Instruments CC2652/CC1352 (zstack): SONOFF Dongle-P, SMLIGHT SLZB-06/06P7"),
            ("deconz", "deCONZ (ConBee, RaspBee)"))
CHANNELS = (11, 15, 20, 25)
DEFAULTS = {"host": "", "port": 6638, "adapter": "ember", "channel": 15}
UI_PORT = 8081


def settings(stack):
    import modules
    return modules.settings(stack, "zigbee")


def _dir(stack):
    return os.path.join(stack, "zigbee2mqtt")


def seed(stack):
    """configuration.yaml the first time (JSON is valid YAML); later starts leave it to Zigbee2MQTT."""
    path = os.path.join(_dir(stack), "configuration.yaml")
    os.makedirs(_dir(stack), exist_ok=True)
    if os.path.exists(path):
        return False
    s = settings(stack)
    cfg = {
        "version": 4,
        "homeassistant": {"enabled": True},
        "mqtt": {"base_topic": "zigbee2mqtt"},
        "frontend": {"enabled": True, "port": 8080, "auth_token": ui_token(stack)},
        "advanced": {"channel": int(s["channel"]), "network_key": "GENERATE", "pan_id": "GENERATE",
                     "ext_pan_id": "GENERATE", "log_level": "info"},
        "availability": {"enabled": True},
    }
    ss.write_if_changed(path, json.dumps(cfg, indent=2) + "\n", 0o600)
    return True


def ui_token(stack):
    return ss.secret(stack, "zigbee_ui_token", lambda: secrets.token_urlsafe(18))


def env(stack):
    s = settings(stack)
    sec = ss.load_json(os.path.join(stack, "state", "secrets.json"), {})
    return [f"ZIGBEE_PORT={radios.tcp_url(s)}", f"ZIGBEE_ADAPTER={s['adapter']}",
            f"ZIGBEE_MQTT_PASSWORD={sec.get('mqtt_homeassistant', '')}"]


def start(stack):
    if settings(stack).get("host"):
        radios.start(stack, "zigbee2mqtt", before=seed)


def stop(stack):
    radios.stop(stack, "zigbee2mqtt")


def form_section(stack):
    s = settings(stack)
    e = html.escape
    adapters = "".join(f"<option value='{k}'{' selected' if s['adapter'] == k else ''}>{e(t)}</option>" for k, t in ADAPTERS)
    formed = os.path.exists(os.path.join(_dir(stack), "coordinator_backup.json"))
    channels = "".join(f"<option value='{c}'{' selected' if int(s['channel']) == c else ''}>{c}</option>" for c in CHANNELS)
    chan = (f"<label>Zigbee channel <select name='zb_channel'>{channels}</select></label>" if not formed else
            f"<label>Zigbee channel <input value='{int(s['channel'])}' disabled></label>")
    status = ""
    if s.get("host"):
        ok = radios.reachable(s["host"], s["port"])
        status = (f"<small>Coordinator at {e(radios.tcp_url(s))}: "
                  f"{'answers' if ok else '<b>does not answer</b> (check its address, port and that it is in Zigbee coordinator mode)'}.</small>")
    return f"""<fieldset id="zigbee"><legend>Zigbee</legend>
<small>Zigbee devices (plugs, sensors) through a network Zigbee coordinator on your Ethernet, such as a SONOFF Dongle Max:
nothing is plugged into the box. Pair devices on Zigbee2MQTT's page; they appear in Home Assistant by themselves.</small>
<div class="row"><label>Coordinator address <input name="zb_host" value="{e(s.get('host', ''))}" placeholder="192.168.1.50"></label>
<label>TCP port <input name="zb_port" value="{e(str(s.get('port', '')))}" placeholder="6638"></label></div>
<small>Both are on the coordinator's own web page (its network or Zigbee settings); put it in <i>Zigbee coordinator</i> mode first.</small>
<label>Chip <select name="zb_adapter">{adapters}</select></label>
{chan}<small>{'The network is formed; its channel stays.' if formed else 'Set once, when the network is first formed: 15, 20 or 25 stay clear of most Wi-Fi.'}</small>
{status}
<p><small>Zigbee2MQTT's page: {radios.port_link(UI_PORT)} on this box, token <code>{e(ui_token(stack))}</code>.</small></p>
</fieldset>"""


def form_check(stack, form):
    if not form.get("zb_host", "").strip():
        return                                  # not set up yet: saved empty, Zigbee2MQTT waits for it
    try:
        radios.check_address(form.get("zb_host"), form.get("zb_port"))
    except ValueError as e:
        raise FieldError("zb_host" if "address" in str(e) else "zb_port", str(e)) from None
    if form.get("zb_adapter") not in dict(ADAPTERS):
        raise FieldError("zb_adapter", "pick the coordinator's chip")
    if form.get("zb_channel") and (not form["zb_channel"].isdigit() or int(form["zb_channel"]) not in CHANNELS):
        raise FieldError("zb_channel", "pick a channel")


def form_save(stack, form):
    import modules
    cur = settings(stack)
    host, port = (radios.check_address(form.get("zb_host"), form.get("zb_port")) if form.get("zb_host", "").strip()
                  else ("", cur.get("port") or DEFAULTS["port"]))
    new = {"host": host, "port": port, "adapter": form["zb_adapter"],
           "channel": int(form["zb_channel"]) if form.get("zb_channel") else cur["channel"]}
    modules.save(stack, "zigbee", new)
    if (new["host"], new["port"], new["adapter"]) != (cur.get("host"), cur.get("port"), cur.get("adapter")):
        ss.write_env(stack, ss.ensure_secrets(stack))
        if host:
            start(stack)
            return ["Zigbee2MQTT restarted with the coordinator's address"]
        stop(stack)
    return []


MODULE = Module(
    id="zigbee",
    title="Zigbee",
    description="Zigbee devices through a network coordinator (Zigbee2MQTT on this box)",
    order=70,
    default_enabled=False,
    defaults=DEFAULTS,
    on_enable=start,
    on_disable=stop,
    form_section=form_section,
    form_check=form_check,
    form_save=form_save,
    compose_profiles=lambda stack: ["zigbee"] if settings(stack).get("host") else [],
    env=env,
)
