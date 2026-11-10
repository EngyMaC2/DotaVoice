"""Steam / Dota 2 discovery and GSI config installation.

The app needs to know where Dota 2 lives so it can drop a Game State
Integration config into the game's ``cfg/gamestate_integration`` directory.
Finding that means:

1. locating the Steam install (registry on Windows, well-known paths elsewhere),
2. reading ``libraryfolders.vdf`` to find the library that holds Dota 2,
3. optionally reading ``loginusers.vdf`` so the app can show which Steam
   profile it is configuring.

Everything here is pure filesystem work (no Steam API, no network), so it can
run during the setup wizard and be unit-tested against temporary directories.
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DOTA_APP_ID = "570"
GSI_CONFIG_NAME = "gamestate_integration_dotavoice.cfg"

# Relative path from a Dota 2 install root to the GSI config folder.
GSI_SUBPATH = Path("game") / "dota" / "cfg" / "gamestate_integration"


# ---------------------------------------------------------------------------
# VDF parsing (Valve's KeyValues format, enough for the files we read)
# ---------------------------------------------------------------------------


def parse_vdf(text: str) -> dict:
    """Parse a Valve KeyValues document into nested dicts.

    Handles the subset used by ``libraryfolders.vdf`` and ``loginusers.vdf``:
    quoted keys/values, nested ``{ }`` blocks, and ``//`` comments. It is not a
    full VDF implementation, but it is dependency-free and sufficient here.
    """
    tokens = _tokenize_vdf(text)
    pos = 0

    def parse_object() -> dict:
        nonlocal pos
        obj: dict = {}
        while pos < len(tokens):
            token = tokens[pos]
            if token == "}":
                pos += 1
                break
            key = token
            pos += 1
            if pos >= len(tokens):
                break
            value = tokens[pos]
            if value == "{":
                pos += 1
                obj[key] = parse_object()
            else:
                pos += 1
                obj[key] = value
        return obj

    # A VDF file is wrapped in a single root key: keep it so callers can look
    # up `data["libraryfolders"]` / `data["users"]` as they expect.
    while pos < len(tokens):
        token = tokens[pos]
        if token == "{":
            pos += 1
            return {"": parse_object()}
        if pos + 1 < len(tokens) and tokens[pos + 1] == "{":
            pos += 2
            return {token: parse_object()}
        pos += 1
    return {}


def _tokenize_vdf(text: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch in "{}":
            tokens.append(ch)
            i += 1
            continue
        if ch == '"':
            i += 1
            buf: list[str] = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                    continue
                buf.append(text[i])
                i += 1
            i += 1  # closing quote
            tokens.append("".join(buf))
            continue
        # Unquoted token (rare, but tolerated).
        buf = []
        while i < n and text[i] not in ' \t\r\n"{}':
            buf.append(text[i])
            i += 1
        tokens.append("".join(buf))
    return tokens


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@dataclass
class SteamUser:
    account_id: str
    persona: str
    most_recent: bool = False


@dataclass
class SteamLibrary:
    path: Path
    apps: dict[str, str]


def candidate_steam_roots() -> list[Path]:
    """Well-known Steam install locations for the current platform."""
    candidates: list[Path] = []
    if sys.platform.startswith("win"):
        candidates += _windows_steam_roots()
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "Steam")
    else:
        home = Path.home()
        candidates += [
            home / ".steam" / "steam",
            home / ".steam" / "root",
            home / ".local" / "share" / "Steam",
            home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam",
        ]
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in candidates:
        resolved = path
        if resolved not in seen and resolved.exists():
            seen.add(resolved)
            unique.append(resolved)
    return unique


def _windows_steam_roots() -> list[Path]:
    roots: list[Path] = []
    try:  # pragma: no cover - Windows-only
        import winreg  # type: ignore

        for hive, key in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
        ):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    for value in ("SteamPath", "InstallPath"):
                        try:
                            raw, _ = winreg.QueryValueEx(handle, value)
                            roots.append(Path(raw))
                        except FileNotFoundError:
                            continue
            except OSError:
                continue
    except Exception as exc:  # pragma: no cover - non-Windows / no registry
        logger.debug("Реестр Steam недоступен: %s", exc)
    return roots


def find_steam_root(explicit: str | Path | None = None) -> Path | None:
    """Return the Steam root directory, or ``None`` if not found."""
    if explicit:
        path = Path(explicit)
        if path.exists():
            return path
    for candidate in candidate_steam_roots():
        return candidate
    return None


def read_libraries(steam_root: Path) -> list[SteamLibrary]:
    """Read every library folder declared by ``libraryfolders.vdf``."""
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    libraries: list[SteamLibrary] = []
    if vdf.exists():
        try:
            data = parse_vdf(vdf.read_text(encoding="utf-8", errors="ignore"))
        except OSError as exc:
            logger.warning("Не удалось прочитать %s: %s", vdf, exc)
            data = {}
        for value in (data.get("libraryfolders") or {}).values():
            if not isinstance(value, dict):
                continue
            raw_path = value.get("path") or ""
            if not raw_path:
                continue
            apps = {
                str(app_id): str(meta.get("name", ""))
                for app_id, meta in (value.get("apps") or {}).items()
                if isinstance(meta, dict)
            }
            libraries.append(SteamLibrary(Path(raw_path), apps))
    # Always include the Steam root's own steamapps folder.
    root_apps = _apps_in_library(steam_root)
    if root_apps is not None:
        libraries.append(SteamLibrary(steam_root, root_apps))
    return libraries


def _apps_in_library(library_root: Path) -> dict[str, str] | None:
    steamapps = library_root / "steamapps"
    if not steamapps.exists():
        return None
    apps: dict[str, str] = {}
    for manifest in steamapps.glob("appmanifest_*.acf"):
        text = manifest.read_text(encoding="utf-8", errors="ignore")
        app_id = re.search(r'"appid"\s*"(\d+)"', text)
        name = re.search(r'"name"\s*"([^"]*)"', text)
        if app_id:
            apps[app_id.group(1)] = name.group(1) if name else ""
    return apps


def find_dota_path(
    steam_root: Path | None = None, explicit: str | Path | None = None
) -> Path | None:
    """Locate the Dota 2 install root (the folder containing ``game/``)."""
    if explicit:
        path = Path(explicit)
        if (path / "game" / "dota").exists():
            return path
        # Accept a path pointing straight at the ``game`` folder or its parent.
        for candidate in (path, path.parent, path.parent.parent):
            if (candidate / "game" / "dota").exists():
                return candidate
    root = find_steam_root(steam_root)
    if root is None:
        return None
    for library in read_libraries(root):
        if DOTA_APP_ID in library.apps:
            common = library.path / "steamapps" / "common" / "dota 2 beta"
            if (common / "game" / "dota").exists():
                return common
        # Fall back to the conventional folder name even without a manifest.
        common = library.path / "steamapps" / "common" / "dota 2 beta"
        if (common / "game" / "dota").exists():
            return common
    return None


def read_users(steam_root: Path) -> list[SteamUser]:
    """Read configured Steam accounts from ``loginusers.vdf``."""
    vdf = steam_root / "config" / "loginusers.vdf"
    if not vdf.exists():
        return []
    try:
        data = parse_vdf(vdf.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return []
    users: list[SteamUser] = []
    for steam_id, info in (data.get("users") or {}).items():
        if not isinstance(info, dict):
            continue
        users.append(
            SteamUser(
                account_id=str(steam_id),
                persona=str(info.get("PersonaName", "")),
                most_recent=str(info.get("MostRecent", "0")) == "1",
            )
        )
    return users


def pick_user(users: list[SteamUser]) -> SteamUser | None:
    if not users:
        return None
    for user in users:
        if user.most_recent:
            return user
    return users[0]


# ---------------------------------------------------------------------------
# GSI config installation
# ---------------------------------------------------------------------------

# The `data` modules the app cannot work without. `allplayers` is what makes the
# other nine players (and their heroes) visible; `draft` is what gives a normal,
# non-spectating client its teammates' heroes. An older build shipped a config
# without them, so a config that is merely *present* is not necessarily usable -
# it has to carry these.
REQUIRED_GSI_MODULES = ("map", "player", "hero", "allplayers", "draft")


def gsi_config_modules(text: str) -> dict[str, str]:
    """The `data` block of a GSI config as ``{module: value}``; empty on error."""
    try:
        root = parse_vdf(text)
    except Exception:  # noqa: BLE001 - any malformed file is simply "not usable"
        return {}
    for value in root.values():
        if isinstance(value, dict) and isinstance(value.get("data"), dict):
            return {str(k): str(v) for k, v in value["data"].items()}
    return {}


def gsi_config_is_current(dota_path: str | Path | None) -> bool:
    """Whether the installed config enables every module the app needs.

    A config written by an older version can be missing ``allplayers`` or
    ``draft``; the game then reports only the local player and no teammate
    heroes. Treating such a file as installed would leave that broken state
    forever, so this is what the startup check looks at, not mere existence.
    """
    if not dota_path:
        return False
    target = Path(dota_path) / GSI_SUBPATH / GSI_CONFIG_NAME
    if not target.exists():
        return False
    try:
        modules = gsi_config_modules(target.read_text(encoding="utf-8"))
    except OSError:
        return False
    return all(modules.get(module) == "1" for module in REQUIRED_GSI_MODULES)


def gsi_config_text(
    token: str,
    *,
    host: str = "127.0.0.1",
    port: int = 12000,
    name: str = "dotavoice",
) -> str:
    """Render the GSI config the game should load."""
    return (
        f'"{name}"\n'
        "{\n"
        f'    "uri"       "http://{host}:{port}/gsi"\n'
        '    "timeout"   "5.0"\n'
        '    "buffer"    "0.1"\n'
        '    "throttle"  "0.1"\n'
        '    "heartbeat" "30.0"\n'
        '    "auth"\n'
        "    {\n"
        f'        "token"  "{token}"\n'
        "    }\n"
        '    "data"\n'
        "    {\n"
        '        "provider"    "1"\n'
        '        "map"         "1"\n'
        '        "player"      "1"\n'
        '        "hero"        "1"\n'
        '        "allplayers"  "1"\n'
        '        "abilities"   "0"\n'
        '        "items"       "0"\n'
        '        "buildings"   "0"\n'
        # The draft module carries every pick (and ban) as soon as it happens,
        # which is the only way a normal, non-spectating client can learn its
        # teammates' heroes - its `allplayers` is empty.
        '        "draft"       "1"\n'
        "    }\n"
        "}\n"
    )


def install_gsi_config(
    dota_path: str | Path,
    token: str,
    *,
    host: str = "127.0.0.1",
    port: int = 12000,
) -> Path:
    """Write the GSI config into the Dota 2 install; return the written path.

    Raises :class:`FileNotFoundError` if ``dota_path`` does not look like a
    Dota 2 install, and :class:`PermissionError` if the folder is not writable
    (the caller can then offer to retry with elevated rights).
    """
    dota = Path(dota_path)
    if not (dota / "game" / "dota").exists():
        raise FileNotFoundError(f"Не найдена установка Dota 2 в {dota}")
    target_dir = dota / GSI_SUBPATH
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / GSI_CONFIG_NAME
    target.write_text(gsi_config_text(token, host=host, port=port), encoding="utf-8")
    return target


def gsi_config_installed(dota_path: str | Path | None) -> bool:
    if not dota_path:
        return False
    return (Path(dota_path) / GSI_SUBPATH / GSI_CONFIG_NAME).exists()


def ensure_gsi_config(
    dota_path: str | Path,
    token: str,
    *,
    host: str = "127.0.0.1",
    port: int = 12000,
) -> bool:
    """Reinstall the GSI config when the installed one is missing or outdated.

    Returns True when a (re)write happened. A config left by an older version
    lacks ``allplayers``/``draft`` and makes the game report only the local
    player, so it is silently upgraded here on every start; a current config is
    left untouched.
    """
    if gsi_config_is_current(dota_path):
        return False
    install_gsi_config(dota_path, token, host=host, port=port)
    return True


def remove_gsi_config(dota_path: str | Path) -> bool:
    target = Path(dota_path) / GSI_SUBPATH / GSI_CONFIG_NAME
    if target.exists():
        target.unlink()
        return True
    return False
