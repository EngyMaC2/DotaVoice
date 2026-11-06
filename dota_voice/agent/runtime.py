"""Per-match runtime: the unit that owns one match session end-to-end.

A :class:`MatchSessionRuntime` binds together

* the persisted :class:`MatchSession` (keyed by ``match_id``),
* the voice backend (channels + mute state),
* the overlay event bus,
* and an OpenHands :class:`Conversation` whose agent can call the voice tools.

The runtime is the only place that mutates session state, so tools and the
orchestrator stay thin. Every mutation goes through :meth:`persist`, which
writes the session keyed by ``match_id`` - that is what makes a game restart
safe (same match id -> same session, no duplicate channels).

Concurrency note
----------------
The service runs on a single asyncio loop, but OpenHands tool executors are
synchronous by contract. Tool calls therefore run on a worker thread and hop
back onto the loop via :meth:`run_sync`. The orchestrator always dispatches
agent/tool work with ``loop.run_in_executor`` so the loop stays free.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from ..bus import EventBus
from ..config import Settings
from ..models import (
    MatchPhase,
    MatchSession,
    OverlayEvent,
    Player,
    Roster,
    SessionStatus,
    Team,
    VoiceState,
)
from ..store import Store
from ..voice import VoiceBackend
from . import context

logger = logging.getLogger(__name__)

_CONV_NAMESPACE = uuid.UUID("6f2a1f9e-3b7c-4d2a-9f1e-0a1b2c3d4e5f")


def conversation_id_for_match(match_id: str) -> str:
    """Deterministic conversation id: same match -> same conversation."""
    return str(uuid.uuid5(_CONV_NAMESPACE, f"match:{match_id}"))


class MatchSessionRuntime:
    def __init__(
        self,
        session: MatchSession,
        *,
        store: Store,
        voice: VoiceBackend,
        bus: EventBus,
        settings: Settings,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.session = session
        self.store = store
        self.voice = voice
        self.bus = bus
        self.settings = settings
        self._loop = loop
        self.conversation: Any | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ------------------------------------------------------------------
    # async <-> sync bridge
    # ------------------------------------------------------------------

    def run_sync(self, coro, timeout: float = 30.0):
        """Run a coroutine from a worker thread on the service event loop.

        Raises if called on the loop thread itself, which would deadlock.
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            return asyncio.run(coro)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            raise RuntimeError(
                "run_sync() must not be called on the event loop thread; "
                "dispatch the tool through loop.run_in_executor instead"
            )
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    # ------------------------------------------------------------------
    # persistence / overlay
    # ------------------------------------------------------------------

    async def persist(self) -> None:
        self.session.bump()
        await self.store.put(self.session)

    async def emit(self, event_type: str, **payload: Any) -> None:
        await self.bus.publish(
            OverlayEvent(type=event_type, match_id=self.session.match_id, payload=payload)
        )

    def _voice_state(self, player_id: str) -> VoiceState:
        state = self.session.voice.get(player_id)
        if state is None:
            state = VoiceState(player_id=player_id)
            self.session.voice[player_id] = state
        return state

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        await self.persist()
        await self.emit(
            "session_started",
            match_id=self.session.match_id,
            phase=self.session.phase.value,
            reconnect=self.session.reconnects > 0,
            reconnects=self.session.reconnects,
        )

    async def end(self, reason: str = "match_ended") -> None:
        self.session.status = SessionStatus.ENDED
        self.session.phase = MatchPhase.ENDED
        self.session.ended_at = time.time()
        await self.persist()
        await self.emit("session_ended", match_id=self.session.match_id, reason=reason)
        await self.voice.delete_channels(self.session.match_id)

    async def note_reconnect(self) -> None:
        self.session.reconnects += 1
        await self.persist()
        await self.emit("reconnected", reconnects=self.session.reconnects)

    async def close(self) -> None:
        if self.conversation is not None:
            try:
                self.conversation.close()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Ошибка при закрытии диалога: %s", exc)
            context.unregister_runtime(self.session.conversation_id)
            self.conversation = None

    # ------------------------------------------------------------------
    # roster / voice operations
    # ------------------------------------------------------------------

    def apply_roster(self, roster: Roster, phase: MatchPhase | None = None) -> bool:
        """Merge a freshly observed roster; return True if teams changed."""
        changed = roster.signature() != self.session.roster.signature()
        if changed:
            self.session.roster = roster
            for player in roster.all_players():
                self._voice_state(player.player_id).team = player.team
        if phase is not None:
            self.session.phase = phase
        self.session.touch()
        return changed

    async def ensure_channels(self) -> bool:
        """Create (or reuse) the two team channels. Returns True if created."""
        created = self.session.radiant_channel_id is None
        radiant, dire = await self.voice.ensure_channels(self.session.match_id)
        self.session.radiant_channel_id = radiant
        self.session.dire_channel_id = dire
        await self.persist()
        await self.emit(
            "channels_ready", radiant_channel_id=radiant, dire_channel_id=dire
        )
        return created

    async def sync_roster(self) -> int:
        await self.voice.sync_roster(self.session.match_id, self.session.roster)
        moved = 0
        for player in self.session.roster.all_players():
            if player.team is Team.UNKNOWN:
                continue
            state = self._voice_state(player.player_id)
            state.team = player.team
            state.channel_id = (
                self.session.radiant_channel_id
                if player.team is Team.RADIANT
                else self.session.dire_channel_id
            )
            moved += 1
        await self.persist()
        await self.emit("roster_synced", **self.roster_payload())
        return moved

    async def move_player(self, player_id: str, team: Team) -> str | None:
        player = None
        for candidate in self.session.roster.all_players():
            if candidate.player_id == player_id or candidate.name == player_id:
                player = candidate
                break
        if player is None:
            return None
        player.team = team
        # Relocate the Player object so roster grouping matches the new team.
        self.session.roster.radiant = [
            p for p in self.session.roster.radiant if p is not player
        ]
        self.session.roster.dire = [
            p for p in self.session.roster.dire if p is not player
        ]
        if team is Team.RADIANT:
            self.session.roster.radiant.append(player)
        else:
            self.session.roster.dire.append(player)

        state = self._voice_state(player.player_id)
        state.team = team
        state.channel_id = (
            self.session.radiant_channel_id
            if team is Team.RADIANT
            else self.session.dire_channel_id
        )
        await self.voice.sync_roster(self.session.match_id, self.session.roster)
        await self.persist()
        await self.emit(
            "player_moved",
            player_id=player.player_id,
            team=team.value,
            channel_id=state.channel_id,
        )
        await self.emit("roster_synced", **self.roster_payload())
        return state.channel_id

    async def set_mute(self, player_id: str, muted: bool) -> bool:
        self._voice_state(player_id).muted = muted
        await self.voice.set_mute(self.session.match_id, player_id, muted)
        await self.persist()
        await self.emit("mute_changed", player_id=player_id, muted=muted)
        return True

    async def set_deafen(self, player_id: str, deafened: bool) -> bool:
        self._voice_state(player_id).deafened = deafened
        await self.voice.set_deafen(self.session.match_id, player_id, deafened)
        await self.persist()
        await self.emit("deafen_changed", player_id=player_id, deafened=deafened)
        return True

    async def set_speaking(self, player_id: str, speaking: bool) -> None:
        state = self._voice_state(player_id)
        if state.speaking == speaking:
            return
        state.speaking = speaking
        state.updated_at = time.time()
        await self.persist()
        await self.emit("speaking_changed", player_id=player_id, speaking=speaking)

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------

    def roster_payload(self) -> dict[str, Any]:
        return {
            "radiant": [self._player_payload(p) for p in self.session.roster.radiant],
            "dire": [self._player_payload(p) for p in self.session.roster.dire],
            "radiant_channel_id": self.session.radiant_channel_id,
            "dire_channel_id": self.session.dire_channel_id,
        }

    def _player_payload(self, player: Player) -> dict[str, Any]:
        state = self.session.voice.get(player.player_id)
        return {
            "player_id": player.player_id,
            "name": player.name or player.player_id,
            "team": player.team.value,
            "team_label": player.team.label,
            "hero": player.hero,
            "slot": player.slot,
            "speaking": bool(state and state.speaking),
            "muted": bool(state and state.muted),
            "deafened": bool(state and state.deafened),
            "channel_id": state.channel_id if state else None,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "match_id": self.session.match_id,
            "conversation_id": self.session.conversation_id,
            "phase": self.session.phase.value,
            "status": self.session.status.value,
            "reconnects": self.session.reconnects,
            "revision": self.session.revision,
            "created_at": self.session.created_at,
            "updated_at": self.session.updated_at,
            "last_seen": self.session.last_seen,
            "llm_enabled": self.settings.llm_enabled,
            "voice_backend": self.settings.voice,
            **self.roster_payload(),
        }

    def state_observation(self):
        from .tools import GetSessionStateObservation

        return GetSessionStateObservation(
            match_id=self.session.match_id,
            phase=self.session.phase.value,
            status=self.session.status.value,
            radiant=self.session.players_by_team(Team.RADIANT),
            dire=self.session.players_by_team(Team.DIRE),
            muted=[pid for pid, st in self.session.voice.items() if st.muted],
            reconnects=self.session.reconnects,
        )
