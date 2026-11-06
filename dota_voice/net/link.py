"""A single encrypted peer link.

A link owns one remote endpoint and speaks three kinds of datagram:

* **handshake** - see :mod:`dota_voice.net.handshake`;
* **voice** - ``DVF1 | counter(8) | ciphertext``, an encrypted Opus frame;
* **control** - ``DVC1 | counter(8) | ciphertext``, an encrypted JSON message
  (mute/deafen/speaking state).

Local mute lives here, and that is a deliberate choice: muting a peer means we
simply stop sending them our audio and stop playing theirs. No third party is
involved, nothing is signalled to a server, and no other peer is affected -
exactly the "each player decides for themselves" model.
"""

from __future__ import annotations

import json
import logging
import struct
import time

from .crypto import AuthenticationError, ReplayError, SessionCipher
from .handshake import (
    CONFIRM_SIZE,
    InitiatorHandshake,
    ResponderHandshake,
    is_handshake_packet,
)
from .transport import Transport

logger = logging.getLogger(__name__)

FRAME_MAGIC = b"DVF1"
CONTROL_MAGIC = b"DVC1"
_FRAME_HEADER = struct.Struct("!4sQ")


class LinkState:
    NEW = "new"
    HANDSHAKING = "handshaking"
    ESTABLISHED = "established"
    FAILED = "failed"


class PeerLink:
    """Encrypted link to one peer. Not thread-safe; drive it from one loop."""

    def __init__(
        self,
        match_id: str,
        peer_name: str,
        address: tuple[str, int],
        transport: Transport,
        *,
        local_name: str = "",
        initiator: bool = True,
        on_voice=None,
        on_control=None,
        on_established=None,
    ) -> None:
        self.match_id = match_id
        self.peer_name = peer_name
        self.address = address
        self.transport = transport
        self.local_name = local_name
        self.initiator = initiator
        self.state = LinkState.NEW
        self.cipher: SessionCipher | None = None
        self.fingerprint = ""
        self.created_at = time.monotonic()
        self.last_activity = time.monotonic()
        # Local, one-sided controls.
        self.muted = False  # do not send them our audio, do not play theirs
        self.deafened = False
        self.peer_speaking = False
        self.peer_muted = False

        self._on_voice = on_voice
        self._on_control = on_control
        self._on_established = on_established
        self._handshake = (
            InitiatorHandshake(match_id) if initiator else ResponderHandshake(match_id)
        )
        self._hello_sent_at: float | None = None

    # ------------------------------------------------------------------
    # handshake
    # ------------------------------------------------------------------

    def start_handshake(self) -> bytes:
        """Return the hello datagram to send (initiator only)."""
        if not isinstance(self._handshake, InitiatorHandshake):
            raise RuntimeError("рукопожатие начинает только инициатор")
        self.state = LinkState.HANDSHAKING
        self._hello_sent_at = time.monotonic()
        return self._handshake.build_hello()

    def handle_datagram(self, data: bytes) -> None:
        """Process one datagram from the peer."""
        if is_handshake_packet(data):
            self._handle_handshake(data)
            return
        if self.state != LinkState.ESTABLISHED or self.cipher is None:
            return
        if data[:4] == FRAME_MAGIC:
            self._handle_voice(data)
        elif data[:4] == CONTROL_MAGIC:
            self._handle_control(data)

    def _handle_handshake(self, data: bytes) -> None:
        try:
            if isinstance(self._handshake, ResponderHandshake):
                confirm, result = self._handshake.accept(data)
                self._activate(result)
                self._send_raw(confirm)
            else:
                if len(data) != CONFIRM_SIZE:
                    return
                result = self._handshake.finish(data)
                self._activate(result)
        except AuthenticationError as exc:
            logger.warning("Рукопожатие с %s отклонено: %s", self.address, exc)
            self.state = LinkState.FAILED

    def _activate(self, result) -> None:
        self.cipher = result.cipher
        self.fingerprint = result.fingerprint
        self.state = LinkState.ESTABLISHED
        self.last_activity = time.monotonic()
        logger.info(
            "Защищённый канал с %s установлен (отпечаток %s)",
            self.address,
            self.fingerprint,
        )
        if self._on_established is not None:
            self._on_established(self)

    # ------------------------------------------------------------------
    # data
    # ------------------------------------------------------------------

    def send_voice(self, frame: bytes) -> bool:
        """Encrypt and send one audio frame. Returns False if muted/not ready."""
        if self.cipher is None or self.muted:
            return False
        counter, ciphertext = self.cipher.encrypt(frame)
        self._send_raw(_FRAME_HEADER.pack(FRAME_MAGIC, counter) + ciphertext)
        return True

    def send_control(self, message: dict) -> bool:
        if self.cipher is None:
            return False
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        counter, ciphertext = self.cipher.encrypt(payload)
        self._send_raw(_FRAME_HEADER.pack(CONTROL_MAGIC, counter) + ciphertext)
        return True

    def _handle_voice(self, data: bytes) -> None:
        _magic, counter = _FRAME_HEADER.unpack(data[: _FRAME_HEADER.size])
        try:
            frame = self.cipher.decrypt(counter, data[_FRAME_HEADER.size :])
        except (AuthenticationError, ReplayError) as exc:
            logger.debug("Отброшен голосовой пакет от %s: %s", self.address, exc)
            return
        self.last_activity = time.monotonic()
        if self.muted or self.deafened:
            return
        if self._on_voice is not None:
            self._on_voice(self, frame)

    def _handle_control(self, data: bytes) -> None:
        _magic, counter = _FRAME_HEADER.unpack(data[: _FRAME_HEADER.size])
        try:
            payload = self.cipher.decrypt(counter, data[_FRAME_HEADER.size :])
        except (AuthenticationError, ReplayError) as exc:
            logger.debug("Отброшено управляющее сообщение от %s: %s", self.address, exc)
            return
        self.last_activity = time.monotonic()
        try:
            message = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if message.get("type") == "state":
            self.peer_speaking = bool(message.get("speaking"))
            self.peer_muted = bool(message.get("muted"))
        if self._on_control is not None:
            self._on_control(self, message)

    # ------------------------------------------------------------------
    # local controls
    # ------------------------------------------------------------------

    def set_muted(self, muted: bool) -> None:
        """Local mute: stop sending to and playing this peer."""
        self.muted = muted

    def set_deafened(self, deafened: bool) -> None:
        self.deafened = deafened

    def is_ready(self) -> bool:
        return self.state == LinkState.ESTABLISHED

    def is_stale(self, timeout: float = 30.0, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        return now - self.last_activity > timeout

    def _send_raw(self, data: bytes) -> None:
        try:
            self.transport.send(data, self.address)
        except Exception as exc:  # pragma: no cover - transport failure
            logger.debug("Не удалось отправить на %s: %s", self.address, exc)

    def close(self) -> None:
        if self.cipher is not None:
            self.send_control({"type": "bye"})
        self.state = LinkState.NEW
        self.cipher = None
