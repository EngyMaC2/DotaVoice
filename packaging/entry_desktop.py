"""Точка входа для сборки PyInstaller.

PyInstaller выполняет этот файл как ``__main__``. Здесь мы аккуратно готовим
окружение (поддержка ``multiprocessing``, путь к ресурсам внутри распакованного
архива) и передаём управление обычному приложению.
"""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path


def _prepare_paths() -> None:
    """Добавить каталог сборки в ``sys.path`` (на случай дополнительных данных)."""
    if getattr(sys, "frozen", False):
        bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        if str(bundle) not in sys.path:
            sys.path.insert(0, str(bundle))


def main() -> int:
    _prepare_paths()
    from dota_voice.desktop.app import run

    return run()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
