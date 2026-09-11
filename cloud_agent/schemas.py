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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cloud_agent.agent_access import AgentAccess  # noqa: TC001 — Pydantic resolves this at runtime.
from cloud_agent.capacity_protocol import (
    AgentHostCapacity,
    HostCapacityReport,
    parse_capacity_block,
)
from cloud_agent.entitlement import REPO_PATTERN
from cloud_agent.execution_profiles import ExecutionProfile
from cloud_agent.host_pool import ENROLL_TOKEN_TTL_S
from cloud_agent_host.event_payloads import bound_event_payload
from cloud_agent_host.shared_protocol import SHARED_MEMBER_ACCOUNTING_VERSION

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

#: What a follow-up sends to clear an inherited effort. A plain null cannot
#: mean this: null is what "no override, inherit" already sends, and the two
#: have to be distinguishable or a thread that once chose an effort could never
#: stop choosing one.
RESET_REASONING_EFFORT = "auto"

#: The reasoning-effort levels this platform offers, weakest first.
#:
#: **Fixed, not derived per model.** Deriving them was tried and withdrawn: the
#: providers' documented domains disagreed with what their APIs accept — Z.AI
#: documents low/high/max for glm-5.3 and its API accepts all seven, saying so
#: itself when handed a bad value ("reasoning_effort must be one of: none,
#: minimal, low, medium, high, xhigh, max") — so a domain copied from a doc is
#: a guess wearing a citation. Four levels everywhere is a claim that holds.
#:
#: Still narrowed by what a runtime can express, which is verified per pinned
#: CLI and load-bearing: claude-code turns a level it does not know into
#: `high` without complaint.
PLATFORM_REASONING_EFFORTS = ("low", "medium", "high", "max")


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
    reasoning_effort: str | None = Field(
        None,
        max_length=32,
        description=(
            "How hard the model should think. Null is Auto — no instruction, "
            "whatever the runtime and model do by default. A value must be one "
            "both the runtime and the chosen model accept (the intersection "
            "reported by /config); anything else is a 400 rather than a "
            "silently dropped or nearest-value setting."
        ),
    )
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

    @model_validator(mode="before")
    @classmethod
    def execution_profile_is_server_owned(cls, value: Any) -> Any:
        """Refuse an apparent profile choice instead of silently ignoring it."""
        if isinstance(value, dict) and "execution_profile" in value:
            raise ValueError(
                "execution_profile is server-owned; callers cannot select or downgrade it"
            )
        return value


class ToolCallResources(BaseModel):
    """Timing and attributable process resources for one tool call.

    Current shared turns leave whole-container sample fields empty because
    sibling members can execute concurrently. Kata may still attach exact
    exit-time accounting for child processes attributed to this call. Older
    rows can contain periodic sample windows; both shapes remain readable.

    A call that ran alongside others could not have its readings separated
    from theirs, so its numbers arrive in the ``shared`` fields with
    ``overlap_n`` above one. Those are upper bounds and must be presented as
    such; a reader that only understands the exclusive fields correctly shows
    nothing for them.
    """

    call_seq: int
    tool_use_id: str
    tool_name: str
    #: ``exact`` when the sandbox's own hooks marked the boundaries,
    #: ``message`` when the window is only when the platform parsed the
    #: call's events — which brackets the execution from outside.
    timing_source: str = "message"
    #: Null for a call that never returned — an outcome, not a missing number.
    duration_ms: int | None = None
    #: Readings taken inside this call's window. Below two, nothing is measured.
    sample_count: int = 0
    #: Calls whose windows shared these readings, including this one.
    overlap_n: int = 1
    #: How much of the call the readings actually span, so a reader can see
    #: that the numbers below are a floor rather than the whole call.
    measured_span_ms: int | None = None
    cpu_usec_exclusive: int | None = None
    cpu_usec_shared: int | None = None
    mem_peak_exclusive_bytes: int | None = None
    mem_peak_shared_bytes: int | None = None
    #: Kernel-observed processes whose starts plausibly belong to this call.
    #: Zero proves none only when the attempt's process observation is complete.
    process_count: int = 0
    #: What the processes this call alone started consumed, from the guest
    #: kernel's exit-time accounting. Independent of the sampler and of the
    #: call's length: a 35 ms call whose child exited has exact numbers here.
    #: CPU and I/O are sums over those processes; the peak is the largest
    #: single process. Null where no exited process was attributed to the
    #: call alone, or where the kernel does not account that counter.
    process_cpu_usec: int | None = None
    process_mem_peak_bytes: int | None = None
    process_io_read_bytes: int | None = None
    process_io_write_bytes: int | None = None
    #: True when a process this call may own was left out of the numbers:
    #: shared with an overlapping call, or still running at turn end. The
    #: numbers above are then floors, and a reader must say so.
    process_resources_partial: bool = False


