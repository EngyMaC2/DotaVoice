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


def test_main_window_talk_button_follows_the_mode(app, engine):
    """The talk button must never be a dead control in any mode."""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from dota_voice.desktop.main_window import MainWindow

    config = AppConfig(setup_complete=True)
    window = MainWindow(engine, config)
    window.refresh()
    # Push-to-talk is the default the settings dialog would set; the label must
    # describe holding the button, not a stale "mic is on/off".
    index = window.mic_combo.findData("push_to_talk")
    window.mic_combo.setCurrentIndex(index)
    window.refresh()
    assert "Удерживайте" in window.talk_btn.text()

    window.mic_combo.setCurrentIndex(window.mic_combo.findData("continuous"))
    window.refresh()
    assert "Заглушить" in window.talk_btn.text() or "Включить" in window.talk_btn.text()
    window.deleteLater()
    app.processEvents()


def test_main_window_self_mute_button_reflects_state(app, engine):
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from dota_voice.desktop.main_window import MainWindow

    window = MainWindow(engine, AppConfig(setup_complete=True))
    window.refresh()
    assert "Заглушить" in window.mute_btn.text()
    engine.toggle_self_mute()
    window.refresh()
    assert "Включить" in window.mute_btn.text()
    assert window.mute_btn.isChecked() is True
    engine.toggle_self_mute()
    window.deleteLater()
    app.processEvents()


def test_overlay_opacity_can_be_changed_and_persists(app, engine, tmp_path, monkeypatch):
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    monkeypatch.setenv("DOTAVOICE_CONFIG_DIR", str(tmp_path))
    from dota_voice.desktop.overlay_position import OverlaySettings, load
    from dota_voice.desktop.overlay_window import OverlayWindow

    overlay = OverlayWindow(engine.overlay_url(), settings=OverlaySettings())
    overlay.set_opacity(0.5)
    assert abs(overlay.windowOpacity() - 0.5) < 0.01
    assert abs(load(tmp_path / "overlay.json").opacity - 0.5) < 0.01
    overlay.deleteLater()
    app.processEvents()


def test_match_panel_lists_all_players_with_connection_status(app, engine):
    """The panel must show every player, their side and whether they are connected.

    This is what lets a user confirm the app works and see how Radiant/Dire are
    split, including players who have not connected their own DotaVoice.
    """
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from dota_voice.desktop.main_window import MatchPanel

    session = {
        "match_id": "7123456789",
        "phase": "playing",
        "reconnects": 0,
        "radiant": [
            {"player_id": "101", "name": "Aegis", "team": "radiant",
             "hero": "juggernaut", "is_local": True, "connected": True,
             "speaking": False, "muted": False, "deafened": False},
            {"player_id": "102", "name": "Frost", "team": "radiant",
             "hero": "", "is_local": False, "connected": False,
             "speaking": False, "muted": False, "deafened": False},
        ],
        "dire": [
            {"player_id": "201", "name": "Bane", "team": "dire",
             "hero": "", "is_local": False, "connected": False,
             "speaking": False, "muted": False, "deafened": False},
        ],
        "unknown": [
            {"player_id": "301", "name": "Mystery", "team": "unknown",
             "hero": "", "is_local": False, "connected": False,
             "speaking": False, "muted": False, "deafened": False},
        ],
    }
    panel = MatchPanel(session, engine, AppConfig(setup_complete=True))
    app.processEvents()

    # Every player has a row, across all three groups.
    assert set(panel.rows) == {"101", "102", "201", "301"}
    assert panel.rows["101"].connected is True
    assert panel.rows["102"].connected is False
    assert "в сети" in panel.rows["101"].status.text()
    assert "не в сети" in panel.rows["102"].status.text()
    # A player who is not connected cannot be muted.
    assert panel.rows["102"].mute_btn.isEnabled() is False
    assert panel.rows["101"].mute_btn.isEnabled() is True
    # The header summarises the connection count.
    assert "подключено к DotaVoice: 1 из 4" in panel.phase.text()
    # The unknown-side group is visible, so those players are still shown.
    assert panel.groups["unknown"].isVisible() or panel.groups["unknown"].isHidden() is False
    panel.deleteLater()
    app.processEvents()


def test_a_player_moving_teams_keeps_one_row(app, engine):
    """Moving a player between sides must reuse their row, not blank it.

    The panel is rebuilt on every refresh; a naive rebuild deletes the old row
    and the deferred delete then wipes the row that was just re-added, leaving a
    gap. This guards against that.
    """
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from dota_voice.desktop.main_window import MatchPanel

    def player(player_id, team, connected=True):
        return {"player_id": player_id, "name": player_id, "team": team,
                "hero": "", "is_local": False, "connected": connected,
                "speaking": False, "muted": False, "deafened": False}

    session = {
        "match_id": "7123456789", "phase": "playing", "reconnects": 0,
        "radiant": [player("101", "radiant")],
        "dire": [],
        "unknown": [],
    }
    panel = MatchPanel(session, engine, AppConfig(setup_complete=True))
    app.processEvents()
    row = panel.rows["101"]
    assert row.parent() is not None

    # The player switches sides; refresh with the new assignment.
    session["radiant"] = []
    session["dire"] = [player("101", "dire")]
    panel.refresh(session)
    app.processEvents()

    assert panel.rows["101"] is row  # the same widget is reused
    assert row.parent() is not None  # and it is still in the tree
    assert row.team == "dire"
    panel.deleteLater()
    app.processEvents()
