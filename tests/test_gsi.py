"""Tests for the GSI payload parser."""

from __future__ import annotations

from dota_voice.gsi import parse_gsi
from dota_voice.models import MatchPhase, Team

from scripts.simulate import payload


def test_parses_match_id_and_phase():
    update = parse_gsi(payload("7123456789", clock=120))
    assert update.match_id == "7123456789"
    assert update.phase is MatchPhase.PLAYING
    assert update.clock_time == 120


def test_splits_players_into_teams():
    update = parse_gsi(payload("1"))
    assert len(update.roster.radiant) == 3
    assert len(update.roster.dire) == 3
    assert update.roster.radiant[0].name == "Aegis"
    assert update.roster.radiant[0].hero == "Juggernaut"
    assert update.roster.radiant[0].player_id == "101"
    assert update.roster.team_of("201") is Team.DIRE


def test_pre_game_and_post_game_phases():
    pre = parse_gsi(payload("1", "DOTA_GAMERULES_STATE_PRE_GAME"))
    post = parse_gsi(payload("1", "DOTA_GAMERULES_STATE_POST_GAME"))
    assert pre.phase is MatchPhase.PRE_GAME
    assert post.phase is MatchPhase.ENDED


def test_missing_match_id_yields_none():
    body = payload("1")
    del body["map"]["matchid"]
    body["map"]["game_state"] = "DOTA_GAMERULES_STATE_INIT"
    update = parse_gsi(body)
    assert update.match_id is None
    assert update.phase is MatchPhase.NONE


def test_auth_token_extracted():
    update = parse_gsi(payload("1", token="s3cr3t"))
    assert update.auth_token == "s3cr3t"


def test_roster_signature_changes_with_teams():
    a = parse_gsi(payload("1")).roster
    b = parse_gsi(payload("1")).roster.model_copy(deep=True)
    assert a.signature() == b.signature()
    b.radiant[0].team = Team.DIRE
    assert a.signature() != b.signature()


def test_hero_selection_phase_is_recognised():
    update = parse_gsi(
        payload(
            "1",
            game_state="DOTA_GAMERULES_STATE_HERO_SELECTION",
        )
    )
    assert update.phase is MatchPhase.HERO_SELECTION
    assert update.phase.wants_voice_room is True


def test_strategy_time_phase_is_recognised():
    update = parse_gsi(
        payload("1", game_state="DOTA_GAMERULES_STATE_STRATEGY_TIME")
    )
    assert update.phase is MatchPhase.STRATEGY_TIME
    assert update.phase.wants_voice_room is True


def test_the_voice_room_is_not_wanted_before_or_after_a_match():
    assert MatchPhase.NONE.wants_voice_room is False
    assert MatchPhase.ENDED.wants_voice_room is False
    assert MatchPhase.PLAYING.wants_voice_room is True


def test_every_phase_has_a_russian_label():
    for phase in MatchPhase:
        assert phase.label
        assert phase.label != phase.value


# ---------------------------------------------------------------------------
# heroes and team names (addendum to ТЗ §1.1)
# ---------------------------------------------------------------------------


def test_players_carry_a_hero_icon_and_team_label():
    """Each row needs an icon, a hero name and the side's name."""
    update = parse_gsi(payload("1"))
    juggernaut = update.roster.radiant[0]
    assert juggernaut.hero_id == "npc_dota_hero_juggernaut"
    assert juggernaut.hero == "Juggernaut"
    assert juggernaut.hero_label == "Juggernaut"
    assert juggernaut.hero_icon.endswith("/heroes/juggernaut.png")
    assert juggernaut.team.label == "Силы Света"


def test_a_player_without_a_pick_shows_the_placeholder():
    """Before a pick there is no hero: the label must read "Выбор…", no icon."""
    body = payload("1")
    body["allplayers"]["0"]["hero_name"] = ""
    body["allplayers"]["0"]["hero_id"] = None
    # The local player's own hero is also in the top-level `hero` module.
    body["hero"]["name"] = ""
    player = parse_gsi(body).roster.radiant[0]
    assert player.hero == ""
    assert player.hero_id == ""
    assert player.hero_icon == ""
    assert player.hero_label == "Выбор…"


