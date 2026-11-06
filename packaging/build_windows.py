"""Сборка DotaVoice для Windows: ``.exe`` (PyInstaller) и ``.msi`` (WiX).

Запускается на Windows. Делает две вещи:

1. собирает ``dist/DotaVoice.exe`` через PyInstaller по спецификации;
2. если найден WiX Toolset (``candle``/``light`` или ``wix`` v4+), собирает
   ``dist/DotaVoice-<версия>.msi``.

Скрипт печатает понятные подсказки, если чего-то не хватает, и не падает, пока
не дойдёт до шага, который действительно требует отсутствующий инструмент.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST = REPO_ROOT / "dist"
SPEC = REPO_ROOT / "packaging" / "dotavoice.spec"
WXS = REPO_ROOT / "packaging" / "installer" / "DotaVoice.wxs"


def _version() -> str:
    import re

    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "0.0.0"


def _run(cmd: list[str]) -> None:
    print("→", " ".join(cmd))
    subprocess.check_call(cmd)


def build_exe(onefile: bool = True) -> Path:
    import importlib.util

    if importlib.util.find_spec("PyInstaller") is None:
        raise SystemExit("Не найден PyInstaller. Установите: pip install pyinstaller")
    _run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            str(SPEC),
            "--noconfirm",
            "--distpath",
            str(DIST),
            "--workpath",
            str(REPO_ROOT / "build"),
        ]
    )
    exe = DIST / ("DotaVoice.exe" if sys.platform.startswith("win") else "DotaVoice")
    if not exe.exists():
        raise SystemExit(f"Сборка не создала {exe}")
    return exe


def _find_wix() -> str | None:
    """Find the WiX v3 toolchain (``candle``), which the ``.wxs`` targets."""
    for name in ("candle", "candle.exe"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (
        Path(r"C:\Program Files (x86)\WiX Toolset v3.14\bin\candle.exe"),
        Path(r"C:\Program Files (x86)\WiX Toolset v3.11\bin\candle.exe"),
    ):
        if candidate.exists():
            return str(candidate)
    return None


def _find_wix_v4() -> str | None:
    found = shutil.which("wix")
    if found:
        return found
    candidate = Path(r"C:\Program Files\WiX Toolset v4\bin\wix.exe")
    return str(candidate) if candidate.exists() else None


def build_msi(exe: Path, version: str) -> Path:
    candle = _find_wix()
    msi = DIST / f"DotaVoice-{version}.msi"
    if candle is None:
        hint = (
            "Найден только WiX v4 — этот файл собран под WiX v3.\n"
            if _find_wix_v4()
            else "WiX Toolset не найден.\n"
        )
        print(
            hint
            + "Установите WiX v3: winget install WiXToolset.WiXToolset.3\n"
            "или скачайте с https://github.com/wixtoolset/wix3/releases и повторите.\n"
            "Сам .exe при этом уже готов и работает без установщика."
        )
        return msi

    # WiX v3: candle (компиляция) + light (линковка), с расширением интерфейса.
    wixobj = REPO_ROOT / "build" / "DotaVoice.wixobj"
    wixobj.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            candle,
            "-ext",
            "WixUIExtension",
            f"-dVersion={version}",
            f"-dSourceDir={DIST}",
            f"-dProjectRoot={REPO_ROOT}",
            "-out",
            str(wixobj),
            str(WXS),
        ]
    )
    light = Path(candle).with_name("light.exe")
    _run(
        [
            str(light),
            "-ext",
            "WixUIExtension",
            "-cultures:ru-ru",
            "-out",
            str(msi),
            str(wixobj),
        ]
    )
    return msi


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сборка DotaVoice для Windows")
    parser.add_argument("--skip-exe", action="store_true", help="не собирать .exe")
    parser.add_argument("--skip-msi", action="store_true", help="не собирать .msi")
    parser.add_argument("--launch", action="store_true", help="запустить .exe после сборки")
    args = parser.parse_args(argv)

    version = _version()
    DIST.mkdir(parents=True, exist_ok=True)

    exe = None
    if not args.skip_exe:
        exe = build_exe()
        print(f"\nГотово: {exe}")
    if not args.skip_msi:
        if exe is None:
            exe = DIST / "DotaVoice.exe"
        msi = build_msi(exe, version)
        if msi.exists():
            print(f"Установщик: {msi}")

    if args.launch and exe is not None and exe.exists():
        subprocess.Popen([str(exe)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
