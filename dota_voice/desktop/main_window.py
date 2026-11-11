"""Main control window.

A native window (no browser) showing the live state of every active match: the
two team channels, who is speaking, who is muted, plus quick controls to mute,
move or end a session. It also hosts push-to-talk and the settings/overlay
toggles. State is refreshed on a timer from the embedded engine.
"""

from __future__ import annotations

import logging
from html import escape

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStatusBar,
    QTabBar,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..appconfig import AppConfig
from ..models import MatchPhase
from ..voice.mic import MicMode
from .api import EngineAPI
from .settings_dialog import SettingsDialog

logger = logging.getLogger(__name__)

REFRESH_MS = 1000


class PlayerRow(QWidget):
    """One participant: name, hero, connection status, speaking, mute."""

    mute_toggled = Signal(str, bool)

    def __init__(self, player: dict, match_id: str) -> None:
        super().__init__()
        self.match_id = match_id
        self.player_id = player["player_id"]
        self.team = player["team"]

        row = QHBoxLayout(self)
        row.setContentsMargins(6, 2, 6, 2)

        self.name = QLabel(
            player.get("display_name") or player.get("name") or self.player_id
        )
        self.name.setMinimumWidth(140)
        # Hero portrait plus its name, so the row reads "[icon] EngyMaC² (Lion)".
        # The portrait is a remote image; when it is not there yet (no pick) the
        # label falls back to the plain "Выбор…" text.
        self.hero = QLabel()
        self.hero.setStyleSheet("color: #8a93a2;")
        self.hero.setTextFormat(Qt.RichText)
        self.hero.setMinimumWidth(150)
        # The side is spelled out beside the name as well as in the group header,
        # so a row is unambiguous if it is ever shown on its own.
        self.team_label = QLabel()
        self.team_label.setStyleSheet("color: #8a93a2; font-size: 11px;")
        self.team_label.setFixedWidth(90)

        # Connection to DotaVoice: a green dot for players on the network, a
        # hollow one for players we only see in the game. This is what answers
        # "is the app actually working and who is connected?".
        self.status = QLabel()
        self.status.setFixedWidth(90)

        self.speaking = QLabel("●")
        self.speaking.setStyleSheet("color: #3a3f4b; font-size: 16px;")
        self.speaking.setToolTip("Говорит")
        self.speaking.setFixedWidth(18)

        self.mute_btn = QPushButton()
        self.mute_btn.setFixedWidth(130)
        self.mute_btn.clicked.connect(self._toggle_mute)

        row.addWidget(self.speaking)
        row.addWidget(self.name)
        row.addWidget(self.hero)
        row.addWidget(self.team_label)
        row.addWidget(self.status)
        row.addStretch(1)
        row.addWidget(self.mute_btn)

        self.update_state(player)

    def update_state(self, player: dict) -> None:
        self.team = player.get("team", self.team)
        self.muted = bool(player.get("muted"))
        self.connected = bool(player.get("connected"))
        # The display name can fill in after the row is first drawn (a teammate's
        # real name arrives with the mesh, or a slot is assigned later), so keep
        # the label in sync instead of freezing the placeholder.
        display = player.get("display_name") or player.get("name") or self.player_id
        if self.name.text() != display:
            self.name.setText(display)
        self.mute_btn.setText("включить звук" if self.muted else "заглушить")
        self._render_hero(player)
        team_name = player.get("team_label") or ""
        self.team_label.setText(team_name)
        if self.connected:
            self.status.setText("● в сети")
            self.status.setStyleSheet("color: #7ed957; font-size: 12px;")
            self.status.setToolTip("DotaVoice запущен и подключён")
        else:
            self.status.setText("○ не в сети")
            self.status.setStyleSheet("color: #8a93a2; font-size: 12px;")
            self.status.setToolTip("DotaVoice не запущен — голос недоступен")
        if player.get("speaking"):
            self.speaking.setStyleSheet("color: #ffd166; font-size: 16px;")
        else:
            self.speaking.setStyleSheet("color: #3a3f4b; font-size: 16px;")
        # Grey out everyone who is not on the network, so the roster reads as a
        # whole at a glance.
        self.setStyleSheet("" if self.connected else "QLabel { color: #8a93a2; }")
        if self.muted:
            self.name.setStyleSheet("color: #8a93a2; text-decoration: line-through;")
        elif not self.connected:
            self.name.setStyleSheet("color: #8a93a2;")
        else:
            self.name.setStyleSheet("")
        # The mute button is meaningless for a player who is not on the network.
        self.mute_btn.setEnabled(self.connected)

    def _toggle_mute(self) -> None:
        self.mute_toggled.emit(self.player_id, not self.muted)

    def _render_hero(self, player: dict) -> None:
        """Draw the hero as a portrait plus its name, or "Выбор…" before a pick.

        The portrait is a remote Dota CDN image; Qt loads it asynchronously and
        simply shows the name if it cannot (offline, or no pick yet), so the row
        is always readable.
        """
        name = player.get("hero_label") or player.get("hero") or "Выбор…"
        icon = player.get("hero_icon") or ""
        if icon:
            html = f'<img src="{escape(icon)}" width="26" height="15"> {escape(name)}'
        else:
            html = f'<span style="color:#5a6472">—</span> {escape(name)}'
        if self.hero.text() != html:
            self.hero.setText(html)


