"""Security building blocks for DotaVoice.

Three concerns live here, all of them about protecting the player rather than
the voice stream:

* :mod:`~dota_voice.security.policy` - the VAC/account safety invariants, written
  down as code that can guard and report on them;
* :mod:`~dota_voice.security.updater` - signed, hash-pinned, staged updates, so a
  compromised download cannot become remote code execution;
* :mod:`~dota_voice.security.autostart` - per-user start-at-login, reversible.

Voice confidentiality itself lives in :mod:`dota_voice.net`.
"""

from __future__ import annotations

from .autostart import (
    AutostartEntry,
    autostart_path,
    current_command,
    disable as disable_autostart,
    enable as enable_autostart,
    is_enabled as autostart_enabled,
    status as autostart_status,
)
from .policy import (
    Invariant,
    PolicyViolation,
    SafetyPolicy,
    SafetyReport,
    audit_config,
    default_policy,
    forbidden_modules,
    looks_like_credential_field,
    report_text,
)
from .updater import (
    Artifact,
    Manifest,
    is_newer,
    UpdateError,
    UpdatePolicy,
    UpdateStatus,
    generate_keypair,
    load_manifest,
    parse_version,
    should_offer,
    sign_manifest,
    verify_artifact,
    verify_manifest,
)

__all__ = [
    "Artifact",
    "AutostartEntry",
    "Invariant",
    "Manifest",
    "PolicyViolation",
    "SafetyPolicy",
    "SafetyReport",
    "UpdateError",
    "UpdatePolicy",
    "UpdateStatus",
    "audit_config",
    "autostart_enabled",
    "autostart_path",
    "autostart_status",
    "current_command",
    "default_policy",
    "disable_autostart",
    "enable_autostart",
    "forbidden_modules",
    "generate_keypair",
    "is_newer",
    "load_manifest",
    "looks_like_credential_field",
    "parse_version",
    "report_text",
    "should_offer",
    "sign_manifest",
    "verify_artifact",
    "verify_manifest",
]
