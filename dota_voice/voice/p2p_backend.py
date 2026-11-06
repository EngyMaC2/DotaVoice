"""Voice backend that carries audio directly between players.

This adapts the encrypted peer mesh (:mod:`dota_voice.net`) to the same
:class:`~dota_voice.voice.base.VoiceBackend` contract the orchestrator already
uses, so the agent tools (`create_voice_channels`, `sync_roster`, `set_mute`, ...)
drive a serverless voice room.

How it differs from the relay backend
-------------------------------------
* Channels are *logical*: a channel is "my team in this match". There is no
  central room; each player links directly to the teammates it discovers.
* Mute is **local and one-sided**. Muting a player stops sending them our audio
  and stops playing theirs. It does not silence them for anyone else, and it
  needs no server - which is exactly the "each player decides for themselves"
  model.
* Team isolation is enforced twice: peers on the other side are never linked, and
  the link itself would not carry the frame anyway.

The backend is opt-in (``DOTAVOICE_VOICE=p2p``) because it opens peer-to-peer
sockets and discovers peers on the local network.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from ..models import Roster, Team
from ..net.discovery import LanDiscovery
from ..net.mesh import VoiceMesh
from ..net.transport import Transport
from .audio import AudioEngine

logger = logging.getLogger(__name__)


class MeshSession:
    """One match: a mesh plus the local audio engine."""

    def __init__(
        self,
        match_id: str,
        *,
        local_name: str,
        local_team: str,
        discovery: LanDiscovery,
        transport: Transport | None = None,
        input_device: str | None = None,
        output_device: str | None = None,
        on_peer_speaking=None,
    ) -> None:
        self.match_id = match_id
        self._loop: asyncio.AbstractEventLoop | None = None
        self._audio = AudioEngine(
            self._on_frame,
            input_device=input_device,
            output_device=output_device,
        )
        self._on_peer_speaking = on_peer_speaking
        self.mesh = VoiceMesh(
            match_id,
            local_name=local_name,
            local_team=local_team,
            transport=transport,
            discovery=discovery,
            on_peer_speaking=self._speaking,
            on_audio=self._play,
        )
        self._transmitting = False
        self._muted_peers: set[str] = set()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def start(self) -> None:
        self.bind_loop(asyncio.get_running_loop())
        await self.mesh.start()
        self._audio.start()

    def _play(self, frame: bytes) -> None:
        """Play a decrypted frame. Already gated by per-peer mute in the link."""
        self._audio.play(frame)

    def _speaking(self, peer_name: str, speaking: bool) -> None:
        if self._on_peer_speaking is not None:
            self._on_peer_speaking(self.match_id, peer_name, speaking)

    async def stop(self) -> None:
        self._audio.stop()
        await self.mesh.stop()

    # ------------------------------------------------------------------

    def set_team(self, team: str) -> None:
        self.mesh.local_team = team
        if self.mesh._discovery is not None:
            self.mesh._discovery.team = team

    def set_transmitting(self, active: bool) -> None:
        if active == self._transmitting:
            return
        self._transmitting = active
        self._audio.set_transmitting(active)
        self.mesh.broadcast_control({"type": "state", "speaking": active})

    def set_deafened(self, deafened: bool) -> None:
        self.mesh.set_deafened(deafened)

    def _on_frame(self, frame: bytes) -> None:
        """Called from the audio capture thread."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(self.mesh.broadcast_voice, frame)

    def mute_peer(self, peer_name: str, muted: bool) -> bool:
        ok = self.mesh.set_peer_muted(peer_name, muted)
        if muted:
            self._muted_peers.add(peer_name)
        else:
            self._muted_peers.discard(peer_name)
        return ok

    def snapshot(self) -> dict:
        data = self.mesh.snapshot()
        data["muted_peers"] = sorted(self._muted_peers)
        data["audio"] = self._audio.ready
        return data


