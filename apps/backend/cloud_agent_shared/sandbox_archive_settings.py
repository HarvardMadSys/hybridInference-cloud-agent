"""Validated deployment-wide policy for retained-sandbox archives.

The control plane stores this policy and returns it on every host heartbeat.
The runner relays it over the authenticated sandbox-manager channel. SandboxManagers
authenticate with their workload identity, or with an AWS access key saved by
an administrator. The secret stays out of ``as_dict``: that form is written
into sandbox markers, and long-lived credentials never enter the sandbox.

Enabled archives also capture a restorable checkpoint at each clean stop: the
container's changes over its image, kept beside the archives. That is the
default; ``checkpoints_enabled=False`` opts out. Only the opt-out ever appears
on the wire, and only a host that advertises
:data:`SANDBOX_CHECKPOINT_CAPABILITY` receives it: older managers reject unknown
policy keys, and a policy without the key means the default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

SANDBOX_ARCHIVE_S3_URI: Final = "s3://freeinference/cloud-agent/sandbox"
SANDBOX_ARCHIVE_BUCKET: Final = "freeinference"
SANDBOX_ARCHIVE_PREFIX: Final = "cloud-agent/sandbox"

MIB: Final = 1024 * 1024
GIB: Final = 1024 * MIB
DEFAULT_MAX_ARCHIVE_BYTES: Final = 2 * GIB
MAX_ARCHIVE_BYTES: Final = 4 * GIB
DEFAULT_MULTIPART_THRESHOLD_BYTES: Final = 8 * MIB
DEFAULT_MULTIPART_PART_BYTES: Final = 8 * MIB
MIN_MULTIPART_PART_BYTES: Final = 5 * MIB
MAX_MULTIPART_PART_BYTES: Final = 64 * MIB
MAX_SINGLE_UPLOAD_BYTES: Final = 64 * MIB
MAX_MULTIPART_PARTS: Final = 10_000

SANDBOX_ARCHIVE_CAPABILITY: Final = "sandbox-archive-settings-v1"
SANDBOX_CHECKPOINT_CAPABILITY: Final = "sandbox-checkpoints-v1"

_ACCOUNT_ID_RE: Final = re.compile(r"[0-9]{12}")
_ACCESS_KEY_ID_RE: Final = re.compile(r"[A-Z0-9]{20}")
_SECRET_ACCESS_KEY_RE: Final = re.compile(r"[A-Za-z0-9/+=]{40}")
_KMS_KEY_ARN_RE: Final = re.compile(
    r"arn:(?P<partition>[^:]+):kms:(?P<region>[a-z0-9-]+):"
    r"(?P<account>[0-9]{12}):key/(?P<key>[A-Za-z0-9-]+)"
)


class SandboxArchivePolicyError(ValueError):
    """The stored or delivered archive policy is incomplete or unsafe."""


def _bounded_int(name: str, value: Any, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SandboxArchivePolicyError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise SandboxArchivePolicyError(f"{name} must be between {minimum} and {maximum} bytes")
    return value


@dataclass(frozen=True)
class SandboxArchivePolicy:
    """One normalized policy revision delivered intact to every host."""

    enabled: bool = False
    region: str = ""
    kms_key_arn: str = ""
    expected_bucket_owner: str = ""
    # True when uploads authenticate with an access key. Markers keep this
    # flag and drop the secret, so a sandbox never receives the credential
    # while still matching the live policy.
    access_key_auth: bool = False
    aws_access_key_id: str = field(default="", compare=False, repr=False)
    aws_secret_access_key: str = field(default="", compare=False, repr=False)
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES
    multipart_threshold_bytes: int = DEFAULT_MULTIPART_THRESHOLD_BYTES
    multipart_part_bytes: int = DEFAULT_MULTIPART_PART_BYTES
    revision: int = 0
    # On by default; checkpoints ride on archives, so disabled archives
    # always mean no checkpoints.
    checkpoints_enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise SandboxArchivePolicyError("enabled must be a boolean")
        if not isinstance(self.checkpoints_enabled, bool):
            raise SandboxArchivePolicyError("checkpoints_enabled must be a boolean")
        if not self.enabled:
            object.__setattr__(self, "checkpoints_enabled", False)
        if not isinstance(self.access_key_auth, bool):
            raise SandboxArchivePolicyError("access_key_auth must be a boolean")
        if (self.aws_access_key_id or self.aws_secret_access_key) and not self.access_key_auth:
            object.__setattr__(self, "access_key_auth", True)
        for name, value in (
            ("region", self.region),
            ("kms_key_arn", self.kms_key_arn),
            ("expected_bucket_owner", self.expected_bucket_owner),
            ("aws_access_key_id", self.aws_access_key_id),
            ("aws_secret_access_key", self.aws_secret_access_key),
        ):
            if not isinstance(value, str) or value.strip() != value:
                raise SandboxArchivePolicyError(f"{name} must be a trimmed string")

        if self.enabled and not self.region:
            raise SandboxArchivePolicyError("region is required when sandbox archives are enabled")
        if bool(self.aws_access_key_id) != bool(self.aws_secret_access_key):
            raise SandboxArchivePolicyError(
                "aws_access_key_id and aws_secret_access_key must both be set"
            )
        if self.aws_access_key_id and _ACCESS_KEY_ID_RE.fullmatch(self.aws_access_key_id) is None:
            raise SandboxArchivePolicyError(
                "aws_access_key_id must be a 20-character AWS access key id"
            )
        if self.aws_secret_access_key and (
            _SECRET_ACCESS_KEY_RE.fullmatch(self.aws_secret_access_key) is None
        ):
            raise SandboxArchivePolicyError(
                "aws_secret_access_key must be a 40-character AWS secret access key"
            )
        if self.enabled and not self.access_key_auth and not self.kms_key_arn:
            raise SandboxArchivePolicyError(
                "kms_key_arn is required when sandbox archives are enabled"
            )
        if self.enabled and not self.access_key_auth and not self.expected_bucket_owner:
            raise SandboxArchivePolicyError(
                "expected_bucket_owner is required when sandbox archives are enabled"
            )

        if self.expected_bucket_owner and not _ACCOUNT_ID_RE.fullmatch(self.expected_bucket_owner):
            raise SandboxArchivePolicyError(
                "expected_bucket_owner must be a 12-digit AWS account id"
            )
        kms_match = _KMS_KEY_ARN_RE.fullmatch(self.kms_key_arn) if self.kms_key_arn else None
        if self.kms_key_arn and kms_match is None:
            raise SandboxArchivePolicyError("kms_key_arn must be a KMS key ARN, not an alias")
        if kms_match is not None and self.region and kms_match.group("region") != self.region:
            raise SandboxArchivePolicyError("the KMS key ARN and archive region must match")
        if (
            kms_match is not None
            and self.expected_bucket_owner
            and kms_match.group("account") != self.expected_bucket_owner
        ):
            raise SandboxArchivePolicyError(
                "the KMS key ARN account and expected bucket owner must match"
            )

        _bounded_int(
            "max_archive_bytes",
            self.max_archive_bytes,
            minimum=1,
            maximum=MAX_ARCHIVE_BYTES,
        )
        _bounded_int(
            "multipart_threshold_bytes",
            self.multipart_threshold_bytes,
            minimum=1,
            maximum=min(self.max_archive_bytes, MAX_SINGLE_UPLOAD_BYTES),
        )
        _bounded_int(
            "multipart_part_bytes",
            self.multipart_part_bytes,
            minimum=MIN_MULTIPART_PART_BYTES,
            maximum=MAX_MULTIPART_PART_BYTES,
        )
        parts = (self.max_archive_bytes + self.multipart_part_bytes - 1) // (
            self.multipart_part_bytes
        )
        if parts > MAX_MULTIPART_PARTS:
            raise SandboxArchivePolicyError(
                "max_archive_bytes and multipart_part_bytes could require more than "
                f"{MAX_MULTIPART_PARTS} S3 parts"
            )
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise SandboxArchivePolicyError("revision must be a non-negative integer")
        if self.revision < 0:
            raise SandboxArchivePolicyError("revision must be a non-negative integer")

    @property
    def checkpoints_opted_out(self) -> bool:
        """Whether enabled archives explicitly skip checkpoints."""
        return self.enabled and not self.checkpoints_enabled

    @property
    def uses_access_key(self) -> bool:
        """Whether uploads authenticate with a saved access key."""
        return self.access_key_auth

    def equivalent(self, other: SandboxArchivePolicy) -> bool:
        """Compare archive identity and credentials.

        Equality ignores credentials so a sandbox marker matches the live
        policy. Heartbeat replay still has to notice a key rotation that
        arrives without a new revision.
        """
        return (
            self == other
            and self.aws_access_key_id == other.aws_access_key_id
            and self.aws_secret_access_key == other.aws_secret_access_key
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> SandboxArchivePolicy:
        """Parse a database or heartbeat object.

        Static credentials are optional. Session tokens and custom endpoints
        stay rejected: an access key is the only extra credential, and the
        destination remains the fixed AWS bucket.
        """
        raw = dict(value or {})
        uri = raw.pop("s3_uri", SANDBOX_ARCHIVE_S3_URI)
        if uri != SANDBOX_ARCHIVE_S3_URI:
            raise SandboxArchivePolicyError(f"s3_uri must be exactly {SANDBOX_ARCHIVE_S3_URI}")
        allowed = {
            "enabled",
            "region",
            "kms_key_arn",
            "expected_bucket_owner",
            "access_key_auth",
            "aws_access_key_id",
            "aws_secret_access_key",
            "max_archive_bytes",
            "multipart_threshold_bytes",
            "multipart_part_bytes",
            "revision",
            "checkpoints_enabled",
            # A response timestamp is metadata, not sandbox manager configuration.
            "updated_at",
            # Admin responses name this flag. Hosts ignore it.
            "aws_secret_access_key_configured",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise SandboxArchivePolicyError(
                "unknown sandbox archive setting(s): " + ", ".join(unknown)
            )
        raw.pop("updated_at", None)
        raw.pop("aws_secret_access_key_configured", None)
        return cls(
            enabled=raw.get("enabled", False),
            region=raw.get("region", ""),
            kms_key_arn=raw.get("kms_key_arn", ""),
            expected_bucket_owner=raw.get("expected_bucket_owner", ""),
            access_key_auth=raw.get("access_key_auth", False),
            aws_access_key_id=raw.get("aws_access_key_id", ""),
            aws_secret_access_key=raw.get("aws_secret_access_key", ""),
            max_archive_bytes=raw.get("max_archive_bytes", DEFAULT_MAX_ARCHIVE_BYTES),
            multipart_threshold_bytes=raw.get(
                "multipart_threshold_bytes", DEFAULT_MULTIPART_THRESHOLD_BYTES
            ),
            multipart_part_bytes=raw.get("multipart_part_bytes", DEFAULT_MULTIPART_PART_BYTES),
            revision=raw.get("revision", 0),
            checkpoints_enabled=raw.get("checkpoints_enabled", True),
        )

    def as_dict(self) -> dict[str, bool | int | str]:
        """Return the marker and admin identity, without AWS credentials.

        Sandbox markers are written inside the container. Keeping the access
        key out of this form is what stops it from entering the sandbox.
        """
        return {
            "enabled": self.enabled,
            "s3_uri": SANDBOX_ARCHIVE_S3_URI,
            "region": self.region,
            "kms_key_arn": self.kms_key_arn,
            "expected_bucket_owner": self.expected_bucket_owner,
            "max_archive_bytes": self.max_archive_bytes,
            "multipart_threshold_bytes": self.multipart_threshold_bytes,
            "multipart_part_bytes": self.multipart_part_bytes,
            "revision": self.revision,
            **({"access_key_auth": True} if self.access_key_auth else {}),
            # Only the opt-out is written. The default stays byte-identical
            # to policies and markers that predate checkpoints.
            **({"checkpoints_enabled": False} if self.checkpoints_opted_out else {}),
        }

    def as_sandbox_manager_dict(self) -> dict[str, bool | int | str]:
        """Return the host payload, including a saved access key when one exists."""
        values = self.as_dict()
        if self.aws_access_key_id:
            values["aws_access_key_id"] = self.aws_access_key_id
        if self.aws_secret_access_key:
            values["aws_secret_access_key"] = self.aws_secret_access_key
        return values

    def as_storage_dict(self) -> dict[str, bool | int | str]:
        """Return only values kept inside the JSONB policy column."""
        values = self.as_dict()
        values.pop("s3_uri")
        values.pop("revision")
        if self.aws_access_key_id:
            values["aws_access_key_id"] = self.aws_access_key_id
        if self.aws_secret_access_key:
            values["aws_secret_access_key"] = self.aws_secret_access_key
        return values


__all__ = [
    "DEFAULT_MAX_ARCHIVE_BYTES",
    "DEFAULT_MULTIPART_PART_BYTES",
    "DEFAULT_MULTIPART_THRESHOLD_BYTES",
    "MAX_ARCHIVE_BYTES",
    "MAX_MULTIPART_PART_BYTES",
    "MAX_SINGLE_UPLOAD_BYTES",
    "MIN_MULTIPART_PART_BYTES",
    "SANDBOX_ARCHIVE_BUCKET",
    "SANDBOX_ARCHIVE_CAPABILITY",
    "SANDBOX_ARCHIVE_PREFIX",
    "SANDBOX_ARCHIVE_S3_URI",
    "SANDBOX_CHECKPOINT_CAPABILITY",
    "SandboxArchivePolicy",
    "SandboxArchivePolicyError",
]
