"""One-shot installer used by the packaged installers and by hand.

It does the things a normal installer must do on any platform:

* create a self-contained virtual environment under the user's data directory,
* install the runtime dependencies (GUI, web engine, audio) into it,
* write launcher entries (a ``.desktop`` file on Linux, Start Menu shortcut on
  Windows, an ``.app``-friendly launcher on macOS),
* optionally run the setup wizard right away.

It is intentionally dependency-free (standard library only) so it can run
before anything is installed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import venv
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
REQUIREMENTS = REPO_ROOT / "requirements.txt"
APP_NAME = "DotaVoice"


def _venv_dir() -> Path:
    override = os.environ.get("DOTAVOICE_VENV")
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / APP_NAME / "venv"


def _venv_python(venv_dir: Path) -> Path:
    if sys.platform.startswith("win"):
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _run(cmd: list[str]) -> None:
    print("→", " ".join(str(part) for part in cmd))
    subprocess.check_call([str(part) for part in cmd])


def create_venv(venv_dir: Path) -> Path:
    python = _venv_python(venv_dir)
    if not python.exists():
        print(f"Создаю виртуальное окружение: {venv_dir}")
        venv.EnvBuilder(with_pip=True, upgrade_deps=True).create(venv_dir)
    else:
        print(f"Окружение уже существует: {venv_dir}")
    return python


def install_dependencies(python: Path) -> None:
    _run([python, "-m", "pip", "install", "--upgrade", "pip"])
    if REQUIREMENTS.exists():
        _run([python, "-m", "pip", "install", "-r", REQUIREMENTS])
    _run([python, "-m", "pip", "install", "-e", str(REPO_ROOT)])


def write_launchers(python: Path) -> list[Path]:
    """Create a platform-appropriate launcher; return the files written."""
    written: list[Path] = []
    if sys.platform.startswith("linux"):
        target = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        apps = target / "applications"
        apps.mkdir(parents=True, exist_ok=True)
        desktop = apps / "dotavoice.desktop"
        icon = REPO_ROOT / "dota_voice" / "static" / "icon.svg"
        desktop.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            "Comment=Голосовой мост для Dota 2\n"
            f"Exec={python} -m dota_voice.desktop\n"
            f"Icon={icon}\n"
            "Terminal=false\n"
            "Categories=Game;AudioVideo;\n",
            encoding="utf-8",
        )
        written.append(desktop)
    elif sys.platform.startswith("win"):  # pragma: no cover - Windows-only
        start_menu = (
            Path(os.environ.get("APPDATA", Path.home()))
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
        )
        start_menu.mkdir(parents=True, exist_ok=True)
        cmd = start_menu / f"{APP_NAME}.cmd"
        cmd.write_text(
            f'@echo off\r\n"{python}" -m dota_voice.desktop %*\r\n',
            encoding="utf-8",
        )
        written.append(cmd)
    elif sys.platform == "darwin":  # pragma: no cover - macOS-only
        target = Path.home() / "Applications"
        target.mkdir(parents=True, exist_ok=True)
        launcher = target / f"{APP_NAME}.command"
        launcher.write_text(
            f'#!/bin/sh\nexec "{python}" -m dota_voice.desktop "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        written.append(launcher)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Установщик DotaVoice")
    parser.add_argument("--venv", help="каталог виртуального окружения")
    parser.add_argument(
        "--no-deps", action="store_true", help="не устанавливать зависимости"
    )
    parser.add_argument(
        "--no-launcher", action="store_true", help="не создавать ярлыки"
    )
    parser.add_argument(
        "--launch", action="store_true", help="запустить приложение после установки"
    )
    args = parser.parse_args(argv)

    venv_dir = Path(args.venv) if args.venv else _venv_dir()
    python = create_venv(venv_dir)
    if not args.no_deps:
        install_dependencies(python)
    if not args.no_launcher:
        for path in write_launchers(python):
            print(f"Ярлык создан: {path}")
    print("\nГотово. Запуск:")
    print(f"  {python} -m dota_voice.desktop")
    if args.launch:
        os.execv(str(python), [str(python), "-m", "dota_voice.desktop"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
