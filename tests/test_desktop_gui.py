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


def test_overlay_remembers_its_position(app, engine, tmp_path, monkeypatch):
    """The overlay must come back where the player dragged it."""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    monkeypatch.setenv("DOTAVOICE_CONFIG_DIR", str(tmp_path))
    from dota_voice.desktop.overlay_position import OverlaySettings, load, save
    from dota_voice.desktop.overlay_window import OverlayWindow

    settings = OverlaySettings()
    settings.position.x = 300
    settings.position.y = 240
    save(settings, tmp_path / "overlay.json")

    overlay = OverlayWindow(
        engine.overlay_url(), settings=load(tmp_path / "overlay.json")
    )
    overlay.reposition()
    assert overlay.x() == 300
    assert overlay.y() == 240

    # A drag that ends writes the new spot to disk.
    from PySide6.QtCore import QPoint

    overlay.begin_drag(QPoint(300, 240))
    overlay.drag_to(QPoint(420, 360))
    overlay.end_drag()
    assert load(tmp_path / "overlay.json").position.x == 420
    overlay.deleteLater()
    app.processEvents()


def test_overlay_can_be_reset_to_a_corner(app, engine, tmp_path, monkeypatch):
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    monkeypatch.setenv("DOTAVOICE_CONFIG_DIR", str(tmp_path))
    from dota_voice.desktop.overlay_position import OverlaySettings
    from dota_voice.desktop.overlay_window import OverlayWindow

    settings = OverlaySettings()
    settings.position.x = 99999  # a monitor that no longer exists
    settings.position.y = 99999
    overlay = OverlayWindow(engine.overlay_url(), settings=settings)
    overlay.reset_position()
    # It must land somewhere on a real screen, not off in space.
    assert overlay.x() < 9000
    overlay.deleteLater()
    app.processEvents()


def test_main_window_offers_the_three_mic_modes(app, engine):
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from dota_voice.desktop.main_window import MainWindow

    config = AppConfig(setup_complete=True)
    window = MainWindow(engine, config)
    modes = [window.mic_combo.itemData(i) for i in range(window.mic_combo.count())]
    assert modes == ["continuous", "push_to_talk", "toggle"]
    window.deleteLater()
    app.processEvents()


def test_settings_dialog_exposes_mic_keys_and_modes(app):
    from dota_voice.desktop.settings_dialog import SettingsDialog

    config = AppConfig(display_name="Тест", push_to_talk_key="b", toggle_key="n")
    dialog = SettingsDialog(config)
    assert dialog.ptt_edit.text() == "b"
    assert dialog.toggle_edit.text() == "n"
    assert dialog.mic_mode_combo.count() == 3

    dialog.mic_mode_combo.setCurrentIndex(dialog.mic_mode_combo.findData("toggle"))
    dialog._save()
    assert config.mic_mode == "toggle"
    dialog.deleteLater()
    app.processEvents()


def test_settings_dialog_warns_about_a_conflicting_key(app):
    from dota_voice.desktop.settings_dialog import SettingsDialog

    dialog = SettingsDialog(AppConfig(push_to_talk_key="q", toggle_key="q"))
    assert "Dota 2" in dialog.mic_warning.text()
    dialog.deleteLater()
    app.processEvents()
