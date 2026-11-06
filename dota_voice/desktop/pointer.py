"""Read the real mouse, even while the game has focus.

An always-on-top overlay has to answer two questions the Qt event system cannot
answer for it:

1. *Where is the cursor right now?* A click-through window never receives
   ``mousemove``, so it can only learn this by asking the operating system.
2. *Is the left button held?* Same problem: the press goes to the game, not to
   us, unless we are currently accepting input.

Both are read directly from the platform (Win32 ``GetCursorPos`` /
``GetAsyncKeyState``, X11 ``XQueryPointer``). The logic above this layer - drag,
hit-testing, click-through - is then plain and testable with a fake pointer.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys
from typing import Protocol

logger = logging.getLogger(__name__)


class Pointer(Protocol):
    """Where the mouse is and whether its left button is down."""

    def pos(self) -> tuple[int, int]: ...

    def left_down(self) -> bool: ...


class NullPointer:
    """Used when the platform offers no way to read the mouse.

    Everything above keeps working; it simply never sees the cursor, so the
    overlay stays click-through and cannot be dragged on that platform.
    """

    def pos(self) -> tuple[int, int]:
        return (0, 0)

    def left_down(self) -> bool:
        return False


class WindowsPointer:
    """Win32 cursor position and button state."""

    VK_LBUTTON = 0x01

    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        self._point = POINT

    def pos(self) -> tuple[int, int]:
        point = self._point()
        if not self._user32.GetCursorPos(ctypes.byref(point)):
            return (0, 0)
        return (int(point.x), int(point.y))

    def left_down(self) -> bool:
        return bool(self._user32.GetAsyncKeyState(self.VK_LBUTTON) & 0x8000)


class X11Pointer:
    """X11 cursor position and button mask via ``XQueryPointer``."""

    def __init__(self) -> None:
        name = ctypes.util.find_library("X11")
        if not name:
            raise RuntimeError("libX11 не найдена")
        self._x11 = ctypes.cdll.LoadLibrary(name)
        self._x11.XOpenDisplay.restype = ctypes.c_void_p
        self._display = self._x11.XOpenDisplay(None)
        if not self._display:
            raise RuntimeError("не удалось открыть дисплей X11")
        # XQueryPointer wants pointers to the root/child windows and to four
        # ints; declare the signature so ctypes does not truncate them.
        self._root = ctypes.c_ulong()
        self._child = ctypes.c_ulong()
        self._root_x = ctypes.c_int()
        self._root_y = ctypes.c_int()
        self._win_x = ctypes.c_int()
        self._win_y = ctypes.c_int()
        self._mask = ctypes.c_uint()

    def _query(self) -> bool:
        # XQueryPointer(display, window, ...) with window=None means the root.
        return bool(
            self._x11.XQueryPointer(
                ctypes.c_void_p(self._display),
                ctypes.c_void_p(0),
                ctypes.byref(self._root),
                ctypes.byref(self._child),
                ctypes.byref(self._root_x),
                ctypes.byref(self._root_y),
                ctypes.byref(self._win_x),
                ctypes.byref(self._win_y),
                ctypes.byref(self._mask),
            )
        )

    def pos(self) -> tuple[int, int]:
        if not self._query():
            return (0, 0)
        return (int(self._root_x.value), int(self._root_y.value))

    def left_down(self) -> bool:
        if not self._query():
            return False
        # Button1Mask is bit 8 of the modifier mask.
        return bool(self._mask.value & (1 << 8))


def default_pointer() -> Pointer:
    """The best pointer reader this platform can offer, or a no-op."""
    if sys.platform.startswith("win"):
        try:
            return WindowsPointer()
        except Exception as exc:  # pragma: no cover - platform dependent
            logger.warning("Чтение курсора Windows недоступно: %s", exc)
            return NullPointer()
    if sys.platform.startswith("linux"):
        try:
            return X11Pointer()
        except Exception as exc:  # pragma: no cover - no display
            logger.info("Чтение курсора недоступно (нет X11): %s", exc)
            return NullPointer()
    return NullPointer()