def test_heroes_are_filled_from_the_draft_module():
    """A normal client has no allplayers: heroes come from the draft instead."""
    body = payload("1", draft=True)
    del body["allplayers"]
    update = parse_gsi(body)
    # The draft carries no account id, so the pick joins the slot in order.
    assert update.roster.radiant[0].hero_id == "npc_dota_hero_juggernaut"
    assert update.roster.radiant[0].hero == "Juggernaut"


def test_draft_does_not_hand_a_pick_to_a_lone_player():
    """A partial roster must not be given a teammate's hero by mistake."""
    body = payload("1", draft=True)
    del body["allplayers"]
    # Only the local player is known; the draft lists five Radiant picks.
    update = parse_gsi(body)
    players = update.roster.all_players()
    assert len(players) == 1
    # Its own hero is the top-level `hero` module, not the first draft pick.
    assert players[0].hero_id == "npc_dota_hero_juggernaut"


def test_steam_id_is_kept_from_allplayers():
    update = parse_gsi(payload("1"))
    # The simulator's allplayers entries carry account ids, not steam ids.
    assert update.roster.radiant[0].player_id == "101"
    assert update.roster.team_of("201") is Team.DIRE


# ---------------------------------------------------------------------------
# The shapes a *normal* client sends: teammates under player.team2/team3
# ---------------------------------------------------------------------------


def test_a_normal_client_shows_all_ten_players():
    """The bug: only the local player showed, the other nine were ignored.

    A normal client hides the other players in ``allplayers`` but still lists
    them under ``player.team2``/``player.team3``; reading that block is what
    makes the whole match appear.
    """
    from scripts.simulate import payload_teams

    update = parse_gsi(payload_teams("1"))
    assert len(update.roster.radiant) == 3
    assert len(update.roster.dire) == 3
    assert len(update.roster.all_players()) == 6
    # Identity is the account id, derived from the teammate's SteamID64, so the
    # same person is one row no matter which source reported them.
    assert update.roster.team_of("201") is Team.DIRE


def test_teammate_heroes_come_from_the_hero_team_block():
    """Each teammate's hero is mirrored under ``hero.team2``/``hero.team3``."""
    from scripts.simulate import payload_teams

    update = parse_gsi(payload_teams("1"))
    by_name = {p.name: p for p in update.roster.all_players()}
    assert by_name["Frost"].hero_id == "npc_dota_hero_crystal_maiden"
    assert by_name["Frost"].hero == "Crystal Maiden"
    assert by_name["Bane"].hero_id == "npc_dota_hero_bane"
    assert by_name["Bane"].team is Team.DIRE


def test_the_local_player_is_flagged_once_across_sources():
    """The local player appears in the player block *and* its team block."""
    from scripts.simulate import payload_teams

    update = parse_gsi(payload_teams("1"))
    locals_ = [p for p in update.roster.all_players() if p.is_local]
    assert len(locals_) == 1
    assert locals_[0].name == "Aegis"
    assert update.local_player is not None
    assert update.local_player.hero == "Juggernaut"


def test_team_slot_places_a_dire_teammate_in_the_dire_half():
    """Dire entries keyed per-team (player0..4) must land on slots 5..9."""
    body = {
        "map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
        "player": {
            "name": "Me",
            "team_name": "radiant",
            "team_slot": 0,
            "team3": {"player0": {"name": "Foe", "team_slot": 0}},
        },
        "hero": {},
        "allplayers": {},
    }
    update = parse_gsi(body)
    foe = next(p for p in update.roster.dire if p.name == "Foe")
    assert foe.slot == 5


def test_a_spectator_team_split_allplayers_is_read():
    """Some spectator builds nest ``allplayers`` under team2/team3 too."""
    body = {
        "map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
        "player": {},
        "hero": {},
        "allplayers": {
            "team2": {"player0": {"name": "A", "accountid": 11, "hero_id": 1}},
            "team3": {"player5": {"name": "B", "accountid": 22, "hero_id": 2}},
        },
    }
    update = parse_gsi(body)
    assert update.roster.radiant[0].player_id == "11"
    assert update.roster.dire[0].player_id == "22"
    assert update.roster.dire[0].slot == 5


