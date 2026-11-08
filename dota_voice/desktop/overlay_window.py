"""Transparent always-on-top overlay window.

This is a real desktop window (not an OBS browser source): frameless,
transparent, and above the game. It renders the same live state the web overlay
does, by loading ``/overlay`` in a web view.

Click-through, but not everywhere
---------------------------------
The overlay must not steal input from the game, so by default every click goes
straight through it. The exceptions are the few controls the player actually
needs to hit: the drag handle, the microphone chip and the self-mute chip.

A click-through window cannot be driven by its own page, and the naive fix -
keep the window click-through and flip it on when the page says the cursor is
over a control - deadlocks: a window with ``WA_TransparentForMouseEvents`` never
delivers ``mousemove`` to its web view, so the page's ``:hover`` state never
updates, so it never reports "the cursor is over a control", so the window never
becomes interactive.

So the window does the work itself. It reads the real cursor from the operating
system (:mod:`dota_voice.desktop.pointer`), converts that point into page
coordinates, and hit-tests the rectangles the page publishes. While the cursor
is over a control it accepts the mouse, so the game does not also receive the
click; everywhere else every click passes straight through. The press, the drag
and the release are all detected by polling the native pointer, so the controls
work even when the window happened to be click-through at the moment of the
press.

Controls
--------
Each interactive rectangle carries an action:

* ``drag``    - press and hold to move the whole overlay;
* ``mic``     - a click cycles the microphone mode;
* ``mute``    - a click silences or restores the local microphone;
* ``hotkeys`` - a click toggles the in-page hotkeys help (forwarded to the page).

Holding ``Ctrl`` turns the whole overlay into a drag surface, so it can be moved
from anywhere instead of only from the small ⠿ handle. The page never handles
these clicks itself when the native bridge is present; the window is the single
source of truth, which is what keeps a click from being counted twice.

Dragging
--------
Press on the ⠿ handle (or hold ``Ctrl`` and press anywhere) and the window
follows the cursor anywhere, including onto another monitor. The position is
written to ``overlay.json`` when the drag ends and restored on the next launch,
so the overlay comes back where it was left.

Growing to fit
--------------
The page reports its real content size through the bridge, and the window grows
(or shrinks) to match, clamped to the screen. A fixed size clipped the bottom
players off a full ten-player match.

The native bridge
-----------------
The page talks to this window through a Qt WebChannel. That requires the
``QWebChannel`` JavaScript class, which is *not* part of the page - it ships
inside the Qt resource system and must be injected. Forgetting to inject it is
silent: the page's bridge shim finds ``QWebChannel`` undefined, bails out, and
every control quietly stops working. So the channel script is read from
``:/qtwebchannel/qwebchannel.js`` and injected before the page loads, together
with a small bootstrap that exposes ``window.overlayBridge``.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import (
    QDateTime,
    QFile,
    QIODevice,
    QObject,
    QPoint,
    Qt,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
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
# How long the button must be held before a press counts as a drag rather than
# a click, so a click on a chip is not misread as a tiny drag.
CLICK_HOLD_MS = 220

# One interactive control the page publishes: (x, y, w, h, action).
Rect = tuple[float, float, float, float, str]


class OverlayBridge(QObject):
    """The object the overlay page talks to.

    The page cannot move a native window by itself, and it cannot even see the
    mouse while the window is click-through. So the page does one thing: it
    reports the rectangles of its interactive controls, each tagged with the
    action a click should perform. The window does the rest - reading the real
    cursor, deciding click-through, and running the drag or the click - in
    Python, where the platform APIs live.

    The bridge is absent in an OBS browser source, and every method here is
    optional there: the page falls back to handling its own clicks and degrades
    to a passive display plus in-page controls.
    """

    mic_cycled = Signal()
    mute_toggled = Signal()
    rects_reported = Signal(list)
    content_resized = Signal(int, int)

    def __init__(self, window: "OverlayWindow") -> None:
        super().__init__(window)
        self._window = window

    @Slot(list)
    def setInteractiveRects(self, rects) -> None:  # noqa: N802 - JS naming
        """Receive ``[{x, y, w, h, action}, ...]`` in CSS pixels of the page.

        ``action`` is ``"drag"`` (press and hold to move the overlay),
        ``"mic"`` (cycle the microphone mode) or ``"mute"`` (toggle self-mute).
        """
        cleaned: list[Rect] = []
        for rect in rects or []:
            try:
                cleaned.append(
                    (
                        float(rect["x"]),
                        float(rect["y"]),
                        float(rect["w"]),
                        float(rect["h"]),
                        str(rect.get("action") or "click"),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        self._window.set_interactive_rects(cleaned)

    @Slot(int, int)
    def setContentSize(self, width: int, height: int) -> None:  # noqa: N802 - JS
        """Receive the page's real content size so the window can grow to fit."""
        self.content_resized.emit(int(width), int(height))

    @Slot()
    def cycleMic(self) -> None:  # noqa: N802 - JS naming
        self.mic_cycled.emit()

    @Slot()
    def toggleMute(self) -> None:  # noqa: N802 - JS naming
        self.mute_toggled.emit()


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
        self._rects: list[Rect] = []
        self._page_scale = 1.0
        # A press that has not yet become a drag: (action, press_time_ms).
        self._press: tuple[str, int] | None = None
        self._dragging = False

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
        # injected before any page script runs, so `overlayBridge` is available
        # immediately.
        self.bridge = OverlayBridge(self)
        self.mic_cycled = self.bridge.mic_cycled
        self.mute_toggled = self.bridge.mute_toggled
        self._channel = QWebChannel(self.view.page())
        self._channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self._channel)
        self._inject_bridge_shim()
        self.view.load(QUrl(overlay_url))

        # Grow to fit the roster: a fixed window clipped the bottom players off a
        # full 10-player match. The page reports its real content size and we
        # resize (keeping the top-left anchored), clamped to the screen.
        self._content_size = SIZE
        self.bridge.content_resized.connect(self._on_content_resized)

        # Poll the native cursor: decide click-through, and drive the drag or
        # the click. This is what makes the overlay usable over a full-screen
        # game, where we never receive mouse events of our own.
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._poll_pointer)
        self._poll.start(POINTER_INTERVAL_MS)

        QTimer.singleShot(0, self.reposition)

    def _on_content_resized(self, width: int, height: int) -> None:
        """Resize the window to the page's content, keeping it on screen."""
        width = max(SIZE[0], min(width, 640))
        height = max(120, min(height, 1200))
        self._content_size = (width, height)
        self.resize(width, height)
        # A resized window must stay fully visible, so nudge it back on screen.
        x, y = clamp_to_screen(
            self.x(), self.y(), width, height, self._screens()
        )
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def _inject_bridge_shim(self) -> None:
        """Expose a tiny `overlayBridge` helper to the page.

        Qt's ``QWebChannel`` JavaScript class is not part of the page; it lives
        in the Qt resource system. Without injecting it the page's bridge shim
        finds ``QWebChannel`` undefined and silently no-ops - every control then
        stops working. So the channel script is injected first, then a bootstrap
        that wires ``window.overlayBridge`` to our ``OverlayBridge`` object.

        Both scripts run in the main world at document creation, before any of
        the page's own scripts, so the page always sees a ready bridge. In an
        OBS browser source there is no Qt, the resource is absent, and the page
        falls back to its own click handling.
        """
        qwebchannel = self._read_channel_script()
        if qwebchannel:
            self._add_script("dotavoice-qwebchannel", qwebchannel)

        bootstrap = """
        (function () {
          window.overlayBridge = {
            available: false,
            setInteractiveRects: function () {},
            setContentSize: function () {},
            cycleMic: function () {},
            toggleMute: function () {},
          };
          if (typeof QWebChannel === 'undefined') return;
          if (typeof qt === 'undefined' || !qt.webChannelTransport) return;
          new QWebChannel(qt.webChannelTransport, function (channel) {
            var b = channel.objects.bridge;
            if (!b) return;
            window.overlayBridge = {
              available: true,
              setInteractiveRects: function (rects) { b.setInteractiveRects(rects); },
              setContentSize: function (w, h) { b.setContentSize(w, h); },
              cycleMic: function () { b.cycleMic(); },
              toggleMute: function () { b.toggleMute(); },
            };
            if (window.reportInteractiveRects) window.reportInteractiveRects();
            if (window.reportContentSize) window.reportContentSize();
          });
        })();
        """
        self._add_script("dotavoice-overlay-bridge", bootstrap)

    @staticmethod
    def _read_channel_script() -> str:
        """Read Qt's ``qwebchannel.js`` from the resource system, if present."""
        handle = QFile(":/qtwebchannel/qwebchannel.js")
        if not handle.open(QIODevice.ReadOnly):
            logger.warning("Не удалось прочитать qwebchannel.js из ресурсов Qt")
            return ""
        try:
            return bytes(handle.readAll()).decode("utf-8")
        finally:
            handle.close()

    def _add_script(self, name: str, source: str) -> None:
        script = QWebEngineScript()
        script.setName(name)
        script.setSourceCode(source)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        self.view.page().scripts().insert(script)

    # ------------------------------------------------------------------
    # click-through via native hit-testing
    # ------------------------------------------------------------------

    def set_interactive_rects(self, rects: list[Rect]) -> None:
        """Store the page's interactive control rectangles (CSS pixels)."""
        self._rects = list(rects)

    def set_interactive(self, interactive: bool) -> None:
        """Accept clicks (True) or pass every click through to the game."""
        if interactive == self._interactive:
            return
        self._interactive = interactive
        self.setAttribute(Qt.WA_TransparentForMouseEvents, not interactive)

    def _hit_test(self, global_x: int, global_y: int) -> str | None:
        """Return the action of the control under a global point, if any.

        The most specific control wins: a ``drag`` handle sitting inside a
        larger clickable area must still start a drag.
        """
        if not self._rects:
            return None
        # The window's top-left in global coordinates, then into page space.
        origin = self.mapToGlobal(QPoint(0, 0))
        px = (global_x - origin.x()) / self._page_scale
        py = (global_y - origin.y()) / self._page_scale
        found: str | None = None
        for x, y, w, h, action in self._rects:
            if x <= px <= x + w and y <= py <= y + h:
                if action == "drag":
                    return "drag"
                found = found or action
        return found

    def _point_in_window(self, global_x: int, global_y: int) -> bool:
        origin = self.mapToGlobal(QPoint(0, 0))
        return (
            origin.x() <= global_x <= origin.x() + self.width()
            and origin.y() <= global_y <= origin.y() + self.height()
        )

    def _poll_pointer(self) -> None:  # pragma: no cover - needs a real desktop
        """Watch the real cursor: toggle click-through, drive a drag or a click."""
        try:
            gx, gy = self._pointer.pos()
            down = self._pointer.left_down()
            # A pointer that predates the Ctrl gesture (or a platform stub) may
            # not implement this; treat that as "Ctrl not held".
            ctrl = getattr(self._pointer, "ctrl_down", None)
            ctrl = bool(ctrl()) if callable(ctrl) else False
        except Exception as exc:
            logger.debug("Не удалось прочитать курсор: %s", exc)
            return

        if self._dragging:
            # A drag is in progress; follow the cursor and finish on release.
            self._drag_to(gx, gy)
            if not down:
                self._end_drag()
            return

        action = self._hit_test(gx, gy)

        # Ctrl+drag anywhere on the overlay moves it. The ⠿ handle is small and
        # easy to miss, so the whole panel is a drag surface while Ctrl is held:
        # press and it starts moving at once (no hold delay - Ctrl is already an
        # explicit gesture). Controls that mean a click (mic, mute, hotkeys) keep
        # their own behaviour even under Ctrl.
        click_actions = ("mic", "mute", "hotkeys")
        if ctrl and self._point_in_window(gx, gy) and action not in click_actions:
            self.set_interactive(True)
            if down:
                if self._press is None:
                    self._begin_drag(gx, gy)
                    self._dragging = True
            else:
                self._press = None
            return

        # Accept the mouse only while the cursor is over a control, so a click on
        # the game is never swallowed by the overlay.
        self.set_interactive(action is not None)

        if action is None:
            self._press = None
            return
        if down:
            if self._press is None:
                self._press = (action, self._now_ms())
            elif (
                action == "drag"
                and self._now_ms() - self._press[1] >= CLICK_HOLD_MS
            ):
                # Held long enough on the handle: this is a drag, not a click.
                self._begin_drag(gx, gy)
                self._dragging = True
        elif self._press is not None:
            # Released over the control it was pressed on: that is a click.
            pressed_action = self._press[0]
            self._press = None
            if pressed_action == action:
                self._activate(action)

    @staticmethod
    def _now_ms() -> int:
        return int(QDateTime.currentMSecsSinceEpoch())

    def _activate(self, action: str) -> None:
        """Perform a control's click action in the window (native path).

        The microphone and self-mute are handled by the app (they call the
        engine). Any other action belongs to the page - e.g. toggling the
        hotkeys panel - so we forward it to ``window.overlayPageAction`` rather
        than guessing what it means here.
        """
        if action == "mic":
            self.mic_cycled.emit()
        elif action == "mute":
            self.mute_toggled.emit()
        else:
            self._run_page_action(action)

    def _run_page_action(self, action: str) -> None:
        """Run ``window.overlayPageAction(<action>)`` in the page, if defined."""
        name = action.replace("'", "")
        self.view.page().runJavaScript(
            f"window.overlayPageAction && window.overlayPageAction('{name}')"
        )

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
        self._dragging = False
        self._press = None
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
