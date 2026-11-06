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
