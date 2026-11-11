"""Tests for the embedded engine thread.

These start the real uvicorn server on an ephemeral port and talk to it over
HTTP, which is exactly how the desktop UI uses it. No Qt and no display needed.
"""

from __future__ import annotations

from dota_voice.appconfig import AppConfig
from dota_voice.desktop.api import EngineAPI
from dota_voice.desktop.engine import EngineThread


def _settings(token: str = ""):
    return AppConfig(host="127.0.0.1", port=0, voice_backend="relay", gsi_token=token).to_settings()


def test_engine_starts_and_reports_health():
    engine = EngineThread(_settings())
    port = engine.start()
    api = EngineAPI(f"http://127.0.0.1:{port}")
    try:
        assert port > 0
        health = api.health()
        assert health["status"] == "ok"
        assert health["voice_backend"] == "relay"
        assert health["sessions"] == 0
        assert "relay" in health
    finally:
        api.close()
        engine.stop()


def test_engine_voice_endpoint_lists_audio_state():
    engine = EngineThread(_settings())
    port = engine.start()
    api = EngineAPI(f"http://127.0.0.1:{port}")
    try:
        voice = api.voice()
        assert voice["backend"] == "relay"
        assert "audio_available" in voice
        assert voice["joined_rooms"] == []
    finally:
        api.close()
        engine.stop()


def test_engine_accepts_gsi_and_creates_session():
    engine = EngineThread(_settings(token="tok"))
    port = engine.start()
    api = EngineAPI(f"http://127.0.0.1:{port}")
    try:
        payload = {
            "auth": {"token": "tok"},
            "map": {"matchid": "4242", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
            "player": {"team_name": "radiant", "name": "Игрок", "accountid": "1"},
            "allplayers": {
                "0": {"team_name": "radiant", "name": "Игрок", "accountid": "1"},
                "5": {"team_name": "dire", "name": "Враг", "accountid": "6"},
            },
        }
        import httpx

        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            response = client.post("/gsi", json=payload)
            assert response.status_code == 200
            assert response.json()["match_id"] == "4242"

        sessions = api.sessions()
        assert len(sessions) == 1
        session = sessions[0]
        assert session["match_id"] == "4242"
        assert [p["name"] for p in session["radiant"]] == ["Игрок"]
        assert [p["name"] for p in session["dire"]] == ["Враг"]
    finally:
        api.close()
        engine.stop()


def test_engine_binds_distinct_ephemeral_ports():
    first = EngineThread(_settings())
    second = EngineThread(_settings())
    p1 = first.start()
    p2 = second.start()
    try:
        assert p1 != p2
    finally:
        first.stop()
        second.stop()


def test_engine_starts_without_std_streams():
    """A packaged ``--windowed`` build has ``sys.stdout``/``sys.stderr`` = None.

    uvicorn's default log formatter calls ``sys.stdout.isatty()``, which used to
    kill the engine thread before it bound the socket. The engine must configure
    its own logging and start regardless.
    """
    import sys

    engine = EngineThread(_settings())
    saved_out, saved_err = sys.stdout, sys.stderr
    sys.stdout = None
    sys.stderr = None
    try:
        port = engine.start(timeout=30.0)
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
    try:
        assert port > 0
        api = EngineAPI(f"http://127.0.0.1:{port}")
        try:
            assert api.health()["status"] == "ok"
        finally:
            api.close()
    finally:
        engine.stop()


def test_engine_reports_real_failure_instead_of_timeout(monkeypatch):
    """A dead engine thread must surface its cause, not a bare timeout."""
    import dota_voice.desktop.engine as engine_module

    def explode(_settings):
        raise RuntimeError("подставная ошибка сборки")

    monkeypatch.setattr(engine_module, "create_app", explode)

    engine = EngineThread(_settings())
    import pytest

    with pytest.raises(RuntimeError) as info:
        engine.start(timeout=10.0)
    assert "подставная ошибка сборки" in str(info.value)


def test_voice_transmit_and_deafen_endpoints():
    engine = EngineThread(_settings())
    port = engine.start()
    try:
        import httpx

        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            transmit = client.post("/api/voice/transmit", json={"active": True})
            assert transmit.status_code == 200
            assert transmit.json()["transmitting"] is True

            deafen = client.post("/api/voice/deafen", json={"deafened": True})
            assert deafen.status_code == 200
            assert deafen.json()["deafened"] is True
    finally:
        engine.stop()
