"""Main control window.

A native window (no browser) showing the live state of every active match: the
two team channels, who is speaking, who is muted, plus quick controls to mute,
move or end a session. It also hosts push-to-talk and the settings/overlay
toggles. State is refreshed on a timer from the embedded engine.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..appconfig import AppConfig
from ..voice.mic import MicMode
from .api import EngineAPI
from .settings_dialog import SettingsDialog

logger = logging.getLogger(__name__)

REFRESH_MS = 1000


class PlayerRow(QWidget):
    """One participant: name, hero, speaking indicator, mute / move buttons."""

    mute_toggled = Signal(str, bool)
    move_requested = Signal(str, str)

    def __init__(self, player: dict, match_id: str) -> None:
        super().__init__()
        self.match_id = match_id
        self.player_id = player["player_id"]
        self.team = player["team"]

        row = QHBoxLayout(self)
        row.setContentsMargins(6, 2, 6, 2)

        self.name = QLabel(player.get("name") or self.player_id)
        self.name.setMinimumWidth(140)
        hero = player.get("hero") or ""
        self.hero = QLabel(hero)
        self.hero.setStyleSheet("color: #8a93a2;")
        self.hero.setMinimumWidth(120)

        self.speaking = QLabel("●")
        self.speaking.setStyleSheet("color: #3a3f4b; font-size: 16px;")
        self.speaking.setToolTip("Говорит")
        self.speaking.setFixedWidth(18)

        self.mute_btn = QPushButton()
        self.mute_btn.setFixedWidth(130)
        self.mute_btn.clicked.connect(self._toggle_mute)

        self.move_btn = QPushButton("⇄ команда")
        self.move_btn.setFixedWidth(120)
        self.move_btn.clicked.connect(self._move)

        row.addWidget(self.speaking)
        row.addWidget(self.name)
        row.addWidget(self.hero)
        row.addStretch(1)
        row.addWidget(self.mute_btn)
        row.addWidget(self.move_btn)

        self.update_state(player)

    def update_state(self, player: dict) -> None:
        self.muted = bool(player.get("muted"))
        self.mute_btn.setText("включить звук" if self.muted else "заглушить")
        if player.get("speaking"):
            self.speaking.setStyleSheet("color: #ffd166; font-size: 16px;")
        else:
            self.speaking.setStyleSheet("color: #3a3f4b; font-size: 16px;")
        if self.muted:
            self.name.setStyleSheet("color: #8a93a2; text-decoration: line-through;")
        else:
            self.name.setStyleSheet("")

    def _toggle_mute(self) -> None:
        self.mute_toggled.emit(self.player_id, not self.muted)

    def _move(self) -> None:
        target = "dire" if self.team == "radiant" else "radiant"
        self.move_requested.emit(self.player_id, target)


class MatchPanel(QGroupBox):
    def __init__(self, session: dict, api: EngineAPI, config: AppConfig) -> None:
        super().__init__()
        self.api = api
        self.config = config
        self.match_id = session["match_id"]
        self.rows: dict[str, PlayerRow] = {}

        self.setTitle(self._title(session))
        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        self.phase = QLabel()
        header.addWidget(self.phase)
        header.addStretch(1)
        end_btn = QPushButton("Завершить сессию")
        end_btn.clicked.connect(self._end)
        header.addWidget(end_btn)
        layout.addLayout(header)

        for team, label in (("radiant", "Силы Света"), ("dire", "Силы Тьмы")):
            group = QGroupBox(label)
            group_layout = QVBoxLayout(group)
            for player in session.get(team, []):
                row = PlayerRow(player, self.match_id)
                row.mute_toggled.connect(self.api.set_mute.__get__(self.api))
                row.move_requested.connect(self.api.move.__get__(self.api))
                self.rows[player["player_id"]] = row
                group_layout.addWidget(row)
            if not session.get(team):
                group_layout.addWidget(QLabel("— нет игроков —"))
            layout.addWidget(group)

        self.refresh(session)

    def _title(self, session: dict) -> str:
        return f"Матч {session['match_id']}"

    def refresh(self, session: dict) -> None:
        phase_labels = {
            "none": "нет матча",
            "pre_game": "подготовка",
            "playing": "идёт игра",
            "ended": "матч завершён",
        }
        reconnect = session.get("reconnects", 0)
        extra = f" · переподключений: {reconnect}" if reconnect else ""
        self.phase.setText(
            f"Фаза: {phase_labels.get(session.get('phase'), session.get('phase'))}{extra}"
        )
        for team in ("radiant", "dire"):
            for player in session.get(team, []):
                row = self.rows.get(player["player_id"])
                if row is not None:
                    row.update_state(player)

    def _end(self) -> None:
        if QMessageBox.question(
            self, "DotaVoice", f"Завершить сессию матча {self.match_id}?"
        ) == QMessageBox.Yes:
            self.api.end_session(self.match_id)


class MainWindow(QMainWindow):
    def __init__(self, api: EngineAPI, config: AppConfig) -> None:
        super().__init__()
        self.api = api
        self.config = config
        self.setWindowTitle("DotaVoice — Command Voice Bridge")
        self.resize(820, 640)

        self._panels: dict[str, MatchPanel] = {}

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        self.status_line = QLabel("Запуск…")
        self.status_line.setWordWrap(True)
        outer.addWidget(self.status_line)

        controls = QHBoxLayout()

        self.mic_label = QLabel()
        controls.addWidget(self.mic_label)

        self.mic_combo = QComboBox()
        for mode in (MicMode.CONTINUOUS, MicMode.PUSH_TO_TALK, MicMode.TOGGLE):
            self.mic_combo.addItem(mode.label, mode.value)
        self.mic_combo.currentIndexChanged.connect(self._on_mic_mode_changed)
        controls.addWidget(self.mic_combo)

        self.talk_btn = QPushButton("Удерживайте, чтобы говорить")
        self.talk_btn.setCheckable(True)
        self.talk_btn.setToolTip("Нажмите и удерживайте (или используйте клавишу)")
        self.talk_btn.pressed.connect(lambda: self.api.mic_key("down"))
        self.talk_btn.released.connect(lambda: self.api.mic_key("up"))
        controls.addWidget(self.talk_btn)

        self.overlay_btn = QPushButton("Показать оверлей")
        self.overlay_btn.setCheckable(True)
        self.overlay_btn.setChecked(self.config.overlay_enabled)
        controls.addWidget(self.overlay_btn)

        settings_btn = QPushButton("Настройки…")
        controls.addWidget(settings_btn)
        outer.addLayout(controls)

        self.invite_line = QLabel()
        self.invite_line.setWordWrap(True)
        outer.addWidget(self.invite_line)

        invite_row = QHBoxLayout()
        self.invite_btn = QPushButton("Скопировать приглашение в комнату")
        self.invite_btn.clicked.connect(self._copy_invite)
        invite_row.addWidget(self.invite_btn)
        invite_row.addStretch(1)
        outer.addLayout(invite_row)

        self.tabs = QTabWidget()
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.addStretch(1)
        self.scroll.setWidget(self.scroll_content)
        self.tabs.addTab(self.scroll, "Матчи")
        outer.addWidget(self.tabs, 1)

        self.setStatusBar(QStatusBar())
        settings_btn.clicked.connect(self._open_settings)

        # In-app shortcut: a fallback for when global hotkeys are unavailable
        # (no X11, or the key is already claimed). The engine also polls the key
        # globally, so this can double-fire - harmless, because the controller
        # ignores a transition to the state it is already in.
        self._shortcut = QShortcut(QKeySequence(config.push_to_talk_key or "v"), self)
        self._shortcut.setContext(Qt.ApplicationShortcut)
        self._shortcut.activated.connect(self._hotkey_pressed)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)
        self.refresh()

    # ------------------------------------------------------------------

    def set_overlay_controller(self, toggle) -> None:
        """Wire the overlay window in without the window importing it."""
        self._toggle_overlay = toggle
        self.overlay_btn.toggled.connect(self._on_overlay_toggled)

    def set_overlay(self, overlay) -> None:
        """Hold a reference to the overlay window for position resets."""
        self._overlay = overlay

    def _on_overlay_toggled(self, checked: bool) -> None:
        self.config.overlay_enabled = checked
        self.config.save()
        if hasattr(self, "_toggle_overlay"):
            self._toggle_overlay(checked)

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.config, self, overlay=getattr(self, "_overlay", None))
        if dialog.exec():
            self.api.set_mic_keys(
                push_to_talk_key=self.config.push_to_talk_key,
                toggle_key=self.config.toggle_key,
                click_feedback=self.config.mic_click_feedback,
            )
            QMessageBox.information(
                self,
                "DotaVoice",
                "Настройки сохранены. Перезапустите приложение, чтобы применить "
                "изменения порта и голосового сервера.",
            )

    # ------------------------------------------------------------------

    def _hotkey_pressed(self) -> None:
        """In-app fallback for the global hotkey."""
        self.api.mic_key("press")

    def _on_mic_mode_changed(self) -> None:
        mode = self.mic_combo.currentData()
        if not mode:
            return
        self.config.mic_mode = mode
        self.config.save()
        self.api.set_mic_mode(mode)

    def _reset_overlay_position(self) -> None:
        overlay = getattr(self, "_overlay", None)
        if overlay is not None:
            overlay.reset_position()

    # ------------------------------------------------------------------

    def _invite_for(self, sessions: list[dict]) -> str:
        """Read the active match's invite without touching the clipboard.

        The invite already travels with the session snapshot, so the periodic
        refresh must not call the copy endpoint - that would overwrite whatever
        the player has on their clipboard every few seconds.
        """
        p2wlan = self.api.p2wlan()
        rooms = p2wlan.get("rooms") or {}
        for session in sessions:
            match_id = session["match_id"]
            invite = session.get("invite_key") or rooms.get(match_id, {}).get(
                "invite_key", ""
            )
            if invite:
                return f"Приглашение в комнату: {invite}"
        return ""

    def _copy_invite(self) -> None:
        """Copy the active match's invite to the clipboard, on demand only."""
        sessions = self.api.sessions()
        for session in sessions:
            data = self.api.p2wlan_invite(session["match_id"])
            if data.get("invite_key"):
                QMessageBox.information(
                    self, "DotaVoice", data.get("message") or "Приглашение скопировано."
                )
                return
        QMessageBox.information(
            self, "DotaVoice", "Активной катки нет — приглашать пока некого."
        )

    @staticmethod
    def _local_name(session: dict) -> str:
        for team in ("radiant", "dire"):
            for player in session.get(team, []):
                if player.get("is_local"):
                    return player.get("player_id") or player.get("name") or "хост"
        return "хост"

    # ------------------------------------------------------------------

    def refresh(self) -> None:
        health = self.api.health()
        voice = self.api.voice()
        sessions = self.api.sessions()

        if not health:
            self.status_line.setText("Движок недоступен — проверьте журнал.")
            return
        audio = "звук активен" if voice.get("audio_available") else "без звука (только состояние)"
        joined = ", ".join(voice.get("joined_rooms", [])) or "—"
        self.status_line.setText(
            f"Голос: {health.get('voice_backend')} · {audio} · "
            f"подключено к комнатам: {joined} · "
            f"активных матчей: {health.get('sessions')} · "
            f"инструментов: {health.get('metrics', {}).get('tool_calls', 0)}"
        )

        self._refresh_mic()
        self._refresh_invite(sessions)

        seen = {s["match_id"] for s in sessions}
        for session in sessions:
            panel = self._panels.get(session["match_id"])
            if panel is None:
                panel = MatchPanel(session, self.api, self.config)
                self._panels[session["match_id"]] = panel
                self.scroll_layout.insertWidget(self.scroll_layout.count() - 1, panel)
            else:
                panel.refresh(session)
        for match_id in list(self._panels):
            if match_id not in seen:
                panel = self._panels.pop(match_id)
                panel.setParent(None)
                panel.deleteLater()

    def _refresh_mic(self) -> None:
        mic = self.api.mic()
        if not mic:
            return
        mode = mic.get("mode")
        index = self.mic_combo.findData(mode)
        if index >= 0 and index != self.mic_combo.currentIndex():
            self.mic_combo.blockSignals(True)
            self.mic_combo.setCurrentIndex(index)
            self.mic_combo.blockSignals(False)
        active = mic.get("active")
        color = mic.get("mode_color", "#888")
        dot = "●" if active else "○"
        self.mic_label.setText(f"<span style='color:{color}'>{dot}</span> Микрофон:")
        self.mic_label.setToolTip(mic.get("mode_hint", ""))
        if active:
            self.talk_btn.setText("Микрофон активен")
        else:
            self.talk_btn.setText("Микрофон выключен")

    def _refresh_invite(self, sessions: list[dict]) -> None:
        if not sessions:
            self.invite_line.setText("")
            return
        p2wlan = self.api.p2wlan()
        if not p2wlan.get("enabled"):
            self.invite_line.setText("")
            return
        message = self._invite_for(sessions)
        self.invite_line.setText(message)
