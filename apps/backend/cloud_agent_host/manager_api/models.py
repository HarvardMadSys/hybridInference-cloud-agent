"""Request bodies the Sandbox Manager validates, shared with the runner's client.

Each model is the one shape a route accepts. Where the runner's
``SandboxManagerClient`` sends exactly that shape, it builds the body from
the same model, so the two sides cannot drift apart field by field.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cloud_agent_host.archive.sandbox_deletion_backup import MAX_ARCHIVE_BYTES

# Pydantic resolves the field types at runtime.
from cloud_agent_host.resident_launch_config import SharedResidentLaunchConfig  # noqa: TC001
from cloud_agent_shared.environment import BoundEnvironment  # noqa: TC001
from cloud_agent_shared.environment_snapshots import EnvironmentSnapshotRef  # noqa: TC001
from cloud_agent_shared.host_settings import SANDBOX_MANAGER_SETTING_KEYS, default_values
from cloud_agent_shared.sandbox_manager_wire import SharedGroupIdentity
from cloud_agent_shared.sandbox_restore import CheckpointRestore
from cloud_agent_shared.ssh_access import SshAccess  # noqa: TC001

#: Protocol validation keeps this upper bound even when the runtime admin value
#: is lower. The shared registry is the single source for the default.
MAX_FILE_WRITE_BYTES = (
    default_values(SANDBOX_MANAGER_SETTING_KEYS)["workspace_file_write_max_mib"] * 1024 * 1024
)


class SshAccessRequest(BaseModel):
    """A group's whole SSH state, sent by the runner or the control plane."""

    model_config = ConfigDict(extra="forbid")
    access: SshAccess = Field(repr=False)
    #: The workspace's own turn starts with this state, so its member's keys
    #: are written. Only the runner, which masks exactly those keys, says so;
    #: a push leaves it False and can only take access away.
    starting: bool = False


class EnvironmentPrepareRequest(BaseModel):
    """Immutable binding supplied only by an authenticated control plane."""

    model_config = ConfigDict(extra="forbid")
    environment: BoundEnvironment = Field(repr=False)
    activation_owner: str | None = Field(None, max_length=128)


class FileWriteRequest(BaseModel):
    """One bounded UTF-8 file replacement."""

    content: str = Field(..., max_length=MAX_FILE_WRITE_BYTES)


class TerminalCreateRequest(BaseModel):
    """Dimensions and fixed environment for one interactive terminal."""

    model_config = ConfigDict(extra="forbid")
    environment: BoundEnvironment | None = Field(None, repr=False)
    action_id: str | None = Field(None, max_length=80)
    run_id: str | None = Field(None, max_length=80)

    rows: int = Field(24, ge=2, le=200)
    cols: int = Field(80, ge=20, le=500)


class TerminalInputRequest(BaseModel):
    """One bounded base64-encoded terminal input chunk."""

    data: str = Field(..., min_length=1, max_length=88_000)


class TerminalResizeRequest(BaseModel):
    """A validated PTY window size."""

    rows: int = Field(..., ge=2, le=200)
    cols: int = Field(..., ge=20, le=500)


class TerminalCoordinationRequest(BaseModel):
    """The protected turn owning one suspension token.

    Each turn holds a token of its own, so concurrent fork siblings in one
    shared workspace never thaw each other's suspension.
    """

    job_id: str = Field(..., min_length=1, max_length=128)


class SharedLeaseRequest(BaseModel):
    """Receipt for one shared member activation mutation."""

    token: str = Field(..., min_length=1, max_length=512)


class SharedInputRequest(BaseModel):
    """The control plane's message for one member's running turn.

    ``owner`` names the activation the control plane saw running
    (``attempt:<id>``); a member now leased to anyone else does not take it.
    ``message_id`` names the waiting message, so the turn's stream can say
    which one it took.
    """

    owner: str = Field(..., min_length=1, max_length=128)
    message_id: str = Field(..., min_length=1, max_length=128)
    text: str = Field(..., min_length=1, max_length=100_000)


