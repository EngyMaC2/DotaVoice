"""One-shot installer used by the packaged installers and by hand.

It does the things a normal installer must do on any platform:

* create a self-contained virtual environment under the user's data directory,
* install the runtime dependencies (GUI, web engine, audio) into it,
* write launch shortcuts on the desktop and in the Start menu / applications
  menu, pointing at ``dist/DotaVoice.exe`` when a packaged build is present,
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


def write_launchers(python: Path, desktop: bool = True) -> list[Path]:
    """Create launch shortcuts; return the files written.

    If a packaged executable exists (``dist/DotaVoice.exe``), the shortcuts point
    at it so the app launches like a normal program; otherwise they run the
    module through the virtual environment's interpreter.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from dota_voice.desktop.shortcuts import create_shortcuts

    launcher = REPO_ROOT / "dist" / (
        "DotaVoice.exe" if sys.platform.startswith("win") else "DotaVoice"
    )
    if launcher.exists():
        return create_shortcuts(launcher=launcher, desktop=desktop, start_menu=True)
    return create_shortcuts(python=python, desktop=desktop, start_menu=True)


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
        "--no-desktop-shortcut",
        action="store_true",
        help="не создавать ярлык на рабочем столе",
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
        for path in write_launchers(python, desktop=not args.no_desktop_shortcut):
            print(f"Ярлык создан: {path}")
    print("\nГотово. Запуск:")
    print(f"  {python} -m dota_voice.desktop")
    if args.launch:
        os.execv(str(python), [str(python), "-m", "dota_voice.desktop"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
