"""Optional modules: features a site turns on or off, each with the same shape (docs: "Solar monitor modules: design").

A module is a package here, setup/modules/<name>/, whose __init__.py defines MODULE = Module(...). The core finds the
packages, keeps each module's settings in state/modules/<id>.json and calls the parts a module provides, only while
it is enabled. Modules never import solar_web.py or display.py; those call in here, and offer the few things a
module needs from them as services (provide() / service()).

  state/modules/<id>.json   {"version": 1, "enabled": true, "settings": {...the module's own settings}}

The first time a module's file is missing, its migrate(stack, None, 0) builds its settings from where the feature
kept them before it was a module (and detect(stack), if given, says whether it starts enabled), so a box that
updates keeps everything. When a module's version goes up, migrate(stack, old, old_version) brings them up to date.
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

RESERVED = ("version", "enabled", "settings")


@dataclasses.dataclass(frozen=True)
class Response:
    """What a module's web route returns; solar_web.py wraps the body in its page layout."""
    code: int = 200
    title: str = ""
    body: str = ""
    location: Optional[str] = None      # set: a 303 redirect there
    setup_msg: Optional[str] = None     # set: the setup page, with this message in the module's section


@dataclasses.dataclass(frozen=True)
class Module:
    id: str
    title: str
    order: int = 100                    # place among the modules (header links, cards, setup sections)
    default_enabled: bool = False
    detect: Optional[Callable] = None   # (stack) -> bool: enabled on the first run (the feature was in use)
    defaults: dict = dataclasses.field(default_factory=dict)  # the module's own settings and their defaults
    version: int = 1
    migrate: Optional[Callable] = None  # (stack, old settings or None, old version) -> settings
    on_disable: Optional[Callable] = None  # (stack): stop whatever it runs before it's turned off
    ha_fields: Optional[Callable] = None  # (stack, site) -> {"<id>_...": value} for /api/ha
    ha_core: Optional[Callable] = None  # (stack) -> {key: value} added to Home Assistant's core.yaml
    nav: Optional[tuple] = None         # (href, label, page title): a page linked in the web pages' header
    card: Optional[Callable] = None     # (stack) -> HTML of its card on the start page
    card_slot: str = "top"              # where on the start page: "top" (after the advisor) or "access" (after the generator)
    banner: Optional[Callable] = None   # (stack) -> HTML shown at the top of the start page ("" for none)
    setup_section: Optional[Callable] = None  # (stack, msg) -> HTML of its own section at the end of the setup page
    routes: dict = dataclasses.field(default_factory=dict)  # ("GET" | "POST", path) -> fn(stack, form) -> Response
    every: dict = dataclasses.field(default_factory=dict)   # seconds -> fn(stack) -> messages to log, in the collector
    compose_profiles: Optional[Callable] = None  # (stack) -> Compose profiles (optional services) to run now


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
    """{"version", "enabled", "settings"} for the module; migrates first if its file is missing or older."""
    m = get(module_id)
    try:
        with open(path(stack, module_id)) as f:
            stored = json.load(f)
    except FileNotFoundError:
        stored = None
    except ValueError as e:              # kept as it is (not replaced by defaults), so nothing is lost
        raise ValueError(f"{path(stack, module_id)} is not valid JSON: {e}") from None
    if stored is not None and "settings" not in stored:   # v0.2.27 kept the settings beside version/enabled
        stored = {"version": stored.get("version", 0), "enabled": stored.get("enabled", m.default_enabled),
                  "settings": {k: v for k, v in stored.items() if k not in RESERVED}}
        _write(stack, module_id, stored)
    if stored is None or stored.get("version", 0) < m.version:
        old = None if stored is None else stored["settings"]
        new = m.migrate(stack, old, 0 if stored is None else stored.get("version", 0)) if m.migrate else (old or {})
        if stored is None:
            enabled = m.detect(stack) if m.detect else m.default_enabled
        else:
            enabled = stored.get("enabled", m.default_enabled)
        stored = {"version": m.version, "enabled": bool(enabled), "settings": new}
        _write(stack, module_id, stored)
    return stored