def test_the_same_player_is_not_added_twice_from_two_shapes():
    """A flat allplayers entry and the player block describe one person."""
    body = payload("1")
    body["allplayers"]["0"] = {
        "accountid": 101,
        "name": "Aegis",
        "team_name": "radiant",
        "hero_name": "npc_dota_hero_juggernaut",
    }
    update = parse_gsi(body)
    aegis = [p for p in update.roster.radiant if p.name == "Aegis"]
    assert len(aegis) == 1
    assert aegis[0].is_local


# ---------------------------------------------------------------------------
# The shapes a *real* Dota 2 client sends: numeric hero ids, slot-only sides
# ---------------------------------------------------------------------------


def test_a_numeric_hero_id_is_named_from_the_id_map():
    """A spectator dump reports ``hero_id`` as a number, not a string name."""
    body = {
        "map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
        "player": {"steamid": "76561198000000001", "name": "Me", "team_name": "radiant"},
        "hero": {},
        "allplayers": {
            "0": {"account_id": 1, "name": "Me", "hero_id": 8},
            "5": {"account_id": 6, "name": "Foe", "hero_id": 77},
        },
    }
    update = parse_gsi(body)
    me = next(p for p in update.roster.radiant if p.name == "Me")
    foe = next(p for p in update.roster.dire if p.name == "Foe")
    assert me.hero_id == "npc_dota_hero_juggernaut"
    assert me.hero == "Juggernaut"
    assert me.hero_icon.endswith("/heroes/juggernaut.png")
    assert foe.hero == "Lycan"


def test_a_hero_id_is_read_from_the_hero_module_too():
    """``hero.team2.playerN.id`` is a number in a normal client's payload."""
    body = {
        "map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
        "player": {
            "steamid": "76561198000000001",
            "name": "Me",
            "team_name": "radiant",
            "team_slot": 0,
            "team2": {"player1": {"name": "Ally", "team_slot": 1}},
        },
        "hero": {"team2": {"player1": {"id": 5, "name": "npc_dota_hero_crystal_maiden"}}},
        "allplayers": {},
    }
    update = parse_gsi(body)
    ally = next(p for p in update.roster.radiant if p.name == "Ally")
    assert ally.hero == "Crystal Maiden"
    assert ally.hero_id == "npc_dota_hero_crystal_maiden"


def test_a_slot_alone_tells_the_side():
    """A flat allplayers with no ``team_name`` is split 0..4 / 5..9."""
    body = {
        "map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
        # No local player block: this is a pure spectator dump.
        "hero": {},
        "allplayers": {
            str(slot): {"account_id": 100 + slot, "name": f"P{slot}", "hero_id": slot + 1}
            for slot in range(10)
        },
    }
    update = parse_gsi(body)
    assert len(update.roster.radiant) == 5
    assert len(update.roster.dire) == 5
    assert update.roster.unknown == []
    assert all(p.slot is not None and p.slot < 5 for p in update.roster.radiant)
    assert all(p.slot is not None and p.slot >= 5 for p in update.roster.dire)


def test_a_source_tv_players_array_is_read():
    """Broadcast tooling sends a flat ``players`` array with numeric sides."""
    body = {
        "map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
        "hero": {},
        "allplayers": {},
        "players": [
            {"player_slot": i, "name": f"P{i}", "hero_id": i + 1, "account_id": 500 + i,
             "team": 2 if i < 5 else 3}
            for i in range(10)
        ],
    }
    update = parse_gsi(body)
    assert len(update.roster.radiant) == 5
    assert len(update.roster.dire) == 5
    assert update.roster.radiant[0].hero == "Anti-Mage"


