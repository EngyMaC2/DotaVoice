"""Tests for automatic team assignment from peers' own clients.

A normal player's GSI reports only their own slot, so the game alone cannot tell
the server who is Radiant and who is Dire. Each player's app, however, announces
its own team through discovery and through the mesh handshake. These tests cover
the two halves of that mechanism:

* the mesh reports a peer's team exactly once per change, and
* the session folds that report into the roster, adding new teammates and
  correcting a team when a peer says otherwise.
"""

from __future__ import annotations

import asyncio

import pytest

from dota_voice.models import Player, Roster, Team
from dota_voice.net.discovery import PeerRecord
from dota_voice.net.mesh import VoiceMesh
from dota_voice.net.transport import UdpTransport


class _StubDiscovery:
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


# ---------------------------------------------------------------------------
# mesh level
# ---------------------------------------------------------------------------


async def test_mesh_reports_a_discovered_peer_team():
    reported: list[tuple[str, str]] = []
    mesh = VoiceMesh(
        "7123456789",
        local_name="alice",
        local_team="radiant",
        transport=UdpTransport(),
        discovery=_StubDiscovery("7123456789", "radiant"),
        on_peer_team=lambda name, team: reported.append((name, team)),
    )
    await mesh.start()
    try:
        mesh._on_discovered(
            PeerRecord(
                match_id="7123456789",
                name="bob",
                host="127.0.0.1",
                port=59999,
                team="dire",
            )
        )
        assert reported == [("bob", "dire")]
        # The same announcement again must not repeat the report.
        mesh._on_discovered(
            PeerRecord(
                match_id="7123456789",
                name="bob",
                host="127.0.0.1",
                port=59999,
                team="dire",
            )
        )
        assert reported == [("bob", "dire")]
    finally:
        await mesh.stop()


async def test_mesh_reports_a_team_change_once():
    reported: list[tuple[str, str]] = []
    mesh = VoiceMesh(
        "7123456789",
        local_name="alice",
        local_team="radiant",
        transport=UdpTransport(),
        discovery=_StubDiscovery("7123456789", "radiant"),
        on_peer_team=lambda name, team: reported.append((name, team)),
    )
    await mesh.start()
    try:
        mesh._on_discovered(
            PeerRecord(match_id="7123456789", name="bob", host="127.0.0.1", port=59998, team="dire")
        )
        mesh._on_discovered(
            PeerRecord(match_id="7123456789", name="bob", host="127.0.0.1", port=59998, team="radiant")
        )
        assert reported == [("bob", "dire"), ("bob", "radiant")]
    finally:
        await mesh.stop()


async def test_mesh_ignores_an_unknown_team():
    reported: list[tuple[str, str]] = []
    mesh = VoiceMesh(
        "7123456789",
        local_name="alice",
        local_team="radiant",
        transport=UdpTransport(),
        discovery=_StubDiscovery("7123456789", "radiant"),
        on_peer_team=lambda name, team: reported.append((name, team)),
    )
    await mesh.start()
    try:
        mesh._on_discovered(
            PeerRecord(match_id="7123456789", name="bob", host="127.0.0.1", port=59997, team="")
        )
        assert reported == []
    finally:
        await mesh.stop()


# ---------------------------------------------------------------------------
# session level
# ---------------------------------------------------------------------------


@pytest.fixture
async def runtime(settings, store, voice, bus):
    from dota_voice.agent.runtime import MatchSessionRuntime
    from dota_voice.models import MatchSession

    session = MatchSession(match_id="7123456789", conversation_id="c1")
    rt = MatchSessionRuntime(
        session, store=store, voice=voice, bus=bus, settings=settings,
        loop=asyncio.get_running_loop(),
    )
    return rt


async def test_a_peer_report_adds_a_missing_teammate(runtime):
    changed = await runtime.merge_peer_team("bob", Team.RADIANT)
    assert changed is True
    assert [p.player_id for p in runtime.session.roster.radiant] == ["bob"]
    # A second report for the same player and team is a no-op.
    assert await runtime.merge_peer_team("bob", Team.RADIANT) is False


async def test_a_peer_report_places_the_player_on_their_side(runtime):
    await runtime.merge_peer_team("carol", Team.DIRE)
    assert [p.player_id for p in runtime.session.roster.dire] == ["carol"]
    assert runtime.session.voice["carol"].team is Team.DIRE


async def test_a_peer_can_correct_our_team_assignment(runtime):
    """A peer's own client is the authority on which side it is on."""
    runtime.apply_roster(
        Roster(radiant=[Player(player_id="dave", name="dave", team=Team.RADIANT)])
    )
    changed = await runtime.merge_peer_team("dave", Team.DIRE)
    assert changed is True
    assert [p.player_id for p in runtime.session.roster.dire] == ["dave"]
    assert runtime.session.roster.radiant == []


async def test_an_unknown_peer_team_is_ignored(runtime):
    assert await runtime.merge_peer_team("eve", Team.UNKNOWN) is False
    assert runtime.session.roster.all_players() == []
