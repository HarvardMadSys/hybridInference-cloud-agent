"""Request/response schemas for the agent-sandbox job API (issue #1041)."""

from __future__ import annotations

import base64
import binascii

# Runtime import, and it has to be: this module has `from __future__ import
# annotations`, but pydantic still resolves every annotation when it builds the
# model, so hiding this one behind TYPE_CHECKING leaves `AgentRunnerHost`
# silently unbuilt and raises at the first request instead of at import.
from datetime import datetime  # noqa: TC003
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from cloud_agent.entitlement import REPO_PATTERN
from cloud_agent.host_pool import ENROLL_TOKEN_TTL_S

# A lease is the only thing that lets the reaper take a job back from a stuck
# or malicious worker. If the worker could pick the TTL, it could pick one long
# enough that the lease never expires — and then the capability token bound to
# that attempt would be neither self-revoking nor cancellable by the owner. The
# server therefore caps it, and 15 minutes is far above any legitimate gap
# between heartbeats.
MAX_LEASE_TTL_SECONDS = 900.0

# Normalized event kinds (issue #1041) plus the control events the platform
# appends. The pattern is the security-relevant part: an event type is
# interpolated into the SSE ``event:`` field, so anything containing a newline
# would let a worker inject arbitrary frames into the owner's stream. Keeping
# the charset to lowercase/digits/underscore makes that structurally impossible
# while still letting runtime adapters introduce new kinds.
EVENT_TYPE_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"

# Where a job's `base_ref` lives once it is persisted. The column stores the
# resolved `base_sha` — a branch moves — so the branch the owner actually
# picked is kept alongside it in `metadata` under a reserved key, which the API
# strips back out on the way to the client. Reserved means the creation path
# drops any caller-supplied copy: it is a fact the server resolved, and the
# publisher targets the PR at it, so a caller must not be able to forge one.
BASE_REF_METADATA_KEY = "_agent_base_ref"


