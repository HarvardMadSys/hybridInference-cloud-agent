"""Conversation listings, project summaries, and thread management responses."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from cloud_agent.schemas.jobs import (
    AgentJobResponse,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)
from cloud_agent.schemas.messages import (
    AgentPendingMessageResponse,  # noqa: TC001 — Pydantic resolves annotations at runtime.
    AgentThreadMessageResponse,  # noqa: TC001 — Pydantic resolves annotations at runtime.
    QueuePausedReason,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)

AgentSessionState = Literal[
    "running", "queued", "idle", "needs_review", "done", "failed", "cancelled"
]
AgentSessionDeleteBlockReason = Literal["fork_in_progress", "publication_pending"]


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

    #: Null is the temporary workspaces: conversations with no repository.
    repo: str | None
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

    #: Null groups the caller's temporary workspaces, which have no repository.
    repo: str | None
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


class AgentThreadPublicationResponse(BaseModel):
    """The workspace snapshot an owner's publication request opened or found."""

    thread_id: str
    snapshot_id: int
    state: Literal["pending", "capturing", "captured", "publishing"]
    requested: bool = Field(
        description=(
            "True when this request opened the snapshot; false when a publication "
            "of the workspace was already on its way and is returned instead."
        )
    )


class AgentThreadResponse(BaseModel):
    """Conversation context and runs for the thread containing a job."""

    thread_id: str
    #: Null for a temporary workspace.
    repo: str | None
    title: str
    messages: list[AgentThreadMessageResponse]
    jobs: list[AgentJobResponse]
    created_at: str | None = None
    updated_at: str | None = None
    archived_at: str | None = None
    #: Messages written while a turn was running, in the order they will go.
    pending_messages: list[AgentPendingMessageResponse] = Field(default_factory=list)
    #: Why the queue is not moving by itself, until the owner continues it.
    queue_paused_reason: QueuePausedReason | None = None


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
