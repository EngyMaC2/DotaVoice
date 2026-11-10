"""Orchestrator: translates game-state events into agent tool calls.

This is the "agentic controller" from the design: it receives a normalized
:class:`GameStateUpdate`, resolves (or creates) the session for that match id,
and drives the OpenHands tools to keep voice channels + overlay in sync.

In deterministic mode the mapping is explicit and testable:

    match_started        -> create_voice_channels + sync_roster
    roster_changed       -> sync_roster
    match_ended          -> end_session

Teams are fixed for the duration of a match: players are placed once, from the
game's own roster (or from a peer's client when the game reports only the local
slot), and are never reshuffled. There is deliberately no "move player" action -
letting anyone cross the boundary would break the two-channel isolation the
whole design exists to provide.

When an LLM key is configured the same event is also handed to the agent so it
can reason about ambiguous cases, but the deterministic path always runs so the
bridge never depends on model availability.
"""

from __future__ import annotations

import contextlib
import logging

from ..gsi import GameStateUpdate
from ..models import MatchPhase, OverlayEvent
from .manager import SessionManager
from .runtime import MatchSessionRuntime
from .tools import (
    CreateVoiceChannelsAction,
    CreateVoiceChannelsTool,
    EndSessionAction,
    EndSessionTool,
    SyncRosterAction,
    SyncRosterTool,
)

logger = logging.getLogger(__name__)


def _copy_to_clipboard(text: str) -> bool:
    """Best-effort clipboard write; a missing clipboard is not an error."""
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()
        return True
    except Exception as exc:  # pragma: no cover - headless / no display
        logger.debug("Не удалось скопировать приглашение в буфер: %s", exc)
        return False


class Orchestrator:
    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager

    async def handle(self, update: GameStateUpdate) -> dict:
        """Process one GSI update. Returns a small result summary."""
        self.manager.metrics["events"] += 1

        if update.match_id is None:
            # No match bound: the client is in the menu or has closed. Any
            # session still active is a leftover, so close it now instead of
            # letting a "phantom match" sit in the overlay and voice rooms until
            # the idle reaper finally gets to it.
            ended = await self.manager.end_all_active("left_match")
            if ended:
                for match_id in ended:
                    await self.manager.bus.publish(
                        OverlayEvent(
                            type="session_closed",
                            match_id=match_id,
                            payload={"reason": "left_match"},
                        )
                    )
            return {
                "action": "cleared" if ended else "ignored",
                "reason": "no_match_id",
                "ended": ended,
            }

        # A brand-new match id means the previous game is over, even when its
        # client never sent POST_GAME. End any other session first so two matches
        # never share the overlay or the voice rooms. A no-op for a match we
        # already track, so the common case costs nothing.
        await self.manager.end_other_matches(update.match_id, "new_match")

        runtime, is_new = await self.manager.attach(update.match_id)

        # Follow the game's phase into the match lifecycle (IDLE -> DRAFT ->
        # IN_GAME). This is idempotent and forward-only, so a late or duplicate
        # packet cannot reopen a finished match.
        await runtime.advance_to_phase(update.phase)

        changed = runtime.apply_roster(update.roster, update.phase)

        actions: list[str] = []

        # 1. Channels + initial placement.
        #
        # The room exists exactly while the match's *lifecycle* says voice is
        # live - not merely when the incoming phase looks live. A restart during
        # the post-match window, or a late PLAYING packet, must not resurrect a
        # room that was already torn down (that would create a duplicate channel
        # and let voice leak after the match). `ensure_channels` is idempotent, so
        # a repeated event re-attaches to the same room instead of making a second.
        if (
            runtime.state.state.voice_active
            and (is_new or runtime.session.radiant_channel_id is None)
        ):
            await self._call(runtime, CreateVoiceChannelsTool.name,
                             CreateVoiceChannelsAction(match_id=update.match_id))
            await self._call(runtime, SyncRosterTool.name,
                             SyncRosterAction(match_id=update.match_id))
            actions += ["create_voice_channels", "sync_roster"]
            if is_new:
                await runtime.start()
            await self._share_invite(runtime)
            actions.append("share_invite")

        # 2. End of match (checked before switches: no point moving players
        #    around in a game that is already over).
        #
        #    The voice rooms are torn down at once, but the session stays alive
        #    for the post-match text window (ТЗ §8.4): we do NOT detach here.
        #    The window is closed later - by its timer, or when everyone leaves.
        if update.phase is MatchPhase.ENDED:
            if runtime.state.window is not None or runtime.state.state.finished:
                # A repeat end event (the game keeps sending POST_GAME): the
                # window is already open, nothing to do but refresh the overlay.
                await runtime.emit("state", **runtime.snapshot())
                return {
                    "action": "handled",
                    "match_id": update.match_id,
                    "is_new": is_new,
                    "actions": actions,
                    "changed": changed,
                }
            await self._call(runtime, EndSessionTool.name,
                             EndSessionAction(reason="match_ended"))
            actions.append("end_session")
            await runtime.emit("state", **runtime.snapshot())
            return {
                "action": "handled",
                "match_id": update.match_id,
                "is_new": is_new,
                "actions": actions,
                "changed": changed,
            }

        # 3. Roster changed (a teammate connected, or the game corrected a
        #    side). Re-sync so everyone sits in the channel of their team. We
        #    never *move* a player between teams by hand: the placement always
        #    follows the game / the player's own client.
        if changed and not is_new:
            await self._call(runtime, SyncRosterTool.name,
                             SyncRosterAction(match_id=update.match_id))
            actions.append("sync_roster")

        # 4. Keep the overlay fresh on every update.
        await runtime.emit("state", **runtime.snapshot())
        runtime.session.touch()

        # 5. Optional LLM reasoning over the raw event (never on the hot path).
        if self.manager.settings.llm_enabled:
            self._maybe_ask_agent(runtime, update)

        return {
            "action": "handled",
            "match_id": update.match_id,
            "is_new": is_new,
            "actions": actions,
            "changed": changed,
        }

    # ------------------------------------------------------------------

    async def _share_invite(self, runtime: MatchSessionRuntime) -> None:
        """Put the room invite on the clipboard and announce it in the overlay.

        MVP for distributing the invite: the player pastes it to teammates. It is
        deliberately manual - auto-posting to the in-game chat is not something
        the safety policy allows without more thought.
        """
        invite = ""
        controller = getattr(runtime.voice, "invite_for", None)
        if callable(controller):
            with contextlib.suppress(Exception):
                invite = controller(runtime.session.match_id) or ""
        if not invite:
            return
        runtime.session.invite_key = invite
        copied = _copy_to_clipboard(invite)
        await runtime.emit(
            "invite_ready",
            invite_key=invite,
            copied=copied,
            message=(
                "Приглашение для команды скопировано — отправь союзникам"
                if copied
                else f"Приглашение для команды: {invite}"
            ),
        )

    async def _call(self, runtime: MatchSessionRuntime, tool_name: str, action) -> None:
        try:
            await self.manager.call_tool_async(runtime, tool_name, action)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("Инструмент %s завершился ошибкой для %s: %s", tool_name, runtime.session.match_id, exc)

    def _maybe_ask_agent(self, runtime: MatchSessionRuntime, update: GameStateUpdate) -> None:
        """Record the raw event for the LLM agent (no run triggered here)."""
        try:
            summary = (
                f"GSI event: phase={update.phase.value} match_id={update.match_id} "
                f"radiant={[p.player_id for p in update.roster.radiant]} "
                f"dire={[p.player_id for p in update.roster.dire]}"
            )
            self.manager.say(runtime, summary)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Не удалось записать событие в диалог: %s", exc)
