"""Persistent application settings.

The desktop app keeps its configuration in a single JSON file under the
platform config directory (:mod:`dota_voice.paths`). This is what the setup
wizard writes when the user picks their Steam/Dota folders, and what the main
window edits afterwards. It converts to the service-level :class:`Settings`
that the engine already understands.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from . import paths
from .config import Settings

logger = logging.getLogger(__name__)


def _default_workspace() -> str:
    return str(paths.workspace_dir())


@dataclass
class AppConfig:
    """User-facing configuration, serialisable to ``settings.json``."""

    # ---- first-run / wizard state ----
    setup_complete: bool = False

    # ---- Steam / Dota 2 ----
    steam_root: str = ""
    dota_path: str = ""
    steam_user_id: str = ""
    steam_persona: str = ""
    gsi_installed: bool = False

    # ---- service ----
    host: str = "127.0.0.1"
    port: int = 12000
    gsi_token: str = "dotavoice"
    log_level: str = "INFO"

    # ---- voice ----
    # P2WLAN + GSI is the default path; Steam is not involved at all.
    voice_backend: str = "p2p"  # p2p (P2WLAN) | relay | memory | discord
    p2p_discovery_group: str = ""
    p2p_steam_sdr: bool = False
    p2p_steam_sdr_fallback: bool = False
    p2wlan_driver: str = "auto"  # auto | local | cli | http
    p2wlan_binary: str = "p2wlan"
    p2wlan_control_url: str = ""
    voice_relay_url: str = ""
    voice_room: str = "match"
    display_name: str = ""
    input_device: str = ""
    output_device: str = ""

    # ---- microphone ----
    mic_mode: str = "continuous"  # continuous | push_to_talk | toggle
    push_to_talk_key: str = "v"
    toggle_key: str = "v"
    mic_click_feedback: bool = True
    push_to_talk: bool = False  # legacy flag, kept for older settings files

    # ---- overlay / desktop behaviour ----
    overlay_enabled: bool = True
    overlay_opacity: float = 0.92
    overlay_position: str = "top-left"  # top-left | top-right | bottom-left | bottom-right
    autostart_matches: bool = True
    minimize_to_tray: bool = True

    # ---- discord (optional backend) ----
    discord_bot_token: str = ""
    discord_guild_id: str = ""
    discord_user_map: dict[str, str] = field(default_factory=dict)

    # ---- optional LLM reasoning layer ----
    llm_model: str = "gpt-4.1"
    llm_api_key: str = ""
    llm_base_url: str = ""

    workspace_dir: str = field(default_factory=_default_workspace)

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        path = path or paths.config_file()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Не удалось прочитать настройки %s: %s", path, exc)
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path | None = None) -> Path:
        path = path or paths.config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def update(self, **values: Any) -> None:
        for key, value in values.items():
            if hasattr(self, key):
                setattr(self, key, value)

    # ------------------------------------------------------------------
    # derived
    # ------------------------------------------------------------------

    @property
    def dota_cfg_dir(self) -> Path | None:
        """Directory where Dota 2 reads GSI configs, if Dota is located."""
        if not self.dota_path:
            return None
        return (
            Path(self.dota_path)
            / "game"
            / "dota"
            / "cfg"
            / "gamestate_integration"
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.dota_path and self.gsi_installed)

    def to_settings(self) -> Settings:
        """Build the engine :class:`Settings` from this configuration."""
        return Settings(
            host=self.host,
            port=self.port,
            gsi_token=self.gsi_token or None,
            store="memory",
            voice=self.voice_backend,
            p2p_discovery_group=self.p2p_discovery_group,
            p2p_steam_sdr=self.p2p_steam_sdr,
            p2p_steam_sdr_fallback=self.p2p_steam_sdr_fallback,
            p2wlan_driver=self.p2wlan_driver,
            p2wlan_binary=self.p2wlan_binary,
            p2wlan_control_url=self.p2wlan_control_url or None,
            voice_relay_url=self.voice_relay_url,
            voice_room=self.voice_room,
            display_name=self.display_name,
            input_device=self.input_device,
            output_device=self.output_device,
            mic_mode=self.mic_mode,
            mic_push_to_talk_key=self.push_to_talk_key,
            mic_toggle_key=self.toggle_key,
            mic_click_feedback=self.mic_click_feedback,
            log_level=self.log_level,
            discord_bot_token=self.discord_bot_token or None,
            discord_guild_id=self.discord_guild_id or None,
            discord_user_map=dict(self.discord_user_map),
            llm_model=self.llm_model,
            llm_api_key=self.llm_api_key or None,
            llm_base_url=self.llm_base_url or None,
            workspace_dir=Path(self.workspace_dir or _default_workspace()),
        )
