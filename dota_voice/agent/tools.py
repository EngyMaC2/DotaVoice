"""OpenHands custom tools exposing voice-bridge capabilities to the agent.

Each tool follows the SDK's Action / Observation / Executor / ToolDefinition
pattern. Executors resolve their per-match :class:`MatchSessionRuntime` through
the conversation id, so one tool class serves many concurrent matches.

Because the SDK calls executors synchronously but the voice/overlay stack is
async, executors use :meth:`MatchSessionRuntime.run_sync` to hop back onto the
service event loop. The orchestrator always dispatches tool/agent work from a
worker thread, so this never blocks the loop.
"""

from __future__ import annotations

from collections.abc import Sequence

from openhands.sdk import Action, Observation, TextContent
from openhands.sdk.tool import ToolDefinition, ToolExecutor, register_tool
from pydantic import Field

from ..models import Team
from .context import require_runtime


def _runtime(conversation):
    if conversation is None:
        raise RuntimeError("Tool executed without a conversation context")
    return require_runtime(str(conversation.id))


# ---------------------------------------------------------------------------
# create_voice_channels
# ---------------------------------------------------------------------------


class CreateVoiceChannelsAction(Action):
    match_id: str = Field(description="Стабильный id матча, к которому привязана сессия.")


class CreateVoiceChannelsObservation(Observation):
    radiant_channel_id: str | None = None
    dire_channel_id: str | None = None
    created: bool = False

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        verb = "Created" if self.created else "Reused"
        return [
            TextContent(
                text=(
                    f"{verb} voice channels: radiant={self.radiant_channel_id}, "
                    f"dire={self.dire_channel_id}"
                )
            )
        ]


class CreateVoiceChannelsExecutor(
    ToolExecutor[CreateVoiceChannelsAction, CreateVoiceChannelsObservation]
):
    def __call__(
        self, action: CreateVoiceChannelsAction, conversation=None
    ) -> CreateVoiceChannelsObservation:
        runtime = _runtime(conversation)
        created = runtime.run_sync(runtime.ensure_channels())
        return CreateVoiceChannelsObservation(
            radiant_channel_id=runtime.session.radiant_channel_id,
            dire_channel_id=runtime.session.dire_channel_id,
            created=created,
        )


class CreateVoiceChannelsTool(
    ToolDefinition[CreateVoiceChannelsAction, CreateVoiceChannelsObservation]
):
    """Create the two isolated team voice channels for the active match."""

    @classmethod
    def create(cls, conv_state, **params) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=(
                    "Создать (или переиспользовать) два изолированных голосовых канала для "
                    "активного матча: Radiant (Силы Света) и Dire (Силы Тьмы). "
                    "Идемпотентно по match_id — повторный вызов возвращает уже "
                    "существующие каналы и никогда не создаёт дубликаты."
                ),
                action_type=CreateVoiceChannelsAction,
                observation_type=CreateVoiceChannelsObservation,
                executor=CreateVoiceChannelsExecutor(),
            )
        ]


# ---------------------------------------------------------------------------
# sync_roster
# ---------------------------------------------------------------------------


class SyncRosterAction(Action):
    match_id: str = Field(description="Id матча, чей состав нужно синхронизировать.")


class SyncRosterObservation(Observation):
    radiant: list[str] = Field(default_factory=list)
    dire: list[str] = Field(default_factory=list)
    moved: int = 0

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [
            TextContent(
                text=(
                    f"Состав синхронизирован: radiant={self.radiant}, dire={self.dire} "
                    f"(размещено участников: {self.moved})"
                )
            )
        ]


class SyncRosterExecutor(ToolExecutor[SyncRosterAction, SyncRosterObservation]):
    def __call__(self, action: SyncRosterAction, conversation=None) -> SyncRosterObservation:
        runtime = _runtime(conversation)
        moved = runtime.run_sync(runtime.sync_roster())
        return SyncRosterObservation(
            radiant=runtime.session.players_by_team(Team.RADIANT),
            dire=runtime.session.players_by_team(Team.DIRE),
            moved=moved,
        )


class SyncRosterTool(ToolDefinition[SyncRosterAction, SyncRosterObservation]):
    """Place every player into the channel matching their current team."""

    @classmethod
    def create(cls, conv_state, **params) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=(
                    "Синхронизировать голосовые каналы с текущим составом из игры. "
                    "Переводит каждого игрока в канал Radiant или Dire согласно "
                    "его команде и автоматически обрабатывает смену команды."
                ),
                action_type=SyncRosterAction,
                observation_type=SyncRosterObservation,
                executor=SyncRosterExecutor(),
            )
        ]


# ---------------------------------------------------------------------------
# set_mute
# ---------------------------------------------------------------------------


class SetMuteAction(Action):
    player_id: str = Field(description="Id или отображаемое имя игрока.")
    muted: bool = Field(default=True, description="True — заглушить, False — включить звук.")
    match_id: str | None = Field(default=None, description="Необязательный id матча.")


class SetMuteObservation(Observation):
    player_id: str
    muted: bool
    ok: bool = True

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [
            TextContent(
                text=f"{self.player_id} теперь {'в муте' if self.muted else 'без мута'}"
            )
        ]