class ProcessObservation(BaseModel):
    """One process created during a turn, observed by the Kata guest kernel."""

    process_seq: int
    uid: int
    pid: int
    tgid: int
    ppid: int | None = None
    comm: str
    executable: str | None = None
    #: Start relative to the turn's guest-observer cursor, not wall clock.
    started_offset_ms: int
    duration_ms: int | None = None
    exit_code: int | None = None
    observed_fork: bool = False
    observed_exec: bool = False
    observed_exit: bool = False
    #: Every plausible call when tool windows overlap; empty is unassigned.
    tool_call_seqs: list[int] = Field(default_factory=list)
    #: Exit-time accounting for the whole thread group, excluding children.
    #: Null until the leader exited, and null for any counter the guest
    #: kernel does not expose; never zero for something unmeasured.
    cpu_usec: int | None = None
    mem_peak_bytes: int | None = None
    io_read_bytes: int | None = None
    io_write_bytes: int | None = None
    #: Bytes passed through read/write, which count pipes and caches too.
    io_rchar_bytes: int | None = None
    io_wchar_bytes: int | None = None


class AttemptResources(BaseModel):
    """Attempt telemetry across historical samples and current guest observations.

    Every aggregate counter is optional and ``coverage`` is not. Current shared
    member turns intentionally report ``coverage = 0`` with null aggregate
    counters, then carry per-tool timing and optional UID-filtered Kata process
    rows. A reader must render that as "turn totals unavailable", not zero use.
    """

    cpu_usec: int | None = None
    mem_peak_bytes: int | None = None
    mem_avg_bytes: int | None = None
    io_read_bytes: int | None = None
    io_write_bytes: int | None = None
    net_rx_bytes: int | None = None
    net_tx_bytes: int | None = None
    sample_count: int = 0
    #: Fraction of the attempt window actually sampled, 0..1.
    coverage: float = 0.0
    sampler_version: str | None = None
    #: How long the turn spent inside tool calls — the union of their
    #: windows, never the sum, so it cannot exceed the turn. Null for a turn
    #: recorded before this was measured, which is not the same as zero.
    tool_time_ms: int | None = None
    #: One row per tool call this attempt issued, in the order issued.
    tool_calls: list[ToolCallResources] = Field(default_factory=list)
    #: True when the attempt issued more calls than ``tool_calls`` reports. A
    #: cap nobody is told about reads exactly like a shorter turn.
    tool_calls_truncated: bool = False
    #: Null where the sandbox was not a Kata guest or its helper was unavailable.
    process_observer: str | None = None
    #: Only true when the observer spanned the turn with no lost/truncated events.
    process_observation_complete: bool = False
    process_events_lost: int = 0
    processes_truncated: bool = False
    processes: list[ProcessObservation] = Field(default_factory=list)


class AgentAttemptSummary(BaseModel):
    """Identity, continuity and duration without unselected resource details."""

    attempt_id: int
    attempt_no: int
    continuity_mode: str | None = None
    context_status: Literal["new", "live", "restored", "lost"] | None = None
    duration_ms: int | None = None


class AgentAttemptTelemetry(AgentAttemptSummary):
    """Measurements fetched for one selected execution attempt."""

    resources: AttemptResources | None = None


