"""Parser for Dota 2 Game State Integration (GSI) payloads.

The GSI HTTP POST body looks roughly like::

    {
      "provider": {"name": "Dota 2", "appid": 570, "version": 40},
      "map": {"matchid": "7123456789", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
              "game_time": 512, "clock_time": 512},
      "player": {"team_name": "radiant", "name": "Me", "steamid": "..."},
      "allplayers": {"<slot>": {"name": "...", "team_name": "dire", "hero_name": "npc_dota_hero_lina"}},
      "hero": {...},
      "auth": {"token": "..."}
    }

We keep parsing defensive: any field can be absent, and the result is a
normalized :class:`GameStateUpdate` that the orchestrator can act on.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .models import MatchPhase, Player, Roster, Team

# game_state values seen in practice
_PLAYING_STATES = {
    "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
    "DOTA_GAMERULES_STATE_PRE_GAME",
}
# Draft and preparation: rosters exist, so the voice room can already be built.
_DRAFT_STATES = {
    "DOTA_GAMERULES_STATE_HERO_SELECTION",
    "DOTA_GAMERULES_STATE_STRATEGY_TIME",
    "DOTA_GAMERULES_STATE_TEAM_SHOWCASE",
    "DOTA_GAMERULES_STATE_WAIT_FOR_PLAYERS_TO_LOAD",
}
_ENDED_STATES = {
    "DOTA_GAMERULES_STATE_POST_GAME",
    "DOTA_GAMERULES_STATE_GAME_ENDED",
}


class GameStateUpdate(BaseModel):
    """Normalized view of one GSI payload."""

    match_id: str | None = None
    phase: MatchPhase = MatchPhase.NONE
    game_state_raw: str = ""
    is_spectating: bool = False
    local_player: Player | None = None
    roster: Roster = Field(default_factory=Roster)
    clock_time: int | None = None
    game_time: int | None = None
    auth_token: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def player_ids(self) -> list[str]:
        return [p.player_id for p in self.roster.all_players()]


def _hero_display(raw: str | None) -> str:
    if not raw:
        return ""
    name = raw
    for prefix in ("npc_dota_hero_", "npc_dota_"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    return name.replace("_", " ").title()


def _phase_from_state(game_state: str, is_spectating: bool) -> MatchPhase:
    state = (game_state or "").upper()
    if state in _ENDED_STATES:
        return MatchPhase.ENDED
    if state in _DRAFT_STATES:
        if state.endswith("HERO_SELECTION"):
            return MatchPhase.HERO_SELECTION
        if state.endswith("STRATEGY_TIME"):
            return MatchPhase.STRATEGY_TIME
        return MatchPhase.PRE_GAME
    if state in _PLAYING_STATES:
        # Pre-game is still "in a match" but not yet playing.
        if state.endswith("PRE_GAME"):
            return MatchPhase.PRE_GAME
        return MatchPhase.PLAYING
    if is_spectating:
        return MatchPhase.PLAYING
    return MatchPhase.NONE


# Dota's `draft` module keys each team by number: 2 = Radiant, 3 = Dire. Picks
# arrive as `pickN_class` (the hero id) in draft order, which is the same order
# as the team's slots - so a pick can be joined to a roster player by position
# even though the draft carries no account id.
_DRAFT_TEAM_KEYS = {Team.RADIANT: "team2", Team.DIRE: "team3"}


def _draft_heroes(draft: dict[str, Any]) -> dict[Team, list[str]]:
    """Extract each team's picked heroes in pick order from the draft module.

    This is how a normal (non-spectating) client learns its teammates' heroes:
    its `allplayers` is empty, but the draft module still lists every pick. An
    empty slot is a ban or a not-yet-made pick, so it is skipped.
    """
    picks: dict[Team, list[str]] = {}
    for team, key in _DRAFT_TEAM_KEYS.items():
        team_data = draft.get(key)
        if not isinstance(team_data, dict):
            continue
        ordered: list[tuple[int, str]] = []
        for field, value in team_data.items():
            if not field.startswith("pick") or not field.endswith("_class"):
                continue
            index = field[len("pick") : -len("_class")]
            if not index.isdigit():
                continue
            hero = str(value or "")
            if hero.startswith("npc_dota_hero_"):
                ordered.append((int(index), hero))
        if ordered:
            picks[team] = [hero for _, hero in sorted(ordered)]
    return picks


def _player_from_slot(slot: str | int, data: dict[str, Any], is_local: bool = False) -> Player:
    account_id = data.get("account_id")
    player_id = str(account_id) if account_id not in (None, 0, "") else str(slot)
    hero_id = str(data.get("hero_name") or data.get("hero") or "")
    return Player(
        player_id=player_id,
        name=str(data.get("name") or data.get("player_name") or ""),
        team=Team.from_gsi(data.get("team_name") or data.get("team")),
        steam_id=str(data.get("steamid") or ""),
        hero=_hero_display(hero_id),
        hero_id=hero_id,
        slot=int(slot) if str(slot).isdigit() else None,
        is_local=is_local,
        # Our own client is always on the network; teammates light up when their
        # app announces itself over discovery.
        connected=is_local,
    )


def parse_gsi(payload: dict[str, Any]) -> GameStateUpdate:
    """Parse a raw GSI JSON body into a :class:`GameStateUpdate`."""
    map_data = payload.get("map") or {}
    player_data = payload.get("player") or {}
    all_players = payload.get("allplayers") or {}
    draft = payload.get("draft") or {}
    auth = payload.get("auth") or {}

    game_state = str(map_data.get("game_state") or "")
    match_id = map_data.get("matchid") or map_data.get("match_id")
    if match_id is not None:
        match_id = str(match_id).strip() or None

    is_spectating = "spectat" in game_state.lower()

    # The local player's identity, so we can flag their slot in `allplayers`.
    local_steam = str(player_data.get("steamid") or "")
    local_account = str(player_data.get("accountid") or player_data.get("account_id") or "")
    local_name = str(player_data.get("name") or "")

    roster = Roster()
    local_player: Player | None = None

    for slot, data in all_players.items():
        if not isinstance(data, dict):
            continue
        entry_steam = str(data.get("steamid") or "")
        entry_account = str(data.get("accountid") or data.get("account_id") or "")
        is_local = bool(data.get("is_local") or data.get("local")) or (
            (local_steam and entry_steam == local_steam)
            or (local_account and entry_account == local_account)
            or (local_name and str(data.get("name") or "") == local_name)
        )
        player = _player_from_slot(slot, data, is_local=is_local)
        if player.team is Team.RADIANT:
            roster.radiant.append(player)
        elif player.team is Team.DIRE:
            roster.dire.append(player)
        else:
            # A slot with no side yet (or a side we cannot read). Keep it so the
            # overlay can show the whole match and who has DotaVoice running.
            roster.unknown.append(player)
        if is_local:
            local_player = player

    # Without `allplayers` (a normal player, not a spectator) we only know the
    # local player; seed the roster with them so the app can still join its own
    # team channel. Teammates show up as they connect their own clients.
    if not all_players and player_data:
        local_player = Player(
            player_id=str(
                player_data.get("accountid")
                or player_data.get("account_id")
                or player_data.get("steamid")
                or "local"
            ),
            name=local_name,
            team=Team.from_gsi(player_data.get("team_name") or player_data.get("team")),
            steam_id=str(player_data.get("steamid") or ""),
            slot=player_data.get("team_slot"),
            is_local=True,
            connected=True,
        )
        if local_player.team is Team.RADIANT:
            roster.radiant.append(local_player)
        elif local_player.team is Team.DIRE:
            roster.dire.append(local_player)
        else:
            roster.unknown.append(local_player)

    phase = _phase_from_state(game_state, is_spectating)

    # The local player's own hero is always in the top-level `hero` module, which
    # is the one source that works in every mode. Prefer it for our slot.
    hero_module = payload.get("hero") or {}
    local_hero = str(hero_module.get("name") or "")
    if local_hero and local_player is not None and not local_player.hero_id:
        local_player.hero = _hero_display(local_hero)
        local_player.hero_id = local_hero

    # Fill any remaining gaps from the draft module. Picks carry no account id,
    # so they are joined to a team's slots in pick order - but only for a team we
    # can see in full, otherwise a lone local player would be handed a random
    # teammate's pick.
    for team, heroes in _draft_heroes(draft).items():
        players = roster.by_team(team)
        if len(players) < 5:
            continue
        ordered = sorted(players, key=lambda p: (p.slot is None, p.slot or 0))
        for player, hero_id in zip(ordered, heroes, strict=False):
            if not player.hero_id:
                player.hero = _hero_display(hero_id)
                player.hero_id = hero_id

    # A match is "live" only when we have an id; GSI reports matchid even for
    # spectated games, which is exactly what we want to bind the session to.
    if match_id is None and phase is MatchPhase.NONE:
        phase = MatchPhase.NONE

    return GameStateUpdate(
        match_id=match_id,
        phase=phase,
        game_state_raw=game_state,
        is_spectating=is_spectating,
        local_player=local_player,
        roster=roster,
        clock_time=map_data.get("clock_time"),
        game_time=map_data.get("game_time"),
        auth_token=auth.get("token"),
        raw=payload,
    )
