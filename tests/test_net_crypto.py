"""Tests for the peer-to-peer crypto layer.

The AEAD is cross-checked against PyNaCl (libsodium) where it is available, so
the tests do not just check that our code agrees with itself. The HChaCha20
vector is the one published in draft-irtf-cfrg-xchacha.
"""

from __future__ import annotations

import struct

import pytest

from dota_voice.net import crypto

nacl = pytest.importorskip("nacl", reason="PyNaCl нужен для проверки совместимости")
import nacl.bindings as libsodium  # noqa: E402


def test_hchacha20_matches_published_vector():
    key = bytes(range(32))
    nonce = bytes.fromhex("000000090000004a0000000031415927")
    expected = bytes.fromhex(
        "82413b4227b27bfed30e42508a877d73"
        "a0f9e4d58a74a853c12ec41326d3ecdc"
    )
    assert crypto.hchacha20(key, nonce) == expected


def test_hchacha20_rejects_bad_lengths():
    with pytest.raises(ValueError):
        crypto.hchacha20(b"short", b"\x00" * 16)
    with pytest.raises(ValueError):
        crypto.hchacha20(b"\x00" * 32, b"short")


def _session_pair():
    a_private = crypto.generate_private_key()
    b_private = crypto.generate_private_key()
    a_public = crypto.public_bytes(a_private)
    b_public = crypto.public_bytes(b_private)
    passphrase = crypto.match_passphrase("7123456789")

    a_keys = crypto.handshake_keys(
        passphrase, crypto.x25519_shared(a_private, b_public), a_public, b_public, initiator=True
    )
    b_keys = crypto.handshake_keys(
        passphrase, crypto.x25519_shared(b_private, a_public), a_public, b_public, initiator=False
    )
    a_send, a_recv, a_prefix = a_keys
    b_send, b_recv, b_prefix = b_keys
    # Directions must be mirrored.
    assert a_send == b_recv and a_recv == b_send and a_prefix == b_prefix
    return (
        crypto.SessionCipher(a_send, a_recv, a_prefix),
        crypto.SessionCipher(b_send, b_recv, b_prefix),
        a_send,
        a_prefix,
    )


def test_shared_secret_agrees():
    a = crypto.generate_private_key()
    b = crypto.generate_private_key()
    assert crypto.x25519_shared(a, crypto.public_bytes(b)) == crypto.x25519_shared(
        b, crypto.public_bytes(a)
    )


def test_session_cipher_roundtrip():
    a, b, _, _ = _session_pair()
    counter, ciphertext = a.encrypt(b"voice frame")
    assert b.decrypt(counter, ciphertext) == b"voice frame"


def test_session_cipher_is_wire_compatible_with_libsodium():
    """Our packets must decrypt in libsodium and vice versa."""
    a, b, send_key, prefix = _session_pair()
    counter, ciphertext = a.encrypt(b"frame")

    subkey = crypto.hchacha20(send_key, prefix)
    nonce = b"\x00\x00\x00\x00" + struct.pack(">Q", counter)
    aad = struct.pack(">Q", counter)
    assert (
        libsodium.crypto_aead_chacha20poly1305_ietf_decrypt(
            ciphertext, aad, nonce, subkey
        )
        == b"frame"
    )
    # And the full 24-byte-nonce XChaCha20 entry point agrees too.
    assert (
        libsodium.crypto_aead_xchacha20poly1305_ietf_decrypt(
            ciphertext, aad, prefix + struct.pack(">Q", counter), send_key
        )
        == b"frame"
    )


def test_we_decrypt_a_libsodium_packet():
    """A packet produced by libsodium must decrypt on our side."""
    a, _, send_key, prefix = _session_pair()
    subkey = crypto.hchacha20(send_key, prefix)
    nonce = b"\x00" * 4 + struct.pack(">Q", 0)
    ciphertext = libsodium.crypto_aead_chacha20poly1305_ietf_encrypt(
        b"from libsodium", struct.pack(">Q", 0), nonce, subkey
    )
    # ``send_key`` is the key 'a' uses to send, which is the key it also expects
    # to receive from a peer that mirrors it; build a cipher around it directly.
    cipher = crypto.SessionCipher(send_key, send_key, prefix)
    assert cipher.decrypt(0, ciphertext) == b"from libsodium"


def test_tampered_packet_is_rejected():
    a, b, _, _ = _session_pair()
    counter, ciphertext = a.encrypt(b"voice frame")
    tampered = bytes([ciphertext[0] ^ 0x01]) + ciphertext[1:]
    with pytest.raises(crypto.AuthenticationError):
        b.decrypt(counter, tampered)


def test_replay_is_rejected():
    a, b, _, _ = _session_pair()
    counter, ciphertext = a.encrypt(b"voice frame")
    assert b.decrypt(counter, ciphertext) == b"voice frame"
    with pytest.raises(crypto.ReplayError):
        b.decrypt(counter, ciphertext)


def test_wrong_counter_is_rejected():
    a, b, _, _ = _session_pair()
    counter, ciphertext = a.encrypt(b"voice frame")
    with pytest.raises(crypto.AuthenticationError):
        b.decrypt(counter + 1, ciphertext)


def test_reordering_within_window_is_accepted():
    a, b, _, _ = _session_pair()
    frames = [a.encrypt(f"frame-{i}".encode()) for i in range(5)]
    # Deliver out of order.
    for counter, ciphertext in reversed(frames):
        assert b.decrypt(counter, ciphertext).startswith(b"frame-")


def test_old_packet_outside_window_is_rejected():
    a, b, _, _ = _session_pair()
    old = a.encrypt(b"old")
    for _ in range(1100):
        b.decrypt(*a.encrypt(b"padding"))
    with pytest.raises(crypto.ReplayError):
        b.decrypt(*old)


def test_match_passphrase_is_deterministic_and_separated():
    assert crypto.match_passphrase("abc") == crypto.match_passphrase("abc")
    assert crypto.match_passphrase("abc") != crypto.match_passphrase("abd")
    assert crypto.match_passphrase("abc") != crypto.match_passphrase("abc", extra=b"salt")


def test_match_passphrase_requires_a_match_id():
    with pytest.raises(ValueError):
        crypto.match_passphrase("")


def test_handshake_mac_detects_a_changed_transcript():
    passphrase = crypto.match_passphrase("1")
    mac = crypto.handshake_mac(passphrase, b"transcript")
    crypto.verify_handshake_mac(passphrase, b"transcript", mac)
    with pytest.raises(crypto.AuthenticationError):
        crypto.verify_handshake_mac(passphrase, b"other", mac)
