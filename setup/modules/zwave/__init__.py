"""Z-Wave: Z-Wave JS UI on the box for a network (PoE/Ethernet) controller, such as the SONOFF Z-Wave 800 Dongle Max.
Devices are paired on Z-Wave JS UI's page (port 8091); Home Assistant's Z-Wave integration connects to its server on
ws://127.0.0.1:3000 (added once in Home Assistant).

The box owns what must not be lost or drift: the controller's address (ZWAVE_PORT in .env), and the network's
security keys, the radio region and the server settings (zwave/box-settings.json, Z-Wave JS UI's "external settings").
The keys are made once and kept in state/secrets.json; the zwave folder (node data) and state/ are both in the box's
USB backup. Lose the keys and every device has to be paired again."""
import html
import json
import os
import secrets

import radios
import solar_setup as ss
from modules import FieldError, Module

# Z-Wave JS's RF regions (zwave-js RFRegion)
REGIONS = ((1, "USA"), (9, "USA, with Long Range"), (0, "Europe"), (11, "Europe, with Long Range"), (2, "Australia / New Zealand"), (3, "Hong Kong"),
           (5, "India"), (6, "Israel"), (7, "Russia"), (8, "China"), (32, "Japan"), (33, "Korea"))
DEFAULTS = {"host": "", "port": "", "region": 1}
UI_PORT = 8091
SERVER_PORT = 3000
KEY_NAMES = ("S0_Legacy", "S2_Unauthenticated", "S2_Authenticated", "S2_AccessControl")
LR_KEY_NAMES = ("S2_Authenticated", "S2_AccessControl")


def settings(stack):
    import modules
    return modules.settings(stack, "zwave")


def _dir(stack):
    return os.path.join(stack, "zwave")


def keys(stack):
    """The network's security keys: made once (16 random bytes each, as hex), never changed."""
    def make():
        return json.dumps({"keys": {k: secrets.token_hex(16).upper() for k in KEY_NAMES},
                           "long_range": {k: secrets.token_hex(16).upper() for k in LR_KEY_NAMES}})
    return json.loads(ss.secret(stack, "zwave_keys", make))


def box_settings(stack):
    """Z-Wave JS UI's external settings: these show as managed by the box in its page."""
    k = keys(stack)
    return {"securityKeys": k["keys"], "securityKeysLongRange": k["long_range"],
            "rf": {"region": int(settings(stack)["region"])},
            "serverEnabled": True, "serverPort": SERVER_PORT, "serverHost": "127.0.0.1",
            "serverServiceDiscoveryDisabled": True}


def write_settings(stack):
    os.makedirs(_dir(stack), exist_ok=True)
    return ss.write_if_changed(os.path.join(_dir(stack), "box-settings.json"),
                               json.dumps(box_settings(stack), indent=2) + "\n", 0o600)


def env(stack):
    s = settings(stack)
    return [f"ZWAVE_PORT={radios.tcp_url(s)}",
            f"ZWAVE_SESSION_SECRET={ss.secret(stack, 'zwave_session_secret', lambda: secrets.token_urlsafe(32))}"]


def start(stack):
    if settings(stack).get("host"):
        radios.start(stack, "zwave-js-ui", before=write_settings)


def stop(stack):
    radios.stop(stack, "zwave-js-ui")


def form_section(stack):
    s = settings(stack)
    e = html.escape
    regions = "".join(f"<option value='{v}'{' selected' if int(s['region']) == v else ''}>{e(t)}</option>" for v, t in REGIONS)
    status = ""
    if s.get("host"):
        ok = radios.reachable(s["host"], s["port"])
        status = (f"<small>Controller at {e(radios.tcp_url(s))}: "
                  f"{'answers' if ok else '<b>does not answer</b> (check its address, port and that it is in Z-Wave controller mode)'}.</small>")
    return f"""<fieldset id="zwave"><legend>Z-Wave</legend>
<small>Z-Wave devices (thermostats, locks) through a network Z-Wave controller on your Ethernet, such as the SONOFF Z-Wave 800
Dongle Max: nothing is plugged into the box. Pair devices on Z-Wave JS UI's page.</small>
<div class="row"><label>Controller address <input name="zw_host" value="{e(s.get('host', ''))}" placeholder="192.168.1.51"></label>
<label>TCP port <input name="zw_port" value="{e(str(s.get('port', '')))}" placeholder="from its web page"></label></div>
<small>Both are on the controller's own web page (its network or serial-over-TCP settings).</small>
<label>Region <select name="zw_region">{regions}</select></label>
{status}
<p><small>Z-Wave JS UI's page: {radios.port_link(UI_PORT)} on this box. The network's security keys are made and kept by this
box (and in its USB backup); the page shows them as managed. <b>Once, in Home Assistant:</b> Settings &rarr; Devices &amp;
services &rarr; Add integration &rarr; <i>Z-Wave</i>, and give it <code>ws://127.0.0.1:{SERVER_PORT}</code>.</small></p>
</fieldset>"""


def form_check(stack, form):
    if not form.get("zw_host", "").strip():
        return
    try:
        radios.check_address(form.get("zw_host"), form.get("zw_port"))
    except ValueError as e:
        raise FieldError("zw_host" if "address" in str(e) else "zw_port", str(e)) from None
    if not str(form.get("zw_region", "")).isdigit() or int(form["zw_region"]) not in dict(REGIONS):
        raise FieldError("zw_region", "pick the region")


def form_save(stack, form):
    import modules
    cur = settings(stack)
    host, port = (radios.check_address(form.get("zw_host"), form.get("zw_port")) if form.get("zw_host", "").strip()
                  else ("", ""))
    region = int(form["zw_region"]) if str(form.get("zw_region", "")).isdigit() else cur["region"]
    new = {"host": host, "port": port, "region": region}
    modules.save(stack, "zwave", new)
    if new != {k: cur.get(k) for k in new}:
        ss.write_env(stack, ss.ensure_secrets(stack))
        if host:
            start(stack)
            return ["Z-Wave JS UI restarted with the controller's settings"]
        stop(stack)
    return []


MODULE = Module(
    id="zwave",
    title="Z-Wave",
    description="Z-Wave devices through a network controller (Z-Wave JS UI on this box)",
    order=71,
    default_enabled=False,
    defaults=DEFAULTS,
    on_enable=start,
    on_disable=stop,
    form_section=form_section,
    form_check=form_check,
    form_save=form_save,
    compose_profiles=lambda stack: ["zwave"] if settings(stack).get("host") else [],
    env=env,
)
