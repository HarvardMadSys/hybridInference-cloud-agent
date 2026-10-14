"""Thread messages, pending-message queues, and interactive questions."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cloud_agent.schemas._prompts import _visible_prompt
from cloud_agent.schemas.jobs import RESET_REASONING_EFFORT, AgentJobResponse


class AgentThreadMessageResponse(BaseModel):
    """One durable user or assistant message in a task thread."""

    id: int
    role: str
    content: str
    job_id: str
    created_at: str | None = None
    mid_turn: bool = Field(
        False,
        description=(
            "A user message sent into this job's turn while it ran, rather than "
            "the prompt that started it. It is ordered among the turn's replies "
            "at the point it was sent; a turn has one prompt and any number of these."
        ),
    )


PendingMessageState = Literal["queued", "held", "sending"]
QueuePausedReason = Literal["stopped", "failed", "archived"]


class AgentThreadMessageCreate(BaseModel):
    """A message for a conversation: the next turn now, or queued behind the running one.

    A follow-up without ``runtime`` or ``goal``. The harness is the thread's and
    cannot change, and a goal has its own routes; a request that still sends
    either is refused rather than having the field quietly dropped.
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(..., min_length=1, max_length=100_000)
    skill_ids: list[Annotated[int, Field(gt=0, strict=True)]] | None = Field(
        None,
        max_length=16,
        description="Omitted inherits the previous turn's skill snapshots; [] clears the selection.",
    )
    attachment_ids: list[Annotated[int, Field(gt=0, strict=True)]] | None = Field(
        None,
        max_length=50,
        description=(
            "Uploads for this message's turn. While the message waits they are pinned "
            "to it, so they are neither swept nor deletable from the composer."
        ),
    )
    model: str | None = Field(None, description="Optional model override for this turn.")
    reasoning_effort: str | None = Field(
        None,
        max_length=32,
        description=(
            "Optional reasoning-effort override for this turn. Omitted inherits the "
            f"previous turn's; the literal {RESET_REASONING_EFFORT!r} resets to Auto."
        ),
    )
    require_idle: bool = Field(
        False,
        strict=True,
        description=(
            "Accept only when every chat sharing the published workspace is idle "
            "with an empty queue."
        ),
    )
    expected_latest_job_id: str | None = Field(
        None,
        min_length=1,
        max_length=128,
        description="The newest turn read by an idle-only action; checked atomically on submit.",
    )

    @model_validator(mode="after")
    def validate_idle_guard(self) -> AgentThreadMessageCreate:
        """Bind an idle-only action to the newest turn its caller actually read."""
        if self.require_idle and (
            not self.expected_latest_job_id or not self.expected_latest_job_id.strip()
        ):
            raise ValueError("require_idle needs expected_latest_job_id")
        if self.expected_latest_job_id is not None and not self.require_idle:
            raise ValueError("expected_latest_job_id needs require_idle")
        return self

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        """Reject visually empty turns and persist the user's trimmed text."""
        return _visible_prompt(value)


class AgentPendingMessageUpdate(BaseModel):
    """An edit to a waiting message, made against the revision the caller read.

    Only the fields sent change. ``state`` is how an editor holds the message
    while it is rewritten (``held``) and lets it go again (``queued``).
    """

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(..., ge=1)
    prompt: str | None = Field(None, min_length=1, max_length=100_000)
    skill_ids: list[Annotated[int, Field(gt=0, strict=True)]] | None = Field(None, max_length=16)
    attachment_ids: list[Annotated[int, Field(gt=0, strict=True)]] | None = Field(
        None, max_length=50
    )
    model: str | None = None
    reasoning_effort: str | None = Field(None, max_length=32)
    state: Literal["held", "queued"] | None = None

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str | None) -> str | None:
        """A rewritten message still has to say something."""
        if value is None:
            raise ValueError("prompt cannot be null; omit it to keep the text")
        return _visible_prompt(value)

    @field_validator("state")
    @classmethod
    def validate_state(cls, value: str | None) -> str | None:
        """Omit the state to keep it; null is not a state a message can be in."""
        if value is None:
            raise ValueError("state cannot be null; omit it to keep the state")
        return value


