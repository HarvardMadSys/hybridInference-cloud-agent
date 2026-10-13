"""Worker claims, leases, retained sandbox lifecycle, and terminal outcomes."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves annotations at runtime.
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from cloud_agent.attachments.models import (
    AttachmentView,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)
from cloud_agent.execution_profiles import ExecutionProfile
from cloud_agent.schemas._host_reports import (
    WORKER_ID_MAX_CHARS,
    _HostCapability,
    _HostReportKey,
    _HostReportVersion,
    _HostVersionMenu,
)
from cloud_agent_shared.environment import (
    BoundEnvironment,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)
from cloud_agent_shared.plugins import (
    SandboxPlugin,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)

# A lease is the only thing that lets the reaper take a job back from a stuck
# or malicious worker. If the worker could pick the TTL, it could pick one long
# enough that the lease never expires — and then the capability token bound to
# that attempt would be neither self-revoking nor cancellable by the owner. The
# server therefore caps it, and 15 minutes is far above any legitimate gap
# between heartbeats.
MAX_LEASE_TTL_SECONDS = 900.0

#: A worker's terminal explanation is trimmed to this rather than refused.
#: Refusing it would refuse the finish, and a turn whose finish cannot land
#: sits on its lease until the reaper takes it — a worse outcome for the owner
#: than a long message cut short.
WORKER_FINISH_DETAIL_MAX_CHARS = 4000


class WorkerClaimAttestation(BaseModel):
    """Runtime evidence collected by this exact claiming runner replica."""

    sandbox_runtime: str | None = Field(None, max_length=128)
    sandbox_image_digest: str | None = Field(None, max_length=512)
    runtime_versions: dict[_HostReportKey, _HostReportVersion] = Field(
        default_factory=dict, max_length=64
    )
    #: Every version this replica's image holds, keyed by runtime id — the menu
    #: a stored selection is checked against before the claim honours it.
    #:
    #: Declared here or it does not exist. The runner has always sent it, and
    #: `claim_job` has always read it, but a model silently drops what it does
    #: not name: the field went over the wire, Pydantic discarded it, the claim
    #: saw an empty menu and fell back to the image default. Every selection an
    #: admin made was a no-op, and the only sign was one log line.
    runtime_versions_available: dict[_HostReportKey, _HostVersionMenu] = Field(
        default_factory=dict, max_length=64
    )
    #: Versions of in-image protocol helpers that are not Agent CLIs (for
    #: example the Claude SDK fork bridge). They ride the same attestation so
    #: the API cannot silently discard evidence used by capability gating.
    helper_versions: dict[_HostReportKey, _HostReportVersion] = Field(
        default_factory=dict, max_length=64
    )
    runner_capabilities: list[_HostCapability] = Field(default_factory=list, max_length=64)
    sandbox_archive_settings_applied_revision: StrictInt | None = Field(None, ge=0)
    host_kernel: str | None = Field(None, max_length=256)
    guest_kernel: str | None = Field(None, max_length=256)
    #: The writable-layer quota, in MiB, a group this claim creates would get;
    #: 0 is none. ``None`` is a runner from before it was reported.
    sandbox_disk_limit_mib: StrictInt | None = Field(None, ge=0, le=16777216)


class WorkerClaimRequest(BaseModel):
    """Worker request to claim the next queued job."""

    # A stale runner must fail loudly instead of having its request-body host
    # identity silently discarded. Host identity comes only from the enrolled
    # credential now; accepting the old field would make a broken rollout look
    # healthy while the server ignored what the client thought it requested.
    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(
        ...,
        max_length=WORKER_ID_MAX_CHARS,
        description="Stable identifier of the claiming worker.",
    )
    lease_ttl_seconds: float = Field(
        120.0,
        gt=0,
        le=MAX_LEASE_TTL_SECONDS,
        description="How long the lease is valid without a heartbeat.",
    )
    attestation: WorkerClaimAttestation | None = Field(
        None,
        description=(
            "Claim-time runtime/image/version/kernel stamp from this exact runner replica. "
            "Required for every retained/shared claim."
        ),
    )


#: The longest an idle host may park a wait. Hosts ask for their poll interval;
#: this only bounds how long a request can hold a connection open.
MAX_WORK_WAIT_SECONDS = 30.0


class WorkerWaitRequest(BaseModel):
    """An idle host asking to be answered when there may be work to claim."""

    model_config = ConfigDict(extra="forbid")

    since: str | None = Field(
        None,
        max_length=64,
        description="The token from this host's previous wait, taken before its last claim.",
    )
    timeout_seconds: float = Field(
        5.0,
        gt=0,
        le=MAX_WORK_WAIT_SECONDS,
        description="How long to wait for a change before answering anyway.",
    )


class WorkerWaitResponse(BaseModel):
    """The token to pass back on the next wait; the host claims either way."""

    token: str


class WorkerSandboxMember(BaseModel):
    """One native chat session inside a shared retained sandbox group."""

    thread_id: str
    native_session_id: str | None = None
    native_state: Literal["new", "ready", "fork_preparing", "lost"] = "new"
    context_status: Literal["new", "live", "restored", "lost"] = "new"
    activation_generation: int = 0
    process_generation: int = 0
    residency_generation: int = 0
    resident_state: Literal["stopped", "starting", "live", "restarting", "lost"] = "stopped"
    resident_slot_id: int | None = None
    active_attempt_id: int | None = None
    admitted_job_id: str | None = None
    admitted_at: datetime | None = None
    last_settled_job_id: str | None = None
    forked_from_thread_id: str | None = None
    fork_anchor_job_id: str | None = None


class WorkerThreadSandbox(BaseModel):
    """The durable same-host container mapping carried with a claimed turn."""

    thread_id: str
    sandbox_id: str
    generation: int
    host: str
    container_id: str | None = None
    container_name: str
    runtime_id: str
    runtime_version: str | None = None
    sandbox_image_digest: str | None = None
    execution_profile: str
    memory_limit: str = "4g"
    cpu_limit: str = "2"
    disk_limit_mib: int = Field(0, ge=0, le=16777216, strict=True)
    stop_timeout_seconds: int = 10
    state: Literal[
        "provisioning",
        "starting",
        "running",
        "stopping",
        "stopped",
        "stopped_dirty",
        "lost",
    ]
    context_status: Literal["new", "live", "restored", "lost"]
    activation_generation: int
    process_generation: int
    active_attempt_id: int | None = None
    admitted_job_id: str | None = None
    admitted_at: datetime | None = None
    last_settled_job_id: str | None = None
    native_session_id: str | None = None
    # Shared-live topology. These values are required by the retained-only
    # runner even though the database row also carries the container's own
    # lifecycle fields above.
    shared_live: Literal[True] = True
    origin_thread_id: str | None = None
    resident_state: Literal["stopped", "starting", "live", "restarting", "lost"] = "stopped"
    residency_generation: int = 0
    resident_slot_id: int | None = None
    resident_slot_capacity: int = Field(8, ge=2, le=128)
    group_last_activity_at: datetime | None = None
    group_members: list[WorkerSandboxMember] = Field(default_factory=list)


class WorkerAttachmentListResponse(BaseModel):
    """Every upload in the claimed job's conversation, without the bytes."""

    attachments: list[AttachmentView] = Field(default_factory=list)