class SharedResidencyLostRequest(BaseModel):
    """Control-plane rejection of one exact member residency generation."""

    generation: int = Field(..., ge=1)


class SharedQuestionAnswerRequest(BaseModel):
    """A bounded native-question reply for one exact activation owner."""

    owner: str = Field(..., min_length=1, max_length=128)
    request_id: str = Field(..., min_length=1, max_length=128)
    answers: dict[
        Annotated[str, Field(min_length=1, max_length=128)],
        Annotated[list[Annotated[str, Field(max_length=10_000)]], Field(max_length=20)],
    ] = Field(default_factory=dict, max_length=10)
    reject: bool = False


class SharedAbandonRequest(SharedLeaseRequest):
    """Release a failed local admission without claiming a settled context."""

    lost: bool = False


class SharedIdleStopRequest(BaseModel):
    """Durable control-plane fence authorizing one shared-container stop."""

    generation: int = Field(..., ge=1)
    # A sandbox manager reconstructed before the first agent activation may observe a
    # generation-zero container after a file/terminal wake. The durable stop
    # fence must still be able to name that exact container.
    activation_generation: int = Field(..., ge=0)
    reason: Literal["idle", "orphaned_attempt", "lost"] = "idle"
    member_activation_generations: (
        dict[Annotated[str, Field(min_length=1, max_length=256)], Annotated[int, Field(ge=0)]]
        | None
    ) = Field(None, max_length=128)

    @model_validator(mode="after")
    def zero_activation_requires_group_stop_scope(self) -> SharedIdleStopRequest:
        """Refuse a generation-zero stop unless it is a fenced group-level orphan/lost stop."""
        if self.activation_generation == 0 and (
            self.reason not in {"orphaned_attempt", "lost"}
            or self.member_activation_generations is None
        ):
            raise ValueError("generation zero requires a fenced group-level orphan/lost stop")
        return self


class SandboxArchivePrepareRequest(BaseModel):
    """Exact stopped identity and immutable S3 object for one admin deletion."""

    request_id: str = Field(..., min_length=1, max_length=128)
    generation: int = Field(..., ge=1)
    expected_container_id: str = Field(..., min_length=1, max_length=256)
    archive_key: str = Field(..., min_length=1, max_length=1024)


class SandboxArchiveDiscardRequest(BaseModel):
    """Exact stopped identity for a deletion that does not upload an archive."""

    request_id: str = Field(..., min_length=1, max_length=128)
    generation: int = Field(..., ge=1)
    # The durable identity that lets the sandbox manager prove an
    # already-absent target.
    sandbox_id: str = Field(..., min_length=1, max_length=256)
    expected_container_id: str = Field(..., min_length=1, max_length=256)


class SandboxArchiveCommitRequest(SandboxArchivePrepareRequest):
    """Prepared receipt independently verified by the trusted control plane."""

    sha256: str = Field(..., pattern="^[0-9a-f]{64}$")
    size_bytes: int = Field(..., gt=0, le=MAX_ARCHIVE_BYTES)


class SandboxArchiveUploadRequest(SandboxArchiveCommitRequest):
    """Checksum-bound single-object upload with no S3 credential on the host."""

    upload_url: str = Field(..., min_length=1, max_length=16384)
    upload_headers: dict[
        Annotated[str, Field(min_length=1, max_length=128)],
        Annotated[str, Field(max_length=2048)],
    ] = Field(..., min_length=4, max_length=4)


class SharedSandboxRestoreRequest(CheckpointRestore):
    """The control plane's record of a moved group's checkpoint."""


class SharedSandboxIdentityRequest(SharedGroupIdentity):
    """Immutable group identity plus its current member manifest."""

    #: The checkpoint a moved group is recreated from, only while it is restoring.
    restore: SharedSandboxRestoreRequest | None = None


