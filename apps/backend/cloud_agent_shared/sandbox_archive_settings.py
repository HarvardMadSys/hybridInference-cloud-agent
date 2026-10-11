"""Validated deployment-wide policy for retained-sandbox archives.

The control plane stores this policy and returns it on every host heartbeat.
The runner only relays it over the authenticated workspace-broker channel; AWS
credentials remain the broker's workload identity and are deliberately not
part of this vocabulary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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

_ACCOUNT_ID_RE: Final = re.compile(r"[0-9]{12}")
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
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES
    multipart_threshold_bytes: int = DEFAULT_MULTIPART_THRESHOLD_BYTES
    multipart_part_bytes: int = DEFAULT_MULTIPART_PART_BYTES
    revision: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise SandboxArchivePolicyError("enabled must be a boolean")
        for name, value in (
            ("region", self.region),
            ("kms_key_arn", self.kms_key_arn),
            ("expected_bucket_owner", self.expected_bucket_owner),
        ):
            if not isinstance(value, str) or value.strip() != value:
                raise SandboxArchivePolicyError(f"{name} must be a trimmed string")

        if self.enabled and not self.region:
            raise SandboxArchivePolicyError("region is required when sandbox archives are enabled")
        if self.enabled and not self.kms_key_arn:
            raise SandboxArchivePolicyError(
                "kms_key_arn is required when sandbox archives are enabled"
            )
        if self.enabled and not self.expected_bucket_owner:
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

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> SandboxArchivePolicy:
        """Parse a database or heartbeat object without accepting credential fields."""
        raw = dict(value or {})
        uri = raw.pop("s3_uri", SANDBOX_ARCHIVE_S3_URI)
        if uri != SANDBOX_ARCHIVE_S3_URI:
            raise SandboxArchivePolicyError(f"s3_uri must be exactly {SANDBOX_ARCHIVE_S3_URI}")
        allowed = {
            "enabled",
            "region",
            "kms_key_arn",
            "expected_bucket_owner",
            "max_archive_bytes",
            "multipart_threshold_bytes",
            "multipart_part_bytes",
            "revision",
            # A response timestamp is metadata, not broker configuration.
            "updated_at",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise SandboxArchivePolicyError(
                "unknown sandbox archive setting(s): " + ", ".join(unknown)
            )
        raw.pop("updated_at", None)
        return cls(
            enabled=raw.get("enabled", False),
            region=raw.get("region", ""),
            kms_key_arn=raw.get("kms_key_arn", ""),
            expected_bucket_owner=raw.get("expected_bucket_owner", ""),
            max_archive_bytes=raw.get("max_archive_bytes", DEFAULT_MAX_ARCHIVE_BYTES),
            multipart_threshold_bytes=raw.get(
                "multipart_threshold_bytes", DEFAULT_MULTIPART_THRESHOLD_BYTES
            ),
            multipart_part_bytes=raw.get("multipart_part_bytes", DEFAULT_MULTIPART_PART_BYTES),
            revision=raw.get("revision", 0),
        )

    def as_dict(self) -> dict[str, bool | int | str]:
        """Return the canonical heartbeat/admin representation."""
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
        }

    def as_storage_dict(self) -> dict[str, bool | int | str]:
        """Return only values kept inside the JSONB policy column."""
        values = self.as_dict()
        values.pop("s3_uri")
        values.pop("revision")
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
    "SandboxArchivePolicy",
    "SandboxArchivePolicyError",
]
