"""Peer-to-peer voice transport.

This package gives DotaVoice a way to carry voice directly between players
instead of through a shared server:

* :mod:`~dota_voice.net.crypto` - X25519 + XChaCha20-Poly1305 primitives;
* :mod:`~dota_voice.net.handshake` - authenticated, forward-secret handshake
  keyed by the match id;
* :mod:`~dota_voice.net.discovery` - match-keyed peer discovery;
* :mod:`~dota_voice.net.transport` - UDP today, Steam SDR as an opt-in stub;
* :mod:`~dota_voice.net.link` - one encrypted link, including local mute;
* :mod:`~dota_voice.net.mesh` - all links for one match.

Everything here is opt-in. The default voice backend stays the relay, and the
default configuration never opens a peer-to-peer socket or touches Steam.
"""

from __future__ import annotations

from .crypto import AuthenticationError, CryptoError, ReplayError
from .discovery import LanDiscovery, PeerRecord, RendezvousDiscovery, match_fingerprint
from .handshake import fingerprint, is_handshake_packet
from .link import LinkState, PeerLink
from .mesh import VoiceMesh
from .transport import SdrUnavailable, SteamSdrTransport, Transport, UdpTransport

__all__ = [
    "AuthenticationError",
    "CryptoError",
    "LanDiscovery",
    "LinkState",
    "PeerLink",
    "PeerRecord",
    "RendezvousDiscovery",
    "ReplayError",
    "SdrUnavailable",
    "SteamSdrTransport",
    "Transport",
    "UdpTransport",
    "VoiceMesh",
    "fingerprint",
    "is_handshake_packet",
    "match_fingerprint",
]
