"""Voice backend that carries audio directly between players.

This adapts the encrypted peer mesh (:mod:`dota_voice.net`) to the same
:class:`~dota_voice.voice.base.VoiceBackend` contract the orchestrator already
uses, so the agent tools (`create_voice_channels`, `sync_roster`, `set_mute`, ...)
drive a serverless voice room.

P2WLAN + GSI is the default path
--------------------------------
The match id from GSI becomes a P2WLAN room, the room gives every teammate a
virtual IP, and the mesh dials those IPs directly. Steam is not involved at all,
so the account risk is zero. If the room cannot be built and the operator asked
for it, the Steam SDR transport is used as a fallback instead.

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
from ..net.p2wlan import P2WlanController, P2WlanRoom
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
        controller: P2WlanController | None = None,
    ) -> None:
        self.match_id = match_id
        self._loop: asyncio.AbstractEventLoop | None = None
        self._audio = AudioEngine(
            self._on_frame,
            input_device=input_device,
            output_device=output_device,
        )
        self._on_peer_speaking = on_peer_speaking
        self._controller = controller
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

    @property
    def room(self) -> P2WlanRoom | None:
        if self._controller is None:
            return None
        return self._controller.room(self.match_id)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def start(self) -> None:
        self.bind_loop(asyncio.get_running_loop())
        await self.mesh.start()
        self._audio.start()
        # The room may already exist (a restart); dial anyone already on it.
        self.dial_room_peers()

    def dial_room_peers(self) -> int:
        """Open links to every member of this match's P2WLAN room.

        Members are addressed by their virtual IPs, so this works regardless of
        NAT: P2WLAN has already made the peers reachable to each other.
        """
        room = self.room
        if room is None:
            return 0
        dialed = 0
        for member, entry in room.members.items():
            if member == self.mesh.local_name or not entry.online:
                continue
            if self.mesh.connect_to(member, (entry.virtual_ip, self.mesh.transport.local_address[1]), entry.team):
                dialed += 1
        return dialed

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
        room = self.room
        data["room"] = {
            "room_id": room.room_id,
            "invite_key": room.invite_key,
            "subnet": room.subnet,
            "gateway": room.gateway,
            "relayed": room.relayed,
            "members": {k: v.virtual_ip for k, v in room.members.items()},
        } if room else None
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
        controller: P2WlanController | None = None,
    ) -> None:
        self._input_device = input_device
        self._output_device = output_device
        self._group = discovery_group
        self._on_peer_speaking = on_peer_speaking
        # Injectable so tests can run without multicast on the real network.
        self._discovery_factory = discovery_factory
        self._controller = controller
        self._sessions: dict[str, MeshSession] = {}
        self._local_name = ""

    # ------------------------------------------------------------------

    @property
    def controller(self) -> P2WlanController | None:
        return self._controller

    @property
    def p2wlan(self) -> dict:
        if self._controller is None:
            return {}
        return self._controller.snapshot()

    def set_local_name(self, name: str) -> None:
        self._local_name = name

    async def create_room(self, match_id: str, team: str = "unknown") -> P2WlanRoom:
        """Create the P2WLAN room for a match and dial whoever is already on it."""
        if self._controller is None:
            raise RuntimeError("P2WLAN не настроен")
        room = await self._controller.create_room(match_id, team)
        self._controller.add_member(match_id, self._local_name or "host", team)
        session = self._sessions.get(match_id)
        if session is not None:
            session.dial_room_peers()
        return room

    async def join_room(self, key: str, *, match_id: str | None = None) -> P2WlanRoom:
        if self._controller is None:
            raise RuntimeError("P2WLAN не настроен")
        return await self._controller.join_room(key, match_id=match_id)

    def invite_for(self, match_id: str) -> str:
        if self._controller is None:
            return ""
        return self._controller.invite_for(match_id)

    def add_room_member(self, match_id: str, member: str, team: str = "unknown"):
        """Record a teammate on the room and dial their virtual IP."""
        if self._controller is None:
            return None
        entry = self._controller.add_member(match_id, member, team)
        session = self._sessions.get(match_id)
        if session is not None:
            session.dial_room_peers()
        return entry

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
                controller=self._controller,
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
        # The P2WLAN room is what actually puts teammates on one network.
        session = await self._session(match_id)
        if self._controller is not None:
            await self.create_room(match_id, session.mesh.local_team)
        session.dial_room_peers()
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
        session.mesh.disconnect(player_id)
        if self._controller is not None:
            self._controller.remove_member(match_id, player_id)

    async def delete_channels(self, match_id: str) -> None:
        session = self._sessions.pop(match_id, None)
        if session is not None:
            await session.stop()
        if self._controller is not None:
            await self._controller.leave_room(match_id)

    async def close(self) -> None:
        """Shut down local sessions but keep the rooms on record.

        Closing the app is not the same as ending a match: the room is the
        player's identity on the team network, and the next launch must rejoin
        it (see ``P2WlanController.close``). Only an ended match drops a room.
        """
        for match_id in list(self._sessions):
            session = self._sessions.pop(match_id, None)
            if session is not None:
                await session.stop()
            if self._controller is not None:
                await self._controller.detach_room(match_id)
        if self._controller is not None:
            await self._controller.close()

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
