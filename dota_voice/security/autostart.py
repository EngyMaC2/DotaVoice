"""Start DotaVoice automatically with the operating system.

The app is meant to be running before a match starts, so it can pick up the
first GSI event. Each platform has a different mechanism:

* **Windows** - a value under the per-user ``Run`` key. No admin rights, easy to
  remove.
* **Linux** - a ``.desktop`` file in ``~/.config/autostart`` (the XDG standard
  that KDE, GNOME and others read).
* **macOS** - a ``LaunchAgent`` plist in ``~/Library/LaunchAgents``.

Everything here is per-user and reversible; nothing writes to a system-wide
location and nothing needs elevation. :func:`disable` removes exactly what
:func:`enable` created.
"""

from __future__ import annotations

import logging
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "AutostartEntry",
    "autostart_path",
    "current_command",
    "disable",
    "enable",
    "is_enabled",
    "status",
]

APP_NAME = "DotaVoice"


@dataclass
class AutostartEntry:
    """Where autostart lives on this platform, and what it runs."""

    path: Path
    command: list[str]
    kind: str  # windows_run | linux_desktop | macos_launchagent


def current_command() -> list[str]:
    """The command that starts the app, as it should be launched at login.

    Uses the frozen executable when packaged, otherwise the current interpreter
    with ``-m dota_voice.desktop``.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "dota_voice.desktop"]


def _windows_run_key():  # pragma: no cover - Windows only
    import winreg

    return winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
    )


def autostart_path() -> Path:
    """The autostart file for this platform (Windows uses the registry)."""
    if sys.platform.startswith("win"):
        # Not a real file; kept for a uniform return type.
        return Path("HKCU") / "Software" / "Microsoft" / "Windows" / "CurrentVersion" / "Run"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"dev.dotavoice.{APP_NAME}.plist"
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    )
    return config_home / "autostart" / "dotavoice.desktop"


def _entry(command: list[str] | None = None) -> AutostartEntry:
    command = command or current_command()
    if sys.platform.startswith("win"):
        kind = "windows_run"
    elif sys.platform == "darwin":
        kind = "macos_launchagent"
    else:
        kind = "linux_desktop"
    return AutostartEntry(path=autostart_path(), command=command, kind=kind)


def _desktop_contents(entry: AutostartEntry) -> str:
    exec_line = " ".join(shlex.quote(part) for part in entry.command)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Голосовой мост DotaVoice запускается вместе с системой\n"
        f"Exec={exec_line}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def _launchagent_contents(entry: AutostartEntry) -> str:
    args = "\n".join(f"        <string>{part}</string>" for part in entry.command)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>dev.dotavoice.{APP_NAME}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"{args}\n"
        "    </array>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "</dict>\n"
        "</plist>\n"
    )


def enable(command: list[str] | None = None) -> AutostartEntry:
    """Register the app to start at login. Returns the entry that was written."""
    entry = _entry(command)
    if entry.kind == "windows_run":  # pragma: no cover - Windows only
        import winreg

        with _windows_run_key() as key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, " ".join(entry.command))
        logger.info("Автозапуск включён (реестр Windows, HKCU\\...\\Run)")
        return entry

    entry.path.parent.mkdir(parents=True, exist_ok=True)
    if entry.kind == "macos_launchagent":
        entry.path.write_text(_launchagent_contents(entry), encoding="utf-8")
    else:
        entry.path.write_text(_desktop_contents(entry), encoding="utf-8")
    logger.info("Автозапуск включён: %s", entry.path)
    return entry


def disable() -> bool:
    """Remove the autostart entry. Returns whether something was removed."""
    if sys.platform.startswith("win"):  # pragma: no cover - Windows only
        import winreg

        try:
            with _windows_run_key() as key:
                winreg.DeleteValue(key, APP_NAME)
            logger.info("Автозапуск выключен (реестр Windows)")
            return True
        except FileNotFoundError:
            return False

    path = autostart_path()
    if path.exists():
        path.unlink()
        logger.info("Автозапуск выключен: %s", path)
        return True
    return False


def is_enabled() -> bool:
    """Whether autostart is currently registered."""
    if sys.platform.startswith("win"):  # pragma: no cover - Windows only
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
            ) as key:
                winreg.QueryValueEx(key, APP_NAME)
            return True
        except FileNotFoundError:
            return False
    return autostart_path().exists()


def status() -> dict:
    """Autostart state for the UI and for ``/healthz``."""
    entry = _entry()
    return {
        "enabled": is_enabled(),
        "kind": entry.kind,
        "path": str(entry.path),
        "command": entry.command,
    }