class WorkerAttachmentContentResponse(BaseModel):
    """One upload's bytes, base64 for the same reason the upload API is JSON."""

    id: int
    path: str
    content_type: str
    content_base64: str


class WorkerClaimResponse(BaseModel):
    """A claimed job plus the capability token scoped to this attempt."""

    environment: BoundEnvironment | None = Field(None, repr=False)

    #: How many uploads this whole conversation holds, not just this turn.
    #: A host with zero here skips the upload fetch entirely, which is every
    #: turn of every task that attached nothing. It counts the thread rather
    #: than the turn because a rebuilt sandbox has to restore all of them.
    thread_attachment_count: int = 0
    #: How many this turn itself added. A retained container already holds the
    #: earlier turns' files — ``/uploads`` is root-owned and read-only, so the
    #: sandbox cannot have removed them — and re-fetching them every turn would
    #: move the whole conversation's bytes again for nothing.
    turn_attachment_count: int = 0
    job_id: str
    thread_id: str
    parent_job_id: str | None = None
    turn_no: int = 1
    attempt_id: int
    attempt_no: int
    repo: str
    base_sha: str | None = None
    task_prompt: str
    setup_script: str | None = None
    plugins: list[SandboxPlugin] = Field(default_factory=list)
    runtime: str
    model: str
    execution_profile: ExecutionProfile = ExecutionProfile.TRUSTED_V1
    #: Null is Auto. A runner too old to read it simply runs at the CLI's
    #: default, which is what the field means anyway.
    reasoning_effort: str | None = None
    #: The CLI version this attempt was stamped with, resolved at claim from
    #: the thread's pin, the deployment's selection, or the host's default.
    #:
    #: The runner puts it on the sandbox's PATH. Undeclared, it was the other
    #: half of the same bug: the claim resolved a version and wrote it onto the
    #: attempt, this model dropped it, the runner saw nothing and ran the image
    #: default — so the row said 2.1.235 while the sandbox ran 2.1.220. A
    #: silently false record is worse than a refused claim, and nothing in the
    #: job's own output contradicts it.
    #:
    #: Null means the image default, which is what a runner too old to read it
    #: runs anyway.
    runtime_version: str | None = Field(None, max_length=128)
    thread_sandbox: WorkerThreadSandbox | None = Field(
        None,
        description=(
            "The retained Docker container already owned by this chat group. Null "
            "only until its first claim reserves one; all claims stay pinned to its host."
        ),
    )
    worker_token: str
    sandbox_token: str = Field(
        "",
        description=(
            "This attempt's gateway grant, scoped to the models it may call. It does "
            "not enter the sandbox: each member's CLI runs on its own residency grant "
            "from the sandbox residency route."
        ),
    )
    mcp_token: str = Field(
        "",
        description=(
            "The sandbox's *second* credential, for this service's MCP relay. "
            "Scoped `mcp`, so it opens the relay and nothing else — not the "
            "worker surface, and not the gateway. The relay's address is the "
            "claiming host's own, the one its network rules admit; the claim "
            "names none."
        ),
    )
    clone_token: str | None = Field(
        None,
        description=(
            "Short-lived read-only credential for this one repository, for the runner to "
            "check it out with. Stays in the runner; never enters the sandbox. Null when "
            "no GitHub App is configured, which is enough for a public repository."
        ),
    )
    github_token: str | None = Field(
        None,
        repr=False,
        description=(
            "Short-lived GitHub App token scoped to this repository with contents and "
            "pull_requests write permissions. Injected as GH_TOKEN in the owning sandbox "
            "member for pushing branches and creating pull requests. Null when no GitHub "
            "App installation covers the repository."
        ),
    )
    mcp_bindings: list[str] = Field(
        default_factory=list,
        description=(
            "Aliases of the MCP connections this job may reach through the control-plane "
            "relay. The runner passes them to the runtime adapter; it never learns their "
            "addresses or their credentials."
        ),
    )
    metadata: dict[str, Any] | None = None


