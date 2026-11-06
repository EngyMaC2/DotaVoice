"""Orchestrator: translates game-state events into agent tool calls.

This is the "agentic controller" from the design: it receives a normalized
:class:`GameStateUpdate`, resolves (or creates) the session for that match id,
and drives the OpenHands tools to keep voice channels + overlay in sync.

In deterministic mode the mapping is explicit and testable:

    match_started        -> create_voice_channels + sync_roster
    roster_changed       -> sync_roster (+ move_player per changed player)
    player_switched_team -> move_player
    match_ended          -> end_session

When an LLM key is configured the same event is also handed to the agent so it
can reason about ambiguous cases, but the deterministic path always runs so the
bridge never depends on model availability.
"""

from __future__ import annotations

import logging

from ..gsi import GameStateUpdate
from ..models import MatchPhase, Team
from .manager import SessionManager
from .runtime import MatchSessionRuntime
from .tools import (
    CreateVoiceChannelsAction,
    CreateVoiceChannelsTool,
    EndSessionAction,
    EndSessionTool,
    MovePlayerAction,
    MovePlayerTool,
    SyncRosterAction,
    SyncRosterTool,
)

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager

    async def handle(self, update: GameStateUpdate) -> dict:
        """Process one GSI update. Returns a small result summary."""
        self.manager.metrics["events"] += 1

        if update.match_id is None:
            # No match bound yet (menus, pre-match). Nothing to orchestrate.
            return {"action": "ignored", "reason": "no_match_id"}

        runtime, is_new = await self.manager.attach(update.match_id)

        previous = runtime.session.roster.model_copy(deep=True)
        changed = runtime.apply_roster(update.roster, update.phase)

        actions: list[str] = []

        # 1. Channels + initial placement.
        if is_new or runtime.session.radiant_channel_id is None:
            await self._call(runtime, CreateVoiceChannelsTool.name,
                             CreateVoiceChannelsAction(match_id=update.match_id))
            await self._call(runtime, SyncRosterTool.name,
                             SyncRosterAction(match_id=update.match_id))
            actions += ["create_voice_channels", "sync_roster"]
            if is_new:
                await runtime.start()

        # 2. End of match (checked before switches: no point moving players
        #    around in a game that is already over).
        if update.phase is MatchPhase.ENDED:
            await self._call(runtime, EndSessionTool.name,
                             EndSessionAction(reason="match_ended"))
            actions.append("end_session")
            await runtime.emit("state", **runtime.snapshot())
            await self.manager.detach(update.match_id)
            return {
                "action": "handled",
                "match_id": update.match_id,
                "is_new": is_new,
                "actions": actions,
                "changed": changed,
            }

        # 3. Team switches -> per-player move.
        if changed and not is_new:
            switches = self._detect_switches(previous, runtime)
            for player_id, team in switches:
                await self._call(
                    runtime,
                    MovePlayerTool.name,
                    MovePlayerAction(player_id=player_id, team=team),
                )
                actions.append(f"move_player:{player_id}->{team.value}")
            # A full sync is cheap and guarantees consistency for players that
            # were added/removed without an explicit team switch.
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

    @staticmethod
    def _detect_switches(
        previous, runtime: MatchSessionRuntime
    ) -> list[tuple[str, Team]]:
        """Return players whose team changed between two roster snapshots."""
        switches: list[tuple[str, Team]] = []
        for player in runtime.session.roster.all_players():
            old_team = previous.team_of(player.player_id)
            if old_team is Team.UNKNOWN:
                continue
            if old_team is not player.team and player.team is not Team.UNKNOWN:
                switches.append((player.player_id, player.team))
        return switches

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
