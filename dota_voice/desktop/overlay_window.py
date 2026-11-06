"""Transparent always-on-top overlay window.

This is a real desktop window (not an OBS browser source): it is frameless,
transparent, sits above the game, and - crucially - never intercepts clicks, so
it cannot steal input from the game underneath. It renders the same live state
the web overlay does by loading ``/overlay`` in a web view and keeping it
click-through.

The window is positioned in the chosen screen corner and can be toggled from the
control panel or the tray icon.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QVBoxLayout, QWidget

logger = logging.getLogger(__name__)

MARGIN = 24
SIZE = (400, 320)


class OverlayWindow(QWidget):
    def __init__(self, overlay_url: str, *, position: str = "top-left", opacity: float = 0.92):
        super().__init__(None)
        self._position = position
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool  # keep it out of the taskbar
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # Click-through: mouse events pass to whatever is below (the game).
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setWindowOpacity(max(0.2, min(1.0, opacity)))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.view = QWebEngineView(self)
        self.view.setAttribute(Qt.WA_TranslucentBackground, True)
        self.view.page().setBackgroundColor(Qt.transparent)
        self.view.setContextMenuPolicy(Qt.NoContextMenu)
        layout.addWidget(self.view)

        self.resize(*SIZE)
        self.view.load(QUrl(overlay_url))
        QTimer.singleShot(0, self.reposition)

    # ------------------------------------------------------------------

    def set_position(self, position: str) -> None:
        self._position = position
        self.reposition()

    def reposition(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:  # pragma: no cover - headless
            return
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
        self.move(corners.get(self._position, corners["top-left"]))

    def show_overlay(self) -> None:
        self.show()
        self.raise_()
        self.reposition()

    # Keep the overlay from ever taking focus away from the game.
    def focusInEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.clearFocus()