class WorkerHeartbeatRequest(BaseModel):
    """Worker lease renewal."""

    lease_ttl_seconds: float = Field(120.0, gt=0, le=MAX_LEASE_TTL_SECONDS)


class WorkerHeartbeatResponse(BaseModel):
    """Lease renewal result, carrying any pending cancellation."""

    ok: bool
    state: str
    cancel_requested: bool


class WorkerTerminalSuspendRequest(BaseModel):
    """Protected workspace phase that requires every owner PTY to pause."""

    phase: Literal["workspace_preparing", "workspace_finalizing"]


class WorkerContinuityRequest(BaseModel):
    """Which continuity rung this attempt is running on (design §9).

    ``hot`` means a matching supervisor-owned resident process accepted this
    turn. ``warm_native`` means the Docker-owned native session was resumed in
    a replacement process. The rung remains diagnostic; NEW/LIVE/RESTORED/LOST
    is the product-facing context contract.
    """

    # The two rungs the retained topology can actually run on. The schema
    # CHECK admits exactly these, so accepting more here would only turn a
    # diagnostic field into a database error.
    mode: Literal["hot", "warm_native"] | None = None
    context_status: Literal["new", "live", "restored", "lost"] | None = None


class WorkerSandboxReserveRequest(BaseModel):
    """Reserve immutable Docker identity before a runner asks its host to create."""

    sandbox_id: str = Field(..., min_length=1, max_length=256)
    container_name: str = Field(..., min_length=1, max_length=253)
    runtime_id: str = Field(..., min_length=1, max_length=128)
    runtime_version: str | None = Field(None, max_length=128)
    sandbox_image_digest: str | None = Field(None, max_length=512)
    execution_profile: ExecutionProfile
    host: str = Field(..., min_length=1, max_length=253)
    shared_live: Literal[True] = True
    resident_slot_capacity: int = Field(8, ge=2, le=128)
    memory_limit: str = Field("4g", min_length=1, max_length=32)
    cpu_limit: str = Field("2", min_length=1, max_length=32)
    disk_limit_mib: int = Field(0, ge=0, le=16777216, strict=True)
    stop_timeout_seconds: int = Field(10, ge=1, le=300)
    #: The image's claude-session-fork helper version, sent by the runner that
    #: is about to create the container from it. Native Fork asks it of the
    #: group rather than of whatever image the host runs by then.
    fork_helper_version: str | None = Field(None, max_length=32)


