"""Tests for Steam/Dota discovery and GSI config installation.

These run entirely against temporary directories - no real Steam install is
required - and cover the VDF parsing that everything else depends on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dota_voice import steam


LIBRARYFOLDERS = '''
"libraryfolders"
{
    "0"
    {
        "path"      "/home/user/.steam/steam"
        "label"     ""
        "apps"
        {
            "570"       "Dota 2"
            "730"       "Counter-Strike 2"
        }
    }
    "1"
    {
        "path"      "/mnt/games/SteamLibrary"
        "apps"
        {
            "570"       "Dota 2"
        }
    }
}
'''

LOGINUSERS = '''
"users"
{
    "76561198000000001"
    {
        "AccountName"   "old"
        "PersonaName"   "Старый"
        "MostRecent"    "0"
    }
    "76561198000000002"
    {
        "AccountName"   "new"
        "PersonaName"   "Новый"
        "MostRecent"    "1"
    }
}
'''


def _make_dota(root: Path) -> Path:
    dota = root / "steamapps" / "common" / "dota 2 beta"
    (dota / "game" / "dota").mkdir(parents=True)
    return dota


def test_parse_vdf_reads_nested_keys():
    data = steam.parse_vdf(LIBRARYFOLDERS)
    folders = data["libraryfolders"]
    assert folders["0"]["path"] == "/home/user/.steam/steam"
    assert folders["0"]["apps"]["570"] == "Dota 2"
    assert folders["1"]["path"] == "/mnt/games/SteamLibrary"


def test_parse_vdf_ignores_comments():
    text = '"root"\n{\n  // комментарий\n  "a" "b"\n}\n'
    assert steam.parse_vdf(text) == {"root": {"a": "b"}}


def test_read_libraries_and_find_dota(tmp_path: Path):
    steam_root = tmp_path / "steam"
    (steam_root / "steamapps").mkdir(parents=True)
    (steam_root / "steamapps" / "libraryfolders.vdf").write_text(
        LIBRARYFOLDERS, encoding="utf-8"
    )
    library = tmp_path / "lib"
    dota = _make_dota(library)
    # Point library 1 at our temporary library so Dota is found there.
    (steam_root / "steamapps" / "libraryfolders.vdf").write_text(
        LIBRARYFOLDERS.replace("/mnt/games/SteamLibrary", str(library)),
        encoding="utf-8",
    )

    libraries = steam.read_libraries(steam_root)
    assert any(lib.path == library for lib in libraries)

    found = steam.find_dota_path(steam_root)
    assert found == dota


def test_find_dota_explicit_path(tmp_path: Path):
    dota = _make_dota(tmp_path)
    assert steam.find_dota_path(explicit=dota) == dota
    # Also accepts the "game" subfolder.
    assert steam.find_dota_path(explicit=dota / "game") == dota


def test_read_users_prefers_most_recent(tmp_path: Path):
    steam_root = tmp_path / "steam"
    (steam_root / "config").mkdir(parents=True)
    (steam_root / "config" / "loginusers.vdf").write_text(LOGINUSERS, encoding="utf-8")

    users = steam.read_users(steam_root)
    assert len(users) == 2
    chosen = steam.pick_user(users)
    assert chosen is not None and chosen.persona == "Новый"


def test_install_gsi_config_writes_file(tmp_path: Path):
    dota = _make_dota(tmp_path)
    target = steam.install_gsi_config(dota, "secret", host="127.0.0.1", port=12345)
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert '"token"  "secret"' in text
    assert "http://127.0.0.1:12345/gsi" in text
    assert steam.gsi_config_installed(dota)
    assert steam.remove_gsi_config(dota)
    assert not steam.gsi_config_installed(dota)


def test_install_gsi_config_rejects_non_dota(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        steam.install_gsi_config(tmp_path, "secret")


def test_gsi_config_text_is_valid_vdf():
    text = steam.gsi_config_text("tok", port=12000)
    data = steam.parse_vdf(text)
    config = data["dotavoice"]
    assert config["uri"] == "http://127.0.0.1:12000/gsi"
    assert config["auth"]["token"] == "tok"
    assert config["data"]["provider"] == "1"
    # allplayers is required so a spectator sees both full teams.
    assert config["data"]["allplayers"] == "1"
    # The draft module is what gives a normal client its teammates' heroes.
    assert config["data"]["draft"] == "1"


def test_repo_gsi_template_requests_allplayers():
    template = Path(__file__).resolve().parent.parent / "gsi" / (
        "gamestate_integration_dotavoice.cfg"
    )
    config = steam.parse_vdf(template.read_text(encoding="utf-8"))["dotavoice"]
    assert config["data"]["allplayers"] == "1"
    assert config["data"]["draft"] == "1"
