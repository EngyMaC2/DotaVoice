"""Tests for the app configuration and path handling."""

from __future__ import annotations

from pathlib import Path

from dota_voice import paths
from dota_voice.appconfig import AppConfig


def test_paths_honour_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTAVOICE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("DOTAVOICE_DATA_DIR", str(tmp_path / "data"))
    assert paths.config_dir() == tmp_path / "cfg"
    assert paths.data_dir() == tmp_path / "data"
    assert paths.config_file() == tmp_path / "cfg" / "settings.json"
    paths.ensure_dirs()
    assert (tmp_path / "cfg").is_dir()
    assert paths.log_file().parent.is_dir()


def test_config_roundtrip(tmp_path: Path):
    path = tmp_path / "settings.json"
    config = AppConfig(dota_path="/games/dota", gsi_token="abc", port=13000)
    config.save(path)

    loaded = AppConfig.load(path)
    assert loaded.dota_path == "/games/dota"
    assert loaded.gsi_token == "abc"
    assert loaded.port == 13000


def test_config_load_missing_returns_defaults(tmp_path: Path):
    config = AppConfig.load(tmp_path / "nope.json")
    assert config.setup_complete is False
    assert config.port == 12000


def test_config_load_ignores_unknown_keys(tmp_path: Path):
    path = tmp_path / "settings.json"
    path.write_text('{"port": 15000, "unknown": 1}', encoding="utf-8")
    config = AppConfig.load(path)
    assert config.port == 15000


def test_config_load_survives_corrupt_json(tmp_path: Path):
    path = tmp_path / "settings.json"
    path.write_text("{ this is not json", encoding="utf-8")
    config = AppConfig.load(path)
    assert config.port == 12000


def test_to_settings_maps_fields():
    config = AppConfig(
        host="127.0.0.1",
        port=12345,
        gsi_token="tok",
        voice_backend="relay",
        display_name="Игрок",
        input_device="Микрофон",
    )
    settings = config.to_settings()
    assert settings.host == "127.0.0.1"
    assert settings.port == 12345
    assert settings.gsi_token == "tok"
    assert settings.voice == "relay"
    assert settings.display_name == "Игрок"
    assert settings.input_device == "Микрофон"


def test_dota_cfg_dir_none_without_path():
    assert AppConfig().dota_cfg_dir is None


def test_dota_cfg_dir_points_at_gsi_folder():
    config = AppConfig(dota_path="/games/dota 2 beta")
    assert config.dota_cfg_dir == Path(
        "/games/dota 2 beta/game/dota/cfg/gamestate_integration"
    )
