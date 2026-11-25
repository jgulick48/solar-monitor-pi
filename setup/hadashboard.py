#!/usr/bin/env python3
"""The Home dashboard in Home Assistant: one phone-first page for checking on the place from anywhere (the Home
Assistant app, through remote access).

  * Now: open alerts, the generator advice, the 5-day outlook, battery, solar, house, battery power, generator.
  * Cameras: each UniFi Protect camera in Home Assistant (a snapshot every few seconds; tap for live video), the
    ones picked for the wall display first.
  * One short section per enabled module: propane, water, boiler, freeze protection, generator.
  * Today, and links to the full Solar dashboard and Home Assistant's Energy page.

It is written by the box (homeassistant/dashboards/home.yaml, a YAML dashboard registered in the solar monitor's
package), from the enabled modules and the camera entities in Home Assistant's registry, whenever either changes:
by the setup run and by the cameras module's 5-minute job. Home Assistant re-reads a YAML dashboard when the page
is opened, so no restart. Like the Solar dashboard, it is replaced on every change: to make your own, copy cards
from it into a dashboard of yours.
"""
import json
import os

import solar_setup as ss

REL = os.path.join("dashboards", "home.yaml")
HEADER = ("# The Home dashboard: written by the solar monitor from its modules and cameras, replaced on every change.\n"
          "# To make your own, create a dashboard in Settings -> Dashboards and copy cards from this one.\n")


def _visible_unless(entity, *states):
    return [{"condition": "state", "entity": entity, "state_not": s} for s in ("unavailable", "unknown") + states]


def _tile(entity, name, **kw):
    return {"type": "tile", "entity": entity, "name": name, "visibility": _visible_unless(entity), **kw}


def camera_entities(stack):
    """UniFi Protect camera entities in Home Assistant (enabled, not hidden), one per camera, as (entity id, name):
    the wall display's picked cameras first, in their order, then the rest by name."""
    ha = os.path.join(stack, "homeassistant", ".storage")
    ents = (ss.load_json(os.path.join(ha, "core.entity_registry"), {}).get("data") or {}).get("entities", [])
    reg = ss.load_json(os.path.join(ha, "core.device_registry"), {}).get("data") or {}
    # cameras behind a console (UniFi Protect's) are under child_devices in Home Assistant 2026.11's registry
    devs = {d.get("id"): d for k in ("devices", "child_devices") for d in reg.get(k) or []}
    per_dev = {}
    for e in ents:
        eid = e.get("entity_id", "")
        if e.get("platform") != "unifiprotect" or not eid.startswith("camera.") or e.get("disabled_by") or e.get("hidden_by"):
            continue
        dev = devs.get(e.get("device_id")) or {}
        name = dev.get("name_by_user") or dev.get("name") or e.get("name") or e.get("original_name") or eid
        key = e.get("device_id") or eid
        best = per_dev.get(key)
        # one per camera: its high-resolution stream when there are several (a doorbell's package camera is separate
        # only when the owner turned it on)
        if best is None or ("high" in eid and "high" not in best[0]):
            per_dev[key] = (eid, name)
    try:
        import protect
        picked = [c["name"] for c in protect.settings(stack).get("cameras") or []]
    except Exception:  # noqa: BLE001 - only the order depends on it
        picked = []
    order = {n.strip().lower(): i for i, n in enumerate(picked)}
    return sorted(per_dev.values(), key=lambda c: (order.get(c[1].strip().lower(), len(order)), c[1].lower()))


def _enabled(stack):
    import modules
    out = set()
    for m in ("propane", "water", "boiler", "freeze", "generator"):
        try:
            if modules.load(stack, m)["enabled"]:
                out.add(m)
        except Exception:  # noqa: BLE001 - one module's broken settings never take the page down
            pass
    # the generator module is on by default; its section only where the site has a generator
    if (ss.load_json(os.path.join(stack, "state", "site.json"), {}).get("generator") or "none") == "none":
        out.discard("generator")
    return out