class ChatPanel(QGroupBox):
    """The team chat: a private room per side, plus the shared post-match room.

    The server owns every access rule; this panel only reflects what it is
    allowed to see. During a match that is the local side's own room; once the
    match ends the side room is gone and the shared "После матча" room appears
    in its place (ТЗ §8.3, §8.4).
    """

    def __init__(self, match_id: str, api: EngineAPI, player_id: str) -> None:
        super().__init__("Чат")
        self.api = api
        self.match_id = match_id
        self.player_id = player_id
        self._channels: list[str] = []
        # Signature of what is on screen, so the periodic refresh does not
        # repaint the log (and lose the scroll position) when nothing changed.
        self._sig = ""

        layout = QVBoxLayout(self)

        self.tabs = QTabBar()
        self.tabs.setExpanding(False)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(90)
        layout.addWidget(self.log)

        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.returnPressed.connect(self._send)
        row.addWidget(self.input)
        self.send_btn = QPushButton("Отправить")
        self.send_btn.clicked.connect(self._send)
        row.addWidget(self.send_btn)
        # The post-match room is left on demand; the window then closes once
        # everyone has left (or its timer runs out).
        self.leave_btn = QPushButton("Выйти")
        self.leave_btn.clicked.connect(self._leave)
        self.leave_btn.setVisible(False)
        row.addWidget(self.leave_btn)
        layout.addLayout(row)

    def refresh(self, payload: dict) -> None:
        channels = payload.get("channels") or []
        enabled = payload.get("enabled", True)
        values = [c.get("value") for c in channels]
        if values != self._channels:
            self._rebuild_tabs(channels)
        self.setTitle(self._title(channels))
        self.setVisible(bool(channels) and enabled)
        if not channels:
            return
        active = self._active()
        history = (payload.get("history") or {}).get(active, [])
        sig = f"{active}:{len(history)}:" + (history[-1].get("id", "") if history else "")
        if sig != self._sig:
            self._sig = sig
            self.log.setHtml(self._render(history))
            bar = self.log.verticalScrollBar()
            bar.setValue(bar.maximum())
        self.input.setPlaceholderText(
            "Сообщение обеим командам…"
            if active == "post_game"
            else "Сообщение команде…"
        )
        self.leave_btn.setVisible(active == "post_game")

    def _rebuild_tabs(self, channels: list[dict]) -> None:
        self.tabs.blockSignals(True)
        while self.tabs.count():
            self.tabs.removeTab(0)
        self._channels = [c.get("value", "") for c in channels]
        for channel in channels:
            self.tabs.addTab(channel.get("label", ""))
        self.tabs.blockSignals(False)
        self._sig = ""  # a new channel set always repaints

    def _on_tab_changed(self, _index: int) -> None:
        # Switching rooms is local; the next refresh paints the new one.
        self._sig = ""

    def _active(self) -> str:
        index = self.tabs.currentIndex()
        if 0 <= index < len(self._channels):
            return self._channels[index]
        return self._channels[0] if self._channels else ""

    @staticmethod
    def _title(channels: list[dict]) -> str:
        if any(c.get("value") == "post_game" for c in channels):
            return "Чат · После матча"
        return "Чат команды"

    @staticmethod
    def _render(history: list[dict]) -> str:
        if not history:
            return '<div style="color:#8a93a2">Сообщений пока нет.</div>'
        colors = {"radiant": "#5aa9ff", "dire": "#ff6b6b"}
        parts = []
        for message in history:
            if message.get("channel") == "post_game":
                color = "#ffd166"
            else:
                color = colors.get(message.get("team", ""), "#c9d1d9")
            author = escape(message.get("author_name") or message.get("author_id") or "")
            text = escape(message.get("text") or "")
            parts.append(
                f'<div><span style="color:{color}">{author}:</span> {text}</div>'
            )
        return "".join(parts)

    def _send(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.api.send_chat(self.match_id, text, self._active())
        self.input.clear()
        self.refresh(self.api.chat(self.match_id))

    def _leave(self) -> None:
        self.api.leave_post_game(self.match_id, self.player_id)


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

        # The team groups are rebuilt on every refresh: teammates appear as
        # their apps connect, and a player can move between sides, so a panel
        # built once at creation would go stale.
        self.groups: dict[str, QGroupBox] = {}
        # The base title is kept so the count can be re-appended on every
        # refresh without piling up "… (5/5) (5/5)".
        self.group_titles: dict[str, str] = {}
        for team, label in (
            ("radiant", "Силы Света"),
            ("dire", "Силы Тьмы"),
            ("unknown", "Не подключены к DotaVoice"),
        ):
            group = QGroupBox(label)
            QVBoxLayout(group)
            group.setVisible(False)
            self.groups[team] = group
            self.group_titles[team] = label
            layout.addWidget(group)

        self.chat = ChatPanel(self.match_id, api, self._local_player_id(session))
        layout.addWidget(self.chat)

        self.refresh(session)

    def _local_player_id(self, session: dict) -> str:
        for team in ("radiant", "dire", "unknown"):
            for player in session.get(team, []):
                if player.get("is_local"):
                    return player.get("player_id") or ""
        return ""

    def _title(self, session: dict) -> str:
        return f"Матч {session['match_id']}"

    def _rebuild_group(self, team: str, players: list[dict]) -> None:
        group = self.groups[team]
        group_layout = group.layout()
        # Detach the old rows without deleting them: a row is reused when its
        # player is re-grouped (e.g. classified from unknown to a side), so
        # deleting it here would make it vanish after the change.
        while group_layout.count():
            group_layout.takeAt(0)
        for player in players:
            row = self.rows.get(player["player_id"])
            if row is None:
                row = PlayerRow(player, self.match_id)
                row.mute_toggled.connect(self.api.set_mute.__get__(self.api))
                self.rows[player["player_id"]] = row
            row.update_state(player)
            group_layout.addWidget(row)
        group.setVisible(bool(players))

    def refresh(self, session: dict) -> None:
        reconnect = session.get("reconnects", 0)
        extra = f" · переподключений: {reconnect}" if reconnect else ""
        connected = sum(
            1
            for team in ("radiant", "dire", "unknown")
            for player in session.get(team, [])
            if player.get("connected")
        )
        total = sum(
            len(session.get(team, [])) for team in ("radiant", "dire", "unknown")
        )
        phase = session.get("phase", "")
        try:
            phase_label = MatchPhase(phase).label
        except ValueError:
            phase_label = phase
        self.phase.setText(
            f"Фаза: {phase_label}"
            f" · подключено к DotaVoice: {connected} из {total}{extra}"
        )
        # Drop rows for players who are gone from the whole match, so the dict
        # does not grow without bound.
        live_ids = {
            player["player_id"]
            for team in ("radiant", "dire", "unknown")
            for player in session.get(team, [])
        }
        for player_id in list(self.rows):
            if player_id not in live_ids:
                row = self.rows.pop(player_id)
                row.setParent(None)
                row.deleteLater()
        for team in ("radiant", "dire", "unknown"):
            players = session.get(team, [])
            # The group header shows how much of the game's roster the app
            # reaches, per side - "Силы Света (5/5)" - matching the control
            # panel, so the count reflects GSI, not merely the rows drawn.
            connected = sum(1 for p in players if p.get("connected"))
            self.groups[team].setTitle(
                f"{self.group_titles[team]} ({connected}/{len(players)})"
            )
            self._rebuild_group(team, players)
        self.chat.refresh(self.api.chat(self.match_id))

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

        # The talk button does whatever the current mode means:
        #   continuous -> it is the mute toggle,
        #   push-to-talk -> hold it to talk,
        #   toggle -> press once to start, again to stop.
        self.talk_btn = QPushButton("Удерживайте, чтобы говорить")
        self.talk_btn.setCheckable(True)
        self.talk_btn.pressed.connect(self._on_talk_pressed)
        self.talk_btn.released.connect(self._on_talk_released)
        controls.addWidget(self.talk_btn)

        # A hard self-mute that means the same thing in every mode.
        self.mute_btn = QPushButton("Заглушить микрофон")
        self.mute_btn.setCheckable(True)
        self.mute_btn.clicked.connect(self._on_self_mute_clicked)
        controls.addWidget(self.mute_btn)

        self.overlay_btn = QPushButton("Показать оверлей")
        self.overlay_btn.setCheckable(True)
        self.overlay_btn.setChecked(self.config.overlay_enabled)
        controls.addWidget(self.overlay_btn)

        self.hotkeys_btn = QPushButton("Горячие клавиши")
        self.hotkeys_btn.clicked.connect(self._show_hotkeys)
        controls.addWidget(self.hotkeys_btn)

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
            # Apply everything the settings dialog can change live, so a toggle
            # in the dialog actually does something instead of waiting for a
            # restart that may never come.
            self.api.set_mic_keys(
                push_to_talk_key=self.config.push_to_talk_key,
                toggle_key=self.config.toggle_key,
                click_feedback=self.config.mic_click_feedback,
            )
            self.api.set_mic_mode(self.config.mic_mode)
            overlay = getattr(self, "_overlay", None)
            if overlay is not None:
                overlay.set_position(self.config.overlay_position)
                overlay.set_opacity(self.config.overlay_opacity)
                if self.config.overlay_enabled:
                    overlay.show_overlay()
                else:
                    overlay.hide()
            self.overlay_btn.blockSignals(True)
            self.overlay_btn.setChecked(self.config.overlay_enabled)
            self.overlay_btn.blockSignals(False)
            QMessageBox.information(
                self,
                "DotaVoice",
                "Настройки сохранены. Изменения микрофона и оверлея применены "
                "сразу; порт и голосовой сервер начнут действовать после "
                "перезапуска.",
            )

    # ------------------------------------------------------------------

    def _hotkey_pressed(self) -> None:
        """In-app fallback for the global hotkey."""
        self.api.mic_key("press")

    def _on_talk_pressed(self) -> None:
        """Talk button: the meaning follows the current microphone mode.

        In push-to-talk it starts transmitting while held; in toggle it flips
        the state; in continuous mode there is nothing to hold, so it acts as
        the mute toggle instead of doing nothing.
        """
        mode = self.mic_combo.currentData()
        if mode == "continuous":
            self.api.toggle_self_mute()
            return
        self.api.mic_key("down")

    def _on_talk_released(self) -> None:
        mode = self.mic_combo.currentData()
        if mode == "push_to_talk":
            self.api.mic_key("up")

    def _on_self_mute_clicked(self) -> None:
        self.api.toggle_self_mute()

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

    def _show_hotkeys(self) -> None:
        """Explain every key and overlay gesture in one place.

        The overlay can be moved only by grabbing the tiny ⠿ handle or by
        holding Ctrl, which is impossible to discover by accident - so this
        dialog spells it out, together with the current push-to-talk keys.
        """
        ptt = (self.config.push_to_talk_key or "v").upper()
        toggle = (self.config.toggle_key or "v").upper()
        chat = (self.config.chat_hotkey or "enter").upper()
        QMessageBox.information(
            self,
            "Горячие клавиши DotaVoice",
            "<b>Оверлей</b><br>"
            "• Удерживайте значок ⠿ и потяните, чтобы перетащить оверлей.<br>"
            "• Либо зажмите <b>Ctrl</b> и потяните из любого места оверлея.<br>"
            "• Клик по значку микрофона — смена режима.<br>"
            "• Кнопка «Заглушить» — мгновенный самомьют.<br>"
            f"• Клавиша <b>{chat}</b> — открыть чат команды прямо в оверлее.<br><br>"
            "<b>Микрофон</b><br>"
            f"• Push-to-talk: клавиша <b>{ptt}</b> — говорите, пока держите.<br>"
            f"• Переключатель: клавиша <b>{toggle}</b> — вкл/выкл по нажатию.<br>"
            "• Клавиши меняются в «Настройки…» → раздел «Голос».<br><br>"
            "<b>Панель управления</b><br>"
            "• Режим «рация»: удерживайте выбранную клавишу, чтобы отметить себя "
            "как говорящего.",
        )

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
        self_muted = mic.get("self_muted")
        color = mic.get("mode_color", "#888")
        dot = "●" if active else "○"
        self.mic_label.setText(f"<span style='color:{color}'>{dot}</span> Микрофон:")
        self.mic_label.setToolTip(mic.get("mode_hint", ""))

        # The talk button's label and hint follow the mode, so it never lies
        # about what a click will do.
        if mode == "continuous":
            self.talk_btn.setText(
                "Заглушить себя" if not self_muted else "Включить себя"
            )
            self.talk_btn.setToolTip("Открытый микрофон: кнопка включает и выключает вас.")
        elif mode == "push_to_talk":
            self.talk_btn.setText("Удерживайте, чтобы говорить")
            self.talk_btn.setToolTip(
                f"Удерживайте кнопку или клавишу «{mic.get('hotkey', 'v')}»."
            )
        else:
            self.talk_btn.setText("Включить/выключить микрофон")
            self.talk_btn.setToolTip(
                f"Нажмите кнопку или клавишу «{mic.get('hotkey', 'v')}», чтобы включить и выключить."
            )

        self.mute_btn.blockSignals(True)
        self.mute_btn.setChecked(bool(self_muted))
        self.mute_btn.blockSignals(False)
        self.mute_btn.setText(
            "Включить микрофон" if self_muted else "Заглушить микрофон"
        )
