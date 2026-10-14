"""Job creation, owner-visible job responses, and reserved job metadata."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from cloud_agent.attachments.models import (
    AttachmentRef,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)
from cloud_agent.entitlement import REPO_PATTERN
from cloud_agent.execution_profiles import ExecutionProfile
from cloud_agent.github_app import is_ref_name
from cloud_agent.goals.models import (  # noqa: TC001 — Pydantic resolves annotations at runtime.
    GoalInput,
    GoalRoundKind,
    GoalView,
)
from cloud_agent.goals.report import (  # noqa: TC001 — Pydantic resolves annotations at runtime.
    GoalReportOutcome,
    GoalReportParse,
)
from cloud_agent.schemas._prompts import _visible_prompt
from cloud_agent.schemas.mcp import (  # noqa: TC001 — Pydantic resolves annotations at runtime.
    McpBindingInput,
    McpBindingSummary,
)
from cloud_agent.schemas.resources import (  # noqa: TC001 — Pydantic resolves annotations at runtime.
    AgentAttemptSummary,
    AttemptResources,
)
from cloud_agent.skills.models import (
    SelectedSkill,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)
from cloud_agent_shared.environment import (
    EnvironmentVariable,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)
from cloud_agent_shared.plugins import MAX_SANDBOX_PLUGINS, SandboxPlugin

# Where a job's `base_ref` lives once it is persisted. The column stores the
# resolved `base_sha` — a branch moves — so the branch the owner actually
# picked is kept alongside it in `metadata` under a reserved key, which the API
# strips back out on the way to the client. Reserved means the creation path
# drops any caller-supplied copy: it is a fact the server resolved, and the
# publisher targets the PR at it, so a caller must not be able to forge one.
BASE_REF_METADATA_KEY = "_agent_base_ref"

#: Reserved metadata mark for jobs the on-call service door created. Stamped by
#: the platform after caller metadata so a body cannot un-mark itself, and read
#: by anything that wants to tell an on-call analysis from an owner's thread
#: (the web UI, spend reports). Reserved in the same sense as the base ref: the
#: creation path drops a caller-supplied copy, so an owner's job cannot claim
#: to be one the platform dispatched.
ONCALL_METADATA_KEY = "oncall"

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

#: Caller metadata is stored on the first turn, copied onto every later turn of
#: the conversation and returned with every job read, so it is bounded by key
#: count and by its serialized size rather than left to the proxy in front.
MAX_JOB_METADATA_KEYS = 64
MAX_JOB_METADATA_BYTES = 16 * 1024


# Unknown fields are still ignored here rather than refused, unlike a message's
# body: the on-call relay posts this same model to its own door, and what it
# sends has included a field this model never declared (``mcp_servers``), so
# refusing unknown fields is a change to coordinate with that caller first.
class AgentJobCreate(BaseModel):
    """Request body for creating an agent job."""

    repo: str | None = Field(
        ...,
        # Shape-checked here as well as against the entitlement allowlist: the
        # value is interpolated into a clone URL and handed to git, which reads
        # a leading `-` as an option wherever it appears.
        pattern=REPO_PATTERN,
        max_length=140,
        # Still required: a client that leaves the field out is refused rather
        # than handed a workspace whose work nothing will publish.
        description=(
            "Target repository, e.g. 'owner/name'. An explicit null starts a temporary "
            "workspace: the sandbox starts empty, and nothing it does is published."
        ),
    )
    # The same bounds and blank check a message to the conversation has: this
    # is that conversation's first message.
    task_prompt: str = Field(
        ..., min_length=1, max_length=100_000, description="What the agent should do."
    )
    runtime: str = Field("claude-code", max_length=64, description="Agent runtime id.")
    model: str = Field(..., max_length=255, description="Gateway model id the runtime should use.")
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
    environment_id: str | None = Field(None, max_length=80)
    environment_revision: int | None = Field(None, ge=0)
    global_revision: int | None = Field(None, ge=0)
    environment_overrides: list[EnvironmentVariable] = Field(
        default_factory=list, max_length=50, repr=False
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
        # A bare commit hash, enforced here as well as in the publisher: git
        # reads a leading `-` as an option even where an operand is expected,
        # so an unconstrained ref would be an argument injection into the
        # trusted process that holds the repository credential.
        pattern=r"^[0-9a-fA-F]{7,64}$",
        description="Commit SHA to work from.",
    )
    skill_ids: list[Annotated[int, Field(gt=0, strict=True)]] | None = Field(
        None,
        max_length=16,
        description="Owned skills for this chat. Omitted uses library defaults; [] selects none.",
    )
    plugins: list[SandboxPlugin] | None = Field(
        None,
        max_length=MAX_SANDBOX_PLUGINS,
        description="Plugins installed for this chat during sandbox initialization.",
    )
    mcp_bindings: list[McpBindingInput] | None = Field(
        None,
        max_length=16,
        description=(
            "MCP connections this session may use, with the capabilities it may use on "
            "each. Omitted means none: there is no implicit default, because connecting "
            "a server is not the same decision as letting a task call it."
        ),
    )
    attachment_ids: list[Annotated[int, Field(gt=0, strict=True)]] | None = Field(
        None,
        max_length=50,
        description=(
            "Staged uploads to place under /uploads in the sandbox. Omitted attaches "
            "nothing: unlike skills there are no account defaults, because an upload "
            "is something done for this one task rather than a standing preference."
        ),
    )
    goal: GoalInput | None = Field(
        None,
        description=(
            "A completion condition for this conversation. The platform judges it at "
            "the end of every turn and queues the next turn itself while the objective "
            "is unmet. There is no round limit unless the caller states one with "
            "max_rounds: by default the goal continues until it is met, paused, cleared, "
            "blocked on the owner, or stopped by an existing run-time limit. Omitted "
            "means no goal: nothing continues without the owner asking again."
        ),
    )
    metadata: dict[str, Any] | None = Field(
        None,
        description=(
            f"Opaque caller metadata: at most {MAX_JOB_METADATA_KEYS} keys and "
            f"{MAX_JOB_METADATA_BYTES // 1024} KiB as JSON."
        ),
    )

    @field_validator("base_ref")
    @classmethod
    def base_ref_is_a_git_ref_name(cls, value: str | None) -> str | None:
        """Refuse a name git would refuse: it is sent to GitHub inside an API path."""
        if value and not is_ref_name(value):
            raise ValueError("base_ref must be a valid Git branch or tag name")
        return value

    @model_validator(mode="before")
    @classmethod
    def execution_profile_is_server_owned(cls, value: Any) -> Any:
        """Refuse an apparent profile choice instead of silently ignoring it."""
        if isinstance(value, dict) and "execution_profile" in value:
            raise ValueError(
                "execution_profile is server-owned; callers cannot select or downgrade it"
            )
        return value

    @model_validator(mode="after")
    def temporary_workspace_names_no_repository(self) -> AgentJobCreate:
        """Refuse a branch, commit or repository environment without a repository.

        Each of these names something inside one repository. Ignoring them on a
        temporary workspace would run a task in a place other than the one its
        request described.
        """
        if self.repo is None:
            named = [
                name for name in ("base_ref", "base_sha", "environment_id") if getattr(self, name)
            ]
            if named:
                raise ValueError(
                    f"{', '.join(named)} must be omitted when repo is null: "
                    "a temporary workspace has no repository"
                )
        return self

    @field_validator("task_prompt")
    @classmethod
    def validate_task_prompt(cls, value: str) -> str:
        """Reject a visually empty task and persist the user's trimmed text."""
        return _visible_prompt(value)

    @field_validator("metadata")
    @classmethod
    def bound_metadata(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """Keep caller metadata small; every turn of the conversation repeats it."""
        if value is None:
            return None
        if len(value) > MAX_JOB_METADATA_KEYS:
            raise ValueError(f"metadata may have at most {MAX_JOB_METADATA_KEYS} keys")
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(encoded.encode("utf-8")) > MAX_JOB_METADATA_BYTES:
            raise ValueError(f"metadata may be at most {MAX_JOB_METADATA_BYTES} bytes as JSON")
        return value


class AgentJobResponse(BaseModel):
    """One agent job as returned to its owner."""

    id: str
    thread_id: str | None = None
    parent_job_id: str | None = None
    turn_no: int = 1
    #: Null for a temporary workspace, which has no repository.
    repo: str | None
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
    # Raised by one each time the row changes. A tab that also receives this
    # row from the owner's change stream keeps whichever revision is higher;
    # the conversation's fields (title, pin) follow ``thread_revision``.
    revision: int = 0
    thread_revision: int = 0
    # Set on turns created by a fork: the original turn this row copies.
    forked_from_job_id: str | None = None
    metadata: dict[str, Any] | None = None
    # How this turn came to exist, and what it was asked to do about the
    # conversation's goal. A snapshot: editing the objective later never
    # rewrites what this turn was told or what it reported.
    turn_source: Literal["human", "goal"] = "human"
    goal_round: int | None = None
    goal_round_kind: GoalRoundKind | None = None
    # What this turn's final message said about the goal, and how it read.
    # ``goal_report_parse`` is the platform's own reading — ``absent``,
    # ``valid``, ``invalid`` or ``unavailable`` — and is what the owner's page
    # renders the block from. ``goal_report_detail`` explains a parse that
    # failed, or the sentence a valid report carried.
    goal_report_parse: GoalReportParse | None = None
    goal_report_outcome: GoalReportOutcome | None = None
    goal_report_detail: str | None = None
    goal_repair_round: int | None = None
    # The conversation's *live* goal, on every turn of it, the way ``title`` is.
    # Editing the objective is visible here immediately; ``goal_round`` above is
    # what this particular turn ran against.
    goal: GoalView | None = None
    # Labels and revisions only. The owner never sees a connection's address or
    # credential — those are the two things this service's MCP relay keeps off
    # the sandbox-facing wire.
    skills: list[SelectedSkill] = Field(default_factory=list)
    #: What this turn attached, without the bytes. The rows behind these ids
    #: are the single copy of the content, so a fork references the same
    #: immutable uploads rather than duplicating them.
    attachments: list[AttachmentRef] = Field(default_factory=list)
    plugins: list[SandboxPlugin] = Field(default_factory=list)
    mcp_bindings: list[McpBindingSummary] = Field(default_factory=list)
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
    mid_turn_input_supported: bool = Field(
        False,
        description=(
            "Whether a waiting message can be sent into this job's turn while it "
            "runs (POST /threads/{thread_id}/messages/{message_id}/send-now): the "
            "host that holds the conversation can steer its runtime."
        ),
    )
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
    # Source control connection state. The composer is gated on this rather
    # than offering pickers that submit something else: with no App installed
    # there is no repository to work on, and a task box that looks ready is a
    # worse answer than one that says what is missing.
    github_connected: bool = False
    github_install_url: str | None = None


class AgentJobListResponse(BaseModel):
    """A page of agent jobs."""

    jobs: list[AgentJobResponse]


class AgentJobCancelResponse(BaseModel):
    """Result of requesting cancellation."""

    id: str
    state: str
    cancel_requested: bool
