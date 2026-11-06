"""Тесты упаковки: иконка, ярлыки, установщик.

Проверяется то, что можно проверить без Windows: иконка действительно
многоразмерная, ярлыки создаются с правильным содержимым, XML установщика
корректен, а скрипт сборки находит версию из ``pyproject.toml``.
"""

from __future__ import annotations

import sys
import xml.dom.minidom
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "packaging"))


# ---------------------------------------------------------------------------
# Иконка
# ---------------------------------------------------------------------------


def test_icon_files_exist():
    assert (REPO_ROOT / "dota_voice" / "static" / "icon.png").exists()
    assert (REPO_ROOT / "dota_voice" / "static" / "icon.ico").exists()


def test_icon_is_multisize():
    icon = Image.open(REPO_ROOT / "dota_voice" / "static" / "icon.ico")
    sizes = {size for size in icon.info.get("sizes", [])}
    assert (256, 256) in sizes
    assert (16, 16) in sizes


def test_icon_generator_matches_package(tmp_path):
    import make_icon

    written = make_icon.write_icons(tmp_path)
    assert {p.name for p in written} == {"icon.png", "icon.ico"}
    generated = Image.open(tmp_path / "icon.png")
    packaged = Image.open(REPO_ROOT / "dota_voice" / "static" / "icon.png")
    assert generated.size == packaged.size == (256, 256)
    assert generated.tobytes() == packaged.tobytes()


# ---------------------------------------------------------------------------
# Ярлыки
# ---------------------------------------------------------------------------


def test_shortcuts_on_desktop_and_menu(tmp_path, monkeypatch):
    from dota_voice.desktop import shortcuts

    desktop = tmp_path / "Desktop"
    data_home = tmp_path / "data"
    monkeypatch.setenv("DOTAVOICE_DESKTOP", str(desktop))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    written = shortcuts.create_shortcuts(python=Path("/usr/bin/python3"))
    names = {p.name for p in written}
    assert "dotavoice.desktop" in names
    assert desktop in {p.parent for p in written}

    entry = (desktop / "dotavoice.desktop").read_text(encoding="utf-8")
    assert "Name=DotaVoice" in entry
    assert "/usr/bin/python3" in entry
    assert "dota_voice.desktop" in entry


def test_shortcuts_point_at_packaged_exe(tmp_path, monkeypatch):
    from dota_voice.desktop import shortcuts

    exe = tmp_path / "DotaVoice.exe"
    exe.write_text("fake", encoding="utf-8")
    monkeypatch.setenv("DOTAVOICE_DESKTOP", str(tmp_path / "Desktop"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    written = shortcuts.create_shortcuts(launcher=exe)
    entry = written[0].read_text(encoding="utf-8")
    assert str(exe) in entry
    assert "-m dota_voice.desktop" not in entry


def test_current_launcher_from_source():
    from dota_voice.desktop import shortcuts

    target, arguments = shortcuts.current_launcher()
    assert target == sys.executable
    assert arguments == "-m dota_voice.desktop"


# ---------------------------------------------------------------------------
# Установщик
# ---------------------------------------------------------------------------


def test_wix_source_is_valid_xml():
    document = xml.dom.minidom.parse(str(REPO_ROOT / "packaging" / "installer" / "DotaVoice.wxs"))
    product = document.getElementsByTagName("Product")
    assert len(product) == 1
    assert product[0].getAttribute("Name") == "DotaVoice"
    assert product[0].getAttribute("Version") == "$(var.Version)"


def test_wix_creates_both_shortcuts():
    text = (REPO_ROOT / "packaging" / "installer" / "DotaVoice.wxs").read_text(encoding="utf-8")
    assert "DesktopFolder" in text
    assert "ProgramMenuFolder" in text
    assert text.count("<Shortcut") == 2


def test_build_script_reads_version():
    import build_windows

    assert build_windows._version() != "0.0.0"
    assert build_windows._version().count(".") >= 1


def test_pyinstaller_spec_mentions_webengine():
    text = (REPO_ROOT / "packaging" / "dotavoice.spec").read_text(encoding="utf-8")
    assert "QtWebEngineWidgets" in text
    assert "icon.ico" in text
    assert "console=False" in text
