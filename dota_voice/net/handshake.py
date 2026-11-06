"""Authenticated, forward-secret handshake for peer voice links.

Design
------
A compact signed-Diffie-Hellman exchange, simple enough to reason about while
giving the voice link the properties it needs. Each message fits one datagram.

* **Hello** (initiator -> responder)::

      magic(4) | version(1) | flags(1) | match_hash(8) |
      initiator_pub(32) | nonce(16) | mac(32)

* **Confirm** (responder -> initiator)::

      magic(4) | version(1) | flags(1) | match_hash(8) |
      responder_pub(32) | mac(32)

Properties
----------
* **Mutual authentication of match membership.** Both sides derive a passphrase
  from the match id (:func:`~dota_voice.net.crypto.match_passphrase`). The hello
  MAC is ``HMAC(passphrase, transcript)`` over every field, so it authenticates
  the exact ephemeral key and nonce. The confirm MAC proves the responder
  derived the same keys; it is over a *different* transcript (labelled
  ``confirm``), so a passive attacker replaying a hello cannot forge it.
* **Forward secrecy.** Session keys come from an ephemeral X25519 exchange, never
  from the passphrase itself. If the passphrase leaks later, recorded traffic
  stays unreadable because the ephemeral private keys are gone.
* **Match privacy.** Only a truncated hash of the match id goes on the wire.
* **Replay / downgrade resistance.** A random per-handshake nonce is bound into
  the key schedule, so a replayed hello derives different keys; the version byte
  is bound into both MACs, so an attacker cannot silently downgrade.

Note on the passphrase: the match id is not a high-entropy secret, so this
authenticates "is in this match", not "is this exact account". Binding a link to
the GSI roster identity is a separate, caller-side check (see
:mod:`dota_voice.net.mesh`), and the two peers also compare a key fingerprint
after the handshake.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

from . import crypto
from .crypto import AuthenticationError, SessionCipher

MAGIC = b"DVS1"
VERSION = 1

FLAG_INITIATOR = 0x01

_HELLO_HEADER = struct.Struct("!4sBB8s")
_CONFIRM_HEADER = struct.Struct("!4sBB8s")
PUBLIC_SIZE = 32
NONCE_SIZE = 16
MAC_SIZE = 32

HELLO_SIZE = _HELLO_HEADER.size + PUBLIC_SIZE + NONCE_SIZE + MAC_SIZE
CONFIRM_SIZE = _CONFIRM_HEADER.size + PUBLIC_SIZE + MAC_SIZE


def match_hash(match_id: str) -> bytes:
    """Truncated, unkeyed hash of the match id for use on the wire."""
    return hashlib.sha256(match_id.encode("utf-8")).digest()[:8]


def fingerprint(public_key: bytes) -> str:
    """Short human-readable fingerprint of a public key."""
    digest = hashlib.sha256(public_key).hexdigest()
    return ":".join(digest[i : i + 4] for i in range(0, 16, 4))


@dataclass
class HandshakeResult:
    """Everything a peer needs after a successful handshake."""

    cipher: SessionCipher
    peer_public: bytes
    nonce: bytes
    initiator: bool

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.peer_public)


def _hello_transcript(digest: bytes, initiator_public: bytes, nonce: bytes) -> bytes:
    return b"hello" + bytes([VERSION]) + digest + initiator_public + nonce


def _confirm_transcript(
    digest: bytes, initiator_public: bytes, responder_public: bytes, nonce: bytes
) -> bytes:
    return b"confirm" + bytes([VERSION]) + digest + initiator_public + responder_public + nonce


class InitiatorHandshake:
    """Client side of the handshake."""

    def __init__(self, match_id: str, *, private_key=None) -> None:
        self.match_id = match_id
        self._passphrase = crypto.match_passphrase(match_id)
        self._private = private_key or crypto.generate_private_key()
        self.public = crypto.public_bytes(self._private)
        self._nonce = crypto.random_bytes(NONCE_SIZE)
        self._digest = match_hash(match_id)

    def build_hello(self) -> bytes:
        """Build the hello datagram."""
        header = _HELLO_HEADER.pack(MAGIC, VERSION, FLAG_INITIATOR, self._digest)
        body = header + self.public + self._nonce
        transcript = _hello_transcript(self._digest, self.public, self._nonce)
        return body + crypto.handshake_mac(self._passphrase, transcript)

    def finish(self, confirm: bytes) -> HandshakeResult:
        """Verify the responder's confirm and derive the session keys."""
        if len(confirm) != CONFIRM_SIZE:
            raise AuthenticationError("неверный размер подтверждения рукопожатия")
        magic, version, flags, digest = _CONFIRM_HEADER.unpack(confirm[: _CONFIRM_HEADER.size])
        if magic != MAGIC:
            raise AuthenticationError("чужой формат рукопожатия")
        if version != VERSION:
            raise AuthenticationError(f"неподдерживаемая версия рукопожатия: {version}")
        if flags & FLAG_INITIATOR:
            raise AuthenticationError("подтверждение не может быть инициирующим")
        if digest != self._digest:
            raise AuthenticationError("подтверждение относится к другому матчу")

        start = _CONFIRM_HEADER.size
        responder_public = confirm[start : start + PUBLIC_SIZE]
        mac = confirm[start + PUBLIC_SIZE :]
        transcript = _confirm_transcript(digest, self.public, responder_public, self._nonce)
        crypto.verify_handshake_mac(self._passphrase, transcript, mac)

        shared = crypto.x25519_shared(self._private, responder_public)
        send, recv, prefix = crypto.handshake_keys(
            self._passphrase, shared, self.public, responder_public, initiator=True
        )
        return HandshakeResult(
            cipher=SessionCipher(send, recv, prefix),
            peer_public=responder_public,
            nonce=self._nonce,
            initiator=True,
        )


