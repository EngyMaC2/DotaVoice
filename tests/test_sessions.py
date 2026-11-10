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
from dota_voice.models import ChatChannel, SessionStatus, Team

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


async def test_a_hero_learned_once_is_not_erased_by_a_later_tick(
    manager, voice, orchestrator
):
    """A normal client's later GSI tick has no allplayers: keep the hero.

    The roster is merged, not replaced, so a tick that carries no hero data must
    not wipe the pick we already showed in the overlay.
    """
    match_id = "4242424242"
    await _handle(orchestrator, payload(match_id, draft=True))
    runtime = manager.get_runtime(match_id)
    lion = next(p for p in runtime.session.roster.radiant if p.player_id == "101")
    assert lion.hero == "Juggernaut"

    # The next tick reports only the local player (no allplayers, no draft).
    bare = payload(match_id, clock=30)
    del bare["allplayers"]
    bare.pop("draft", None)
    await _handle(orchestrator, bare)

    lion = next(p for p in runtime.session.roster.radiant if p.player_id == "101")
    assert lion.hero == "Juggernaut"
    assert lion.hero_id == "npc_dota_hero_juggernaut"


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


async def test_match_end_opens_the_post_game_window(manager, voice, orchestrator):
    """The match ending tears down voice but keeps the session for post-game text.

    ТЗ §8.4: after the match the voice rooms close at once, but a 10-15 minute
    text window stays open so the ten players can keep talking. The session is
    therefore *not* detached on end - it is closed later, by the timer or when
    everyone leaves.
    """
    match_id = "444555666"
    await _handle(orchestrator, payload(match_id))
    assert voice.members(match_id, Team.RADIANT)

    result = await _handle(
        orchestrator, payload(match_id, "DOTA_GAMERULES_STATE_POST_GAME", clock=2400)
    )
    assert "end_session" in result["actions"]

    runtime = manager.get_runtime(match_id)
    assert runtime is not None  # kept alive for the post-match window
    assert runtime.session.status is SessionStatus.ACTIVE
    assert runtime.state.state.value == "post_game"
    assert runtime.session.post_game_until is not None
    # Voice is gone; the match server is not.
    assert voice.members(match_id, Team.RADIANT) == set()

    # A repeat POST_GAME must not reopen anything or double the window.
    window = runtime.session.post_game_until
    again = await _handle(
        orchestrator, payload(match_id, "DOTA_GAMERULES_STATE_POST_GAME", clock=2500)
    )
    assert "end_session" not in again["actions"]
    assert runtime.session.post_game_until == window


async def test_the_post_game_window_closes_the_match_server(
    manager, voice, orchestrator, settings
):
    """When the window's timer expires the match is closed and its chat dropped."""
    match_id = "555666777"
    await _handle(orchestrator, payload(match_id))
    await _handle(
        orchestrator, payload(match_id, "DOTA_GAMERULES_STATE_POST_GAME", clock=2400)
    )
    runtime = manager.get_runtime(match_id)
    assert runtime is not None
    # Write something into the post-match chat, then let the timer run out.
    await runtime.post_message(
        player_id="101", text="gg wp", channel=ChatChannel.POST_GAME
    )
    assert runtime.chat.count(match_id) == 1

    runtime.state.window.started_at -= runtime.state.post_game_seconds + 1
    reaped = await manager.reap_stale()
    assert match_id in reaped
    assert manager.get_runtime(match_id) is None
    assert runtime.chat.count(match_id) == 0  # chat dropped with the server


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


async def test_a_restart_in_the_post_game_window_keeps_voice_dead(
    manager, voice, orchestrator
):
    """A restart after the match ended must not bring the voice rooms back."""
    match_id = "777000111"
    await _handle(orchestrator, payload(match_id))
    await _handle(
        orchestrator, payload(match_id, "DOTA_GAMERULES_STATE_POST_GAME", clock=2400)
    )
    assert manager.get_runtime(match_id).state.state.value == "post_game"

    # Restart mid-window: the process drops the runtime, then sees the match
    # again. The rooms were torn down at the end and must stay down - no
    # duplicate channels, no voice after the match.
    await manager.detach(match_id)
    result = await _handle(
        orchestrator, payload(match_id, "DOTA_GAMERULES_STATE_POST_GAME", clock=2500)
    )
    assert result["is_new"] is False
    runtime = manager.get_runtime(match_id)
    assert runtime.state.state.value == "post_game"
    assert runtime.session.radiant_channel_id is None
    assert runtime.session.dire_channel_id is None
    assert voice.members(match_id, Team.RADIANT) == set()


