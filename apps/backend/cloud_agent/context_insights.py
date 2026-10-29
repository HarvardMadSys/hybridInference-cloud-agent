"""An owner's conversations compared by their context profiles.

The Usage page's Context insights list every conversation with runs created in
the selected range (the usage history's own basis, ``job_created_at`` in UTC
days, Fork copies left out), each summarized from its attempts' stored
context profiles: the largest and latest request observed, the window the CLI
reported, how often it compacted, how many requests it made. Each entry links
to that conversation's Context view.

Profiles are derived lazily (see :mod:`cloud_agent.context_profile`); a page
read derives a bounded number of missing ones and says how many remain, so a
ranking is never presented as complete while it is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime  # noqa: TC003 — Pydantic resolves the model annotations.
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.admin_observability import cursor_scope
from cloud_agent.admin_usage import AdminUsageAggregate  # noqa: TC001
from cloud_agent.context_profile import CONTENT_CATEGORIES, TOTAL_FIELDS
from cloud_agent.usage_range import UsageRange  # noqa: TC001
from cloud_agent_shared.text_tokens import ESTIMATE_BASIS

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

ContextSort = Literal["recent", "peak", "compactions", "requests"]
CONTEXT_SORTS: tuple[ContextSort, ...] = ("recent", "peak", "compactions", "requests")

DEFAULT_CONTEXT_PAGE = 25
MAX_CONTEXT_PAGE = 50
#: Attempts one page read may profile before answering, and the events it may
#: read doing so. An attempt it starts is always finished, so the second bound
#: can be passed by one attempt, never by a second.
REFRESH_ATTEMPTS_PER_READ = 40
REFRESH_EVENTS_PER_READ = 40_000
#: Missing profiles counted exactly; beyond it the page says "more than".
PENDING_COUNT_CAP = 10_000


#: Characters a search may hold, as on the conversation list.
MAX_SEARCH_CHARS = 200


@dataclass(frozen=True)
class InsightsFilters:
    """What narrows the list: a search, one project, one UTC day of the range.

    The search matches a conversation's title or repository, case-insensitive,
    as the conversation list searches. ``repo`` is one repository and
    ``temporary`` the conversations without one; they are never both set.
    """

    q: str = ""
    repo: str | None = None
    temporary: bool = False
    day_since: datetime | None = None
    day_until: datetime | None = None

    def params(self, *, day: bool = True, project: bool = True) -> list[Any]:
        """The statement parameters, in the order the runs selection reads them."""
        values: list[Any] = [
            self.q,
            self.repo if project else None,
            self.temporary if project else False,
        ]
        return [*values, self.day_since, self.day_until] if day else values

    def scope(self) -> list[Any]:
        """What a cursor is bound to, beyond the sort and range."""
        return [self.q, self.repo, self.temporary, self.day_since and self.day_since.isoformat()]


def context_cursor_scope(
    sort: ContextSort, preset: str | None, filters: InsightsFilters | None = None
) -> str:
    """Bind a cursor to the list it continues: its sort, range and filters."""
    if filters is None or filters == InsightsFilters():
        return cursor_scope(["owner-context-insights", sort, preset])
    return cursor_scope(["owner-context-insights", sort, preset, *filters.scope()])


#: Recorded-content categories, in the order a chart stacks them: the turns'
#: own prompts, what the platform wrapped them in, then the stored content.
RecordedCategory = Literal[
    "turn_prompts",
    "platform_context",
    "user_messages",
    "assistant",
    "reasoning",
    "tool_calls",
    "tool_results",
    "compaction_summary",
    "injected",
]
RECORDED_CATEGORIES: tuple[RecordedCategory, ...] = (
    "turn_prompts",
    "platform_context",
    *CONTENT_CATEGORIES,
)
#: Where each category's per-conversation sum sits in a composition row.
_ROW_COLUMN: dict[str, str] = {
    "turn_prompts": "prompt_tokens",
    "platform_context": "platform_tokens",
}


class RecordedShare(BaseModel):
    """One category's recorded content, estimated in tokens."""

    model_config = ConfigDict(extra="forbid")

    key: RecordedCategory
    tokens: int = Field(ge=0)


