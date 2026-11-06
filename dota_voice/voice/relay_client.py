"""The app's own voice client.

The desktop app connects to a match room as a regular participant, exactly like
every other player: it joins, sends audio while the user transmits (push-to-talk
or open mic), and plays audio it receives. This is what lets the app itself sit
in the channel and show a live "who is speaking" overlay.

It talks to the same UDP relay as everyone else, so it works whether the relay
runs embedded in this process or on another machine (``voice_relay_url``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time

from .audio import AudioEngine
from .protocol import (
    TEAM_DIRE,
    TEAM_RADIANT,
    Packet,
    PacketType,
    decode,
    encode,
)

logger = logging.getLogger(__name__)


class RelayClient(asyncio.DatagramProtocol):
    """UDP participant bound to one match room."""

    def __init__(
        self,
        room: str,
        player: str,
        *,
        relay_host: str = "127.0.0.1",
        relay_port: int = 12000,
        input_device: str | None = None,
        output_device: str | None = None,
        on_peer_speaking=None,
    ) -> None:
        self.room = room
        self.player = player
        self.team = TEAM_RADIANT
        self.relay_host = relay_host
        self.relay_port = relay_port
        self._on_peer_speaking = on_peer_speaking

        self._transport: asyncio.DatagramTransport | None = None
        self._seq = 0
        self._transmitting = False
        self._deafened = False
        self._audio = AudioEngine(
            self._send_frame,
            input_device=input_device,
            output_device=output_device,
        )
        self._ping_task: asyncio.Task | None = None
        self.peers: dict[str, float] = {}  # player -> last-heard monotonic

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: self,
            remote_addr=(self.relay_host, self.relay_port),
            family=socket.AF_INET,
        )
        self._transport = transport
        self._audio.start()
        self._send(PacketType.JOIN)
        self._ping_task = asyncio.create_task(self._ping_loop())
        logger.info("Голосовой клиент подключился к комнате %s как %s", self.room, self.player)

    async def stop(self) -> None:
        if self._ping_task is not None:
            self._ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ping_task
            self._ping_task = None
        if self._transport is not None:
            self._send(PacketType.LEAVE)
            self._transport.close()
            self._transport = None
        self._audio.stop()

    # ------------------------------------------------------------------
    # outgoing
    # ------------------------------------------------------------------

    def set_team(self, team: str) -> None:
        self.team = TEAM_RADIANT if team == "radiant" else TEAM_DIRE
        self._send(PacketType.JOIN)

    def set_transmitting(self, active: bool) -> None:
        if active == self._transmitting:
            return
        self._transmitting = active
        self._audio.set_transmitting(active)
        self._send(PacketType.SPEAKING, payload=b"\x01" if active else b"\x00")

    def set_deafened(self, deafened: bool) -> None:
        self._deafened = deafened

    def _send_frame(self, payload: bytes) -> None:
        self._seq += 1
        self._send(PacketType.AUDIO, payload=payload)

    def _send(self, ptype: PacketType, payload: bytes = b"") -> None:
        if self._transport is None:
            return
        packet = Packet(
            type=ptype,
            team=self.team,
            room=self.room,
            player=self.player,
            seq=self._seq,
            payload=payload,
        )
        with contextlib.suppress(Exception):
            self._transport.sendto(encode(packet))

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            self._send(PacketType.PING)

    # ------------------------------------------------------------------
    # incoming
    # ------------------------------------------------------------------

    def datagram_received(self, data: bytes, addr) -> None:
        packet = decode(data)
        if packet is None or packet.room != self.room:
            return
        if packet.type == PacketType.AUDIO:
            self.peers[packet.player] = time.monotonic()
            if not self._deafened:
                self._audio.play(packet.payload)
            self._notify(packet.player, True)
        elif packet.type == PacketType.SPEAKING:
            self._notify(packet.player, bool(packet.payload and packet.payload[0]))
        elif packet.type == PacketType.LEAVE:
            self.peers.pop(packet.player, None)
            self._notify(packet.player, False)

    def _notify(self, player: str, speaking: bool) -> None:
        if self._on_peer_speaking is not None:
            with contextlib.suppress(Exception):
                self._on_peer_speaking(player, speaking)

    def error_received(self, exc) -> None:  # pragma: no cover - transport error
        logger.debug("Ошибка голосового клиента: %s", exc)

    @property
    def audio_active(self) -> bool:
        return self._audio.ready
