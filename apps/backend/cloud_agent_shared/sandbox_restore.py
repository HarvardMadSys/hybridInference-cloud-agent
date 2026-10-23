"""What the control plane and a host agree on to move a stopped group.

A group whose host is gone can continue on another host from its latest
checkpoint. The control plane decides which checkpoint covers a group and
which host may restore it; the host's sandbox manager downloads, checks and
loads it. Both read the facts below, so they live here rather than in either
implementation package.
"""

from __future__ import annotations

import re
from datetime import datetime  # noqa: TC003 — Pydantic resolves annotations at runtime.
from typing import Annotated, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

#: A host's sandbox manager can recreate a group from a checkpoint. A claim
#: hands a cold group only to a replica that attests it.
SANDBOX_RESTORE_CAPABILITY: Final = "sandbox-restore-v1"

#: The start fence a workspace request took, sent with it to a sandbox manager
#: route that may start the group's container. The fence labels only the run
#: that request is admitted to, so a checkpoint names no request whose work it
#: does not hold, and a start under a fence sealed by a stop is refused.
START_FENCE_HEADER: Final = "X-Agent-Start-Seq"

#: The checkpoint archive and receipt format every current host understands.
CHECKPOINT_FORMAT_VERSION: Final = 1
RestoreFormatVersion = Annotated[int, Field(ge=1)]


def parse_start_fence(value: str | None) -> int | None:
    """The start fence a request header names, or ``None`` when it names none.

    A request without a usable fence is still served; its run is left
    unlabelled, so no checkpoint of it covers the group.
    """
    if value is None or not value.isascii() or not value.isdigit() or len(value) > 18:
        return None
    return int(value)


#: How long a cold group waits after its first, second and third failed restore
#: before a host may claim it again. A restore fails on a registry or S3
#: outage as readily as on a broken checkpoint. Spaced only by the queue's short
#: retry delay, every claim fell inside an outage of a minute or two and the
#: chat was lost for good. These waits spread the attempts over more than half
#: an hour, which a passing outage does not outlast; a checkpoint that is really
#: broken still ends lost, only later.
RESTORE_RETRY_DELAYS_S: Final = (30.0, 300.0, 1800.0)

#: Claims a cold group gets before it is marked lost: the first, and one after
#: each wait above. A restore that fails before the turn is admitted returns
#: the turn to the queue, so without a bound one unreadable checkpoint would
#: be retried forever.
MAX_RESTORE_ATTEMPTS: Final = len(RESTORE_RETRY_DELAYS_S) + 1

#: Why a host hands a turn back when its sandbox manager could not recreate the
#: group. It counts as a failed restore. A claim handed back for any other reason
#: before its host reserved the group never reached the checkpoint, and counts
#: as none.
RESTORE_FAILED_RELEASE_REASON: Final = "sandbox_restore_failed"

#: A base image another host can fetch by itself: a registry host with a dot,
#: a repository, and a digest. A locally built image has no such name, and a
#: group created from one cannot leave its host. The same text is used as a
#: PostgreSQL regular expression.
PULLABLE_IMAGE_PATTERN: Final = (
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+(:[0-9]{1,5})?"
    r"/[a-z0-9]+([._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$"
)
_PULLABLE_IMAGE_RE: Final = re.compile(PULLABLE_IMAGE_PATTERN)


def pullable_image(reference: str | None) -> bool:
    """Whether another host can pull ``reference`` without this host's daemon."""
    return bool(reference) and _PULLABLE_IMAGE_RE.fullmatch(reference or "") is not None


class SandboxCheckpointMember(BaseModel):
    """One chat's native-session position inside a checkpoint."""

    model_config = ConfigDict(frozen=True)

    thread_id: str = Field(..., min_length=1, max_length=128)
    native_session_id: str | None = Field(None, max_length=256)
    last_settled_job_id: str | None = Field(None, max_length=256)
    context_status: str | None = Field(None, max_length=32)


class SandboxCheckpointReceipt(BaseModel):
    """Durable identity of one uploaded checkpoint, reported to the control plane."""

    model_config = ConfigDict(frozen=True)

    checkpoint_id: str = Field(..., pattern=r"^[0-9a-f]{32}$")
    sandbox_id: str = Field(..., min_length=1, max_length=128)
    generation: int = Field(..., ge=1)
    container_id: str = Field(..., min_length=12, max_length=128)
    base_image: str = Field(..., min_length=1, max_length=256)
    runtime_id: str = Field(..., min_length=1, max_length=128)
    runtime_version: str | None = Field(None, max_length=128)
    object_key: str = Field(
        ...,
        pattern=r"^cloud-agent/sandbox/v2/[0-9a-f]{64}/g[0-9]+/[0-9a-f]{32}\.oci\.tar$",
        max_length=256,
    )
    version_id: str | None = Field(None, max_length=1024)
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(..., ge=1)
    new_layer_bytes: int = Field(..., ge=0)
    layer_count: int = Field(..., ge=1, le=1024)
    created_at: datetime
    members: list[SandboxCheckpointMember] = Field(default_factory=list, max_length=128)
    durations_ms: dict[str, int] = Field(default_factory=dict, max_length=16)
    format_version: Literal[1] = CHECKPOINT_FORMAT_VERSION
    #: The control plane's start fence for the run this checkpoint ends. The
    #: checkpoint covers the group only while that fence is still current;
    #: ``None`` (the fence was unknown here) never covers it.
    start_seq: int | None = Field(None, ge=0)

    @classmethod
    def from_dict(cls, value: Any) -> Self:
        """Rebuild and validate a stored receipt."""
        if not isinstance(value, dict) or "format_version" not in value:
            raise ValueError("stored checkpoint receipt has no format version")
        return cls.model_validate(value)

    def as_dict(self) -> dict[str, Any]:
        """Return the stable JSON form the store keeps and heartbeat carries."""
        payload = self.model_dump(mode="json")
        # The host has always emitted datetime.isoformat(), including +00:00.
        # Pydantic's JSON mode shortens that offset to Z, so pin the wire bytes.
        payload["created_at"] = self.created_at.isoformat()
        return payload


class CheckpointRestore(BaseModel):
    """The control plane's record of a moved group's checkpoint."""

    model_config = ConfigDict(frozen=True)

    checkpoint_id: str = Field(..., pattern=r"^[0-9a-f]{32}$")
    object_key: str = Field(..., min_length=1, max_length=1024)
    version_id: str | None = Field(None, max_length=1024)
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(..., gt=0)
    base_image: str = Field(..., min_length=1, max_length=512)
    # Keep the published >=1 request contract during extraction. The host calls
    # require_supported before it reads an archive.
    format_version: RestoreFormatVersion = CHECKPOINT_FORMAT_VERSION

    def require_supported(self) -> Self:
        """Return this restore after applying this host release's capabilities."""
        if self.format_version != CHECKPOINT_FORMAT_VERSION:
            raise ValueError("checkpoint format is not supported by this host")
        if not pullable_image(self.base_image):
            raise ValueError("checkpoint base image is not a pullable digest")
        return self


__all__ = [
    "CHECKPOINT_FORMAT_VERSION",
    "MAX_RESTORE_ATTEMPTS",
    "PULLABLE_IMAGE_PATTERN",
    "RESTORE_FAILED_RELEASE_REASON",
    "RESTORE_RETRY_DELAYS_S",
    "SANDBOX_RESTORE_CAPABILITY",
    "CheckpointRestore",
    "RestoreFormatVersion",
    "SandboxCheckpointMember",
    "SandboxCheckpointReceipt",
    "pullable_image",
]
