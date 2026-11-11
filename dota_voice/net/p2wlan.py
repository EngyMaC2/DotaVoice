"""P2WLAN: the local virtual network that carries team voice.

What this is
------------
P2WLAN gives the players of one match a private virtual network with its own
addresses - like a small Hamachi, except it is ours, it is open source, and the
control/relay servers are ones the operator hosts. The game tells us the match
id through GSI; we turn that id into a room, hand the invite to teammates, and
everyone ends up on the same virtual subnet with a virtual IP.

Why it is the default
---------------------
The earlier design carried voice over Steam Datagram Relay with AppID 480. That
works, but it leans on Steamworks with a development app id, which sits in a grey
zone of Valve's terms. P2WLAN touches Steam not at all: it is a plain virtual
network, so the account risk is zero. Steam SDR stays available as an explicit,
off-by-default fallback for the cases where P2WLAN cannot punch through NAT.

Design
------
:class:`P2WlanController` owns the room lifecycle and is **idempotent by
``match_id``**: creating a room for a match that already has one returns the
existing room instead of a second one. That is what keeps a game restart from
producing duplicate rooms, and it is why the room map is persisted - a restarted
app re-attaches to the same room.

The actual network work is behind :class:`P2WlanDriver`:

* :class:`LocalDriver` - allocates virtual IPs in-process and routes over the
  loopback address. This is the default, it needs no external binary, and it is
  what the tests exercise.
* :class:`CliDriver` - drives the real ``p2wlan`` command line.
* :class:`HttpDriver` - talks to a self-hosted control server.

Only the driver changes between "works in tests" and "works on the operator's
VPS"; the room logic above it does not.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

# Every match gets its own /24 out of a private range, derived from the match id.
_VIRTUAL_BASE = ipaddress.ip_network("10.42.0.0/16")
DEFAULT_CONTROL_PORT = 7800
DEFAULT_RELAY_PORT = 7801


class P2WlanError(RuntimeError):
    """Raised when a room cannot be created, joined or left."""


def virtual_subnet(match_id: str) -> ipaddress.IPv4Network:
    """The /24 a match's virtual network lives in.

    Derived from a hash of the match id so it is stable across restarts and
    identical on every teammate's machine, without anyone coordinating.
    """
    digest = hashlib.sha256(f"p2wlan:{match_id}".encode()).digest()
    index = int.from_bytes(digest[:2], "big") % (2 ** 8)
    return list(_VIRTUAL_BASE.subnets(new_prefix=24))[index]


def virtual_address(match_id: str, member: str) -> str:
    """The virtual IP a member gets inside its match's subnet.

    ``.1`` is the gateway (the host), so members start at ``.2``.
    """
    subnet = virtual_subnet(match_id)
    digest = hashlib.sha256(f"{match_id}:{member}".encode()).digest()
    offset = 2 + int.from_bytes(digest[:2], "big") % (subnet.num_addresses - 3)
    return str(subnet.network_address + offset)


def invite_key(match_id: str, team: str = "") -> str:
    """The short, shareable key teammates paste to join the room."""
    digest = hashlib.sha256(f"invite:{match_id}".encode()).hexdigest()[:12]
    suffix = f"-{team}" if team else ""
    return f"p2w-{digest}{suffix}"


@dataclass
class RoomMember:
    """One player on the virtual network."""

    member: str
    virtual_ip: str
    team: str = "unknown"
    joined_at: float = field(default_factory=time.time)
    online: bool = True


@dataclass
class P2WlanRoom:
    """A virtual network for exactly one match."""

    match_id: str
    team: str = "unknown"
    invite_key: str = ""
    subnet: str = ""
    gateway: str = ""
    members: dict[str, RoomMember] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    joined: bool = False
    relayed: bool = False
    driver: str = "local"

    @property
    def room_id(self) -> str:
        """Room id is the match id: one room per match, by construction."""
        return self.match_id

    def add_member(self, member: str, team: str = "unknown") -> RoomMember:
        existing = self.members.get(member)
        if existing is not None:
            existing.online = True
            if team and team != "unknown":
                existing.team = team
            return existing
        entry = RoomMember(
            member=member,
            virtual_ip=virtual_address(self.match_id, member),
            team=team or "unknown",
        )
        self.members[member] = entry
        return entry

    def remove_member(self, member: str) -> None:
        self.members.pop(member, None)

    def address_for(self, member: str) -> str | None:
        entry = self.members.get(member)
        return entry.virtual_ip if entry else None

    def as_dict(self) -> dict:
        data = asdict(self)
        data["room_id"] = self.room_id
        return data


class P2WlanDriver(Protocol):
    """The network side of a room: create it, join it, leave it."""

    name: str

    async def create(self, room: P2WlanRoom) -> None: ...

    async def join(self, room: P2WlanRoom, key: str) -> None: ...

    async def leave(self, room: P2WlanRoom) -> None: ...


class LocalDriver:
    """In-process driver: allocates virtual IPs, routes over loopback.

    No external binary, no privileges, no configuration. This is what makes the
    P2WLAN path testable and what lets a single machine run several members for a
    demo. The room's virtual addresses are real, stable identifiers; the traffic
    between them goes over the local interface.
    """

    name = "local"

    async def create(self, room: P2WlanRoom) -> None:
        room.joined = True

    async def join(self, room: P2WlanRoom, key: str) -> None:
        room.joined = True

    async def leave(self, room: P2WlanRoom) -> None:
        room.joined = False
        room.members.clear()


class CliDriver:
    """Drives the real ``p2wlan`` command line.

    Every call is best-effort with a timeout, and failures raise
    :class:`P2WlanError` so the controller can fall back rather than hang.
    """

    name = "cli"

    def __init__(self, binary: str = "p2wlan", *, timeout: float = 15.0) -> None:
        self.binary = binary
        self.timeout = timeout

    @staticmethod
    def available(binary: str = "p2wlan") -> bool:
        return shutil.which(binary) is not None

    def _run(self, args: list[str]) -> str:
        try:
            result = subprocess.run(
                [self.binary, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise P2WlanError(f"не удалось запустить {self.binary}: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise P2WlanError(f"{self.binary} {' '.join(args)}: {detail}")
        return result.stdout

    async def create(self, room: P2WlanRoom) -> None:
        out = await asyncio.get_running_loop().run_in_executor(
            None, self._run, ["room", "create", room.room_id, "--subnet", room.subnet]
        )
        room.joined = True
        key = _extract_invite(out)
        if key:
            room.invite_key = key

    async def join(self, room: P2WlanRoom, key: str) -> None:
        await asyncio.get_running_loop().run_in_executor(
            None, self._run, ["room", "connect", key]
        )
        room.joined = True

    async def leave(self, room: P2WlanRoom) -> None:
        await asyncio.get_running_loop().run_in_executor(
            None, self._run, ["room", "leave", room.room_id]
        )
        room.joined = False


class HttpDriver:
    """Talks to a self-hosted P2WLAN control server.

    The operator runs the control and relay servers on their own host (a small
    free VPS is plenty); the app only needs the base URL.
    """

    name = "http"

    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def _post(self, path: str, body: dict) -> dict:
        import httpx

        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=body)
                response.raise_for_status()
                return response.json()
        except Exception as exc:
            raise P2WlanError(f"control-сервер недоступен ({url}): {exc}") from exc

    async def create(self, room: P2WlanRoom) -> None:
        data = await self._post(
            "/rooms", {"room_id": room.room_id, "subnet": room.subnet, "team": room.team}
        )
        room.joined = True
        room.invite_key = data.get("invite_key") or room.invite_key

    async def join(self, room: P2WlanRoom, key: str) -> None:
        await self._post("/rooms/join", {"invite_key": key})
        room.joined = True

    async def leave(self, room: P2WlanRoom) -> None:
        await self._post("/rooms/leave", {"room_id": room.room_id})
        room.joined = False


def _extract_invite(output: str) -> str:
    """Pull an invite key out of CLI output, tolerating extra chatter."""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("p2w-"):
            return stripped
        if "invite" in stripped.lower() and ":" in stripped:
            return stripped.split(":", 1)[1].strip()
    return ""


class P2WlanController:
    """Room lifecycle for every match, idempotent by ``match_id``.

    Persisting the room map means a restarted app rejoins the room it already
    had instead of creating a new one, which is the same guarantee the session
    layer gives for voice channels.
    """

    def __init__(
        self,
        *,
        driver: P2WlanDriver | None = None,
        state_path: Path | None = None,
        display_name: str = "",
        team: str = "unknown",
        fallback_to_sdr: bool = False,
    ) -> None:
        self._driver = driver or LocalDriver()
        self._state_path = state_path
        self._display_name = display_name
        self._team = team
        self._fallback_to_sdr = fallback_to_sdr
        self._rooms: dict[str, P2WlanRoom] = {}
        self._lock = asyncio.Lock()
        self.metrics = {"rooms_created": 0, "rooms_reused": 0, "rooms_left": 0}
        self._load()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Не удалось прочитать карту комнат P2WLAN: %s", exc)
            return
        for match_id, data in (raw.get("rooms") or {}).items():
            members = {
                key: RoomMember(**value) for key, value in (data.get("members") or {}).items()
            }
            self._rooms[match_id] = P2WlanRoom(
                match_id=match_id,
                team=data.get("team", "unknown"),
                invite_key=data.get("invite_key", ""),
                subnet=data.get("subnet", ""),
                gateway=data.get("gateway", ""),
                members=members,
                created_at=data.get("created_at", time.time()),
                joined=bool(data.get("joined")),
                relayed=bool(data.get("relayed")),
                driver=data.get("driver", self._driver.name),
            )

    def _save(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"rooms": {mid: room.as_dict() for mid, room in self._rooms.items()}}
            self._state_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:  # pragma: no cover - disk failure
            logger.warning("Не удалось сохранить карту комнат P2WLAN: %s", exc)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def room(self, match_id: str) -> P2WlanRoom | None:
        return self._rooms.get(match_id)

    @property
    def rooms(self) -> dict[str, P2WlanRoom]:
        return dict(self._rooms)

    async def create_room(self, match_id: str, team: str = "unknown") -> P2WlanRoom:
        """Create (or return) the room for a match. Never duplicates."""
        async with self._lock:
            existing = self._rooms.get(match_id)
            if existing is not None:
                if team and team != "unknown":
                    existing.team = team
                # A room restored from the state file (or detached at shutdown)
                # has no live network yet. Re-attach with join when we know the
                # key, so a restart actually rejoins the same room instead of
                # forking a second one.
                if not existing.joined:
                    try:
                        if existing.invite_key:
                            await self._driver.join(existing, existing.invite_key)
                        else:
                            await self._driver.create(existing)
                    except P2WlanError as exc:
                        if not self._fallback_to_sdr:
                            raise
                        logger.warning(
                            "P2WLAN не смог восстановить комнату (%s); запасной путь", exc
                        )
                        existing.relayed = True
                existing.joined = True
                self.metrics["rooms_reused"] += 1
                self._save()
                return existing

            subnet = virtual_subnet(match_id)
            room = P2WlanRoom(
                match_id=match_id,
                team=team or self._team,
                invite_key=invite_key(match_id, team if team != "unknown" else ""),
                subnet=str(subnet),
                gateway=str(subnet.network_address + 1),
                driver=self._driver.name,
            )
            room.add_member(self._display_name or "host", room.team)
            try:
                await self._driver.create(room)
            except P2WlanError as exc:
                if not self._fallback_to_sdr:
                    raise
                logger.warning(
                    "P2WLAN не смог создать комнату (%s); включаю запасной путь", exc
                )
                room.relayed = True
            self._rooms[match_id] = room
            self.metrics["rooms_created"] += 1
            self._save()
            logger.info(
                "Комната P2WLAN %s создана (подсеть %s, приглашение %s)",
                match_id,
                room.subnet,
                room.invite_key,
            )
            return room

    async def join_room(self, key: str, *, match_id: str | None = None) -> P2WlanRoom:
        """Join a room by invite key.

        When the key belongs to a match we already know, we reuse that room; the
        room id is always derived from the match id, so joining cannot fork a
        second network for the same match.
        """
        target_id = match_id or self._match_for_key(key)
        if target_id is None:
            raise P2WlanError("неизвестное приглашение: укажите match_id или создайте комнату")

        async with self._lock:
            room = self._rooms.get(target_id)
            if room is None:
                subnet = virtual_subnet(target_id)
                room = P2WlanRoom(
                    match_id=target_id,
                    team=self._team,
                    invite_key=key,
                    subnet=str(subnet),
                    gateway=str(subnet.network_address + 1),
                    driver=self._driver.name,
                )
                self._rooms[target_id] = room
                self.metrics["rooms_created"] += 1
            room.add_member(self._display_name or "host", self._team)
            try:
                await self._driver.join(room, key)
            except P2WlanError as exc:
                if not self._fallback_to_sdr:
                    raise
                logger.warning("P2WLAN не смог подключиться (%s); включаю запасной путь", exc)
                room.relayed = True
            self._save()
            return room

    def _match_for_key(self, key: str) -> str | None:
        for match_id, room in self._rooms.items():
            if room.invite_key == key:
                return match_id
        return None

    async def leave_room(self, match_id: str) -> bool:
        """Tear a room down at the end of a match. Returns whether it existed."""
        async with self._lock:
            room = self._rooms.pop(match_id, None)
            if room is None:
                return False
            try:
                await self._driver.leave(room)
            except P2WlanError as exc:  # pragma: no cover - driver failure
                logger.warning("P2WLAN не смог корректно выйти из комнаты: %s", exc)
            self.metrics["rooms_left"] += 1
            self._save()
            logger.info("Комната P2WLAN %s закрыта", match_id)
            return True

    async def close(self) -> None:
        """Detach from the network without forgetting our rooms.

        A clean exit must not wipe the state file: the room is the player's
        identity in the match, and forgetting it would make the next launch
        create a second network for a match that is still running.
        """
        for match_id in list(self._rooms):
            await self.detach_room(match_id)

    async def detach_room(self, match_id: str) -> bool:
        """Leave the network for a match but keep its room on record."""
        async with self._lock:
            room = self._rooms.get(match_id)
            if room is None:
                return False
            members = dict(room.members)
            try:
                await self._driver.leave(room)
            except P2WlanError as exc:  # pragma: no cover - driver failure
                logger.warning("P2WLAN не смог корректно выйти из комнаты: %s", exc)
            # Drivers may drop the roster on leave; the file keeps it so the
            # next launch restores the same members and addresses.
            room.members = members
            room.joined = False
            self._save()
            return True

    # ------------------------------------------------------------------
    # membership
    # ------------------------------------------------------------------

    def add_member(self, match_id: str, member: str, team: str = "unknown") -> RoomMember | None:
        room = self._rooms.get(match_id)
        if room is None:
            return None
        entry = room.add_member(member, team)
        self._save()
        return entry

    def remove_member(self, match_id: str, member: str) -> None:
        room = self._rooms.get(match_id)
        if room is None:
            return
        room.remove_member(member)
        self._save()

    def snapshot(self) -> dict:
        return {
            "driver": self._driver.name,
            "metrics": dict(self.metrics),
            "rooms": {
                match_id: {
                    **room.as_dict(),
                    "members": {k: asdict(v) for k, v in room.members.items()},
                }
                for match_id, room in self._rooms.items()
            },
        }
