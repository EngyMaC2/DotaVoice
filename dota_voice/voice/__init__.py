"""Voice backend factory + public exports."""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from .base import VoiceBackend, channel_name
from .memory import MemoryVoiceBackend

__all__ = ["VoiceBackend", "MemoryVoiceBackend", "channel_name", "build_voice_backend"]


def build_p2wlan_controller(settings: Settings):
    """Pick a P2WLAN driver from the configuration.

    ``auto`` prefers the real CLI when it is installed and falls back to the
    in-process driver otherwise, so the app works on a fresh machine with no
    P2WLAN binary and upgrades itself to the real network when one is present.
    """
    from ..net.p2wlan import CliDriver, HttpDriver, LocalDriver, P2WlanController

    driver_name = (settings.p2wlan_driver or "auto").lower()
    driver = None
    if driver_name == "cli":
        driver = CliDriver(settings.p2wlan_binary)
    elif driver_name == "http":
        driver = HttpDriver(settings.p2wlan_control_url)
    elif driver_name == "auto":
        if settings.p2wlan_control_url:
            driver = HttpDriver(settings.p2wlan_control_url)
        elif CliDriver.available(settings.p2wlan_binary):
            driver = CliDriver(settings.p2wlan_binary)
        else:
            driver = LocalDriver()
    else:
        driver = LocalDriver()

    state_path = settings.p2wlan_state_path
    if state_path is None:
        from .. import paths

        state_path = paths.p2wlan_file()
    return P2WlanController(
        driver=driver,
        state_path=Path(state_path),
        display_name=settings.display_name,
        fallback_to_sdr=settings.steam_sdr_active,
    )


def build_voice_backend(
    settings: Settings,
    relay=None,
    on_peer_speaking=None,
    on_peer_team=None,
    on_peer_left=None,
) -> VoiceBackend:
    if settings.voice == "p2p":
        from .p2p_backend import P2PVoiceBackend

        backend = P2PVoiceBackend(
            input_device=settings.input_device or None,
            output_device=settings.output_device or None,
            discovery_group=settings.p2p_discovery_group or None,
            on_peer_speaking=on_peer_speaking,
            on_peer_team=on_peer_team,
            on_peer_left=on_peer_left,
            controller=build_p2wlan_controller(settings),
        )
        backend.set_local_name(settings.display_name or "")
        return backend
    if settings.voice == "discord":
        from .discord import DiscordVoiceBackend

        return DiscordVoiceBackend(
            bot_token=settings.discord_bot_token or "",
            guild_id=settings.discord_guild_id or "",
            user_map=settings.discord_user_map,
        )
    if settings.voice == "relay":
        from .relay import VoiceRelay
        from .relay_backend import RelayVoiceBackend

        relay = relay or VoiceRelay(port=settings.port)
        return RelayVoiceBackend(relay)
    return MemoryVoiceBackend()
