"""Cryptography for peer-to-peer voice links.

Threat model
------------
Voice travels directly between players, so the transport is *not* trusted: an
observer can read, drop, reorder and replay datagrams. The goals are

* **confidentiality** - only the two peers can hear a call;
* **integrity** - a modified packet is rejected, never decoded;
* **authentication** - a peer must prove it is in the same match;
* **forward secrecy** - ephemeral keys mean a later key leak does not decrypt
  past traffic;
* **replay resistance** - a replayed packet is dropped.

Primitives
----------
* ``X25519`` for the ephemeral key exchange.
* ``HKDF-SHA256`` to derive directional keys and the match passphrase.
* ``XChaCha20-Poly1305`` (AEAD) for every voice packet.

Why XChaCha20 rather than plain ChaCha20-Poly1305? Its 24-byte nonce is large
enough that a random per-direction prefix plus a 64-bit counter can never
collide, so the AEAD subkey is derived once per direction (see
:class:`SessionCipher`) instead of once per packet, keeping the hot path inside
the fast C implementation.

No libsodium dependency: the HChaCha20 step is a small pure Python function and
the AEAD comes from ``cryptography``.
"""

from __future__ import annotations

import hmac
import os
import struct
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

__all__ = [
    "AuthenticationError",
    "CryptoError",
    "ReplayError",
    "ReplayWindow",
    "SessionCipher",
    "derive_key",
    "generate_private_key",
    "hchacha20",
    "handshake_keys",
    "handshake_mac",
    "match_passphrase",
    "private_from_bytes",
    "public_bytes",
    "public_from_bytes",
    "random_bytes",
    "verify_handshake_mac",
    "x25519_shared",
]

KEY_SIZE = 32
NONCE_PREFIX_SIZE = 16
MAC_SIZE = 16
# XChaCha20-Poly1305 nonce = 16-byte prefix + 8-byte counter.
NONCE_SIZE = 24

# Domain separation labels so one secret is never reused for two purposes.
_LABEL_PSK = b"dotavoice/v1/match-passphrase"
_LABEL_HANDSHAKE = b"dotavoice/v1/handshake"


class CryptoError(Exception):
    """Base class for every failure raised by this module."""


class AuthenticationError(CryptoError):
    """A packet or handshake could not be authenticated."""


class ReplayError(CryptoError):
    """A packet arrived with a counter that was already seen or too old."""


# ---------------------------------------------------------------------------
# HChaCha20 (draft-irtf-cfrg-xchacha, section 2.2)
# ---------------------------------------------------------------------------

_SIGMA = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)


def _rotl32(value: int, count: int) -> int:
    return ((value << count) | (value >> (32 - count))) & 0xFFFFFFFF


def _quarter_round(state: list[int], a: int, b: int, c: int, d: int) -> None:
    state[a] = (state[a] + state[b]) & 0xFFFFFFFF
    state[d] = _rotl32(state[d] ^ state[a], 16)
    state[c] = (state[c] + state[d]) & 0xFFFFFFFF
    state[b] = _rotl32(state[b] ^ state[c], 12)
    state[a] = (state[a] + state[b]) & 0xFFFFFFFF
    state[d] = _rotl32(state[d] ^ state[a], 8)
    state[c] = (state[c] + state[d]) & 0xFFFFFFFF
    state[b] = _rotl32(state[b] ^ state[c], 7)


def hchacha20(key: bytes, nonce: bytes) -> bytes:
    """Derive a 32-byte subkey from a key and a 16-byte nonce.

    Unlike ChaCha20's block function the initial state is *not* added back at the
    end; the output is words 0..3 followed by words 12..15.
    """
    if len(key) != KEY_SIZE:
        raise ValueError("ключ HChaCha20 должен быть 32 байта")
    if len(nonce) != 16:
        raise ValueError("nonce HChaCha20 должен быть 16 байт")

    state = list(_SIGMA) + list(struct.unpack("<8I", key)) + list(struct.unpack("<4I", nonce))

    for _ in range(10):
        _quarter_round(state, 0, 4, 8, 12)
        _quarter_round(state, 1, 5, 9, 13)
        _quarter_round(state, 2, 6, 10, 14)
        _quarter_round(state, 3, 7, 11, 15)
        _quarter_round(state, 0, 5, 10, 15)
        _quarter_round(state, 1, 6, 11, 12)
        _quarter_round(state, 2, 7, 8, 13)
        _quarter_round(state, 3, 4, 9, 14)

    return struct.pack("<8I", *(state[0:4] + state[12:16]))


# ---------------------------------------------------------------------------
# Key material
# ---------------------------------------------------------------------------


def generate_private_key() -> X25519PrivateKey:
    """A fresh ephemeral X25519 private key."""
    return X25519PrivateKey.generate()


def public_bytes(key: X25519PrivateKey | X25519PublicKey) -> bytes:
    """Raw 32-byte public key."""
    public = key.public_key() if isinstance(key, X25519PrivateKey) else key
    return public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def private_from_bytes(raw: bytes) -> X25519PrivateKey:
    return X25519PrivateKey.from_private_bytes(raw)


def public_from_bytes(raw: bytes) -> X25519PublicKey:
    if len(raw) != KEY_SIZE:
        raise ValueError("публичный ключ X25519 должен быть 32 байта")
    return X25519PublicKey.from_public_bytes(raw)


def x25519_shared(private: X25519PrivateKey, peer_public: bytes) -> bytes:
    """Raw ECDH output; both sides compute the same value."""
    return private.exchange(public_from_bytes(peer_public))


