"""Optional modules: features a site turns on or off, each with the same shape (docs: "Solar monitor modules: design").

A module is a package here, setup/modules/<name>/, whose __init__.py defines MODULE = Module(...). The core finds the
packages, keeps each module's settings in state/modules/<id>.json and calls the parts a module provides, only while
it is enabled. Modules never import solar_web.py or display.py; those call in here.

  state/modules/<id>.json   {"version": 1, "enabled": true, ...the module's own settings}

The first time a module's file is missing, its migrate(stack, None) builds it from the settings the feature had
before it was a module, so a box that updates keeps everything. When a module's version goes up, migrate(stack,
old, old_version) brings the file up to date.
"""
import dataclasses
import importlib
import json
import os
import pkgutil
import sys
import time
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import solar_setup as ss  # noqa: E402

RESERVED = ("version", "enabled")


@dataclasses.dataclass(frozen=True)
class Response:
    """What a module's web route returns; solar_web.py wraps the body in its page layout."""
    code: int = 200
    title: str = ""
    body: str = ""
    location: Optional[str] = None      # set: a 303 redirect there


@dataclasses.dataclass(frozen=True)
class Module:
    id: str
    title: str
    order: int = 100                    # place among the modules (header links, start page cards)
    default_enabled: bool = False
    defaults: dict = dataclasses.field(default_factory=dict)  # the module's own settings and their defaults
    version: int = 1
    migrate: Optional[Callable] = None  # (stack, old settings or None, old version) -> settings
    ha_fields: Optional[Callable] = None  # (stack, site) -> {"<id>_...": value} for /api/ha
    nav: Optional[tuple] = None         # (href, label): a page linked in the web pages' header
    card: Optional[Callable] = None     # (stack) -> HTML of its card on the start page
    routes: dict = dataclasses.field(default_factory=dict)  # ("GET" | "POST", path) -> fn(stack, form) -> Response


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "modules:", *a, file=sys.stderr, flush=True)


_ALL = None


def all_modules():
    """Every module in this release, in their order."""
    global _ALL
    if _ALL is None:
        found = []
        for info in pkgutil.iter_modules(__path__):
            if info.ispkg:
                m = getattr(importlib.import_module(f"{__name__}.{info.name}"), "MODULE", None)
                if isinstance(m, Module):
                    found.append(m)
        _ALL = sorted(found, key=lambda m: (m.order, m.id))
    return _ALL


def get(module_id):
    for m in all_modules():
        if m.id == module_id:
            return m
    raise KeyError(f"no module {module_id!r}")


def path(stack, module_id):
    return os.path.join(stack, "state", "modules", f"{module_id}.json")


def load(stack, module_id):
    """The module's settings with "enabled" and "version"; migrates first if its file is missing or older."""
    m = get(module_id)
    try:
        with open(path(stack, module_id)) as f:
            stored = json.load(f)
    except FileNotFoundError:
        stored = None
    except ValueError as e:              # kept as it is (not replaced by defaults), so nothing is lost
        raise ValueError(f"{path(stack, module_id)} is not valid JSON: {e}") from None
    if stored is None or stored.get("version", 0) < m.version:
        old = None if stored is None else {k: v for k, v in stored.items() if k not in RESERVED}
        new = m.migrate(stack, old, 0 if stored is None else stored.get("version", 0)) if m.migrate else (old or {})
        enabled = (stored or {}).get("enabled", new.pop("enabled", m.default_enabled))
        new.pop("enabled", None)
        stored = {"version": m.version, "enabled": bool(enabled), **new}
        _write(stack, module_id, stored)
    return {**m.defaults, **stored}


def settings(stack, module_id):
    """Only the module's own settings (no "enabled"/"version")."""
    return {k: v for k, v in load(stack, module_id).items() if k not in RESERVED}


def save(stack, module_id, values):
    """Store the module's own settings, keeping whether it's enabled."""
    cur = load(stack, module_id)
    _write(stack, module_id, {**{k: cur[k] for k in RESERVED}, **{k: v for k, v in values.items() if k not in RESERVED}})


def set_enabled(stack, module_id, on):
    cur = load(stack, module_id)
    _write(stack, module_id, {**cur, "enabled": bool(on)})


def _write(stack, module_id, d):
    os.makedirs(os.path.dirname(path(stack, module_id)), exist_ok=True)
    ss.write_if_changed(path(stack, module_id), json.dumps(d, indent=2) + "\n", 0o600)


def enabled(stack):
    out = []
    for m in all_modules():
        try:
            if load(stack, m.id)["enabled"]:
                out.append(m)
        except Exception as e:  # noqa: BLE001 - one module's broken file never takes the others down
            log(f"{m.id}: settings unreadable, treated as off:", e)
    return out


def migrate_all(stack):
    """Create or update every module's settings file (solar_setup.py runs this on start and after an update)."""
    for m in all_modules():
        try:
            load(stack, m.id)
        except Exception as e:  # noqa: BLE001
            log(f"{m.id}: migration failed:", e)


# --------------------------------------------------------------------------
# What the core collects from the enabled modules
# --------------------------------------------------------------------------
def ha_fields(stack, site):
    """The enabled modules' /api/ha fields. Each key must start with "<id>_" so modules never clash."""
    out = {}
    for m in enabled(stack):
        if not m.ha_fields:
            continue
        try:
            fields = m.ha_fields(stack, site)
        except Exception as e:  # noqa: BLE001 - never let one module break the main figures
            log(f"{m.id}: ha_fields failed:", e)
            continue
        for k, v in fields.items():
            if not k.startswith(m.id + "_"):
                raise ValueError(f"module {m.id}: Home Assistant field {k!r} must start with {m.id + '_'!r}")
            out[k] = v
    return out


def nav(stack):
    return [m.nav for m in enabled(stack) if m.nav]


def cards(stack):
    html = []
    for m in enabled(stack):
        if m.card:
            try:
                html.append(m.card(stack))
            except Exception as e:  # noqa: BLE001
                log(f"{m.id}: card failed:", e)
                html.append(f"<div class='card'><b>{m.title}</b>: couldn't be shown ({type(e).__name__}).</div>")
    return "\n".join(html)


def route(stack, method, url_path):
    """The enabled module's handler for this request, or None."""
    for m in enabled(stack):
        fn = m.routes.get((method, url_path))
        if fn:
            return fn
    return None