class AgentJobResponse(BaseModel):
    """One agent job as returned to its owner."""

    id: str
    thread_id: str | None = None
    parent_job_id: str | None = None
    turn_no: int = 1
    repo: str
    task_prompt: str
    title: str | None = None
    title_source: str | None = None
    runtime: str
    model: str
    execution_profile: ExecutionProfile = ExecutionProfile.TRUSTED_V1
    #: Null means Auto, which is also what every turn created before this
    #: control existed reports. The UI renders nothing for it: an absent chip
    #: is the honest display of "nobody chose".
    reasoning_effort: str | None = None
    base_ref: str | None = None
    base_sha: str | None = None
    output_branch: str | None = None
    state: str
    conversation_state: Literal["queued", "running", "idle", "failed"] | None = None
    cancel_requested: bool = False
    current_attempt_id: int | None = None
    published_pr_url: str | None = None
    published_commit_sha: str | None = None
    detail: str | None = None
    # Set on turns created by a fork: the original turn this row copies.
    forked_from_job_id: str | None = None
    metadata: dict[str, Any] | None = None
    # Names only. The owner never sees a server's URL or credential — those are
    # the two things this service's MCP relay keeps off the sandbox-facing wire.
    mcp_servers: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    pinned_at: str | None = None
    archived_at: str | None = None
    # Read from the billing ledger, never from anything the agent reports
    # about itself. Attribution only: there is no per-task cap to compare it
    # against, and the account's daily quota is what actually governs spend.
    spent_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    model_calls: int | None = None
    # Attempt continuity telemetry. Current retained follow-ups report hot or
    # warm-native; a first NEW turn has no continuity rung, while cold/replay
    # remain readable for historical rows.
    #
    # **Populated only by the single-job read** (``GET /jobs/{id}``), which is
    # the one the turn view renders from. List and thread reads leave all
    # telemetry fields null rather than paying a per-row lookup for data that
    # those paths never display — so a null here means "not fetched on this path",
    # which is NOT the same statement as ``resources.coverage == 0``
    # ("fetched, and nothing could be measured"). A future consumer that
    # wants these in a listing needs a batched read, not a loop.
    telemetry_attempt_id: int | None = None
    continuity_mode: str | None = None
    # Product-level context correctness for restartable chats. Kept beside the
    # optimization rung above: RESTORED can be warm_native today and another
    # native transport tomorrow without rewriting what the user was promised.
    context_status: Literal["new", "live", "restored", "lost"] | None = None
    # Process residency is deliberately separate from context correctness.
    # A RESTORED turn may already be HOT again, while a LIVE context may be
    # briefly WARM during a member-only process restart.
    resident_state: Literal["stopped", "starting", "live", "restarting", "lost"] | None = None
    sandbox_id: str | None = None
    workspace_shared: bool = False
    native_state: Literal["new", "ready", "fork_preparing", "lost"] | None = None
    fork_anchor_job_id: str | None = None
    native_fork_available: bool = False
    workspace_publication_state: (
        Literal["pending", "capturing", "captured", "publishing", "published", "failed"] | None
    ) = None
    workspace_epoch: int | None = None
    # These clocks and duration describe the newest execution attempt, not
    # queue time or a sum across historical attempts. They are populated from
    # that attempt's recorded timestamps on the single-job read only; null
    # also covers an unavailable/unfetched record, not just a turn yet to run.
    # In particular, updated_at is never a substitute for a missing finish.
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    resources: AttemptResources | None = None
    # Only summaries are repeated in job reads; resource details are fetched
    # for the selected attempt through its own endpoint.
    attempt_telemetry: list[AgentAttemptSummary] = Field(default_factory=list)
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


AgentSessionState = Literal[
    "running", "queued", "idle", "needs_review", "done", "failed", "cancelled"
]
AgentSessionDeleteBlockReason = Literal[
    "not_archived", "session_active", "fork_in_progress", "publication_pending"
]


class AgentSessionResponse(BaseModel):
    """One conversation with its stable title and latest turn."""

    thread_id: str
    title: str
    archived_at: str | None = None
    job: AgentJobResponse
    can_delete: bool
    delete_block_reason: AgentSessionDeleteBlockReason | None = None


class AgentSessionProjectCount(BaseModel):
    """A project facet after archive, title and state filters, before repo."""

    repo: str
    count: int


class AgentSessionListResponse(BaseModel):
    """A cursor page of conversations, with optional complete filtered counts."""

    sessions: list[AgentSessionResponse]
    total: int | None
    next_cursor: str | None = None
    projects: list[AgentSessionProjectCount] | None


class AgentSessionSelection(BaseModel):
    """An explicit bounded snapshot of conversations selected by their owner."""

    thread_ids: list[str] = Field(..., min_length=1, max_length=1000)

    @field_validator("thread_ids")
    @classmethod
    def validate_thread_ids(cls, values: list[str]) -> list[str]:
        """Reject ambiguous selections and invalid database text values."""
        if any(not value or len(value) > 128 or "\x00" in value for value in values):
            raise ValueError("thread ids must contain between 1 and 128 valid characters")
        if len(values) != len(set(values)):
            raise ValueError("thread ids must be unique")
        return values


class AgentSessionArchiveRequest(AgentSessionSelection):
    """Set archive state for every selected conversation atomically."""

    archived: bool


class AgentSessionDeleteResponse(BaseModel):
    """Conversations permanently removed from owner-visible history."""

    thread_ids: list[str]


class AgentConversation(BaseModel):
    """One conversation, with its canonical title and latest run."""

    job: AgentJobResponse
    title: str
    archived: bool
    last_activity_at: str | None = None


class AgentConversationListResponse(BaseModel):
    """A searchable page of conversations owned by the caller."""

    conversations: list[AgentConversation]
    next_cursor: str | None = None


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


class AgentThreadEvent(AgentJobEvent):
    """One event, with the turn it belongs to.

    The per-job stream needs no such label — every row is the turn being
    watched. Across a thread it is what lets a reader see where one turn
    ended and the next began.
    """

    job_id: str
    turn_no: int = 1


class AgentThreadEventsResponse(BaseModel):
    """A page of a whole thread's events, plus the cursor to resume from."""

    events: list[AgentThreadEvent]
    next_cursor: int


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


