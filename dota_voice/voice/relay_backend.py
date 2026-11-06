"""Voice backend that drives the built-in relay.

This adapts the :class:`~dota_voice.voice.relay.VoiceRelay` to the
:class:`~dota_voice.voice.base.VoiceBackend` contract the orchestrator uses, so
the agent's tools (`create_voice_channels`, `sync_roster`, `set_mute`, ...)
control a real, self-hosted voice room with no external service.
"""

from __future__ import annotations

import logging

from ..models import Roster, Team
from .protocol import TEAM_DIRE, TEAM_RADIANT
from .relay import VoiceRelay

logger = logging.getLogger(__name__)


class RelayVoiceBackend:
    def __init__(self, relay: VoiceRelay) -> None:
        self._relay = relay

    async def ensure_channels(self, match_id: str) -> tuple[str, str]:
        self._relay.ensure_room(match_id)
        return (f"{match_id}-radiant", f"{match_id}-dire")

    async def sync_roster(self, match_id: str, roster: Roster) -> None:
        teams: dict[str, int] = {}
        for player in roster.all_players():
            teams[player.player_id] = (
                TEAM_RADIANT if player.team == Team.RADIANT else TEAM_DIRE
            )
            if player.name:
                teams[player.name] = teams[player.player_id]
        self._relay.set_teams(match_id, teams)

    async def set_mute(self, match_id: str, player_id: str, muted: bool) -> None:
        self._relay.set_muted(match_id, player_id, muted)

    async def set_deafen(self, match_id: str, player_id: str, deafened: bool) -> None:
        self._relay.set_deafened(match_id, player_id, deafened)

    async def remove_player(self, match_id: str, player_id: str) -> None:
        room = self._relay.ensure_room(match_id)
        for addr, peer in list(room.peers.items()):
            if peer.player == player_id:
                room.peers.pop(addr, None)

    async def delete_channels(self, match_id: str) -> None:
        self._relay.close_room(match_id)

    async def close(self) -> None:
        await self._relay.stop()