class RecordedComposition(BaseModel):
    """What the turns recorded, each stored item counted once, by category.

    An estimate from characters (``basis``), never a billed or reported
    count. A turn counts its kept attempt only (a retried one was replaced),
    a delegated agent's own context is left out, and a Fork's copied turns
    belong to the conversation they were copied from. CLI system prompts and
    tool definitions were never stored, so they are not a category: nothing
    is derived by subtracting this from a reported total.
    """

    model_config = ConfigDict(extra="forbid")

    basis: str = ESTIMATE_BASIS
    categories: list[RecordedShare]
    total_tokens: int = Field(ge=0)
    turns: int = Field(ge=0)
    #: Turns whose kept attempt has a current profile; the rest are not counted yet.
    turns_profiled: int = Field(ge=0)
    #: Turns with at least one stored item clipped when recorded (undercounted).
    turns_truncated: int = Field(ge=0)


class ProviderTotals(BaseModel):
    """Every request's size as the provider reported it (via the CLI), summed.

    Each request resends what the conversation kept, so this is per-request
    exposure: retained text counts once for every request that carried it.
    Cache reads and writes are parts of ``prompt_tokens``, not additions.
    """

    model_config = ConfigDict(extra="forbid")

    requests: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    #: Requests whose output the CLI did not report; their output is not summed.
    output_unreported: int = Field(ge=0)
    subagent_requests: int = Field(ge=0)
    subagent_prompt_tokens: int = Field(ge=0)
    subagent_output_tokens: int = Field(ge=0)


class ContextComposition(BaseModel):
    """Recorded content (estimated, once) beside reported request sizes (exact, per request)."""

    model_config = ConfigDict(extra="forbid")

    recorded: RecordedComposition
    provider: ProviderTotals


def _int(row: Mapping[str, Any], key: str) -> int:
    value = row.get(key)
    return int(value) if value is not None else 0


def compose(rows: Iterable[Mapping[str, Any]]) -> ContextComposition:
    """Sum per-conversation composition rows; every number is the rows' own."""
    rows = list(rows)
    shares = {
        key: sum(_int(row, _ROW_COLUMN.get(key, key)) for row in rows)
        for key in RECORDED_CATEGORIES
    }
    provider = {field: sum(_int(row, f"main_{field}") for row in rows) for field in TOTAL_FIELDS}
    return ContextComposition(
        recorded=RecordedComposition(
            categories=[
                RecordedShare(key=key, tokens=tokens) for key, tokens in shares.items() if tokens
            ],
            total_tokens=sum(shares.values()),
            turns=sum(_int(row, "turns") for row in rows),
            turns_profiled=sum(_int(row, "turns_profiled") for row in rows),
            turns_truncated=sum(_int(row, "turns_truncated") for row in rows),
        ),
        provider=ProviderTotals(
            **provider,
            subagent_requests=sum(_int(row, "subagent_requests") for row in rows),
            subagent_prompt_tokens=sum(_int(row, "subagent_prompt_tokens") for row in rows),
            subagent_output_tokens=sum(_int(row, "subagent_output_tokens") for row in rows),
        ),
    )


class ContextActivityDay(BaseModel):
    """One UTC day's activity among the matching runs (the heatmap's cell)."""

    model_config = ConfigDict(extra="forbid")

    day: date
    turns: int = Field(ge=0)
    conversations: int = Field(ge=0)
    main_requests: int = Field(ge=0)
    compactions: int = Field(ge=0)
    peak_prompt_tokens: int | None = None


