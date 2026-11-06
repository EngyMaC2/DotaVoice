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
