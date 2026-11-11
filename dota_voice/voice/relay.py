"""UDP voice relay with team isolation and server-side mute.

Clients send small datagrams (see :mod:`dota_voice.voice.protocol`). The relay
keeps one *room* per match and forwards each audio packet only to peers that

* are in the same room, and
* are on the same team as the sender (Radiant cannot hear Dire), and
* are not muted by the server.

The relay is what makes "mute by button" authoritative: a muted player's audio
is dropped before it reaches anyone, no matter what their client does.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

from .protocol import (
    TEAM_RADIANT,
    Packet,
    PacketType,
    decode,
    encode,
)

logger = logging.getLogger(__name__)

PEER_TIMEOUT = 30.0  # seconds without a packet before a peer is dropped


@dataclass
class Peer:
    addr: tuple[str, int]
    room: str
    player: str
    team: int
    seq: int = 0
    last_seen: float = field(default_factory=time.monotonic)
    speaking: bool = False

    @property
    def key(self) -> tuple[str, int]:
        return self.addr


@dataclass
class Room:
    match_id: str
    peers: dict[tuple[str, int], Peer] = field(default_factory=dict)
    muted: set[str] = field(default_factory=set)
    deafened: set[str] = field(default_factory=set)
    teams: dict[str, int] = field(default_factory=dict)


class VoiceRelay:
    """Asyncio UDP server. One instance serves every match room."""

    def __init__(self, host: str = "0.0.0.0", port: int = 12000) -> None:
        self.host = host
        self.port = port
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _RelayProtocol | None = None
        self._rooms: dict[str, Room] = {}
        self._reaper: asyncio.Task | None = None
        self.bytes_forwarded = 0
        self.packets_dropped = 0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _RelayProtocol(self),
            local_addr=(self.host, self.port),
        )
        self._transport = transport
        self._protocol = protocol
        # Read back the real port so callers can use an ephemeral one (0).
        self.port = transport.get_extra_info("sockname")[1]
        self._reaper = asyncio.create_task(self._reap_loop())
        logger.info("Голосовой ретранслятор слушает %s:%s (UDP)", self.host, self.port)

    async def stop(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    # ------------------------------------------------------------------
    # rooms
    # ------------------------------------------------------------------

    def ensure_room(self, match_id: str) -> Room:
        room = self._rooms.get(match_id)
        if room is None:
            room = Room(match_id=match_id)
            self._rooms[match_id] = room
        return room

    def set_teams(self, match_id: str, teams: dict[str, int]) -> None:
        """Update the authoritative player -> team mapping for a room."""
        room = self.ensure_room(match_id)
        room.teams.update(teams)

    def set_muted(self, match_id: str, player: str, muted: bool) -> None:
        room = self.ensure_room(match_id)
        if muted:
            room.muted.add(player)
        else:
            room.muted.discard(player)

    def set_deafened(self, match_id: str, player: str, deafened: bool) -> None:
        room = self.ensure_room(match_id)
        if deafened:
            room.deafened.add(player)
        else:
            room.deafened.discard(player)

    def close_room(self, match_id: str) -> None:
        room = self._rooms.pop(match_id, None)
        if room and self._transport is not None:
            for peer in room.peers.values():
                with contextlib.suppress(Exception):
                    self._transport.sendto(
                        encode(Packet(PacketType.LEAVE, peer.team, match_id, peer.player)),
                        peer.addr,
                    )

    def room_snapshot(self, match_id: str) -> dict:
        room = self._rooms.get(match_id)
        if room is None:
            return {"match_id": match_id, "peers": [], "muted": [], "deafened": []}
        return {
            "match_id": match_id,
            "peers": [
                {
                    "player": peer.player,
                    "team": "radiant" if peer.team == TEAM_RADIANT else "dire",
                    "addr": f"{peer.addr[0]}:{peer.addr[1]}",
                    "speaking": peer.speaking,
                    "muted": peer.player in room.muted,
                }
                for peer in room.peers.values()
            ],
            "muted": sorted(room.muted),
            "deafened": sorted(room.deafened),
        }

    @property
    def room_count(self) -> int:
        return len(self._rooms)

    # ------------------------------------------------------------------
    # packet handling
    # ------------------------------------------------------------------

    def handle_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        packet = decode(data)
        if packet is None:
            self.packets_dropped += 1
            return
        room = self._rooms.get(packet.room)
        if room is None:
            # Rooms are created by the bridge from game state. Audio for a room
            # we do not know about is dropped rather than auto-creating one.
            if packet.type == PacketType.AUDIO:
                self.packets_dropped += 1
                return
            room = self.ensure_room(packet.room)
        peer = room.peers.get(addr)
        if peer is None:
            peer = Peer(addr=addr, room=packet.room, player=packet.player, team=packet.team)
            room.peers[addr] = peer
            logger.info(
                "Участник %s подключился к комнате %s (%s)",
                packet.player,
                packet.room,
                "Силы Света" if packet.team == TEAM_RADIANT else "Силы Тьмы",
            )
        # The relay's team table (fed by GSI) wins over what the client claims.
        peer.team = room.teams.get(packet.player, packet.team)
        peer.player = packet.player or peer.player
        peer.last_seen = time.monotonic()

        if packet.type == PacketType.AUDIO:
            peer.seq = packet.seq
            peer.speaking = True
            self._forward_audio(room, peer, packet.payload)
        elif packet.type == PacketType.SPEAKING:
            peer.speaking = bool(packet.payload and packet.payload[0])
        elif packet.type == PacketType.PING:
            self._send(addr, Packet(PacketType.PING, peer.team, room.match_id, peer.player))
        elif packet.type == PacketType.LEAVE:
            room.peers.pop(addr, None)

    def _forward_audio(self, room: Room, sender: Peer, payload: bytes) -> None:
        if sender.player in room.muted:
            self.packets_dropped += 1
            return
        if not payload or self._transport is None:
            return
        for peer in list(room.peers.values()):
            if peer.addr == sender.addr:
                continue
            if peer.team != sender.team:  # channel isolation
                continue
            if peer.player in room.deafened:
                continue
            self._send(
                peer.addr,
                Packet(
                    PacketType.AUDIO,
                    sender.team,
                    room.match_id,
                    sender.player,
                    seq=sender.seq,
                    payload=payload,
                ),
            )
            self.bytes_forwarded += len(payload)

    def _send(self, addr: tuple[str, int], packet: Packet) -> None:
        if self._transport is None:
            return
        try:
            self._transport.sendto(encode(packet), addr)
        except Exception as exc:  # pragma: no cover - transport failure
            logger.debug("Не удалось отправить пакет %s: %s", addr, exc)

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            for room in list(self._rooms.values()):
                for addr, peer in list(room.peers.items()):
                    if now - peer.last_seen > PEER_TIMEOUT:
                        room.peers.pop(addr, None)
                        logger.info("Участник %s отключился (таймаут)", peer.player)


class _RelayProtocol(asyncio.DatagramProtocol):
    def __init__(self, relay: VoiceRelay) -> None:
        self._relay = relay

    def datagram_received(self, data: bytes, addr) -> None:
        self._relay.handle_datagram(data, addr)

    def error_received(self, exc) -> None:  # pragma: no cover - transport error
        logger.debug("Ошибка UDP: %s", exc)
