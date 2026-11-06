"""Transparent always-on-top overlay window.

This is a real desktop window (not an OBS browser source): frameless,
transparent, and above the game. It renders the same live state the web overlay
does, by loading ``/overlay`` in a web view.

Click-through, but not everywhere
---------------------------------
The overlay must not steal input from the game, so by default every click goes
straight through it. The exceptions are the few controls the player actually
needs to hit: the drag handle and the microphone indicator.

The naive way to do this - keep the window click-through and flip it on when the
page says the cursor is over a control - does not work, and it is worth stating
why, because the bug is invisible until you try to use it. A window with
``WA_TransparentForMouseEvents`` never delivers ``mousemove`` to its web view,
so the page's ``:hover`` state never updates, so it never reports "the cursor is
over a control", so the window never becomes interactive. The cursor could sit
on the handle forever and nothing would happen. It is a deadlock between the
window and the page.

So the window does its own hit-testing instead. It reads the real cursor from
the operating system (:mod:`dota_voice.desktop.pointer`), converts that point
into page coordinates, and asks the page "is this point over an interactive
element?" via ``elementFromPoint``. Only then does it accept the mouse. Because
we know the cursor position natively, we can also run the drag ourselves: we do
not need the page to receive a single mouse event.

Dragging
--------
Press on the ⠿ handle and the window follows the cursor anywhere, including onto
another monitor. The position is written to ``overlay.json`` when the drag ends
and restored on the next launch, so the overlay comes back where it was left.
The drag is driven by the native pointer poll, so it works even though the press
was delivered to the game.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QVBoxLayout, QWidget

from .overlay_position import (
    OverlaySettings,
    clamp_to_screen,
    save,
)
from .pointer import Pointer, default_pointer

logger = logging.getLogger(__name__)

MARGIN = 24
SIZE = (400, 340)
# How often we look at the cursor to decide click-through and drive a drag.
POINTER_INTERVAL_MS = 40


class OverlayBridge(QObject):
    """The object the overlay page talks to.

    The page cannot move a native window by itself, and it cannot even see the
    mouse while the window is click-through. So the page does one thing: it
    reports the rectangles of its interactive controls. The window does the
    rest - reading the real cursor, deciding click-through, and running the
    drag - in Python, where the platform APIs live.

    The bridge is absent in an OBS browser source, and every method here is
    optional there: the page degrades to a passive display.
    """

    mic_cycled = Signal()
    rects_reported = Signal(list)

    def __init__(self, window: "OverlayWindow") -> None:
        super().__init__(window)
        self._window = window

    @Slot(list)
    def setInteractiveRects(self, rects) -> None:  # noqa: N802 - JS naming
        """Receive ``[{x, y, w, h, drag}, ...]`` in CSS pixels of the page.

        ``drag`` marks a rectangle that also starts a window drag; other
        interactive rectangles (the microphone indicator) accept a click but
        never move the overlay.
        """
        cleaned: list[tuple[float, float, float, float, bool]] = []
        for rect in rects or []:
            try:
                cleaned.append(
                    (
                        float(rect["x"]),
                        float(rect["y"]),
                        float(rect["w"]),
                        float(rect["h"]),
                        bool(rect.get("drag")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        self._window.set_interactive_rects(cleaned)

    @Slot()
    def cycleMic(self) -> None:  # noqa: N802 - JS naming
        self.mic_cycled.emit()


class OverlayWindow(QWidget):
    """A draggable, position-remembering, mostly click-through overlay."""

    position_changed = Signal(int, int, int)

    def __init__(
        self,
        overlay_url: str,
        *,
        settings: OverlaySettings | None = None,
        position: str | None = None,
        opacity: float | None = None,
        pointer: Pointer | None = None,
    ) -> None:
        super().__init__(None)
        self._settings = settings or OverlaySettings()
        if position:
            self._settings.position.corner = position
        if opacity is not None:
            self._settings.opacity = opacity

        self._pointer = pointer or default_pointer()
        self._drag_origin: tuple[int, int] | None = None
        self._window_origin: QPoint | None = None
        self._interactive = False
        # Interactive control rectangles in page CSS pixels, reported by the page.
        self._rects: list[tuple[float, float, float, float]] = []
        self._page_scale = 1.0

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool  # keep it out of the taskbar
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowOpacity(max(0.2, min(1.0, self._settings.opacity)))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.view = QWebEngineView(self)
        self.view.setAttribute(Qt.WA_TranslucentBackground, True)
        self.view.page().setBackgroundColor(Qt.transparent)
        self.view.setContextMenuPolicy(Qt.NoContextMenu)
        layout.addWidget(self.view)

        self.resize(*SIZE)
        # Start click-through: the game keeps every click until the cursor
        # reaches one of our controls.
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        # The page reports its control rectangles through this bridge; it is
        # injected before any page script runs, so `bridge` is available
        # immediately.
        self.bridge = OverlayBridge(self)
        self.mic_cycled = self.bridge.mic_cycled
        self._channel = QWebChannel(self.view.page())
        self._channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self._channel)
        self._inject_bridge_shim()
        self.view.load(QUrl(overlay_url))

        # Poll the native cursor: decide click-through, and run the drag. This
        # is what makes the overlay usable over a full-screen game, where we
        # never receive mouse events of our own.
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._poll_pointer)
        self._poll.start(POINTER_INTERVAL_MS)

        QTimer.singleShot(0, self.reposition)

    def _inject_bridge_shim(self) -> None:
        """Expose a tiny `overlayBridge` helper to the page.

        It no-ops when there is no Qt WebChannel (an OBS browser source), so the
        same HTML works in both places.
        """
        shim = """
        (function () {
          window.overlayBridge = {
            available: false,
            setInteractiveRects: function () {},
            cycleMic: function () {},
          };
          if (typeof QWebChannel === 'undefined') return;
          new QWebChannel(qt.webChannelTransport, function (channel) {
            var b = channel.objects.bridge;
            if (!b) return;
            window.overlayBridge = {
              available: true,
              setInteractiveRects: function (rects) { b.setInteractiveRects(rects); },
              cycleMic: function () { b.cycleMic(); },
            };
            if (window.reportInteractiveRects) window.reportInteractiveRects();
          });
        })();
        """
        script = QWebEngineScript()
        script.setName("dotavoice-overlay-bridge")
        script.setSourceCode(shim)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        self.view.page().scripts().insert(script)

    # ------------------------------------------------------------------
    # click-through via native hit-testing
    # ------------------------------------------------------------------

    def set_interactive_rects(
        self, rects: list[tuple[float, float, float, float, bool]]
    ) -> None:
        """Store the page's interactive control rectangles (CSS pixels)."""
        self._rects = list(rects)

    def set_interactive(self, interactive: bool) -> None:
        """Accept clicks (True) or pass every click through to the game."""
        if interactive == self._interactive:
            return
        self._interactive = interactive
        self.setAttribute(Qt.WA_TransparentForMouseEvents, not interactive)

    def _hit_test(self, global_x: int, global_y: int) -> tuple[bool, bool]:
        """Return ``(over_control, starts_drag)`` for a global point."""
        if not self._rects:
            return (False, False)
        # The window's top-left in global coordinates, then into page space.
        origin = self.mapToGlobal(QPoint(0, 0))
        px = (global_x - origin.x()) / self._page_scale
        py = (global_y - origin.y()) / self._page_scale
        over = False
        drag = False
        for x, y, w, h, is_drag in self._rects:
            if x <= px <= x + w and y <= py <= y + h:
                over = True
                drag = drag or is_drag
        return (over, drag)

    def _poll_pointer(self) -> None:  # pragma: no cover - needs a real desktop
        """Watch the real cursor: toggle click-through, drive an active drag."""
        try:
            gx, gy = self._pointer.pos()
            down = self._pointer.left_down()
        except Exception as exc:
            logger.debug("Не удалось прочитать курсор: %s", exc)
            return

        if self._drag_origin is not None:
            # A drag is in progress; follow the cursor and finish on release.
            self._drag_to(gx, gy)
            if not down:
                self._end_drag()
            return

        over, starts_drag = self._hit_test(gx, gy)
        self.set_interactive(over)
        # A press on the handle while we are visible starts a drag. A press on
        # the mic indicator does not: that control only needs the click.
        if starts_drag and down and self.isVisible():
            self._begin_drag(gx, gy)

    # ------------------------------------------------------------------
    # dragging (driven by the native pointer)
    # ------------------------------------------------------------------

    def _begin_drag(self, global_x: int, global_y: int) -> None:
        self._drag_origin = (int(global_x), int(global_y))
        self._window_origin = self.pos()

    def _drag_to(self, global_x: int, global_y: int) -> None:
        if self._drag_origin is None or self._window_origin is None:
            return
        dx = int(global_x) - self._drag_origin[0]
        dy = int(global_y) - self._drag_origin[1]
        self.move(self._window_origin + QPoint(dx, dy))

    def _end_drag(self) -> None:
        self._drag_origin = None
        self._window_origin = None
        self.remember_position()

    # Public aliases kept for the tests and for programmatic use.
    def begin_drag(self, global_pos: QPoint) -> None:
        self._begin_drag(global_pos.x(), global_pos.y())

    def drag_to(self, global_pos: QPoint) -> None:
        self._drag_to(global_pos.x(), global_pos.y())

    def end_drag(self) -> None:
        self._end_drag()

    # ------------------------------------------------------------------
    # position
    # ------------------------------------------------------------------

    def _screens(self) -> list[tuple[int, int, int, int]]:
        rects = []
        for screen in QGuiApplication.screens():
            area = screen.availableGeometry()
            rects.append((area.left(), area.top(), area.width(), area.height()))
        return rects

    def monitor_index(self) -> int:
        screen = QGuiApplication.screenAt(self.frameGeometry().center())
        if screen is None:
            return self._settings.position.monitor_index
        screens = QGuiApplication.screens()
        return screens.index(screen) if screen in screens else 0

    def remember_position(self) -> None:
        self._settings.position.x = self.x()
        self._settings.position.y = self.y()
        self._settings.position.monitor_index = self.monitor_index()
        save(self._settings)
        self.position_changed.emit(self.x(), self.y(), self._settings.position.monitor_index)

    def reposition(self) -> None:
        """Place the window at the saved spot, or the chosen corner."""
        pos = self._settings.position
        if pos.has_coordinates:
            x, y = clamp_to_screen(
                int(pos.x), int(pos.y), self.width(), self.height(), self._screens()
            )
            self.move(x, y)
            return
        self._move_to_corner(pos.corner)

    def _move_to_corner(self, corner: str) -> None:
        screens = QGuiApplication.screens()
        index = self._settings.position.monitor_index
        if not screens:
            return  # pragma: no cover - headless
        screen = screens[index] if 0 <= index < len(screens) else screens[0]
        area = screen.availableGeometry()
        width, height = self.width(), self.height()
        corners = {
            "top-left": QPoint(area.left() + MARGIN, area.top() + MARGIN),
            "top-right": QPoint(area.right() - width - MARGIN, area.top() + MARGIN),
            "bottom-left": QPoint(area.left() + MARGIN, area.bottom() - height - MARGIN),
            "bottom-right": QPoint(
                area.right() - width - MARGIN, area.bottom() - height - MARGIN
            ),
        }
        self.move(corners.get(corner, corners["top-left"]))

    def set_position(self, position: str) -> None:
        """Snap to a corner and forget any dragged coordinates."""
        self._settings.position.corner = position
        self._settings.position.x = None
        self._settings.position.y = None
        save(self._settings)
        self._move_to_corner(position)

    def set_opacity(self, opacity: float) -> None:
        """Change the overlay's transparency and remember it."""
        self._settings.opacity = max(0.2, min(1.0, float(opacity)))
        self.setWindowOpacity(self._settings.opacity)
        save(self._settings)

    def reset_position(self) -> None:
        """Bring the overlay back to a corner the player can definitely see."""
        self.set_position(self._settings.position.corner or "top-left")
        self.reposition()

    @property
    def settings(self) -> OverlaySettings:
        return self._settings

    # ------------------------------------------------------------------

    def show_overlay(self) -> None:
        self.show()
        self.raise_()
        self.reposition()

    # Keep the overlay from ever taking focus away from the game.
    def focusInEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.clearFocus()
