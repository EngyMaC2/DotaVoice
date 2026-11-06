"""End-to-end demo of the Command Voice Bridge, fully in-process.

It wires the real service (real OpenHands conversations, real tools, real
session store) and feeds simulated GSI payloads through it, printing the
resulting voice state at each step. No network and no game required.

Run::

    python -m scripts.demo
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from dota_voice.agent import Orchestrator, SessionManager
from dota_voice.bus import EventBus
from dota_voice.config import Settings
from dota_voice.gsi import parse_gsi
from dota_voice.store import MemoryStore
from dota_voice.voice.memory import MemoryVoiceBackend

from .simulate import DIRE, RADIANT, payload


def _settings(workspace: Path) -> Settings:
    return Settings(
        store="memory",
        voice="memory",
        llm_api_key=None,
        workspace_dir=workspace,
        session_grace_seconds=900,
    )


async def _build(workspace: Path):
    settings = _settings(workspace)
    bus = EventBus()
    voice = MemoryVoiceBackend()
    manager = SessionManager(
        store=MemoryStore(), voice=voice, bus=bus, settings=settings
    )
    manager.bind_loop(asyncio.get_running_loop())
    orchestrator = Orchestrator(manager)
    return settings, bus, voice, manager, orchestrator


async def run_demo() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        settings, bus, voice, manager, orchestrator = await _build(workspace)

        overlay_events: list[dict] = []

        async def collect():
            async for event in bus.stream():
                overlay_events.append(event.model_dump(mode="json"))

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0.05)

        match_id = "7123456789"

        async def feed(label: str, body: dict) -> dict:
            print(f"\n=== {label} ===")
            result = await orchestrator.handle(parse_gsi(body))
            print("результат:", json.dumps(result, ensure_ascii=False))
            _print_state(manager, match_id)
            return result

        await feed("1. матч начался", payload(match_id, "DOTA_GAMERULES_STATE_PRE_GAME"))
        await feed("2. игра идёт", payload(match_id, clock=60))

        radiant = [p for p in RADIANT if p["name"] != "Ember"]
        dire = [*DIRE, RADIANT[2]]
        await feed(
            "3. Ember меняет команду: Силы Света -> Силы Тьмы",
            payload(match_id, radiant=radiant, dire=dire, clock=120),
        )

        print("\n=== 4. мут игрока 101 + речь игрока 202 ===")
        runtime = manager.get_runtime(match_id)
        await runtime.set_mute("101", True)
        await runtime.set_speaking("202", True)
        _print_state(manager, match_id)

        await feed(
            "5. матч завершён",
            payload(match_id, "DOTA_GAMERULES_STATE_POST_GAME", clock=2400),
        )

        await asyncio.sleep(0.1)
        collector.cancel()
        print(f"\nСобытий для оверлея опубликовано: {len(overlay_events)}")
        for ev in overlay_events:
            print("  -", ev["type"])
        print("Сессия сохранена под match_id:", match_id)
        print("Каталог диалогов агента:", settings.conversations_dir)
        for path in sorted(settings.conversations_dir.glob("*/base_state.json")):
            print("  диалог:", path.parent.name)
        await manager.close()
        await voice.close()


def _print_state(manager, match_id: str) -> None:
    runtime = manager.get_runtime(match_id)
    if runtime is None:
        print("  (сессия завершена / отключена)")
        return
    snap = runtime.snapshot()
    phase = {
        "pre_game": "подготовка",
        "playing": "идёт игра",
        "ended": "матч завершён",
        "none": "нет матча",
    }.get(snap["phase"], snap["phase"])
    status = "активна" if snap["status"] == "active" else "завершена"
    print(f"  фаза={phase} статус={status} переподключений={snap['reconnects']}")
    for team, label in (("radiant", "Силы Света"), ("dire", "Силы Тьмы")):
        names = [
            f"{p['name']}{'[мут]' if p['muted'] else ''}{'[говорит]' if p['speaking'] else ''}"
            for p in snap[team]
        ]
        print(f"  {label}: {names}")


def main() -> None:
    asyncio.run(run_demo())


if __name__ == "__main__":
    main()
