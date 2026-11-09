"""Tests for the match lifecycle state machine and the post-match window.

The machine is pure, so these tests need no I/O: they pin the transitions that
keep a match from reopening after it ends, and the rules that decide when the
post-match window is open, expired, or done because everyone left.
"""

from __future__ import annotations

from dota_voice.match_state import (
    POST_GAME_SECONDS,
    BridgeState,
    MatchStateMachine,
    PostMatchWindow,
)
from dota_voice.models import MatchPhase


# ---------------------------------------------------------------------------
# transitions
# ---------------------------------------------------------------------------


def test_a_match_walks_the_lifecycle_in_order():
    machine = MatchStateMachine()
    assert machine.state is BridgeState.IDLE
    assert machine.advance(MatchPhase.HERO_SELECTION)
    assert machine.state is BridgeState.DRAFT
    assert machine.advance(MatchPhase.PLAYING)
    assert machine.state is BridgeState.IN_GAME
    assert machine.advance(MatchPhase.ENDED)
    assert machine.state is BridgeState.POST_GAME


def test_a_late_packet_never_reopens_a_finished_match():
    machine = MatchStateMachine()
    machine.advance(MatchPhase.PLAYING)
    machine.advance(MatchPhase.ENDED)
    # The game can send an earlier phase around a transition; it must be ignored.
    assert not machine.advance(MatchPhase.PLAYING)
    assert not machine.advance(MatchPhase.HERO_SELECTION)
    assert machine.state is BridgeState.POST_GAME


def test_a_spectator_may_join_a_match_already_in_progress():
    machine = MatchStateMachine()
    assert machine.advance(MatchPhase.PLAYING)
    assert machine.state is BridgeState.IN_GAME


def test_an_illegal_transition_is_refused_not_obeyed():
    machine = MatchStateMachine(BridgeState.CLOSED)
    assert not machine.to(BridgeState.IN_GAME, reason="bogus")
    assert machine.state is BridgeState.CLOSED


def test_voice_lives_only_through_the_match():
    assert BridgeState.DRAFT.voice_active
    assert BridgeState.IN_GAME.voice_active
    assert not BridgeState.POST_GAME.voice_active
    assert not BridgeState.CLOSED.voice_active


def test_chat_outlives_voice_into_the_post_game_window():
    assert BridgeState.DRAFT.chat_active
    assert BridgeState.IN_GAME.chat_active
    assert BridgeState.POST_GAME.chat_active
    assert not BridgeState.CLOSED.chat_active


# ---------------------------------------------------------------------------
# post-match window
# ---------------------------------------------------------------------------


def test_the_window_starts_when_the_match_ends():
    machine = MatchStateMachine()
    machine.advance(MatchPhase.PLAYING)
    window = machine.open_post_game(now=1000.0)
    assert machine.state is BridgeState.POST_GAME
    assert window.duration == POST_GAME_SECONDS
    assert window.deadline == 1000.0 + POST_GAME_SECONDS


def test_opening_the_window_twice_does_not_restart_it():
    machine = MatchStateMachine()
    first = machine.open_post_game(now=1000.0)
    second = machine.open_post_game(now=1100.0)
    assert first is second
    assert second.started_at == 1000.0


def test_the_window_expires_on_its_timer():
    window = PostMatchWindow(duration=60.0, started_at=1000.0)
    assert not window.expired(1059.0)
    assert window.expired(1060.0)
    assert window.remaining(1060.0) == 0.0


def test_the_window_closes_once_everyone_has_left():
    window = PostMatchWindow(duration=600.0, started_at=1000.0)
    window.leave("101")
    assert not window.everyone_left(total=3)
    window.leave("102")
    window.leave("103")
    assert window.everyone_left(total=3)


def test_an_empty_match_is_never_considered_all_left():
    window = PostMatchWindow()
    assert not window.everyone_left(total=0)


def test_closing_the_machine_clears_the_window():
    machine = MatchStateMachine()
    machine.open_post_game(now=1000.0)
    machine.close("everyone_left")
    assert machine.state is BridgeState.CLOSED
    assert machine.window is None
