"""An asyncio pub/sub bus used to fan overlay events out to WebSocket clients.

Kept intentionally tiny and dependency-free: subscribers get their own bounded
queue so a slow overlay client can never block the orchestrator.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from .models import OverlayEvent


class EventBus:
    def __init__(self, max_queue: int = 256) -> None:
        self._max_queue = max_queue
        self._subscribers: set[asyncio.Queue[OverlayEvent]] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[OverlayEvent]:
        queue: asyncio.Queue[OverlayEvent] = asyncio.Queue(maxsize=self._max_queue)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[OverlayEvent]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def publish(self, event: OverlayEvent) -> None:
        async with self._lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest event for this slow subscriber and retry once.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    async def stream(self) -> AsyncIterator[OverlayEvent]:
        queue = await self.subscribe()
        try:
            while True:
                yield await queue.get()
        finally:
            await self.unsubscribe(queue)