class ResponderHandshake:
    """Server side of the handshake."""

    def __init__(self, match_id: str, *, private_key=None) -> None:
        self.match_id = match_id
        self._passphrase = crypto.match_passphrase(match_id)
        self._private = private_key or crypto.generate_private_key()
        self.public = crypto.public_bytes(self._private)
        self._digest = match_hash(match_id)

    def accept(self, hello: bytes) -> tuple[bytes, HandshakeResult]:
        """Verify a hello; return ``(confirm_datagram, result)``."""
        if len(hello) != HELLO_SIZE:
            raise AuthenticationError("неверный размер приветствия")
        magic, version, flags, digest = _HELLO_HEADER.unpack(hello[: _HELLO_HEADER.size])
        if magic != MAGIC:
            raise AuthenticationError("чужой формат рукопожатия")
        if version != VERSION:
            raise AuthenticationError(f"неподдерживаемая версия рукопожатия: {version}")
        if not flags & FLAG_INITIATOR:
            raise AuthenticationError("приветствие должно быть инициирующим")
        if digest != self._digest:
            raise AuthenticationError("приветствие относится к другому матчу")

        start = _HELLO_HEADER.size
        initiator_public = hello[start : start + PUBLIC_SIZE]
        nonce = hello[start + PUBLIC_SIZE : start + PUBLIC_SIZE + NONCE_SIZE]
        mac = hello[start + PUBLIC_SIZE + NONCE_SIZE :]

        transcript = _hello_transcript(digest, initiator_public, nonce)
        crypto.verify_handshake_mac(self._passphrase, transcript, mac)

        shared = crypto.x25519_shared(self._private, initiator_public)
        send, recv, prefix = crypto.handshake_keys(
            self._passphrase, shared, initiator_public, self.public, initiator=False
        )
        confirm_transcript = _confirm_transcript(digest, initiator_public, self.public, nonce)
        confirm_mac = crypto.handshake_mac(self._passphrase, confirm_transcript)
        confirm = _CONFIRM_HEADER.pack(MAGIC, VERSION, 0, digest) + self.public + confirm_mac
        result = HandshakeResult(
            cipher=SessionCipher(send, recv, prefix),
            peer_public=initiator_public,
            nonce=nonce,
            initiator=False,
        )
        return confirm, result


def is_handshake_packet(data: bytes) -> bool:
    """Whether a datagram looks like a handshake rather than a voice frame."""
    return len(data) >= _HELLO_HEADER.size and data[:4] == MAGIC
