"""Host enrollment, registration, heartbeat, and residency protocols."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves annotations at runtime.
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BaseModel, Field, StrictInt, ValidationError, model_validator

from cloud_agent.capacity_protocol import HostCapacityReport, parse_capacity_block
from cloud_agent.host_pool import ENROLL_TOKEN_TTL_S
from cloud_agent.schemas._host_reports import (
    HOST_LABELS_MAX_ITEMS,
    _HostCapability,
    _HostLabelValue,
    _HostReportKey,
    _HostReportVersion,
    _HostVersionMenu,
)
from cloud_agent.schemas._sandbox_manager_wire import _SandboxManagerWireNames
from cloud_agent.schemas.archive_settings import (
    HostSandboxArchiveSettings,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)
from cloud_agent.schemas.workers import (
    WorkerSandboxMember,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)
from cloud_agent_shared.host_settings import MAX_SETTINGS_KEYS
from cloud_agent_shared.sandbox_resources import (
    SandboxResourceSample,
    parse_sandbox_resource_sample,
)
from cloud_agent_shared.shared_protocol import HOST_LAUNCH_ERROR_MAX_CHARS

# The host name is charset-checked in the same shape and for the same reason
# as ``WorkerClaimRequest.host``: it is a primary key an operator reads off a
# page and clicks, and it is interpolated into nothing but parameters — but a
# machine that can write arbitrary bytes into the pool list can make the list
# unreadable, which is a denial of the one tool for fixing a bad host.
HOST_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


class IssueHostEnrollTokenRequest(BaseModel):
    """Admin request for a one-time host enrollment token."""

    host: str = Field(
        ...,
        max_length=253,
        pattern=HOST_NAME_PATTERN,
        description=(
            "The name this token may register, chosen here rather than by the "
            "machine that redeems it. Registration upserts, so a name already "
            "in use has its credential revoked and its row reset to "
            "'enrolling' — a typo on a machine being set up would otherwise "
            "evict a live host from the pool. Naming an existing host is how a "
            "rebuilt machine is deliberately re-enrolled."
        ),
    )
    ttl_seconds: float = Field(
        ENROLL_TOKEN_TTL_S,
        gt=0,
        le=86400.0,
        description=(
            "How long the token may be redeemed for. Short by default: it is "
            "pasted into a machine being set up right now, and a token still "
            "live tomorrow is a credential nobody is tracking."
        ),
    )


class IssueHostEnrollTokenResponse(BaseModel):
    """The enrollment token, shown exactly once."""

    enroll_token: str = Field(
        ...,
        description=(
            "Single-use. Only its hash is stored, so this value cannot be "
            "recovered — issue another one rather than looking it up."
        ),
    )
    host: str = Field(
        ...,
        description=(
            "The only name this token can register. Echoed back so the value "
            "the token was bound to is visible next to the token itself."
        ),
    )
    expires_at: datetime


class HostRegisterRequest(BaseModel):
    """A machine presenting an enrollment token to join the pool."""

    enroll_token: str = Field(..., min_length=1, max_length=512)
    name: str = Field(
        ...,
        max_length=253,
        pattern=HOST_NAME_PATTERN,
        description=(
            "This machine's name in the pool. It must be the name the token "
            "was issued for — the token names its host, so a machine cannot "
            "register under a name an admin did not choose for it."
        ),
    )
    capabilities: list[_HostCapability] = Field(
        default_factory=list,
        max_length=64,
        description=(
            "What this machine can run — runtime tiers, sandbox backends, "
            "whatever a job may ask for. A job goes to a host only when the "
            "host declares everything the job requires."
        ),
    )
    slots_total: int = Field(
        ...,
        gt=0,
        le=1024,
        description="Concurrent attempts this enrolled host accepts.",
    )
    sandbox_manager_url: str = Field(
        ...,
        validation_alias=AliasChoices("sandbox_manager_url", "broker_url"),
        max_length=2048,
        description=(
            "Where this host's sandbox manager listens, as the control plane "
            "reaches it. Every executable attempt records an enrolled host, and "
            "files, terminals and Git resolve the sandbox manager from that host row. "
            "Omitting it leaves the host unready and unable to serve chats."
        ),
    )
    runner_version: str | None = Field(None, max_length=128)
    labels: dict[_HostReportKey, _HostLabelValue] = Field(
        default_factory=dict, max_length=HOST_LABELS_MAX_ITEMS
    )


class HostRegisterResponse(_SandboxManagerWireNames):
    """The host's identity and the credential it authenticates with."""

    host: str
    credential: str = Field(
        ...,
        description=(
            "Long-lived host credential. Shown once; only its hash is stored. "
            "Revoking the host clears that hash and the credential dies."
        ),
    )
    status: str
    broker_error: str | None = Field(
        default=None, deprecated=True, description="Compatibility alias for sandbox_manager_error."
    )
    sandbox_manager_error: str | None = Field(
        None,
        description=(
            "Set when the control plane could not use the sandbox_manager_url this host "
            "reported: unreachable from here, or refusing the shared sandbox manager "
            "credential. Enrolment still succeeds — a sandbox manager is often still "
            "starting — but every file, terminal and git request for this "
            "host's jobs will fail until it is fixed."
        ),
    )


