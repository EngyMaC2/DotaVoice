"""Agent construction.

The bridge runs in two modes:

* **Deterministic** (default, no ``LLM_API_KEY``): the orchestrator maps game
  events straight to tool calls. No LLM is invoked, so there is no per-event
  cost or latency. This is the recommended production mode - the value of the
  OpenHands SDK here is reliable, typed tool execution plus per-match session
  lifecycle and persistence, not "chatting with an AI".
* **Reasoning** (``LLM_API_KEY`` set): the same tools are exposed to an LLM
  agent so it can make judgement calls (e.g. resolving a roster in a game that
  has no GSI by reading a screenshot, or deciding how to reconcile conflicting
  team data).
"""

from __future__ import annotations

from openhands.sdk import LLM, Agent
from openhands.sdk.tool import Tool
from pydantic import SecretStr

from ..config import Settings
from . import tools as voice_tools

SYSTEM_PROMPT = """\
You are the controller of a "Command Voice Bridge" for team games.

Your job is to keep the game's live state and the voice channels in sync:
- the active match is identified by a stable match id,
- players are split into two isolated channels: Radiant (Силы Света) and Dire (Силы Тьма),
- you apply mutes/deafens and keep the transparent overlay accurate.

Rules:
- Never create duplicate channels: the session is bound to the match id, and
  create_voice_channels is idempotent. If a match restarts, re-attach to the
  same session instead of starting a new one.
- Always call sync_roster after the roster changes so players land in the
  right channel.
- Keep actions minimal and deterministic: one game event -> the smallest set
  of tool calls that reflects it.
"""


def build_agent(settings: Settings) -> Agent:
    """Build the OpenHands agent for a match conversation.

    Tools are referenced by name (registered via ``voice_tools.register_all``),
    which keeps conversations serialisable and re-attachable across restarts.
    """
    voice_tools.register_all()
    tools = [Tool(name=tool.name) for tool in voice_tools.ALL_TOOLS]

    llm = LLM(
        usage_id="voice-bridge",
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=SecretStr(settings.llm_api_key or "not-used"),
    )
    return Agent(llm=llm, tools=tools, system_prompt=SYSTEM_PROMPT)
