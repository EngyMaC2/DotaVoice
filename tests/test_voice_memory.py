"""Tests for the in-memory voice backend semantics."""

from __future__ import annotations

from dota_voice.models import Player, Roster, Team
from dota_voice.voice.memory import MemoryVoiceBackend


def _roster() -> Roster:
    return Roster(
        radiant=[
            Player(player_id="1", name="A", team=Team.RADIANT),
            Player(player_id="2", name="B", team=Team.RADIANT),
        ],
        dire=[Player(player_id="3", name="C", team=Team.DIRE)],
    )


async def test_channels_are_isolated_per_team():
    backend = MemoryVoiceBackend()
    await backend.ensure_channels("m1")
    await backend.sync_roster("m1", _roster())

    assert backend.members("m1", Team.RADIANT) == {"1", "2"}
    assert backend.members("m1", Team.DIRE) == {"3"}
    # No overlap: a player is in exactly one channel.
    assert not (backend.members("m1", Team.RADIANT) & backend.members("m1", Team.DIRE))


async def test_ensure_channels_is_idempotent():
    backend = MemoryVoiceBackend()
    first = await backend.ensure_channels("m1")
    second = await backend.ensure_channels("m1")
    assert first == second


async def test_team_switch_moves_member():
    backend = MemoryVoiceBackend()
    await backend.sync_roster("m1", _roster())

    moved = _roster()
    moved.dire.append(moved.radiant.pop(1))  # player 2 -> dire
    moved.dire[-1].team = Team.DIRE
    await backend.sync_roster("m1", moved)

    assert backend.members("m1", Team.RADIANT) == {"1"}
    assert backend.members("m1", Team.DIRE) == {"3", "2"}
    assert backend.channel_of("m1", "2") == "m1-dire"


async def test_mute_state_roundtrip():
    backend = MemoryVoiceBackend()
    await backend.sync_roster("m1", _roster())
    await backend.set_mute("m1", "1", True)
    assert backend.state_of("m1", "1").muted is True
    await backend.set_mute("m1", "1", False)
    assert backend.state_of("m1", "1").muted is False


async def test_removed_player_leaves_channel():
    backend = MemoryVoiceBackend()
    await backend.sync_roster("m1", _roster())
    smaller = Roster(radiant=[Player(player_id="1", team=Team.RADIANT)], dire=[])
    await backend.sync_roster("m1", smaller)
    assert backend.members("m1", Team.RADIANT) == {"1"}
    assert backend.channel_of("m1", "2") is None
