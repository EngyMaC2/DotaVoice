"""Tests for session idempotency, team switching, mute, and reconnects.

These exercise the real agent layer: every session creates a real OpenHands
conversation and the tools run through the SDK's ``execute_tool`` path.
"""

from __future__ import annotations

import asyncio

import pytest

from dota_voice.agent import Orchestrator
from dota_voice.agent.runtime import conversation_id_for_match
from dota_voice.gsi import parse_gsi
from dota_voice.models import Team

from scripts.simulate import DIRE, RADIANT, payload


async def _handle(orchestrator, body):
    return await orchestrator.handle(parse_gsi(body))


@pytest.fixture
def orchestrator(manager) -> Orchestrator:
    return Orchestrator(manager)


async def test_first_match_creates_session_and_channels(manager, voice, orchestrator):
    match_id = "7123456789"
    result = await _handle(orchestrator, payload(match_id, "DOTA_GAMERULES_STATE_PRE_GAME"))

    assert result["action"] == "handled"
    assert result["is_new"] is True
    runtime = manager.get_runtime(match_id)
    assert runtime is not None
    assert runtime.session.radiant_channel_id
    assert runtime.session.dire_channel_id
    # All six players landed in the right channel.
    assert voice.members(match_id, Team.RADIANT) == {"101", "102", "103"}
    assert voice.members(match_id, Team.DIRE) == {"201", "202", "203"}


async def test_same_match_id_does_not_duplicate_session(manager, voice, orchestrator):
    match_id = "555000111"
    await _handle(orchestrator, payload(match_id))
    first_channels = (
        manager.get_runtime(match_id).session.radiant_channel_id,
        manager.get_runtime(match_id).session.dire_channel_id,
    )

    # Simulate repeated GSI heartbeats for the same match.
    for clock in range(5):
        result = await _handle(orchestrator, payload(match_id, clock=clock))
        assert result["is_new"] is False

    runtime = manager.get_runtime(match_id)
    assert (
        runtime.session.radiant_channel_id,
        runtime.session.dire_channel_id,
    ) == first_channels
    # Exactly one runtime for this match.
    assert len([r for r in manager.active_runtimes() if r.session.match_id == match_id]) == 1


async def test_conversation_id_is_deterministic():
    assert conversation_id_for_match("abc") == conversation_id_for_match("abc")
    assert conversation_id_for_match("abc") != conversation_id_for_match("def")


async def test_a_reported_side_change_does_not_reshuffle_anyone(
    manager, voice, orchestrator
):
    """A player's side is fixed for the match; a later report cannot move them.

    In Dota a side never changes mid-match, so a conflicting roster tick is a
    glitch. Acting on it would break the two-channel isolation, so the bridge
    keeps the placement it already made and emits no move action.
    """
    match_id = "999888777"
    await _handle(orchestrator, payload(match_id))
    assert "103" in voice.members(match_id, Team.RADIANT)

    radiant = [p for p in RADIANT if p["name"] != "Ember"]
    dire = [*DIRE, RADIANT[2]]  # a tick that claims Ember is now on Dire
    result = await _handle(orchestrator, payload(match_id, radiant=radiant, dire=dire, clock=200))

    # No per-player move action exists, and the placement is unchanged.
    assert not any(a.startswith("move_player") for a in result["actions"])
    assert "103" in voice.members(match_id, Team.RADIANT)
    assert "103" not in voice.members(match_id, Team.DIRE)


async def test_a_peer_cannot_move_a_player_the_game_already_placed(
    manager, voice, orchestrator
):
    """A peer announcement must not drag a placed player across the boundary.

    This is the isolation guarantee: once the game has put a player on a side,
    a conflicting team announcement from that player's own client is ignored.
    """
    match_id = "222333444"
    await _handle(orchestrator, payload(match_id))
    runtime = manager.get_runtime(match_id)
    assert "101" in voice.members(match_id, Team.RADIANT)

    # A peer for 101 claims Dire; the game says Radiant, so nothing moves.
    changed = await runtime.merge_peer_team("101", Team.DIRE)
    assert changed is False
    assert "101" in voice.members(match_id, Team.RADIANT)
    assert "101" not in voice.members(match_id, Team.DIRE)