class ResidencyBeat(BaseModel):
    """One hot sandbox a host reports alive."""

    thread_id: str = Field(..., min_length=1, max_length=128)
    generation: int = Field(..., ge=1)


class SandboxCheckpointMember(BaseModel):
    """One chat's native-session position inside a checkpoint."""

    thread_id: str = Field(..., min_length=1, max_length=128)
    native_session_id: str | None = Field(None, max_length=256)
    last_settled_job_id: str | None = Field(None, max_length=256)
    context_status: str | None = Field(None, max_length=32)


class SandboxCheckpointBeat(BaseModel):
    """The latest restorable checkpoint a host stored for one retained group."""

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
    format_version: Literal[1] = 1


class RetainedSandboxBeat(BaseModel):
    """Docker-observed lifecycle of one retained chat sandbox on this host."""

    thread_id: str = Field(..., min_length=1, max_length=128)
    sandbox_id: str = Field(..., min_length=1, max_length=128)
    container_id: str = Field(..., min_length=12, max_length=128)
    generation: int = Field(..., ge=1)
    activation_generation: int = Field(..., ge=0)
    running: bool
    activity_active: bool | None = Field(
        None,
        description=(
            "Whether this exact supervisor generation currently has a turn, "
            "terminal, or bounded workspace operation open. Null means the "
            "sandbox manager could not prove activity state after reconstruction."
        ),
    )
    idle_remaining_seconds: float | None = Field(
        None,
        ge=0,
        le=7 * 24 * 60 * 60,
        description=(
            "Remaining settled-idle delay observed from the host monotonic clock. "
            "Null is unknown or currently active, not an immediate deadline."
        ),
    )
    shared_live: Literal[True] = True
    members: list[WorkerSandboxMember] = Field(default_factory=list, max_length=128)
    resource_usage: SandboxResourceSample | None = None
    terminal_count: StrictInt | None = Field(None, ge=0, le=4096)
    checkpoint: SandboxCheckpointBeat | None = None

    @model_validator(mode="before")
    @classmethod
    def _drop_unusable_resources(cls, data: Any) -> Any:
        """Optional diagnostics must never refuse a valid lifecycle heartbeat."""
        if not isinstance(data, dict):
            return data
        result = dict(data)
        if result.get("resource_usage") is not None:
            result["resource_usage"] = parse_sandbox_resource_sample(result["resource_usage"])
        if result.get("checkpoint") is not None:
            try:
                result["checkpoint"] = SandboxCheckpointBeat.model_validate(result["checkpoint"])
            except ValidationError:
                result["checkpoint"] = None
        count = result.get("terminal_count")
        if count is not None and (
            isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 4096
        ):
            result["terminal_count"] = None
        return result