class P2PVoiceBackend:
    """Serverless voice backend over the encrypted peer mesh."""

    def __init__(
        self,
        *,
        input_device: str | None = None,
        output_device: str | None = None,
        discovery_group: str | None = None,
        on_peer_speaking=None,
        discovery_factory=None,
    ) -> None:
        self._input_device = input_device
        self._output_device = output_device
        self._group = discovery_group
        self._on_peer_speaking = on_peer_speaking
        # Injectable so tests can run without multicast on the real network.
        self._discovery_factory = discovery_factory
        self._sessions: dict[str, MeshSession] = {}
        self._local_name = ""

    # ------------------------------------------------------------------

    def set_local_name(self, name: str) -> None:
        self._local_name = name

    def _dispatch_speaking(self, match_id: str, peer_name: str, speaking: bool) -> None:
        """Look the callback up per call, so it can be replaced at runtime."""
        if self._on_peer_speaking is not None:
            self._on_peer_speaking(match_id, peer_name, speaking)

    def session_for(self, match_id: str) -> MeshSession | None:
        return self._sessions.get(match_id)

    @property
    def sessions(self) -> dict[str, MeshSession]:
        return dict(self._sessions)

    def _make_discovery(self, match_id: str, team: str):
        if self._discovery_factory is not None:
            return self._discovery_factory(match_id, team)
        kwargs = {"group": self._group} if self._group else {}
        return LanDiscovery(
            match_id,
            name=self._local_name,
            team=team,
            **kwargs,
        )

    async def _session(self, match_id: str, team: str = "unknown") -> MeshSession:
        session = self._sessions.get(match_id)
        if session is None:
            session = MeshSession(
                match_id,
                local_name=self._local_name,
                local_team=team,
                discovery=self._make_discovery(match_id, team),
                input_device=self._input_device,
                output_device=self._output_device,
                on_peer_speaking=self._dispatch_speaking,
            )
            await session.start()
            self._sessions[match_id] = session
        else:
            session.set_team(team)
        return session

    # ------------------------------------------------------------------
    # VoiceBackend contract
    # ------------------------------------------------------------------

    async def ensure_channels(self, match_id: str) -> tuple[str, str]:
        # Channels are logical; starting the mesh is what "creating" means.
        await self._session(match_id)
        return (f"{match_id}-radiant", f"{match_id}-dire")

    async def sync_roster(self, match_id: str, roster: Roster) -> None:
        team = "unknown"
        for player in roster.all_players():
            if player.is_local:
                team = player.team.value
                break
        session = await self._session(match_id, team)
        session.set_team(team)
        for player in roster.all_players():
            if player.team is Team.UNKNOWN:
                continue
            # A player who has been muted keeps that state across roster syncs.
            if player.player_id in session._muted_peers:
                session.mute_peer(player.player_id, True)

    async def set_mute(self, match_id: str, player_id: str, muted: bool) -> None:
        session = self._sessions.get(match_id)
        if session is None:
            return
        if not session.mute_peer(player_id, muted):
            logger.debug("Не удалось изменить мут для %s: участник не найден", player_id)

    async def set_deafen(self, match_id: str, player_id: str, deafened: bool) -> None:
        session = self._sessions.get(match_id)
        if session is not None:
            session.set_deafened(deafened)

    async def remove_player(self, match_id: str, player_id: str) -> None:
        session = self._sessions.get(match_id)
        if session is None:
            return
        link = session.mesh.link_for(player_id)
        if link is not None:
            link.close()
            session.mesh._links.pop(link.address, None)

    async def delete_channels(self, match_id: str) -> None:
        session = self._sessions.pop(match_id, None)
        if session is not None:
            await session.stop()

    async def close(self) -> None:
        for match_id in list(self._sessions):
            await self.delete_channels(match_id)

    # ------------------------------------------------------------------
    # local controls (used by the API layer)
    # ------------------------------------------------------------------

    def set_transmitting(self, active: bool) -> list[str]:
        for session in self._sessions.values():
            session.set_transmitting(active)
        return list(self._sessions)

    def set_deafened(self, deafened: bool) -> list[str]:
        for session in self._sessions.values():
            session.set_deafened(deafened)
        return list(self._sessions)

    @property
    def joined_rooms(self) -> list[str]:
        return list(self._sessions)

    def snapshot(self) -> dict:
        return {
            "backend": "p2p",
            "matches": {
                match_id: session.snapshot()
                for match_id, session in self._sessions.items()
            },
        }

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            await self.close()