class AgentJobCreate(BaseModel):
    """Request body for creating an agent job."""

    repo: str = Field(
        ...,
        # Shape-checked here as well as against the entitlement allowlist: the
        # value is interpolated into a clone URL and handed to git, which reads
        # a leading `-` as an option wherever it appears.
        pattern=REPO_PATTERN,
        max_length=140,
        description="Target repository, e.g. 'owner/name'.",
    )
    task_prompt: str = Field(..., description="What the agent should do.")
    runtime: str = Field("claude-code", description="Agent runtime id.")
    model: str = Field(..., description="Gateway model id the runtime should use.")
    setup_script: str | None = Field(
        None,
        max_length=8000,
        description=(
            "Shell run before the agent, under the setup egress tier. What it adds "
            "to the worktree is cached per repository, script, and sandbox image, "
            "so a retry does not reinstall."
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
        # A bare commit hash, enforced here as well as in the publisher: git
        # reads a leading `-` as an option even where an operand is expected,
        # so an unconstrained ref would be an argument injection into the
        # trusted process that holds the repository credential.
        pattern=r"^[0-9a-fA-F]{7,64}$",
        description="Commit SHA to work from.",
    )
    mcp_servers: list[str] | None = Field(
        None,
        max_length=16,
        description=(
            "MCP servers from this deployment's registry to expose to the agent. "
            "Omitted takes the registry's defaults; an explicit [] means none. Each "
            "name must be configured, and the runtime must support MCP."
        ),
    )
    metadata: dict[str, Any] | None = Field(None, description="Opaque caller metadata.")


class AgentJobResponse(BaseModel):
    """One agent job as returned to its owner."""

    id: str
    thread_id: str | None = None
    parent_job_id: str | None = None
    turn_no: int = 1
    repo: str
    task_prompt: str
    runtime: str
    model: str
    base_ref: str | None = None
    base_sha: str | None = None
    output_branch: str | None = None
    state: str
    cancel_requested: bool = False
    current_attempt_id: int | None = None
    published_pr_url: str | None = None
    published_commit_sha: str | None = None
    detail: str | None = None
    # Set on turns created by a fork: the original turn this row copies.
    forked_from_job_id: str | None = None
    metadata: dict[str, Any] | None = None
    # Names only. The owner never sees a server's URL or credential — those are
    # the two things proxying through the gateway exists to keep off the wire.
    mcp_servers: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    pinned_at: str | None = None
    # Read from the billing ledger, never from anything the agent reports
    # about itself. Attribution only: there is no per-task cap to compare it
    # against, and the account's daily quota is what actually governs spend.
    spent_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    model_calls: int | None = None
    # The deployment's egress posture for this job's two phases, so the owner
    # can see what the sandbox could reach rather than take it on trust.
    setup_egress_tier: str | None = None
    agent_egress_tier: str | None = None
    # Source control connection state. The composer is gated on this rather
    # than offering pickers that submit something else: with no App installed
    # there is no repository to work on, and a task box that looks ready is a
    # worse answer than one that says what is missing.
    github_connected: bool = False
    github_install_url: str | None = None


class AgentJobListResponse(BaseModel):
    """A page of agent jobs."""

    jobs: list[AgentJobResponse]


class AgentProject(BaseModel):
    """One repo the caller has run tasks in, summarized for the task tree."""

    repo: str
    # Conversations, not turns — the sidebar shows one row per conversation.
    task_count: int
    # Non-terminal jobs, so a collapsed project can still show live work.
    active_count: int
    last_activity_at: str | None = None
    pinned_count: int = 0
    pinned_at: str | None = None


class AgentProjectListResponse(BaseModel):
    """Every project the caller has tasks in, most recently active first."""

    projects: list[AgentProject]


class AgentJobEvent(BaseModel):
    """One normalized event from an agent job's append-only stream."""

    id: int
    attempt_id: int
    seq: int
    event_type: str
    payload: dict[str, Any] | None = None
    created_at: str | None = None


class AgentJobEventsResponse(BaseModel):
    """A page of events plus the cursor to resume from."""

    events: list[AgentJobEvent]
    next_cursor: int


class AgentJobCancelResponse(BaseModel):
    """Result of requesting cancellation."""

    id: str
    state: str
    cancel_requested: bool


class AgentThreadArchiveResponse(BaseModel):
    """Archive state for the thread containing a requested job."""

    thread_id: str
    archived: bool
    archived_at: str | None = None


class AgentThreadPinResponse(BaseModel):
    """Pin state for the thread containing a requested job."""

    thread_id: str
    pinned: bool
    pinned_at: str | None = None


class AgentFollowUpRequest(BaseModel):
    """A new user turn appended to an existing task thread."""

    prompt: str = Field(..., min_length=1, max_length=100_000)
    runtime: str | None = Field(None, description="Optional harness override for this turn.")
    model: str | None = Field(None, description="Optional model override for this turn.")

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        """Reject visually empty turns and persist the user's trimmed text."""
        prompt = value.strip()
        if not prompt:
            raise ValueError("prompt must not be blank")
        return prompt


class AgentThreadMessageResponse(BaseModel):
    """One durable user or assistant message in a task thread."""

    id: int
    role: str
    content: str
    job_id: str
    created_at: str | None = None


class AgentThreadResponse(BaseModel):
    """Conversation context and runs for the thread containing a job."""

    thread_id: str
    repo: str
    title: str
    messages: list[AgentThreadMessageResponse]
    jobs: list[AgentJobResponse]
    created_at: str | None = None
    updated_at: str | None = None


class AgentJobArtifactResponse(BaseModel):
    """One stored artifact (e.g. the produced patch)."""

    job_id: str
    attempt_id: int
    kind: str
    content: str
    created_at: str | None = None


class AgentWorkspaceEntry(BaseModel):
    """One safe child in an agent job's read-only workspace browser."""

    name: str
    path: str
    kind: Literal["file", "directory", "symlink"]
    size: int | None = None
    binary: bool = False
    truncated: bool = False
    status: Literal["added", "modified", "deleted"] | None = None
    omitted_reason: str | None = None


class AgentWorkspaceResponse(BaseModel):
    """A directory listing or bounded text-file preview from a job workspace."""

    path: str
    kind: Literal["file", "directory", "symlink"]
    entries: list[AgentWorkspaceEntry] | None = None
    content: str | None = None
    size: int | None = None
    binary: bool = False
    truncated: bool = False
    status: Literal["added", "modified", "deleted"] | None = None
    omitted_reason: str | None = None
    writable: bool = False
    source: Literal["workspace", "snapshot"] = "snapshot"


class AgentWorkspaceWriteRequest(BaseModel):
    """Replace one UTF-8 file in a live worktree."""

    content: str = Field(..., max_length=2 * 1024 * 1024)


class AgentTerminalRequest(BaseModel):
    """Execute one user-entered shell command in the workspace sandbox."""

    command: str = Field(..., min_length=1, max_length=8000)
    cwd: str = Field("/workspace", min_length=1, max_length=4096)
    timeout_seconds: float = Field(60.0, gt=0, le=120)


class AgentTerminalResponse(BaseModel):
    """Bounded output and resulting working directory for one command."""

    output: str
    stderr: str
    exit_code: int
    cwd: str


_MAX_TERMINAL_INPUT_BYTES = 64 * 1024
_MAX_TERMINAL_INPUT_BASE64_CHARS = 4 * ((_MAX_TERMINAL_INPUT_BYTES + 2) // 3)


class AgentTerminalSessionCreateRequest(BaseModel):
    """Open one interactive PTY after an agent run has settled."""

    rows: int = Field(24, ge=2, le=200)
    cols: int = Field(80, ge=20, le=500)


class AgentTerminalSessionInputRequest(BaseModel):
    """Base64-encoded bytes to write to an interactive PTY."""

    data: str = Field(..., min_length=1, max_length=_MAX_TERMINAL_INPUT_BASE64_CHARS)

    @field_validator("data")
    @classmethod
    def valid_bounded_base64(cls, value: str) -> str:
        """Reject malformed or oversized input before it reaches the broker."""
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("data must be valid base64") from exc
        if len(decoded) > _MAX_TERMINAL_INPUT_BYTES:
            raise ValueError("decoded terminal input must be at most 65536 bytes")
        return value


class AgentTerminalSessionResizeRequest(BaseModel):
    """Resize an interactive PTY."""

    rows: int = Field(..., ge=2, le=200)
    cols: int = Field(..., ge=20, le=500)


class AgentTerminalSessionResponse(BaseModel):
    """Owner-safe metadata for one interactive terminal session."""

    id: str = Field(..., pattern=r"^term_[A-Za-z0-9_-]{1,80}$")
    shell: str = Field(..., min_length=1, max_length=4096)
    state: str = Field(..., min_length=1, max_length=32)
    cwd: str = Field(..., min_length=1, max_length=4096)
    rows: int = Field(..., ge=2, le=200)
    cols: int = Field(..., ge=20, le=500)
    last_seq: int = Field(..., ge=0)


class AgentTerminalSessionListResponse(BaseModel):
    """Interactive terminal sessions retained by one live workspace."""

    terminals: list[AgentTerminalSessionResponse] = Field(default_factory=list)


class AgentGitChange(BaseModel):
    """One worktree status row."""

    code: str
    path: str


class AgentGitCommit(BaseModel):
    """One recent commit visible from the worktree."""

    sha: str
    short_sha: str
    subject: str
    author: str
    authored_at: str


class AgentGitWorkspaceResponse(BaseModel):
    """Live diff/status/commit data, or an unavailable legacy-workspace marker."""

    available: bool
    branch: str = ""
    changes: list[AgentGitChange] = Field(default_factory=list)
    patch: str = ""
    commits: list[AgentGitCommit] = Field(default_factory=list)


# ── Worker-facing (capability-token authenticated) ─────────────────────


class WorkerClaimRequest(BaseModel):
    """Worker request to claim the next queued job."""

    worker_id: str = Field(..., description="Stable identifier of the claiming worker.")
    lease_ttl_seconds: float = Field(
        120.0,
        gt=0,
        le=MAX_LEASE_TTL_SECONDS,
        description="How long the lease is valid without a heartbeat.",
    )
    # Bounded and charset-checked because it is a primary key an admin reads
    # off a page and clicks: an unconstrained field here lets a runner write
    # whatever it likes into the machine list.
    host: str | None = Field(
        None,
        max_length=253,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description=(
            "Machine this runner sits on, shared by its replicas. Joins the "
            "pool an operator can pin agent jobs to. Omitted by runners that "
            "predate host reporting."
        ),
    )


class WorkerClaimResponse(BaseModel):
    """A claimed job plus the capability token scoped to this attempt."""

    job_id: str
    thread_id: str | None = None
    parent_job_id: str | None = None
    turn_no: int = 1
    attempt_id: int
    attempt_no: int
    repo: str
    base_sha: str | None = None
    task_prompt: str
    setup_script: str | None = None
    runtime: str
    model: str
    worker_token: str
    sandbox_token: str = Field(
        "",
        description=(
            "The inference credential that enters the sandbox: a gateway grant, "
            "scoped to the models this attempt may call."
        ),
    )
    mcp_token: str = Field(
        "",
        description=(
            "The sandbox's *second* credential, for this service's MCP relay. "
            "Scoped `mcp`, so it opens the relay and nothing else — not the "
            "worker surface, and not the gateway. Empty when this deployment "
            "serves no MCP."
        ),
    )
    mcp_base_url: str = Field(
        "",
        description=(
            "The relay as the sandbox reaches it. Empty means no MCP for this "
            "run, which a runtime treats as a refusal rather than resolving "
            "servers against the inference address."
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
    context_messages: list[dict[str, str]] = Field(
        default_factory=list,
        description="Prior platform conversation turns, excluding the current prompt.",
    )
    context_patch: str | None = Field(
        None,
        description="Successful parent patch to rehydrate before this follow-up runs.",
    )
    mcp_servers: list[str] = Field(
        default_factory=list,
        description=(
            "MCP server names this job may reach through the gateway proxy. The runner "
            "passes them to the runtime adapter; it never learns their addresses."
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


class WorkerEventRequest(BaseModel):
    """One normalized event reported by a worker."""

    event_type: str = Field(
        ...,
        pattern=EVENT_TYPE_PATTERN,
        description="thinking|message|tool_use|...|lifecycle",
    )
    payload: dict[str, Any] | None = None


class WorkerEventResponse(BaseModel):
    """Accepted event id (the SSE cursor value)."""

    event_id: int


class WorkerTerminalSuspendRequest(BaseModel):
    """Protected workspace phase that requires every owner PTY to pause."""

    phase: Literal["workspace_preparing", "workspace_finalizing"]


class WorkerArtifactRequest(BaseModel):
    """An artifact produced by a worker (e.g. the git patch)."""

    kind: str = Field(..., description="Artifact kind, e.g. 'patch'.")
    content: str


class WorkerArtifactResponse(BaseModel):
    """Stored artifact id."""

    artifact_id: int


class WorkerFinishRequest(BaseModel):
    """Terminal transition reported by a worker."""

    state: str = Field(..., description="succeeded|failed|cancelled")
    detail: str | None = None
    setup_script: str | None = Field(
        None,
        max_length=8000,
        description=(
            "Shell run before the agent, under the setup egress tier. What it adds "
            "to the worktree is cached per repository, script, and sandbox image, "
            "so a retry does not reinstall."
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


class WorkerPublishRequest(BaseModel):
    """Publisher result recorded against the one-shot publish transition."""

    pr_url: str


class WorkerAckResponse(BaseModel):
    """Generic worker acknowledgement."""

    ok: bool
    state: str | None = None


class McpServerSummary(BaseModel):
    """One MCP server the composer may offer.

    Public fields only: a server's URL and its credential stay on the gateway,
    so neither has a field here to leak into.
    """

    name: str
    description: str = ""
    tools: list[str] = Field(
        default_factory=list,
        description="Tools exposed. Empty means every tool the server offers.",
    )
    default: bool = False


class AgentConfigResponse(BaseModel):
    """What this deployment will actually accept, for the task composer.

    The composer used to show a repository, a branch, a runtime and a model as
    static labels while submitting different hardcoded values — so the UI
    described a job nobody was running. These are the real answers.
    """

    repos: list[str] = Field(
        default_factory=list,
        description="Repositories this deployment is entitled to work on. Empty means none.",
    )
    runtimes: list[str] = Field(default_factory=list, description="Runtime ids that can run here.")
    models: list[str] = Field(
        default_factory=list,
        description=(
            "Model ids an agent job can actually call for this user — the same "
            "predicate the create endpoint enforces, not the /v1/models list."
        ),
    )
    mcp_servers: list[McpServerSummary] = Field(
        default_factory=list,
        description="MCP servers this deployment offers. Empty means none are configured.",
    )
    mcp_runtimes: list[str] = Field(
        default_factory=list,
        description=(
            "Runtime ids that can be given MCP servers. A runtime outside this list "
            "refuses a job that names any, rather than dropping them silently."
        ),
    )
    setup_egress_tier: str | None = None
    agent_egress_tier: str | None = None
    # Source control connection state. The composer is gated on this rather
    # than offering pickers that submit something else: with no App installed
    # there is no repository to work on, and a task box that looks ready is a
    # worse answer than one that says what is missing.
    github_connected: bool = False
    github_install_url: str | None = None


class GitHubConnectRequest(BaseModel):
    """The callback values GitHub hands back after the user authorizes."""

    code: str = Field(..., min_length=1, max_length=512)
    state: str = Field(..., min_length=16, max_length=512)


class GitHubConnectionResponse(BaseModel):
    """Which GitHub installations this user has connected."""

    connections: list[dict[str, Any]] = Field(default_factory=list)
    repos: list[str] = Field(default_factory=list)
    install_url: str | None = None


class SourceControlAccount(BaseModel):
    """Public identity metadata for a connected provider account."""

    id: str
    label: str
    web_url: str | None = None


class SourceControlRepository(BaseModel):
    """A provider-attested repository safe to render in the UI."""

    id: str
    name: str
    web_url: str | None = None


class SourceControlProviderResponse(BaseModel):
    """Connection and capability status for one supported provider."""

    provider: Literal["github", "gitlab"]
    configured: bool
    connected: bool
    connect_url: str | None = None
    manage_url: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    accounts: list[SourceControlAccount] = Field(default_factory=list)
    repositories: list[SourceControlRepository] = Field(default_factory=list)
    error: str | None = None


class SourceControlIntegrationsResponse(BaseModel):
    """All source-control providers available to the current user."""

    providers: list[SourceControlProviderResponse]


class OAuthConnectRequest(BaseModel):
    """Authorization callback values supplied by GitHub or GitLab."""

    code: str = Field(..., min_length=1, max_length=2048)
    state: str = Field(..., min_length=16, max_length=512)


class RepoBranchesResponse(BaseModel):
    """Branches of one repository the caller is entitled to."""

    default: str | None = None
    branches: list[str] = Field(default_factory=list)


# The runner-host pool. These three lived in the gateway's `schemas_admin.py`
# alongside its own admin schemas; the pool is this service's to schedule, so
# they move here rather than leaving `api/admin_hosts.py` importing across the
# split. Copied unchanged.


class AgentRunnerHost(BaseModel):  # type: ignore[no-any-unimported]
    """One machine in the agent runner pool.

    Everything below ``seconds_since_seen`` arrived with the dynamic pool
    (phase G) and every one of those fields has a default. A host that joined
    by polling — the only way in before enrollment existed — has none of them
    set, and it must still render on the admin page rather than 500 it.
    """

    host: str
    active: bool
    last_worker_id: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    seconds_since_seen: float = Field(
        ...,
        description=(
            "Age of the last claim poll, by the server's clock. A runner polls "
            "only between jobs, so a busy machine can look quiet for as long as "
            "its longest job."
        ),
    )
    status: str = Field(
        "online",
        description=(
            "Pool lifecycle state: enrolling, online, draining, disabled or "
            "offline. Only an online host takes new claims."
        ),
    )
    enrolled: bool = Field(
        False,
        description=(
            "Whether this host holds a credential issued by enrollment. False "
            "means it joined by polling and is not expected to heartbeat, so "
            "the offline reaper leaves it alone."
        ),
    )
    labels: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[str] = Field(
        default_factory=list,
        description="What this machine can run. A job may only go to a host declaring all of it.",
    )
    slots_total: int | None = Field(
        None,
        description="Concurrent attempts this host accepts. Null means unmetered.",
    )
    slots_used: int = Field(0, description="Attempts currently holding one of this host's slots.")
    runner_version: str | None = None
    broker_url: str | None = Field(
        None,
        description="Where this host's workspace broker listens, as the control plane reaches it.",
    )
    last_heartbeat_at: datetime | None = None


class ListAgentRunnerHostsResponse(BaseModel):  # type: ignore[no-any-unimported]
    """The runner pool and which member currently takes jobs."""

    hosts: list[AgentRunnerHost]
    active_host: str | None = Field(
        None,
        description="Pinned host, or null when any runner may claim.",
    )


class SetActiveAgentRunnerHostRequest(BaseModel):  # type: ignore[no-any-unimported]
    """Pin agent jobs to one host, or unpin with ``null``."""

    host: str | None = Field(
        None,
        max_length=253,
        description="A host that has polled at least once, or null to unpin.",
    )


# ── Host pool: enrollment and heartbeat ────────────────────────────────
#
# The host name is charset-checked in the same shape and for the same reason
# as ``WorkerClaimRequest.host``: it is a primary key an operator reads off a
# page and clicks, and it is interpolated into nothing but parameters — but a
# machine that can write arbitrary bytes into the pool list can make the list
# unreadable, which is a denial of the one tool for fixing a bad host.
HOST_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


class IssueHostEnrollTokenRequest(BaseModel):  # type: ignore[no-any-unimported]
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


class IssueHostEnrollTokenResponse(BaseModel):  # type: ignore[no-any-unimported]
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


class HostRegisterRequest(BaseModel):  # type: ignore[no-any-unimported]
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
    capabilities: list[str] = Field(
        default_factory=list,
        max_length=64,
        description=(
            "What this machine can run — runtime tiers, sandbox backends, "
            "whatever a job may ask for. A job goes to a host only when the "
            "host declares everything the job requires."
        ),
    )
    slots_total: int | None = Field(
        None,
        gt=0,
        le=1024,
        description="Concurrent attempts to accept. Omit for unmetered (the pre-pool behaviour).",
    )
    broker_url: str | None = Field(
        None,
        max_length=2048,
        description=(
            "Where this host's workspace broker listens, as the control plane "
            "reaches it. Supply it on every enrolled host that runs jobs: once "
            "an attempt records this host, the broker is resolved from this "
            "row and the global AGENT_WORKSPACE_BROKER_URL is NOT consulted. "
            "Omitting it means file, terminal and git requests for this host's "
            "jobs answer 'no broker' (409) rather than being served from "
            "another machine's disk, which is the one fallback that would be "
            "actively wrong. The env var remains the answer only for attempts "
            "that record no host at all."
        ),
    )
    runner_version: str | None = Field(None, max_length=128)
    labels: dict[str, str] = Field(default_factory=dict)


class HostRegisterResponse(BaseModel):  # type: ignore[no-any-unimported]
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
        None,
        description=(
            "Set when the control plane could not use the broker_url this host "
            "reported: unreachable from here, or refusing the shared broker "
            "credential. Enrolment still succeeds — a broker is often still "
            "starting — but every file, terminal and git request for this "
            "host's jobs will fail until it is fixed."
        ),
    )


class HostHeartbeatRequest(BaseModel):  # type: ignore[no-any-unimported]
    """A host reporting that it is still there, and what it now looks like.

    Every field is optional and only overwrites when present, so a heartbeat
    that carries nothing but liveness does not blank out what enrollment
    declared.
    """

    slots_total: int | None = Field(None, gt=0, le=1024)
    runner_version: str | None = Field(None, max_length=128)
    broker_url: str | None = Field(None, max_length=2048)
    capabilities: list[str] | None = Field(None, max_length=64)


class HostHeartbeatResponse(BaseModel):  # type: ignore[no-any-unimported]
    """What the control plane thinks of this host after the heartbeat."""

    host: str
    status: str
    slots_total: int | None = None
    slots_used: int = 0
    broker_error: str | None = Field(
        None,
        description=(
            "Set when the control plane could not use the broker_url this host "
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


# ── Session (E8) ───────────────────────────────────────────────────────


class SessionLoginRequest(BaseModel):  # type: ignore[no-any-unimported]
    """The gateway identity token, traded once for a session cookie.

    The browser holds this for exactly one call: the callback page redeems its
    authorization code at the gateway and hands the result straight here. After
    that the token is spent and the cookie is the only credential in play, so a
    token intercepted afterwards buys nothing.
    """

    identity_token: str = Field(..., min_length=1, max_length=8192)


class SessionUserResponse(BaseModel):  # type: ignore[no-any-unimported]
    """Who the session says the caller is.

    ``id`` rather than ``user_id``: this is the shape ``web/src/lib/api/
    session.ts`` was written against, and it is the contract for all three
    session routes.
    """

    id: str
    email: str | None = None
    role: str
    user_name: str | None = None
