"""Tests for the security layer: policy, signed updates and autostart."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from dota_voice.appconfig import AppConfig
from dota_voice.security import autostart, policy
from dota_voice.security.policy import (
    Invariant,
    PolicyViolation,
    SafetyPolicy,
    audit_config,
    default_policy,
    forbidden_modules,
    report_text,
)
from dota_voice.security.updater import (
    Artifact,
    Manifest,
    UpdateError,
    UpdatePolicy,
    generate_keypair,
    is_newer,
    load_manifest,
    parse_version,
    should_offer,
    sign_manifest,
    verify_artifact,
    verify_manifest,
)


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


def test_default_policy_guards_clean():
    default_policy().guard()  # must not raise


@pytest.mark.parametrize(
    "flag",
    ["read_game_memory", "inject_into_game", "automate_gameplay",
     "request_steam_credentials", "internal_overlay"],
)
def test_unsafe_flags_are_refused(flag):
    candidate = SafetyPolicy(**{flag: True})
    with pytest.raises(PolicyViolation):
        candidate.guard()


def test_local_mute_is_required():
    with pytest.raises(PolicyViolation) as excinfo:
        SafetyPolicy(local_mute=False).guard()
    assert excinfo.value.invariant is Invariant.LOCAL_MUTE


def test_sdr_requires_an_explicit_decision():
    with pytest.raises(PolicyViolation):
        SafetyPolicy().guard_sdr()
    SafetyPolicy(steam_sdr_enabled=True).guard_sdr()


def test_credential_like_field_names_are_recognised():
    for name in ("steam_password", "steam_login", "api_key", "db_secret", "password"):
        assert policy.looks_like_credential_field(name)
    # The GSI token is ours, generated locally, and is not a Steam credential.
    assert not policy.looks_like_credential_field("gsi_token")
    assert not policy.looks_like_credential_field("port")


def test_audit_of_a_normal_config_is_clean():
    report = audit_config(AppConfig())
    assert report.ok
    assert report.violations == []
    assert report.invariants


def test_audit_catches_a_stored_credential():
    config = AppConfig()
    config.__dict__["steam_password"] = "hunter2"
    report = audit_config(config)
    assert not report.ok
    assert "steam_password" in report.violations[0]


def test_audit_flags_sdr_and_explains_the_risk():
    config = AppConfig()
    config.p2p_steam_sdr = True
    candidate = SafetyPolicy(steam_sdr_enabled=True)
    report = audit_config(config, candidate)
    assert any("Steam SDR" in note for note in report.notes)


def test_report_text_is_russian_and_lists_invariants():
    text = report_text(audit_config(AppConfig()))
    assert "Проверка безопасности" in text
    assert "Гарантии" in text
    assert "память Dota 2" in text


def test_forbidden_modules_flags_memory_apis():
    assert forbidden_modules(["pymem", "os", "frida.core"]) == ["pymem", "frida.core"]
    assert forbidden_modules(["os", "asyncio"]) == []


# ---------------------------------------------------------------------------
# updater
# ---------------------------------------------------------------------------


def _signed_manifest(version="1.2.0", **overrides):
    private, public = generate_keypair()
    manifest = {
        "manifest_version": 1,
        "version": version,
        "min_supported_version": overrides.pop("min_supported_version", "1.0.0"),
        "rollout_percent": overrides.pop("rollout_percent", 100),
        "artifacts": [
            {
                "url": "https://example.invalid/DotaVoice.msi",
                "sha256": "ab" * 32,
                "size": 4,
                "platform": "any",
            }
        ],
    }
    manifest.update(overrides)
    manifest["signature"] = sign_manifest(manifest, private)
    return manifest, public


def test_a_signed_manifest_verifies():
    manifest, public = _signed_manifest()
    assert verify_manifest(manifest, public) is manifest


def test_an_unsigned_manifest_is_rejected():
    manifest, public = _signed_manifest()
    manifest.pop("signature")
    with pytest.raises(UpdateError):
        verify_manifest(manifest, public)


def test_a_tampered_manifest_is_rejected():
    manifest, public = _signed_manifest()
    manifest["version"] = "9.9.9"
    with pytest.raises(UpdateError):
        verify_manifest(manifest, public)


def test_a_manifest_signed_by_another_key_is_rejected():
    manifest, _ = _signed_manifest()
    _, other_public = generate_keypair()
    with pytest.raises(UpdateError):
        verify_manifest(manifest, other_public)


def test_a_manifest_with_a_bad_version_field_is_rejected():
    manifest, public = _signed_manifest(manifest_version=2)
    with pytest.raises(UpdateError):
        verify_manifest(manifest, public)


def test_version_comparison():
    assert parse_version("1.2.10") > parse_version("1.2.9")
    assert parse_version("1.2") == parse_version("1.2.0")
    assert is_newer("2.0.0", "1.9.9")
    assert not is_newer("1.0.0", "1.0.0")


def test_newer_version_is_offered():
    manifest, public = _signed_manifest()
    status = should_offer(
        Manifest.from_dict(manifest),
        UpdatePolicy(public_key=public, current_version="1.0.0", install_id="x"),
    )
    assert status.available and not status.forced
    assert status.version == "1.2.0"


def test_current_version_is_not_offered():
    manifest, _ = _signed_manifest()
    status = should_offer(Manifest.from_dict(manifest), UpdatePolicy(current_version="1.2.0"))
    assert not status.available
    assert status.reason == "up_to_date"


def test_below_the_version_floor_forces_the_update():
    manifest, _ = _signed_manifest()
    status = should_offer(Manifest.from_dict(manifest), UpdatePolicy(current_version="0.9.0"))
    assert status.available and status.forced
    assert status.reason == "below_min_supported"


def test_staged_rollout_is_deterministic_and_partial():
    manifest, _ = _signed_manifest(rollout_percent=10)
    parsed = Manifest.from_dict(manifest)
    decisions = {
        should_offer(parsed, UpdatePolicy(current_version="1.0.0", install_id=f"user-{i}")).available
        for i in range(200)
    }
    # Some users are in the rollout and some are not; both outcomes must appear.
    assert decisions == {True, False}
    first = should_offer(parsed, UpdatePolicy(current_version="1.0.0", install_id="user-1"))
    second = should_offer(parsed, UpdatePolicy(current_version="1.0.0", install_id="user-1"))
    assert first.available == second.available


def test_forced_update_ignores_the_rollout_percentage():
    manifest, _ = _signed_manifest(rollout_percent=1)
    status = should_offer(Manifest.from_dict(manifest), UpdatePolicy(current_version="0.1.0"))
    assert status.forced


def test_security_rollback_allows_a_downgrade():
    manifest, _ = _signed_manifest(version="1.1.0", security_rollback=True)
    status = should_offer(Manifest.from_dict(manifest), UpdatePolicy(current_version="1.2.0"))
    assert status.available and status.forced
    assert status.reason == "security_rollback"


def test_artifact_hash_is_verified():
    data = b"payload"
    artifact = Artifact(url="u", sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    assert verify_artifact(artifact, data) == data
    with pytest.raises(UpdateError):
        verify_artifact(artifact, data + b"!")


def test_artifact_size_mismatch_is_caught():
    data = b"payload"
    artifact = Artifact(url="u", sha256=hashlib.sha256(data).hexdigest(), size=99)
    with pytest.raises(UpdateError):
        verify_artifact(artifact, data)


def test_load_manifest_runs_the_whole_check():
    manifest, public = _signed_manifest()
    status = load_manifest(
        json.dumps(manifest),
        public,
        policy=UpdatePolicy(current_version="1.0.0", install_id="x"),
    )
    assert status.available


def test_load_manifest_rejects_malformed_json():
    _, public = _signed_manifest()
    with pytest.raises(UpdateError):
        load_manifest("{not json", public)


def test_private_signing_key_is_written_with_restrictive_permissions(tmp_path: Path):
    generate_keypair(tmp_path)
    key_file = tmp_path / "update-signing.key"
    assert key_file.exists()
    if os.name == "posix":
        assert key_file.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# autostart
# ---------------------------------------------------------------------------


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def test_autostart_enable_disable_roundtrip(xdg):
    entry = autostart.enable()
    assert entry.path.exists()
    assert autostart.is_enabled()
    assert autostart.disable() is True
    assert not autostart.is_enabled()
    assert autostart.disable() is False


def test_autostart_writes_a_valid_desktop_entry(xdg):
    entry = autostart.enable(command=["/usr/bin/dotavoice-gui"])
    text = entry.path.read_text(encoding="utf-8")
    assert "[Desktop Entry]" in text
    assert "Exec=/usr/bin/dotavoice-gui" in text
    assert "dotavoice.desktop" in str(entry.path)


def test_autostart_status_reports_the_platform(xdg):
    status = autostart.status()
    assert status["enabled"] is False
    assert status["kind"] in ("linux_desktop", "macos_launchagent", "windows_run")
    assert status["command"]
