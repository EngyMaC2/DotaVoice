"""Microphone modes and global hotkeys.

Three ways to talk, matching what voice apps normally offer:

``Открытый микрофон`` (continuous)
    The microphone is always live. Simplest, and the mode most people want when
    playing with friends.

``Push-to-Talk``
    The microphone is live only while the key is held down.

``Toggle``
    One press turns the microphone on, the next turns it off. Useful when the
    key is awkward to hold.

The mode and the keys persist between runs, and the same controller is used by
the overlay (click the indicator to cycle) and by the main window (settings), so
the two can never disagree.

Global hotkeys
--------------
A hotkey that only fires while our window has focus is useless here: Dota 2 has
the focus. So the key is read directly from the operating system:

* Windows - ``GetAsyncKeyState`` polled on a small timer;
* Linux (X11) - ``XQueryKeymap`` polled the same way;
* anything else - no global polling, and the app says so honestly instead of
  pretending the key works.

The polling layer is behind :class:`KeyPoller`, so the mode logic above it is
plain, deterministic and testable without touching real input.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

logger = logging.getLogger(__name__)

POLL_INTERVAL = 0.03


class MicMode(str, Enum):
    """How the microphone decides when to transmit."""

    CONTINUOUS = "continuous"
    PUSH_TO_TALK = "push_to_talk"
    TOGGLE = "toggle"

    @property
    def label(self) -> str:
        return {
            "continuous": "Открытый микрофон",
            "push_to_talk": "Push-to-Talk",
            "toggle": "Toggle",
        }[self.value]

    @property
    def short_label(self) -> str:
        return {
            "continuous": "Постоянный",
            "push_to_talk": "Нажимной",
            "toggle": "Переключательный",
        }[self.value]

    @property
    def color(self) -> str:
        """Indicator colour: green, yellow, blue."""
        return {
            "continuous": "#7ed957",
            "push_to_talk": "#ffd166",
            "toggle": "#4d9dff",
        }[self.value]

    @property
    def hint(self) -> str:
        return {
            "continuous": "Микрофон всегда включён — вас слышно постоянно.",
            "push_to_talk": "Микрофон включён, пока удерживаете клавишу.",
            "toggle": "Нажатие включает микрофон, следующее — выключает.",
        }[self.value]

    def next(self) -> "MicMode":
        """Cycle Постоянный → Push-to-Talk → Toggle → Постоянный."""
        order = [MicMode.CONTINUOUS, MicMode.PUSH_TO_TALK, MicMode.TOGGLE]
        return order[(order.index(self) + 1) % len(order)]

    @classmethod
    def parse(cls, value: str | None) -> "MicMode":
        if not value:
            return cls.CONTINUOUS
        # Accept the spellings a human or a UI might produce: "Push-to-Talk",
        # "push to talk", "PUSH_TO_TALK" all mean the same mode.
        normalised = str(value).strip().lower().replace("-", "_").replace(" ", "_")
        try:
            return cls(normalised)
        except ValueError:
            return cls.CONTINUOUS


# Keys that clash with Dota 2's default controls. Assigning one is allowed, but
# the UI warns, because a hotkey that also moves your hero is worse than useless.
DOTA_CONFLICTING_KEYS = frozenset(
    {"q", "w", "e", "r", "d", "f", "a", "s", "z", "x", "c", "v", "b", "g", "t", "h", "space"}
)

# Virtual key codes for the letters and digits we accept, so the Windows poller
# needs no external library.
_VK_CODES: dict[str, int] = {}
for _ch in "abcdefghijklmnopqrstuvwxyz":
    _VK_CODES[_ch] = 0x41 + (ord(_ch) - ord("a"))
for _digit in "0123456789":
    _VK_CODES[_digit] = 0x30 + int(_digit)
_VK_CODES.update(
    {
        "space": 0x20,
        "shift": 0x10,
        "ctrl": 0x11,
        "alt": 0x12,
        "tab": 0x09,
        "caps_lock": 0x14,
    }
)


def key_code(key: str) -> int | None:
    return _VK_CODES.get((key or "").strip().lower())


def conflicts_with_dota(key: str) -> bool:
    return (key or "").strip().lower() in DOTA_CONFLICTING_KEYS


class KeyPoller(Protocol):
    """Reads whether a key is currently held down."""

    def is_down(self, key: str) -> bool: ...


class NullPoller:
    """Used when the platform has no global key reading."""

    def is_down(self, key: str) -> bool:
        return False


class WindowsKeyPoller:
    """Reads key state straight from user32, so it works over the game."""

    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32

    def is_down(self, key: str) -> bool:
        code = key_code(key)
        if code is None:
            return False
        # High bit set means the key is currently held.
        return bool(self._user32.GetAsyncKeyState(code) & 0x8000)


class X11KeyPoller:
    """Polls the X11 keymap; works while another window (the game) has focus."""

    def __init__(self) -> None:
        name = ctypes.util.find_library("X11")
        if not name:
            raise RuntimeError("libX11 не найдена")
        self._x11 = ctypes.cdll.LoadLibrary(name)
        self._x11.XOpenDisplay.restype = ctypes.c_void_p
        self._display = self._x11.XOpenDisplay(None)
        if not self._display:
            raise RuntimeError("не удалось открыть дисплей X11")
        # XKeysymToKeycode needs a keysym; letters map to their ASCII codes.
        self._x11.XKeysymToKeycode.restype = ctypes.c_ubyte

    def is_down(self, key: str) -> bool:
        normalized = (key or "").strip().lower()
        if len(normalized) != 1:
            return False
        keysym = ord(normalized)
        keycode = self._x11.XKeysymToKeycode(ctypes.c_void_p(self._display), keysym)
        if not keycode:
            return False
        keys = (ctypes.c_char * 32)()
        self._x11.XQueryKeymap(ctypes.c_void_p(self._display), keys)
        byte = keycode // 8
        bit = keycode % 8
        return bool(keys[byte] & (1 << bit))


def default_poller() -> KeyPoller:
    """The best poller this platform can offer, or a no-op."""
    import sys

    if sys.platform.startswith("win"):
        try:
            return WindowsKeyPoller()
        except Exception as exc:  # pragma: no cover - platform dependent
            logger.warning("Глобальные клавиши Windows недоступны: %s", exc)
            return NullPoller()
    if sys.platform.startswith("linux"):
        try:
            return X11KeyPoller()
        except Exception as exc:  # pragma: no cover - no display
            logger.info("Глобальные клавиши недоступны (нет X11): %s", exc)
            return NullPoller()
    return NullPoller()


@dataclass
class MicSettings:
    """Everything about the microphone that persists between runs."""

    mode: str = MicMode.CONTINUOUS.value
    push_to_talk_key: str = "v"
    toggle_key: str = "v"
    click_feedback: bool = True

    @property
    def mode_enum(self) -> MicMode:
        return MicMode.parse(self.mode)


class MicrophoneController:
    """Owns the mode, the keys, and whether we are transmitting right now.

    All state transitions live here. Callers only ever ask "is the microphone
    live?" and subscribe to changes, so the overlay, the window and the voice
    backend cannot drift apart.
    """

    def __init__(
        self,
        *,
        settings: MicSettings | None = None,
        path: Path | None = None,
        poller: KeyPoller | None = None,
        on_change: Callable[[bool], None] | None = None,
        on_mode_change: Callable[[MicMode], None] | None = None,
    ) -> None:
        self._path = path
        self._settings = settings or self._load()
        self._poller = poller
        self._on_change = on_change
        self._on_mode_change = on_mode_change
        self._active = self._settings.mode_enum is MicMode.CONTINUOUS
        self._key_held = False
        self._key_down = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _load(self) -> MicSettings:
        if self._path is None or not self._path.exists():
            return MicSettings()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Не удалось прочитать настройки микрофона: %s", exc)
            return MicSettings()
        known = {f for f in asdict(MicSettings())}
        return MicSettings(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(asdict(self._settings), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - disk failure
            logger.warning("Не удалось сохранить настройки микрофона: %s", exc)

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    @property
    def settings(self) -> MicSettings:
        return self._settings

    @property
    def mode(self) -> MicMode:
        return self._settings.mode_enum

    @property
    def active(self) -> bool:
        """Whether the microphone is live right now."""
        return self._active

    @property
    def hotkey(self) -> str:
        return (
            self._settings.push_to_talk_key
            if self.mode is MicMode.PUSH_TO_TALK
            else self._settings.toggle_key
        )

    def snapshot(self) -> dict:
        return {
            "mode": self.mode.value,
            "mode_label": self.mode.label,
            "mode_short_label": self.mode.short_label,
            "mode_color": self.mode.color,
            "mode_hint": self.mode.hint,
            "active": self._active,
            "hotkey": self.hotkey,
            "push_to_talk_key": self._settings.push_to_talk_key,
            "toggle_key": self._settings.toggle_key,
            "click_feedback": self._settings.click_feedback,
            "global_hotkeys": not isinstance(self._poller, NullPoller)
            and self._poller is not None,
        }

    # ------------------------------------------------------------------
    # transitions
    # ------------------------------------------------------------------

    def _set_active(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        logger.info(
            "Микрофон %s (%s)", "включён" if active else "выключен", self.mode.label
        )
        if self._on_change is not None:
            self._on_change(active)

    def set_mode(self, mode: MicMode | str) -> MicMode:
        new_mode = mode if isinstance(mode, MicMode) else MicMode.parse(str(mode))
        self._settings.mode = new_mode.value
        # Entering a mode resets the live state to that mode's natural default.
        self._key_held = False
        self._set_active(new_mode is MicMode.CONTINUOUS)
        self.save()
        if self._on_mode_change is not None:
            self._on_mode_change(new_mode)
        return new_mode

    def cycle_mode(self) -> MicMode:
        """Used by the overlay: click the indicator to move to the next mode."""
        return self.set_mode(self.mode.next())

    def set_push_to_talk_key(self, key: str) -> None:
        self._settings.push_to_talk_key = (key or "v").strip().lower() or "v"
        self.save()

    def set_toggle_key(self, key: str) -> None:
        self._settings.toggle_key = (key or "v").strip().lower() or "v"
        self.save()

    def set_click_feedback(self, enabled: bool) -> None:
        self._settings.click_feedback = bool(enabled)
        self.save()

    # ------------------------------------------------------------------
    # key events (also driven directly by the in-app shortcut)
    # ------------------------------------------------------------------

    def key_down(self) -> None:
        if self.mode is MicMode.PUSH_TO_TALK:
            self._key_held = True
            self._set_active(True)
        elif self.mode is MicMode.TOGGLE:
            self._key_held = True
            self._set_active(not self._active)

    def key_up(self) -> None:
        self._key_held = False
        if self.mode is MicMode.PUSH_TO_TALK:
            self._set_active(False)

    def press(self) -> None:
        """One complete press-and-release, as an in-app shortcut delivers it."""
        self.key_down()
        self.key_up()

    # ------------------------------------------------------------------
    # global polling
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Begin watching the key globally. Returns whether it is possible."""
        if self._poller is None:
            self._poller = default_poller()
        if isinstance(self._poller, NullPoller):
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, name="dotavoice-hotkeys", daemon=True
        )
        self._thread.start()
        return True

    def _poll_once(self) -> bool:
        """One edge-detection step. Returns whether the key is currently down.

        Kept separate from the loop so the mode machine can be tested by driving
        a fake poller, without a real keyboard or a background thread.
        """
        key = self.hotkey
        down = self._poller.is_down(key) if self._poller is not None else False
        if down and not self._key_down:
            self.key_down()
        elif not down and self._key_down:
            self.key_up()
        self._key_down = down
        return down

    def _poll_loop(self) -> None:  # pragma: no cover - needs a real desktop
        while not self._stop.is_set():
            self._poll_once()
            time.sleep(POLL_INTERVAL)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