class AgentSessionArchiveResponse(BaseModel):
    """Archive state for the complete selected conversation snapshot."""

    sessions: list[AgentThreadArchiveResponse]


class AgentThreadRenameRequest(BaseModel):
    """An owner-selected title for the entire conversation."""

    title: str = Field(..., min_length=1, max_length=160)

    @field_validator("title", mode="before")
    @classmethod
    def trim_title(cls, value: Any) -> Any:
        """Validate the length of the trimmed title, including blank titles."""
        return value.strip() if isinstance(value, str) else value


class AgentThreadRenameResponse(BaseModel):
    """The persisted title for the thread containing a requested job."""

    thread_id: str
    title: str


class AgentThreadPinResponse(BaseModel):
    """Pin state for the thread containing a requested job."""

    thread_id: str
    pinned: bool
    pinned_at: str | None = None


class AgentFollowUpRequest(BaseModel):
    """A new user turn appended to an existing task thread."""

    prompt: str = Field(..., min_length=1, max_length=100_000)
    runtime: str | None = Field(
        None,
        description=(
            "Accepted only when it names the thread's own harness — a thread's "
            "harness is fixed (its session is the unit of continuity; design "
            "D2). Any other value is a 400; start a new chat to switch."
        ),
    )
    model: str | None = Field(None, description="Optional model override for this turn.")
    reasoning_effort: str | None = Field(
        None,
        max_length=32,
        description=(
            "Optional reasoning-effort override for this turn. Omitted inherits "
            "the previous turn's, as an omitted model does. The literal "
            f"{RESET_REASONING_EFFORT!r} resets to Auto — effort needs a way "
            "back that model never did, because null is a value a turn can "
            "legitimately hold rather than the absence of one."
        ),
    )

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
    archived_at: str | None = None


class AgentThreadStatsResponse(BaseModel):
    """Aggregate numbers for a thread, derived from its stored events.

    Optional fields are ``null`` when the thread's runtime does not report
    the underlying measure — never ``0``, which a reader would take as a
    measured fact. ``timing_source`` and ``tokens_source`` tell a client how
    much to trust the rest, so the UI can label what it renders.
    """

    thread_id: str
    turns: int
    tool_calls: int
    failed_tool_calls: int
    wall_seconds: float
    model_seconds: float
    tool_seconds: float
    ttft_seconds: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_hit_ratio: float | None = None
    output_tokens_per_second: float | None = None
    tokens_source: str
    timing_source: str


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
    source: Literal["workspace"] = "workspace"


class AgentWorkspaceWriteRequest(BaseModel):
    """Replace one UTF-8 file in a retained workspace."""

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
    """Live diff, status, and commit data from the retained workspace."""

    available: bool
    branch: str = ""
    changes: list[AgentGitChange] = Field(default_factory=list)
    patch: str = ""
    commits: list[AgentGitCommit] = Field(default_factory=list)


# ── Worker-facing (capability-token authenticated) ─────────────────────


class WorkerClaimAttestation(BaseModel):
    """Runtime evidence collected by this exact claiming runner replica."""

    sandbox_runtime: str | None = Field(None, max_length=128)
    sandbox_image_digest: str | None = Field(None, max_length=512)
    runtime_versions: dict[str, str] = Field(default_factory=dict, max_length=64)
    #: Every version this replica's image holds, keyed by runtime id — the menu
    #: a stored selection is checked against before the claim honours it.
    #:
    #: Declared here or it does not exist. The runner has always sent it, and
    #: `claim_job` has always read it, but a model silently drops what it does
    #: not name: the field went over the wire, Pydantic discarded it, the claim
    #: saw an empty menu and fell back to the image default. Every selection an
    #: admin made was a no-op, and the only sign was one log line.
    runtime_versions_available: dict[str, list[str]] = Field(default_factory=dict, max_length=64)
    #: Versions of in-image protocol helpers that are not Agent CLIs (for
    #: example the Claude SDK fork bridge). They ride the same attestation so
    #: the API cannot silently discard evidence used by capability gating.
    helper_versions: dict[str, str] = Field(default_factory=dict, max_length=64)
    shared_sandbox_protocol: str | None = Field(None, max_length=32)
    runner_capabilities: list[str] = Field(default_factory=list, max_length=64)
    host_kernel: str | None = Field(None, max_length=256)
    guest_kernel: str | None = Field(None, max_length=256)


class WorkerClaimRequest(BaseModel):
    """Worker request to claim the next queued job."""

    # A stale runner must fail loudly instead of having its request-body host
    # identity silently discarded. Host identity comes only from the enrolled
    # credential now; accepting the old field would make a broken rollout look
    # healthy while the server ignored what the client thought it requested.
    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(..., description="Stable identifier of the claiming worker.")
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