def build(stack):
    """The dashboard, as data."""
    on = _enabled(stack)
    now = {"type": "grid", "cards": [
        {"type": "heading", "heading": "Now", "icon": "mdi:home-lightning-bolt"},
        {"type": "markdown", "grid_options": {"columns": "full"},
         "content": "{% for a in state_attr('sensor.site_alerts', 'alerts') or [] %}"
                    "<ha-alert alert-type=\"warning\">{{ a }}</ha-alert>{% endfor %}",
         "visibility": [{"condition": "numeric_state", "entity": "sensor.site_alerts", "above": 0}]},
        {"type": "markdown", "grid_options": {"columns": "full"},
         "content": "<ha-alert alert-type=\"{{ 'warning' if is_state('sensor.site_generator_advice', 'warn') else 'success' }}\">"
                    "{{ state_attr('sensor.site_generator_advice', 'advice_text') }}</ha-alert>",
         "visibility": [{"condition": "state", "entity": "sensor.site_generator_advice", "state_not": s}
                        for s in ("none", "info", "unavailable", "unknown")]},
        {"type": "markdown", "grid_options": {"columns": "full"},
         "content": "<ha-alert alert-type=\"{{ 'warning' if is_state('sensor.site_outlook', 'storm') else 'info' }}\">"
                    "{{ state_attr('sensor.site_outlook', 'outlook_text') }}</ha-alert>",
         "visibility": [{"condition": "state", "entity": "sensor.site_outlook", "state_not": s}
                        for s in ("none", "ok", "unavailable", "unknown")]},
        _tile("sensor.site_battery_level", "Battery", color="green"),
        _tile("sensor.site_battery_runtime", "Lasts about"),
        _tile("sensor.site_solar_power", "Solar", color="amber"),
        _tile("sensor.site_house_load", "House", color="blue"),
        _tile("sensor.site_battery_power", "Battery (+ charging)", color="green"),
        {**_tile("sensor.site_generator_power", "Generator", color="purple"),
         "visibility": [{"condition": "state", "entity": "sensor.site_generator_advice", "attribute": "generator_mode",
                         "state_not": "none"}]},
    ]}
    sections = [now]
    cams = camera_entities(stack)
    if cams:
        sections.append({"type": "grid", "cards": [{"type": "heading", "heading": "Cameras", "icon": "mdi:cctv"}] + [
            {"type": "picture-entity", "entity": eid, "name": name, "camera_view": "auto", "show_state": False,
             "grid_options": {"columns": "full"}} for eid, name in cams]})
    if "propane" in on:
        sections.append({"type": "grid", "cards": [
            {"type": "heading", "heading": "Propane", "icon": "mdi:gas-cylinder"},
            _tile("sensor.site_propane_days_left", "Days left", color="orange"),
            _tile("sensor.site_propane_used_today", "Used today")]})
    if "water" in on:
        sections.append({"type": "grid", "cards": [
            {"type": "heading", "heading": "Water", "icon": "mdi:water"},
            {**_tile("sensor.site_water_alert", "Alert", color="red", grid_options={"columns": "full"}),
             "visibility": _visible_unless("sensor.site_water_alert", "none")},
            _tile("sensor.site_water_days_left", "Days left", color="blue"),
            _tile("sensor.site_water_tank_level", "Tank", color="blue"),
            _tile("sensor.site_water_used_today", "Used today"),
            _tile("binary_sensor.site_water_pump_running", "Pump")]})
    if "boiler" in on:
        sections.append({"type": "grid", "cards": [
            {"type": "heading", "heading": "Heating", "icon": "mdi:water-boiler"},
            {**_tile("sensor.site_boiler_fault", "Fault", color="red", grid_options={"columns": "full"}),
             "visibility": _visible_unless("sensor.site_boiler_fault", "none")},
            _tile("sensor.site_boiler_state", "Boiler", color="deep-orange"),
            _tile("sensor.site_floor_heat_today", "Heat to the floor today"),
            _tile("sensor.site_boiler_burner_today", "Burner today")]})
    if "freeze" in on:
        sections.append({"type": "grid", "cards": [
            {"type": "heading", "heading": "Freeze protection", "icon": "mdi:snowflake-alert"},
            {**_tile("sensor.site_freeze_alert", "Alert", color="red", grid_options={"columns": "full"}),
             "visibility": _visible_unless("sensor.site_freeze_alert", "none")},
            _tile("sensor.site_lowest_watched_temperature", "Coldest watched", color="light-blue"),
            _tile("sensor.site_forecast_low", "Forecast low (24 h)", color="light-blue")]})
    if "generator" in on:
        sections.append({"type": "grid", "cards": [
            {"type": "heading", "heading": "Generator", "icon": "mdi:engine"},
            _tile("sensor.site_generator_last_run", "Last run", color="purple"),
            _tile("sensor.site_generator_today", "Today"),
            _tile("sensor.site_generator_hours_since_service", "Hours since service")]})
    sections.append({"type": "grid", "cards": [
        {"type": "heading", "heading": "Today", "icon": "mdi:calendar-today"},
        _tile("sensor.site_solar_today", "Solar", color="amber"),
        _tile("sensor.site_used_today", "Used", color="blue"),
        _tile("sensor.site_solar_forecast_tomorrow", "Solar tomorrow", color="amber"),
        {"type": "button", "name": "Solar details", "icon": "mdi:solar-power-variant",
         "tap_action": {"action": "navigate", "navigation_path": "/solar-site/solar"}},
        {"type": "button", "name": "Energy", "icon": "mdi:lightning-bolt",
         "tap_action": {"action": "navigate", "navigation_path": "/energy"}}]})
    return {"title": "Home", "views": [{"title": "Home", "path": "home", "icon": "mdi:home", "type": "sections",
                                        "max_columns": 2, "sections": sections}]}


def write(stack):
    """Write homeassistant/dashboards/home.yaml. True when it changed."""
    try:
        import yaml
        body = yaml.safe_dump(build(stack), sort_keys=False, allow_unicode=True, width=120)
    except ImportError:
        body = json.dumps(build(stack), indent=2) + "\n"       # JSON is YAML too
    path = os.path.join(stack, "homeassistant", REL)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return ss.write_if_changed(path, HEADER + body, 0o644)
