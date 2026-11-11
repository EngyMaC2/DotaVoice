"""Agent layer: OpenHands tools, per-match runtime, session manager, orchestrator."""

from .factory import SYSTEM_PROMPT, build_agent
from .manager import SessionManager
from .orchestrator import Orchestrator
from .runtime import MatchSessionRuntime, conversation_id_for_match

__all__ = [
    "build_agent",
    "SYSTEM_PROMPT",
    "MatchSessionRuntime",
    "conversation_id_for_match",
    "SessionManager",
    "Orchestrator",
]
