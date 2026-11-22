"""Weather on the wall display, with the solar figures: now, the next hours and days, and (in the US) National
Weather Service alerts. Fetching and the view are setup/wx.py; this is its switch, settings and schedule."""
import os

import solar_setup as ss
import wx
from modules import Module

LEGACY = ("state", "display.json")       # where the settings were before modules (v0.2.29 and earlier)
DAYS = range(2, 8)
PAGES = ((5, "5 seconds"), (8, "8 seconds"), (12, "12 seconds"), (20, "20 seconds"), (30, "30 seconds"),
         (0, "never (show only what fits)"))


def _legacy(stack):
    return ss.load_json(os.path.join(stack, *LEGACY), {})


def detect(stack):
    return bool(_legacy(stack).get("weather", True))


def migrate(stack, old, version):
    if old is None:
        d = _legacy(stack)
        return {"days": d.get("wx_days", 7), "page": d.get("wx_page", 8)}
    return old


def form_section(stack):
    import modules
    s = modules.settings(stack, "weather")
    days = "".join(f"<option value='{n}'{' selected' if s['days'] == n else ''}>{n} days</option>" for n in DAYS)
    pages = "".join(f"<option value='{v}'{' selected' if s['page'] == v else ''}>{t}</option>" for v, t in PAGES)
    return f"""<fieldset id="weather-display"><legend>Weather on the screen</legend>
<small>Shown with the solar figures: now, the next hours and days, wind; National Weather Service alerts in the US.</small>
<div class="row"><label>Forecast <select name="wx_days">{days}</select></label>
<label>When they don't all fit, show the next ones every <select name="wx_page">{pages}</select></label></div></fieldset>"""


def form_save(stack, form):
    import modules
    def pick(name, allowed, default):
        v = form.get(name, "")
        return int(v) if str(v).isdigit() and int(v) in allowed else default
    modules.save(stack, "weather", {"days": pick("wx_days", DAYS, 7), "page": pick("wx_page", dict(PAGES), 8)})
    return []


def display_data(stack, site, now):
    import modules
    s = modules.settings(stack, "weather")
    return {"weather": wx.view(stack, site, now), "wx_days": s["days"], "wx_page": s["page"]}


MODULE = Module(
    id="weather",
    title="Weather",
    description="on the wall display: now, the next hours and days, and weather alerts",
    order=40,
    default_enabled=True,
    detect=detect,
    defaults={"days": 7, "page": 8},
    migrate=migrate,
    form_section=form_section,
    form_save=form_save,
    display_data=display_data,
    # forecast every 30 minutes (again in 5 after a failure); alerts every 10
    every={1800: (wx.fetch_forecast, 300), 600: wx.fetch_alerts},
)
