"""Remembering where the overlay sits on screen.

The overlay is an ordinary window, so the player can drag it wherever it is out
of the way - over a corner of the minimap, on a second monitor, anywhere. That
position is saved and restored on the next launch, because having to move it
again every game is exactly the kind of thing that makes people stop using an
overlay.

Two details worth stating:

* the position is stored per monitor, so a multi-monitor setup keeps each
  window where it was;
* a saved position is validated against the current screens on load. Monitors
  get unplugged and resolutions change, and an overlay restored onto a screen
  that no longer exists is invisible - which looks like a crash. If the saved
  spot is off every screen we fall back to the default corner.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import paths

logger = logging.getLogger(__name__)

DEFAULT_MARGIN = 24


@dataclass
class OverlayPosition:
    """Where the overlay was left, plus which monitor it was on."""

    x: int | None = None
    y: int | None = None
    monitor_index: int = 0
    corner: str = "top-left"

    @property
    def has_coordinates(self) -> bool:
        return self.x is not None and self.y is not None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class OverlaySettings:
    """Everything about the overlay that persists."""

    enabled: bool = True
    opacity: float = 0.92
    position: OverlayPosition = field(default_factory=OverlayPosition)

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "opacity": self.opacity,
            "position": self.position.as_dict(),
        }


def load(path: Path | None = None) -> OverlaySettings:
    path = path or paths.overlay_file()
    if not path.exists():
        return OverlaySettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Не удалось прочитать положение оверлея: %s", exc)
        return OverlaySettings()
    position_raw = raw.get("position") or {}
    position = OverlayPosition(
        x=position_raw.get("x"),
        y=position_raw.get("y"),
        monitor_index=int(position_raw.get("monitor_index") or 0),
        corner=position_raw.get("corner") or "top-left",
    )
    return OverlaySettings(
        enabled=bool(raw.get("enabled", True)),
        opacity=float(raw.get("opacity", 0.92)),
        position=position,
    )


def save(settings: OverlaySettings, path: Path | None = None) -> Path:
    path = path or paths.overlay_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(settings.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def clamp_to_screen(
    x: int, y: int, width: int, height: int, screens: list[tuple[int, int, int, int]]
) -> tuple[int, int]:
    """Nudge a saved position back onto a real screen.

    ``screens`` is a list of ``(left, top, width, height)`` rectangles. The window
    is kept fully visible: a partially off-screen overlay hides its own buttons.
    """
    if not screens:
        return x, y
    for left, top, screen_w, screen_h in screens:
        if left <= x <= left + screen_w - 40 and top <= y <= top + screen_h - 40:
            return (
                min(max(x, left), left + max(0, screen_w - width)),
                min(max(y, top), top + max(0, screen_h - height)),
            )
    # Nothing matched: the monitor is gone. Use the primary screen's top-left.
    left, top, screen_w, screen_h = screens[0]
    return (
        min(max(x, left), left + max(0, screen_w - width)),
        min(max(y, top), top + max(0, screen_h - height)),
    )
