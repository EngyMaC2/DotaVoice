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

from .heroes import hero_display, hero_npc
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

# Dota numbers a player's slot globally: 0..4 are Radiant, 5..9 are Dire. This is
# the one convention that holds in every payload shape, so it is the fallback
# when neither ``team_name`` nor ``team`` is present.
_RADIANT_SLOTS = range(0, 5)
_DIRE_SLOTS = range(5, 10)


def _team_from_slot(slot: int | None) -> Team:
    if slot is None:
        return Team.UNKNOWN
    if slot in _RADIANT_SLOTS:
        return Team.RADIANT
    if slot in _DIRE_SLOTS:
        return Team.DIRE
    return Team.UNKNOWN


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


def _hero_display(raw: Any) -> str:
    """Readable hero name from a string id, bare key or numeric id."""
    return hero_display(str(raw) if raw is not None else None)


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
    its `allplayers` is empty, but the draft module still lists every pick. Each
    pick carries both a ``pickN_class`` (string name) and a ``pickN_id`` (numeric
    id); either alone is enough, since the id map names the hero. An empty slot is
    a ban or a not-yet-made pick, so it is skipped.
    """
    picks: dict[Team, list[str]] = {}
    for team, key in _DRAFT_TEAM_KEYS.items():
        team_data = draft.get(key)
        if not isinstance(team_data, dict):
            continue
        ordered: list[tuple[int, str]] = []
        for field, value in team_data.items():
            if not field.startswith("pick") or not field.endswith(("_class", "_id")):
                continue
            suffix = "_class" if field.endswith("_class") else "_id"
            index = field[len("pick") : -len(suffix)]
            if not index.isdigit():
                continue
            hero = hero_npc(str(value)) if value not in (None, "") else ""
            if hero:
                ordered.append((int(index), hero))
        if ordered:
            # A class and an id can describe the same pick; keep one per index.
            by_index = dict(sorted(ordered))
            picks[team] = list(by_index.values())
    return picks


# The `player`, `hero` and (in some builds) `allplayers` modules split their
# entries by team: `team2` is Radiant and `team3` is Dire - the same numbering
# the draft uses.
_TEAM_BLOCK_KEYS = {"team2": Team.RADIANT, "team3": Team.DIRE}


def _slot_number(key: Any) -> int | None:
    """Turn a slot key into an index; ``"player7"`` and ``"7"`` both give 7."""
    text = str(key)
    if text.startswith("player"):
        text = text[len("player") :]
    return int(text) if text.isdigit() else None


def _identity_keys(data: dict[str, Any]) -> list[str]:
    """Identity keys for a player, strongest first: account, steam, then name.

    GSI is inconsistent about which id it fills in: a spectator dump carries
    account ids, the local `player` block carries a steam id, and the team blocks
    sometimes carry only a display name. Collecting all of them lets the same
    person be recognised across sources instead of showing up twice - and lets
    the voice network match a peer to the right roster row by Steam id rather
    than by a display name that two players can share.
    """
    keys: list[str] = []
    account = data.get("accountid") or data.get("account_id")
    if account not in (None, 0, "", "0"):
        keys.append(f"account:{account}")
    steam = data.get("steamid") or data.get("steam_id")
    if steam not in (None, "", 0, "0"):
        keys.append(f"steam:{steam}")
    name = str(data.get("name") or data.get("player_name") or "").strip().lower()
    if name:
        keys.append(f"name:{name}")
    return keys


def _iter_team_blocks(block: Any):
    """Yield ``(team, raw_index, data)`` for a team-split block.

    ``raw_index`` is the ``playerN`` number as it arrived, kept so the same key
    can be matched between the ``player`` and ``hero`` modules even when their
    slot numbering differs. A list (some builds send ``team2`` as an array) is
    yielded with its positional index.
    """
    if not isinstance(block, dict):
        return
    for key, team in _TEAM_BLOCK_KEYS.items():
        team_block = block.get(key)
        if isinstance(team_block, list):
            for index, value in enumerate(team_block):
                if isinstance(value, dict):
                    yield team, index, value
            continue
        if not isinstance(team_block, dict):
            continue
        for player_key, value in team_block.items():
            if not isinstance(value, dict):
                continue
            index = _slot_number(player_key)
            if index is None:
                continue
            yield team, index, value


def _global_slot(team: Team, index: int, data: dict[str, Any]) -> int:
    """The 0..9 slot for a team-block entry.

    Prefer the game's own ``team_slot`` (0..4 within the team); fall back to the
    ``playerN`` key, whose numbering is per-team in some builds and global in
    others - so a Dire key below 5 is shifted into the Dire half.
    """
    team_slot = data.get("team_slot")
    if isinstance(team_slot, int) and 0 <= team_slot <= 4:
        return (5 if team is Team.DIRE else 0) + team_slot
    if team is Team.DIRE and index < 5:
        return index + 5
    return index


def _hero_from_value(value: Any) -> str:
    """Canonical hero id from any of the shapes GSI uses for a hero.

    A ``hero`` module entry is ``{"name": "npc_dota_hero_lina", "id": 5}`` (or a
    bare string, or a bare number); all of them resolve through the id map.
    """
    if isinstance(value, dict):
        return hero_npc(value.get("name") or value.get("hero_id") or value.get("id"))
    if isinstance(value, (str, int)):
        return hero_npc(value)
    return ""


def _slot_from_data(data: dict[str, Any], team: Team) -> int | None:
    """Best-effort global 0..9 slot from a player block's own fields."""
    player_slot = data.get("player_slot")
    if isinstance(player_slot, int) and 0 <= player_slot <= 9:
        return player_slot
    team_slot = data.get("team_slot")
    if isinstance(team_slot, int) and 0 <= team_slot <= 4 and team is not Team.UNKNOWN:
        return (5 if team is Team.DIRE else 0) + team_slot
    return None