class RetainedSandboxStop(BaseModel):
    """Exact durable fence authorizing a host to stop one retained container."""

    thread_id: str = Field(..., min_length=1, max_length=128)
    generation: int = Field(..., ge=1)
    # A fork target is born stopped at generation zero. If the sandbox manager starts
    # it for a file/terminal operation and then restarts before the first agent
    # turn, the durable idle-stop fence still legitimately names generation
    # zero. Rejecting it here leaks that reconstructed container in RUNNING.
    activation_generation: int = Field(..., ge=0)
    reason: Literal["idle", "orphaned_attempt", "lost"] = "idle"
    shared_live: Literal[True] = True
    member_activation_generations: (
        dict[Annotated[str, Field(min_length=1, max_length=256)], Annotated[int, Field(ge=0)]]
        | None
    ) = Field(None, max_length=128)


class HostHeartbeatRequest(BaseModel):
    """A host reporting that it is still there, and what it now looks like.

    Every field is optional and only overwrites when present, so a heartbeat
    that carries nothing but liveness does not blank out what enrollment
    declared.
    """

    physical_host: str | None = Field(
        None,
        min_length=1,
        max_length=255,
        pattern=r"^\S(?:[^\r\n]*\S)?$",
        description="Physical server name for admin grouping only; never a scheduling or credential identity.",
    )
    slots_total: int | None = Field(None, gt=0, le=1024)
    launch_error: str | None = Field(
        None,
        max_length=HOST_LAUNCH_ERROR_MAX_CHARS,
        description=(
            "Why this host is claiming nothing despite having room. The empty "
            "string is a positive report that nothing is wrong and clears what "
            "is stored; omitting the field leaves it, the same way an "
            "unreported runtime does."
        ),
    )
    runner_version: str | None = Field(None, max_length=128)
    sandbox_manager_url: str | None = Field(
        None,
        max_length=2048,
        validation_alias=AliasChoices("sandbox_manager_url", "broker_url"),
    )
    capabilities: list[_HostCapability] | None = Field(None, max_length=64)
    sandbox_runtime: str | None = Field(
        None,
        max_length=128,
        description=(
            "The container runtime jobs actually execute under, e.g. "
            "'io.containerd.kata.v2'. Empty string is a positive statement — "
            "shared-kernel runc — where omitting the field means 'not "
            "reported' and keeps the last known value. This is the isolation "
            "attestation the scheduler and admin read; it must say what runs, "
            "not what the deployment intended."
        ),
    )
    sandbox_image_digest: str | None = Field(
        None,
        max_length=512,
        description=(
            "Content digest of the sandbox image this host launches, from the "
            "local image store (RepoDigest, or the local image Id for a "
            "never-pushed build). Stamped onto attempts at claim so a retry "
            "can know exactly what executed the original."
        ),
    )
    runtime_versions: dict[_HostReportKey, _HostReportVersion] | None = Field(
        None,
        max_length=64,
        description=(
            "The *default* CLI version per runtime id (e.g. "
            "{'claude-code': '1.2.3'}), read from image labels. An empty dict "
            "means 'inspected, image carries no version labels'."
        ),
    )
    runtime_versions_available: dict[_HostReportKey, _HostVersionMenu] | None = Field(
        None,
        max_length=64,
        description=(
            "Every CLI version this image holds, keyed by runtime id, default "
            "first — the menu an admin may select from. Null means not "
            "reported and leaves the control plane's last knowledge in place."
        ),
    )
    capacity: HostCapacityReport | None = Field(
        None,
        description=(
            "Optional host CPU / memory / retained-container observation, "
            "taken on the runner's own clock within the last five minutes. A "
            "runner that does not collect it simply omits the field, and one "
            "whose block is unusable has the block dropped rather than the "
            "heartbeat refused."
        ),
    )
    residencies: list[ResidencyBeat] | None = Field(
        None,
        max_length=128,
        description=(
            "The hot sandboxes this host still holds, generation-scoped. Each "
            "listed residency's lease is extended; one that no longer holds "
            "its generation comes back in residencies_lost and the host tears "
            "that sandbox down (design §7)."
        ),
    )
    retained_sandboxes: list[RetainedSandboxBeat] | None = Field(
        None,
        max_length=4096,
        description=(
            "Exact Docker observations for restartable chat containers. The "
            "control plane reconciles only matching host/sandbox/container "
            "generations and never treats an omitted row as deletion."
        ),
    )
    settings_applied: dict[str, int | float] | None = Field(
        None,
        max_length=MAX_SETTINGS_KEYS,
        description=(
            "The runtime settings this host is applying now, by key — the "
            "sandbox manager's as it last confirmed them. Null means not "
            "reported and keeps what is stored."
        ),
    )
    settings_local: dict[str, int | float] | None = Field(
        None,
        max_length=MAX_SETTINGS_KEYS,
        description=(
            "The host's original bootstrap environment values, for diagnostics "
            "and one-time migration. Null means not reported and keeps what is stored."
        ),
    )
    sandbox_archive_settings_applied_revision: StrictInt | None = Field(
        None,
        ge=0,
        description=(
            "The archive-policy revision the authenticated sandbox manager confirmed. "
            "Explicit null clears a stale confirmation after restart; omitting the field "
            "keeps the last observation for compatibility with older runners."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_unusable_capacity(cls, data: Any) -> Any:
        """Turn an unusable capacity block into an absent one.

        The block is optional, and its absence is a first-class answer: an
        older runner sends none, and a newer one may send a version or a shape
        this control plane does not know. Refusing the whole heartbeat over it
        would trade a gap in a graph for a host that reads as dead — so the
        block is dropped here, before validation, and the liveness fields are
        validated exactly as strictly as they were.
        """
        if (
            isinstance(data, dict)
            and data.get("capacity") is not None
            and parse_capacity_block(data["capacity"]) is None
        ):
            return {**data, "capacity": None}
        return data


class HostResidencyEstablishResponse(BaseModel):
    """Attempt-fenced residency granted to a supervisor-owned member process."""

    thread_id: str
    generation: int
    ttl_seconds: float
    credential: str | None = None
    model: str | None = None


class HostHeartbeatResponse(_SandboxManagerWireNames):
    """What the control plane thinks of this host after the heartbeat."""

    host: str
    status: str
    slots_total: int | None = None
    slots_used: int = 0
    slots_desired: int | None = Field(
        None,
        description=(
            "Turns at once an operator asked this host to run, or null for the "
            "host's own default. The host serves it as its limit from here on; "
            "the claim already admits against the lower of this and the limit "
            "the host last reported."
        ),
    )
    settings: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "The complete runtime settings for this host: per-host overrides, "
            "then global admin values, then built-in defaults. Clearing an "
            "override returns the next level's value, never a legacy environment value."
        ),
    )
    sandbox_archive_settings: HostSandboxArchiveSettings
    residencies_lost: list[ResidencyBeat] = Field(
        default_factory=list,
        description=(
            "The reported beats whose generation no longer holds its "
            "residency — echoed back with the generation, so a late answer "
            "to an old beat cannot end the successor the host has since "
            "established. The host fences those member processes; the retained "
            "container still owns their native session files."
        ),
    )
    retained_sandboxes_stop: list[RetainedSandboxStop] = Field(
        default_factory=list,
        description=(
            "Settled-idle or failed-attempt Docker stop commands fenced to the "
            "durable container and activation generations reported by this host."
        ),
    )
    broker_error: str | None = Field(
        default=None, deprecated=True, description="Compatibility alias for sandbox_manager_error."
    )
    sandbox_manager_error: str | None = Field(
        None,
        description=(
            "Set when the control plane could not use the sandbox_manager_url this host "
            "reported. Checked at most once a minute, and always when the "
            "reported address changes."
        ),
    )
    heartbeat_interval_seconds: float = Field(
        ...,
        description=(
            "How often to report in. Missing three consecutive intervals is "
            "what moves a host to offline."
        ),
    )
