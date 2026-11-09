"""Mesh of encrypted peer links for one match.

:class:`VoiceMesh` wires discovery, a transport and a set of
:class:`~dota_voice.net.link.PeerLink` objects together. It is the piece the
voice backend drives:

* when discovery reports a peer, the mesh opens an encrypted link;
* audio handed to the mesh is encrypted once per peer and sent;
* audio received is decrypted and surfaced to a callback;
* per-peer mute is local and one-sided.

No session state is kept here - the match session (keyed by ``match_id``) lives
in :mod:`dota_voice.agent.manager`, so a restart re-attaches to the same session
and re-opens the same links rather than creating new ones.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from .discovery import LanDiscovery, PeerRecord
from .link import LinkState, PeerLink
from .transport import Transport, UdpTransport

logger = logging.getLogger(__name__)

# Peers we have not heard from for this long are dropped.
LINK_TIMEOUT = 30.0
# A handshake that never completes is abandoned rather than left half-open.
HANDSHAKE_TIMEOUT = 8.0
_SWEEP_INTERVAL = 5.0


class VoiceMesh:
    def __init__(
        self,
        match_id: str,
        *,
        local_name: str = "",
        local_team: str = "unknown",
        transport: Transport | None = None,
        discovery: LanDiscovery | None = None,
        on_peer_speaking=None,
        on_peer_joined=None,
        on_peer_left=None,
        on_peer_team=None,
        on_audio=None,
        on_chat=None,
    ) -> None:
        self.match_id = match_id
        self.local_name = local_name
        self.local_team = local_team
        self.transport = transport or UdpTransport()
        self._discovery = discovery
        self._links: dict[tuple[str, int], PeerLink] = {}
        # Team reported by each peer's announcement, keyed by address.
        self._peer_teams: dict[tuple[str, int], str] = {}
        # Latest announcement per address, including peers we are *not* linked
        # to. Kept so the post-match window can dial the other side without
        # waiting for a fresh announcement.
        self._discovered: dict[tuple[str, int], PeerRecord] = {}
        self._on_peer_speaking = on_peer_speaking
        self._on_peer_joined = on_peer_joined
        self._on_peer_left = on_peer_left
        # Called with ``(peer_name, team)`` whenever a peer's side is learned,
        # so the session can show the full team split for a normal player whose
        # own GSI only reports their slot.
        self._on_peer_team = on_peer_team
        # Called with each decrypted frame, whatever link it came from.
        self._on_audio = on_audio
        # Called with ``(peer_name, message_dict)`` for each chat message.
        self._on_chat = on_chat
        # Off during a match: teammates only. Turned on for the post-match
        # window (ТЗ §8.4) so the two sides can talk in the shared channel. Even
        # then the media path refuses to carry voice across the boundary - only
        # text travels on a cross-team link.
        self.allow_cross_team = False
        self._sweeper: asyncio.Task | None = None
        self._running = False
        # Peers whose team we have already reported, so we do not spam the
        # session with the same announcement every couple of seconds.
        self._reported_teams: dict[str, str] = {}

    # ------------------------------------------------------------------

    async def start(self) -> None:
        await self.transport.start()
        self.transport.on_datagram(self._on_datagram)
        if self._discovery is not None:
            self._discovery.port = self.transport.local_address[1]
            self._discovery.team = self.local_team
            self._discovery.on_peer(self._on_discovered)
            await self._discovery.start()
        self._sweeper = asyncio.create_task(self._sweep_loop())
        self._running = True
        logger.info("P2P-сеть матча запущена (локальный порт %s)", self.transport.local_address[1])

    async def stop(self) -> None:
        self._running = False
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None
        for link in list(self._links.values()):
            link.close()
        self._links.clear()
        if self._discovery is not None:
            await self._discovery.stop()
        await self.transport.stop()

    # ------------------------------------------------------------------

    @property
    def links(self) -> list[PeerLink]:
        return list(self._links.values())

    @property
    def established_count(self) -> int:
        return sum(1 for link in self._links.values() if link.is_ready())

    def link_for(self, peer_name: str) -> PeerLink | None:
        for link in self._links.values():
            if link.peer_name == peer_name:
                return link
        return None

    # ------------------------------------------------------------------

    def _team_ok(self, addr: tuple[str, int], team: str | None = None) -> bool:
        """Whether a peer may share a *voice* channel with us.

        Team isolation is enforced here, not just in the relay: we only ever
        carry audio to peers on our own side. A peer whose team we do not know
        yet is not linked, so an unknown sender cannot join by racing discovery.
        """
        if self.local_team in ("", "unknown"):
            return True
        peer_team = team if team is not None else self._peer_teams.get(addr, "unknown")
        return peer_team == self.local_team

    def _should_link(self, addr: tuple[str, int], team: str | None = None) -> bool:
        """Whether we may open a link to a peer at all.

        A teammate always may. An opposing peer may be linked only while the
        post-match window is open (``allow_cross_team``), and even then the link
        carries text only, because :meth:`_handle_voice` and
        :meth:`broadcast_voice` still gate audio on :meth:`_team_ok`.
        """
        if self._team_ok(addr, team):
            return True
        return self.allow_cross_team

    def _report_team(self, name: str, team: str) -> None:
        """Tell the session about a peer's team, once per change.

        A normal player's GSI reports only their own slot, so the game alone
        cannot tell us the full team split. Every peer's announcement carries
        its team, though, so as peers appear the session learns the split and
        can place each player in the right channel - automatically.
        """
        if not name or not team or team == "unknown":
            return
        if self._reported_teams.get(name) == team:
            return
        self._reported_teams[name] = team
        if self._on_peer_team is not None:
            with contextlib.suppress(Exception):
                self._on_peer_team(name, team)

    def _on_discovered(self, record: PeerRecord) -> None:
        if not self._running:
            return
        key = record.address
        # Remember every announcement, even from the other side, so the
        # post-match window can dial them later without a new announcement.
        self._discovered[key] = record
        if record.team:
            self._peer_teams[key] = record.team
            self._report_team(record.name or f"{record.host}:{record.port}", record.team)
        if key in self._links:
            return
        # Skip our own announcements (same port on loopback).
        if record.port == self.transport.local_address[1]:
            return
        if not self._should_link(key, record.team):
            logger.debug("Участник %s из другой команды — канал не создаётся", key)
            return
        self.connect_to(record.name or f"{record.host}:{record.port}", key, record.team)

    def dial_cross_team(self) -> int:
        """Open text-only links to the opposing side (post-match window).

        Called when the match ends. Only peers whose side we know *and* which
        differ from ours are dialled - teammates already have a link. Voice
        stays blocked on these links; only the shared post-match channel uses
        them. Returns how many links were opened.
        """
        self.allow_cross_team = True
        dialed = 0
        for key, record in list(self._discovered.items()):
            if key in self._links:
                continue
            if record.port == self.transport.local_address[1]:
                continue
            if self._team_ok(key, record.team):
                continue  # a teammate (already linked) or an unknown side
            if not record.team or record.team == "unknown":
                continue
            link = self.connect_to(
                record.name or f"{record.host}:{record.port}", key, record.team
            )
            if link is not None:
                dialed += 1
        return dialed

    def connect_to(
        self, peer_name: str, address: tuple[str, int], team: str = "unknown"
    ) -> PeerLink | None:
        """Open an encrypted link to a known endpoint.

        Used both by discovery and by P2WLAN: once a teammate has a virtual IP on
        the room's subnet, we do not need to find them again - we can dial them
        directly. Returns ``None`` if the peer is filtered out or already linked.
        """
        if address in self._links:
            return self._links[address]
        if team and team != "unknown":
            self._peer_teams[address] = team
        if not self._should_link(address, team):
            logger.debug("Участник %s из другой команды — канал не создаётся", address)
            return None
        link = PeerLink(
            self.match_id,
            peer_name or f"{address[0]}:{address[1]}",
            address,
            self.transport,
            local_name=self.local_name,
            initiator=True,
            on_voice=self._handle_voice,
            on_control=self._handle_control,
            on_established=self._handle_established,
        )
        self._links[address] = link
        hello = link.start_handshake()
        link.transport.send(hello, link.address)
        logger.info("Начинаю рукопожатие с %s", link.address)
        return link

    def disconnect(self, peer_name: str) -> bool:
        """Close the link to a peer (used when a teammate leaves the room)."""
        link = self.link_for(peer_name)
        if link is None:
            return False
        link.close()
        self._links.pop(link.address, None)
        self._peer_teams.pop(link.address, None)
        return True

    def _on_datagram(self, data: bytes, addr) -> None:
        link = self._links.get(addr)
        if link is None:
            # An unknown peer may be opening a link to us (we are the responder).
            link = PeerLink(
                self.match_id,
                f"{addr[0]}:{addr[1]}",
                addr,
                self.transport,
                local_name=self.local_name,
                initiator=False,
                on_voice=self._handle_voice,
                on_control=self._handle_control,
                on_established=self._handle_established,
            )
            self._links[addr] = link
        link.handle_datagram(data)
        if link.state == LinkState.FAILED:
            self._links.pop(addr, None)

    def _handle_established(self, link: PeerLink) -> None:
        # The link is only a transport. Team isolation is enforced on the media
        # path below, so a link to a peer we have not classified yet can stay up
        # without leaking audio - it simply carries nothing until teams are known.
        # Announce our identity so the peer can classify us the same way.
        link.send_control({"type": "hello", "team": self.local_team, "name": self.local_name})
        if self._on_peer_joined is not None:
            with contextlib.suppress(Exception):
                self._on_peer_joined(link)

    def _handle_voice(self, link: PeerLink, frame: bytes) -> None:
        if not self._team_ok(link.address):
            return
        if self._on_audio is not None:
            with contextlib.suppress(Exception):
                self._on_audio(frame)
        if self._on_peer_speaking is not None:
            with contextlib.suppress(Exception):
                self._on_peer_speaking(link.peer_name, True)

    def _dedupe(self, link: PeerLink) -> None:
        """Keep one link per peer.

        The same peer can be reachable over several addresses (loopback plus a
        LAN address, or two interfaces). Without this, its audio would be
        decrypted and played once per address - an echo, and wasted bandwidth.
        The first established link wins; later duplicates are closed.
        """
        for other in list(self._links.values()):
            if other is link or not other.is_ready():
                continue
            if other.peer_name == link.peer_name:
                logger.info(
                    "Дублирующий канал с %s (%s) закрыт в пользу %s",
                    link.peer_name,
                    link.address,
                    other.address,
                )
                link.close()
                self._links.pop(link.address, None)
                self._peer_teams.pop(link.address, None)
                return

    def _handle_control(self, link: PeerLink, message: dict) -> None:
        if message.get("type") == "bye":
            # The peer's app is shutting down; drop the link now instead of
            # waiting for the silence timeout, and tell the session so the
            # roster can grey that player out at once.
            self._links.pop(link.address, None)
            self._peer_teams.pop(link.address, None)
            if self._on_peer_left is not None:
                with contextlib.suppress(Exception):
                    self._on_peer_left(link.peer_name)
            return
        if message.get("team"):
            self._peer_teams[link.address] = str(message["team"])
        if message.get("name"):
            link.peer_name = str(message["name"])
            self._dedupe(link)
        if message.get("team") and message.get("name"):
            self._report_team(link.peer_name, str(message["team"]))
        if message.get("type") == "chat":
            self._handle_chat(link, message)
            return
        if message.get("type") == "state" and self._on_peer_speaking is not None:
            # A cross-team link carries no voice, so a "speaking" flag from the
            # other side would be a lie in the overlay - ignore it there.
            if not self._team_ok(link.address):
                return
            with contextlib.suppress(Exception):
                self._on_peer_speaking(link.peer_name, bool(message.get("speaking")))

    def _handle_chat(self, link: PeerLink, message: dict) -> None:
        """Receive a chat message from a peer.

        The sender's side is taken from what *we* learned about them, never from
        the message itself: a peer cannot label itself Radiant to slip text into
        the other team's channel. Team text is dropped unless it came from a
        teammate; the shared post-match channel is accepted from either side.
        """
        if self._on_chat is None:
            return
        channel = str(message.get("channel") or "")
        from_team = self._peer_teams.get(link.address, "unknown")
        if channel in ("radiant", "dire") and from_team != channel:
            logger.debug(
                "Отброшено командное сообщение от %s: сторона не совпадает", link.peer_name
            )
            return
        with contextlib.suppress(Exception):
            self._on_chat(
                link.peer_name,
                {
                    "channel": channel,
                    "text": str(message.get("text") or ""),
                    "id": str(message.get("id") or ""),
                    "ts": message.get("ts"),
                    "team": from_team,
                },
            )

    # ------------------------------------------------------------------
    # outgoing
    # ------------------------------------------------------------------

    def broadcast_voice(self, frame: bytes) -> int:
        """Encrypt and send a frame to every established teammate.

        Peers on the other side, or whose team is not known yet, are skipped: we
        never send our voice across the team boundary.
        """
        sent = 0
        for link in self._links.values():
            if not self._team_ok(link.address):
                continue
            if link.is_ready() and link.send_voice(frame):
                sent += 1
        return sent

    def broadcast_control(self, message: dict) -> int:
        sent = 0
        for link in self._links.values():
            if link.is_ready() and link.send_control(message):
                sent += 1
        return sent

    def send_chat(self, channel: str, text: str, *, message_id: str = "", ts=None) -> int:
        """Send a chat message to the peers that should receive it.

        Team text goes only to teammates; the shared post-match channel goes to
        every ready link, including the other side (text only - voice never
        follows this path). Returns how many peers received it.
        """
        message = {
            "type": "chat",
            "channel": channel,
            "text": text,
            "id": message_id,
            "ts": ts,
        }
        team_channel = channel in ("radiant", "dire")
        sent = 0
        for link in self._links.values():
            if not link.is_ready():
                continue
            if team_channel and not self._team_ok(link.address):
                continue
            if link.send_control(message):
                sent += 1
        return sent

    def set_peer_muted(self, peer_name: str, muted: bool) -> bool:
        link = self.link_for(peer_name)
        if link is None:
            return False
        link.set_muted(muted)
        return True

    def set_deafened(self, deafened: bool) -> None:
        for link in self._links.values():
            link.set_deafened(deafened)

    def mute_all(self, muted: bool) -> None:
        for link in self._links.values():
            link.set_muted(muted)

    # ------------------------------------------------------------------

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(_SWEEP_INTERVAL)
            await self._sweep_once()

    async def _sweep_once(self) -> None:
        """Drop half-open and silent links. Split out so tests can call it."""
        now = time.monotonic()
        for addr, link in list(self._links.items()):
            if link.state == LinkState.HANDSHAKING and now - link.created_at > HANDSHAKE_TIMEOUT:
                logger.info("Рукопожатие с %s не завершилось, соединение отброшено", link.peer_name)
                self._links.pop(addr, None)
                continue
            if link.is_stale(LINK_TIMEOUT, now):
                logger.info("Участник %s отключился (таймаут P2P)", link.peer_name)
                link.close()
                self._links.pop(addr, None)
                if self._on_peer_left is not None:
                    with contextlib.suppress(Exception):
                        self._on_peer_left(link.peer_name)

    def snapshot(self) -> dict:
        return {
            "match_id": self.match_id,
            "local_port": self.transport.local_address[1],
            "peers": [
                {
                    "name": link.peer_name,
                    "address": f"{link.address[0]}:{link.address[1]}",
                    "state": link.state,
                    "fingerprint": link.fingerprint,
                    "muted": link.muted,
                    "deafened": link.deafened,
                    "speaking": link.peer_speaking,
                }
                for link in self._links.values()
            ],
        }
