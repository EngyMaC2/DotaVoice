"""``python -m dota_voice.desktop`` entry point."""

from __future__ import annotations

from .app import run

if __name__ == "__main__":
    raise SystemExit(run())
