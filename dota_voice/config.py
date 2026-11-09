"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _path(value: str | None) -> Path | None:
    return Path(value) if value else None


@dataclass
class Settings:
    """Service settings.

    Everything has a safe default so the service boots with zero config; the
    only thing you must wire up for a real game is the GSI token.
    """

    host: str = "0.0.0.0"
    port: int = 12000
    gsi_token: str | None = None
    store: str = "memory"  # memory | redis
    redis_url: str = "redis://localhost:6379/0"
    # P2WLAN + GSI is the default: a private virtual network per match, no
    # Steamworks involved. See dota_voice/net/p2wlan.py.
    voice: str = "p2p"  # p2p (P2WLAN) | relay | discord | memory
    session_grace_seconds: int = 900
    log_level: str = "INFO"

    # Chat (ТЗ §6.3, §8.3). Team text stays on a side; the shared post-match
    # channel carries text only. Turning this off keeps the mesh strictly
    # team-isolated (voice and text alike).
    chat_enabled: bool = True
    # Length of the post-match discussion window, in seconds (ТЗ §8.4:
    # 10-15 minutes, 12 by default).
    post_game_seconds: int = 12 * 60

    # Peer-to-peer voice (encrypted, serverless)
    p2p_discovery_group: str = ""
    # Steam SDR is an optional fallback for players P2WLAN cannot reach. Off by
    # default: it uses Steamworks and is a grey area of Valve's terms.
    p2p_steam_sdr: bool = False
    # Use Steam SDR automatically when P2WLAN fails to build a room.
    p2p_steam_sdr_fallback: bool = False

    # P2WLAN (virtual network per match)
    p2wlan_driver: str = "auto"  # auto | local | cli | http
    p2wlan_binary: str = "p2wlan"
    p2wlan_control_url: str = ""
    p2wlan_relay_url: str = ""
    p2wlan_state_path: Path | None = None

    # Microphone
    mic_mode: str = "continuous"  # continuous | push_to_talk | toggle
    mic_push_to_talk_key: str = "v"
    mic_toggle_key: str = "v"
    mic_click_feedback: bool = True

    # Voice relay (self-hosted team voice)
    voice_relay_url: str = ""  # udp://host:port; empty = embedded relay on `port`
    voice_room: str = "match"
    display_name: str = ""
    input_device: str = ""
    output_device: str = ""

    # Discord
    discord_bot_token: str | None = None
    discord_guild_id: str | None = None
    discord_user_map: dict[str, str] = field(default_factory=dict)

    # Updates / distribution
    app_version: str = "0.1.0"
    update_public_key: str = ""

    # Optional LLM layer
    llm_model: str = "gpt-4.1"
    llm_api_key: str | None = None
    llm_base_url: str | None = None

    # Workspace / persistence
    workspace_dir: Path = field(default_factory=lambda: Path.cwd())

    @property
    def p2p_enabled(self) -> bool:
        return self.voice == "p2p"

    @property
    def steam_sdr_active(self) -> bool:
        """Whether Steam SDR may actually be used.

        SDR is only ever engaged when the operator turned it on *and* asked for
        it as a fallback, so the default path never touches Steamworks.
        """
        return self.p2p_steam_sdr and self.p2p_steam_sdr_fallback

    def guard_safety(self) -> None:
        """Refuse unsafe combinations before the service starts."""
        from .security.policy import default_policy

        policy = default_policy()
        policy.steam_sdr_enabled = self.p2p_steam_sdr
        policy.guard()
        if self.p2p_steam_sdr:
            # Enabling SDR is an explicit choice; make the trade-off visible.
            policy.guard_sdr()

    @property
    def llm_enabled(self) -> bool:
        """Whether the agent should reason with an LLM or run deterministically."""
        return bool(self.llm_api_key)

    @property
    def conversations_dir(self) -> Path:
        return self.workspace_dir / ".conversations"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = env if env is not None else dict(os.environ)
        user_map: dict[str, str] = {}
        raw_map = env.get("DISCORD_USER_MAP", "")
        if raw_map.strip():
            try:
                parsed = json.loads(raw_map)
                if isinstance(parsed, dict):
                    user_map = {str(k): str(v) for k, v in parsed.items()}
            except json.JSONDecodeError:
                user_map = {}
        return cls(
            host=env.get("DOTAVOICE_HOST", "0.0.0.0"),
            port=_int(env.get("DOTAVOICE_PORT"), 12000),
            gsi_token=env.get("DOTAVOICE_GSI_TOKEN") or None,
            store=env.get("DOTAVOICE_STORE", "memory").lower(),
            redis_url=env.get("DOTAVOICE_REDIS_URL", "redis://localhost:6379/0"),
            voice=env.get("DOTAVOICE_VOICE", "p2p").lower(),
            voice_relay_url=env.get("DOTAVOICE_VOICE_RELAY_URL")
            or env.get("DOTAVOICE_RELAY_URL", ""),
            voice_room=env.get("DOTAVOICE_ROOM", "match"),
            p2p_discovery_group=env.get("DOTAVOICE_P2P_GROUP", ""),
            p2p_steam_sdr=_bool(env.get("DOTAVOICE_P2P_STEAM_SDR"), False),
            p2p_steam_sdr_fallback=_bool(
                env.get("DOTAVOICE_P2P_STEAM_SDR_FALLBACK"), False
            ),
            p2wlan_driver=env.get("DOTAVOICE_P2WLAN_DRIVER", "auto").lower(),
            p2wlan_binary=env.get("DOTAVOICE_P2WLAN_BINARY", "p2wlan"),
            p2wlan_control_url=env.get("DOTAVOICE_P2WLAN_CONTROL_URL", ""),
            p2wlan_relay_url=env.get("DOTAVOICE_P2WLAN_RELAY_URL", ""),
            p2wlan_state_path=_path(env.get("DOTAVOICE_P2WLAN_STATE")),
            mic_mode=env.get("DOTAVOICE_MIC_MODE", "continuous").lower(),
            mic_push_to_talk_key=env.get("DOTAVOICE_MIC_PTT_KEY", "v"),
            mic_toggle_key=env.get("DOTAVOICE_MIC_TOGGLE_KEY", "v"),
            mic_click_feedback=_bool(env.get("DOTAVOICE_MIC_CLICK"), True),
            display_name=env.get("DOTAVOICE_DISPLAY_NAME")
            or env.get("DOTAVOICE_NAME", ""),
            input_device=env.get("DOTAVOICE_INPUT_DEVICE", ""),
            output_device=env.get("DOTAVOICE_OUTPUT_DEVICE", ""),
            session_grace_seconds=_int(env.get("DOTAVOICE_SESSION_GRACE_SECONDS"), 900),
            chat_enabled=_bool(env.get("DOTAVOICE_CHAT"), True),
            post_game_seconds=_int(
                env.get("DOTAVOICE_POST_GAME_SECONDS"), 12 * 60
            ),
            log_level=env.get("DOTAVOICE_LOG_LEVEL", "INFO").upper(),
            discord_bot_token=env.get("DISCORD_BOT_TOKEN") or None,
            discord_guild_id=env.get("DISCORD_GUILD_ID") or None,
            discord_user_map=user_map,
            app_version=env.get("DOTAVOICE_VERSION", "0.1.0"),
            update_public_key=env.get("DOTAVOICE_UPDATE_PUBLIC_KEY", ""),
            llm_model=env.get("LLM_MODEL", "gpt-4.1"),
            llm_api_key=env.get("LLM_API_KEY") or None,
            llm_base_url=env.get("LLM_BASE_URL") or None,
            workspace_dir=Path(env.get("DOTAVOICE_WORKSPACE", str(Path.cwd()))),
        )
