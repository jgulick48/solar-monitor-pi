#!/usr/bin/env python3
"""Writes stack/grafana/dashboards/boiler.json and water.json: python3 tools/gen_module_dashboards.py (from the repository root).
Edit this file, not the JSON, so the panels stay consistent; tests/test_dashboards.py checks every metric they query."""
import json
import sys

DS = {"type": "prometheus", "uid": "victoriametrics"}
TU = ("(1 + 0.8 * $tu) + 32 * $tu", "(1 + 0.8 * $tu)")      # a temperature, and a difference, in the chosen unit


class Board:
    def __init__(self):
        self.panels, self.y, self.id = [], 0, 0

    def add(self, p, w, h, x=None):
        self.id += 1
        if x is None:
            x = getattr(self, "x", 0)
        if x + w > 24:
            x, self.y = 0, self.y + self.rowh
        p.update(id=self.id, gridPos={"x": x, "y": self.y, "w": w, "h": h}, datasource=DS)
        self.panels.append(p)
        self.x, self.rowh = x + w, h
        if self.x >= 24:
            self.x, self.y = 0, self.y + h

    def row(self, title):
        if getattr(self, "x", 0):
            self.x, self.y = 0, self.y + self.rowh
        self.id += 1
        self.panels.append({"id": self.id, "type": "row", "title": title, "collapsed": False,
                            "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}, "panels": []})
        self.y += 1


def t(expr, legend="", ref="A", instant=False, interval=None):
    d = {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref}
    if instant:
        d.update(instant=True, range=False)
    if interval:
        d["interval"] = interval
    return d


def stat(title, targets, unit="none", decimals=None, mappings=None, thresholds=None, desc="", color_mode="value"):
    fc = {"unit": unit, "mappings": mappings or [], "color": {"mode": "thresholds"},
          "thresholds": {"mode": "absolute", "steps": thresholds or [{"color": "text", "value": None}]}}
    if decimals is not None:
        fc["decimals"] = decimals
    return {"type": "stat", "title": title, "description": desc, "targets": targets,
            "fieldConfig": {"defaults": fc, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "colorMode": color_mode,
                        "graphMode": "none", "textMode": "value", "justifyMode": "center"}}


def series(title, targets, unit="none", desc="", stack=False, bars=False, axis="", minv=None, interval=None, decimals=None,
           overrides=(), softmin=None):
    custom = {"lineWidth": 1, "fillOpacity": 80 if bars else 8, "drawStyle": "bars" if bars else "line",
              "spanNulls": False, "showPoints": "never", "axisLabel": axis,
              "stacking": {"mode": "normal" if stack else "none", "group": "A"}}
    d = {"unit": unit, "custom": custom}
    if minv is not None:
        d["min"] = minv
    if softmin is not None:
        custom["axisSoftMin"] = softmin        # bars from 0; a line on its own axis can still go below
    if decimals is not None:
        d["decimals"] = decimals
    p = {"type": "timeseries", "title": title, "description": desc, "targets": targets,
         "fieldConfig": {"defaults": d, "overrides": [
             {"matcher": {"id": "byName", "options": name}, "properties": [{"id": k, "value": v} for k, v in props.items()]}
             for name, props in overrides]},
         "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}}
    if interval:
        p["interval"] = interval
    return p


def timeline(title, targets, on_text, on_color, desc=""):
    mappings = [{"type": "value", "options": {"1": {"text": on_text, "index": 0}, "0": {"text": "off", "index": 1}}}]
    return {"type": "state-timeline", "title": title, "description": desc, "targets": targets,
            "fieldConfig": {"defaults": {"mappings": mappings, "color": {"mode": "thresholds"},
                                         "thresholds": {"mode": "absolute", "steps": [{"color": "transparent", "value": None},
                                                                                      {"color": on_color, "value": 0.5}]},
                                         "custom": {"fillOpacity": 80, "lineWidth": 0}}, "overrides": []},
            "options": {"showValue": "never", "mergeValues": True, "rowHeight": 0.8, "legend": {"showLegend": False}}}


def onoff(on_text, on_color, off_text="", off_color="transparent"):
    return [{"type": "value", "options": {"1": {"text": on_text, "color": on_color, "index": 0},
                                          "0": {"text": off_text, "color": off_color, "index": 1}}}]


def dashboard(uid, title, desc, panels, variables=(), time_from="now-24h"):
    return {"uid": uid, "title": title, "tags": ["solar", uid], "timezone": "browser", "schemaVersion": 39,
            "refresh": "1m", "time": {"from": time_from, "to": "now"}, "description": desc,
            "annotations": {"list": []}, "templating": {"list": list(variables)}, "panels": panels,
            "editable": False, "graphTooltip": 1}


def right_line(label=""):
    return {"custom.drawStyle": "line", "custom.axisPlacement": "right", "custom.showPoints": "always",
            "custom.lineWidth": 2, "custom.fillOpacity": 0, "custom.axisLabel": label}


TEMP_UNIT = {"name": "tu", "label": "Temperature", "type": "custom", "query": "°F : 1,°C : 0",
             "current": {"text": "°F", "value": "1"}, "options": [], "hide": 0}
DAYS = "1d"
GREEN, RED, ORANGE, BLUE = "green", "red", "orange", "blue"


def boiler():
    b = Board()
    states = [{"type": "value", "options": {"0": {"text": "Idle", "color": "text", "index": 0},
                                            "1": {"text": "Heating", "color": ORANGE, "index": 1},
                                            "2": {"text": "Hot water", "color": BLUE, "index": 2},
                                            "3": {"text": "Unclear", "color": "yellow", "index": 3},
                                            "-1": {"text": "Offline", "color": RED, "index": 4},
                                            "-2": {"text": "Setting up", "color": "text", "index": 5}}}]
    b.add(stat("Boiler now", [t("boiler_state", instant=True)], mappings=states), 4, 4)
    b.add(stat("Burner, last 24 h", [t("avg_over_time(boiler_burner_on[24h]) * 100", instant=True)], unit="percent", decimals=0,
               desc="Share of the last 24 hours the burner ran."), 4, 4)
    b.add(stat("Burner today", [t('boiler_burner_minutes_today{kind="all"}', instant=True)], unit="m", decimals=0,
               desc="Minutes the burner has run since midnight (heating and hot water)."), 4, 4)
    b.add(stat("Heat to the floor now", [t("boiler_heat_output_btuh", instant=True)], unit="none", decimals=0,
               desc="BTU/h: floor flow x fluid factor x the floor difference (needs the floor flow in setup)."), 4, 4)
    b.add(stat("Heat today", [t("boiler_heat_today_kbtu", instant=True)], unit="none", decimals=0,
               desc="Thousand BTU delivered to the floor since midnight."), 4, 4)
    b.add(stat("Faults and alerts now", [t("sum(boiler_rule_active)", instant=True)], decimals=0,
               thresholds=[{"color": GREEN, "value": None}, {"color": RED, "value": 1}], color_mode="background",
               desc="Rules active now: see the boiler page for which."), 4, 4)

    b.row("Now and recent")
    b.add(series("Temperatures", [t(f"boiler_temperature_c * {TU[0]}", "{{role}}"),
                                  t(f"boiler_outdoor_c * {TU[0]}", "outdoor", "B")], axis="${tu:text}", decimals=1,
                 desc="Probe temperatures (offsets applied) and the forecast's outdoor temperature."), 24, 10)
    b.add(timeline("Burner, call for heat, hot water", [t("max_over_time(boiler_burner_on[$__interval])", "Burner"),
                                                        t("max_over_time(boiler_call_for_heat[$__interval])", "Call for heat", "B"),
                                                        t("max_over_time(boiler_hot_water_draw[$__interval])", "Hot water draw", "C")],
                   "on", ORANGE, desc="At long ranges each slot shows on if it was on at any time in it."), 24, 5)
    b.add(series("Floor difference", [t(f"boiler_floor_difference_c * {TU[1]}", "supply - return")], axis="${tu:text}",
                 decimals=1, desc="Floor supply minus floor return, probe offsets applied."), 12, 8)
    b.add(series("Heat to the floor", [t("boiler_heat_output_btuh", "BTU/h")], minv=0,
                 desc="Needs the floor flow and fluid in the boiler's settings."), 12, 8)
    b.add(timeline("Faults and alerts", [t("max_over_time(boiler_rule_active[$__interval])", "{{rule}}")], "active", RED), 24, 6)

    b.row("Per day")
    b.add(series("Burner minutes per day", [t('last_over_time(boiler_day_burner_minutes{kind="heating"}[1d])', "heating", interval=DAYS),
                                            t('last_over_time(boiler_day_burner_minutes{kind="hot_water"}[1d])', "hot water", "B", interval=DAYS)],
                 unit="m", bars=True, stack=True, interval=DAYS, minv=0,
                 desc="Each finished day, by what the burner was doing (unclear minutes are left out)."), 12, 8)
    b.add(series("Heat delivered per day", [t("last_over_time(boiler_day_heat_kbtu[1d])", "kBTU", interval=DAYS)],
                 bars=True, interval=DAYS, minv=0), 12, 8)
    b.add(series("Degree days and outdoor mean", [t("last_over_time(boiler_day_hdd[1d])", "heating degree days (65 °F)", interval=DAYS),
                                                  t(f"last_over_time(boiler_day_outdoor_c[1d]) * {TU[0]}", "outdoor mean", "B", interval=DAYS)],
                 bars=True, interval=DAYS, decimals=1, softmin=0, overrides=[("outdoor mean", right_line("${tu:text}"))]),
          12, 8)
    b.add(series("Burner runs and faults per day", [t("last_over_time(boiler_day_runs[1d])", "runs", interval=DAYS),
                                                    t("last_over_time(boiler_day_faults[1d])", "faults", "B", interval=DAYS)],
                 bars=True, interval=DAYS, minv=0), 12, 8)

    b.row("Probes")
    b.add(series("Learned probe offsets", [t(f"boiler_probe_offset_c * {TU[1]}", "{{pair}}")], axis="${tu:text}", decimals=2,
                 desc="What the box subtracts from each probe pair, learned while no heat flows. More than 2 °C apart at rest is a probe problem."), 24, 7)
    return dashboard("boiler", "Boiler", "The boiler module: burner, floor temperatures, heat delivered and faults. Empty until the boiler module is on.",
                     b.panels, [TEMP_UNIT])


def water():
    b = Board()
    b.add(stat("Pump now", [t("water_pump_running", instant=True)],
               mappings=onoff("Running", BLUE, "Off", "text")), 4, 4)
    b.add(stat("Pump today", [t("water_pump_run_seconds_today", instant=True)], unit="s", decimals=0), 4, 4)
    b.add(stat("Starts today", [t("water_pump_starts_today", instant=True)], decimals=0), 4, 4)
    b.add(stat("Used today", [t("water_used_today_gal", instant=True)], unit="none", decimals=0,
               desc="Gallons: from the meter, else the tank, else the pump's run time x its flow (an estimate)."), 4, 4)
    b.add(stat("Tank", [t("water_tank_level_percent", instant=True)], unit="percent", decimals=0,
               thresholds=[{"color": RED, "value": None}, {"color": ORANGE, "value": 15}, {"color": GREEN, "value": 30}]), 4, 4)
    b.add(stat("Alerts now", [t("sum(water_rule_active)", instant=True)], decimals=0,
               thresholds=[{"color": GREEN, "value": None}, {"color": RED, "value": 1}], color_mode="background"), 4, 4)

    b.row("Now and recent")
    b.add(series("Pump power", [t('max_over_time(ha_state[$__interval]) and on(entity_id) water_role_info{role="pump_power"}', "pump")],
                 unit="watt", minv=0, desc="The pump plug's readings (the HA state feed, in its own unit); at long ranges, the highest in each slot so short runs still show."), 24, 9)
    b.add(series("Pump starts and run time per hour",
                 [t("increase(water_pump_starts_today[1h])", "starts"),
                  t("increase(water_pump_run_seconds_today[1h]) / 60", "minutes running", "B")], minv=0, interval="1h", bars=True,
                 overrides=[("minutes running", right_line("minutes"))],
                 desc="Lots of short starts per hour with little run time points to a waterlogged pressure tank."), 12, 8)
    b.add(series("Tank level", [t("water_tank_level_percent", "%")], unit="percent", minv=0), 12, 8)
    b.add(series("Water meter", [t('ha_state and on(entity_id) water_role_info{role="water_meter"}', "meter total")],
                 desc="The meter's running total, in its own unit."), 12, 8)
    b.add(series("Days of water left", [t("water_days_left", "days")], minv=0, decimals=1,
                 desc="The tank's gallons over the last 7 days' average use."), 12, 8)
    b.add(timeline("Alerts", [t("max_over_time(water_rule_active[$__interval])", "{{rule}}")], "active", RED), 24, 6)

    b.row("Per day")
    b.add(series("Use per day", [t("last_over_time(water_day_used_gal[1d])", "{{source}}", interval=DAYS)], bars=True, interval=DAYS, minv=0,
                 desc="Gallons per finished day; the legend says where it came from (meter, tank or pump estimate)."), 12, 8)
    b.add(series("Pump run time per day", [t("last_over_time(water_day_pump_run_minutes[1d])", "minutes", interval=DAYS)],
                 unit="m", bars=True, interval=DAYS, minv=0), 12, 8)
    b.add(series("Pump runs and longest run per day", [t("last_over_time(water_day_pump_runs[1d])", "runs", interval=DAYS),
                                                       t("last_over_time(water_day_longest_run_seconds[1d]) / 60", "longest run (min)", "B", interval=DAYS)],
                 bars=True, interval=DAYS, minv=0, overrides=[("longest run (min)", right_line("longest run, min"))]), 12, 8)
    b.add(series("Night hours with water use", [t("last_over_time(water_day_night_hours[1d])", "of the 4 hours from 1 to 5 am", interval=DAYS)],
                 bars=True, interval=DAYS, minv=0, desc="4 of 4 sends the water-used-all-night alert."), 12, 8)
    return dashboard("water", "Water", "The water module: pump runs, use, tank and alerts. Empty until the water module is on.", b.panels,
                     time_from="now-7d")


root = sys.argv[1] if len(sys.argv) > 1 else "."
for name, d in (("boiler", boiler()), ("water", water())):
    with open(f"{root}/stack/grafana/dashboards/{name}.json", "w") as f:
        json.dump(d, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(name, len(d["panels"]), "panels")
