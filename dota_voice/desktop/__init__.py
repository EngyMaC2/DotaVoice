"""Desktop application package.

The bridge ships as a real desktop program, not a website: a native window with
a setup wizard (Steam / Dota 2 folders), a control panel, and a transparent
always-on-top overlay. The HTTP/WebSocket engine from :mod:`dota_voice.server`
runs embedded in a background thread; the UI talks to it over localhost and
never requires the user to open a browser.
"""

from __future__ import annotations

__all__ = ["run"]


def run(*args, **kwargs):  # pragma: no cover - thin lazy import
    from .app import run as _run

    return _run(*args, **kwargs)
