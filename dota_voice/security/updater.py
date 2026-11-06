"""Signed, verified application updates.

An auto-updater is the single most dangerous feature in a desktop app: if an
attacker can substitute the update, they get code execution on every user's
machine, and no amount of voice encryption helps. So the update path is built
around verification, not around trust in the transport.

Guarantees
----------
1. **Everything is signed.** The manifest is signed with Ed25519. The client
   ships with the public key baked in and refuses any manifest that does not
   verify. HTTPS alone is not treated as sufficient.
2. **Artifacts are hash-pinned.** Each artifact carries a SHA-256 digest, checked
   after download and before it is handed to the installer.
3. **Version floor.** A ``min_supported_version`` in the manifest forces old,
   possibly vulnerable builds to update instead of staying on the network.
4. **Staged rollout.** A manifest can say "this build is for 10% of users"; the
   decision is deterministic per install id, so a user does not flip between
   "update" and "no update" on every check.
5. **No downgrade.** An older version is never installed over a newer one unless
   the manifest explicitly marks it as a security rollback.

Key handling
------------
The signing key must never live on a developer laptop. It belongs in CI secrets
or an HSM; only the public key is compiled into the app. :func:`generate_keypair`
exists for tooling and tests, and writes the private key with owner-only
permissions as a reminder that it is sensitive.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import platform
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Artifact",
    "Manifest",
    "UpdateError",
    "UpdatePolicy",
    "UpdateStatus",
    "generate_keypair",
    "load_public_key",
    "should_offer",
    "verify_artifact",
    "verify_manifest",
]

MANIFEST_VERSION = 1


class UpdateError(RuntimeError):
    """Any failure in the update path."""


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def generate_keypair(directory: Path | None = None) -> tuple[str, str]:
    """Generate an Ed25519 keypair; return ``(private_b64, public_b64)``.

    When ``directory`` is given the private key is written to
    ``update-signing.key`` with ``0600`` permissions. Keep it out of the repo.
    """
    private = Ed25519PrivateKey.generate()
    private_bytes = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        key_path = directory / "update-signing.key"
        key_path.write_text(_b64(private_bytes), encoding="ascii")
        with contextlib.suppress(OSError):
            os.chmod(key_path, 0o600)
        logger.warning(
            "Приватный ключ подписи записан в %s. Не коммитьте его: храните ключ "
            "в секретах CI или в HSM.",
            key_path,
        )
    return _b64(private_bytes), _b64(public_bytes)


def load_public_key(value: str | bytes) -> Ed25519PublicKey:
    """Load a public key from base64 (the form shipped in the app)."""
    raw = _unb64(value) if isinstance(value, str) else value
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise UpdateError("некорректный публичный ключ обновлений") from exc


def sign_manifest(manifest: dict[str, Any], private_key_b64: str) -> str:
    """Produce the detached signature (base64) for a manifest dict."""
    private = Ed25519PrivateKey.from_private_bytes(_unb64(private_key_b64))
    return _b64(private.sign(_canonical(manifest)))


def _canonical(manifest: dict[str, Any]) -> bytes:
    """Deterministic bytes for signing: sorted keys, no whitespace drift."""
    payload = {k: v for k, v in manifest.items() if k != "signature"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_manifest(manifest: dict[str, Any], public_key: str | bytes) -> dict[str, Any]:
    """Verify a signed manifest; return it on success, raise otherwise."""
    signature = manifest.get("signature")
    if not signature:
        raise UpdateError("манифест обновления не подписан")
    key = load_public_key(public_key)
    try:
        key.verify(_unb64(signature), _canonical(manifest))
    except (InvalidSignature, ValueError) as exc:
        raise UpdateError("подпись манифеста обновления недействительна") from exc
    if int(manifest.get("manifest_version", 0)) != MANIFEST_VERSION:
        raise UpdateError(
            f"неподдерживаемая версия манифеста: {manifest.get('manifest_version')}"
        )
    return manifest


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, ...]:
    """Parse a dotted version into a comparable tuple.

    Trailing zeros are dropped so ``1.2`` and ``1.2.0`` compare equal; otherwise
    Python's tuple ordering would rank ``(1, 2)`` below ``(1, 2, 0)``.
    """
    parts: list[int] = []
    for chunk in str(text).split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


@dataclass
class Artifact:
    """One downloadable file with its pinned hash."""

    url: str
    sha256: str
    size: int = 0
    platform: str = "any"
    kind: str = "installer"

    def verify(self, data: bytes) -> None:
        digest = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(digest.lower(), self.sha256.lower()):
            raise UpdateError(
                "контрольная сумма загруженного файла не совпала — файл повреждён "
                "или подменён"
            )
        if self.size and len(data) != self.size:
            raise UpdateError("размер загруженного файла не совпал с манифестом")


@dataclass
class Manifest:
    version: str
    artifacts: list[Artifact] = field(default_factory=list)
    min_supported_version: str = "0"
    rollout_percent: int = 100
    security_rollback: bool = False
    notes: str = ""
    published_at: float = 0.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Manifest":
        artifacts = [
            Artifact(
                url=str(item["url"]),
                sha256=str(item["sha256"]),
                size=int(item.get("size", 0)),
                platform=str(item.get("platform", "any")),
                kind=str(item.get("kind", "installer")),
            )
            for item in raw.get("artifacts", [])
            if isinstance(item, dict) and "url" in item and "sha256" in item
        ]
        return cls(
            version=str(raw.get("version", "0")),
            artifacts=artifacts,
            min_supported_version=str(raw.get("min_supported_version", "0")),
            rollout_percent=int(raw.get("rollout_percent", 100)),
            security_rollback=bool(raw.get("security_rollback", False)),
            notes=str(raw.get("notes", "")),
            published_at=float(raw.get("published_at", 0.0) or 0.0),
        )

    def artifact_for(self, system: str | None = None) -> Artifact | None:
        system = (system or platform.system()).lower()
        for artifact in self.artifacts:
            if artifact.platform.lower() in (system, "any"):
                return artifact
        return None


@dataclass
class UpdatePolicy:
    """Client-side update rules."""

    public_key: str = ""
    current_version: str = "0.1.0"
    install_id: str = ""
    channel: str = "stable"

    def as_dict(self) -> dict[str, Any]:
        # The install id is random, not a device fingerprint: it exists only to
        # make staged rollout stable for one installation.
        return {
            "current_version": self.current_version,
            "channel": self.channel,
        }


@dataclass
class UpdateStatus:
    available: bool = False
    forced: bool = False
    version: str = ""
    reason: str = ""
    manifest: Manifest | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "forced": self.forced,
            "version": self.version,
            "reason": self.reason,
        }


def _rollout_bucket(install_id: str, version: str) -> int:
    """Deterministic 0..99 bucket for one install and one version."""
    digest = hashlib.sha256(f"{install_id}:{version}".encode("utf-8")).digest()
    return digest[0] % 100


def should_offer(
    manifest: Manifest,
    policy: UpdatePolicy,
    *,
    now: float | None = None,
) -> UpdateStatus:
    """Decide whether to offer (or force) this update.

    Order of checks matters: a forced security floor wins over everything,
    including a rollout percentage, because leaving an old vulnerable build in
    place is the worse outcome.
    """
    if manifest.security_rollback and is_newer(policy.current_version, manifest.version):
        return UpdateStatus(
            available=True,
            forced=True,
            version=manifest.version,
            reason="security_rollback",
            manifest=manifest,
        )

    if not is_newer(manifest.version, policy.current_version):
        return UpdateStatus(reason="up_to_date")

    forced = parse_version(policy.current_version) < parse_version(
        manifest.min_supported_version
    )
    if forced:
        return UpdateStatus(
            available=True,
            forced=True,
            version=manifest.version,
            reason="below_min_supported",
            manifest=manifest,
        )

    if manifest.rollout_percent < 100:
        bucket = _rollout_bucket(policy.install_id or "anonymous", manifest.version)
        if bucket >= manifest.rollout_percent:
            return UpdateStatus(
                reason=f"staged_rollout:{bucket}>={manifest.rollout_percent}"
            )

    return UpdateStatus(
        available=True,
        version=manifest.version,
        reason="newer_version",
        manifest=manifest,
    )


def verify_artifact(artifact: Artifact, data: bytes) -> bytes:
    """Verify a downloaded artifact's hash and return the bytes."""
    artifact.verify(data)
    return data


def load_manifest(
    text: str, public_key: str, *, policy: UpdatePolicy | None = None
) -> UpdateStatus:
    """Parse, verify and evaluate a manifest document (the whole update check)."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UpdateError("манифест обновления не является корректным JSON") from exc
    if not isinstance(raw, dict):
        raise UpdateError("манифест обновления должен быть объектом JSON")
    verify_manifest(raw, public_key)
    manifest = Manifest.from_dict(raw)
    return should_offer(manifest, policy or UpdatePolicy(), now=time.time())
