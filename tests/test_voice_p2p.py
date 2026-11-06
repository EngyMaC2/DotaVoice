"""Tests for the peer-to-peer voice backend.

Two backends are driven directly and joined over real loopback sockets, so the
whole path - discovery record, handshake, encryption, mute - is exercised.
"""

from __future__ import annotations

import asyncio

import pytest

from dota_voice.models import Player, Roster, Team
from dota_voice.net.discovery import PeerRecord
from dota_voice.voice import build_voice_backend
from dota_voice.voice.p2p_backend import P2PVoiceBackend
from dota_voice.config import Settings


def _roster(player_id: str, team: Team) -> Roster:
    return Roster(**{team.value: [Player(player_id=player_id, team=team, is_local=True)]})


class _StubDiscovery:
    """Offline stand-in for LanDiscovery.

    The real discovery uses multicast, which is not available (or not
    deterministic) in every test environment, so tests hand peers to each other
    explicitly through this stub. The handshake, encryption and mute paths are
    all still the real ones.
    """

    def __init__(self, match_id: str, team: str) -> None:
        self.match_id = match_id
        self.team = team
        self.port = 0
        self._callback = None

    def on_peer(self, callback) -> None:
        self._callback = callback

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _factory(match_id: str, team: str):
    return _StubDiscovery(match_id, team)


async def _joined(name: str, player_id: str, team: Team, match_id: str = "7123456789"):
    backend = P2PVoiceBackend(discovery_factory=_factory)
    backend.set_local_name(name)
    await backend.sync_roster(match_id, _roster(player_id, team))
    return backend


def _connect(backend_a, backend_b, match_id: str = "7123456789", team: str = "radiant"):
    session_a = backend_a.session_for(match_id)
    session_b = backend_b.session_for(match_id)
    session_a.mesh._on_discovered(
        PeerRecord(
            match_id=match_id,
            name=session_b.mesh.local_name,
            host="127.0.0.1",
            port=session_b.mesh.transport.local_address[1],
            team=team,
        )
    )


async def _wait_established(*backends, match_id: str = "7123456789"):
    """Wait until every backend has a link and has classified its peers.

    Establishment and the identity exchange are separate datagrams, so a test
    that only waits for ``established`` can still race the ``hello`` that tells a
    peer which team it is on.
    """
    for _ in range(60):
        ready = True
        for backend in backends:
            mesh = backend.session_for(match_id).mesh
            if not mesh.established_count:
                ready = False
                break
            if any(link.address not in mesh._peer_teams for link in mesh.links if link.is_ready()):
                ready = False
                break
        if ready:
            return True
        await asyncio.sleep(0.05)
    return False


async def test_teammates_establish_an_encrypted_link():
    a = await _joined("alice", "alice", Team.RADIANT)
    b = await _joined("bob", "bob", Team.RADIANT)
    _connect(a, b)
    try:
        assert await _wait_established(a, b)
    finally:
        await a.close()
        await b.close()


async def test_voice_reaches_a_teammate_and_reports_speaking():
    a = await _joined("alice", "alice", Team.RADIANT)
    b = await _joined("bob", "bob", Team.RADIANT)
    heard: list[tuple[str, str, bool]] = []
    b._on_peer_speaking = lambda match_id, name, speaking: heard.append(
        (match_id, name, speaking)
    )
    _connect(a, b)
    try:
        assert await _wait_established(a, b)
        session_a = a.session_for("7123456789")
        assert session_a.mesh.broadcast_voice(b"opus") == 1
        await asyncio.sleep(0.1)
        assert heard == [("7123456789", "alice", True)]
    finally:
        await a.close()
        await b.close()


async def test_no_link_forms_across_the_team_boundary():
    """A peer on the other side is never even linked, let alone sent audio."""
    a = await _joined("alice", "alice", Team.RADIANT)
    b = await _joined("bob", "bob", Team.DIRE)
    heard: list[str] = []
    b._on_peer_speaking = lambda match_id, name, speaking: heard.append(name)
    _connect(a, b, team="dire")
    try:
        await asyncio.sleep(0.3)
        mesh_a = a.session_for("7123456789").mesh
        mesh_b = b.session_for("7123456789").mesh
        assert mesh_a.established_count == 0
        assert mesh_b.established_count == 0
        assert mesh_a.broadcast_voice(b"opus") == 0
        await asyncio.sleep(0.1)
        assert heard == []
    finally:
        await a.close()
        await b.close()


async def test_local_mute_stops_playback_one_sidedly():
    a = await _joined("alice", "alice", Team.RADIANT)
    b = await _joined("bob", "bob", Team.RADIANT)
    heard: list[str] = []
    b._on_peer_speaking = lambda match_id, name, speaking: heard.append(name)
    _connect(a, b)
    try:
        assert await _wait_established(a, b)
        await b.set_mute("7123456789", "alice", True)
        a.session_for("7123456789").mesh.broadcast_voice(b"opus")
        await asyncio.sleep(0.1)
        assert heard == []
        # Only bob muted alice; alice is unaffected.
        assert b.session_for("7123456789").mesh.link_for("alice").muted is True
    finally:
        await a.close()
        await b.close()


async def test_ensure_channels_are_logical_and_stable():
    a = await _joined("alice", "alice", Team.RADIANT)
    try:
        radiant, dire = await a.ensure_channels("7123456789")
        assert radiant == "7123456789-radiant"
        assert dire == "7123456789-dire"
        assert a.joined_rooms == ["7123456789"]
    finally:
        await a.close()
    assert a.joined_rooms == []


async def test_snapshot_describes_the_mesh():
    a = await _joined("alice", "alice", Team.RADIANT)
    b = await _joined("bob", "bob", Team.RADIANT)
    _connect(a, b)
    try:
        assert await _wait_established(a, b)
        snapshot = a.snapshot()
        assert snapshot["backend"] == "p2p"
        peers = snapshot["matches"]["7123456789"]["peers"]
        assert peers and peers[0]["state"] == "established"
        assert peers[0]["fingerprint"]
    finally:
        await a.close()
        await b.close()


async def test_transmitting_and_deafen_are_local_controls():
    a = await _joined("alice", "alice", Team.RADIANT)
    try:
        assert a.set_transmitting(True) == ["7123456789"]
        assert a.set_deafened(True) == ["7123456789"]
    finally:
        await a.close()


def test_factory_builds_the_p2p_backend():
    settings = Settings(voice="p2p", store="memory")
    backend = build_voice_backend(settings)
    assert isinstance(backend, P2PVoiceBackend)
    assert backend._discovery_factory is None


def test_sdr_is_off_by_default():
    settings = Settings()
    assert settings.p2p_steam_sdr is False
    settings.guard_safety()  # must not raise


def test_enabling_sdr_without_support_is_refused():
    """SDR stays off unless the operator opts in; the flag alone is not enough."""
    from dota_voice.net.transport import SdrUnavailable, SteamSdrTransport

    with pytest.raises(SdrUnavailable):
        SteamSdrTransport(480, enabled=True)
