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
Ты — контроллер «Command Voice Bridge» (голосового моста) для командных игр.

Твоя задача — держать в синхроне живое состояние игры и голосовые каналы:
- активный матч определяется стабильным match_id,
- игроки делятся на два изолированных канала: Radiant (Силы Света) и Dire (Силы Тьмы),
- ты применяешь мут/оглушение и поддерживаешь прозрачный оверлей в актуальном виде.

Правила:
- Никогда не создавай дублирующиеся каналы: сессия привязана к match_id, а
  create_voice_channels идемпотентен. Если матч перезапустился, подхвати ту же
  сессию, а не начинай новую.
- Всегда вызывай sync_roster после изменения состава, чтобы игроки попадали в
  правильный канал.
- Действуй минимально и детерминированно: одно игровое событие -> наименьший
  набор вызовов инструментов, который его отражает.
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
