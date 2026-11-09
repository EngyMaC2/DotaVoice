"""Tests for the handshake, discovery and the encrypted mesh.

The mesh tests run over real UDP sockets on the loopback interface, so they
exercise the actual datagram path rather than a mock transport.
"""

from __future__ import annotations

import asyncio

import pytest

from dota_voice.net.crypto import AuthenticationError
from dota_voice.net.discovery import (
    LanDiscovery,
    PeerRecord,
    decode_announce,
    encode_announce,
    match_fingerprint,
)
from dota_voice.net.handshake import (
    CONFIRM_SIZE,
    HELLO_SIZE,
    InitiatorHandshake,
    ResponderHandshake,
    fingerprint,
    is_handshake_packet,
    match_hash,
)
from dota_voice.net.link import PeerLink
from dota_voice.net.mesh import VoiceMesh
from dota_voice.net.transport import SdrUnavailable, SteamSdrTransport, UdpTransport

MATCH = "7123456789"


# ---------------------------------------------------------------------------
# handshake
# ---------------------------------------------------------------------------


def _pair(match_id: str = MATCH):
    initiator = InitiatorHandshake(match_id)
    responder = ResponderHandshake(match_id)
    confirm, responder_result = responder.accept(initiator.build_hello())
    initiator_result = initiator.finish(confirm)
    return initiator_result, responder_result


def test_handshake_derives_matching_keys():
    a, b = _pair()
    counter, ciphertext = a.cipher.encrypt(b"audio")
    assert b.cipher.decrypt(counter, ciphertext) == b"audio"
    counter, ciphertext = b.cipher.encrypt(b"reply")
    assert a.cipher.decrypt(counter, ciphertext) == b"reply"


def test_handshake_fingerprints_identify_the_peer():
    a, b = _pair()
    # Each side reports the *other* side's ephemeral key, so the two fingerprints
    # differ, are formatted consistently, and match the key they describe.
    assert a.fingerprint == fingerprint(a.peer_public)
    assert b.fingerprint == fingerprint(b.peer_public)
    assert a.fingerprint != b.fingerprint


def test_handshake_packet_sizes_are_stable():
    initiator = InitiatorHandshake(MATCH)
    hello = initiator.build_hello()
    assert len(hello) == HELLO_SIZE
    assert is_handshake_packet(hello)
    confirm, _ = ResponderHandshake(MATCH).accept(hello)
    assert len(confirm) == CONFIRM_SIZE


def test_handshake_rejects_a_different_match():
    initiator = InitiatorHandshake("111")
    with pytest.raises(AuthenticationError):
        ResponderHandshake("222").accept(initiator.build_hello())


def test_handshake_rejects_a_tampered_hello():
    hello = bytearray(InitiatorHandshake(MATCH).build_hello())
    hello[12] ^= 0xFF
    with pytest.raises(AuthenticationError):
        ResponderHandshake(MATCH).accept(bytes(hello))


def test_handshake_rejects_a_forged_confirm():
    initiator = InitiatorHandshake(MATCH)
    confirm, _ = ResponderHandshake(MATCH).accept(initiator.build_hello())
    # A fresh initiator has a different nonce, so the old confirm must not fit.
    with pytest.raises(AuthenticationError):
        InitiatorHandshake(MATCH).finish(confirm)


def test_replayed_hello_yields_a_fresh_session():
    """Replaying a hello must not reproduce an earlier session's keys."""
    initiator = InitiatorHandshake(MATCH)
    hello = initiator.build_hello()

    confirm_one, result_one = ResponderHandshake(MATCH).accept(hello)
    initiator_one = initiator.finish(confirm_one)

    confirm_two, result_two = ResponderHandshake(MATCH).accept(hello)
    initiator_two = initiator.finish(confirm_two)

    counter, ciphertext = initiator_one.cipher.encrypt(b"secret")
    with pytest.raises(AuthenticationError):
        result_two.cipher.decrypt(counter, ciphertext)
    assert initiator_two.cipher is not None


def test_match_hash_hides_the_match_id():
    digest = match_hash(MATCH)
    assert len(digest) == 8
    assert MATCH.encode() not in digest
    assert match_fingerprint(MATCH) != MATCH


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def test_announce_roundtrip_includes_team():
    record = PeerRecord(match_id=MATCH, name="Алиса", host="0.0.0.0", port=45678, team="dire")
    decoded = decode_announce(encode_announce(record))
    assert decoded == (match_hash(MATCH), "dire", 45678, "Алиса")


def test_announce_rejects_foreign_magic():
    data = encode_announce(PeerRecord(match_id=MATCH, name="x", host="h", port=1))
    assert decode_announce(b"XXXX" + data[4:]) is None
    assert decode_announce(b"short") is None


