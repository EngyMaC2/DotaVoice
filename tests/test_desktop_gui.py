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
    # The chat hotkey is edited here and must round-trip to the config.
    dialog.chat_hotkey_edit.setText("t")
    dialog._save()
    assert config.display_name == "Новое имя"
    assert config.chat_hotkey == "t"
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


def test_settings_dialog_drops_discord_relay_and_sdr(app):
    """The ТЗ asks for the Discord, relay-address and Steam SDR controls gone.

    They described transports the app no longer offers, so they must not appear
    on the Voice tab at all.
    """
    from dota_voice.desktop.settings_dialog import SettingsDialog

    dialog = SettingsDialog(AppConfig())
    assert not hasattr(dialog, "discord_token")
    assert not hasattr(dialog, "discord_guild")
    assert not hasattr(dialog, "relay_edit")
    assert not hasattr(dialog, "steam_sdr")
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
    # Each side's group header counts who is in DotaVoice out of the whole team
    # ("connected/total"), so a full team reads "5/5" and a gap is visible.
    assert panel.groups["radiant"].title() == "Силы Света (1/2)"
    assert panel.groups["dire"].title() == "Силы Тьмы (0/1)"
    # The unknown-side group is visible, so those players are still shown.
    assert panel.groups["unknown"].isVisible() or panel.groups["unknown"].isHidden() is False
    panel.deleteLater()
    app.processEvents()


def test_player_row_shows_the_hero_and_team(app, engine):
    """The window row must read "[icon] Nick (Hero) · Side" (ТЗ addendum §1.1)."""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from dota_voice.desktop.main_window import MatchPanel

    session = {
        "match_id": "7123456789", "phase": "playing", "reconnects": 0,
        "radiant": [
            {"player_id": "101", "name": "EngyMaC²", "team": "radiant",
             "team_label": "Силы Света", "hero": "Lion", "hero_label": "Lion",
             "hero_icon": "https://example.invalid/lion.png",
             "is_local": True, "connected": True,
             "speaking": False, "muted": False, "deafened": False},
        ],
        "dire": [
            # No pick yet: the placeholder must show instead of a blank.
            {"player_id": "201", "name": "Bane", "team": "dire",
             "team_label": "Силы Тьмы", "hero": "", "hero_label": "Выбор…",
             "hero_icon": "", "is_local": False, "connected": False,
             "speaking": False, "muted": False, "deafened": False},
        ],
        "unknown": [],
    }
    panel = MatchPanel(session, engine, AppConfig(setup_complete=True))
    app.processEvents()

    lion = panel.rows["101"]
    assert "Lion" in lion.hero.text()
    assert "lion.png" in lion.hero.text()  # the portrait is embedded
    assert lion.team_label.text() == "Силы Света"

    waiting = panel.rows["201"]
    assert "Выбор…" in waiting.hero.text()
    assert "<img" not in waiting.hero.text()  # no portrait before a pick
    assert waiting.team_label.text() == "Силы Тьмы"
    panel.deleteLater()
    app.processEvents()


def test_a_player_reclassified_from_unknown_keeps_one_row(app, engine):
    """Re-grouping a player must reuse their row, not blank it.

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
        "radiant": [],
        "dire": [],
        "unknown": [player("101", "unknown", connected=False)],
    }
    panel = MatchPanel(session, engine, AppConfig(setup_complete=True))
    app.processEvents()
    row = panel.rows["101"]
    assert row.parent() is not None

    # The player's side is learned; refresh with the new grouping.
    session["unknown"] = []
    session["dire"] = [player("101", "dire")]
    panel.refresh(session)
    app.processEvents()

    assert panel.rows["101"] is row  # the same widget is reused
    assert row.parent() is not None  # and it is still in the tree
    assert row.team == "dire"
    panel.deleteLater()
    app.processEvents()


def test_the_panel_has_no_move_button(app, engine):
    """There is no manual team reshuffle control anywhere in the panel."""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtWidgets import QPushButton

    from dota_voice.desktop.main_window import MatchPanel

    def player(player_id, team, connected=True):
        return {"player_id": player_id, "name": player_id, "team": team,
                "hero": "", "is_local": False, "connected": connected,
                "speaking": False, "muted": False, "deafened": False}

    session = {
        "match_id": "7123456789", "phase": "playing", "reconnects": 0,
        "radiant": [player("101", "radiant")],
        "dire": [player("201", "dire")],
        "unknown": [],
    }
    panel = MatchPanel(session, engine, AppConfig(setup_complete=True))
    app.processEvents()
    texts = [
        button.text()
        for button in panel.findChildren(QPushButton)
    ]
    assert not any("команд" in text.lower() for text in texts), texts
    assert not hasattr(panel.rows["101"], "move_btn")
    panel.deleteLater()
    app.processEvents()


def _feed_gsi(engine, match_id="7123456789", game_state=None):
    """Push one GSI tick through the engine, exactly as Dota 2 would."""
    from scripts.simulate import payload

    # AppConfig's default token is what the engine fixture was started with.
    body = payload(match_id, token="dotavoice")
    if game_state:
        body["map"]["game_state"] = game_state
    engine._client.post("/gsi", json=body)


def test_the_panel_shows_the_team_chat(app, engine):
    """The native window must expose the same team chat as the web panel."""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from dota_voice.desktop.main_window import MatchPanel

    _feed_gsi(engine)
    session = next(s for s in engine.sessions() if s["match_id"] == "7123456789")
    panel = MatchPanel(session, engine, AppConfig(setup_complete=True))
    app.processEvents()

    # The local player is Radiant, so the panel offers the Radiant room.
    assert panel.chat.tabs.count() == 1
    assert panel.chat._active() == "radiant"

    panel.chat.input.setText("push top")
    panel.chat._send()
    app.processEvents()
    assert "push top" in panel.chat.log.toPlainText()
    panel.deleteLater()
    app.processEvents()


def test_the_panel_switches_to_the_post_game_room(app, engine):
    """Once the match ends the side room is replaced by the shared room."""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from dota_voice.desktop.main_window import MatchPanel

    _feed_gsi(engine)
    _feed_gsi(engine, game_state="DOTA_GAMERULES_STATE_POST_GAME")
    session = next(s for s in engine.sessions() if s["match_id"] == "7123456789")
    panel = MatchPanel(session, engine, AppConfig(setup_complete=True))
    app.processEvents()

    assert panel.chat._active() == "post_game"
    # isHidden() reflects the panel's own show/hide, independent of whether the
    # whole window is on screen (the test never shows it).
    assert not panel.chat.leave_btn.isHidden()
    panel.deleteLater()
    app.processEvents()


def test_player_row_shows_a_readable_name_not_a_steam_id(app):
    """A teammate's GSI name is a bare SteamID64; the row must show "Игрок N"."""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from dota_voice.desktop.main_window import PlayerRow

    row = PlayerRow(
        {
            "player_id": "76561197960000002",
            "name": "76561197960000002",
            "display_name": "Игрок 2",
            "team": "radiant",
            "team_label": "Силы Света",
            "connected": False,
        },
        "7123456789",
    )
    assert row.name.text() == "Игрок 2"
    # When the real name arrives later the label updates in place.
    row.update_state(
        {
            "player_id": "76561197960000002",
            "name": "Frost",
            "display_name": "Frost",
            "team": "radiant",
            "team_label": "Силы Света",
            "connected": True,
        }
    )
    assert row.name.text() == "Frost"
    row.deleteLater()
    app.processEvents()