def settings(stack, module_id):
    """The module's own settings, with its defaults for what isn't stored."""
    return {**get(module_id).defaults, **load(stack, module_id)["settings"]}


def save(stack, module_id, values):
    """Store the module's own settings, keeping whether it's enabled."""
    cur = load(stack, module_id)
    _write(stack, module_id, {**cur, "settings": dict(values)})


def set_enabled(stack, module_id, on):
    cur = load(stack, module_id)
    m = get(module_id)
    if cur["enabled"] and not on and m.on_disable:
        m.on_disable(stack)
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
# Services the rest of the box offers to modules (solar_web.py provides them when it starts)
# --------------------------------------------------------------------------
_SERVICES = {}


def provide(name, fn):
    _SERVICES[name] = fn


def service(name):
    if name not in _SERVICES:
        raise RuntimeError(f"the {name!r} service isn't available here")
    return _SERVICES[name]


# --------------------------------------------------------------------------
# What the core collects from the enabled modules
# --------------------------------------------------------------------------
def _each(stack, attr, *args, default=None):
    """(module, result) for every enabled module that provides `attr`; a module that fails is logged and skipped."""
    for m in enabled(stack):
        fn = getattr(m, attr)
        if not fn:
            continue
        try:
            yield m, fn(stack, *args)
        except Exception as e:  # noqa: BLE001 - never let one module break the page or the others
            log(f"{m.id}: {attr} failed:", e)
            if default is not None:
                yield m, default(m, e)


def ha_fields(stack, site):
    """The enabled modules' /api/ha fields. Each key must start with "<id>_" so modules never clash."""
    out = {}
    for m, fields in _each(stack, "ha_fields", site):
        for k, v in fields.items():
            if not k.startswith(m.id + "_"):
                raise ValueError(f"module {m.id}: Home Assistant field {k!r} must start with {m.id + '_'!r}")
            out[k] = v
    return out


def ha_core(stack):
    out = {}
    for _, d in _each(stack, "ha_core"):
        out.update(d)
    return out


def nav(stack):
    return [m.nav for m in enabled(stack) if m.nav]


def cards(stack, slot="top"):
    failed = lambda m, e: f"<div class='card'><b>{m.title}</b>: couldn't be shown ({type(e).__name__}).</div>"  # noqa: E731
    return "\n".join(h for m, h in _each(stack, "card", default=failed) if m.card_slot == slot)


def banners(stack):
    return "".join(h for _, h in _each(stack, "banner") if h)


def setup_sections(stack, msgs=None):
    """The enabled modules' setup-page sections; msgs: {module id: message for its section}."""
    out = []
    for m in enabled(stack):
        if m.setup_section:
            try:
                out.append(m.setup_section(stack, (msgs or {}).get(m.id, "")))
            except Exception as e:  # noqa: BLE001
                log(f"{m.id}: setup_section failed:", e)
                out.append(f"<h2>{m.title}</h2><p class='err'>This section couldn't be shown ({type(e).__name__}: {e}).</p>")
    return "\n".join(out)


def compose_profiles(stack):
    out = []
    for _, profiles in _each(stack, "compose_profiles"):
        out += [p for p in profiles if p not in out]
    return out


def route(stack, method, url_path):
    """(module, handler) of the enabled module that answers this request, or (None, None)."""
    for m in enabled(stack):
        fn = m.routes.get((method, url_path))
        if fn:
            return m, fn
    return None, None


def run_due(stack, last, now=None):
    """The collector's hooks: run each enabled module's `every` functions whose time has come. `last` remembers
    when each ran ({(id, seconds): time}); a hook that fails is logged and tried again on its next turn."""
    now = now if now is not None else time.time()
    for m in enabled(stack):
        for secs, fn in m.every.items():
            key = (m.id, secs)
            if now - last.get(key, 0) < secs:
                continue
            last[key] = now
            try:
                for msg in fn(stack) or ():
                    log(f"{m.title}: {msg}")
            except Exception as e:  # noqa: BLE001 - keep collecting
                log(f"{m.title}: failed:", e)