class WorkerClaimResponse(BaseModel):
    """A claimed job plus the capability token scoped to this attempt."""

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
    mcp_servers: list[str] = Field(
        default_factory=list,
        description=(
            "MCP server names this job may reach through the control-plane relay. The runner "
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
    event_seq: int | None = Field(
        None,
        ge=1,
        description=(
            "The worker's own monotonically increasing number for this upload "
            "within its attempt. A replayed number is answered with the "
            "already-stored event's id instead of a duplicate row, which is "
            "what makes retrying a dropped response safe. Omitted by workers "
            "that predate it; those keep today's at-least-once behaviour."
        ),
    )

    @field_validator("payload")
    @classmethod
    def bound_payload(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """Keep stored events bounded even when an older worker uploads them."""
        return bound_event_payload(value) if value is not None else None


class WorkerEventResponse(BaseModel):
    """Accepted event id (the SSE cursor value)."""

    event_id: int


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
    stop_timeout_seconds: int = Field(10, ge=1, le=300)


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
    process_generation: int = Field(..., ge=1)


class WorkerSandboxAdmissionRequest(BaseModel):
    """Durably reserve this job before its prompt can reach Agent CLI stdin."""

    sandbox_generation: int = Field(..., ge=1)
    activation_generation: int = Field(..., ge=1)


class WorkerSandboxSettleRequest(BaseModel):
    """A native context boundary that may safely begin the idle timeout."""

    sandbox_generation: int = Field(..., ge=1)
    activation_generation: int = Field(..., ge=1)
    context_status: Literal["new", "live", "restored"]
    native_session_id: str | None = Field(None, max_length=512)
    base_sha: str | None = Field(None, min_length=7, max_length=64)
    idle_timeout_seconds: float = Field(1800.0, gt=0, le=7 * 24 * 60 * 60)


class WorkerSandboxAbandonRequest(BaseModel):
    """Release a failed activation, explicitly marking ambiguous context LOST."""

    sandbox_generation: int = Field(..., ge=1)
    activation_generation: int = Field(..., ge=1)
    lost: bool
    detail: str | None = Field(None, max_length=1000)
    idle_timeout_seconds: float = Field(1800.0, gt=0, le=7 * 24 * 60 * 60)


class WorkerToolCallResources(BaseModel):
    """One tool call's slice of a closed window, reported by the runner.

    Exclusive and shared counters are mutually exclusive by construction, and
    the store holds that as a constraint: a row carrying both has lost the
    only distinction these two columns exist to preserve.
    """

    call_seq: int = Field(..., ge=1)
    tool_use_id: str = Field(..., max_length=200)
    tool_name: str = Field(..., max_length=120)
    timing_source: Literal["message", "exact"] = "message"
    duration_ms: int | None = Field(None, ge=0)
    sample_count: int = Field(0, ge=0)
    overlap_n: int = Field(1, ge=1)
    measured_span_ms: int | None = Field(None, ge=0)
    cpu_usec_exclusive: int | None = Field(None, ge=0)
    cpu_usec_shared: int | None = Field(None, ge=0)
    mem_peak_exclusive_bytes: int | None = Field(None, ge=0)
    mem_peak_shared_bytes: int | None = Field(None, ge=0)
    process_count: int = Field(0, ge=0)
    process_cpu_usec: int | None = Field(None, ge=0)
    process_mem_peak_bytes: int | None = Field(None, ge=0)
    process_io_read_bytes: int | None = Field(None, ge=0)
    process_io_write_bytes: int | None = Field(None, ge=0)
    process_resources_partial: bool = False

    @property
    def has_process_resources(self) -> bool:
        """Whether any exit-accounted counter was charged to this call."""
        return (
            self.process_cpu_usec is not None
            or self.process_mem_peak_bytes is not None
            or self.process_io_read_bytes is not None
            or self.process_io_write_bytes is not None
        )

    @model_validator(mode="after")
    def counters_match_the_window_evidence(self) -> WorkerToolCallResources:
        """A worker cannot relabel shared or unmeasured counters as exact."""
        exclusive = self.cpu_usec_exclusive is not None or self.mem_peak_exclusive_bytes is not None
        shared = self.cpu_usec_shared is not None or self.mem_peak_shared_bytes is not None
        if exclusive and shared:
            raise ValueError("a tool row cannot mix exclusive and shared counters")
        if self.overlap_n == 1 and shared:
            raise ValueError("a non-overlapping tool row cannot carry shared counters")
        if self.overlap_n > 1 and exclusive:
            raise ValueError("an overlapping tool row cannot carry exclusive counters")
        if self.sample_count < 2 and (exclusive or shared or self.measured_span_ms is not None):
            raise ValueError("fewer than two samples cannot support a tool measurement")
        if self.process_count == 0 and (
            self.has_process_resources or self.process_resources_partial
        ):
            raise ValueError("a call with no attributed process cannot carry process resources")
        return self


class WorkerProcessObservation(BaseModel):
    """One bounded guest-kernel process identity reported by the broker."""

    process_seq: int = Field(..., ge=1)
    uid: int = Field(..., ge=0)
    pid: int = Field(..., ge=1)
    tgid: int = Field(..., ge=1)
    ppid: int | None = Field(None, ge=1)
    comm: str = Field(..., min_length=1, max_length=64)
    executable: str | None = Field(None, max_length=512)
    started_offset_ms: int = Field(..., ge=0)
    duration_ms: int | None = Field(None, ge=0)
    exit_code: int | None = Field(None, ge=0, le=255)
    observed_fork: bool = False
    observed_exec: bool = False
    observed_exit: bool = False
    tool_call_seqs: list[int] = Field(default_factory=list, max_length=500)
    cpu_usec: int | None = Field(None, ge=0)
    mem_peak_bytes: int | None = Field(None, ge=0)
    io_read_bytes: int | None = Field(None, ge=0)
    io_write_bytes: int | None = Field(None, ge=0)
    io_rchar_bytes: int | None = Field(None, ge=0)
    io_wchar_bytes: int | None = Field(None, ge=0)

    @model_validator(mode="after")
    def counters_need_an_observed_exit(self) -> WorkerProcessObservation:
        """Exit-time accounting cannot describe a process that has not exited."""
        if not self.observed_exit and any(
            value is not None
            for value in (
                self.cpu_usec,
                self.mem_peak_bytes,
                self.io_read_bytes,
                self.io_write_bytes,
                self.io_rchar_bytes,
                self.io_wchar_bytes,
            )
        ):
            raise ValueError("a process that has not exited cannot carry exit counters")
        return self


class WorkerResourcesRequest(BaseModel):
    """One closed attempt's resource/timing telemetry, reported by the runner.

    Historical sampled counters are cumulative deltas, not rates. Current
    shared-member reports intentionally omit them because a container delta
    includes concurrent siblings. ``sampler_version`` identifies that semantic
    shape; ``coverage = 0`` and absent counters stay visibly unavailable while
    tool timing and guest process rows can still be present.
    """

    cpu_usec: int | None = Field(None, ge=0)
    mem_peak_bytes: int | None = Field(None, ge=0)
    mem_avg_bytes: int | None = Field(None, ge=0)
    io_read_bytes: int | None = Field(None, ge=0)
    io_write_bytes: int | None = Field(None, ge=0)
    net_rx_bytes: int | None = Field(None, ge=0)
    net_tx_bytes: int | None = Field(None, ge=0)
    sample_count: int = Field(0, ge=0)
    coverage: float = Field(0.0, ge=0.0, le=1.0)
    sampler_version: str = Field(..., max_length=64)
    #: Bounded so one turn cannot post an unbounded list; the runner caps at
    #: the same number and says so through ``tool_calls_truncated``.
    tool_calls: list[WorkerToolCallResources] = Field(default_factory=list, max_length=500)
    tool_calls_truncated: bool = False
    tool_time_ms: int | None = Field(None, ge=0)
    process_observer: Literal["kata-guest-ebpf-v1"] | None = None
    process_observation_complete: bool = False
    process_events_lost: int = Field(0, ge=0)
    processes_truncated: bool = False
    processes: list[WorkerProcessObservation] = Field(
        default_factory=list,
        max_length=4096,
    )

    @model_validator(mode="after")
    def completeness_flags_match_their_evidence(self) -> WorkerResourcesRequest:
        """Refuse combinations that would make a gap render as certainty."""
        call_seqs = [call.call_seq for call in self.tool_calls]
        if len(call_seqs) != len(set(call_seqs)):
            raise ValueError("tool call sequence numbers must be unique")
        process_seqs = [process.process_seq for process in self.processes]
        if len(process_seqs) != len(set(process_seqs)):
            raise ValueError("process sequence numbers must be unique")

        known_call_seqs = set(call_seqs)
        observed_process_counts = dict.fromkeys(known_call_seqs, 0)
        for process in self.processes:
            referenced = process.tool_call_seqs
            if len(referenced) != len(set(referenced)):
                raise ValueError("a process cannot reference the same tool call twice")
            if not set(referenced).issubset(known_call_seqs):
                raise ValueError("a process cannot reference an absent tool call")
            for call_seq in referenced:
                observed_process_counts[call_seq] += 1
        if any(
            call.process_count != observed_process_counts[call.call_seq] for call in self.tool_calls
        ):
            raise ValueError("tool process_count must match the attached process rows")

        if self.sampler_version == SHARED_MEMBER_ACCOUNTING_VERSION:
            aggregates = (
                self.cpu_usec,
                self.mem_peak_bytes,
                self.mem_avg_bytes,
                self.io_read_bytes,
                self.io_write_bytes,
                self.net_rx_bytes,
                self.net_tx_bytes,
            )
            sampled_tool_evidence = any(
                call.sample_count != 0
                or call.measured_span_ms is not None
                or call.cpu_usec_exclusive is not None
                or call.cpu_usec_shared is not None
                or call.mem_peak_exclusive_bytes is not None
                or call.mem_peak_shared_bytes is not None
                for call in self.tool_calls
            )
            if (
                any(value is not None for value in aggregates)
                or self.sample_count != 0
                or self.coverage != 0.0
                or sampled_tool_evidence
            ):
                raise ValueError(
                    "shared-member-v1 cannot attribute sampled container counters to one member"
                )
        if self.tool_calls_truncated:
            has_call_measurement = any(
                call.sample_count > 0
                or call.measured_span_ms is not None
                or call.cpu_usec_exclusive is not None
                or call.cpu_usec_shared is not None
                or call.mem_peak_exclusive_bytes is not None
                or call.mem_peak_shared_bytes is not None
                for call in self.tool_calls
            )
            if has_call_measurement or self.tool_time_ms is not None:
                raise ValueError(
                    "a truncated tool-window list cannot support attribution or a time split"
                )
        if (
            any(call.duration_ms is None for call in self.tool_calls)
            and self.tool_time_ms is not None
        ):
            raise ValueError("an unfinished tool call cannot support a complete time split")
        if self.process_observation_complete and (
            self.process_observer is None
            or self.process_events_lost != 0
            or self.processes_truncated
        ):
            raise ValueError("complete process observation contradicts its loss metadata")
        if self.process_observer is None and (
            self.processes
            or self.process_events_lost > 0
            or self.processes_truncated
            or any(
                call.process_count > 0
                or call.has_process_resources
                or call.process_resources_partial
                for call in self.tool_calls
            )
        ):
            raise ValueError("process rows require a named guest observer")
        return self


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
    runtime_reasoning_efforts: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Effort levels each runtime can express, in its own vocabulary. A "
            "runtime absent here cannot express one at all."
        ),
    )
    model_reasoning_efforts: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Effort levels each model accepts, from the gateway's registry. The "
            "composer offers the intersection with the chosen runtime's, and no "
            "control at all when that is empty — which is also what it shows "
            "against a gateway too old to answer this."
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


class McpIntegrationResponse(BaseModel):
    """One catalogue MCP server, and whether this user has connected it.

    Carries no credential and no upstream URL — the same rule as
    :class:`McpServerSummary`, for the same reason, and enforced the same way:
    there is no field either could occupy. ``account_label`` is what the
    provider calls the connected account, which is the user's own name for it
    and the only way to tell two connections apart in the UI.
    """

    name: str
    label: str
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)
    #: Whether connecting is possible on this deployment right now.
    configured: bool = False
    connected: bool = False
    account_label: str | None = None
    connect_url: str | None = None
    #: Why a connection that exists buys nothing — expired, unreadable, or a
    #: provider withdrawn since. ``connected`` stays true because the row is
    #: really there and disconnecting it is the way out; this says what the
    #: owner has to do about it. Mirrors ``SourceControlProviderResponse``.
    error: str | None = None