def test_panel_labels_every_phase_in_russian(app, engine):
    """The native window must not print a raw English phase key during the draft."""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from dota_voice.desktop.main_window import MatchPanel

    _feed_gsi(engine)
    _feed_gsi(engine, game_state="DOTA_GAMERULES_STATE_HERO_SELECTION")
    session = next(s for s in engine.sessions() if s["match_id"] == "7123456789")
    panel = MatchPanel(session, engine, AppConfig(setup_complete=True))
    app.processEvents()

    assert "выбор героев" in panel.phase.text()
    assert "hero_selection" not in panel.phase.text()
    panel.deleteLater()
    app.processEvents()


def test_the_panel_shows_all_ten_players_when_only_one_is_online(app, engine):
    """The window must draw the whole match, not only the connected players.

    A spectator's GSI lists all ten; the reported bug was that everyone without
    DotaVoice running was hidden, so only the local player (plus voice peers)
    appeared. All ten rows must be present, with only the local one "в сети".
    """
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from dota_voice.desktop.main_window import MatchPanel

    from scripts.simulate import DIRE, RADIANT, payload

    radiant = RADIANT + [
        {"account_id": 104, "name": "Tide", "hero_name": "npc_dota_hero_tidehunter", "hero_id": 29},
        {"account_id": 105, "name": "Lina", "hero_name": "npc_dota_hero_lina", "hero_id": 5},
    ]
    dire = DIRE + [
        {"account_id": 204, "name": "Pudge", "hero_name": "npc_dota_hero_pudge", "hero_id": 14},
        {"account_id": 205, "name": "Sniper", "hero_name": "npc_dota_hero_sniper", "hero_id": 35},
    ]
    engine._client.post(
        "/gsi",
        json=payload("7123456789", token="dotavoice", radiant=radiant, dire=dire),
    )
    session = next(s for s in engine.sessions() if s["match_id"] == "7123456789")
    panel = MatchPanel(session, engine, AppConfig(setup_complete=True))
    app.processEvents()

    # Every one of the ten players has a row, across both sides.
    assert len(panel.rows) == 10
    assert len(session["radiant"]) == 5
    assert len(session["dire"]) == 5
    # Only the local player is connected; the rest are shown but offline.
    online = [pid for pid, row in panel.rows.items() if row.connected]
    assert online == ["101"]
    for pid, row in panel.rows.items():
        assert row.isVisible() or row.isHidden() is False
        assert "не в сети" in row.status.text() if pid != "101" else "в сети" in row.status.text()
    # The team headers count "in DotaVoice / in the match": 1 of 5 on each side.
    assert panel.groups["radiant"].title() == "Силы Света (1/5)"
    assert panel.groups["dire"].title() == "Силы Тьмы (0/5)"
    panel.deleteLater()
    app.processEvents()


def test_the_main_window_has_no_invite_controls(app, engine):
    """The manual invite system was removed; nothing invite-related may remain."""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtWidgets import QLabel, QPushButton

    from dota_voice.desktop.main_window import MainWindow

    window = MainWindow(engine, AppConfig(setup_complete=True))
    window.refresh()
    app.processEvents()

    assert not hasattr(window, "invite_line")
    assert not hasattr(window, "invite_btn")
    texts = [w.text() for w in window.findChildren(QLabel)]
    texts += [w.text() for w in window.findChildren(QPushButton)]
    assert not any("Приглашение" in t or "приглашение" in t for t in texts)
    window.deleteLater()
    app.processEvents()
