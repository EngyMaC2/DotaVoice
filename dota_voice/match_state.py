"""The match state machine: one match, from search to a fully closed server.

The service is not "running since launch"; it is a sequence of matches, and each
match walks a fixed lifecycle (ТЗ §5):

    IDLE -> SEARCHING -> DRAFT -> IN_GAME -> POST_GAME -> CLOSING -> CLOSED -> IDLE

The game drives the early part of that path through GSI (`game_state`), and the
bridge drives the last part itself: when the match ends the voice rooms are *not*
torn down at once - a post-match window opens so the ten players can talk for a
few minutes - and only when that window closes (its timer runs out, or everyone
has left) does the server close for real.

This module is deliberately pure: it holds the state, validates transitions, and
answers questions ("do the voice rooms exist now?", "is the post-match chat
open?"). It does no I/O, so it is trivially testable and the runtime stays the
only place that touches the voice backend and the store.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

from .models import MatchPhase

logger = logging.getLogger(__name__)

# Default length of the post-match window. ТЗ §8.4 asks for 10-15 minutes with a
# 12-minute default.
POST_GAME_SECONDS = 12 * 60


class BridgeState(Enum):
    """Lifecycle state of one match session."""

    IDLE = "idle"
    SEARCHING = "searching"
    DRAFT = "draft"
    IN_GAME = "in_game"
    POST_GAME = "post_game"
    CLOSING = "closing"
    CLOSED = "closed"

    @property
    def label(self) -> str:
        return {
            "idle": "ожидание",
            "searching": "поиск игры",
            "draft": "драфт",
            "in_game": "идёт игра",
            "post_game": "после матча",
            "closing": "закрытие",
            "closed": "закрыто",
        }[self.value]

    @property
    def voice_active(self) -> bool:
        """Whether the two team voice rooms should exist in this state.

        Rooms are built at the draft (rosters are known then) and torn down when
        the match ends; the post-match window carries text only, no voice.
        """
        return self in {BridgeState.DRAFT, BridgeState.IN_GAME}

    @property
    def chat_active(self) -> bool:
        """Whether text chat is available (it outlives voice into POST_GAME)."""
        return self in {BridgeState.DRAFT, BridgeState.IN_GAME, BridgeState.POST_GAME}

    @property
    def finished(self) -> bool:
        return self in {BridgeState.CLOSING, BridgeState.CLOSED}


# Allowed forward transitions. Anything else is an out-of-order event (a GSI
# tick that arrives late, a duplicate, or a glitch) and is logged, not obeyed.
#
# IDLE/SEARCHING may jump straight to any live state: a spectator joins a match
# that is already in progress (IN_GAME), or launches the app after it ended
# (POST_GAME), without ever seeing the draft. CLOSING is reachable from any live
# state - a match can be torn down from the draft or mid-game too (the player
# closed Dota, or the session went idle).
_ALLOWED: dict[BridgeState, set[BridgeState]] = {
    BridgeState.IDLE: {
        BridgeState.SEARCHING,
        BridgeState.DRAFT,
        BridgeState.IN_GAME,
        BridgeState.POST_GAME,
    },
    BridgeState.SEARCHING: {
        BridgeState.DRAFT,
        BridgeState.IN_GAME,
        BridgeState.POST_GAME,
        BridgeState.IDLE,
    },
    BridgeState.DRAFT: {BridgeState.IN_GAME, BridgeState.POST_GAME, BridgeState.CLOSING},
    BridgeState.IN_GAME: {BridgeState.POST_GAME, BridgeState.DRAFT, BridgeState.CLOSING},
    BridgeState.POST_GAME: {BridgeState.CLOSING},
    BridgeState.CLOSING: {BridgeState.CLOSED, BridgeState.IDLE},
    BridgeState.CLOSED: {BridgeState.IDLE},
}

# GSI phase -> bridge state. PRE_GAME is still the setup window (rosters known,
# game not started), so it stays in DRAFT; only GAME_IN_PROGRESS is IN_GAME.
_PHASE_TO_STATE: dict[MatchPhase, BridgeState] = {
    MatchPhase.NONE: BridgeState.IDLE,
    MatchPhase.HERO_SELECTION: BridgeState.DRAFT,
    MatchPhase.STRATEGY_TIME: BridgeState.DRAFT,
    MatchPhase.PRE_GAME: BridgeState.DRAFT,
    MatchPhase.PLAYING: BridgeState.IN_GAME,
    MatchPhase.ENDED: BridgeState.POST_GAME,
}


@dataclass
class Transition:
    at: float
    from_state: BridgeState
    to_state: BridgeState
    reason: str = ""


@dataclass
class PostMatchWindow:
    """The 10-15 minute text-only window after a match.

    It closes when the timer runs out *or* when every player has pressed
    "Выйти", whichever comes first (ТЗ §8.4). The window is a value object so the
    runtime and the reaper can both ask it questions without shared state.
    """

    duration: float = POST_GAME_SECONDS
    started_at: float = field(default_factory=time.time)
    left: set[str] = field(default_factory=set)

    @property
    def deadline(self) -> float:
        return self.started_at + self.duration

    def remaining(self, now: float | None = None) -> float:
        return max(0.0, self.deadline - (now if now is not None else time.time()))

    def expired(self, now: float | None = None) -> bool:
        return self.remaining(now) <= 0.0

    def leave(self, player_id: str) -> None:
        if player_id:
            self.left.add(player_id)

    def everyone_left(self, total: int) -> bool:
        """True once every player in the match has left the window."""
        return total > 0 and len(self.left) >= total


class MatchStateMachine:
    """Tracks one match's lifecycle state and its transition history."""

    def __init__(
        self,
        state: BridgeState = BridgeState.IDLE,
        *,
        post_game_seconds: float = POST_GAME_SECONDS,
    ) -> None:
        self.state = state
        self.post_game_seconds = post_game_seconds
        self.window: PostMatchWindow | None = None
        self.history: list[Transition] = []

    # ------------------------------------------------------------------

    def can(self, new_state: BridgeState) -> bool:
        return new_state is self.state or new_state in _ALLOWED.get(self.state, set())

    def to(self, new_state: BridgeState, reason: str = "") -> bool:
        """Move to ``new_state`` if the transition is legal. Returns whether it moved."""
        if new_state is self.state:
            return False
        if not self.can(new_state):
            logger.warning(
                "Недопустимый переход состояния %s -> %s (%s); игнорирую",
                self.state.value,
                new_state.value,
                reason or "без причины",
            )
            return False
        previous = self.state
        self.state = new_state
        self.history.append(
            Transition(time.time(), previous, new_state, reason)
        )
        return True

    def advance(self, phase: MatchPhase) -> bool:
        """Follow the game's phase. Returns whether the state changed.

        A phase never moves the match *backwards*: a late GSI packet that still
        carries an earlier phase (the game sends them out of order around
        transitions) must not reopen a match that already ended.
        """
        target = _PHASE_TO_STATE.get(phase, BridgeState.IDLE)
        if self.state is BridgeState.POST_GAME and target in (
            BridgeState.IDLE,
            BridgeState.DRAFT,
            BridgeState.IN_GAME,
        ):
            return False
        if target is self.state:
            return False
        # Forward-only within a match; allow IDLE -> DRAFT (roster seen first).
        if target in (BridgeState.IDLE, BridgeState.SEARCHING) and self.state in (
            BridgeState.DRAFT,
            BridgeState.IN_GAME,
        ):
            return False
        return self.to(target, reason=f"gsi:{phase.value}")

    # ------------------------------------------------------------------
    # post-match window
    # ------------------------------------------------------------------

    def open_post_game(self, now: float | None = None) -> PostMatchWindow:
        """Enter POST_GAME and start the window (idempotent)."""
        if self.state is not BridgeState.POST_GAME:
            self.to(BridgeState.POST_GAME, reason="match_ended")
        if self.window is None:
            self.window = PostMatchWindow(
                duration=self.post_game_seconds,
                started_at=now if now is not None else time.time(),
            )
        return self.window

    def close(self, reason: str = "closed") -> bool:
        """Move through CLOSING to CLOSED."""
        self.to(BridgeState.CLOSING, reason=reason)
        self.to(BridgeState.CLOSED, reason=reason)
        self.window = None
        return True
