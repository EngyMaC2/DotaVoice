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
    # The page reports control rectangles; emulate the real controls: the drag
    # handle at the top-left and the two chips beside it.
    overlay.set_interactive_rects(
        [
            (16.0, 16.0, 26.0, 26.0, "drag"),   # handle (drag)
            (50.0, 16.0, 120.0, 24.0, "mic"),   # mic chip (click)
            (180.0, 16.0, 90.0, 24.0, "mute"),  # self-mute chip (click)
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

    # Press on the handle and hold long enough that it becomes a drag.
    pointer.x = origin.x() + 20
    pointer.y = origin.y() + 20
    pointer.down = True
    overlay._poll_pointer()
    assert overlay._press is not None
    overlay._press = ("drag", overlay._now_ms() - 1000)
    overlay._poll_pointer()
    assert overlay._dragging is True

    pointer.x += 120
    pointer.y += 80
    overlay._poll_pointer()
    moved = overlay.pos()
    assert moved == start + QPoint(120, 80)

    pointer.down = False
    overlay._poll_pointer()
    assert overlay._dragging is False
    # The new position was written to disk.
    saved = overlay_position.load(tmp_path / "overlay.json")
    assert saved.position.x == moved.x()
    assert saved.position.y == moved.y()

    overlay.deleteLater()
    app.processEvents()


def test_a_quick_click_on_the_mic_chip_cycles_without_dragging(app, engine, tmp_path, monkeypatch):
    pointer = FakePointer()
    overlay = _make_overlay(engine, pointer, tmp_path, monkeypatch)
    overlay.show_overlay()
    origin = overlay.mapToGlobal(QPoint(0, 0))
    start = overlay.pos()
    cycled = []
    overlay.mic_cycled.connect(lambda: cycled.append(True))

    # Press and release on the mic chip without holding long enough to drag.
    pointer.x = origin.x() + 100
    pointer.y = origin.y() + 25
    pointer.down = True
    overlay._poll_pointer()
    assert overlay._dragging is False
    pointer.down = False
    overlay._poll_pointer()

    assert cycled == [True]
    assert overlay.pos() == start  # a click never moves the overlay

    overlay.deleteLater()
    app.processEvents()


def test_a_click_on_the_mute_chip_toggles_self_mute(app, engine, tmp_path, monkeypatch):
    pointer = FakePointer()
    overlay = _make_overlay(engine, pointer, tmp_path, monkeypatch)
    overlay.show_overlay()
    origin = overlay.mapToGlobal(QPoint(0, 0))
    muted = []
    overlay.mute_toggled.connect(lambda: muted.append(True))

    pointer.x = origin.x() + 220
    pointer.y = origin.y() + 25
    pointer.down = True
    overlay._poll_pointer()
    pointer.down = False
    overlay._poll_pointer()

    assert muted == [True]
    overlay.deleteLater()
    app.processEvents()


def test_hit_test_returns_the_control_action(app, engine, tmp_path, monkeypatch):
    overlay = _make_overlay(engine, FakePointer(), tmp_path, monkeypatch)
    origin = overlay.mapToGlobal(QPoint(0, 0))
    assert overlay._hit_test(origin.x() + 20, origin.y() + 20) == "drag"
    assert overlay._hit_test(origin.x() + 100, origin.y() + 25) == "mic"
    assert overlay._hit_test(origin.x() + 220, origin.y() + 25) == "mute"
    assert overlay._hit_test(origin.x() + 900, origin.y() + 900) is None
    overlay.deleteLater()
    app.processEvents()


def test_the_page_reports_its_controls_through_the_bridge(app, engine, tmp_path, monkeypatch):
    """The controls only work if the page can reach the window.

    This is the regression guard for the silent failure where the page's bridge
    shim found Qt's ``QWebChannel`` class undefined and quietly did nothing, so
    neither the mic chip nor the drag handle responded.
    """
    from PySide6.QtCore import QEventLoop, QTimer

    overlay = _make_overlay(engine, FakePointer(), tmp_path, monkeypatch)
    # Wait for the page (and the async WebChannel handshake) to settle.
    loop = QEventLoop()
    QTimer.singleShot(3000, loop.quit)
    loop.exec()

    actions = {rect[4] for rect in overlay._rects}
    assert "drag" in actions
    assert "mic" in actions
    assert "mute" in actions
    overlay.deleteLater()
    app.processEvents()
