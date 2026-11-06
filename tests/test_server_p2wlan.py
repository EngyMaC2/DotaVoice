"""Tests for the microphone and P2WLAN HTTP endpoints.

These drive the real FastAPI app with the P2WLAN voice backend, so the request
paths that the desktop UI and the overlay use are the ones under test.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dota_voice.config import Settings
from dota_voice.server import create_app

from scripts.simulate import payload


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Keep the microphone state and the room map inside the test's tmp dir.
    monkeypatch.setenv("DOTAVOICE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DOTAVOICE_DATA_DIR", str(tmp_path / "data"))
    settings = Settings(
        store="memory",
        voice="p2p",
        p2wlan_driver="local",
        p2wlan_state_path=tmp_path / "p2wlan.json",
        llm_api_key=None,
        workspace_dir=tmp_path,
        gsi_token="test-token",
        mic_mode="continuous",
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _gsi(client, match_id="7123456789", **kwargs):
    return client.post("/gsi", json=payload(match_id, token="test-token", **kwargs))


# ----------------------------------------------------------------------
# microphone
# ----------------------------------------------------------------------


def test_mic_status_reports_the_mode(client):
    body = client.get("/api/voice/mic").json()
    assert body["mode"] == "continuous"
    assert body["mode_label"]
    assert body["active"] is True


def test_setting_the_mic_mode_persists_it(client):
    resp = client.post("/api/voice/mic", json={"mode": "push_to_talk"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "push_to_talk"
    # Entering push-to-talk closes the microphone until the key is held.
    assert body["active"] is False
    assert client.get("/api/voice/mic").json()["mode"] == "push_to_talk"


def test_cycling_the_mic_mode_walks_through_all_three(client):
    assert client.post("/api/voice/mic/cycle").json()["mode"] == "push_to_talk"
    assert client.post("/api/voice/mic/cycle").json()["mode"] == "toggle"
    assert client.post("/api/voice/mic/cycle").json()["mode"] == "continuous"


def test_mic_key_endpoint_drives_push_to_talk(client):
    client.post("/api/voice/mic", json={"mode": "push_to_talk"})
    assert client.post("/api/voice/mic/key", json={"action": "down"}).json()["active"] is True
    assert client.post("/api/voice/mic/key", json={"action": "up"}).json()["active"] is False


def test_a_conflicting_hotkey_produces_a_warning(client):
    body = client.post("/api/voice/mic", json={"push_to_talk_key": "q"}).json()
    assert body["warnings"]
    assert "Dota 2" in body["warnings"][0]
    assert body["push_to_talk_key"] == "q"


def test_a_free_hotkey_produces_no_warning(client):
    body = client.post("/api/voice/mic", json={"toggle_key": "n"}).json()
    assert body["warnings"] == []


def test_healthz_exposes_the_mic_and_sdr_state(client):
    body = client.get("/healthz").json()
    assert body["p2wlan_enabled"] is True
    assert body["steam_sdr"] is False  # off by default
    assert body["mic"]["mode"] == "continuous"


# ----------------------------------------------------------------------
# P2WLAN
# ----------------------------------------------------------------------


def test_p2wlan_status_is_enabled_with_the_p2p_backend(client):
    body = client.get("/api/p2wlan").json()
    assert body["enabled"] is True
    assert body["driver"] == "local"


def test_gsi_creates_a_room_with_a_shareable_invite(client):
    _gsi(client)
    rooms = client.get("/api/p2wlan").json()["rooms"]
    assert "7123456789" in rooms
    room = rooms["7123456789"]
    assert room["invite_key"].startswith("p2w-")
    assert room["subnet"].startswith("10.42.")


def test_the_invite_endpoint_returns_and_copies_the_key(client, monkeypatch):
    monkeypatch.setattr("dota_voice.server._copy_clipboard", lambda text: True)
    _gsi(client)
    body = client.post("/api/p2wlan/invite", json={"match_id": "7123456789"}).json()
    assert body["ok"] is True
    assert body["invite_key"].startswith("p2w-")
    assert body["copied"] is True
    assert "скопировано" in body["message"]


def test_the_invite_endpoint_works_without_a_clipboard(client, monkeypatch):
    monkeypatch.setattr("dota_voice.server._copy_clipboard", lambda text: False)
    _gsi(client)
    body = client.post("/api/p2wlan/invite", json={"match_id": "7123456789"}).json()
    assert body["copied"] is False
    assert body["invite_key"] in body["message"]


def test_the_invite_endpoint_requires_a_match_id(client):
    resp = client.post("/api/p2wlan/invite", json={})
    assert resp.status_code == 400


def test_joining_by_invite_puts_a_teammate_on_the_room(client):
    _gsi(client)
    invite = client.post(
        "/api/p2wlan/invite", json={"match_id": "7123456789"}
    ).json()["invite_key"]
    body = client.post(
        "/api/p2wlan/join", json={"invite_key": invite, "match_id": "7123456789"}
    ).json()
    assert body["ok"] is True
    assert body["subnet"].startswith("10.42.")


def test_joining_with_a_bad_invite_is_rejected(client):
    resp = client.post("/api/p2wlan/join", json={"invite_key": "p2w-nope"})
    assert resp.status_code == 400


def test_adding_a_member_gives_them_a_virtual_address(client):
    _gsi(client)
    body = client.post(
        "/api/p2wlan/members",
        json={"match_id": "7123456789", "member": "bob", "team": "radiant"},
    ).json()
    assert body["ok"] is True
    assert body["virtual_ip"].startswith("10.42.")


def test_adding_a_member_to_an_unknown_match_is_a_404(client):
    resp = client.post(
        "/api/p2wlan/members", json={"match_id": "nope", "member": "bob"}
    )
    assert resp.status_code == 404


def test_the_session_snapshot_carries_the_invite_key(client):
    """The desktop UI reads the invite from the session list, not the room list."""
    _gsi(client)
    session = client.get("/api/sessions").json()["sessions"][0]
    invite = client.post(
        "/api/p2wlan/invite", json={"match_id": "7123456789"}
    ).json()["invite_key"]
    assert session["invite_key"] == invite


def test_a_restart_reuses_the_room_instead_of_duplicating_it(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTAVOICE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DOTAVOICE_DATA_DIR", str(tmp_path / "data"))
    state = tmp_path / "p2wlan.json"
    settings = Settings(
        store="memory",
        voice="p2p",
        p2wlan_driver="local",
        p2wlan_state_path=state,
        llm_api_key=None,
        workspace_dir=tmp_path,
        gsi_token="test-token",
    )

    with TestClient(create_app(settings)) as first:
        _gsi(first)
        invite = first.get("/api/p2wlan").json()["rooms"]["7123456789"]["invite_key"]

    # A second app instance over the same state file is the restart.
    with TestClient(create_app(settings)) as second:
        _gsi(second)
        rooms = second.get("/api/p2wlan").json()["rooms"]
        assert len(rooms) == 1
        assert rooms["7123456789"]["invite_key"] == invite
        # The restart must actually rejoin the network, not just remember it.
        assert rooms["7123456789"]["joined"] is True
        metrics = second.get("/api/p2wlan").json()["metrics"]
        assert metrics["rooms_created"] == 0
        assert metrics["rooms_reused"] == 1


def test_ending_a_match_drops_the_room(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTAVOICE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DOTAVOICE_DATA_DIR", str(tmp_path / "data"))
    state = tmp_path / "p2wlan.json"
    settings = Settings(
        store="memory",
        voice="p2p",
        p2wlan_driver="local",
        p2wlan_state_path=state,
        llm_api_key=None,
        workspace_dir=tmp_path,
        gsi_token="test-token",
    )

    with TestClient(create_app(settings)) as client:
        _gsi(client)
        assert "7123456789" in client.get("/api/p2wlan").json()["rooms"]
        client.post(
            "/gsi",
            json=payload("7123456789", token="test-token",
                         game_state="DOTA_GAMERULES_STATE_POST_GAME"),
        )
        assert client.get("/api/p2wlan").json()["rooms"] == {}
