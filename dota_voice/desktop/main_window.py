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
        self.ptt_btn = QPushButton("Удерживайте для передачи (push-to-talk)")
        self.ptt_btn.setCheckable(True)
        self.ptt_btn.setToolTip("Нажмите и удерживайте, чтобы говорить")
        self.ptt_btn.pressed.connect(lambda: self._set_transmitting(True))
        self.ptt_btn.released.connect(lambda: self._set_transmitting(False))
        controls.addWidget(self.ptt_btn)

        self.overlay_btn = QPushButton("Показать оверлей")
        self.overlay_btn.setCheckable(True)
        self.overlay_btn.setChecked(self.config.overlay_enabled)
        controls.addWidget(self.overlay_btn)

        settings_btn = QPushButton("Настройки…")
        controls.addWidget(settings_btn)
        outer.addLayout(controls)

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

        self._shortcut = QShortcut(QKeySequence(config.push_to_talk_key or "v"), self)
        self._shortcut.setContext(Qt.ApplicationShortcut)
        self._shortcut.activated.connect(self._ptt_hotkey)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)
        self.refresh()

    # ------------------------------------------------------------------

    def set_overlay_controller(self, toggle) -> None:
        """Wire the overlay window in without the window importing it."""
        self._toggle_overlay = toggle
        self.overlay_btn.toggled.connect(self._on_overlay_toggled)

    def _on_overlay_toggled(self, checked: bool) -> None:
        self.config.overlay_enabled = checked
        self.config.save()
        if hasattr(self, "_toggle_overlay"):
            self._toggle_overlay(checked)

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.config, self)
        if dialog.exec():
            QMessageBox.information(
                self,
                "DotaVoice",
                "Настройки сохранены. Перезапустите приложение, чтобы применить "
                "изменения порта и голосового сервера.",
            )

    # ------------------------------------------------------------------

    def _set_transmitting(self, active: bool) -> None:
        # Send the microphone into the match rooms (real push-to-talk)...
        self.api.transmit(active)
        # ...and mark the local player as speaking for the overlay.
        for session in self.api.sessions():
            name = self._local_name(session)
            self.api.speaking(session["match_id"], name, active)
        self.ptt_btn.setText(
            "Говорите…" if active else "Удерживайте для передачи (push-to-talk)"
        )

    def _ptt_hotkey(self) -> None:
        self.ptt_btn.setChecked(not self.ptt_btn.isChecked())
        self._set_transmitting(self.ptt_btn.isChecked())

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
