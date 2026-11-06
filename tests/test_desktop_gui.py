"""Headless GUI tests for the desktop window and overlay.

These run with Qt's ``offscreen`` platform so they need no display, and they
skip cleanly if PySide6 (or its web engine) is unavailable in the environment.
The overlay test asserts the two properties that matter: it is transparent and
it never intercepts mouse events.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from dota_voice.appconfig import AppConfig  # noqa: E402
from dota_voice.desktop.api import EngineAPI  # noqa: E402
from dota_voice.desktop.engine import EngineThread  # noqa: E402


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


@pytest.fixture()
def engine():
    thread = EngineThread(AppConfig(host="127.0.0.1", port=0).to_settings())
    port = thread.start()
    api = EngineAPI(f"http://127.0.0.1:{port}")
    try:
        yield api
    finally:
        api.close()
        thread.stop()


def test_main_window_shows_sessions(app, engine):
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from dota_voice.desktop.main_window import MainWindow

    config = AppConfig(setup_complete=True)
    window = MainWindow(engine, config)
    window.refresh()
    assert "Голос" in window.status_line.text()
    assert window.windowTitle().startswith("DotaVoice")
    assert window._panels == {}
    window.deleteLater()
    app.processEvents()


def test_overlay_is_transparent_and_click_through(app, engine):
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from dota_voice.desktop.overlay_window import OverlayWindow

    overlay = OverlayWindow(engine.overlay_url(), position="top-right")
    assert overlay.testAttribute(Qt.WA_TranslucentBackground)
    assert overlay.testAttribute(Qt.WA_TransparentForMouseEvents)
    assert overlay.windowFlags() & Qt.FramelessWindowHint
    assert overlay.windowFlags() & Qt.WindowStaysOnTopHint
    overlay.deleteLater()
    app.processEvents()


def test_settings_dialog_collects_values(app):
    from dota_voice.desktop.settings_dialog import SettingsDialog

    config = AppConfig(display_name="Тест", port=12345)
    dialog = SettingsDialog(config)
    assert dialog.name_edit.text() == "Тест"
    dialog.name_edit.setText("Новое имя")
    dialog._save()
    assert config.display_name == "Новое имя"
    dialog.deleteLater()
    app.processEvents()


def test_setup_wizard_builds_pages_and_collects(app, tmp_path):
    from dota_voice.desktop.wizard import SetupWizard

    dota = tmp_path / "steamapps" / "common" / "dota 2 beta"
    (dota / "game" / "dota").mkdir(parents=True)

    config = AppConfig()
    wizard = SetupWizard(config)
    assert wizard.pageIds()  # wizard has pages
    wizard.steam_page.dota_edit.setText(str(dota))
    wizard.voice_page.name_edit.setText("Игрок")
    wizard.overlay_page.enabled.setChecked(False)
    wizard.steam_page.collect()
    wizard.voice_page.collect()
    wizard.overlay_page.collect()

    assert config.dota_path == str(dota)
    assert config.display_name == "Игрок"
    assert config.overlay_enabled is False
    wizard.deleteLater()
    app.processEvents()


def test_wizard_installs_gsi_config(app, tmp_path):
    from dota_voice.desktop.wizard import SetupWizard

    dota = tmp_path / "steamapps" / "common" / "dota 2 beta"
    (dota / "game" / "dota").mkdir(parents=True)

    config = AppConfig()
    wizard = SetupWizard(config)
    wizard.steam_page.dota_edit.setText(str(dota))
    wizard.steam_page.token_edit.setText("секрет")
    wizard.accept()

    assert config.setup_complete is True
    assert config.gsi_installed is True
    from dota_voice import steam

    assert steam.gsi_config_installed(dota)
    wizard.deleteLater()
    app.processEvents()