def _collect_players(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Fold every source that describes a participant into merged records.

    Dota reports players in several shapes depending on the mode and build:

    * a spectator dump: flat ``allplayers`` keyed by global slot ``"0".."9"``
      (a list of slots is also accepted);
    * some spectator builds: ``allplayers.team2/team3`` -> ``playerN``;
    * a normal client: only its own ``player`` block, with its teammates under
      ``player.team2/team3`` -> ``playerN``;
    * SourceTV / broadcast tooling: a flat ``players`` array.

    A normal client's own ``allplayers`` is empty, so the team blocks are what
    make the whole match appear; a spectator's ``allplayers`` carries everyone
    but often only a numeric ``hero_id`` and no ``team_name``, so the side is
    inferred from the global slot. Merging every source is what lets the app show
    all ten players and each one's hero in every mode.
    """
    all_players = payload.get("allplayers") or {}
    player_block = payload.get("player") or {}
    hero_block = payload.get("hero") or {}

    hero_by_key: dict[tuple[Team, int], str] = {}
    for team, index, value in _iter_team_blocks(hero_block):
        hero = _hero_from_value(value)
        if hero:
            hero_by_key[(team, index)] = hero

    records: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}

    def find(keys: list[str]) -> dict[str, Any] | None:
        # Prefer an exact id match; only fall back to the display name, the
        # weakest key, which could in principle collide.
        for key in keys:
            if not key.startswith("name:") and key in by_key:
                return by_key[key]
        for key in keys:
            if key.startswith("name:") and key in by_key:
                return by_key[key]
        return None

    def add(data, *, team=None, slot=None, hero="", is_local=False) -> None:
        if not isinstance(data, dict):
            return
        keys = _identity_keys(data)
        record = find(keys)
        if record is None:
            record = {
                "data": {},
                "team": Team.UNKNOWN,
                "slot": None,
                "hero": "",
                "is_local": False,
            }
            records.append(record)
        if slot is not None and record["slot"] is None:
            record["slot"] = slot
        # Side: an explicit team wins; otherwise the block's own field; and if
        # that is still unknown, the global slot tells the side (0..4 Radiant,
        # 5..9 Dire) - the one convention every payload shape agrees on.
        if team is not None and team is not Team.UNKNOWN:
            record["team"] = team
        elif record["team"] is Team.UNKNOWN:
            own = Team.from_gsi(data.get("team_name") or data.get("team"))
            record["team"] = own if own is not Team.UNKNOWN else _team_from_slot(record["slot"])
        if hero and not record["hero"]:
            record["hero"] = hero
        if is_local:
            record["is_local"] = True
        for field, value in data.items():
            if field in _TEAM_BLOCK_KEYS:
                continue
            if value not in (None, "", 0, []):
                record["data"][field] = value
        for key in keys:
            by_key.setdefault(key, record)

    # 0. Broadcast tooling: a flat `players` array (also tolerate an allplayers
    #    list). Slots are global and the side may be a name or a number.
    for source in (payload.get("players"), all_players if isinstance(all_players, list) else None):
        if not isinstance(source, list):
            continue
        for entry in source:
            if not isinstance(entry, dict):
                continue
            team = Team.from_gsi(entry.get("team_name") or entry.get("team"))
            slot = _slot_from_data(entry, team)
            if slot is None:
                raw_slot = entry.get("player_slot", entry.get("slot"))
                if isinstance(raw_slot, int) and 0 <= raw_slot <= 9:
                    slot = raw_slot
            add(entry, team=team, slot=slot, hero=_hero_from_value(entry.get("hero_id")))

    # 1. Spectator dump, flat: allplayers keyed by global slot.
    if isinstance(all_players, dict):
        for key, value in all_players.items():
            if not isinstance(value, dict) or key in _TEAM_BLOCK_KEYS:
                continue
            slot = _slot_number(key)
            if slot is None:
                continue
            add(
                value,
                team=Team.from_gsi(value.get("team_name") or value.get("team")),
                slot=slot,
                hero=_hero_from_value(value.get("hero_name") or value.get("hero_id")),
            )

    # 2. Spectator dump, team-split: allplayers.team2/team3 -> playerN.
    for team, index, value in _iter_team_blocks(all_players):
        add(
            value,
            team=team,
            slot=_global_slot(team, index, value),
            hero=hero_by_key.get((team, index), ""),
        )

    # 3. Normal client: teammates live in player.team2/team3 -> playerN.
    for team, index, value in _iter_team_blocks(player_block):
        add(
            value,
            team=team,
            slot=_global_slot(team, index, value),
            hero=hero_by_key.get((team, index), ""),
        )

    # 4. The local player's own block.
    if isinstance(player_block, dict) and player_block:
        team = Team.from_gsi(player_block.get("team_name") or player_block.get("team"))
        add(
            player_block,
            team=team,
            slot=_slot_from_data(player_block, team),
            hero=_hero_from_value(hero_block),
            is_local=True,
        )

    # The local hero sits at the top level of the `hero` module in every mode.
    local_hero = _hero_from_value(hero_block)
    if local_hero:
        for record in records:
            if record["is_local"] and not record["hero"]:
                record["hero"] = local_hero

    return records


def _account_from_steam(steam: Any) -> str:
    """Dota account id from a SteamID64 (``7656119...``), or "" if not convertible.

    A normal client's own ``player`` block carries only a SteamID64, while
    ``allplayers`` and the voice network key players by the numeric Dota account
    id. Deriving one from the other keeps a player's identity the same on both
    sides, so the local row is not duplicated and peers match the right row.
    """
    text = str(steam or "").strip()
    if not text.isdigit():
        return ""
    value = int(text)
    account = value - 76561197960265728
    return str(account) if account > 0 else ""


def _player_from_record(record: dict[str, Any]) -> Player:
    data = record["data"]
    slot = record["slot"]
    hero_id = hero_npc(record["hero"] or data.get("hero_name") or data.get("hero"))
    steam_id = str(data.get("steamid") or data.get("steam_id") or "")
    account = data.get("accountid") or data.get("account_id") or _account_from_steam(steam_id)
    player_id = str(account or steam_id or (slot if slot is not None else ""))
    return Player(
        player_id=player_id or "local",
        name=str(data.get("name") or data.get("player_name") or ""),
        team=record["team"],
        steam_id=steam_id,
        hero=hero_display(hero_id),
        hero_id=hero_id,
        slot=slot,
        is_local=bool(record["is_local"]),
        # Our own client is always on the network; teammates light up when their
        # app announces itself over discovery.
        connected=bool(record["is_local"]),
    )


def parse_gsi(payload: dict[str, Any]) -> GameStateUpdate:
    """Parse a raw GSI JSON body into a :class:`GameStateUpdate`."""
    map_data = payload.get("map") or {}
    draft = payload.get("draft") or {}
    auth = payload.get("auth") or {}

    game_state = str(map_data.get("game_state") or "")
    match_id = map_data.get("matchid") or map_data.get("match_id")
    if match_id is not None:
        match_id = str(match_id).strip() or None

    is_spectating = "spectat" in game_state.lower()

    roster = Roster()
    local_player: Player | None = None
    for record in _collect_players(payload):
        player = _player_from_record(record)
        if player.team is Team.RADIANT:
            roster.radiant.append(player)
        elif player.team is Team.DIRE:
            roster.dire.append(player)
        else:
            # A slot with no side yet (or a side we cannot read). Keep it so the
            # overlay can show the whole match and who has DotaVoice running.
            roster.unknown.append(player)
        if player.is_local:
            local_player = player

    phase = _phase_from_state(game_state, is_spectating)

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
