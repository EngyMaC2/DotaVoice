"""Platform-specific locations for the app's own files.

Everything the application writes about itself (settings, logs, cached session
state) lives under these directories, so the app behaves like a normal desktop
program: per-user config on Windows/macOS/Linux, no files next to the source.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "DotaVoice"
APP_SLUG = "dotavoice"


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def config_dir() -> Path:
    """Directory for user-editable settings."""
    override = _env_path("DOTAVOICE_CONFIG_DIR")
    if override is not None:
        return override
    if sys.platform.startswith("win"):
        base = _env_path("APPDATA") or Path.home() / "AppData" / "Roaming"
        return base / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = _env_path("XDG_CONFIG_HOME") or Path.home() / ".config"
    return base / APP_SLUG


def data_dir() -> Path:
    """Directory for logs and cached state."""
    override = _env_path("DOTAVOICE_DATA_DIR")
    if override is not None:
        return override
    if sys.platform.startswith("win"):
        base = _env_path("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return base / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = _env_path("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return base / APP_SLUG


def config_file() -> Path:
    return config_dir() / "settings.json"


def log_file() -> Path:
    return data_dir() / "logs" / "dotavoice.log"


def sessions_dir() -> Path:
    return data_dir() / "sessions"


def overlay_file() -> Path:
    """Where the overlay remembers its position between runs."""
    return config_dir() / "overlay.json"


def p2wlan_file() -> Path:
    """Where the P2WLAN room map is persisted, so rooms survive a restart."""
    return data_dir() / "p2wlan.json"


def mic_file() -> Path:
    """Where the microphone mode and hotkeys are persisted."""
    return config_dir() / "microphone.json"


def workspace_dir() -> Path:
    return data_dir() / "workspace"


def ensure_dirs() -> None:
    """Create every directory the app writes to."""
    for path in (config_dir(), data_dir(), sessions_dir(), workspace_dir()):
        path.mkdir(parents=True, exist_ok=True)
    log_file().parent.mkdir(parents=True, exist_ok=True)


def desktop_entry_dir() -> Path:
    """Where a Linux ``.desktop`` launcher belongs (empty elsewhere)."""
    if sys.platform.startswith("linux"):
        base = _env_path("XDG_DATA_HOME") or Path.home() / ".local" / "share"
        return base / "applications"
    return Path()