async def test_mute_tool_updates_state_and_overlay(manager, voice, orchestrator, bus):
    match_id = "111222333"
    await _handle(orchestrator, payload(match_id))
    runtime = manager.get_runtime(match_id)

    received = []

    async def collect():
        async for event in bus.stream():
            received.append(event)

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.05)

    from dota_voice.agent.tools import SetMuteAction, SetMuteTool

    await manager.call_tool_async(runtime, SetMuteTool.name, SetMuteAction(player_id="101", muted=True))
    assert voice.state_of(match_id, "101").muted is True
    assert runtime.session.voice["101"].muted is True

    await asyncio.sleep(0.05)
    task.cancel()
    assert any(e.type == "mute_changed" for e in received)


async def test_match_end_marks_session_ended_and_tears_down(manager, voice, orchestrator):
    match_id = "444555666"
    await _handle(orchestrator, payload(match_id))
    assert voice.members(match_id, Team.RADIANT)

    result = await _handle(
        orchestrator, payload(match_id, "DOTA_GAMERULES_STATE_POST_GAME", clock=2400)
    )
    assert "end_session" in result["actions"]
    # Session detached from the manager after ending.
    assert manager.get_runtime(match_id) is None


async def test_reconnect_after_restart_reuses_session(manager, voice, orchestrator):
    match_id = "777000999"
    await _handle(orchestrator, payload(match_id))
    channels = (
        manager.get_runtime(match_id).session.radiant_channel_id,
        manager.get_runtime(match_id).session.dire_channel_id,
    )

    # Simulate a client restart: detach (as the process would) and re-feed GSI.
    await manager.detach(match_id)
    assert manager.get_runtime(match_id) is None

    result = await _handle(orchestrator, payload(match_id, clock=42))
    assert result["is_new"] is False  # same match -> reused, not recreated
    runtime = manager.get_runtime(match_id)
    assert runtime.session.reconnects == 1
    assert (
        runtime.session.radiant_channel_id,
        runtime.session.dire_channel_id,
    ) == channels
    assert voice.members(match_id, Team.RADIANT) == {"101", "102", "103"}


async def test_reap_stale_ends_idle_session(manager, voice, orchestrator):
    match_id = "123123123"
    await _handle(orchestrator, payload(match_id))

    # Reap with a "now" far in the future.
    reaped = await manager.reap_stale(now=manager._last_seen[match_id] + 10_000)
    assert match_id in reaped
    assert manager.get_runtime(match_id) is None


async def test_stale_persisted_conversation_is_recovered(manager, voice, orchestrator, settings):
    """A conversation saved by an older build must not brick the session.

    Persistence is what makes a restart seamless, but a conversation written
    when the tool set was different can name a tool that no longer exists and
    the SDK refuses to reload it. Previously that made *every* GSI tick raise,
    so the match never started. The manager must notice, drop the unrecoverable
    state, and open a clean conversation instead.
    """
    import json

    match_id = "444000222"
    conversation_id = conversation_id_for_match(match_id)
    conv_dir = settings.conversations_dir / conversation_id.replace("-", "")
    conv_dir.mkdir(parents=True, exist_ok=True)
    # A base_state whose tool list references a tool this build removed.
    (conv_dir / "base_state.json").write_text(
        json.dumps(
            {
                "id": conversation_id,
                "agent": {
                    "tools": [
                        {"name": "move_player", "params": {}},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    result = await _handle(orchestrator, payload(match_id))

    assert result["action"] == "handled"
    assert result["is_new"] is True
    runtime = manager.get_runtime(match_id)
    assert runtime is not None
    assert runtime.conversation is not None
    assert voice.members(match_id, Team.RADIANT) == {"101", "102", "103"}
