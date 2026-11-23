"""Home Assistant history: Home Assistant entities the owner wants kept in this box's VictoriaMetrics (5 years, for
Grafana and for re-analysing later), beside the ones the modules read themselves (those are always recorded).

Home Assistant publishes each one to the box's Mosquitto (mqtt_statestream in packages/site.yaml); the collector
records every change and the current value once a minute (hastates.py: ha_state{entity_id=...} and, for climate
devices, ha_attribute{entity_id=...,attribute=...}). Changing the list restarts Home Assistant once, because
statestream reads its settings only when Home Assistant starts."""
import html
import time

import hastates
from modules import FieldError, Module

DEFAULTS = {"entities": []}
SLOTS = 3               # empty lines offered on the form


def settings(stack):
    import modules
    return modules.settings(stack, "hahistory")


def chosen(stack):
    return [e for e in settings(stack).get("entities") or [] if e]


def _age(at):
    if not at:
        return ""
    s = max(0, int(time.time() - at))
    return f"{s} s ago" if s < 120 else f"{s // 60} min ago" if s < 7200 else f"{s // 3600} h ago"


def _all_recorded(stack):
    """Everything recorded now, with whether it is the owner's choice or a module's."""
    import modules
    mine = set(chosen(stack))
    out = []
    for e in modules.ha_entities(stack):
        out.append((e, "yours" if e in mine else "a module reads it"))
    return out


def form_section(stack):
    e = html.escape
    ents = chosen(stack)
    known = hastates.known(stack)
    opts = "".join(f"<option value='{e(k['entity_id'])}'>{e(k['name'])}{' (' + e(k['unit']) + ')' if k['unit'] else ''}</option>"
                   for k in known)
    lines = ""
    for i, ent in enumerate(ents + [""] * SLOTS):
        cur = hastates.get(stack, ent) if ent else None
        now = (f" <small>{e(str(cur.get('state')))}{' ' + e(cur['unit']) if cur.get('unit') else ''}, changed {_age(cur.get('at'))}</small>"
               if cur else (" <small>no value yet</small>" if ent else ""))
        lines += (f"<div class='row'><label>Entity <input name='hh_entity{i}' value='{e(ent)}' list='hh_known' "
                  f"placeholder='sensor.boiler_probes_floor_supply' style='min-width:22em'></label>{now}</div>")
    module_ones = [x for x, why in _all_recorded(stack) if why != "yours"]
    also = (f"<small>Also recorded because a module reads them: {e(', '.join(module_ones))}.</small>" if module_ones else "")
    return f"""<fieldset id="hahistory"><legend>Home Assistant history</legend>
<small>Home Assistant entities to keep in this box's history (VictoriaMetrics, 5 years) for Grafana and for looking back
later: temperature probes, plugs, thermostats. Every change is kept, and the value once a minute. Start typing to pick
from Home Assistant's entities; clear a line to stop recording it (what was recorded stays). Save to get more lines.</small>
<datalist id="hh_known">{opts}</datalist>
{lines}
{also}
<small>Changing the list restarts Home Assistant once (about a minute).</small>
</fieldset>"""


def _parse(form):
    out = []
    for k in sorted((k for k in form if k.startswith("hh_entity")), key=lambda k: int(k[9:] or 0)):
        v = (form.get(k) or "").strip().lower()
        if not v:
            continue
        if not hastates.ENTITY_RE.match(v):
            raise FieldError(k, "an entity id such as sensor.boiler_probes_floor_supply")
        if v not in out:
            out.append(v)
    return out


def form_check(stack, form):
    _parse(form)


def form_save(stack, form):
    import modules
    ents = _parse(form)
    if ents == chosen(stack):
        return []
    modules.save(stack, "hahistory", {**settings(stack), "entities": ents})
    if hastates.apply(stack):
        return ["Home Assistant restarting to publish the new list of entities"]
    return []


MODULE = Module(
    id="hahistory",
    title="Home Assistant history",
    description="Keep chosen Home Assistant entities (probes, plugs, thermostats) in this box's history",
    order=72,
    default_enabled=False,
    defaults=DEFAULTS,
    form_section=form_section,
    form_check=form_check,
    form_save=form_save,
    ha_entities=chosen,
)
