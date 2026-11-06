"""Создание ярлыков запуска на всех платформах.

Приложение должно вести себя как обычная программа: ярлык на рабочем столе и в
меню «Пуск» (Windows), в меню приложений и на рабочем столе (Linux), в
``~/Applications`` (macOS). Этот модуль собирает такие ярлыки из одного места,
чтобы установщик и мастер настройки делали одно и то же.

Для Windows ярлык делается двумя способами:

* если доступен ``pywin32`` — через COM (``IShellLink``), это даёт настоящий
  ``.lnk`` с иконкой;
* иначе — через PowerShell (``WScript.Shell``), который есть на любой Windows.

В обоих случаях иконка берётся из ``icon.ico`` пакета.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from .. import paths

logger = logging.getLogger(__name__)

APP_NAME = "DotaVoice"
APP_COMMENT = "Голосовой мост для Dota 2"


def icon_file() -> Path:
    """Путь к ``.ico`` в пакете (используется ярлыками Windows)."""
    return Path(__file__).resolve().parent.parent / "static" / "icon.ico"


def icon_png() -> Path:
    return Path(__file__).resolve().parent.parent / "static" / "icon.png"


def _start_menu_dir() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home()))
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _desktop_dir() -> Path:
    """Каталог рабочего стола.

    На Windows он известен через ``USERPROFILE`` (или переопределяется
    ``DOTAVOICE_DESKTOP``); на Linux — через ``XDG_DESKTOP_DIR``/``Desktop``.
    """
    override = os.environ.get("DOTAVOICE_DESKTOP")
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        return Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
    if sys.platform == "darwin":
        return Path.home() / "Desktop"
    xdg = os.environ.get("XDG_DESKTOP_DIR")
    if xdg:
        return Path(xdg)
    return Path.home() / "Desktop"


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def _windows_shortcut_powershell(
    link: Path, target: str, arguments: str, workdir: Path, icon: Path | None
) -> bool:
    icon_line = f"$s.IconLocation = '{icon}'" if icon and icon.exists() else ""
    script = (
        "$w = New-Object -ComObject WScript.Shell; "
        f"$s = $w.CreateShortcut('{link}'); "
        f"$s.TargetPath = '{target}'; "
        f"$s.Arguments = '{arguments}'; "
        f"$s.WorkingDirectory = '{workdir}'; "
        f"$s.Description = '{APP_COMMENT}'; "
        f"{icon_line}; "
        "$s.Save()"
    )
    try:
        subprocess.check_call(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning("Не удалось создать ярлык %s: %s", link, exc)
        return False


def _windows_shortcut_com(
    link: Path, target: str, arguments: str, workdir: Path, icon: Path | None
) -> bool:
    try:
        import win32com.client  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(str(link))
        shortcut.TargetPath = target
        shortcut.Arguments = arguments
        shortcut.WorkingDirectory = str(workdir)
        shortcut.Description = APP_COMMENT
        if icon and icon.exists():
            shortcut.IconLocation = str(icon)
        shortcut.save()
        return True
    except Exception as exc:  # pragma: no cover - Windows COM failure
        logger.warning("COM-ярлык не создан (%s): %s", link, exc)
        return False


def create_windows_shortcut(
    link: Path,
    target: str,
    arguments: str = "",
    workdir: Path | None = None,
    icon: Path | None = None,
) -> bool:
    link.parent.mkdir(parents=True, exist_ok=True)
    workdir = workdir or Path.home()
    if _windows_shortcut_com(link, target, arguments, workdir, icon):
        return True
    return _windows_shortcut_powershell(link, target, arguments, workdir, icon)


# ---------------------------------------------------------------------------
# Linux / macOS
# ---------------------------------------------------------------------------


def create_linux_shortcut(
    link: Path, exec_line: str, icon: Path | None = None
) -> Path:
    link.parent.mkdir(parents=True, exist_ok=True)
    icon_line = f"Icon={icon}\n" if icon and icon.exists() else ""
    link.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        f"Comment={APP_COMMENT}\n"
        f"Exec={exec_line}\n"
        f"{icon_line}"
        "Terminal=false\n"
        "Categories=Game;AudioVideo;\n",
        encoding="utf-8",
    )
    link.chmod(0o755)
    return link


def create_macos_launcher(link: Path, exec_line: str) -> Path:
    link.parent.mkdir(parents=True, exist_ok=True)
    link.write_text(f'#!/bin/sh\nexec {exec_line} "$@"\n', encoding="utf-8")
    link.chmod(0o755)
    return link


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------


def current_launcher() -> tuple[str, str]:
    """Return ``(target, arguments)`` that would relaunch this app.

    Under a PyInstaller build ``sys.executable`` is the ``.exe`` itself; from
    source it is the interpreter, so the module has to be named explicitly.
    """
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    return sys.executable, "-m dota_voice.desktop"


def create_shortcuts(
    *,
    launcher: Path | None = None,
    python: Path | None = None,
    desktop: bool = True,
    start_menu: bool = True,
) -> list[Path]:
    """Create launch shortcuts; return the files written.

    ``launcher`` — готовый исполняемый файл (``.exe`` после сборки PyInstaller).
    Если он не задан, используется ``python`` и модуль ``dota_voice.desktop``.
    """
    written: list[Path] = []
    icon = icon_file()

    if launcher is not None and Path(launcher).exists():
        target = str(launcher)
        arguments = ""
        workdir = Path(launcher).resolve().parent
    elif python is not None:
        target = str(python)
        arguments = "-m dota_voice.desktop"
        workdir = Path(python).resolve().parent
    else:
        return written

    return _write_for_platform(
        target, arguments, workdir, icon, desktop=desktop, start_menu=start_menu
    )


def create_shortcuts_for_self(desktop: bool = True, start_menu: bool = True) -> list[Path]:
    """Create shortcuts that relaunch the currently running app."""
    target, arguments = current_launcher()
    workdir = Path(target).resolve().parent
    return _write_for_platform(
        target, arguments, workdir, icon_file(), desktop=desktop, start_menu=start_menu
    )


def _write_for_platform(
    target: str,
    arguments: str,
    workdir: Path,
    icon: Path,
    *,
    desktop: bool,
    start_menu: bool,
) -> list[Path]:
    written: list[Path] = []

    if sys.platform.startswith("win"):  # pragma: no cover - Windows-only
        if start_menu:
            link = _start_menu_dir() / f"{APP_NAME}.lnk"
            if create_windows_shortcut(link, target, arguments, workdir, icon):
                written.append(link)
        if desktop:
            link = _desktop_dir() / f"{APP_NAME}.lnk"
            if create_windows_shortcut(link, target, arguments, workdir, icon):
                written.append(link)
    elif sys.platform == "darwin":  # pragma: no cover - macOS-only
        exec_line = f'"{target}" {arguments}'.strip()
        if start_menu:
            written.append(
                create_macos_launcher(
                    Path.home() / "Applications" / f"{APP_NAME}.command", exec_line
                )
            )
        if desktop:
            written.append(
                create_macos_launcher(_desktop_dir() / f"{APP_NAME}.command", exec_line)
            )
    else:
        exec_line = f'"{target}" {arguments}'.strip()
        icon_png_path = icon_png() if icon == icon_file() else icon
        if start_menu:
            written.append(
                create_linux_shortcut(
                    paths.desktop_entry_dir() / "dotavoice.desktop",
                    exec_line,
                    icon_png_path,
                )
            )
        if desktop:
            written.append(
                create_linux_shortcut(
                    _desktop_dir() / "dotavoice.desktop", exec_line, icon_png_path
                )
            )
    return written