class WorkerSandboxRunningRequest(BaseModel):
    """Docker start result fenced by sandbox and activation generations."""

    sandbox_generation: int = Field(..., ge=1)
    activation_generation: int = Field(..., ge=1)
    container_id: str = Field(..., min_length=1, max_length=256)
    context_status: Literal["new", "live", "restored"]


class WorkerSandboxResidencyRequest(BaseModel):
    """Mint/rotate the renewable grant for a supervisor-owned member process."""

    sandbox_id: str = Field(..., min_length=1, max_length=256)
    runtime_id: str = Field(..., min_length=1, max_length=128)
    runtime_version: str | None = Field(None, max_length=128)


class WorkerSandboxProcessRequest(BaseModel):
    """The latest CLI process generation proven by the host supervisor."""

    sandbox_generation: int = Field(..., ge=1)
    activation_generation: int = Field(..., ge=1)
    residency_generation: int = Field(..., ge=1)
    process_generation: int = Field(..., ge=1)


class WorkerSandboxAdmissionRequest(BaseModel):
    """Durably reserve this job before its prompt can reach Agent CLI stdin."""

    sandbox_generation: int = Field(..., ge=1)
    activation_generation: int = Field(..., ge=1)
    residency_generation: int = Field(..., ge=1)


class WorkerSandboxSettleRequest(BaseModel):
    """A native context boundary that may safely begin the idle timeout."""

    sandbox_generation: int = Field(..., ge=1)
    activation_generation: int = Field(..., ge=1)
    residency_generation: int = Field(..., ge=1)
    context_status: Literal["new", "live", "restored"]
    native_session_id: str | None = Field(None, max_length=512)
    base_sha: str | None = Field(None, min_length=7, max_length=64)
    idle_timeout_seconds: float = Field(1800.0, gt=0, le=7 * 24 * 60 * 60)

    @field_validator("native_session_id")
    @classmethod
    def normalize_native_session_id(cls, value: str | None) -> str | None:
        """Repeat the host's locator canonicalization at the API trust boundary."""
        if value is None:
            return None
        return value.strip() or None


