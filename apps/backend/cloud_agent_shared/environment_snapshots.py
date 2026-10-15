"""What the control plane and a host agree on to reuse an environment's installation.

A snapshot is a container after its environment's setup script ran, without
the repository's own files, kept in S3 like a checkpoint. A new workspace of
the same owner, environment, setup fingerprint and sandbox image starts from it
and skips the installation. Both sides read the names and limits below, so they
live here rather than in either implementation package.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime  # noqa: TC003 - Pydantic resolves annotations at runtime.
from pathlib import PurePosixPath
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cloud_agent_shared.sandbox_archive_settings import SANDBOX_ARCHIVE_PREFIX
from cloud_agent_shared.sandbox_restore import pullable_image

#: A host can build environment snapshots and create a workspace from one. A
#: prebuild is handed only to a replica that attests it, and a claim names a
#: snapshot only to one.
ENVIRONMENT_SNAPSHOT_CAPABILITY: Final = "environment-snapshot-v1"

#: A snapshot is not handed out once it is this old: dependencies drift, and
#: the bucket's lifecycle rule (30 days) must never remove one still in use.
SNAPSHOT_MAX_AGE_DAYS: Final = 25
#: A ready snapshot nobody started from for this long is deleted.
SNAPSHOT_UNUSED_DAYS: Final = 7
#: An owner's ready snapshots are kept within this many bytes, least recently
#: used first out.
SNAPSHOT_OWNER_QUOTA_BYTES: Final = 20 * 1024 * 1024 * 1024
#: Replaced snapshots stay this long, for workspaces already starting from one.
SNAPSHOT_SUPERSEDED_GRACE_HOURS: Final = 24

#: Dependency manifests whose content decides whether a snapshot still holds
#: the right dependencies. The first seven are the lockfiles behind the setup
#: script suggestions.
LOCKFILE_NAMES: Final = frozenset(
    {
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "uv.lock",
        "poetry.lock",
        "Cargo.lock",
        "go.sum",
        "bun.lock",
        "bun.lockb",
        "Pipfile.lock",
        "Gemfile.lock",
        "composer.lock",
    }
)
_REQUIREMENTS_RE: Final = re.compile(r"requirements[\w.-]*\.txt")
#: At most this many manifests are fingerprinted; a repository with more is
#: never matched, so it always installs in full.
MAX_LOCKFILES: Final = 200

SNAPSHOT_IMAGE_REPOSITORY: Final = "freeinference-environment"
#: Label naming the snapshot a workspace container was created from.
SNAPSHOT_LABEL: Final = "org.freeinference.chat-environment-snapshot"
#: Root-only marker inside a container: setup is done for this fingerprint.
SETUP_MARKER_NAME: Final = "environment-setup-v1.json"

_SNAPSHOT_ID_RE: Final = re.compile(r"[0-9a-f]{32}")
_DIGEST_RE: Final = re.compile(r"[0-9a-f]{64}")
_SHA_RE: Final = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def is_lockfile(path: str) -> bool:
    """Whether a tracked repository path is a dependency manifest."""
    name = PurePosixPath(path).name
    return name in LOCKFILE_NAMES or _REQUIREMENTS_RE.fullmatch(name) is not None


def valid_lockfiles(value: object) -> bool:
    """Whether ``value`` maps dependency manifests to their Git object ids."""
    return (
        isinstance(value, dict)
        and len(value) <= MAX_LOCKFILES
        and all(
            isinstance(path, str)
            and is_lockfile(path)
            and isinstance(blob, str)
            and _SHA_RE.fullmatch(blob) is not None
            for path, blob in value.items()
        )
    )


def owner_hash(user_id: str) -> str:
    """The owner identity in object keys and image names, never the id itself."""
    if not user_id:
        raise ValueError("snapshot owner must be named")
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def valid_snapshot_id(snapshot_id: str) -> bool:
    """Whether ``snapshot_id`` is the 32 lowercase hex digits a snapshot is named by."""
    return isinstance(snapshot_id, str) and _SNAPSHOT_ID_RE.fullmatch(snapshot_id) is not None


def snapshot_object_key(owner: str, snapshot_id: str) -> str:
    """Where one owner's snapshot is stored, under the archive prefix hosts may write."""
    if _DIGEST_RE.fullmatch(owner or "") is None or not valid_snapshot_id(snapshot_id):
        raise ValueError("snapshot key needs an owner hash and a snapshot id")
    return f"{SANDBOX_ARCHIVE_PREFIX}/env/v1/{owner}/{snapshot_id}.oci.tar"


def snapshot_image_reference(owner: str, snapshot_id: str) -> str:
    """The one local name a snapshot image has on any host."""
    if _DIGEST_RE.fullmatch(owner or "") is None or not valid_snapshot_id(snapshot_id):
        raise ValueError("snapshot image needs an owner hash and a snapshot id")
    return f"{SNAPSHOT_IMAGE_REPOSITORY}/{owner[:32]}:{snapshot_id}"


class EnvironmentSnapshotRef(BaseModel):
    """A ready snapshot as a claim hands it to a host, without any value."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot_id: str
    owner: str
    object_key: str
    version_id: str | None = Field(default=None, max_length=1024)
    sha256: str
    size_bytes: int = Field(gt=0)
    base_image: str = Field(max_length=512)
    source_sha: str
    lockfiles: dict[str, str] = Field(default_factory=dict, max_length=MAX_LOCKFILES)
    built_at: datetime

    @field_validator("snapshot_id")
    @classmethod
    def _snapshot_id(cls, value: str) -> str:
        if not valid_snapshot_id(value):
            raise ValueError("snapshot id must be 32 lowercase hex digits")
        return value

    @field_validator("owner", "sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST_RE.fullmatch(value) is None:
            raise ValueError("must be 64 lowercase hex digits")
        return value

    @field_validator("source_sha")
    @classmethod
    def _source_sha(cls, value: str) -> str:
        if _SHA_RE.fullmatch(value) is None:
            raise ValueError("source commit must be a full object id")
        return value

    @field_validator("base_image")
    @classmethod
    def _base_image(cls, value: str) -> str:
        if not pullable_image(value):
            raise ValueError("snapshot base image must be a pullable digest")
        return value

    @field_validator("lockfiles")
    @classmethod
    def _lockfiles(cls, value: dict[str, str]) -> dict[str, str]:
        if not valid_lockfiles(value):
            raise ValueError("lockfile fingerprints name manifests and Git object ids")
        return value

    def model_post_init(self, _context: object) -> None:
        """The key and owner must agree, so a claim cannot point at another owner."""
        if self.object_key != snapshot_object_key(self.owner, self.snapshot_id):
            raise ValueError("snapshot key does not belong to its owner")


__all__ = [
    "ENVIRONMENT_SNAPSHOT_CAPABILITY",
    "LOCKFILE_NAMES",
    "MAX_LOCKFILES",
    "SETUP_MARKER_NAME",
    "SNAPSHOT_IMAGE_REPOSITORY",
    "SNAPSHOT_LABEL",
    "SNAPSHOT_MAX_AGE_DAYS",
    "SNAPSHOT_OWNER_QUOTA_BYTES",
    "SNAPSHOT_SUPERSEDED_GRACE_HOURS",
    "SNAPSHOT_UNUSED_DAYS",
    "EnvironmentSnapshotRef",
    "is_lockfile",
    "owner_hash",
    "snapshot_image_reference",
    "snapshot_object_key",
    "valid_lockfiles",
    "valid_snapshot_id",
]
