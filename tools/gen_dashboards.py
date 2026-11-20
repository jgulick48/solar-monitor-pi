#!/usr/bin/env python3
"""Generate the Grafana dashboards in stack/grafana/dashboards/ (run after editing)."""
import json, os
OUT = os.path.join(os.path.dirname(__file__), "..", "stack", "grafana", "dashboards")
DS = {"type": "prometheus", "uid": "victoriametrics"}
_id = [0]
def nid():
    _id[0] += 1; return _id[0]
def tgt(expr, legend="", ref="A"):
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref}
def stat(title, expr, unit, x, y, w=4, h=4, decimals=1, thresholds=None, color_mode="value"):
    return {"id": nid(), "type": "stat", "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": [tgt(expr)], "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": color_mode, "graphMode": "area"},
            "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals,
                "thresholds": thresholds or {"mode": "absolute", "steps": [{"color": "green", "value": None}]}}, "overrides": []}}
def ts(title, targets, unit, x, y, w=12, h=8, stack=False, decimals=None):
    return {"id": nid(), "type": "timeseries", "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": targets, "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
            "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals, "custom": {"lineWidth": 1, "fillOpacity": 10,
                "stacking": {"mode": "normal" if stack else "none"}}}, "overrides": []}}
def row(title, y):
    return {"id": nid(), "type": "row", "title": title, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "collapsed": False, "panels": []}

soc_thr = {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "orange", "value": 25}, {"color": "green", "value": 50}]}
P = []
P.append(row("Now (all inverters)", 0))
P += [
    stat("Battery", "max(lxp_soc)", "percent", 0, 1, thresholds=soc_thr, decimals=0),
    stat("Solar", "sum(lxp_p_pv)", "watt", 4, 1, decimals=0),
    stat("House load", "sum(lxp_p_load) or sum(lxp_p_eps)", "watt", 8, 1, decimals=0),
    stat("Battery power (+ charging)", "sum(lxp_p_charge) - sum(lxp_p_discharge)", "watt", 12, 1, decimals=0),
    stat("Battery voltage", "avg(lxp_v_bat)", "volt", 16, 1, decimals=1),
    stat("Inverters online", "sum(lxp_bridge_connected)", "none", 20, 1, decimals=0,
         thresholds={"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}]}),
]
P.append(row("Today", 5))
P += [
    stat("Solar today", "sum(lxp_e_pv_day)", "kwatth", 0, 6, w=6),
    stat("Used today", "sum(lxp_e_load_day) or sum(lxp_e_eps_day)", "kwatth", 6, 6, w=6),
    stat("Battery charged today", "sum(lxp_e_chg_day)", "kwatth", 12, 6, w=6),
    stat("Battery discharged today", "sum(lxp_e_dischg_day)", "kwatth", 18, 6, w=6),
]
P.append(row("History", 10))
P += [
    ts("Power", [tgt("sum(lxp_p_pv)", "Solar", "A"), tgt("sum(lxp_p_load) or sum(lxp_p_eps)", "House load", "B"),
                 tgt("sum(lxp_p_charge) - sum(lxp_p_discharge)", "Battery (+ charging)", "C"),
                 tgt("sum(lxp_p_gen)", "Generator input", "D")], "watt", 0, 11, w=16, h=9),
    ts("Battery state of charge", [tgt("max(lxp_soc)", "SoC", "A")], "percent", 16, 11, w=8, h=9, decimals=0),
    ts("Daily solar and use", [tgt("max_over_time(sum(lxp_e_pv_day)[1d:5m])", "Solar", "A"),
                               tgt("max_over_time((sum(lxp_e_load_day) or sum(lxp_e_eps_day))[1d:5m])", "Used", "B")], "kwatth", 0, 20, w=24, h=8),
]
P.append(row("Per inverter", 28))
P += [
    ts("Solar per inverter", [tgt("sum by (deploymentID) (lxp_p_pv)", "{{deploymentID}}")], "watt", 0, 29, stack=True),
    ts("Load per inverter", [tgt("sum by (deploymentID) (lxp_p_load or lxp_p_eps)", "{{deploymentID}}")], "watt", 12, 29, stack=True),
    ts("Inverter temperatures", [tgt("max by (deploymentID) (lxp_t_inner)", "{{deploymentID}} inner", "A"),
                                 tgt("max by (deploymentID) (lxp_t_rad_1)", "{{deploymentID}} radiator", "B")], "celsius", 0, 37),
    ts("Fault / warning codes", [tgt("max by (deploymentID) (lxp_fault_code)", "{{deploymentID}} fault", "A"),
                                 tgt("max by (deploymentID) (lxp_warning_code)", "{{deploymentID}} warning", "B")], "none", 12, 37),
    ts("Dongle connection", [tgt("max by (deploymentID) (lxp_bridge_connected)", "{{deploymentID}} connected", "A"),
                             tgt("max by (deploymentID) (lxp_bridge_last_frame_age_s)", "{{deploymentID}} last frame age (s)", "B")], "none", 0, 45, w=24),
]
dash = {"uid": "solar-overview", "title": "Solar overview", "tags": ["solar"], "timezone": "browser", "schemaVersion": 39,
        "refresh": "30s", "time": {"from": "now-24h", "to": "now"}, "panels": P, "templating": {"list": []}, "annotations": {"list": []}}
os.makedirs(OUT, exist_ok=True)
with open(os.path.join(OUT, "solar-overview.json"), "w") as f:
    json.dump(dash, f, indent=1)
print("wrote", len(P), "panels")
