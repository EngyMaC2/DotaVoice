"""Tests for the Discord voice backend.

Discord is an external HTTP API, so these tests drive the real backend code
through an ``httpx.MockTransport``. That stubs only the network boundary - all
of our logic (channel reuse, member moves, mute flags, URL/body shapes) runs
for real. This is the one place a stub is justified; everything else in the
suite exercises real code paths.
"""

from __future__ import annotations

import json

import httpx
import pytest

from dota_voice.models import Player, Roster, Team
from dota_voice.voice.discord import DiscordVoiceBackend


class FakeDiscord:
    """Minimal in-memory Discord: channels + member state."""

    def __init__(self) -> None:
        self.channels: dict[str, dict] = {}
        self.members: dict[str, dict] = {}
        self.requests: list[tuple[str, str, dict]] = []
        self._next_id = 1000

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = {}
        if request.content:
            body = json.loads(request.content.decode())
        path = request.url.path
        self.requests.append((request.method, path, body))

        if request.method == "GET" and path == "/api/v10/guilds/42/channels":
            return httpx.Response(200, json=list(self.channels.values()))

        if request.method == "POST" and path == "/api/v10/guilds/42/channels":
            self._next_id += 1
            cid = str(self._next_id)
            self.channels[cid] = {"id": cid, "name": body["name"], "type": body["type"]}
            return httpx.Response(201, json=self.channels[cid])

        if request.method == "PATCH" and "/members/" in path:
            user_id = path.rsplit("/", 1)[-1]
            state = self.members.setdefault(user_id, {})
            state.update(body)
            return httpx.Response(200, json={"user": {"id": user_id}, **state})

        if request.method == "DELETE" and path.startswith("/api/v10/channels/"):
            self.channels.pop(path.rsplit("/", 1)[-1], None)
            return httpx.Response(204)

        return httpx.Response(404, json={"message": "not found", "path": path})


@pytest.fixture
def fake() -> FakeDiscord:
    return FakeDiscord()


@pytest.fixture
def backend(fake) -> DiscordVoiceBackend:
    client = httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(fake.handler),
    )
    return DiscordVoiceBackend(
        bot_token="test-token",
        guild_id="42",
        user_map={"101": "9001", "102": "9002", "201": "9003"},
        client=client,
    )


def _roster() -> Roster:
    return Roster(
        radiant=[
            Player(player_id="101", name="A", team=Team.RADIANT),
            Player(player_id="102", name="B", team=Team.RADIANT),
        ],
        dire=[Player(player_id="201", name="C", team=Team.DIRE)],
    )


async def test_creates_two_channels(backend, fake):
    radiant, dire = await backend.ensure_channels("m1")
    assert radiant != dire
    names = {c["name"] for c in fake.channels.values()}
    assert any("Силы Света" in n and "m1" in n for n in names)
    assert any("Силы Тьмы" in n and "m1" in n for n in names)


async def test_ensure_channels_reuses_existing_by_name(backend, fake):
    # Pre-create a channel with the exact expected name (simulates a restart).
    fake.channels["777"] = {"id": "777", "name": "Силы Света · m1", "type": 2}
    radiant, _ = await backend.ensure_channels("m1")
    assert radiant == "777"
    # Only the missing Dire channel was created.
    created = [r for r in fake.requests if r[0] == "POST"]
    assert len(created) == 1


async def test_sync_roster_moves_members(backend, fake):
    await backend.sync_roster("m1", _roster())
    assert fake.members["9001"]["channel_id"] != fake.members["9003"]["channel_id"]
    assert fake.members["9001"]["channel_id"] == fake.members["9002"]["channel_id"]


async def test_set_mute_sends_flag(backend, fake):
    await backend.sync_roster("m1", _roster())
    await backend.set_mute("m1", "101", True)
    assert fake.members["9001"]["mute"] is True
    await backend.set_mute("m1", "101", False)
    assert fake.members["9001"]["mute"] is False


async def test_set_deafen_sends_flag(backend, fake):
    await backend.sync_roster("m1", _roster())
    await backend.set_deafen("m1", "201", True)
    assert fake.members["9003"]["deaf"] is True


async def test_unknown_player_mapping_is_skipped(backend, fake):
    roster = Roster(radiant=[Player(player_id="999", name="Nobody", team=Team.RADIANT)])
    await backend.sync_roster("m1", roster)
    assert "999" not in {p for p in fake.members}


async def test_delete_channels_removes_them(backend, fake):
    await backend.ensure_channels("m1")
    assert fake.channels
    await backend.delete_channels("m1")
    assert not fake.channels


async def test_requires_credentials():
    with pytest.raises(ValueError):
        DiscordVoiceBackend(bot_token="", guild_id="42")
