"""Tests for the HTTP surface: GSI ingest, manual controls, overlay socket."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from dota_voice.config import Settings
from dota_voice.server import create_app

from scripts.simulate import payload


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        store="memory",
        voice="memory",
        llm_api_key=None,
        workspace_dir=tmp_path,
        gsi_token="test-token",
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _gsi(client, match_id="7123456789", **kwargs):
    return client.post("/gsi", json=payload(match_id, token="test-token", **kwargs))


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["voice_backend"] == "memory"


def test_gsi_requires_valid_token(client):
    bad = client.post("/gsi", json=payload("1", token="wrong"))
    assert bad.status_code == 403
    good = _gsi(client)
    assert good.status_code == 200
    assert good.json()["action"] == "handled"


def test_gsi_creates_session_and_splits_teams(client):
    resp = _gsi(client)
    assert resp.json()["is_new"] is True

    sessions = client.get("/api/sessions").json()["sessions"]
    assert len(sessions) == 1
    session = sessions[0]
    assert session["match_id"] == "7123456789"
    assert len(session["radiant"]) == 3
    assert len(session["dire"]) == 3
    assert session["radiant_channel_id"]
    assert session["dire_channel_id"]


def test_repeated_gsi_does_not_create_duplicates(client):
    _gsi(client)
    for _ in range(3):
        assert _gsi(client).json()["is_new"] is False
    sessions = client.get("/api/sessions").json()["sessions"]
    assert len(sessions) == 1


def test_manual_mute_endpoint(client):
    _gsi(client)
    resp = client.post("/api/sessions/7123456789/mute", json={"player_id": "101", "muted": True})
    assert resp.status_code == 200
    snap = resp.json()
    muted = [p for p in snap["radiant"] if p["player_id"] == "101"][0]
    assert muted["muted"] is True


def test_manual_move_endpoint(client):
    _gsi(client)
    resp = client.post(
        "/api/sessions/7123456789/move", json={"player_id": "101", "team": "dire"}
    )
    assert resp.status_code == 200
    snap = resp.json()
    assert any(p["player_id"] == "101" for p in snap["dire"])
    assert all(p["player_id"] != "101" for p in snap["radiant"])


def test_manual_move_rejects_bad_team(client):
    _gsi(client)
    resp = client.post(
        "/api/sessions/7123456789/move", json={"player_id": "101", "team": "neutral"}
    )
    assert resp.status_code == 400


def test_end_session_endpoint(client):
    _gsi(client)
    assert client.post("/api/sessions/7123456789/end").status_code == 200
    assert client.get("/api/sessions").json()["sessions"] == []


def test_unknown_session_returns_404(client):
    assert client.post("/api/sessions/nope/mute", json={"player_id": "1"}).status_code == 404


def test_overlay_page_is_transparent_and_pointer_transparent(client):
    html = client.get("/overlay").text
    assert "background: transparent" in html
    assert "pointer-events: none" in html


def test_control_page_loads(client):
    html = client.get("/").text
    assert "Command Voice Bridge" in html


def test_overlay_websocket_streams_state(client):
    _gsi(client)
    with client.websocket_connect("/ws/overlay") as ws:
        # On connect the socket is primed with current state.
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "state"
        assert msg["match_id"] == "7123456789"
        assert len(msg["payload"]["radiant"]) == 3


def test_overlay_page_has_a_drag_handle_and_mic_indicator(client):
    html = client.get("/overlay").text
    # The drag handle and the mic chip are the only interactive elements; both
    # are marked so the desktop window knows where to accept clicks.
    assert 'id="handle"' in html
    assert 'data-interactive' in html
    assert 'id="mic"' in html
    assert "overlayBridge" in html


def test_overlay_page_speaks_simple_russian(client):
    html = client.get("/overlay").text
    assert "Силы Света" in html
    assert "Силы Тьмы" in html
    assert "Ожидание активной катки" in html


def test_mic_state_is_broadcast_to_the_overlay(client):
    with client.websocket_connect("/ws/overlay") as ws:
        client.post("/api/voice/mic", json={"mode": "push_to_talk"})
        # The mode change is announced so the indicator follows along.
        for _ in range(5):
            msg = json.loads(ws.receive_text())
            if msg["type"] == "mic_state":
                assert msg["payload"]["mode"] == "push_to_talk"
                return
    raise AssertionError("overlay never received a mic_state event")


def test_control_page_shows_the_mic_and_invite_controls(client):
    html = client.get("/").text
    assert 'id="mic"' in html
    assert 'id="invite"' not in html  # the pill is created per session instead
    assert "Скопировать" in html
    assert "выбор героев" in html  # every phase has a Russian label


# ----------------------------------------------------------------------
# automatic team assignment from peers
# ----------------------------------------------------------------------


def test_peer_team_report_creates_and_places_a_teammate(client):
    """A normal player's GSI has no allplayers; teammates arrive from peers."""
    # A non-spectator payload: only the local player is known.
    body = {
        "provider": {"name": "Dota 2", "appid": 570},
        "map": {"matchid": "9988776655", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
        "player": {
            "steamid": "76561198000000101",
            "accountid": "101",
            "name": "Aegis",
            "team_name": "radiant",
        },
        "auth": {"token": "test-token"},
    }
    client.post("/gsi", json=body)

    sessions = client.get("/api/sessions").json()["sessions"]
    match = next(s for s in sessions if s["match_id"] == "9988776655")
    assert len(match["radiant"]) == 1  # only the local player

    # Now the engine learns a teammate's side from that teammate's own client.
    client.app.state.service._on_peer_team("9988776655", "bob", "dire")

    # The callback hops onto the loop; give it a moment, then re-read.
    import time

    for _ in range(50):
        sessions = client.get("/api/sessions").json()["sessions"]
        match = next(s for s in sessions if s["match_id"] == "9988776655")
        if any(p["player_id"] == "bob" for p in match["dire"]):
            break
        time.sleep(0.02)
    assert any(p["player_id"] == "bob" for p in match["dire"])


# ----------------------------------------------------------------------
# self-mute
# ----------------------------------------------------------------------


def test_mic_self_mute_toggles_and_is_reported(client):
    start = client.get("/api/voice/mic").json()
    assert start["self_muted"] is False
    muted = client.post("/api/voice/mic/mute", json={"toggle": True}).json()
    assert muted["self_muted"] is True
    assert muted["active"] is False
    back = client.post("/api/voice/mic/mute", json={"toggle": True}).json()
    assert back["self_muted"] is False
    assert back["active"] is True


def test_mic_self_mute_can_be_set_explicitly(client):
    resp = client.post("/api/voice/mic", json={"self_muted": True}).json()
    assert resp["self_muted"] is True
    resp = client.post("/api/voice/mic", json={"self_muted": False}).json()
    assert resp["self_muted"] is False