class ContextProject(BaseModel):
    """One project of the matching runs: a repository, or none (temporary workspaces)."""

    model_config = ConfigDict(extra="forbid")

    #: The conversation's repository; None groups the temporary workspaces.
    repo: str | None = None
    conversations: int = Field(ge=0)
    turns: int = Field(ge=0)
    main_requests: int = Field(ge=0)
    compactions: int = Field(ge=0)
    peak_prompt_tokens: int | None = None
    last_turn_at: datetime


class InsightsFilterEcho(BaseModel):
    """The filters this page applied, as the server understood them."""

    model_config = ConfigDict(extra="forbid")

    q: str = ""
    repo: str | None = None
    temporary: bool = False
    day: date | None = None


class ConversationContext(BaseModel):
    """One conversation's context profile over its runs in the range."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    title: str | None = None
    repo: str | None = None
    archived: bool = False
    #: The conversation's latest turn, which a link opens.
    latest_job_id: str
    turns: int = Field(ge=0)
    first_turn_at: datetime
    last_turn_at: datetime
    runtimes: list[str]
    models: list[str]
    attempts: int = Field(ge=0)
    #: Attempts whose profile is current; the rest are still to be derived.
    attempts_profiled: int = Field(ge=0)
    main_requests: int = Field(ge=0)
    subagent_requests: int = Field(ge=0)
    peak_prompt_tokens: int | None = None
    latest_prompt_tokens: int | None = None
    latest_observed_at: datetime | None = None
    #: The largest window a CLI reported for these runs; None when none did.
    context_window: int | None = None
    compactions: int = Field(ge=0)
    #: Gateway-recorded usage and cost of these runs, summed by the server.
    usage: AdminUsageAggregate | None = None
    #: Recorded content and reported request sizes of these runs.
    composition: ContextComposition | None = None


class ContextInsightsSummary(BaseModel):
    """Totals over every conversation in the range, not only this page."""

    model_config = ConfigDict(extra="forbid")

    conversations: int = Field(ge=0)
    conversations_observed: int = Field(ge=0)
    turns: int = Field(default=0, ge=0)
    main_requests: int = Field(ge=0)
    subagent_requests: int = Field(ge=0)
    compactions: int = Field(ge=0)
    peak_prompt_tokens: int | None = None
    peak_thread_id: str | None = None
    #: Runs in the range whose conversation was deleted: counted in usage,
    #: not listed here, because there is nothing left to open.
    deleted_conversation_runs: int = Field(ge=0)


class ContextProfiling(BaseModel):
    """How complete the profiles behind this page are."""

    model_config = ConfigDict(extra="forbid")

    #: Attempts in the range still without a current profile.
    pending_attempts: int = Field(ge=0)
    #: ``pending_attempts`` stopped counting at its cap.
    pending_capped: bool = False
    #: Attempts this read profiled before answering.
    refreshed: int = Field(ge=0)
    complete: bool


class ContextInsightsPage(BaseModel):
    """A page of the owner's conversations, ranked by one context measure."""

    model_config = ConfigDict(extra="forbid")

    period: UsageRange
    sort: ContextSort
    time_basis: Literal["job_created_at"] = "job_created_at"
    generated_at: datetime
    summary: ContextInsightsSummary
    profiling: ContextProfiling
    conversations: list[ConversationContext]
    next_cursor: str | None = None
    filters: InsightsFilterEcho = Field(default_factory=InsightsFilterEcho)
    #: The first page only (a later page continues its list, not its charts).
    #: Every UTC day of the range with any matching run, oldest first: the
    #: whole range, the day filter aside, never a window of it.
    activity: list[ContextActivityDay] | None = None
    #: Every project of the matching runs, the project filter aside.
    projects: list[ContextProject] | None = None
    #: Every matching conversation's composition summed.
    composition: ContextComposition | None = None


class ContextInsightsCursor(BaseModel):
    """Continue a ranking after its previous page's last conversation."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    scope: str
    since: datetime
    until: datetime
    number: int
    at: datetime
    thread_id: str = Field(min_length=1, max_length=256)