def derive_key(
    secret: bytes,
    *,
    salt: bytes = b"",
    info: bytes = b"",
    length: int = KEY_SIZE,
) -> bytes:
    """HKDF-SHA256 key derivation."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=salt or None,
        info=info,
    ).derive(secret)


def match_passphrase(match_id: str, *, extra: bytes = b"") -> bytes:
    """Derive the shared passphrase that proves membership in a match.

    The match id comes from Game State Integration, which the game only sends to
    a client that is actually in (or spectating) the match. ``extra`` binds the
    passphrase to an additional value, for example a session salt published with
    the discovery record.

    The match id is *not* a high-entropy secret - anyone who learns it can derive
    the same passphrase. It authenticates "is in this match", not "is this
    specific person"; peer identity is checked against the GSI roster separately.
    """
    if not match_id:
        raise ValueError("match_id обязателен для парольной фразы матча")
    return derive_key(match_id.encode("utf-8"), salt=extra or b"", info=_LABEL_PSK)


def handshake_mac(passphrase: bytes, transcript: bytes) -> bytes:
    """Keyed MAC used to prove knowledge of the match passphrase."""
    return hmac.new(passphrase, transcript, "sha256").digest()


def verify_handshake_mac(passphrase: bytes, transcript: bytes, mac: bytes) -> None:
    if not hmac.compare_digest(handshake_mac(passphrase, transcript), mac):
        raise AuthenticationError("рукопожатие не подтверждено: неверный ключ матча")


def handshake_keys(
    passphrase: bytes,
    shared_secret: bytes,
    initiator_public: bytes,
    responder_public: bytes,
    *,
    initiator: bool,
) -> tuple[bytes, bytes, bytes]:
    """Derive ``(send_key, recv_key, nonce_prefix)`` for one handshake.

    Both peers feed the same transcript in, so both end up with a matched pair of
    directional keys; ``initiator`` only decides which one is "send".
    """
    transcript = initiator_public + responder_public
    material = derive_key(
        shared_secret,
        salt=passphrase,
        info=_LABEL_HANDSHAKE + transcript,
        length=3 * KEY_SIZE,
    )
    first = material[:KEY_SIZE]
    second = material[KEY_SIZE : 2 * KEY_SIZE]
    prefix = material[2 * KEY_SIZE :]
    send, recv = (first, second) if initiator else (second, first)
    return send, recv, prefix[:NONCE_PREFIX_SIZE]


# ---------------------------------------------------------------------------
# AEAD session cipher
# ---------------------------------------------------------------------------


@dataclass
class ReplayWindow:
    """Sliding-window replay filter over a monotonic packet counter.

    Accepts a packet if its counter has not been seen, tolerating reordering up
    to ``size`` packets. Anything older than the window is rejected, which is the
    right trade-off for realtime voice: a packet that late is useless anyway.
    """

    size: int = 1024
    highest: int = -1
    _seen: set[int] = field(default_factory=set)

    def check(self, counter: int) -> None:
        if counter < 0:
            raise ReplayError("отрицательный счётчик пакета")
        if counter + self.size <= self.highest:
            raise ReplayError(f"пакет {counter} вне окна (устарел)")
        if counter in self._seen:
            raise ReplayError(f"повтор пакета {counter}")
        self._seen.add(counter)
        if counter > self.highest:
            self.highest = counter
        if len(self._seen) > self.size * 2:
            cutoff = self.highest - self.size
            self._seen = {c for c in self._seen if c > cutoff}


class SessionCipher:
    """One AEAD cipher for a peer link, used in both directions.

    The nonce is ``prefix || counter`` where ``prefix`` is random per session and
    direction and ``counter`` is a 64-bit big-endian counter that never repeats.
    Because the prefix is fixed, the HChaCha20 subkey is computed once here and
    reused for every packet.
    """

    def __init__(
        self,
        send_key: bytes,
        recv_key: bytes,
        nonce_prefix: bytes,
        *,
        context: bytes = b"",
        window_size: int = 1024,
    ) -> None:
        if len(nonce_prefix) != NONCE_PREFIX_SIZE:
            raise ValueError("префикс nonce должен быть 16 байт")
        self._context = context
        self._send = ChaCha20Poly1305(hchacha20(send_key, nonce_prefix))
        self._recv = ChaCha20Poly1305(hchacha20(recv_key, nonce_prefix))
        self._prefix = nonce_prefix
        self._send_counter = 0
        self._replay = ReplayWindow(size=window_size)

    # -- helpers --------------------------------------------------------

    def _nonce(self, counter: int) -> bytes:
        # XChaCha construction: the 24-byte nonce is split into the 16-byte
        # prefix (used to derive the subkey in __init__) and the remaining 8
        # bytes, which become the IETF ChaCha20-Poly1305 nonce as
        # ``0x00000000 || counter``.
        return b"\x00\x00\x00\x00" + struct.pack(">Q", counter)

    def _aad(self, counter: int) -> bytes:
        return self._context + struct.pack(">Q", counter)

    # -- public API -----------------------------------------------------

    @property
    def send_counter(self) -> int:
        return self._send_counter

    def encrypt(self, plaintext: bytes) -> tuple[int, bytes]:
        """Encrypt one frame; returns ``(counter, ciphertext)``.

        The counter travels with the packet so the receiver can rebuild the
        nonce; it is also authenticated as associated data.
        """
        counter = self._send_counter
        self._send_counter += 1
        return counter, self._send.encrypt(self._nonce(counter), plaintext, self._aad(counter))

    def decrypt(self, counter: int, ciphertext: bytes) -> bytes:
        """Verify and decrypt a frame, rejecting replays and tampering."""
        self._replay.check(counter)
        try:
            return self._recv.decrypt(self._nonce(counter), ciphertext, self._aad(counter))
        except InvalidTag as exc:
            raise AuthenticationError("пакет не прошёл проверку подлинности") from exc


def random_bytes(length: int) -> bytes:
    return os.urandom(length)