class AgentPendingMessageAttachment(BaseModel):
    """One upload a waiting message holds, without its bytes."""

    id: int
    path: str
    content_type: str
    size_bytes: int


class AgentPendingMessageResponse(BaseModel):
    """A message the owner wrote that has not become a turn yet."""

    id: str
    thread_id: str
    #: The order it will be sent in. Assigned once; editing never moves it.
    position: int
    state: PendingMessageState
    #: What the next edit or delete must name, so a stale tab loses cleanly.
    revision: int
    prompt: str
    skill_ids: list[int] | None = None
    attachment_ids: list[int] | None = None
    attachments: list[AgentPendingMessageAttachment] = Field(default_factory=list)
    model: str | None = None
    reasoning_effort: str | None = None
    #: Why the platform held this message instead of sending it, if it did.
    last_error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class AgentThreadQueueResponse(BaseModel):
    """A conversation's queue, and its newest turn, without the transcript.

    What a page polls while messages wait: small enough to ask for often, and
    enough to notice the moment one of them became the next turn.
    """

    thread_id: str
    pending_messages: list[AgentPendingMessageResponse] = Field(default_factory=list)
    queue_paused_reason: QueuePausedReason | None = None
    latest_job_id: str | None = None
    latest_turn_no: int | None = None
    latest_job_state: str | None = None
    #: Whether a waiting message can be sent into the newest turn while it runs.
    mid_turn_input_supported: bool = False


class AgentPendingMessageSendNow(BaseModel):
    """Send one waiting message into the live turn, at the revision read."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(..., ge=1)


class AgentQuestionOption(BaseModel):
    """One choice in a native agent question."""

    label: str
    description: str = ""


class AgentQuestion(BaseModel):
    """A native question rendered without exposing its transport dialect."""

    id: str
    header: str = ""
    question: str
    multiple: bool = False
    options: list[AgentQuestionOption] = Field(default_factory=list)
    allow_custom: bool = True
    #: ``plan_approval`` renders the question as the plan's Markdown and offers
    #: feedback; plain questions carry no kind.
    kind: Literal["plan_approval"] | None = None


class AgentQuestionRequest(BaseModel):
    """The correlated questions one native tool is waiting to have answered."""

    request_id: str
    questions: list[AgentQuestion]


class AgentThreadQuestionsResponse(BaseModel):
    """Pending native questions and the exact attempt that asked them."""

    job_id: str
    attempt_id: int | None = None
    questions: list[AgentQuestionRequest] = Field(default_factory=list)


class AgentQuestionAnswer(BaseModel):
    """Answer a native request only on the job and attempt the owner read."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(..., min_length=1, max_length=128)
    attempt_id: int = Field(..., ge=1)
    request_id: str = Field(..., min_length=1, max_length=128)
    answers: dict[
        Annotated[str, Field(min_length=1, max_length=128)],
        Annotated[
            list[Annotated[str, Field(min_length=1, max_length=10_000)]], Field(max_length=20)
        ],
    ] = Field(default_factory=dict, max_length=10)
    reject: bool = False


class AgentQuestionAnswerResponse(BaseModel):
    """Whether the live native request accepted the answer."""

    accepted: bool
    reason: str | None = None


class AgentThreadMessageResult(BaseModel):
    """What a queue action left behind.

    ``job`` is the turn this request started, if it started one: the message
    just sent when nothing was running, or an older waiting message that went
    first. ``pending`` is the message this request wrote or changed, while it is
    still waiting.
    """

    pending: AgentPendingMessageResponse | None = None
    job: AgentJobResponse | None = None
    queue_paused_reason: QueuePausedReason | None = None
    #: Send-now only: why the live turn cannot have it. It still waits, and
    #: goes when the turn ends. Without one, the message is ``sending``: the
    #: turn has it for the model's next step, and the transcript shows it once
    #: the turn took it.
    not_delivered_reason: str | None = None
