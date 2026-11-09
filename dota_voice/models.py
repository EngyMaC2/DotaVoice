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
    # The 64-bit Steam id, when the game reports one. Kept alongside the short
    # account id so a player can be matched across the GSI, draft and mesh
    # sources even when one of them omits the account id.
    steam_id: str = Field(default="", description="64-bit Steam id, if known")
    hero: str = Field(default="", description="Hero display name, if known")
    # The raw GSI hero id (``npc_dota_hero_lina``). Kept so the UI can build a
    # stable hero-icon URL and tell "no pick yet" apart from "hero unknown".
    hero_id: str = Field(default="", description="Raw GSI hero id, e.g. npc_dota_hero_lina")
    slot: int | None = Field(default=None, description="Player slot index 0..9")
    is_local: bool = Field(default=False, description="Is the local/observed player")
    connected: bool = Field(
        default=False,
        description="Is this player's DotaVoice app on the same voice network",
    )

    @property
    def hero_icon(self) -> str:
        """Hero portrait URL, or empty when no hero has been picked yet.

        Dota serves the portraits from its CDN; the GSI id maps 1:1 to the file
        name, so the UI can show the same icon the game does without shipping
        any art of its own.
        """
        if not self.hero_id.startswith("npc_dota_hero_"):
            return ""
        return (
            "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/"
            f"heroes/{self.hero_id[len('npc_dota_hero_'):]}.png"
        )

    @property
    def hero_label(self) -> str:
        """What to show in place of a hero: the name, or "Выбор…" before a pick."""
        return self.hero or "Выбор…"


class Roster(BaseModel):
    """Team composition for a match."""

    radiant: list[Player] = Field(default_factory=list)
    dire: list[Player] = Field(default_factory=list)
    # Players we know are in the match but whose side we cannot tell yet - a
    # teammate whose app has not announced itself, or a non-spectator slot the
    # game hides. They are still shown, greyed out, so the player can see the
    # full match and who has DotaVoice running.
    unknown: list[Player] = Field(default_factory=list)

    def all_players(self) -> list[Player]:
        return [*self.radiant, *self.dire, *self.unknown]

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
        return list(self.unknown)

    def signature(self) -> str:
        """A stable fingerprint of team assignment used for change detection."""
        parts = [
            f"{p.player_id}:{p.team.value}"
            for p in sorted(
                self.all_players(), key=lambda x: (x.slot is None, x.slot, x.player_id)
            )
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
    # Lifecycle state of the match (ТЗ §5), a `match_state.BridgeState` value.
    # Kept as a plain string so the model stays free of the state-machine module.
    state: str = "idle"
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
    # Post-match window (ТЗ §8.4): the deadline and who has pressed "Выйти".
    post_game_until: float | None = None
    post_game_seconds: float = 12 * 60
    departed: list[str] = Field(default_factory=list)
    revision: int = 0

    def bump(self) -> None:
        self.updated_at = time.time()
        self.revision += 1

    def touch(self) -> None:
        self.last_seen = time.time()

    def players_by_team(self, team: Team) -> list[str]:
        return [p.player_id for p in self.roster.by_team(team)]


class ChatChannel(str, Enum):
    """A text channel in a match (ТЗ §8.3).

    ``RADIANT`` / ``DIRE`` are private to a side and exist while the match is on.
    ``POST_GAME`` is the shared channel that opens for a few minutes after the
    match - the "post-match discussion" - and is the only place text crosses the
    team boundary. Voice never crosses it at any point.
    """

    RADIANT = "radiant"
    DIRE = "dire"
    POST_GAME = "post_game"

    @property
    def label(self) -> str:
        return {
            "radiant": "Свет",
            "dire": "Тьма",
            "post_game": "После матча",
        }[self.value]

    @property
    def shared(self) -> bool:
        """Whether both sides may read and write this channel."""
        return self is ChatChannel.POST_GAME

    @classmethod
    def for_team(cls, team: "Team") -> "ChatChannel":
        if team is Team.DIRE:
            return cls.DIRE
        return cls.RADIANT


class ChatMessage(BaseModel):
    """One text message, stamped with the channel it was written to."""

    id: str = Field(default="", description="Stable id for de-duplication")
    channel: ChatChannel = ChatChannel.RADIANT
    author_id: str = ""
    author_name: str = ""
    team: Team = Team.UNKNOWN
    text: str = ""
    ts: float = Field(default_factory=time.time)


class OverlayEvent(BaseModel):
    """A single message pushed to overlay clients over WebSocket."""

    type: str
    match_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=time.time)
