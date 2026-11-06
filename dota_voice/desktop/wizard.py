"""First-run setup wizard.

Walks the user through the two things the app genuinely needs to know: where
Dota 2 is installed (so it can install the GSI config) and who they are in the
match (so the overlay and voice room show the right name). Everything is
auto-detected where possible and can be corrected by hand.
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from .. import steam
from ..appconfig import AppConfig

logger = logging.getLogger(__name__)


class WelcomePage(QWizardPage):
    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.setTitle("Добро пожаловать в DotaVoice")
        self.setSubTitle(
            "Голосовой мост подключается к вашим матчам Dota 2 и создаёт "
            "два командных голосовых канала автоматически."
        )
        layout = QVBoxLayout(self)
        text = QLabel(
            "<p>Мастер поможет за пару шагов:</p>"
            "<ul>"
            "<li>найти Steam и Dota 2 и установить файл интеграции (GSI);</li>"
            "<li>указать ваше имя в матче и голосовое устройство;</li>"
            "<li>настроить прозрачный оверлей.</li>"
            "</ul>"
            "<p>Игру и Steam перезапускать не обязательно — достаточно начать матч.</p>"
        )
        text.setWordWrap(True)
        layout.addWidget(text)
        layout.addStretch(1)


class SteamPage(QWizardPage):
    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self.setTitle("Steam и Dota 2")
        self.setSubTitle("Приложение найдёт игру и установит в её профиль файл интеграции.")

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.steam_edit = QLineEdit(config.steam_root)
        self.steam_edit.setPlaceholderText("папка Steam (определится автоматически)")
        steam_btn = QPushButton("Обзор…")
        steam_btn.clicked.connect(self._pick_steam)
        form.addRow("Папка Steam:", _with_button(self.steam_edit, steam_btn))

        self.dota_edit = QLineEdit(config.dota_path)
        self.dota_edit.setPlaceholderText("папка Dota 2 (…/steamapps/common/dota 2 beta)")
        dota_btn = QPushButton("Обзор…")
        dota_btn.clicked.connect(self._pick_dota)
        form.addRow("Папка Dota 2:", _with_button(self.dota_edit, dota_btn))

        self.user_combo = QComboBox()
        self.user_combo.setEditable(True)
        form.addRow("Профиль Steam:", self.user_combo)

        self.token_edit = QLineEdit(config.gsi_token)
        form.addRow("Токен GSI:", self.token_edit)

        layout.addLayout(form)

        detect_btn = QPushButton("Найти автоматически")
        detect_btn.clicked.connect(self.autodetect)
        layout.addWidget(detect_btn)

        self.desktop_shortcut = QCheckBox("Создать ярлык на рабочем столе")
        self.desktop_shortcut.setChecked(True)
        layout.addWidget(self.desktop_shortcut)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch(1)

        self.autodetect(silent=True)

    # ------------------------------------------------------------------

    def _pick_steam(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Папка Steam", self.steam_edit.text())
        if path:
            self.steam_edit.setText(path)
            self._refresh_users(path)

    def _pick_dota(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Папка Dota 2", self.dota_edit.text())
        if path:
            self.dota_edit.setText(path)
            self._validate_dota()

    def autodetect(self, silent: bool = False) -> None:
        root = steam.find_steam_root(self.steam_edit.text() or None)
        if root is not None:
            self.steam_edit.setText(str(root))
            self._refresh_users(str(root))
        dota = steam.find_dota_path(
            steam_root=root, explicit=self.dota_edit.text() or None
        )
        if dota is not None:
            self.dota_edit.setText(str(dota))
        if not silent:
            self.status.setText(
                "Найдено автоматически." if dota else
                "Не удалось найти Dota 2 — укажите папку вручную."
            )
        self._validate_dota()

    def _refresh_users(self, steam_root: str) -> None:
        users = steam.read_users(__import__("pathlib").Path(steam_root))
        self.user_combo.clear()
        for user in users:
            label = user.persona or user.account_id
            self.user_combo.addItem(label, user.account_id)
        chosen = steam.pick_user(users)
        if chosen is not None:
            self.user_combo.setCurrentText(chosen.persona or chosen.account_id)

    def _validate_dota(self) -> None:
        path = self.dota_edit.text().strip()
        if path and (__import__("pathlib").Path(path) / "game" / "dota").exists():
            self.status.setText("Dota 2 найдена. Файл интеграции будет установлен.")
        elif path:
            self.status.setText("Похоже, это не папка Dota 2 (нет каталога game/dota).")

    def validatePage(self) -> bool:  # noqa: N802 - Qt API
        dota = self.dota_edit.text().strip()
        if not dota:
            QMessageBox.warning(self, "DotaVoice", "Укажите папку Dota 2.")
            return False
        return True

    def collect(self) -> None:
        self.config.steam_root = self.steam_edit.text().strip()
        self.config.dota_path = self.dota_edit.text().strip()
        self.config.gsi_token = self.token_edit.text().strip() or "dotavoice"
        index = self.user_combo.currentIndex()
        if index >= 0:
            self.config.steam_user_id = self.user_combo.itemData(index) or ""
        self.config.steam_persona = self.user_combo.currentText().strip()


class VoicePage(QWizardPage):
    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self.setTitle("Голос")
        self.setSubTitle("Ваше имя в матче и микрофон.")

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.name_edit = QLineEdit(config.display_name)
        self.name_edit.setPlaceholderText("как вас звать в голосовом канале")
        form.addRow("Отображаемое имя:", self.name_edit)

        self.input_combo = QComboBox()
        self.output_combo = QComboBox()
        self._fill_devices()
        form.addRow("Микрофон:", self.input_combo)
        form.addRow("Устройство вывода:", self.output_combo)

        layout.addLayout(form)

        hint = QLabel(
            "Если звуковое устройство недоступно, приложение всё равно подключится "
            "к комнате матча и покажет, кто говорит."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)

    def _fill_devices(self) -> None:
        from ..voice.audio import audio_available, list_devices

        self.input_combo.addItem("По умолчанию", "")
        self.output_combo.addItem("По умолчанию", "")
        if not audio_available():
            return
        for device in list_devices():
            label = device["name"]
            if device["inputs"] > 0:
                self.input_combo.addItem(label, label)
            if device["outputs"] > 0:
                self.output_combo.addItem(label, label)

    def collect(self) -> None:
        self.config.display_name = self.name_edit.text().strip()
        self.config.input_device = self.input_combo.currentData() or ""
        self.config.output_device = self.output_combo.currentData() or ""


class OverlayPage(QWizardPage):
    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self.setTitle("Оверлей")
        self.setSubTitle("Прозрачное окно поверх игры: кто говорит, кто в муте, кто за какую сторону.")

        layout = QVBoxLayout(self)
        self.enabled = QCheckBox("Показывать оверлей при активном матче")
        self.enabled.setChecked(config.overlay_enabled)
        self.always_on_top = QCheckBox("Поверх всех окон (не перехватывает клики)")
        self.always_on_top.setChecked(True)
        layout.addWidget(self.enabled)
        layout.addWidget(self.always_on_top)

        form = QFormLayout()
        self.position = QComboBox()
        for key, label in (
            ("top-left", "сверху слева"),
            ("top-right", "сверху справа"),
            ("bottom-left", "снизу слева"),
            ("bottom-right", "снизу справа"),
        ):
            self.position.addItem(label, key)
        form.addRow("Положение:", self.position)
        layout.addLayout(form)
        layout.addStretch(1)

    def collect(self) -> None:
        self.config.overlay_enabled = self.enabled.isChecked()
        self.config.overlay_position = self.position.currentData() or "top-left"


class SetupWizard(QWizard):
    def __init__(self, config: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("DotaVoice — первоначальная настройка")
        self.setWizardStyle(QWizard.ModernStyle)
        self.resize(640, 480)

        self.welcome = WelcomePage(config)
        self.steam_page = SteamPage(config)
        self.voice_page = VoicePage(config)
        self.overlay_page = OverlayPage(config)
        self.addPage(self.welcome)
        self.addPage(self.steam_page)
        self.addPage(self.voice_page)
        self.addPage(self.overlay_page)

    def accept(self) -> None:  # noqa: N802 - Qt API
        self.steam_page.collect()
        self.voice_page.collect()
        self.overlay_page.collect()
        try:
            target = steam.install_gsi_config(
                self.config.dota_path,
                self.config.gsi_token,
                host=self.config.host,
                port=self.config.port,
            )
            self.config.gsi_installed = True
            logger.info("GSI-конфиг установлен: %s", target)
        except (FileNotFoundError, PermissionError, OSError) as exc:
            self.config.gsi_installed = False
            QMessageBox.critical(
                self,
                "Не удалось установить файл интеграции",
                f"{exc}\n\nЗапустите приложение от имени администратора или "
                "укажите другую папку Dota 2.",
            )
            return
        self.config.setup_complete = True
        if self.steam_page.desktop_shortcut.isChecked():
            self._create_shortcut()
        super().accept()

    def _create_shortcut(self) -> None:
        try:
            from .shortcuts import create_shortcuts_for_self

            created = create_shortcuts_for_self(desktop=True, start_menu=True)
            for path in created:
                logger.info("Ярлык создан: %s", path)
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning("Не удалось создать ярлык: %s", exc)


def _with_button(edit: QLineEdit, button: QPushButton):
    """Return a QWidget pairing a line edit with a browse button."""
    from PySide6.QtWidgets import QWidget

    container = QWidget()
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(edit, 1)
    row.addWidget(button)
    return container


__all__ = ["SetupWizard", "SteamPage"]