class SetMuteExecutor(ToolExecutor[SetMuteAction, SetMuteObservation]):
    def __call__(self, action: SetMuteAction, conversation=None) -> SetMuteObservation:
        runtime = _runtime(conversation)
        ok = runtime.run_sync(runtime.set_mute(action.player_id, action.muted))
        return SetMuteObservation(player_id=action.player_id, muted=action.muted, ok=ok)


class SetMuteTool(ToolDefinition[SetMuteAction, SetMuteObservation]):
    """Server-side mute / unmute of a player (push-to-talk friendly)."""

    @classmethod
    def create(cls, conv_state, **params) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=(
                    "Mute or unmute a player at the voice-server level and reflect "
                    "it in the overlay. Idempotent."
                ),
                action_type=SetMuteAction,
                observation_type=SetMuteObservation,
                executor=SetMuteExecutor(),
            )
        ]


# ---------------------------------------------------------------------------
# set_deafen
# ---------------------------------------------------------------------------


class SetDeafenAction(Action):
    player_id: str = Field(description="Id или отображаемое имя игрока.")
    deafened: bool = Field(default=True, description="True — оглушить, иначе снять оглушение.")


class SetDeafenObservation(Observation):
    player_id: str
    deafened: bool
    ok: bool = True

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        state = "оглушён" if self.deafened else "без оглушения"
        return [TextContent(text=f"{self.player_id} теперь {state}")]


class SetDeafenExecutor(ToolExecutor[SetDeafenAction, SetDeafenObservation]):
    def __call__(self, action: SetDeafenAction, conversation=None) -> SetDeafenObservation:
        runtime = _runtime(conversation)
        ok = runtime.run_sync(runtime.set_deafen(action.player_id, action.deafened))
        return SetDeafenObservation(
            player_id=action.player_id, deafened=action.deafened, ok=ok
        )


class SetDeafenTool(ToolDefinition[SetDeafenAction, SetDeafenObservation]):
    """Server-side deafen / undeafen of a player."""

    @classmethod
    def create(cls, conv_state, **params) -> Sequence[ToolDefinition]:
        return [
            cls(
                description="Оглушить или снять оглушение с игрока на уровне голосового сервера.",
                action_type=SetDeafenAction,
                observation_type=SetDeafenObservation,
                executor=SetDeafenExecutor(),
            )
        ]


# ---------------------------------------------------------------------------
# get_session_state
# ---------------------------------------------------------------------------


class GetSessionStateAction(Action):
    match_id: str | None = Field(default=None, description="Необязательный id матча.")


class GetSessionStateObservation(Observation):
    match_id: str
    phase: str
    status: str
    radiant: list[str] = Field(default_factory=list)
    dire: list[str] = Field(default_factory=list)
    muted: list[str] = Field(default_factory=list)
    reconnects: int = 0

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [
            TextContent(
                text=(
                    f"match={self.match_id} phase={self.phase} status={self.status} "
                    f"reconnects={self.reconnects}\n"
                    f"radiant={self.radiant}\ndire={self.dire}\nmuted={self.muted}"
                )
            )
        ]


class GetSessionStateExecutor(ToolExecutor[GetSessionStateAction, GetSessionStateObservation]):
    def __call__(
        self, action: GetSessionStateAction, conversation=None
    ) -> GetSessionStateObservation:
        return _runtime(conversation).state_observation()


class GetSessionStateTool(ToolDefinition[GetSessionStateAction, GetSessionStateObservation]):
    """Read the current session state (roster, mutes, reconnect count)."""

    @classmethod
    def create(cls, conv_state, **params) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=(
                    "Return the current voice-bridge session state: match phase, "
                    "team rosters, muted players, and reconnect count."
                ),
                action_type=GetSessionStateAction,
                observation_type=GetSessionStateObservation,
                executor=GetSessionStateExecutor(),
            )
        ]


# ---------------------------------------------------------------------------
# end_session
# ---------------------------------------------------------------------------


class EndSessionAction(Action):
    reason: str = Field(default="match_ended", description="Why the session ended.")


class EndSessionObservation(Observation):
    match_id: str
    ended: bool = True

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [TextContent(text=f"Session {self.match_id} ended")]


class EndSessionExecutor(ToolExecutor[EndSessionAction, EndSessionObservation]):
    def __call__(self, action: EndSessionAction, conversation=None) -> EndSessionObservation:
        runtime = _runtime(conversation)
        runtime.run_sync(runtime.end(action.reason))
        return EndSessionObservation(match_id=runtime.session.match_id, ended=True)


class EndSessionTool(ToolDefinition[EndSessionAction, EndSessionObservation]):
    """Mark the match session ended and tear down its voice channels."""

    @classmethod
    def create(cls, conv_state, **params) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=(
                    "End the current match session: mark it ended, notify the "
                    "overlay, and tear down the two voice channels."
                ),
                action_type=EndSessionAction,
                observation_type=EndSessionObservation,
                executor=EndSessionExecutor(),
            )
        ]


ALL_TOOLS = [
    CreateVoiceChannelsTool,
    SyncRosterTool,
    SetMuteTool,
    SetDeafenTool,
    GetSessionStateTool,
    EndSessionTool,
]


def register_all() -> None:
    """Register every voice-bridge tool by name (idempotent)."""
    for tool in ALL_TOOLS:
        if tool.name not in _REGISTERED:
            register_tool(tool.name, tool)
            _REGISTERED.add(tool.name)


_REGISTERED: set[str] = set()
