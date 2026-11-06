"""Tests for the built-in voice relay.

These exercise the real UDP path: two sockets talk to a live relay to verify
team isolation, server-side mute, deafen and room snapshots. Audio frames are
tiny byte blobs - no sound device is needed.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from dota_voice.voice.protocol import (
    Packet,
    PacketType,
    decode,
    encode,
    TEAM_DIRE,
    TEAM_RADIANT,
)
from dota_voice.voice.relay import VoiceRelay


class Client:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.setblocking(False)

    def send(self, port: int, packet: Packet) -> None:
        self.sock.sendto(encode(packet), ("127.0.0.1", port))

    def recv(self) -> Packet | None:
        try:
            data, _ = self.sock.recvfrom(4096)
        except BlockingIOError:
            return None
        return decode(data)

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
async def relay():
    relay = VoiceRelay(host="127.0.0.1", port=0)
    await relay.start()
    try:
        yield relay
    finally:
        await relay.stop()


async def _settle() -> None:
    await asyncio.sleep(0.15)


def test_protocol_roundtrip():
    packet = Packet(
        type=PacketType.AUDIO, team=TEAM_RADIANT, room="m1", player="p1",
        seq=7, payload=b"hello",
    )
    decoded = decode(encode(packet))
    assert decoded is not None
    assert decoded.type is PacketType.AUDIO
    assert decoded.room == "m1"
    assert decoded.player == "p1"
    assert decoded.payload == b"hello"


def test_protocol_rejects_garbage():
    assert decode(b"") is None
    assert decode(b"\xff\xff") is None
    assert decode(b"{not json}") is None


async def test_audio_isolated_between_teams(relay: VoiceRelay):
    radiant, dire = Client(), Client()
    relay.set_teams("m1", {"r": TEAM_RADIANT, "d": TEAM_DIRE})
    radiant.send(relay.port, Packet(PacketType.JOIN, TEAM_RADIANT, "m1", "r"))
    dire.send(relay.port, Packet(PacketType.JOIN, TEAM_DIRE, "m1", "d"))
    await _settle()

    radiant.send(relay.port, Packet(PacketType.AUDIO, TEAM_RADIANT, "m1", "r", payload=b"x"))
    await _settle()
    assert dire.recv() is None  # opposite team hears nothing

    radiant.close()
    dire.close()


async def test_teammate_receives_audio(relay: VoiceRelay):
    a, b = Client(), Client()
    relay.set_teams("m1", {"a": TEAM_RADIANT, "b": TEAM_RADIANT})
    a.send(relay.port, Packet(PacketType.JOIN, TEAM_RADIANT, "m1", "a"))
    b.send(relay.port, Packet(PacketType.JOIN, TEAM_RADIANT, "m1", "b"))
    await _settle()

    a.send(relay.port, Packet(PacketType.AUDIO, TEAM_RADIANT, "m1", "a", payload=b"hi"))
    await _settle()
    packet = b.recv()
    assert packet is not None
    assert packet.player == "a"
    assert packet.payload == b"hi"

    a.close()
    b.close()


async def test_server_side_mute_drops_audio(relay: VoiceRelay):
    a, b = Client(), Client()
    relay.set_teams("m1", {"a": TEAM_RADIANT, "b": TEAM_RADIANT})
    a.send(relay.port, Packet(PacketType.JOIN, TEAM_RADIANT, "m1", "a"))
    b.send(relay.port, Packet(PacketType.JOIN, TEAM_RADIANT, "m1", "b"))
    await _settle()

    relay.set_muted("m1", "a", True)
    a.send(relay.port, Packet(PacketType.AUDIO, TEAM_RADIANT, "m1", "a", payload=b"x"))
    await _settle()
    assert b.recv() is None

    relay.set_muted("m1", "a", False)
    a.send(relay.port, Packet(PacketType.AUDIO, TEAM_RADIANT, "m1", "a", payload=b"y"))
    await _settle()
    packet = b.recv()
    assert packet is not None and packet.payload == b"y"

    a.close()
    b.close()


async def test_deafened_speaker_gets_no_audio(relay: VoiceRelay):
    a, b = Client(), Client()
    relay.set_teams("m1", {"a": TEAM_RADIANT, "b": TEAM_RADIANT})
    a.send(relay.port, Packet(PacketType.JOIN, TEAM_RADIANT, "m1", "a"))
    b.send(relay.port, Packet(PacketType.JOIN, TEAM_RADIANT, "m1", "b"))
    await _settle()

    relay.set_deafened("m1", "a", True)
    b.send(relay.port, Packet(PacketType.AUDIO, TEAM_RADIANT, "m1", "b", payload=b"z"))
    await _settle()
    assert a.recv() is None

    a.close()
    b.close()


async def test_room_snapshot_and_lifecycle(relay: VoiceRelay):
    a = Client()
    relay.set_teams("m1", {"a": TEAM_RADIANT})
    a.send(relay.port, Packet(PacketType.JOIN, TEAM_RADIANT, "m1", "a"))
    await _settle()

    snapshot = relay.room_snapshot("m1")
    assert snapshot["match_id"] == "m1"
    assert [peer["player"] for peer in snapshot["peers"]] == ["a"]
    assert relay.room_count == 1

    relay.close_room("m1")
    assert relay.room_count == 0
    assert relay.room_snapshot("m1")["peers"] == []

    a.close()


async def test_unknown_room_audio_is_dropped(relay: VoiceRelay):
    a = Client()
    # No room / team registered: nothing should be forwarded or crash.
    a.send(relay.port, Packet(PacketType.AUDIO, TEAM_RADIANT, "ghost", "a", payload=b"x"))
    await _settle()
    assert relay.room_count == 0
    assert relay.packets_dropped >= 1
    a.close()


async def test_leave_removes_peer(relay: VoiceRelay):
    a = Client()
    relay.set_teams("m1", {"a": TEAM_RADIANT})
    a.send(relay.port, Packet(PacketType.JOIN, TEAM_RADIANT, "m1", "a"))
    await _settle()
    assert relay.room_snapshot("m1")["peers"]
    a.send(relay.port, Packet(PacketType.LEAVE, TEAM_RADIANT, "m1", "a"))
    await _settle()
    assert relay.room_snapshot("m1")["peers"] == []
    a.close()
