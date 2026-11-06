"""In-memory voice backend.

Used for local development, the demo, and the test suite. It models channels
and per-player voice state faithfully enough that the orchestrator, the agent
tools, and the overlay can all be exercised without a real voice server.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..models import Roster, Team, VoiceState
from .base import channel_name


@dataclass
class _Channel:
    channel_id: str
    name: str
    team: Team
    members: set[str] = field(default_factory=set)


class MemoryVoiceBackend:
    def __init__(self) -> None:
        # match_id -> team -> channel
        self._channels: dict[str, dict[Team, _Channel]] = {}
        # match_id -> player_id -> VoiceState
        self._states: dict[str, dict[str, VoiceState]] = {}

    async def ensure_channels(self, match_id: str) -> tuple[str, str]:
        channels = self._channels.setdefault(match_id, {})
        for team in (Team.RADIANT, Team.DIRE):
            if team not in channels:
                channels[team] = _Channel(
                    channel_id=f"{match_id}-{team.value}",
                    name=channel_name(match_id, team),
                    team=team,
                )
        return channels[Team.RADIANT].channel_id, channels[Team.DIRE].channel_id

    async def sync_roster(self, match_id: str, roster: Roster) -> None:
        await self.ensure_channels(match_id)
        channels = self._channels[match_id]
        states = self._states.setdefault(match_id, {})

        assigned: set[str] = set()
        for player in roster.all_players():
            if player.team is Team.UNKNOWN:
                continue
            target = channels[player.team]
            assigned.add(player.player_id)
            # Remove from the other channel first (team switch).
            for team, channel in channels.items():
                if team is not player.team:
                    channel.members.discard(player.player_id)
            target.members.add(player.player_id)
            state = states.get(player.player_id) or VoiceState(player_id=player.player_id)
            state.team = player.team
            state.channel_id = target.channel_id
            state.updated_at = time.time()
            states[player.player_id] = state

        # Players no longer in the roster leave their channel.
        for player_id, state in list(states.items()):
            if player_id not in assigned:
                for channel in channels.values():
                    channel.members.discard(player_id)
                state.channel_id = None
                state.speaking = False

    async def set_mute(self, match_id: str, player_id: str, muted: bool) -> None:
        state = self._state(match_id, player_id)
        state.muted = muted
        state.updated_at = time.time()

    async def set_deafen(self, match_id: str, player_id: str, deafened: bool) -> None:
        state = self._state(match_id, player_id)
        state.deafened = deafened
        state.updated_at = time.time()

    async def remove_player(self, match_id: str, player_id: str) -> None:
        for channel in self._channels.get(match_id, {}).values():
            channel.members.discard(player_id)
        self._states.get(match_id, {}).pop(player_id, None)

    async def delete_channels(self, match_id: str) -> None:
        self._channels.pop(match_id, None)

    async def close(self) -> None:  # pragma: no cover - nothing to release
        self._channels.clear()
        self._states.clear()

    # ---- introspection helpers used by tests / overlay ----

    def members(self, match_id: str, team: Team) -> set[str]:
        channel = self._channels.get(match_id, {}).get(team)
        return set(channel.members) if channel else set()

    def channel_of(self, match_id: str, player_id: str) -> str | None:
        state = self._states.get(match_id, {}).get(player_id)
        return state.channel_id if state else None

    def state_of(self, match_id: str, player_id: str) -> VoiceState | None:
        return self._states.get(match_id, {}).get(player_id)

    def _state(self, match_id: str, player_id: str) -> VoiceState:
        states = self._states.setdefault(match_id, {})
        state = states.get(player_id)
        if state is None:
            state = VoiceState(player_id=player_id)
            states[player_id] = state
        return state
