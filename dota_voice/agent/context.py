"""Registry that lets tool executors reach the runtime for their conversation.

Each match owns exactly one OpenHands conversation, and therefore exactly one
:class:`MatchSessionRuntime`. Tool executors receive the ``conversation`` in
their ``__call__``, so they look the runtime up by conversation id here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .runtime import MatchSessionRuntime

_RUNTIMES: dict[str, "MatchSessionRuntime"] = {}


def register_runtime(conversation_id: str, runtime: "MatchSessionRuntime") -> None:
    _RUNTIMES[str(conversation_id)] = runtime


def require_runtime(conversation_id: str) -> "MatchSessionRuntime":
    runtime = _RUNTIMES.get(str(conversation_id))
    if runtime is None:
        raise RuntimeError(
            f"No voice runtime registered for conversation {conversation_id!r}"
        )
    return runtime


def unregister_runtime(conversation_id: str) -> None:
    _RUNTIMES.pop(str(conversation_id), None)