class SourceControlIntegrationsResponse(BaseModel):
    """Everything the Integrations page shows the current user.

    Named for source control because that is all it carried first; ``mcp`` is
    the second kind of connection on the same page, kept in one response so the
    page makes one request and cannot render half-refreshed.
    """

    providers: list[SourceControlProviderResponse]
    mcp: list[McpIntegrationResponse] = Field(default_factory=list)


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
    sandbox_runtime: str | None = Field(
        None,
        description=(
            "Attested container runtime for this host's jobs. '' = "
            "shared-kernel runc; null = the host has never reported one."
        ),
    )
    sandbox_image_digest: str | None = Field(
        None,
        description="Digest of the sandbox image this host launches, as last reported.",
    )
    runtime_versions: dict[str, str] | None = Field(
        None,
        description=(
            "The *default* CLI version per runtime id — what a job runs when "
            "nothing has been selected."
        ),
    )
    runtime_versions_available: dict[str, list[str]] | None = Field(
        None,
        description=(
            "Every CLI version this host's image holds, keyed by runtime id, "
            "default first. The menu an admin may select from; the control "
            "plane cannot serve a version absent from it."
        ),
    )
    capacity_epoch: str | None = Field(
        None,
        description=(
            "Generation of this host's capacity data; rotated by re-enrollment. "
            "A client uses it to drop a curve that belongs to the machine this "
            "one replaced."
        ),
    )
    capacity: AgentHostCapacity | None = Field(
        None,
        description=(
            "The last accepted host observation, with the server's own "
            "received/sampled times and freshness. Null means no usable "
            "observation has ever arrived — which is not the same as an idle "
            "machine, and is rendered as unavailable rather than as zero."
        ),
    )


