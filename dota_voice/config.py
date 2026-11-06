"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


@dataclass
class Settings:
    """Service settings.

    Everything has a safe default so the service boots with zero config; the
    only thing you must wire up for a real game is the GSI token.
    """

    host: str = "0.0.0.0"
    port: int = 12000
    gsi_token: str | None = None
    store: str = "memory"  # memory | redis
    redis_url: str = "redis://localhost:6379/0"
    voice: str = "memory"  # memory | discord
    session_grace_seconds: int = 900
    log_level: str = "INFO"

    # Discord
    discord_bot_token: str | None = None
    discord_guild_id: str | None = None
    discord_user_map: dict[str, str] = field(default_factory=dict)

    # Optional LLM layer
    llm_model: str = "gpt-4.1"
    llm_api_key: str | None = None
    llm_base_url: str | None = None

    # Workspace / persistence
    workspace_dir: Path = field(default_factory=lambda: Path.cwd())

    @property
    def llm_enabled(self) -> bool:
        """Whether the agent should reason with an LLM or run deterministically."""
        return bool(self.llm_api_key)

    @property
    def conversations_dir(self) -> Path:
        return self.workspace_dir / ".conversations"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = env if env is not None else dict(os.environ)
        user_map: dict[str, str] = {}
        raw_map = env.get("DISCORD_USER_MAP", "")
        if raw_map.strip():
            try:
                parsed = json.loads(raw_map)
                if isinstance(parsed, dict):
                    user_map = {str(k): str(v) for k, v in parsed.items()}
            except json.JSONDecodeError:
                user_map = {}
        return cls(
            host=env.get("DOTAVOICE_HOST", "0.0.0.0"),
            port=_int(env.get("DOTAVOICE_PORT"), 12000),
            gsi_token=env.get("DOTAVOICE_GSI_TOKEN") or None,
            store=env.get("DOTAVOICE_STORE", "memory").lower(),
            redis_url=env.get("DOTAVOICE_REDIS_URL", "redis://localhost:6379/0"),
            voice=env.get("DOTAVOICE_VOICE", "memory").lower(),
            session_grace_seconds=_int(env.get("DOTAVOICE_SESSION_GRACE_SECONDS"), 900),
            log_level=env.get("DOTAVOICE_LOG_LEVEL", "INFO").upper(),
            discord_bot_token=env.get("DISCORD_BOT_TOKEN") or None,
            discord_guild_id=env.get("DISCORD_GUILD_ID") or None,
            discord_user_map=user_map,
            llm_model=env.get("LLM_MODEL", "gpt-4.1"),
            llm_api_key=env.get("LLM_API_KEY") or None,
            llm_base_url=env.get("LLM_BASE_URL") or None,
            workspace_dir=Path(env.get("DOTAVOICE_WORKSPACE", str(Path.cwd()))),
        )
