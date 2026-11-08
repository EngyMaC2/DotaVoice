"""End-to-end tests for the two-channel team isolation and automatic placement.

The concept the app has to deliver is narrow and concrete: two people each
launch the app, start a match, and the app *automatically* connects each of them
to the channel of their own team - and never lets anyone cross to the other
side. These tests drive the real structure (the real orchestrator, the real
session manager, real OpenHands conversations and tools, and the real encrypted
peer mesh) rather than a stand-in, so they fail if the structure only works in
theory.

They cover:

* automatic placement from the game's own roster (spectator/authoritative);
* automatic placement from a player's own client (non-spectator GSI);
* automatic placement completed by peers' own clients;
* the real mesh refusing to link or carry audio across the team boundary;
* no duplicate channels across a restart;
* the whole HTTP + WebSocket bridge working end to end.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from dota_voice.agent import Orchestrator, SessionManager
from dota_voice.bus import EventBus
from dota_voice.config import Settings
from dota_voice.gsi import parse_gsi
from dota_voice.models import Player, Roster, Team
from dota_voice.net.discovery import PeerRecord
from dota_voice.server import create_app
from dota_voice.store import MemoryStore
from dota_voice.voice import build_voice_backend
from dota_voice.voice.memory import MemoryVoiceBackend

from scripts.simulate import payload


# ---------------------------------------------------------------------------
# one player's app, wired for real
# ---------------------------------------------------------------------------


class _PlayerApp:
    """One player's running bridge: real agent layer over a voice backend."""

    def __init__(self, voice, settings, store=None, bus=None) -> None:
        self.voice = voice
        self.bus = bus or EventBus()
        self.manager = SessionManager(
            store=store or MemoryStore(),
            voice=voice,
            bus=self.bus,
            settings=settings,
        )
        self.manager.bind_loop(asyncio.get_running_loop())
        self.orchestrator = Orchestrator(self.manager)

    async def gsi(self, body: dict) -> dict:
        return await self.orchestrator.handle(parse_gsi(body))

    async def close(self) -> None:
        await self.manager.close()


def _non_spectator_payload(
    match_id: str, account_id: str, name: str, team: str, token: str | None = None
) -> dict:
    """A normal player's GSI: it carries only the local slot, no allplayers."""
    body = {
        "provider": {"name": "Dota 2", "appid": 570, "version": 40},
        "map": {
            "matchid": match_id,
            "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
            "game_time": 60,
        },
        "player": {
            "steamid": f"7656119800000{account_id}",
            "accountid": account_id,
            "name": name,
            "team_name": team,
        },
        "hero": {"name": "npc_dota_hero_lina", "level": 1},
    }
    if token:
        body["auth"] = {"token": token}
    return body


@pytest.fixture
def app_settings(tmp_path):
    return Settings(
        store="memory",
        voice="memory",
        llm_api_key=None,
        workspace_dir=tmp_path,
        session_grace_seconds=900,
    )


# ---------------------------------------------------------------------------
# automatic placement
# ---------------------------------------------------------------------------


async def test_two_players_are_each_placed_on_their_own_side(app_settings):
    """Two apps, one match: each player lands in the channel of their team.

    This is the core promise - nobody sorts anyone by hand. Each app reads its
    own side from its own client and joins the matching channel.
    """
    match_id = "7123456789"
    alice = _PlayerApp(MemoryVoiceBackend(), app_settings)
    bob = _PlayerApp(MemoryVoiceBackend(), app_settings)
    try:
        await alice.gsi(_non_spectator_payload(match_id, "101", "Alice", "radiant"))
        await bob.gsi(_non_spectator_payload(match_id, "201", "Bob", "dire"))

        a_runtime = alice.manager.get_runtime(match_id)
        b_runtime = bob.manager.get_runtime(match_id)
        assert a_runtime is not None and b_runtime is not None

        # Alice is on Radiant, in the Radiant channel, and nowhere else.
        assert a_runtime.session.radiant_channel_id
        assert a_runtime.session.dire_channel_id
        assert alice.voice.channel_of(match_id, "101") == a_runtime.session.radiant_channel_id
        assert alice.voice.members(match_id, Team.RADIANT) == {"101"}
        assert alice.voice.members(match_id, Team.DIRE) == set()

        # Bob is on Dire, in the Dire channel, and nowhere else.
        assert bob.voice.channel_of(match_id, "201") == b_runtime.session.dire_channel_id
        assert bob.voice.members(match_id, Team.DIRE) == {"201"}
        assert bob.voice.members(match_id, Team.RADIANT) == set()
    finally:
        await alice.close()
        await bob.close()


