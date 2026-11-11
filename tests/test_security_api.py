"""Tests for the security HTTP endpoints."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from dota_voice.config import Settings
from dota_voice.security.updater import generate_keypair, sign_manifest
from dota_voice.server import create_app


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


def test_security_status_lists_invariants_and_policy(client):
    resp = client.get("/api/security")
    assert resp.status_code == 200
    body = resp.json()
    assert body["policy"]["read_game_memory"] is False
    assert body["policy"]["local_mute"] is True
    assert "no_memory_read" in body["invariants"]
    assert "no_credential_request" in body["invariants"]
    assert "autostart" in body


def test_security_audit_accepts_a_clean_payload(client):
    resp = client.post("/api/security/audit", json={"port": 3000, "display_name": "Игрок"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "Проверка безопасности" in body["text"]


def test_security_audit_flags_a_credential_payload(client):
    resp = client.post("/api/security/audit", json={"steam_password": "hunter2"})
    body = resp.json()
    assert body["ok"] is False
    assert "steam_password" in body["violations"][0]


def test_autostart_can_be_toggled(client, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    resp = client.post("/api/security/autostart", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    assert client.get("/api/security/autostart").json()["enabled"] is True
    resp = client.post("/api/security/autostart", json={"enabled": False})
    assert resp.json()["enabled"] is False
    assert client.get("/api/security/autostart").json()["enabled"] is False


def test_update_check_accepts_a_signed_manifest(client):
    private, public = generate_keypair()
    manifest = {
        "manifest_version": 1,
        "version": "2.0.0",
        "min_supported_version": "0.1.0",
        "rollout_percent": 100,
        "artifacts": [],
    }
    manifest["signature"] = sign_manifest(manifest, private)
    resp = client.post(
        "/api/security/update/check",
        json={"manifest": json.dumps(manifest), "public_key": public, "current_version": "0.1.0"},
    )
    assert resp.status_code == 200
    assert resp.json()["available"] is True
    assert resp.json()["version"] == "2.0.0"


def test_update_check_rejects_a_tampered_manifest(client):
    private, public = generate_keypair()
    manifest = {"manifest_version": 1, "version": "2.0.0", "artifacts": []}
    manifest["signature"] = sign_manifest(manifest, private)
    manifest["version"] = "9.9.9"
    resp = client.post(
        "/api/security/update/check",
        json={"manifest": json.dumps(manifest), "public_key": public},
    )
    assert resp.status_code == 400
    assert "подпис" in resp.json()["detail"]


def test_update_check_requires_a_manifest(client):
    resp = client.post("/api/security/update/check", json={})
    assert resp.status_code == 400


def test_update_check_requires_a_public_key(client):
    resp = client.post("/api/security/update/check", json={"manifest": "{}"})
    assert resp.status_code == 400
