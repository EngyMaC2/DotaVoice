"""Keeps the app itself joined to every active match room.

The desktop app is not a passive controller: it sits in the voice room like any
other player. This component watches the session event bus and, for each active
match, opens a :class:`~dota_voice.voice.relay_client.RelayClient` bound to that
room. Audio heard from peers is turned into overlay ``speaking`` state so the
window and the transparent overlay light up whoever is talking.

Because joining is driven by session events (which are keyed by ``match_id``),
the app connects to a match automatically as soon as GSI reports one, and
reconnects to the same room if the session is re-attached.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from urllib.parse import urlparse

from .bus import EventBus
from .config import Settings
from .models import OverlayEvent
from .voice.relay_client import RelayClient

logger = logging.getLogger(__name__)

# How long after the last audio packet a peer is still shown as speaking.
SPEAKING_HOLD = 0.5
_SWEEP_INTERVAL = 0.25


def parse_relay_url(url: str, default_port: int) -> tuple[str, int]:
    """Turn ``udp://host:port`` (or ``host:port``) into ``(host, port)``."""
    if not url:
        return "127.0.0.1", default_port
    parsed = urlparse(url if "://" in url else f"udp://{url}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or default_port
    return host, port


class VoicePresence:
    def __init__(
        self,
        bus: EventBus,
        manager,
        settings: Settings,
        *,
        on_join=None,
        relay=None,
    ) -> None:
        self._bus = bus
        self._manager = manager
        self._settings = settings
        self._on_join = on_join
        self._relay = relay
        self._clients: dict[str, RelayClient] = {}
        self._last_heard: dict[tuple[str, str], float] = {}
        self._speaking: dict[tuple[str, str], bool] = {}
        self._task: asyncio.Task | None = None
        self._sweeper: asyncio.Task | None = None
        self._host, self._port = parse_relay_url(
            settings.voice_relay_url, settings.port
        )

    def _endpoint(self) -> tuple[str, int]:
        """Resolve the relay endpoint, following an embedded relay's real port."""
        if self._settings.voice_relay_url:
            return self._host, self._port
        if self._relay is not None:
            return self._relay.host, self._relay.port
        return self._host, self._port

    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._consume())
        self._sweeper = asyncio.create_task(self._sweep())

    async def stop(self) -> None:
        for task in (self._task, self._sweeper):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._task = self._sweeper = None
        for client in list(self._clients.values()):
            await client.stop()
        self._clients.clear()

    # ------------------------------------------------------------------

    async def _consume(self) -> None:
        queue = await self._bus.subscribe()
        try:
            while True:
                event: OverlayEvent = await queue.get()
                if event.match_id is None:
                    continue
                if event.type in ("session_started", "state", "roster_synced",
                                  "reconnected"):
                    await self._ensure_client(event.match_id)
                elif event.type == "session_ended":
                    await self._drop_client(event.match_id)
        finally:
            await self._bus.unsubscribe(queue)

    async def _ensure_client(self, match_id: str) -> None:
        runtime = self._manager.get_runtime(match_id)
        if runtime is None:
            return
        client = self._clients.get(match_id)
        if client is not None:
            # Keep the room and the local team in step with the session.
            client.set_team(self._local_team(runtime))
            return
        name = self._local_name(runtime)
        host, port = self._endpoint()
        client = RelayClient(
            room=match_id,
            player=name,
            relay_host=host,
            relay_port=port,
            input_device=self._settings.input_device or None,
            output_device=self._settings.output_device or None,
            on_peer_speaking=lambda player, speaking: self._on_peer(
                match_id, player, speaking
            ),
        )
        client.set_team(self._local_team(runtime))
        try:
            await client.start()
        except Exception as exc:  # pragma: no cover - socket failure
            logger.warning("Не удалось подключиться к комнате %s: %s", match_id, exc)
            return
        self._clients[match_id] = client
        if self._on_join is not None:
            with contextlib.suppress(Exception):
                self._on_join(match_id, client)

    async def _drop_client(self, match_id: str) -> None:
        client = self._clients.pop(match_id, None)
        if client is not None:
            await client.stop()
        for key in [k for k in self._last_heard if k[0] == match_id]:
            self._last_heard.pop(key, None)
            self._speaking.pop(key, None)

    # ------------------------------------------------------------------

    def _local_name(self, runtime) -> str:
        # The stable in-game id is the relay identity; the display name is only
        # for humans, and the overlay renders it from the GSI roster.
        for player in runtime.session.roster.all_players():
            if player.is_local:
                return player.player_id
        return self._settings.display_name or "хост"

    def _local_team(self, runtime) -> str:
        for player in runtime.session.roster.all_players():
            if player.is_local:
                return player.team.value
        return "radiant"

    def _on_peer(self, match_id: str, player: str, speaking: bool) -> None:
        key = (match_id, player)
        self._last_heard[key] = time.monotonic()
        if speaking:
            self._set_speaking(match_id, player, True)

    def _set_speaking(self, match_id: str, player: str, speaking: bool) -> None:
        key = (match_id, player)
        if self._speaking.get(key) == speaking:
            return
        self._speaking[key] = speaking
        runtime = self._manager.get_runtime(match_id)
        if runtime is None or runtime._loop is None:
            return
        coro = runtime.set_speaking(player, speaking)
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(coro, runtime._loop)

    async def _sweep(self) -> None:
        """Clear the speaking flag once a peer has been silent for a moment."""
        while True:
            await asyncio.sleep(_SWEEP_INTERVAL)
            now = time.monotonic()
            for key, last in list(self._last_heard.items()):
                if self._speaking.get(key) and now - last > SPEAKING_HOLD:
                    self._set_speaking(key[0], key[1], False)

    # ------------------------------------------------------------------

    def client_for(self, match_id: str) -> RelayClient | None:
        return self._clients.get(match_id)

    def set_transmitting(self, active: bool) -> list[str]:
        """Start/stop sending the local microphone in every joined room."""
        for client in self._clients.values():
            client.set_transmitting(active)
        return list(self._clients)

    def set_deafened(self, deafened: bool) -> list[str]:
        for client in self._clients.values():
            client.set_deafened(deafened)
        return list(self._clients)

    @property
    def joined_rooms(self) -> list[str]:
        return list(self._clients)
