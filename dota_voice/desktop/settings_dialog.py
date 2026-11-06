"""Settings dialog.

Edits the same :class:`~dota_voice.appconfig.AppConfig` the wizard writes, plus
the Dota/Steam folders and the GSI install action. Kept intentionally plain so
it stays readable in Russian and easy to extend.
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import steam
from ..appconfig import AppConfig
from ..voice.mic import MicMode, conflicts_with_dota

logger = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    def __init__(self, config: AppConfig, parent=None, overlay=None) -> None:
        super().__init__(parent)
        self.config = config
        self.overlay = overlay
        self.setWindowTitle("Настройки DotaVoice")
        self.resize(600, 560)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        tabs.addTab(self._game_tab(), "Игра")
        tabs.addTab(self._voice_tab(), "Голос")
        tabs.addTab(self._overlay_tab(), "Оверлей")

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel, self
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------

    def _game_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.steam_edit = QLineEdit(self.config.steam_root)
        browse = QPushButton("Обзор…")
        browse.clicked.connect(self._pick_steam)
        form.addRow("Папка Steam:", _row(self.steam_edit, browse))

        self.dota_edit = QLineEdit(self.config.dota_path)
        browse_dota = QPushButton("Обзор…")
        browse_dota.clicked.connect(self._pick_dota)
        form.addRow("Папка Dota 2:", _row(self.dota_edit, browse_dota))

        self.token_edit = QLineEdit(self.config.gsi_token)
        form.addRow("Токен GSI:", self.token_edit)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(self.config.port)
        form.addRow("Порт приложения:", self.port_spin)

        install_btn = QPushButton("Установить файл интеграции (GSI)")
        install_btn.clicked.connect(self._install_gsi)
        form.addRow("", install_btn)

        self.gsi_status = QLabel()
        form.addRow("", self.gsi_status)
        self._update_gsi_status()
        return page

    def _voice_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.name_edit = QLineEdit(self.config.display_name)
        form.addRow("Отображаемое имя:", self.name_edit)

        self.backend_combo = QComboBox()
        for key, label in (
            ("p2p", "Напрямую между игроками через P2WLAN (по умолчанию)"),
            ("relay", "Свой голосовой сервер в интернете"),
            ("memory", "Только состояние (без звука)"),
            ("discord", "Discord"),
        ):
            self.backend_combo.addItem(label, key)
        index = self.backend_combo.findData(self.config.voice_backend)
        self.backend_combo.setCurrentIndex(max(0, index))
        self.backend_combo.setToolTip(
            "DotaVoice сам создаёт локальную голосовую комнату для команды, "
            "и голос идёт напрямую между игроками. Steam для голоса не нужен."
        )
        form.addRow("Голосовой бэкенд:", self.backend_combo)

        self.relay_edit = QLineEdit(self.config.voice_relay_url)
        self.relay_edit.setPlaceholderText("udp://хост:порт — только для своего сервера")
        form.addRow("Адрес своего сервера:", self.relay_edit)

        from ..voice.audio import audio_available, list_devices

        self.input_combo = QComboBox()
        self.output_combo = QComboBox()
        self.input_combo.addItem("По умолчанию", "")
        self.output_combo.addItem("По умолчанию", "")
        if audio_available():
            for device in list_devices():
                if device["inputs"] > 0:
                    self.input_combo.addItem(device["name"], device["name"])
                if device["outputs"] > 0:
                    self.output_combo.addItem(device["name"], device["name"])
        self._select(self.input_combo, self.config.input_device)
        self._select(self.output_combo, self.config.output_device)
        form.addRow("Микрофон:", self.input_combo)
        form.addRow("Вывод звука:", self.output_combo)

        # ---- microphone mode ----
        self.mic_mode_combo = QComboBox()
        for mode in (MicMode.CONTINUOUS, MicMode.PUSH_TO_TALK, MicMode.TOGGLE):
            self.mic_mode_combo.addItem(mode.label, mode.value)
        self.mic_mode_combo.setToolTip(
            "Постоянный: микрофон открыт всё время.\n"
            "Push-to-Talk: говорите, пока держите клавишу.\n"
            "Переключатель: одно нажатие включает, второе выключает."
        )
        index = self.mic_mode_combo.findData(self.config.mic_mode)
        self.mic_mode_combo.setCurrentIndex(max(0, index))
        form.addRow("Режим микрофона:", self.mic_mode_combo)

        self.ptt_edit = QLineEdit(self.config.push_to_talk_key)
        self.ptt_edit.setMaxLength(1)
        self.ptt_edit.setPlaceholderText("одна клавиша, например V")
        form.addRow("Клавиша «говорить»:", self.ptt_edit)

        self.toggle_edit = QLineEdit(self.config.toggle_key)
        self.toggle_edit.setMaxLength(1)
        self.toggle_edit.setPlaceholderText("одна клавиша, например V")
        form.addRow("Клавиша переключателя:", self.toggle_edit)

        self.mic_warning = QLabel()
        self.mic_warning.setWordWrap(True)
        self.mic_warning.setStyleSheet("color: #c98a00;")
        form.addRow("", self.mic_warning)
        self.ptt_edit.textChanged.connect(self._check_hotkeys)
        self.toggle_edit.textChanged.connect(self._check_hotkeys)
        self._check_hotkeys()

        self.steam_sdr = QCheckBox("Разрешить Steam SDR как запасной вариант")
        self.steam_sdr.setChecked(self.config.p2p_steam_sdr_fallback)
        self.steam_sdr.setToolTip(
            "Выключено по умолчанию. Включайте только если у команды не "
            "получается соединиться напрямую: тогда голос пойдёт через Steam."
        )
        form.addRow("", self.steam_sdr)

        self.discord_token = QLineEdit(self.config.discord_bot_token)
        self.discord_guild = QLineEdit(self.config.discord_guild_id)
        form.addRow("Discord: токен бота:", self.discord_token)
        form.addRow("Discord: id сервера:", self.discord_guild)
        return page

    def _check_hotkeys(self, *_args) -> None:
        """Warn when a hotkey collides with a default Dota 2 control."""
        keys = [self.ptt_edit.text().strip(), self.toggle_edit.text().strip()]
        clashes = sorted({k.upper() for k in keys if k and conflicts_with_dota(k)})
        self.mic_warning.setText(
            "Эти клавиши заняты в управлении Dota 2: "
            + ", ".join(clashes)
            + ". Выберите другие, иначе игра и микрофон будут мешать друг другу."
            if clashes
            else ""
        )

    def _overlay_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.overlay_enabled = QCheckBox("Показывать оверлей")
        self.overlay_enabled.setChecked(self.config.overlay_enabled)
        form.addRow("", self.overlay_enabled)

        hint = QLabel(
            "Оверлей можно перетаскивать мышью за значок ⠿ — он запомнит своё место "
            "и вернётся туда при следующем запуске. Кликом по значку микрофона "
            "переключается режим."
        )
        hint.setWordWrap(True)
        form.addRow("", hint)

        self.position_combo = QComboBox()
        for key, label in (
            ("top-left", "сверху слева"),
            ("top-right", "сверху справа"),
            ("bottom-left", "снизу слева"),
            ("bottom-right", "снизу справа"),
        ):
            self.position_combo.addItem(label, key)
        index = self.position_combo.findData(self.config.overlay_position)
        self.position_combo.setCurrentIndex(max(0, index))
        form.addRow("Положение по умолчанию:", self.position_combo)

        reset_btn = QPushButton("Вернуть оверлей на место")
        reset_btn.clicked.connect(self._reset_overlay_position)
        form.addRow("", reset_btn)

        self.opacity_spin = QSpinBox()
        self.opacity_spin.setRange(20, 100)
        self.opacity_spin.setSuffix(" %")
        self.opacity_spin.setValue(int(self.config.overlay_opacity * 100))
        form.addRow("Прозрачность:", self.opacity_spin)
        return page

    def _reset_overlay_position(self) -> None:
        if self.overlay is not None:
            self.overlay.reset_position()
            QMessageBox.information(self, "DotaVoice", "Оверлей возвращён на место.")

    # ------------------------------------------------------------------

    def _select(self, combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _pick_steam(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Папка Steam", self.steam_edit.text())
        if path:
            self.steam_edit.setText(path)

    def _pick_dota(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Папка Dota 2", self.dota_edit.text())
        if path:
            self.dota_edit.setText(path)
            self._update_gsi_status()

    def _update_gsi_status(self) -> None:
        installed = steam.gsi_config_installed(self.dota_edit.text().strip())
        self.gsi_status.setText(
            "Файл интеграции установлен." if installed
            else "Файл интеграции ещё не установлен."
        )

    def _install_gsi(self) -> None:
        try:
            target = steam.install_gsi_config(
                self.dota_edit.text().strip(),
                self.token_edit.text().strip() or "dotavoice",
                port=self.port_spin.value(),
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            QMessageBox.critical(self, "DotaVoice", str(exc))
            return
        self._update_gsi_status()
        QMessageBox.information(self, "DotaVoice", f"Файл установлен:\n{target}")

    def _save(self) -> None:
        self.config.steam_root = self.steam_edit.text().strip()
        self.config.dota_path = self.dota_edit.text().strip()
        self.config.gsi_token = self.token_edit.text().strip() or "dotavoice"
        self.config.port = self.port_spin.value()
        self.config.display_name = self.name_edit.text().strip()
        self.config.voice_backend = self.backend_combo.currentData() or "p2p"
        self.config.voice_relay_url = self.relay_edit.text().strip()
        self.config.input_device = self.input_combo.currentData() or ""
        self.config.output_device = self.output_combo.currentData() or ""
        self.config.mic_mode = self.mic_mode_combo.currentData() or "continuous"
        self.config.push_to_talk_key = self.ptt_edit.text().strip() or "v"
        self.config.toggle_key = self.toggle_edit.text().strip() or "v"
        self.config.p2p_steam_sdr_fallback = self.steam_sdr.isChecked()
        self.config.discord_bot_token = self.discord_token.text().strip()
        self.config.discord_guild_id = self.discord_guild.text().strip()
        self.config.overlay_enabled = self.overlay_enabled.isChecked()
        self.config.overlay_position = self.position_combo.currentData() or "top-left"
        self.config.overlay_opacity = self.opacity_spin.value() / 100.0
        self.config.save()
        self.accept()


def _row(widget: QWidget, button: QPushButton) -> QWidget:
    container = QWidget()
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(widget, 1)
    row.addWidget(button)
    return container
