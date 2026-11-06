"""Voice backend factory + public exports."""

from __future__ import annotations

from ..config import Settings
from .base import VoiceBackend, channel_name
from .memory import MemoryVoiceBackend

__all__ = ["VoiceBackend", "MemoryVoiceBackend", "channel_name", "build_voice_backend"]


def build_voice_backend(settings: Settings, relay=None, on_peer_speaking=None) -> VoiceBackend:
    if settings.voice == "p2p":
        from .p2p_backend import P2PVoiceBackend

        backend = P2PVoiceBackend(
            input_device=settings.input_device or None,
            output_device=settings.output_device or None,
            discovery_group=settings.p2p_discovery_group or None,
            on_peer_speaking=on_peer_speaking,
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
