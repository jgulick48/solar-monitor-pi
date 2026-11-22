"""Small pieces of the web pages' look shared by solar_web.py and the modules (which don't import solar_web)."""
import html
import re

ERROR_WORDS = re.compile(r"^(not saved|not turned on)|failed|couldn't|can't|didn't|isn't running|wasn't", re.I)


def is_error(text):
    return bool(ERROR_WORDS.search(text or ""))


def notice(kind, text, toast=False, link=None, anchor=True):
    """A message box: kind "ok" or "err". toast=True: pinned to the top of the window with a close button."""
    extra = f" <a href='{link[0]}'>{html.escape(link[1])}</a>" if link else ""
    if toast:
        return (f"<div class='toast {kind}' role='{'alert' if kind == 'err' else 'status'}'>{html.escape(text)}"
                "<button type='button' class='x' aria-label='Close' onclick='this.parentNode.remove()'>&times;</button></div>")
    return f"<div {'id=notice ' if anchor else ''}class='card notice {kind}'>{html.escape(text)}{extra}</div>"
