"""Peer discovery keyed by ``match_id``.

The problem
-----------
Players in a match each run the app, but they have no shared contact list and we
deliberately do **not** want a central server (see :mod:`dota_voice.security.policy`).
They do share one piece of information: the ``match_id`` the game hands every
client through GSI. Discovery turns that shared key into a set of reachable peer
endpoints.

How it works here
-----------------
:class:`LanDiscovery` announces on a multicast group. Every datagram carries a
truncated hash of the match id (never the id itself) plus the sender's
endpoint. Peers in the same match recognise each other, peers in other matches
ignore the announcement. This is a real, working discovery layer for the common
case where the players are on the same LAN or the same host.

What this deliberately is not
-----------------------------
The original design sketch proposed a *global DHT* keyed by match id. Two honest
problems with that:

1. A DHT lookup for a key that is public and guessable is itself an attack
   surface - anyone can query "who is in match X" and enumerate participants.
   Publishing endpoints to a global network leaks who is playing with whom.
2. Bootstrapping a real DHT needs either a hard-coded bootstrap list (a central
   dependency in disguise) or a peer exchange, plus a NAT traversal story the
   DHT cannot provide on its own.

So discovery is split into two backends behind one interface:

* :class:`LanDiscovery` - implemented and default. No infrastructure, no leak
  beyond the local network.
* :class:`RendezvousDiscovery` - a thin, documented hook for a *self-hosted*
  rendezvous/relay that an operator can point the app at. It is opt-in and
  carries no endpoints anywhere by default.

For players not on a shared LAN, the practical answer is the operator's own
relay (already supported by the ``relay`` voice backend) or, if they accept the
trade-offs, the Steam SDR transport. Both keep the choice with the user.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import socket
import struct
import time
from dataclasses import dataclass, field

from .handshake import match_hash

logger = logging.getLogger(__name__)

MAGIC = b"DVD1"
# magic, version, match digest, team, port, name length
_HEADER = struct.Struct("!4sBB8sHH")
MAX_NAME = 48

TEAM_CODES = {"radiant": 0, "dire": 1, "unknown": 2}
TEAM_NAMES = {0: "radiant", 1: "dire", 2: "unknown"}

DEFAULT_GROUP = "239.255.42.99"
DEFAULT_PORT = 45678

ANNOUNCE_INTERVAL = 2.0
PEER_TTL = 12.0


@dataclass
class PeerRecord:
    """A discovered peer endpoint."""

    match_id: str
    name: str
    host: str
    port: int
    public_key: bytes = b""
    team: str = "unknown"
    seen_at: float = field(default_factory=time.monotonic)

    @property
    def address(self) -> tuple[str, int]:
        return (self.host, self.port)

    def is_stale(self, ttl: float = PEER_TTL, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        return now - self.seen_at > ttl


def encode_announce(record: PeerRecord) -> bytes:
    digest = match_hash(record.match_id)
    name = record.name.encode("utf-8")[:MAX_NAME]
    team = TEAM_CODES.get(record.team, 2)
    return _HEADER.pack(MAGIC, 1, team, digest, record.port, len(name)) + name


def decode_announce(data: bytes) -> tuple[bytes, str, int, str] | None:
    """Return ``(match_digest, team, port, name)`` or ``None`` if malformed."""
    if len(data) < _HEADER.size:
        return None
    magic, _version, team_code, digest, port, name_len = _HEADER.unpack(data[: _HEADER.size])
    if magic != MAGIC:
        return None
    name = data[_HEADER.size : _HEADER.size + name_len]
    if len(name) != name_len:
        return None
    return digest, TEAM_NAMES.get(team_code, "unknown"), port, name.decode("utf-8", errors="ignore")


class LanDiscovery:
    """Multicast discovery on the local network, keyed by match id."""

    def __init__(
        self,
        match_id: str,
        *,
        name: str = "",
        port: int = 0,
        group: str = DEFAULT_GROUP,
        group_port: int = DEFAULT_PORT,
        team: str = "unknown",
    ) -> None:
        self.match_id = match_id
        self.name = name
        self.port = port
        self.team = team
        self.group = group
        self.group_port = group_port
        self._digest = match_hash(match_id)
        self._peers: dict[tuple[str, int], PeerRecord] = {}
        self._transport: asyncio.DatagramTransport | None = None
        self._socket: socket.socket | None = None
        self._announce_task: asyncio.Task | None = None
        self._on_peer = None

    # ------------------------------------------------------------------

    def on_peer(self, callback) -> None:
        self._on_peer = callback

    @property
    def peers(self) -> list[PeerRecord]:
        return [p for p in self._peers.values() if not p.is_stale()]

    def known_peers(self) -> list[PeerRecord]:
        return list(self._peers.values())

    # ------------------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):  # pragma: no cover - platform dependent
            pass
        sock.bind(("", self.group_port))
        mreq = struct.pack("4s4s", socket.inet_aton(self.group), socket.inet_aton("0.0.0.0"))
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as exc:  # pragma: no cover - no multicast support
            logger.warning("Не удалось подключиться к multicast-группе: %s", exc)
            sock.close()
            raise
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setblocking(False)
        self._socket = sock
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _DiscoveryProtocol(self), sock=sock
        )
        self._transport = transport
        self._announce_task = asyncio.create_task(self._announce_loop())
        logger.info("Обнаружение по матчу включено (группа %s:%s)", self.group, self.group_port)

    async def stop(self) -> None:
        if self._announce_task is not None:
            self._announce_task.cancel()
            try:
                await self._announce_task
            except asyncio.CancelledError:
                pass
            self._announce_task = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self._socket = None

    # ------------------------------------------------------------------

    def announce(self) -> None:
        if self._transport is None:
            return
        record = PeerRecord(
            match_id=self.match_id,
            name=self.name,
            host="0.0.0.0",
            port=self.port,
            team=self.team,
        )
        try:
            self._transport.sendto(encode_announce(record), (self.group, self.group_port))
        except Exception as exc:  # pragma: no cover - transport failure
            logger.debug("Не удалось отправить объявление: %s", exc)

    async def _announce_loop(self) -> None:
        while True:
            self.announce()
            await asyncio.sleep(ANNOUNCE_INTERVAL)

    def handle_datagram(self, data: bytes, addr) -> None:
        decoded = decode_announce(data)
        if decoded is None:
            return
        digest, team, port, name = decoded
        if digest != self._digest:
            return
        host = addr[0]
        if port == 0:
            port = addr[1]
        key = (host, port)
        existing = self._peers.get(key)
        if existing is None:
            record = PeerRecord(
                match_id=self.match_id, name=name, host=host, port=port, team=team
            )
            self._peers[key] = record
            logger.info("Найден участник матча %s по адресу %s:%s", self.match_id, host, port)
            if self._on_peer is not None:
                try:
                    self._on_peer(record)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("Обработчик найденного участника упал: %s", exc)
        else:
            existing.seen_at = time.monotonic()
            existing.team = team
            if name:
                existing.name = name


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, discovery: LanDiscovery) -> None:
        self._discovery = discovery

    def datagram_received(self, data: bytes, addr) -> None:
        self._discovery.handle_datagram(data, addr)

    def error_received(self, exc) -> None:  # pragma: no cover
        logger.debug("Ошибка обнаружения: %s", exc)


class RendezvousDiscovery:
    """Opt-in hook for a self-hosted rendezvous service.

    Intentionally inert by default: constructing it without a URL, or with
    ``enabled=False``, does nothing and publishes nothing. It exists so an
    operator can wire their own meeting point without the project shipping a
    central server.
    """

    def __init__(self, match_id: str, *, url: str = "", enabled: bool = False) -> None:
        self.match_id = match_id
        self.url = url
        self.enabled = enabled and bool(url)
        self._peers: list[PeerRecord] = []

    async def start(self) -> None:
        if not self.enabled:
            logger.info("Встречный сервис не настроен — используется только локальное обнаружение")
        # A real implementation would POST an announce and poll/subscribe for
        # peers. Kept out of the default build on purpose.

    async def stop(self) -> None:
        return None

    @property
    def peers(self) -> list[PeerRecord]:
        return list(self._peers)


def match_fingerprint(match_id: str) -> str:
    """Stable short id for logs - never the raw match id."""
    return hashlib.sha256(match_id.encode("utf-8")).hexdigest()[:12]