class SharedSandboxAcquireRequest(SharedSandboxIdentityRequest):
    """Lease one member while preserving sibling concurrency."""

    owner: str = Field(..., min_length=1, max_length=256)
    activation_generation: int = Field(..., ge=1)
    has_settled_context: bool = False
    #: An installation snapshot a new group may start from; ignored for a
    #: group that already has a container here.
    environment_snapshot: EnvironmentSnapshotRef | None = None
    #: The measurement selection the caller is profiling this path under.
    #: Deliberately unvalidated: a sandbox manager sees one call at a time and cannot
    #: know which attempt it belongs to, so the caller states the selection and
    #: `operation_profiling.start_stage_recording` decides whether to honour
    #: it. Typing or bounding this field would let a malformed *measurement*
    #: setting reject a sandbox request with 422 before the supervisor ever saw
    #: it, and measurement configuration must never fail the business call.
    profiling: Any = None


class EnvironmentSnapshotCaptureRequest(BaseModel):
    """Save one stopped environment run's container as an installation snapshot."""

    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(..., min_length=1, max_length=128)
    lease_generation: int = Field(..., ge=1)
    container_id: str = Field(..., min_length=1, max_length=256)
    sandbox_image_digest: str | None = Field(None, max_length=512)
    owner: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str = Field(..., pattern=r"^[0-9a-f]{32}$")


class EnvironmentSnapshotDeletion(BaseModel):
    """One snapshot the control plane retired: its object, if stored, and local image."""

    model_config = ConfigDict(extra="forbid")
    snapshot_id: str = Field(..., pattern=r"^[0-9a-f]{32}$")
    owner: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    object_key: str | None = Field(None, max_length=1024)
    version_id: str | None = Field(None, max_length=1024)


class EnvironmentSnapshotDeleteRequest(BaseModel):
    """Snapshots to delete from the bucket and from this host's images."""

    model_config = ConfigDict(extra="forbid")
    snapshots: list[EnvironmentSnapshotDeletion] = Field(..., max_length=100)


class SharedResidentLaunchRequest(SharedLeaseRequest):
    """Lease fence plus resident launch configuration."""

    launch: SharedResidentLaunchConfig
    #: See ``SharedSandboxAcquireRequest.profiling``. Deliberately unvalidated
    #: here too, so a malformed selection cannot reject a resident request.
    profiling: Any = None


class SharedTurnRequest(SharedLeaseRequest):
    """One prompt delivered to an already-owned resident process."""

    prompt: str = Field(..., min_length=1, max_length=2_000_000)
    model: str = Field(..., min_length=1, max_length=256)
    reasoning_effort: str | None = Field(None, max_length=64)


class SharedSettleRequest(SharedLeaseRequest):
    """One member's successful native turn boundary."""

    job_id: str = Field(..., min_length=1, max_length=256)
    context_status: Literal["new", "live", "restored"]


class SharedForkRequest(SharedSandboxIdentityRequest):
    """Prompt-free fork-only request inside one shared sandbox."""

    operation_id: str = Field(..., min_length=1, max_length=256)
    source_native_session_id: str = Field(..., min_length=1, max_length=512)
    source_native_turn_no: int = Field(..., ge=1, le=100_000)
    target_thread_id: str = Field(..., min_length=1, max_length=256)
    target_slot_id: int = Field(..., ge=0, le=127)
    source_launch: SharedResidentLaunchConfig
    target_launch: SharedResidentLaunchConfig


class SharedInitialArchiveRequest(SharedSandboxIdentityRequest):
    """Exact fresh activation whose prepared workspace becomes the baseline."""

    expected_container_id: str = Field(..., min_length=1, max_length=256)
    token: str = Field(..., min_length=1, max_length=512)
    preparation_id: str = Field(..., min_length=1, max_length=256)
    base_sha: str = Field(..., min_length=7, max_length=64)
    #: Return once the bundle is durable on this host (``upload: pending``) and
    #: upload it after. An older runner omits it and waits for the upload.
    defer_upload: bool = False


class SharedInitialArchiveStatusRequest(SharedSandboxIdentityRequest):
    """Exact fresh activation checking for a crash-committed baseline."""

    expected_container_id: str = Field(..., min_length=1, max_length=256)
    token: str = Field(..., min_length=1, max_length=512)
    preparation_id: str = Field(..., min_length=1, max_length=256)