async def test_team_chat_is_isolated_during_the_match(manager, voice, orchestrator):
    """Each side talks in its own room and cannot read the other side's."""
    from dota_voice.chat import ChatError

    match_id = "800900001"
    await _handle(orchestrator, payload(match_id))
    runtime = manager.get_runtime(match_id)

    await runtime.post_message(player_id="101", text="push top")
    await runtime.post_message(player_id="201", text="gank mid")

    radiant = runtime.chat_history(team=Team.RADIANT)
    dire = runtime.chat_history(team=Team.DIRE)
    assert [m["text"] for m in radiant] == ["push top"]
    assert [m["text"] for m in dire] == ["gank mid"]

    # A Radiant player cannot write into the Dire room.
    with pytest.raises(ChatError):
        await runtime.post_message(
            player_id="101", text="sneaky", channel=ChatChannel.DIRE
        )


async def test_the_shared_room_opens_only_after_the_match(manager, voice, orchestrator):
    from dota_voice.chat import ChatError

    match_id = "800900002"
    await _handle(orchestrator, payload(match_id))
    runtime = manager.get_runtime(match_id)

    # During the match only the team room is offered, and the shared room is shut.
    during = {c["value"] for c in runtime.chat_channels(Team.RADIANT)}
    assert during == {"radiant"}
    with pytest.raises(ChatError):
        await runtime.post_message(player_id="101", text="gg", channel=ChatChannel.POST_GAME)

    # End the match: voice closes, the side room is shut, the shared room opens.
    await runtime.end("match_ended")
    after = {c["value"] for c in runtime.chat_channels(Team.RADIANT)}
    assert after == {"post_game"}

    await runtime.post_message(player_id="101", text="gg wp", channel=ChatChannel.POST_GAME)
    await runtime.post_message(player_id="201", text="well played", channel=ChatChannel.POST_GAME)
    shared = [
        m["text"]
        for m in runtime.chat_history(team=Team.DIRE, channel=ChatChannel.POST_GAME)
    ]
    assert shared == ["gg wp", "well played"]


async def test_a_team_room_is_shut_after_the_match(manager, voice, orchestrator):
    from dota_voice.chat import ChatError

    match_id = "800900003"
    await _handle(orchestrator, payload(match_id))
    runtime = manager.get_runtime(match_id)
    await runtime.end("match_ended")

    # Voice is gone and the private team rooms are closed; only the shared room
    # remains for the post-match window.
    with pytest.raises(ChatError):
        await runtime.post_message(player_id="101", text="late", channel=ChatChannel.RADIANT)


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


# ---------------------------------------------------------------------------
# one match at a time: no "phantom" sessions
# ---------------------------------------------------------------------------


async def test_a_new_match_ends_the_previous_one(manager, voice, orchestrator):
    """Starting a second match must not leave the first alive alongside it.

    A client that never sent POST_GAME (crash, alt-tab, queue again) would
    otherwise leave the old match in the overlay and its voice rooms open - the
    "phantom match" the ТЗ calls out.
    """
    first, second = "1111111111", "2222222222"
    await _handle(orchestrator, payload(first))
    assert manager.get_runtime(first) is not None

    await _handle(orchestrator, payload(second))

    assert manager.get_runtime(first) is None
    assert manager.get_runtime(second) is not None
    # Only the live match has a session, and the old rooms are gone.
    assert [rt.session.match_id for rt in manager.active_runtimes()] == [second]


async def test_leaving_to_the_menu_closes_the_session(manager, voice, orchestrator):
    """A payload with no match id (menu / client closed) ends the session."""
    match_id = "3333333333"
    await _handle(orchestrator, payload(match_id))
    assert manager.get_runtime(match_id) is not None

    menu = payload(match_id)
    del menu["map"]["matchid"]
    menu["map"]["game_state"] = "DOTA_GAMERULES_STATE_INIT"
    result = await _handle(orchestrator, menu)

    assert result["action"] == "cleared"
    assert match_id in result["ended"]
    assert manager.get_runtime(match_id) is None
    assert manager.active_runtimes() == []


async def test_the_menu_teardown_is_quiet_when_nothing_is_active(manager, orchestrator):
    """An idle client sending menu ticks must not report phantom teardowns."""
    menu = payload("1")
    del menu["map"]["matchid"]
    menu["map"]["game_state"] = "DOTA_GAMERULES_STATE_INIT"
    result = await _handle(orchestrator, menu)
    assert result["action"] == "ignored"
    assert result["ended"] == []
