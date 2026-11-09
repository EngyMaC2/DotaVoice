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