async def test_the_spectator_roster_places_all_ten_players_automatically(app_settings):
    """A spectator (or the game's authoritative view) sorts everyone at once."""
    match_id = "555666777"
    app = _PlayerApp(MemoryVoiceBackend(), app_settings)
    try:
        await app.gsi(payload(match_id, "DOTA_GAMERULES_STATE_PRE_GAME"))
        runtime = app.manager.get_runtime(match_id)
        radiant_ids = {p.player_id for p in runtime.session.roster.radiant}
        dire_ids = {p.player_id for p in runtime.session.roster.dire}
        assert radiant_ids == {"101", "102", "103"}
        assert dire_ids == {"201", "202", "203"}
        # Every player sits in exactly the channel of their team.
        for pid in radiant_ids:
            assert app.voice.channel_of(match_id, pid) == runtime.session.radiant_channel_id
        for pid in dire_ids:
            assert app.voice.channel_of(match_id, pid) == runtime.session.dire_channel_id
        # The two channels share no members: strict isolation.
        assert app.voice.members(match_id, Team.RADIANT).isdisjoint(
            app.voice.members(match_id, Team.DIRE)
        )
    finally:
        await app.close()


async def test_a_teammate_is_placed_automatically_when_their_app_appears(app_settings):
    """A non-spectator sees only itself; teammates are placed as they connect."""
    match_id = "123123123"
    app = _PlayerApp(MemoryVoiceBackend(), app_settings)
    try:
        await app.gsi(_non_spectator_payload(match_id, "101", "Alice", "radiant"))
        runtime = app.manager.get_runtime(match_id)
        assert app.voice.members(match_id, Team.RADIANT) == {"101"}

        # A teammate's own client announces its side.
        assert await runtime.merge_peer_team("Frost", Team.RADIANT) is True
        assert await runtime.merge_peer_team("Bane", Team.DIRE) is True
        await runtime.sync_roster()

        assert app.voice.members(match_id, Team.RADIANT) == {"101", "Frost"}
        assert app.voice.members(match_id, Team.DIRE) == {"Bane"}
        # Alice did not move: she is still on Radiant.
        assert app.voice.channel_of(match_id, "101") == runtime.session.radiant_channel_id
    finally:
        await app.close()


async def test_a_peer_announcement_cannot_cross_the_boundary(app_settings):
    """Once the game placed a player, a peer may not drag them to the other side.

    This is what keeps the two channels isolated when players run their own
    clients and can report whatever they like.
    """
    match_id = "456456456"
    app = _PlayerApp(MemoryVoiceBackend(), app_settings)
    try:
        await app.gsi(payload(match_id))  # spectator view: authoritative
        runtime = app.manager.get_runtime(match_id)
        assert "101" in app.voice.members(match_id, Team.RADIANT)

        # A peer claiming 101 is on Dire must be ignored: the game said Radiant.
        assert await runtime.merge_peer_team("101", Team.DIRE) is False
        assert app.voice.members(match_id, Team.RADIANT) == {"101", "102", "103"}
        assert "101" not in app.voice.members(match_id, Team.DIRE)
    finally:
        await app.close()


# ---------------------------------------------------------------------------
# the real mesh: no link, no audio, across the boundary
# ---------------------------------------------------------------------------


class _StubDiscovery:
    """Offline stand-in for multicast discovery (see tests/test_voice_p2p.py)."""

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


async def _mesh_backend(name: str, player_id: str, team: Team, match_id: str):
    backend = build_voice_backend(
        Settings(voice="p2p", llm_api_key=None), on_peer_team=None
    )
    # Replace discovery with the deterministic stub for the test.
    backend._discovery_factory = lambda m, t: _StubDiscovery(m, t)
    backend.set_local_name(name)
    await backend.sync_roster(
        match_id,
        Roster(**{team.value: [Player(player_id=player_id, team=team, is_local=True)]}),
    )
    return backend


def _announce(from_backend, to_backend, match_id: str, team: str) -> None:
    """Feed ``to_backend`` a discovery record for ``from_backend``."""
    session = from_backend.session_for(match_id)
    to_backend.session_for(match_id).mesh._on_discovered(
        PeerRecord(
            match_id=match_id,
            name=session.mesh.local_name,
            host="127.0.0.1",
            port=session.mesh.transport.local_address[1],
            team=team,
        )
    )


async def _settle(*backends, match_id: str) -> None:
    for _ in range(60):
        ready = all(
            not b.session_for(match_id).mesh.links
            or all(
                link.address in b.session_for(match_id).mesh._peer_teams
                for link in b.session_for(match_id).mesh.links
                if link.is_ready()
            )
            for b in backends
        )
        if ready:
            await asyncio.sleep(0.1)
            return
        await asyncio.sleep(0.05)


