"""Voice backend factory + public exports."""

from __future__ import annotations

from ..config import Settings
from .base import VoiceBackend, channel_name
from .memory import MemoryVoiceBackend

__all__ = ["VoiceBackend", "MemoryVoiceBackend", "channel_name", "build_voice_backend"]


def build_voice_backend(settings: Settings, relay=None) -> VoiceBackend:
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
