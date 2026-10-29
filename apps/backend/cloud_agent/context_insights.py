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
from datetime import datetime  # noqa: TC003 — Pydantic resolves the model annotations.
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.admin_observability import cursor_scope
from cloud_agent.admin_usage import AdminUsageAggregate  # noqa: TC001
from cloud_agent.usage_range import UsageRange  # noqa: TC001

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


class ContextInsightsSummary(BaseModel):
    """Totals over every conversation in the range, not only this page."""

    model_config = ConfigDict(extra="forbid")

    conversations: int = Field(ge=0)
    conversations_observed: int = Field(ge=0)
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