async def test_the_real_mesh_links_teammates_and_isolates_the_other_team():
    """Teammates link and exchange audio; the other side does neither.

    Three players: two on Radiant, one on Dire. The two Radiant players must
    connect and hear each other; the Dire player must never even open a link,
    and no audio may reach them.
    """
    match_id = "999000111"
    alice = await _mesh_backend("alice", "alice", Team.RADIANT, match_id)
    bob = await _mesh_backend("bob", "bob", Team.RADIANT, match_id)
    carol = await _mesh_backend("carol", "carol", Team.DIRE, match_id)

    heard: list[str] = []
    carol._on_peer_speaking = lambda m, name, speaking: heard.append(name)
    bob._on_peer_speaking = lambda m, name, speaking: heard.append(name)

    try:
        # Alice learns about both peers from discovery. Bob (Radiant) is a
        # teammate so a link forms; Carol (Dire) is filtered out immediately.
        _announce(bob, alice, match_id, "radiant")
        _announce(carol, alice, match_id, "dire")
        await _settle(alice, bob, carol, match_id=match_id)

        mesh_alice = alice.session_for(match_id).mesh
        mesh_bob = bob.session_for(match_id).mesh
        mesh_carol = carol.session_for(match_id).mesh

        # Teammates are linked and can hear each other.
        assert mesh_alice.established_count == 1
        assert mesh_bob.established_count == 1
        assert mesh_alice.broadcast_voice(b"opus") == 1
        await asyncio.sleep(0.1)
        assert "alice" in heard

        # The other side is never linked and receives nothing.
        assert mesh_carol.established_count == 0
        assert mesh_carol.links == []
        heard.clear()
        assert mesh_alice.broadcast_voice(b"opus") == 1  # still only the teammate
        await asyncio.sleep(0.1)
        assert heard == ["alice"]  # carol heard nothing
    finally:
        await alice.close()
        await bob.close()
        await carol.close()


# ---------------------------------------------------------------------------
# survivability: a restart never duplicates channels
# ---------------------------------------------------------------------------


async def test_a_restart_reuses_the_same_channels(app_settings):
    """The same match id always maps to the same two channels.

    Restarting the game (a repeat ``match_started``) must re-attach, not build a
    second room, and must not move anyone between sides.
    """
    match_id = "321321321"
    app = _PlayerApp(MemoryVoiceBackend(), app_settings)
    try:
        await app.gsi(payload(match_id))
        runtime = app.manager.get_runtime(match_id)
        radiant_channel = runtime.session.radiant_channel_id
        dire_channel = runtime.session.dire_channel_id

        for _ in range(3):
            await app.gsi(payload(match_id, clock=120))
            same = app.manager.get_runtime(match_id)
            assert same is runtime
            assert same.session.radiant_channel_id == radiant_channel
            assert same.session.dire_channel_id == dire_channel

        assert len(app.manager.snapshots()) == 1
        assert app.voice.members(match_id, Team.RADIANT) == {"101", "102", "103"}
        assert app.voice.members(match_id, Team.DIRE) == {"201", "202", "203"}
    finally:
        await app.close()


# ---------------------------------------------------------------------------
# the whole bridge, over HTTP and WebSocket
# ---------------------------------------------------------------------------


def test_the_whole_bridge_works_end_to_end(tmp_path):
    """GSI -> session -> overlay socket -> mute, over the real HTTP surface.

    This is the "does the structure actually run" check: a real app is created,
    a match is reported, the overlay socket sees the team split, push-to-talk
    mutes a player, and the mute shows up in the next snapshot.
    """
    settings = Settings(
        store="memory",
        voice="memory",
        llm_api_key=None,
        workspace_dir=tmp_path,
        gsi_token="tok",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        # 1. The game reports a match; the app builds the two channels.
        body = payload("777888999", token="tok")
        response = client.post("/gsi", json=body)
        assert response.status_code == 200
        assert response.json()["is_new"] is True

        # 2. The overlay socket is primed with the team split.
        with client.websocket_connect("/ws/overlay") as ws:
            message = json.loads(ws.receive_text())
            assert message["type"] == "state"
            assert message["match_id"] == "777888999"
            assert {p["player_id"] for p in message["payload"]["radiant"]} == {
                "101",
                "102",
                "103",
            }
            assert {p["player_id"] for p in message["payload"]["dire"]} == {
                "201",
                "202",
                "203",
            }

        # 3. Push-to-talk mutes a teammate server-side, and it sticks.
        muted = client.post(
            "/api/sessions/777888999/mute", json={"player_id": "101", "muted": True}
        )
        assert muted.status_code == 200
        session = client.get("/api/sessions").json()["sessions"][0]
        row = next(p for p in session["radiant"] if p["player_id"] == "101")
        assert row["muted"] is True

        # 4. A repeat of the same match re-attaches and stays one session.
        assert client.post("/gsi", json=body).json()["is_new"] is False
        assert len(client.get("/api/sessions").json()["sessions"]) == 1
