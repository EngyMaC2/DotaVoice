"""Desktop entry point: ``python -m dota_voice.desktop`` / ``dotavoice-gui``.

Flow:

1. load (or create) the user configuration;
2. if this is the first run, show the setup wizard and write the GSI config;
3. start the engine in a background thread;
4. show the control window and, if enabled, the transparent overlay.

Everything is native Qt; nothing here requires a browser.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .. import paths
from ..appconfig import AppConfig
from . import overlay_position
from .api import EngineAPI
from .engine import EngineThread
from .main_window import MainWindow
from .overlay_window import ChatHotkeyWatcher, OverlayWindow
from .wizard import SetupWizard

logger = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    paths.ensure_dirs()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler(paths.log_file(), encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dotavoice",
        description="DotaVoice — голосовой мост для Dota 2",
    )
    parser.add_argument(
        "--reset-setup", action="store_true", help="показать мастер настройки заново"
    )
    parser.add_argument(
        "--no-overlay", action="store_true", help="не показывать оверлей"
    )
    parser.add_argument(
        "--config", help="путь к файлу настроек (по умолчанию — системный)"
    )
    parser.add_argument(
        "--headless-check", action="store_true",
        help="запустить движок, проверить его и выйти (для тестов)",
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = None
    if args.config:
        from pathlib import Path

        config_path = Path(args.config)
    config = AppConfig.load(config_path)
    _configure_logging(config.log_level)

    # ------------------------------------------------------------------
    # headless self-check: start the engine, probe it, exit
    # ------------------------------------------------------------------
    if args.headless_check:
        engine = EngineThread(config.to_settings())
        port = engine.start()
        api = EngineAPI(f"http://127.0.0.1:{port}")
        try:
            health = api.health()
            logger.info("Проверка движка: %s", health)
            print(health)
        finally:
            api.close()
            engine.stop()
        return 0

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("DotaVoice")
    app.setApplicationDisplayName("DotaVoice")

    # ------------------------------------------------------------------
    # setup wizard on first run
    # ------------------------------------------------------------------
    if args.reset_setup or not config.setup_complete:
        wizard = SetupWizard(config)
        if wizard.exec() != SetupWizard.Accepted:
            logger.info("Настройка отменена пользователем")
            return 1
        config.save(config_path)

    # ------------------------------------------------------------------
    # engine
    # ------------------------------------------------------------------
    engine = EngineThread(config.to_settings())
    try:
        port = engine.start()
    except RuntimeError as exc:
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.critical(None, "DotaVoice", f"Не удалось запустить движок: {exc}")
        return 1
    api = EngineAPI(f"http://127.0.0.1:{port}")

    window = MainWindow(api, config)

    overlay = None
    if config.overlay_enabled and not args.no_overlay:
        overlay = OverlayWindow(
            api.overlay_url(),
            settings=overlay_position.load(),
            position=config.overlay_position,
            opacity=config.overlay_opacity,
        )
        # The overlay's chips drive the engine directly: the mic chip cycles the
        # mode, the mute chip toggles the local self-mute.
        overlay.mic_cycled.connect(lambda: api.cycle_mic())
        overlay.mute_toggled.connect(lambda: api.toggle_self_mute())
        # A global hotkey opens the chat box, since the click-through overlay
        # never holds focus and cannot use a Qt shortcut.
        chat_hotkey = ChatHotkeyWatcher(overlay, key=config.chat_hotkey)
        chat_hotkey.triggered.connect(lambda: overlay.open_chat())
        window.set_overlay(overlay)

    def toggle_overlay(show: bool) -> None:
        if overlay is None:
            return
        if show:
            overlay.show_overlay()
        else:
            overlay.hide()

    window.set_overlay_controller(toggle_overlay)
    if overlay is not None:
        overlay.show_overlay()

    window.show()
    try:
        return app.exec()
    finally:
        api.close()
        engine.stop()
        if overlay is not None:
            overlay.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
