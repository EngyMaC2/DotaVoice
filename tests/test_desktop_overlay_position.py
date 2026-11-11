"""Tests for overlay position persistence and clamping.

The point of these is the failure mode that makes an overlay look broken: a
saved position on a monitor that is no longer connected. The window must come
back somewhere visible instead of off-screen.
"""

from __future__ import annotations

import json

from dota_voice.desktop import overlay_position
from dota_voice.desktop.overlay_position import (
    OverlayPosition,
    OverlaySettings,
    clamp_to_screen,
    load,
    save,
)


def test_defaults_when_no_file_exists(tmp_path):
    settings = load(tmp_path / "overlay.json")
    assert settings.enabled is True
    assert settings.position.corner == "top-left"
    assert settings.position.has_coordinates is False


def test_a_dragged_position_round_trips(tmp_path):
    path = tmp_path / "overlay.json"
    settings = OverlaySettings(
        opacity=0.8,
        position=OverlayPosition(x=1200, y=300, monitor_index=1, corner="top-right"),
    )
    save(settings, path)

    loaded = load(path)
    assert loaded.opacity == 0.8
    assert loaded.position.x == 1200
    assert loaded.position.y == 300
    assert loaded.position.monitor_index == 1
    assert loaded.position.has_coordinates is True


def test_the_file_is_human_readable_json(tmp_path):
    path = tmp_path / "overlay.json"
    save(OverlaySettings(position=OverlayPosition(x=10, y=20)), path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["position"]["x"] == 10
    assert raw["position"]["y"] == 20


def test_a_corrupt_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "overlay.json"
    path.write_text("не json", encoding="utf-8")
    settings = load(path)
    assert settings.position.corner == "top-left"


def test_save_creates_missing_directories(tmp_path):
    path = tmp_path / "nested" / "deeper" / "overlay.json"
    save(OverlaySettings(), path)
    assert path.exists()


# ----------------------------------------------------------------------
# clamping
# ----------------------------------------------------------------------


def test_a_position_inside_a_screen_is_kept():
    screens = [(0, 0, 1920, 1080)]
    assert clamp_to_screen(400, 300, 400, 340, screens) == (400, 300)


def test_a_position_on_a_second_screen_is_kept():
    screens = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]
    assert clamp_to_screen(2400, 200, 400, 340, screens) == (2400, 200)


def test_a_position_on_a_removed_monitor_is_pulled_back_into_view():
    # The overlay was left on a second monitor that is now unplugged.
    screens = [(0, 0, 1920, 1080)]
    x, y = clamp_to_screen(3000, 400, 400, 340, screens)
    assert 0 <= x <= 1920 - 400
    assert 0 <= y <= 1080 - 340


def test_a_position_hanging_off_the_right_edge_is_clamped():
    screens = [(0, 0, 1920, 1080)]
    x, y = clamp_to_screen(1900, 100, 400, 340, screens)
    assert x == 1920 - 400
    assert y == 100


def test_a_negative_position_is_clamped_to_the_origin():
    screens = [(0, 0, 1920, 1080)]
    x, y = clamp_to_screen(-500, -200, 400, 340, screens)
    assert (x, y) == (0, 0)


def test_no_screens_reports_the_position_unchanged():
    # Nothing to clamp against; better to leave the value than to invent one.
    assert clamp_to_screen(500, 600, 400, 340, []) == (500, 600)


def test_load_uses_the_configured_directory_when_no_path_is_given(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTAVOICE_CONFIG_DIR", str(tmp_path))
    path = overlay_position.paths.overlay_file()
    assert path.parent == tmp_path
    assert path.name == "overlay.json"
