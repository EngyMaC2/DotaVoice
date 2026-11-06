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

logger = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    def __init__(self, config: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Настройки DotaVoice")
        self.resize(600, 520)

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
            ("relay", "Встроенный голосовой сервер (по умолчанию)"),
            ("memory", "Только состояние (без звука)"),
            ("discord", "Discord"),
        ):
            self.backend_combo.addItem(label, key)
        index = self.backend_combo.findData(self.config.voice_backend)
        self.backend_combo.setCurrentIndex(max(0, index))
        form.addRow("Голосовой бэкенд:", self.backend_combo)

        self.relay_edit = QLineEdit(self.config.voice_relay_url)
        self.relay_edit.setPlaceholderText("пусто = встроенный сервер; или udp://host:порт")
        form.addRow("Адрес голосового сервера:", self.relay_edit)

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

        self.ptt_edit = QLineEdit(self.config.push_to_talk_key)
        self.ptt_edit.setMaxLength(1)
        form.addRow("Клавиша push-to-talk:", self.ptt_edit)

        self.discord_token = QLineEdit(self.config.discord_bot_token)
        self.discord_guild = QLineEdit(self.config.discord_guild_id)
        form.addRow("Discord: токен бота:", self.discord_token)
        form.addRow("Discord: id сервера:", self.discord_guild)
        return page

    def _overlay_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.overlay_enabled = QCheckBox("Показывать оверлей")
        self.overlay_enabled.setChecked(self.config.overlay_enabled)
        form.addRow("", self.overlay_enabled)

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
        form.addRow("Положение:", self.position_combo)
        return page

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
        self.config.voice_backend = self.backend_combo.currentData() or "relay"
        self.config.voice_relay_url = self.relay_edit.text().strip()
        self.config.input_device = self.input_combo.currentData() or ""
        self.config.output_device = self.output_combo.currentData() or ""
        self.config.push_to_talk_key = self.ptt_edit.text().strip() or "v"
        self.config.discord_bot_token = self.discord_token.text().strip()
        self.config.discord_guild_id = self.discord_guild.text().strip()
        self.config.overlay_enabled = self.overlay_enabled.isChecked()
        self.config.overlay_position = self.position_combo.currentData() or "top-left"
        self.config.save()
        self.accept()


def _row(widget: QWidget, button: QPushButton) -> QWidget:
    container = QWidget()
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(widget, 1)
    row.addWidget(button)
    return container