def test_discovery_ignores_other_matches():
    discovery = LanDiscovery(MATCH)
    seen: list[PeerRecord] = []
    discovery.on_peer(seen.append)
    other = PeerRecord(match_id="999", name="bob", host="0.0.0.0", port=1234)
    discovery.handle_datagram(encode_announce(other), ("10.0.0.9", 1234))
    assert seen == []
    same = PeerRecord(match_id=MATCH, name="bob", host="0.0.0.0", port=1234)
    discovery.handle_datagram(encode_announce(same), ("10.0.0.9", 1234))
    assert [p.name for p in seen] == ["bob"]


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------


async def test_udp_transport_moves_datagrams():
    received: list[tuple[bytes, tuple[str, int]]] = []
    left = UdpTransport(host="127.0.0.1", port=0)
    right = UdpTransport(host="127.0.0.1", port=0)
    await left.start()
    await right.start()
    right.on_datagram(lambda data, addr: received.append((data, addr)))
    left.send(b"ping", right.local_address)
    await asyncio.sleep(0.05)
    await left.stop()
    await right.stop()
    assert received and received[0][0] == b"ping"


def test_steam_sdr_requires_an_explicit_opt_in():
    with pytest.raises(SdrUnavailable):
        SteamSdrTransport(480)


# ---------------------------------------------------------------------------
# mesh over real sockets
# ---------------------------------------------------------------------------


class _StubDiscovery:
    """Stands in for LanDiscovery so a test can hand a peer an address."""

    def __init__(self) -> None:
        self.port = 0
        self.team = "unknown"
        self._callback = None

    def on_peer(self, callback) -> None:
        self._callback = callback

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def feed(self, record: PeerRecord) -> None:
        if self._callback is not None:
            self._callback(record)


async def _mesh_pair(team_a="radiant", team_b="radiant"):
    disc_a, disc_b = _StubDiscovery(), _StubDiscovery()
    a = VoiceMesh(
        MATCH, local_name="alice", local_team=team_a,
        transport=UdpTransport(host="127.0.0.1", port=0), discovery=disc_a,
    )
    b = VoiceMesh(
        MATCH, local_name="bob", local_team=team_b,
        transport=UdpTransport(host="127.0.0.1", port=0), discovery=disc_b,
    )
    await a.start()
    await b.start()
    disc_a.feed(
        PeerRecord(match_id=MATCH, name="bob", host="127.0.0.1",
                   port=b.transport.local_address[1], team=team_b)
    )
    for _ in range(40):
        if a.established_count and b.established_count:
            break
        await asyncio.sleep(0.05)
    return a, b


async def test_mesh_establishes_an_encrypted_link():
    a, b = await _mesh_pair()
    try:
        assert a.established_count == 1
        assert b.established_count == 1
        # The responder learned the initiator's name from the encrypted hello.
        assert b.links[0].peer_name == "alice"
    finally:
        await a.stop()
        await b.stop()


async def test_mesh_delivers_voice_to_a_teammate():
    a, b = await _mesh_pair()
    heard: list[str] = []
    b._on_peer_speaking = lambda name, speaking: heard.append(name)
    try:
        assert a.broadcast_voice(b"frame") == 1
        await asyncio.sleep(0.1)
        assert heard == ["alice"]
    finally:
        await a.stop()
        await b.stop()


async def test_mesh_does_not_carry_voice_across_teams():
    a, b = await _mesh_pair(team_a="radiant", team_b="dire")
    heard: list[str] = []
    b._on_peer_speaking = lambda name, speaking: heard.append(name)
    try:
        assert a.broadcast_voice(b"frame") == 0
        await asyncio.sleep(0.1)
        assert heard == []
    finally:
        await a.stop()
        await b.stop()


async def test_no_link_forms_across_the_team_boundary_during_the_match():
    """Opposing players must not even be linked while the match runs."""
    a, b = await _mesh_pair(team_a="radiant", team_b="dire")
    try:
        assert a.established_count == 0
        assert b.established_count == 0
    finally:
        await a.stop()
        await b.stop()


async def test_team_chat_reaches_only_teammates():
    a, b = await _mesh_pair(team_a="radiant", team_b="radiant")
    seen: list[dict] = []
    b._on_chat = lambda name, message: seen.append(message)
    try:
        assert a.send_chat("radiant", "push top", message_id="m1") == 1
        await asyncio.sleep(0.1)
        assert [m["text"] for m in seen] == ["push top"]
    finally:
        await a.stop()
        await b.stop()


