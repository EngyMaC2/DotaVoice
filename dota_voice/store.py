"""Session persistence.

A "катка" is stored keyed by its stable ``match_id``. Two backends are
provided:

* :class:`MemoryStore` - process-local, zero setup (default).
* :class:`RedisStore` - survives service restarts (``DOTAVOICE_STORE=redis``).

Both expose the same async interface so the rest of the service is unaware of
which one is in use.
"""

from __future__ import annotations

from typing import Protocol

from .models import MatchSession


class Store(Protocol):
    async def get(self, match_id: str) -> MatchSession | None: ...

    async def put(self, session: MatchSession) -> None: ...

    async def delete(self, match_id: str) -> None: ...

    async def list(self) -> list[MatchSession]: ...

    async def close(self) -> None: ...


class MemoryStore:
    def __init__(self) -> None:
        self._data: dict[str, MatchSession] = {}

    async def get(self, match_id: str) -> MatchSession | None:
        session = self._data.get(match_id)
        return session.model_copy(deep=True) if session else None

    async def put(self, session: MatchSession) -> None:
        self._data[session.match_id] = session.model_copy(deep=True)

    async def delete(self, match_id: str) -> None:
        self._data.pop(match_id, None)

    async def list(self) -> list[MatchSession]:
        return [s.model_copy(deep=True) for s in self._data.values()]

    async def close(self) -> None:  # pragma: no cover - nothing to release
        self._data.clear()


class RedisStore:
    """Redis-backed store. Values are JSON-encoded :class:`MatchSession`s."""

    KEY_PREFIX = "dotavoice:session:"

    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis  # imported lazily

        self._client = aioredis.from_url(url, decode_responses=True)

    def _key(self, match_id: str) -> str:
        return f"{self.KEY_PREFIX}{match_id}"

    async def get(self, match_id: str) -> MatchSession | None:
        raw = await self._client.get(self._key(match_id))
        if not raw:
            return None
        return MatchSession.model_validate_json(raw)

    async def put(self, session: MatchSession) -> None:
        await self._client.set(self._key(session.match_id), session.model_dump_json())

    async def delete(self, match_id: str) -> None:
        await self._client.delete(self._key(match_id))

    async def list(self) -> list[MatchSession]:
        keys = [k async for k in self._client.scan_iter(f"{self.KEY_PREFIX}*")]
        if not keys:
            return []
        values = await self._client.mget(keys)
        return [MatchSession.model_validate_json(v) for v in values if v]

    async def close(self) -> None:
        await self._client.aclose()


def build_store(kind: str, redis_url: str) -> Store:
    if kind == "redis":
        try:
            return RedisStore(redis_url)
        except Exception:  # pragma: no cover - fall back when redis is absent
            return MemoryStore()
    return MemoryStore()
