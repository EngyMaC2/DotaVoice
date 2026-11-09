"""Voice backend abstraction.

The orchestrator talks only to this interface, so Discord / TeamSpeak / a
custom WebRTC SFU can be swapped without touching the agent or the GSI layer.
"""

from __future__ import annotations

from typing import Protocol

from ..models import Roster, Team


class VoiceBackend(Protocol):
    """Contract for a voice provider."""

    async def ensure_channels(self, match_id: str) -> tuple[str, str]:
        """Create (or reuse) the two team channels; return (radiant, dire)."""
        ...

    async def sync_roster(self, match_id: str, roster: Roster) -> None:
        """Move each player to the channel matching their current team."""
        ...

    async def set_mute(self, match_id: str, player_id: str, muted: bool) -> None:
        ...

    async def set_deafen(self, match_id: str, player_id: str, deafened: bool) -> None:
        ...

    async def remove_player(self, match_id: str, player_id: str) -> None:
        ...

    async def delete_channels(self, match_id: str) -> None:
        """Tear the match's channels (and any transport) down for good."""
        ...

    async def enter_post_game(self, match_id: str) -> None:
        """Voice is over, but a text transport may stay up (optional).

        A backend that carries text (the peer mesh) keeps its transport alive so
        the post-match discussion works; one that does not can just tear down.
        Backends without this method get :meth:`delete_channels` instead.
        """
        ...

    async def close(self) -> None:
        ...


def channel_name(match_id: str, team: Team) -> str:
    return f"{team.label} · {match_id}"
