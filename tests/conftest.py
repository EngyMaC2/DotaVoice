"""Shared pytest fixtures.

The agent layer is exercised with the real OpenHands SDK (real conversations,
real tool execution) but with a stub LLM key so no network calls happen.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dota_voice.bus import EventBus
from dota_voice.config import Settings
from dota_voice.store import MemoryStore
from dota_voice.voice.memory import MemoryVoiceBackend


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        store="memory",
        voice="memory",
        llm_api_key=None,
        workspace_dir=tmp_path,
        session_grace_seconds=900,
    )


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def voice() -> MemoryVoiceBackend:
    return MemoryVoiceBackend()


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
async def manager(settings, store, voice, bus):
    from dota_voice.agent import SessionManager

    manager = SessionManager(store=store, voice=voice, bus=bus, settings=settings)
    manager.bind_loop(asyncio.get_running_loop())
    yield manager
    await manager.close()
