"""Session manager: one OpenHands conversation per match id.

This is where "одна катка без разрывов" is enforced. A session is created the
first time a ``match_id`` is seen and stored under that id. Every later event
for the same id - including one that arrives after the game client restarted -
re-attaches to the existing session and conversation rather than creating a
new one. Channels are therefore never duplicated.
"""

from __future__ import annotations

import asyncio
import logging
import time

from openhands.sdk import Conversation

from ..bus import EventBus
from ..config import Settings
from ..models import MatchSession, SessionStatus
from ..store import Store
from ..voice import VoiceBackend
from . import context
from .factory import build_agent
from .runtime import MatchSessionRuntime, conversation_id_for_match

logger = logging.getLogger(__name__)


class SessionManager:
    def __init__(
        self,
        *,
        store: Store,
        voice: VoiceBackend,
        bus: EventBus,
        settings: Settings,
    ) -> None:
        self.store = store
        self.voice = voice
        self.bus = bus
        self.settings = settings
        self._runtimes: dict[str, MatchSessionRuntime] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # match_id -> monotonic time it was last seen
        self._last_seen: dict[str, float] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        # simple metrics
        self.metrics = {
            "sessions_created": 0,
            "sessions_reused": 0,
            "reconnects": 0,
            "events": 0,
            "tool_calls": 0,
            "tool_errors": 0,
        }

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ------------------------------------------------------------------
    # lookup
    # ------------------------------------------------------------------

    def _lock(self, match_id: str) -> asyncio.Lock:
        lock = self._locks.get(match_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[match_id] = lock
        return lock

    def get_runtime(self, match_id: str) -> MatchSessionRuntime | None:
        return self._runtimes.get(match_id)

    def active_runtimes(self) -> list[MatchSessionRuntime]:
        return [
            rt
            for rt in self._runtimes.values()
            if rt.session.status is SessionStatus.ACTIVE
        ]

    def snapshots(self) -> list[dict]:
        return [rt.snapshot() for rt in self.active_runtimes()]

    # ------------------------------------------------------------------
    # attach / detach
    # ------------------------------------------------------------------

    async def attach(self, match_id: str) -> tuple[MatchSessionRuntime, bool]:
        """Return ``(runtime, is_new)`` for a match id, creating if needed.

        The whole operation is guarded by a per-match lock so two concurrent
        GSI events for a brand-new match cannot race into two sessions.
        """
        async with self._lock(match_id):
            existing = self._runtimes.get(match_id)
            if existing is not None:
                self._last_seen[match_id] = time.monotonic()
                self.metrics["sessions_reused"] += 1
                return existing, False

            session = await self.store.get(match_id)
            is_new = session is None or session.status is SessionStatus.ENDED
            if is_new:
                session = MatchSession(
                    match_id=match_id,
                    conversation_id=conversation_id_for_match(match_id),
                )
                self.metrics["sessions_created"] += 1
            else:
                # Same match seen again after a restart: reuse and count a
                # reconnect (note_reconnect below performs the increment).
                self.metrics["reconnects"] += 1

            runtime = MatchSessionRuntime(
                session,
                store=self.store,
                voice=self.voice,
                bus=self.bus,
                settings=self.settings,
                loop=self._loop,
            )
            self._attach_conversation(runtime)
            context.register_runtime(session.conversation_id, runtime)
            self._runtimes[match_id] = runtime
            self._last_seen[match_id] = time.monotonic()

            if not is_new:
                await runtime.note_reconnect()
                await runtime.ensure_channels()
            return runtime, is_new

    def _attach_conversation(self, runtime: MatchSessionRuntime) -> None:
        """Create or re-attach the OpenHands conversation for this match.

        Persistence makes a restart seamless: the same ``match_id`` maps to the
        same conversation id, so the history is reloaded rather than rebuilt. But
        a conversation written by an *older* build can name a tool that no longer
        exists (the tool set changed), and the SDK refuses to reload it - which
        would make every GSI tick fail. When that happens the saved state is
        unrecoverable, so we discard just this conversation's directory and start
        it clean; the voice session itself is keyed on the store, not on this
        history, so the match keeps its channels.
        """
        import uuid as _uuid

        conversation_id = _uuid.UUID(runtime.session.conversation_id)
        try:
            runtime.conversation = self._open_conversation(conversation_id)
        except Exception as exc:
            logger.warning(
                "Не удалось восстановить диалог %s (%s); начинаю заново",
                conversation_id,
                exc,
            )
            self._discard_conversation_state(conversation_id)
            runtime.conversation = self._open_conversation(conversation_id)
        runtime.conversation._ensure_agent_ready()
        if self._loop is not None:
            runtime.bind_loop(self._loop)

    def _open_conversation(self, conversation_id):
        return Conversation(
            agent=build_agent(self.settings),
            workspace=str(self.settings.workspace_dir),
            persistence_dir=str(self.settings.conversations_dir),
            conversation_id=conversation_id,
            visualizer=None,
            delete_on_close=False,
        )

    def _discard_conversation_state(self, conversation_id) -> None:
        """Remove a conversation's persisted state so it can be recreated."""
        import shutil

        path = self.settings.conversations_dir / conversation_id.hex
        try:
            shutil.rmtree(path, ignore_errors=True)
        except OSError as exc:  # pragma: no cover - defensive
            logger.debug("Не удалось удалить состояние диалога %s: %s", path, exc)

    async def detach(self, match_id: str, *, teardown: bool = False) -> None:
        runtime = self._runtimes.pop(match_id, None)
        if runtime is None:
            return
        if teardown:
            await runtime.end("teardown")
        await runtime.close()
        self._locks.pop(match_id, None)

    async def close(self) -> None:
        for runtime in list(self._runtimes.values()):
            await runtime.close()
        self._runtimes.clear()

    # ------------------------------------------------------------------
    # reaping
    # ------------------------------------------------------------------

    async def reap_stale(self, now: float | None = None) -> list[str]:
        """End sessions that have not been seen within the grace period.

        The grace window is what makes a *restart* safe: a client that drops
        for a moment and comes back with the same match id still finds the
        session alive. Only after the window do we tear channels down.
        """
        now = now if now is not None else time.monotonic()
        grace = self.settings.session_grace_seconds
        reaped: list[str] = []
        for match_id, runtime in list(self._runtimes.items()):
            if runtime.session.status is not SessionStatus.ACTIVE:
                continue
            last = self._last_seen.get(match_id, now)
            if now - last > grace:
                logger.info("Разбор устаревшей сессии %s (простой > %s с)", match_id, grace)
                await runtime.end("idle_timeout")
                await runtime.close()
                self._runtimes.pop(match_id, None)
                self._locks.pop(match_id, None)
                reaped.append(match_id)
        return reaped

    # ------------------------------------------------------------------
    # tool dispatch (used by the orchestrator)
    # ------------------------------------------------------------------

    def call_tool(self, runtime: MatchSessionRuntime, tool_name: str, action) -> object:
        """Execute a tool on the runtime's conversation (worker-thread safe).

        Must be called from a worker thread: the conversation executes the tool
        synchronously, and the tool hops back to the loop for async work.
        """
        conversation = runtime.conversation
        if conversation is None:
            raise RuntimeError(f"Session {runtime.session.match_id} has no conversation")
        self.metrics["tool_calls"] += 1
        try:
            return conversation.execute_tool(tool_name, action)
        except Exception:
            self.metrics["tool_errors"] += 1
            raise

    async def call_tool_async(self, runtime: MatchSessionRuntime, tool_name: str, action):
        """Async wrapper: runs the sync tool call on a worker thread."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self.call_tool, runtime, tool_name, action
        )

    def say(self, runtime: MatchSessionRuntime, message: str) -> None:
        """Record a game event in the conversation log (persisted history).

        Synchronous and cheap - it appends a user MessageEvent without
        triggering an LLM run. Used for the session audit trail.
        """
        if runtime.conversation is None:
            return
        runtime.conversation.send_message(message)