class WorkerSandboxAbandonRequest(BaseModel):
    """Release a failed activation, explicitly marking ambiguous context LOST."""

    sandbox_generation: int = Field(..., ge=1)
    activation_generation: int = Field(..., ge=1)
    lost: bool
    detail: str | None = Field(None, max_length=1000)
    idle_timeout_seconds: float = Field(1800.0, gt=0, le=7 * 24 * 60 * 60)


class WorkerReleaseRequest(BaseModel):
    """A claimed turn handed back because it provably never began."""

    reason: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
        description=(
            "Why the turn was never started, in the audit trail's own "
            "vocabulary — 'launch_failed' when the process that would have run "
            "it could not be started or exited without taking delivery. This "
            "is not a way to abandon a turn that is running: the control plane "
            "refuses the release once the turn has been admitted."
        ),
    )


class WorkerFinishRequest(BaseModel):
    """Terminal transition reported by a worker."""

    state: str = Field(..., description="succeeded|failed|cancelled")
    detail: str | None = Field(
        None,
        description=(
            "Why the turn ended as it did. Kept to the first "
            f"{WORKER_FINISH_DETAIL_MAX_CHARS} characters; a longer one is trimmed, "
            "not refused."
        ),
    )
    setup_script: str | None = Field(
        None,
        max_length=8000,
        description=(
            "Shell run in the sandbox before the agent starts. What it adds "
            "to a new workspace remains in its retained container. Omitted inherits "
            "the selected environment; explicit null disables its install script."
        ),
    )
    base_ref: str | None = Field(
        None,
        max_length=255,
        description=(
            "Branch to work from. Resolved to a commit at creation and stored as "
            "base_sha — a branch moves, and the publisher applies onto a pinned commit. "
            "The draft PR targets this branch; jobs that name none target the "
            "deployment default."
        ),
    )
    base_sha: str | None = Field(
        None,
        pattern=r"^[0-9a-fA-F]{7,64}$",
        description=(
            "The commit the agent actually worked from. Recorded only when the job "
            "did not already carry one — the publisher cannot apply a patch without "
            "knowing its base, and a job may be submitted without naming a commit."
        ),
    )

    @field_validator("detail")
    @classmethod
    def _trim_detail(cls, value: str | None) -> str | None:
        """Keep the stored explanation bounded without refusing the finish."""
        if value is None or len(value) <= WORKER_FINISH_DETAIL_MAX_CHARS:
            return value
        return value[: WORKER_FINISH_DETAIL_MAX_CHARS - 1] + "\u2026"


class WorkerAckResponse(BaseModel):
    """Generic worker acknowledgement."""

    ok: bool
    state: str | None = None


class WorkerMeasurementRequest(BaseModel):
    """One finished measurement, as the producer froze it.

    Deliberately opaque below the envelope: the record is validated against the
    collector's own bounds and its facts whitelist by the route, because those
    constants live with the producer that produced it and a schema that
    restated them would be a second copy free to drift.
    """

    #: The frozen record, exactly as the producer emitted it.
    record: dict[str, Any]
    #: The experiment manifest, sent with the first record a producer delivers
    #: so the catalogue can be created. Omitted afterwards.
    run: dict[str, Any] | None = None


class WorkerMeasurementResponse(BaseModel):
    """What the control plane did with one measurement.

    ``result`` is an outcome rather than a bare acknowledgement because the
    producer has to distinguish three things it would otherwise guess at:
    stored, already stored (a retry that must not count twice), and refused
    (which is terminal and must not be retried).
    """

    result: Literal["stored", "duplicate", "conflict", "run_conflict", "rejected"]
    measurement_id: str | None = None
    reason: str | None = None
