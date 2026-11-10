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
import contextlib
import logging
import time
import uuid
from typing import Any

from ..bus import EventBus
from ..chat import ChatError, ChatService
from ..config import Settings
from ..match_state import BridgeState, MatchStateMachine
from ..models import (
    ChatChannel,
    ChatMessage,
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


def player_display_name(player: Player) -> str:
    """A name the overlay can always show for a player.

    The game gives a real name for spectators (``allplayers``) and for the local
    slot, but a normal client's GSI often carries only a bare slot number with
    no name at all. Falling back to the raw account id would print an opaque
    number, so use the slot ("Игрок 4") or a plain "Игрок" instead - the roster
    must read as people, not ids.
    """
    name = (player.name or "").strip()
    if name:
        return name
    if player.slot is not None:
        return f"Игрок {player.slot + 1}"
    return "Игрок"


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
        chat: ChatService | None = None,
    ) -> None:
        self.session = session
        self.store = store
        self.voice = voice
        self.bus = bus
        self.settings = settings
        self._loop = loop
        self.conversation: Any | None = None
        self.chat = chat or ChatService()
        # Rebuild the state machine from the persisted state so a restart keeps
        # the match's lifecycle (and, importantly, its post-match window).
        try:
            initial = BridgeState(session.state)
        except ValueError:
            initial = BridgeState.IDLE
        self.state = MatchStateMachine(
            initial, post_game_seconds=session.post_game_seconds
        )
        if session.post_game_until is not None:
            from ..match_state import PostMatchWindow

            self.state.window = PostMatchWindow(
                duration=session.post_game_seconds,
                started_at=session.post_game_until - session.post_game_seconds,
                left=set(session.departed),
            )

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
        """End the match: close voice, open the post-match text window.

        The voice rooms are torn down at once - the match is over and voice must
        not linger - but the *session* stays alive for the post-match window
        (ТЗ §8.4) so the players can keep talking in text. The window is closed
        later by :meth:`close_post_game` (timer expired, or everyone left).
        """
        self.session.phase = MatchPhase.ENDED
        await self._sync_state(BridgeState.POST_GAME, reason=reason)
        window = self.state.window
        self.session.post_game_until = window.deadline if window else None
        await self._teardown_voice()
        await self.persist()
        await self.emit(
            "session_ended",
            match_id=self.session.match_id,
            reason=reason,
            post_game_until=self.session.post_game_until,
            post_game_remaining=window.remaining() if window else 0.0,
        )

    async def _teardown_voice(self) -> None:
        """Close the voice rooms, keeping any text transport if the backend can.

        The peer mesh doubles as the text transport, so for it we call
        ``enter_post_game`` (voice stops, text stays). For a backend that only
        does voice we just delete the channels.
        """
        enter = getattr(self.voice, "enter_post_game", None)
        if callable(enter):
            with contextlib.suppress(Exception):
                await enter(self.session.match_id)
        else:
            await self.voice.delete_channels(self.session.match_id)
        self.session.radiant_channel_id = None
        self.session.dire_channel_id = None

    async def close_post_game(self, reason: str = "post_game_closed") -> None:
        """Close the post-match window and shut the match server down.

        Called when the window's timer expires or every player has left. After
        this the session is ENDED, its chat is dropped, and its channels are
        already gone - nothing of the match remains (ТЗ §6.4, §8.4).
        """
        self.state.close(reason)
        self.session.status = SessionStatus.ENDED
        self.session.ended_at = time.time()
        self.session.post_game_until = None
        self.session.state = self.state.state.value
        self.chat.drop(self.session.match_id)
        await self.voice.delete_channels(self.session.match_id)
        await self.persist()
        await self.emit(
            "session_closed",
            match_id=self.session.match_id,
            reason=reason,
        )

    async def leave_post_game(self, player_id: str) -> bool:
        """A player pressed "Выйти". Returns True if that closed the window.

        One player leaving never affects the others - the window only closes
        when *everyone* has left, or when its timer runs out.
        """
        if self.state.state is not BridgeState.POST_GAME:
            return False
        window = self.state.window
        if window is None:
            return False
        window.leave(player_id)
        if player_id and player_id not in self.session.departed:
            self.session.departed.append(player_id)
        total = len(self.session.roster.all_players())
        await self.persist()
        await self.emit(
            "post_game_leave",
            player_id=player_id,
            remaining=window.remaining(),
            departed=list(self.session.departed),
            everyone_left=window.everyone_left(total),
        )
        if window.everyone_left(total):
            await self.close_post_game("everyone_left")
            return True
        return False

    def post_game_expired(self, now: float | None = None) -> bool:
        if self.state.state is not BridgeState.POST_GAME:
            return False
        window = self.state.window
        return bool(window and window.expired(now))

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
        """Merge a freshly observed roster; return True if teams changed.

        The merge matters for ordinary players: a non-spectator GSI payload
        carries only the local slot, so the incoming roster is nearly empty while
        the session already knows the teammates their apps announced. Replacing
        the roster outright would drop them on every GSI tick; instead we keep
        what we learned from peers and only update what the game actually tells
        us.
        """
        merged = self._merge_roster(roster)
        changed = merged.signature() != self.session.roster.signature()
        if changed:
            self.session.roster = merged
            for player in merged.all_players():
                self._voice_state(player.player_id).team = player.team
        if phase is not None:
            self.session.phase = phase
        self.session.touch()
        return changed

    def _merge_roster(self, incoming: Roster) -> Roster:
        """Fold an observed roster into what the session already knows.

        Two rules keep the two channels stable:

        * a player's side, once placed, never changes for the match - the game
          is authoritative when it first reports a side, but a later conflicting
          report cannot reshuffle anyone (in Dota a side is fixed for the whole
          match, so a change is a glitch we must not act on);
        * a teammate we only know from their own client stays until their app
          goes away, so a non-spectator's nearly-empty GSI tick does not drop
          them.
        """
        known: dict[str, Player] = {
            p.player_id: p for p in self.session.roster.all_players()
        }
        merged = Roster()
        seen: set[str] = set()
        for player in incoming.all_players():
            previous = known.get(player.player_id)
            team = player.team
            connected = player.connected
            if previous is not None:
                # Sticky placement: keep the side we already know.
                if previous.team is not Team.UNKNOWN:
                    if team is not Team.UNKNOWN and team is not previous.team:
                        logger.warning(
                            "Игра сообщает новую сторону %s для игрока %s (%s) — "
                            "оставляю прежнюю: команды в матче не меняются",
                            team.value,
                            player.player_id,
                            previous.team.value,
                        )
                    team = previous.team
                connected = connected or previous.connected
            # Hero data can arrive on one tick and be absent on the next (a
            # normal client's GSI carries no `allplayers`), so never let a later
            # tick erase a hero we already learned.
            hero = player.hero or (previous.hero if previous else "")
            hero_id = player.hero_id or (previous.hero_id if previous else "")
            steam_id = player.steam_id or (previous.steam_id if previous else "")
            player = player.model_copy(
                update={
                    "team": team,
                    "connected": connected,
                    "hero": hero,
                    "hero_id": hero_id,
                    "steam_id": steam_id,
                }
            )
            self._place(merged, player)
            seen.add(player.player_id)

        # Players we only know from peers stay until their app goes away.
        for player_id, previous in known.items():
            if player_id in seen or not previous.connected:
                continue
            self._place(merged, previous.model_copy())
        return merged

    @staticmethod
    def _place(roster: Roster, player: Player) -> None:
        if player.team is Team.RADIANT:
            roster.radiant.append(player)
        elif player.team is Team.DIRE:
            roster.dire.append(player)
        else:
            roster.unknown.append(player)

    async def ensure_channels(self) -> bool:
        """Create (or reuse) the two team channels. Returns True if created.

        Refuses once the match's lifecycle has left the voice phase: a restart
        during the post-match window (or a stray PLAYING packet) must not bring
        the rooms back. ``attach`` and the mesh both call this, so the guard has
        to live here rather than only in the orchestrator.
        """
        if not self.state.state.voice_active:
            return False
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
        if not self.state.state.voice_active:
            return 0
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

    async def place_player(self, player_id: str, team: Team) -> str | None:
        """Place a player whose side we had not been able to tell yet.

        The team always comes from a player's own client (or from the game's
        authoritative view), never from a person. Teams are fixed for the match,
        so this only ever *classifies* a player we previously listed as unknown -
        it never reshuffles someone the game already placed.
        """
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
        self.session.roster.unknown = [
            p for p in self.session.roster.unknown if p is not player
        ]
        if team is Team.RADIANT:
            self.session.roster.radiant.append(player)
        elif team is Team.DIRE:
            self.session.roster.dire.append(player)
        else:
            self.session.roster.unknown.append(player)

        state = self._voice_state(player.player_id)
        state.team = team
        state.channel_id = (
            self.session.radiant_channel_id
            if team is Team.RADIANT
            else self.session.dire_channel_id
        )
        await self.voice.sync_roster(self.session.match_id, self.session.roster)
        await self.persist()
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

    async def merge_peer_team(self, peer_name: str, team: Team) -> bool:
        """Fold a team reported by a peer's own client into the roster.

        A normal player's GSI only reports their own slot, so the server cannot
        learn the full split from the game alone. Each player's app announces its
        own team, though, so as peers appear the session fills in the teams it
        is missing and can place everyone in the right channel - automatically,
        without a human sorting anyone.

        A peer can only *classify a player we had not placed yet*. It can never
        move a player the game itself already put on a side: teams are fixed for
        the match, and honouring a stray announcement would let a player cross
        the isolation boundary. GSI is the authority; peers only fill gaps.

        Returns True if this changed the roster (a new player, or a newly
        classified one), which is the signal to re-sync channels.
        """
        if not peer_name or team is Team.UNKNOWN:
            return False
        existing = None
        for player in self.session.roster.all_players():
            if player.player_id == peer_name or player.name == peer_name:
                existing = player
                break
        if existing is None:
            # A teammate we had not seen yet: add them on their reported side.
            player = Player(
                player_id=peer_name, name=peer_name, team=team, connected=True
            )
            if team is Team.RADIANT:
                self.session.roster.radiant.append(player)
            else:
                self.session.roster.dire.append(player)
            self._voice_state(player.player_id).team = team
            await self.persist()
            await self.emit(
                "player_joined",
                player_id=player.player_id,
                name=player.name,
                team=team.value,
            )
            return True
        if existing.team is team:
            if not existing.connected:
                # Same side, but this is the first time we see the peer's app:
                # light them up so the overlay shows them as connected.
                existing.connected = True
                await self.persist()
                await self.emit(
                    "player_connected",
                    player_id=existing.player_id,
                    team=team.value,
                )
                return True
            return False
        if existing.team is not Team.UNKNOWN:
            # The game already placed this player, and it disagrees with the
            # peer's announcement. Trust the game and keep the sides isolated
            # rather than let an announcement move someone across the boundary.
            logger.warning(
                "Пир %s сообщает команду %s, но игра уже определила %s — оставляю сторону игры",
                peer_name,
                team.value,
                existing.team.value,
            )
            if not existing.connected:
                existing.connected = True
                await self.persist()
                await self.emit(
                    "player_connected",
                    player_id=existing.player_id,
                    team=existing.team.value,
                )
                return True
            return False
        # We had this player but could not tell their side yet; the peer's own
        # client is the authority here, so classify them now.
        existing.connected = True
        await self.place_player(existing.player_id, team)
        return True

    async def mark_peer_disconnected(self, peer_name: str) -> bool:
        """Grey out a teammate whose app left the voice network.

        The player stays in the roster - the game still shows them - but the
        connection flag drops, so the overlay reads "not connected" again.
        """
        for player in self.session.roster.all_players():
            if player.player_id == peer_name or player.name == peer_name:
                if not player.connected:
                    return False
                player.connected = False
                state = self._voice_state(player.player_id)
                state.speaking = False
                await self.persist()
                await self.emit("player_disconnected", player_id=player.player_id)
                return True
        return False

    async def set_speaking(self, player_id: str, speaking: bool) -> None:
        state = self._voice_state(player_id)
        if state.speaking == speaking:
            return
        state.speaking = speaking
        state.updated_at = time.time()
        await self.persist()
        await self.emit("speaking_changed", player_id=player_id, speaking=speaking)

    # ------------------------------------------------------------------
    # lifecycle state
    # ------------------------------------------------------------------

    async def _sync_state(self, new_state: BridgeState, *, reason: str = "") -> bool:
        """Move the match state machine and persist it. Returns whether it moved."""
        if new_state is BridgeState.POST_GAME:
            self.state.open_post_game()
        else:
            self.state.to(new_state, reason=reason)
        moved = self.session.state != self.state.state.value
        self.session.state = self.state.state.value
        return moved

    async def advance_to_phase(self, phase: MatchPhase) -> bool:
        """Follow a GSI phase into the match lifecycle.

        The room is built at the draft, so a match that reports a playing state
        without a draft first (a spectator joining late) still gets its rooms:
        the orchestrator calls this and then ensures channels.
        """
        if not self.state.advance(phase):
            return False
        self.session.state = self.state.state.value
        await self.persist()
        await self.emit(
            "state_changed",
            state=self.state.state.value,
            state_label=self.state.state.label,
            phase=phase.value,
        )
        return True

    # ------------------------------------------------------------------
    # chat
    # ------------------------------------------------------------------

    def chat_channels(self, team: Team) -> list[dict[str, str]]:
        """Channels a player can actually write to right now, for the UI.

        While the match runs that is the side's own room; once it ends the side
        room is shut and the shared post-match room opens instead. Showing only
        the writable rooms keeps the panel in step with what the server accepts.
        """
        channels: list[ChatChannel] = []
        if self.state.state in (BridgeState.DRAFT, BridgeState.IN_GAME):
            channels.extend(c for c in self.chat.channels_for(team) if not c.shared)
        if self.state.state is BridgeState.POST_GAME:
            channels.append(ChatChannel.POST_GAME)
        return [{"value": c.value, "label": c.label} for c in channels]

    async def post_message(
        self,
        *,
        player_id: str,
        text: str,
        channel: ChatChannel | None = None,
        message_id: str = "",
        ts: float | None = None,
    ) -> ChatMessage:
        """Write a chat message from a player, enforcing channel access.

        The author's side comes from the roster, never from the request, so a
        client cannot claim the other team's channel. Raises :class:`ChatError`
        when the channel is not available (wrong side, or no match in progress).
        """
        team = self.session.roster.team_of(player_id)
        player = next(
            (
                p
                for p in self.session.roster.all_players()
                if p.player_id == player_id or p.name == player_id
            ),
            None,
        )
        if player is not None:
            team = player.team
        chosen = channel or ChatChannel.for_team(team)
        if not self.state.state.chat_active:
            raise ChatError("чат недоступен: матч не идёт")
        if chosen.shared and self.state.state is not BridgeState.POST_GAME:
            raise ChatError("канал «После матча» ещё не открыт")
        if chosen in (ChatChannel.RADIANT, ChatChannel.DIRE) and self.state.state not in (
            BridgeState.DRAFT,
            BridgeState.IN_GAME,
        ):
            raise ChatError("командный чат закрыт")
        message = self.chat.post(
            self.session.match_id,
            channel=chosen,
            author_id=player_id,
            author_name=(player.name if player and player.name else player_id),
            team=team,
            text=text,
            message_id=message_id,
            ts=ts,
        )
        await self.emit(
            "chat_message",
            message=message.model_dump(mode="json"),
            channel=chosen.value,
            channel_label=chosen.label,
        )
        return message

    def chat_history(
        self, *, team: Team, channel: ChatChannel | None = None, limit: int = 100
    ) -> list[dict]:
        return [
            m.model_dump(mode="json")
            for m in self.chat.history(
                self.session.match_id, team=team, channel=channel, limit=limit
            )
        ]

    async def ingest_chat(
        self,
        *,
        author_id: str,
        text: str,
        channel: ChatChannel,
        team: Team,
        message_id: str = "",
        ts: float | None = None,
    ) -> tuple[ChatMessage | None, bool]:
        """Record a chat message that arrived from a peer.

        Returns ``(message, is_new)``. ``is_new`` is False for a message we
        already have, which is what stops a shared post-match message from being
        relayed around the mesh forever: it is forwarded once, then recognised
        as a duplicate and dropped.
        """
        if not self.state.state.chat_active:
            return None, False
        if not self.chat.can_write(team, channel):
            return None, False
        if message_id and self.chat.has_message(
            self.session.match_id, channel, message_id
        ):
            return None, False
        message = self.chat.post(
            self.session.match_id,
            channel=channel,
            author_id=author_id,
            author_name=author_id,
            team=team,
            text=text,
            message_id=message_id,
            ts=ts,
        )
        await self.emit(
            "chat_message",
            message=message.model_dump(mode="json"),
            channel=channel.value,
            channel_label=channel.label,
        )
        return message, True

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------

    def roster_payload(self) -> dict[str, Any]:
        return {
            "radiant": [self._player_payload(p) for p in self.session.roster.radiant],
            "dire": [self._player_payload(p) for p in self.session.roster.dire],
            "unknown": [self._player_payload(p) for p in self.session.roster.unknown],
            "radiant_channel_id": self.session.radiant_channel_id,
            "dire_channel_id": self.session.dire_channel_id,
        }

    def chat_payload(self, team: Team) -> dict[str, Any]:
        """Everything the UI needs to draw the chat panel for one side.

        When chat is switched off the panel is empty rather than merely flagged,
        so a client that ignores the flag still shows nothing to write to.
        """
        if not self.settings.chat_enabled:
            return {
                "team": team.value,
                "channels": [],
                "history": {},
                "enabled": False,
            }
        channels = self.chat_channels(team)
        history: dict[str, list[dict]] = {}
        for entry in channels:
            channel = ChatChannel(entry["value"])
            history[channel.value] = self.chat_history(team=team, channel=channel)
        return {
            "team": team.value,
            "channels": channels,
            "history": history,
            "enabled": True,
        }

    def local_player(self) -> Player | None:
        for player in self.session.roster.all_players():
            if player.is_local:
                return player
        return None

    def local_team(self) -> Team:
        """The side the local player is on, or UNKNOWN if the game has not said."""
        player = self.local_player()
        return player.team if player is not None else Team.UNKNOWN

    def _player_payload(self, player: Player) -> dict[str, Any]:
        state = self.session.voice.get(player.player_id)
        return {
            "player_id": player.player_id,
            "name": player.name or player.player_id,
            # A name the UI can always show: the game name when we have it, a
            # neutral "Игрок N" from the slot otherwise (a normal, non-spectating
            # client often reports only a bare slot with no name at all).
            "display_name": player_display_name(player),
            "team": player.team.value,
            "team_label": player.team.label,
            "hero": player.hero,
            "hero_id": player.hero_id,
            "hero_icon": player.hero_icon,
            # "Выбор…" until a hero is picked, so a row is never blank.
            "hero_label": player.hero_label,
            "steam_id": player.steam_id,
            "slot": player.slot,
            "is_local": player.is_local,
            "connected": player.connected,
            "speaking": bool(state and state.speaking),
            "muted": bool(state and state.muted),
            "deafened": bool(state and state.deafened),
            "channel_id": state.channel_id if state else None,
        }

    def snapshot(self) -> dict[str, Any]:
        window = self.state.window
        return {
            "match_id": self.session.match_id,
            "conversation_id": self.session.conversation_id,
            "phase": self.session.phase.value,
            "state": self.state.state.value,
            "state_label": self.state.state.label,
            "voice_active": self.state.state.voice_active,
            "chat_active": self.state.state.chat_active,
            "status": self.session.status.value,
            "reconnects": self.session.reconnects,
            "revision": self.session.revision,
            "created_at": self.session.created_at,
            "updated_at": self.session.updated_at,
            "last_seen": self.session.last_seen,
            "llm_enabled": self.settings.llm_enabled,
            "voice_backend": self.settings.voice,
            "invite_key": self.session.invite_key,
            "post_game_until": self.session.post_game_until,
            "post_game_remaining": window.remaining() if window else 0.0,
            "departed": list(self.session.departed),
            # Which chat rooms the local player can write to right now. Carried
            # in the state so the overlay learns that the post-match room opened
            # without polling; the history itself is fetched once per channel set.
            "chat_channels": self.chat_channels(self.local_team()),
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