def test_the_local_account_id_is_derived_from_its_steam_id():
    """The local block's SteamID64 becomes the same account id allplayers uses."""
    account = 39734373
    body = {
        "map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
        "player": {"steamid": str(76561197960265728 + account), "name": "Me",
                   "team_name": "radiant", "team_slot": 0},
        "hero": {"name": "npc_dota_hero_lina"},
        "allplayers": {
            "0": {"account_id": account, "name": "Me", "hero_id": 5},
            "1": {"account_id": 39734374, "name": "Ally", "hero_id": 6},
        },
    }
    update = parse_gsi(body)
    local = [p for p in update.roster.all_players() if p.is_local]
    assert len(local) == 1
    assert local[0].player_id == str(account)
    assert local[0].steam_id == str(76561197960265728 + account)
    # The ally is a distinct person, not folded into the local row.
    assert len(update.roster.radiant) == 2


def test_allplayers_keyed_player0_to_player9_yields_ten_players():
    """The exact shape Dota sends: a dict keyed ``player0``..``player9``.

    Each entry carries ``name``, ``team_name``, ``hero_name`` and ``steamid``.
    Reading the object's keys (instead of its values) is the classic bug that
    leaves the roster empty, so this pins the full ten-player result.
    """
    def steam(i: int) -> str:
        return str(76561197960265728 + 100000 + i)

    allplayers = {}
    for i in range(10):
        allplayers[f"player{i}"] = {
            "name": f"Player{i}",
            "team_name": "radiant" if i < 5 else "dire",
            "hero_name": "npc_dota_hero_viper" if i < 5 else "npc_dota_hero_lina",
            "steamid": steam(i),
        }
    body = {
        "map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
        "allplayers": allplayers,
    }
    update = parse_gsi(body)
    assert len(update.roster.radiant) == 5
    assert len(update.roster.dire) == 5
    assert len(update.roster.all_players()) == 10
    # Names, sides and heroes all come through for every player.
    radiant = {p.name: p for p in update.roster.radiant}
    assert set(radiant) == {f"Player{i}" for i in range(5)}
    assert all(p.hero == "Viper" for p in update.roster.radiant)
    assert all(p.hero == "Lina" for p in update.roster.dire)
    assert all(p.steam_id.startswith("7656119") for p in update.roster.all_players())


def test_a_hero_the_id_map_does_not_know_still_gets_a_name():
    """A brand-new hero is not in the bundled id map: name it from the npc id.

    The map cannot list a hero Valve added after this build, but the raw
    ``npc_dota_hero_*`` id is still readable - so the row shows "Brand New Hero"
    instead of being stuck on "Выбор…", which would look like no pick.
    """
    body = {
        "map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
        "allplayers": {
            "0": {"account_id": 1, "name": "A", "team_name": "radiant",
                  "hero_name": "npc_dota_hero_brand_new_hero"},
        },
    }
    player = parse_gsi(body).roster.radiant[0]
    assert player.hero == "Brand New Hero"
    assert player.hero_label == "Brand New Hero"
    # No icon (we have no canonical id), but the name is shown, not "Выбор…".
    assert player.hero_icon == ""


def test_an_allplayers_key_we_cannot_number_still_yields_a_row():
    """A key that is neither "N" nor "playerN" must not drop the player.

    The flat reader used to skip such a key outright, which would hide players
    if a future build renamed the keys. The entry's own ``player_slot`` recovers
    the row, so the roster stays complete.
    """
    body = {
        "map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
        "allplayers": {
            f"slot{i}": {"account_id": 100 + i, "name": f"P{i}", "hero_id": 5,
                         "player_slot": i}
            for i in range(10)
        },
    }
    update = parse_gsi(body)
    assert len(update.roster.radiant) == 5
    assert len(update.roster.dire) == 5
    assert len(update.roster.all_players()) == 10


def test_a_draft_pick_by_numeric_id_is_named():
    """The draft also carries ``pickN_id``; the id map names it."""
    body = {
        "map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_HERO_SELECTION"},
        "hero": {},
        # A full lobby with no heroes yet: the draft is the only source.
        "allplayers": {
            str(slot): {"account_id": 100 + slot, "name": f"P{slot}"} for slot in range(10)
        },
        "draft": {"team2": {"pick0_id": 8, "pick1_id": 5}},
    }
    update = parse_gsi(body)
    by_slot = {p.slot: p for p in update.roster.radiant}
    assert by_slot[0].hero == "Juggernaut"
    assert by_slot[1].hero == "Crystal Maiden"