class ListAgentRunnerHostsResponse(BaseModel):  # type: ignore[no-any-unimported]
    """Runner pool, new-chat placement, and selected CLI versions."""

    execution_profile: str = Field(
        "untrusted-kata-v1",
        description="Server-owned policy for new chats; used to show eligible servers in admin.",
    )
    hosts: list[AgentRunnerHost]
    active_host: str | None = Field(
        None,
        description=(
            "Host that may materialize chats without a sandbox, or null when "
            "any claimable host may. Retained groups stay on their owning host."
        ),
    )
    runtime_versions: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "The deployment's chosen CLI version per runtime id. A runtime "
            "absent here runs its claiming host's default."
        ),
    )
    observed_at: datetime | None = Field(
        None,
        description=(
            "When the database answered this read, by its own clock. Every "
            "pool response carries it, so an operation's answer and a poll's "
            "answer are comparable without the client's clock."
        ),
    )


class SetActiveAgentRunnerHostRequest(BaseModel):  # type: ignore[no-any-unimported]
    """Pin new-chat placement to one host, or unpin with ``null``."""

    host: str | None = Field(
        None,
        max_length=253,
        description="A host that has polled at least once, or null to unpin.",
    )


class SetRuntimeVersionRequest(BaseModel):  # type: ignore[no-any-unimported]
    """Choose the CLI version this deployment runs for one runtime."""

    runtime: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Runtime id, as the registry knows it (e.g. 'claude-code').",
    )
    version: str | None = Field(
        None,
        max_length=128,
        description=(
            "A version some host in the pool offers, or null to return this "
            "runtime to the image default. Applies to new threads; a thread "
            "already running keeps the version it started on."
        ),
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
    slots_total: int = Field(
        ...,
        gt=0,
        le=1024,
        description="Concurrent attempts this enrolled host accepts.",
    )
    broker_url: str = Field(
        ...,
        max_length=2048,
        description=(
            "Where this host's workspace broker listens, as the control plane "
            "reaches it. Every executable attempt records an enrolled host, and "
            "files, terminals and Git resolve the broker from that host row. "
            "Omitting it leaves the host unready and unable to serve chats."
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


class ResidencyBeat(BaseModel):  # type: ignore[no-any-unimported]
    """One hot sandbox a host reports alive."""

    thread_id: str = Field(..., min_length=1, max_length=128)
    generation: int = Field(..., ge=1)


class RetainedSandboxBeat(BaseModel):  # type: ignore[no-any-unimported]
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
            "broker could not prove activity state after reconstruction."
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


class RetainedSandboxStop(BaseModel):  # type: ignore[no-any-unimported]
    """Exact durable fence authorizing a host to stop one retained container."""

    thread_id: str = Field(..., min_length=1, max_length=128)
    generation: int = Field(..., ge=1)
    # A fork target is born stopped at generation zero. If the broker starts
    # it for a file/terminal operation and then restarts before the first agent
    # turn, the durable idle-stop fence still legitimately names generation
    # zero. Rejecting it here leaks that reconstructed container in RUNNING.
    activation_generation: int = Field(..., ge=0)
    reason: Literal["idle", "orphaned_attempt", "lost"] = "idle"
    shared_live: Literal[True] = True


class HostHeartbeatRequest(BaseModel):  # type: ignore[no-any-unimported]
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
    runner_version: str | None = Field(None, max_length=128)
    broker_url: str | None = Field(None, max_length=2048)
    capabilities: list[str] | None = Field(None, max_length=64)
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
    runtime_versions: dict[str, str] | None = Field(
        None,
        max_length=64,
        description=(
            "The *default* CLI version per runtime id (e.g. "
            "{'claude-code': '1.2.3'}), read from image labels. An empty dict "
            "means 'inspected, image carries no version labels'."
        ),
    )
    runtime_versions_available: dict[str, list[str]] | None = Field(
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


class HostResidencyEstablishResponse(BaseModel):  # type: ignore[no-any-unimported]
    """Attempt-fenced residency granted to a supervisor-owned member process."""

    thread_id: str
    generation: int
    ttl_seconds: float
    credential: str | None = None
    model: str | None = None


class HostHeartbeatResponse(BaseModel):  # type: ignore[no-any-unimported]
    """What the control plane thinks of this host after the heartbeat."""

    host: str
    status: str
    slots_total: int | None = None
    slots_used: int = 0
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


class SessionUserResponse(BaseModel):  # type: ignore[no-any-unimported]
    """Who the session says the caller is.

    ``id`` rather than ``user_id``: this is the shape ``web/src/lib/api/
    session.ts`` was written against, and it is the contract for all three
    session routes.
    """

    id: str
    agent_access: AgentAccess
