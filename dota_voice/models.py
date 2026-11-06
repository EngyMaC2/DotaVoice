"""Domain models shared across the service.

These are deliberately framework-agnostic (plain pydantic) so they can be used
by the GSI parser, the voice backends, the overlay, and the agent tools alike.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Team(str, Enum):
    """A side in the match. `UNKNOWN` covers spectators/undefined slots."""

    RADIANT = "radiant"
    DIRE = "dire"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {"radiant": "Силы Света", "dire": "Силы Тьмы"}.get(self.value, "—")

    @classmethod
    def from_gsi(cls, raw: Any) -> "Team":
        """Map a Dota GSI team field (`team_name` / `team`) to a Team.

        GSI reports `"radiant"`, `"dire"`, or `"none"` for the local player's
        own slot when not in a match.
        """
        if raw is None:
            return cls.UNKNOWN
        value = str(raw).strip().lower()
        if value in ("radiant", "2"):
            return cls.RADIANT
        if value in ("dire", "3"):
            return cls.DIRE
        return cls.UNKNOWN


class MatchPhase(str, Enum):
    """Lifecycle phase of a match, derived from `game_state`."""

    NONE = "none"
    HERO_SELECTION = "hero_selection"
    STRATEGY_TIME = "strategy_time"
    PRE_GAME = "pre_game"
    PLAYING = "playing"
    ENDED = "ended"

    @property
    def label(self) -> str:
        return {
            "none": "нет матча",
            "hero_selection": "выбор героев",
            "strategy_time": "подготовка к игре",
            "pre_game": "подготовка",
            "playing": "идёт игра",
            "ended": "матч завершён",
        }[self.value]

    @property
    def wants_voice_room(self) -> bool:
        """Whether the team voice room should exist in this phase.

        The room is created as soon as the rosters are known - hero selection -
        so everyone is already talking during the draft. It is torn down when the
        match ends.
        """
        return self in {
            MatchPhase.HERO_SELECTION,
            MatchPhase.STRATEGY_TIME,
            MatchPhase.PRE_GAME,
            MatchPhase.PLAYING,
        }


class Player(BaseModel):
    """A single participant as reported by the game."""

    player_id: str = Field(description="Stable in-game account id (or slot id)")
    name: str = Field(default="", description="Display name")
    team: Team = Team.UNKNOWN
    hero: str = Field(default="", description="Hero name, if known")
    slot: int | None = Field(default=None, description="Player slot index 0..9")
    is_local: bool = Field(default=False, description="Is the local/observed player")


class Roster(BaseModel):
    """Team composition for a match."""

    radiant: list[Player] = Field(default_factory=list)
    dire: list[Player] = Field(default_factory=list)

    def all_players(self) -> list[Player]:
        return [*self.radiant, *self.dire]

    def team_of(self, player_id: str) -> Team:
        for player in self.all_players():
            if player.player_id == player_id or player.name == player_id:
                return player.team
        return Team.UNKNOWN

    def by_team(self, team: Team) -> list[Player]:
        if team is Team.RADIANT:
            return list(self.radiant)
        if team is Team.DIRE:
            return list(self.dire)
        return []

    def signature(self) -> str:
        """A stable fingerprint of team assignment used for change detection."""
        parts = [
            f"{p.player_id}:{p.team.value}"
            for p in sorted(self.all_players(), key=lambda x: (x.slot is None, x.slot, x.player_id))
        ]
        return "|".join(parts)


class VoiceState(BaseModel):
    """Live voice state for a player (speaking / muted)."""

    player_id: str
    team: Team = Team.UNKNOWN
    speaking: bool = False
    muted: bool = False
    deafened: bool = False
    channel_id: str | None = None
    updated_at: float = Field(default_factory=time.time)


class SessionStatus(str, Enum):
    ACTIVE = "active"
    ENDED = "ended"


class MatchSession(BaseModel):
    """A long-lived "катка" bound to a stable match id.

    Persisting this by `match_id` is what makes the service resilient to game
    restarts: a repeated `match_started` for the same id re-attaches to the
    existing session instead of creating a duplicate.
    """

    match_id: str
    conversation_id: str
    phase: MatchPhase = MatchPhase.PRE_GAME
    status: SessionStatus = SessionStatus.ACTIVE
    roster: Roster = Field(default_factory=Roster)
    radiant_channel_id: str | None = None
    dire_channel_id: str | None = None
    invite_key: str = Field(default="", description="P2WLAN invite for the team")
    voice: dict[str, VoiceState] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    last_seen: float = Field(default_factory=time.time)
    reconnects: int = 0
    ended_at: float | None = None
    revision: int = 0

    def bump(self) -> None:
        self.updated_at = time.time()
        self.revision += 1

    def touch(self) -> None:
        self.last_seen = time.time()

    def players_by_team(self, team: Team) -> list[str]:
        return [p.player_id for p in self.roster.by_team(team)]


class OverlayEvent(BaseModel):
    """A single message pushed to overlay clients over WebSocket."""

    type: str
    match_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=time.time)
