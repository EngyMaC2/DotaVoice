"""Pluggable transports for peer voice traffic.

A transport only has to move opaque datagrams between two endpoints; all
encryption and framing lives above it (see :mod:`dota_voice.net.link`). Two
implementations ship here:

* :class:`UdpTransport` - a real ``asyncio`` UDP socket. Works on a LAN and
  anywhere the peers can reach each other directly.
* :class:`SteamSdrTransport` - a *stub* for Steam Datagram Relay.

Why the Steam transport is a stub
---------------------------------
SDR is genuinely attractive: Valve's relays do NAT traversal, hide IP addresses
and encrypt the transport. But using it requires the Steamworks SDK, an app id
and a per-user Steam ticket, and it binds the project to Steam's terms. The
research is clear enough that this should be an *operator decision*, not
something the app does silently:

* it needs a real Steamworks app id (Spacewar, 480, is only for development);
* Valve can change access to SDR at any time;
* the security value is real but so is the account/terms risk.

So the code path exists and is documented, but it refuses to run until an
operator explicitly enables it and provides a Steamworks build. Nothing in the
default configuration touches Steam. That keeps the "no account risk by default"
promise in :mod:`dota_voice.security.policy`.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable
from typing import Protocol

logger = logging.getLogger(__name__)

# A datagram handler receives raw bytes and the sender address.
DatagramHandler = Callable[[bytes, tuple[str, int]], None]


class Transport(Protocol):
    """Moves opaque datagrams; owns a local endpoint."""

    @property
    def local_address(self) -> tuple[str, int]: ...

    def send(self, data: bytes, addr: tuple[str, int]) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, transport: "UdpTransport") -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self._transport._dispatch(data, addr)

    def error_received(self, exc) -> None:  # pragma: no cover - transport error
        logger.debug("Ошибка UDP-транспорта: %s", exc)


class UdpTransport:
    """A plain UDP socket. The default, fully local transport."""

    def __init__(self, host: str = "0.0.0.0", port: int = 0) -> None:
        self._host = host
        self._port = port
        self._transport: asyncio.DatagramTransport | None = None
        self._handlers: list[DatagramHandler] = []
        self._address: tuple[str, int] = (host, port)

    @property
    def local_address(self) -> tuple[str, int]:
        return self._address

    @property
    def port(self) -> int:
        return self._address[1]

    def on_datagram(self, handler: DatagramHandler) -> None:
        self._handlers.append(handler)

    def _dispatch(self, data: bytes, addr) -> None:
        for handler in list(self._handlers):
            try:
                handler(data, addr)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Обработчик датаграммы упал: %s", exc)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _UdpProtocol(self),
            local_addr=(self._host, self._port),
            family=socket.AF_INET,
        )
        self._transport = transport
        self._address = transport.get_extra_info("sockname")[:2]
        logger.info("P2P-транспорт слушает %s:%s (UDP)", *self._address)

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def send(self, data: bytes, addr: tuple[str, int]) -> None:
        if self._transport is None:
            return
        try:
            self._transport.sendto(data, addr)
        except Exception as exc:  # pragma: no cover - transport failure
            logger.debug("Не удалось отправить датаграмму %s: %s", addr, exc)


class SdrUnavailable(RuntimeError):
    """Raised when the Steam SDR transport is used without opting in."""


class SteamSdrTransport:
    """Steam Datagram Relay transport - requires an explicit opt-in.

    Construction and use fail unless the operator has enabled SDR *and* a
    Steamworks build is available. See the module docstring for the reasoning.
    """

    def __init__(self, app_id: int, *, enabled: bool = False) -> None:
        if not enabled:
            raise SdrUnavailable(
                "Транспорт Steam SDR отключён. Включите его осознанно "
                "(DOTAVOICE_P2P_STEAM_SDR=1) и соберите приложение с Steamworks SDK."
            )
        try:  # pragma: no cover - depends on a Steamworks build
            import importlib.util

            if importlib.util.find_spec("steamworks") is None:
                raise ImportError("steamworks")
        except Exception as exc:  # pragma: no cover
            raise SdrUnavailable(
                "Steamworks SDK не найден. Соберите приложение со Steamworks "
                "и укажите app id, разрешённый для SDR."
            ) from exc
        self.app_id = app_id
        self._address: tuple[str, int] = ("0.0.0.0", 0)

    @property
    def local_address(self) -> tuple[str, int]:
        return self._address

    async def start(self) -> None:  # pragma: no cover - needs Steam runtime
        raise SdrUnavailable("Steam SDR требует запущенного клиента Steam")

    async def stop(self) -> None:  # pragma: no cover
        return None

    def send(self, data: bytes, addr: tuple[str, int]) -> None:  # pragma: no cover
        raise SdrUnavailable("Steam SDR не инициализирован")
