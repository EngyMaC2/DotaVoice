# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-спецификация для сборки DotaVoice в один .exe.

Собирает оконное приложение (без консоли) с иконкой, кладёт внутрь статические
страницы (``overlay.html``, ``control.html``, иконки) и корректно включает
Qt WebEngine, который PyInstaller иначе может пропустить.

Сборка:

    pyinstaller packaging/dotavoice.spec --noconfirm

Результат: ``dist/DotaVoice.exe`` (один файл).
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

REPO_ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - задаёт PyInstaller
STATIC = REPO_ROOT / "dota_voice" / "static"

datas = [
    (str(STATIC / "overlay.html"), "dota_voice/static"),
    (str(STATIC / "control.html"), "dota_voice/static"),
    (str(STATIC / "icon.svg"), "dota_voice/static"),
    (str(STATIC / "icon.png"), "dota_voice/static"),
    (str(STATIC / "icon.ico"), "dota_voice/static"),
]

# Qt WebEngine тянет ресурсы и вспомогательные процессы, которые нужно включить
# явно, иначе оверлей покажет пустую страницу.
hiddenimports = [
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebChannel",
    "PySide6.QtNetwork",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "tiktoken_ext.openai_public",
    "tiktoken_ext",
]

hiddenimports += collect_submodules("dota_voice")

# litellm читает карту цен и токенизаторы из файлов данных — без них импорт
# падает прямо при старте.
for package in ("litellm", "tiktoken", "tiktoken_ext", "openhands"):
    datas += collect_data_files(package)


a = Analysis(  # noqa: F821 - задаёт PyInstaller
    [str(REPO_ROOT / "packaging" / "entry_desktop.py")],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DotaVoice",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(STATIC / "icon.ico"),
)
