"""Transparent always-on-top overlay window.

This is a real desktop window (not an OBS browser source): frameless,
transparent, and above the game. It renders the same live state the web overlay
does, by loading ``/overlay`` in a web view.

Click-through, but not everywhere
---------------------------------
The overlay must not steal input from the game, so by default every click goes
straight through it. The exceptions are the few controls the player actually
needs to hit: the drag handle and the microphone indicator. Those are marked in
the page with ``data-interactive``, and the window flips its mouse transparency
on and off as the cursor crosses them.

Dragging
--------
Grab the ⠿ handle and the window follows the mouse anywhere, including onto
another monitor. The position is written to ``overlay.json`` when the drag ends
and restored on the next launch, so the overlay comes back where it was left.
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

logger = logging.getLogger(__name__)

MARGIN = 24
SIZE = (400, 340)


class OverlayBridge(QObject):
    """The object the overlay page talks to.

    The page cannot move a native window by itself, so it calls these slots. That
    keeps the drag logic in one place and lets the page stay a plain HTML file
    that also works as an OBS browser source (where the bridge is simply absent).
    """

    mic_cycled = Signal()

    def __init__(self, window: "OverlayWindow") -> None:
        super().__init__(window)
        self._window = window
        self._dragging = False

    @Slot(int, int)
    def beginDrag(self, x: int, y: int) -> None:  # noqa: N802 - JS naming
        self._dragging = True
        self._window.begin_drag(QPoint(int(x), int(y)))

    @Slot(int, int)
    def dragTo(self, x: int, y: int) -> None:  # noqa: N802 - JS naming
        if not self._dragging:
            return
        self._window.drag_to(QPoint(int(x), int(y)))

    @Slot()
    def endDrag(self) -> None:  # noqa: N802 - JS naming
        if not self._dragging:
            return
        self._dragging = False
        self._window.end_drag()

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
    ) -> None:
        super().__init__(None)
        self._settings = settings or OverlaySettings()
        if position:
            self._settings.position.corner = position
        if opacity is not None:
            self._settings.opacity = opacity

        self._drag_origin: QPoint | None = None
        self._window_origin: QPoint | None = None
        self._interactive = False

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

        # The page moves the window through this bridge; it is injected before
        # any page script runs, so `bridge` is available immediately.
        self.bridge = OverlayBridge(self)
        self.mic_cycled = self.bridge.mic_cycled
        self._channel = QWebChannel(self.view.page())
        self._channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self._channel)
        self._inject_bridge_shim()
        self.view.load(QUrl(overlay_url))

        # The page tells us when the cursor is over a control, so we know when
        # to accept clicks instead of passing them to the game.
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._poll_interactive)
        self._poll.start(120)

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
            beginDrag: function () {},
            dragTo: function () {},
            endDrag: function () {},
            cycleMic: function () {},
          };
          if (typeof QWebChannel === 'undefined') return;
          new QWebChannel(qt.webChannelTransport, function (channel) {
            var b = channel.objects.bridge;
            if (!b) return;
            window.overlayBridge = {
              available: true,
              beginDrag: function (x, y) { b.beginDrag(x, y); },
              dragTo: function (x, y) { b.dragTo(x, y); },
              endDrag: function () { b.endDrag(); },
              cycleMic: function () { b.cycleMic(); },
            };
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
    # click-through
    # ------------------------------------------------------------------

    def set_interactive(self, interactive: bool) -> None:
        """Accept clicks (True) or pass every click through to the game."""
        if interactive == self._interactive:
            return
        self._interactive = interactive
        self.setAttribute(Qt.WA_TransparentForMouseEvents, not interactive)

    def _poll_interactive(self) -> None:  # pragma: no cover - needs a real page
        """Ask the page whether the cursor is over an interactive element."""
        script = (
            "(function(){"
            "  var el = document.querySelector(':hover');"
            "  while (el) {"
            "    if (el.dataset && el.dataset.interactive) return true;"
            "    el = el.parentElement;"
            "  }"
            "  return false;"
            "})()"
        )
        try:
            self.view.page().runJavaScript(script, self._on_interactive_result)
        except Exception as exc:
            logger.debug("Не удалось опросить оверлей: %s", exc)

    def _on_interactive_result(self, result) -> None:  # pragma: no cover - Qt callback
        self.set_interactive(bool(result))

    # ------------------------------------------------------------------
    # dragging
    # ------------------------------------------------------------------

    def begin_drag(self, global_pos: QPoint) -> None:
        self._drag_origin = global_pos
        self._window_origin = self.pos()

    def drag_to(self, global_pos: QPoint) -> None:
        if self._drag_origin is None or self._window_origin is None:
            return
        delta = global_pos - self._drag_origin
        self.move(self._window_origin + delta)

    def end_drag(self) -> None:
        self._drag_origin = None
        self._window_origin = None
        self.remember_position()

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