async def test_cross_team_chat_opens_only_after_the_match():
    """Text crosses the boundary only once the post-match window is dialled."""
    a, b = await _mesh_pair(team_a="radiant", team_b="dire")
    seen: list[dict] = []
    b._on_chat = lambda name, message: seen.append(message)
    try:
        # No link yet, so nothing crosses.
        assert a.send_chat("post_game", "gg") == 0
        await asyncio.sleep(0.1)
        assert seen == []

        # Dial the other side; now the shared channel can reach them.
        assert a.dial_cross_team() == 1
        for _ in range(40):
            if a.established_count and b.established_count:
                break
            await asyncio.sleep(0.05)
        assert a.send_chat("post_game", "gg", message_id="p1") == 1
        await asyncio.sleep(0.1)
        assert [m["text"] for m in seen] == ["gg"]

        # Voice still does not cross, even with the link up.
        heard: list[str] = []
        b._on_peer_speaking = lambda name, speaking: heard.append(name)
        assert a.broadcast_voice(b"frame") == 0
        await asyncio.sleep(0.1)
        assert heard == []
    finally:
        await a.stop()
        await b.stop()


async def test_a_peer_cannot_forge_a_team_channel_message():
    """A Dire peer cannot slip text into the Radiant room by lying about side."""
    a, b = await _mesh_pair(team_a="radiant", team_b="dire")
    seen: list[dict] = []
    a._on_chat = lambda name, message: seen.append(message)
    try:
        a.dial_cross_team()
        for _ in range(40):
            if a.established_count and b.established_count:
                break
            await asyncio.sleep(0.05)
        # a is Radiant and knows its peer is Dire. A message claiming the
        # "radiant" channel must be dropped: the side comes from the link, not
        # from the message.
        link = a.links[0]
        a._handle_chat(
            link, {"channel": "radiant", "text": "fake", "id": "f1", "ts": 0.0}
        )
        assert seen == []
        # The shared channel, however, is accepted from the other side.
        a._handle_chat(
            link, {"channel": "post_game", "text": "gg", "id": "p1", "ts": 0.0}
        )
        assert [m["text"] for m in seen] == ["gg"]
    finally:
        await a.stop()
        await b.stop()


async def test_local_mute_is_one_sided():
    a, b = await _mesh_pair()
    heard: list[str] = []
    b._on_peer_speaking = lambda name, speaking: heard.append(name)
    try:
        assert b.set_peer_muted("alice", True) is True
        a.broadcast_voice(b"frame")
        await asyncio.sleep(0.1)
        assert heard == []
        assert b.set_peer_muted("alice", False) is True
        a.broadcast_voice(b"frame")
        await asyncio.sleep(0.1)
        assert heard == ["alice"]
    finally:
        await a.stop()
        await b.stop()


async def test_link_rejects_replayed_voice_frames():
    a, b = await _mesh_pair()
    try:
        link = a.links[0]
        counter, ciphertext = link.cipher.encrypt(b"frame")
        from dota_voice.net.link import FRAME_MAGIC, _FRAME_HEADER

        datagram = _FRAME_HEADER.pack(FRAME_MAGIC, counter) + ciphertext
        peer_link = b.links[0]
        played: list[bytes] = []
        peer_link._on_voice = lambda link, frame: played.append(frame)
        peer_link.handle_datagram(datagram)
        assert played == [b"frame"]
        peer_link.handle_datagram(datagram)
        assert played == [b"frame"]  # the replay was dropped
    finally:
        await a.stop()
        await b.stop()


async def test_unknown_sender_that_fails_handshake_is_dropped():
    a = VoiceMesh(
        MATCH, local_name="alice", local_team="radiant",
        transport=UdpTransport(host="127.0.0.1", port=0), discovery=_StubDiscovery(),
    )
    await a.start()
    try:
        intruder = PeerLink(
            "999", "eve", a.transport.local_address, a.transport, initiator=True
        )
        hello = intruder.start_handshake()
        a._on_datagram(hello, ("127.0.0.1", 5555))
        await asyncio.sleep(0.05)
        assert a._links.get(("127.0.0.1", 5555)) is None
    finally:
        await a.stop()


async def test_handshake_timeout_abandons_a_stuck_link():
    mesh = VoiceMesh(
        MATCH, local_name="alice", local_team="radiant",
        transport=UdpTransport(host="127.0.0.1", port=0), discovery=_StubDiscovery(),
    )
    await mesh.start()
    try:
        link = PeerLink(MATCH, "ghost", ("127.0.0.1", 1), mesh.transport, initiator=True)
        link.start_handshake()
        mesh._links[link.address] = link
        # Pretend the handshake started long ago.
        link.created_at -= 100
        await mesh._sweep_once()
        assert link.address not in mesh._links
    finally:
        await mesh.stop()


async def test_a_peer_saying_bye_is_reported_as_gone():
    """When a teammate's app shuts down, the mesh must report them as left."""
    left: list[str] = []
    a, b = await _mesh_pair()
    a._on_peer_left = lambda name: left.append(name)
    try:
        # bob closes the link, which sends an encrypted "bye" to alice.
        b.links[0].close()
        for _ in range(40):
            if left:
                break
            await asyncio.sleep(0.05)
        assert left == ["bob"]
        # The link is dropped, so a later broadcast goes nowhere.
        assert a.established_count == 0
    finally:
        await a.stop()
        await b.stop()
