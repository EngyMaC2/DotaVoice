"""Wire format for the built-in voice relay.

Packets are small binary datagrams (the transport is UDP). The layout is:

    magic(4) type(1) team(1) room_len(1) player_len(1) seq(4) room player payload

``magic`` guards against stray traffic; ``type`` selects the message; ``team``
carries the sender's side so the relay can enforce channel isolation without a
separate lookup. Audio payloads are raw 16-bit mono PCM (see :mod:`audio`).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

MAGIC = b"DV01"
_HEADER = struct.Struct("!4sBBBB I")

TEAM_RADIANT = 0
TEAM_DIRE = 1

MAX_DATAGRAM = 1400  # stay well under a typical MTU
MAX_NAME = 64


class PacketType(IntEnum):
    JOIN = 1
    AUDIO = 2
    LEAVE = 3
    PING = 4
    MUTE = 5
    DEAFEN = 6
    SPEAKING = 7


@dataclass
class Packet:
    type: PacketType
    team: int
    room: str
    player: str
    seq: int = 0
    payload: bytes = b""


def encode(packet: Packet) -> bytes:
    room = packet.room.encode("utf-8")[:MAX_NAME]
    player = packet.player.encode("utf-8")[:MAX_NAME]
    header = _HEADER.pack(
        MAGIC,
        int(packet.type),
        packet.team & 0xFF,
        len(room),
        len(player),
        packet.seq & 0xFFFFFFFF,
    )
    return header + room + player + packet.payload


def decode(data: bytes) -> Packet | None:
    """Decode a datagram, or return ``None`` if it is not a valid packet."""
    size = _HEADER.size
    if len(data) < size:
        return None
    magic, ptype, team, room_len, player_len, seq = _HEADER.unpack(data[:size])
    if magic != MAGIC:
        return None
    start = size
    end_room = start + room_len
    end_player = end_room + player_len
    if end_player > len(data):
        return None
    try:
        packet_type = PacketType(ptype)
    except ValueError:
        return None
    return Packet(
        type=packet_type,
        team=team,
        room=data[start:end_room].decode("utf-8", errors="ignore"),
        player=data[end_room:end_player].decode("utf-8", errors="ignore"),
        seq=seq,
        payload=data[end_player:],
    )
