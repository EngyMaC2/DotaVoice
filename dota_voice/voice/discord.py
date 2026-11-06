"""Discord voice backend built on the REST API (no gateway dependency).

Why REST: the orchestrator only needs to create the two team channels, move
members between them, and server-mute/deafen members. All of that is available
over the plain HTTP API, which keeps the runtime dependency-free and lets the
OpenHands agent drive it deterministically.

Limitations (documented in the README):

* Moving a member requires them to already be connected to a voice channel in
  the guild. The bot cannot drag a disconnected user into a channel; it creates
  a small "lobby" channel and players join that first.
* A REST-only bot does not receive ``VOICE_STATE_UPDATE`` / speaking events.
  Speaking indicators are therefore driven by the game/overlay side (see
  :mod:`dota_voice.overlay`) rather than by the gateway.
"""

from __future__ import annotations

import logging

import httpx

from ..models import Roster, Team
from .base import channel_name

logger = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"

# Discord channel types
_GUILD_VOICE = 2


class DiscordAPIError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Discord API error {status}: {body[:300]}")
        self.status = status
        self.body = body


class DiscordVoiceBackend:
    def __init__(
        self,
        bot_token: str,
        guild_id: str,
        user_map: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not bot_token or not guild_id:
            raise ValueError("DiscordVoiceBackend requires bot_token and guild_id")
        self._guild_id = str(guild_id)
        self._user_map = {str(k): str(v) for k, v in (user_map or {}).items()}
        self._client = client or httpx.AsyncClient(
            base_url=API_BASE,
            headers={"Authorization": f"Bot {bot_token}"},
            timeout=10.0,
        )
        # match_id -> team -> channel_id
        self._channels: dict[str, dict[Team, str]] = {}
        # match_id -> set of discord user ids we placed (for cleanup)
        self._members: dict[str, set[str]] = {}
        # match_id -> player_id -> discord user id we have seen
        self._resolved: dict[str, dict[str, str]] = {}

    # ---- helpers ----

    def discord_user(self, player_id: str) -> str | None:
        """Resolve a game player id/name to a Discord user id."""
        if player_id in self._user_map:
            return self._user_map[player_id]
        # Also accept a map keyed by display name, matched case-insensitively.
        lowered = {k.lower(): v for k, v in self._user_map.items()}
        return lowered.get(player_id.lower())

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        resp = await self._client.request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise DiscordAPIError(resp.status_code, resp.text)
        return resp

    async def _list_voice_channels(self) -> dict[str, str]:
        """Return ``{channel_name: channel_id}`` for guild voice channels."""
        resp = await self._request("GET", f"/guilds/{self._guild_id}/channels")
        found: dict[str, str] = {}
        for channel in resp.json():
            if channel.get("type") == _GUILD_VOICE:
                found[channel["name"]] = channel["id"]
        return found

    async def _create_voice_channel(self, name: str) -> str:
        resp = await self._request(
            "POST",
            f"/guilds/{self._guild_id}/channels",
            json={"name": name, "type": _GUILD_VOICE},
        )
        return resp.json()["id"]

    async def _move_member(self, user_id: str, channel_id: str) -> None:
        # Requires the member to already be in a voice channel.
        await self._request(
            "PATCH",
            f"/guilds/{self._guild_id}/members/{user_id}",
            json={"channel_id": channel_id},
        )

    async def _set_member_flags(
        self, user_id: str, *, mute: bool | None = None, deaf: bool | None = None
    ) -> None:
        body: dict[str, bool] = {}
        if mute is not None:
            body["mute"] = mute
        if deaf is not None:
            body["deaf"] = deaf
        if body:
            await self._request(
                "PATCH", f"/guilds/{self._guild_id}/members/{user_id}", json=body
            )

    # ---- VoiceBackend ----

    async def ensure_channels(self, match_id: str) -> tuple[str, str]:
        channels = self._channels.get(match_id)
        if channels and Team.RADIANT in channels and Team.DIRE in channels:
            return channels[Team.RADIANT], channels[Team.DIRE]
        # Reuse channels that already exist by name so a service restart (which
        # loses the in-memory cache) never creates duplicates in the guild.
        existing = await self._list_voice_channels()
        channels = {}
        for team in (Team.RADIANT, Team.DIRE):
            name = channel_name(match_id, team)
            channels[team] = existing.get(name) or await self._create_voice_channel(name)
        self._channels[match_id] = channels
        self._members.setdefault(match_id, set())
        self._resolved.setdefault(match_id, {})
        return channels[Team.RADIANT], channels[Team.DIRE]

    async def sync_roster(self, match_id: str, roster: Roster) -> None:
        radiant_id, dire_id = await self.ensure_channels(match_id)
        channels = {Team.RADIANT: radiant_id, Team.DIRE: dire_id}
        resolved = self._resolved.setdefault(match_id, {})
        members = self._members.setdefault(match_id, set())

        for player in roster.all_players():
            if player.team is Team.UNKNOWN:
                continue
            user_id = self.discord_user(player.player_id) or self.discord_user(player.name)
            if not user_id:
                continue
            resolved[player.player_id] = user_id
            members.add(user_id)
            try:
                await self._move_member(user_id, channels[player.team])
            except DiscordAPIError as exc:
                # 400/404 usually means the member is not in a voice channel yet.
                logger.warning(
                    "Could not move %s into %s channel: %s",
                    player.player_id,
                    player.team.value,
                    exc,
                )

    async def set_mute(self, match_id: str, player_id: str, muted: bool) -> None:
        user_id = self._resolved.get(match_id, {}).get(player_id) or self.discord_user(
            player_id
        )
        if not user_id:
            logger.warning("No Discord user mapping for player %s", player_id)
            return
        await self._set_member_flags(user_id, mute=muted)

    async def set_deafen(self, match_id: str, player_id: str, deafened: bool) -> None:
        user_id = self._resolved.get(match_id, {}).get(player_id) or self.discord_user(
            player_id
        )
        if not user_id:
            logger.warning("No Discord user mapping for player %s", player_id)
            return
        await self._set_member_flags(user_id, deaf=deafened)

    async def remove_player(self, match_id: str, player_id: str) -> None:
        self._resolved.get(match_id, {}).pop(player_id, None)

    async def delete_channels(self, match_id: str) -> None:
        channels = self._channels.pop(match_id, {})
        self._members.pop(match_id, None)
        self._resolved.pop(match_id, None)
        for channel_id in channels.values():
            try:
                await self._request("DELETE", f"/channels/{channel_id}")
            except DiscordAPIError as exc:  # pragma: no cover - network path
                logger.warning("Could not delete channel %s: %s", channel_id, exc)

    async def close(self) -> None:
        await self._client.aclose()
