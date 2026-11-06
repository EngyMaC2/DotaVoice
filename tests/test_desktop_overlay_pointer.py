"""Tests for the overlay's native hit-testing and pointer-driven dragging.

These cover the bug that made the overlay feel broken: the window was
click-through, so it never received a mousemove, so it could never notice the
cursor was over the drag handle. The fix reads the real cursor instead. Here a
fake pointer stands in for the operating system, so the whole interaction -
hover toggles click-through, a press on the handle drags the window, releasing
saves the position - is exercised without a real desktop.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from dota_voice.appconfig import AppConfig  # noqa: E402
from dota_voice.desktop import overlay_position  # noqa: E402
from dota_voice.desktop.api import EngineAPI  # noqa: E402
from dota_voice.desktop.engine import EngineThread  # noqa: E402


class FakePointer:
    """A scriptable stand-in for the real cursor."""

    def __init__(self) -> None:
        self.x = 0
        self.y = 0
        self.down = False

    def pos(self) -> tuple[int, int]:
        return (self.x, self.y)

    def left_down(self) -> bool:
        return self.down


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


@pytest.fixture()
def engine():
    thread = EngineThread(AppConfig(host="127.0.0.1", port=0).to_settings())
    port = thread.start()
    api = EngineAPI(f"http://127.0.0.1:{port}")
    try:
        yield api
    finally:
        api.close()
        thread.stop()


def _make_overlay(engine, pointer, tmp_path, monkeypatch):
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    monkeypatch.setenv("DOTAVOICE_CONFIG_DIR", str(tmp_path))
    from dota_voice.desktop.overlay_window import OverlayWindow

    overlay = OverlayWindow(engine.overlay_url(), pointer=pointer)
    overlay.reposition()
    # The page reports control rectangles; emulate the two real controls:
    # the drag handle at the top-left and the mic pill beside it.
    overlay.set_interactive_rects(
        [
            (16.0, 16.0, 26.0, 26.0, True),   # handle (drag)
            (50.0, 16.0, 120.0, 24.0, False),  # mic indicator (click only)
        ]
    )
    return overlay


def test_the_overlay_starts_click_through(app, engine, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt

    overlay = _make_overlay(engine, FakePointer(), tmp_path, monkeypatch)
    assert overlay.testAttribute(Qt.WA_TransparentForMouseEvents)
    overlay.deleteLater()
    app.processEvents()


def test_hovering_the_handle_makes_the_window_interactive(app, engine, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt

    pointer = FakePointer()
    overlay = _make_overlay(engine, pointer, tmp_path, monkeypatch)
    origin = overlay.mapToGlobal(QPoint(0, 0))

    # Cursor far from any control: still click-through.
    pointer.x = origin.x() + 300
    pointer.y = origin.y() + 300
    overlay._poll_pointer()
    assert overlay.testAttribute(Qt.WA_TransparentForMouseEvents)

    # Cursor over the handle: the window now accepts input.
    pointer.x = origin.x() + 20
    pointer.y = origin.y() + 20
    overlay._poll_pointer()
    assert not overlay.testAttribute(Qt.WA_TransparentForMouseEvents)

    overlay.deleteLater()
    app.processEvents()


def test_a_press_on_the_handle_drags_the_window(app, engine, tmp_path, monkeypatch):
    pointer = FakePointer()
    overlay = _make_overlay(engine, pointer, tmp_path, monkeypatch)
    overlay.show_overlay()
    origin = overlay.mapToGlobal(QPoint(0, 0))
    start = overlay.pos()

    # Press on the handle, move the cursor, release.
    pointer.x = origin.x() + 20
    pointer.y = origin.y() + 20
    pointer.down = True
    overlay._poll_pointer()
    assert overlay._drag_origin is not None

    pointer.x += 120
    pointer.y += 80
    overlay._poll_pointer()
    moved = overlay.pos()
    assert moved == start + QPoint(120, 80)

    pointer.down = False
    overlay._poll_pointer()
    assert overlay._drag_origin is None
    # The new position was written to disk.
    saved = overlay_position.load(tmp_path / "overlay.json")
    assert saved.position.x == moved.x()
    assert saved.position.y == moved.y()

    overlay.deleteLater()
    app.processEvents()


def test_a_press_on_the_mic_indicator_does_not_drag(app, engine, tmp_path, monkeypatch):
    pointer = FakePointer()
    overlay = _make_overlay(engine, pointer, tmp_path, monkeypatch)
    overlay.show_overlay()
    origin = overlay.mapToGlobal(QPoint(0, 0))

    # The mic pill sits to the right of the handle; pressing it must not move
    # the window, it only needs the click to reach the page.
    pointer.x = origin.x() + 100
    pointer.y = origin.y() + 25
    pointer.down = True
    overlay._poll_pointer()
    assert overlay._drag_origin is None

    overlay.deleteLater()
    app.processEvents()


def test_hit_test_returns_drag_only_for_the_handle(app, engine, tmp_path, monkeypatch):
    overlay = _make_overlay(engine, FakePointer(), tmp_path, monkeypatch)
    origin = overlay.mapToGlobal(QPoint(0, 0))
    assert overlay._hit_test(origin.x() + 20, origin.y() + 20) == (True, True)
    assert overlay._hit_test(origin.x() + 100, origin.y() + 25) == (True, False)
    assert overlay._hit_test(origin.x() + 900, origin.y() + 900) == (False, False)
    overlay.deleteLater()
    app.processEvents()
