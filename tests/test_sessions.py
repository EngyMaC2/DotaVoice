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


async def test_team_switch_moves_player(manager, voice, orchestrator):
    match_id = "999888777"
    await _handle(orchestrator, payload(match_id))

    radiant = [p for p in RADIANT if p["name"] != "Ember"]
    dire = [*DIRE, RADIANT[2]]  # Ember (account 103) joins Dire
    result = await _handle(orchestrator, payload(match_id, radiant=radiant, dire=dire, clock=200))

    assert result["changed"] is True
    assert any(a.startswith("move_player:103") for a in result["actions"])
    assert "103" in voice.members(match_id, Team.DIRE)
    assert "103" not in voice.members(match_id, Team.RADIANT)
    assert voice.channel_of(match_id, "103") == manager.get_runtime(match_id).session.dire_channel_id


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
