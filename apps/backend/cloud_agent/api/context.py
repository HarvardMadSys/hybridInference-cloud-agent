"""Context reads: one conversation's Context view, and the Usage page's insights.

Both are the signed-in owner's own and never cached. The view answers 404 for a
conversation that is not the caller's or is deleted, the same as its stats; the
insights list only the caller's runs. Neither returns another owner's data,
any credential, or the content of an event: profiles hold sizes and counts.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.admin_observability import decode_cursor, encode_cursor
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_grant_client, get_storage, require_store
from cloud_agent.api.owner_usage import resolve_period, usage_unavailable
from cloud_agent.context_dna import MAX_DNA_REQUESTS, DnaBands
from cloud_agent.context_insights import (
    DEFAULT_CONTEXT_PAGE,
    MAX_CONTEXT_PAGE,
    MAX_SEARCH_CHARS,
    PENDING_COUNT_CAP,
    REFRESH_ATTEMPTS_PER_READ,
    REFRESH_EVENTS_PER_READ,
    ContextActivityDay,
    ContextInsightsCursor,
    ContextInsightsPage,
    ContextInsightsSummary,
    ContextProfiling,
    ContextProject,
    ContextSort,
    ConversationContext,
    InsightsFilterEcho,
    InsightsFilters,
    compose,
    context_cursor_scope,
)
from cloud_agent.context_request import RequestComposition
from cloud_agent.context_timing import ThreadTiming, assemble_timing
from cloud_agent.context_view import ThreadContextView, assemble_thread_context
from cloud_agent.logging_utils import get_logger
from cloud_agent.model_ttft import read_request_pages
from cloud_agent.storage.admin_usage_queries import UsageQueryTooLarge, UsageQueryUnavailable
from cloud_agent.usage_batches import owner_usage
from cloud_agent.usage_range import RangePreset  # noqa: TC001 — FastAPI resolves it.

if TYPE_CHECKING:
    from cloud_agent.grants import GrantClient
    from cloud_agent.ports import ContextQueries, OwnerUsageQueries
    from cloud_agent.storage.admin_usage_queries import AdminUsageQueries

logger = get_logger(__name__)


class _ContextStorage(Protocol):
    """The capabilities these reads reach for; every call names the owner."""

    context_queries: ContextQueries
    ledger: Any
    owner_usage: OwnerUsageQueries
    admin_usage: AdminUsageQueries


def _no_store(response: Response) -> None:
    """An owner's context must not outlive the read."""
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/v1/agent", tags=["context"], dependencies=[Depends(_no_store)])


@router.get("/jobs/{job_id}/thread/context", response_model=ThreadContextView)
async def get_thread_context(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _ContextStorage | None = Depends(get_storage),
) -> ThreadContextView:
    """The Context view of the caller's conversation holding ``job_id``."""
    context_store: _ContextStorage = require_store(store)
    result = await context_store.context_queries.thread_context(
        job_id=job_id, user_id=user["user_id"]
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No thread for {job_id}"}},
        )
    return assemble_thread_context(
        thread_id=result["thread_id"],
        generated_at=datetime.now(UTC),
        jobs=result["jobs"],
        attempts=result["attempts"],
    )


#: Started executions whose Gateway request rows one timing read covers, newest
#: first; each costs one bounded page. Older executions stay "not split".
THREAD_TIMING_SOURCES = 16


@router.get("/jobs/{job_id}/thread/context/timing", response_model=ThreadTiming)
async def get_thread_context_timing(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _ContextStorage | None = Depends(get_storage),
    grants: GrantClient = Depends(get_grant_client),
) -> ThreadTiming:
    """Where the caller's conversation's execution time went.

    Read when the dashboard asks, never at settlement: the attempts' recorded
    spans and tool windows, and the Gateway's own request log for the newest
    executions (TTFT and latency per request). Nothing is asked of a CLI.
    """
    context_store: _ContextStorage = require_store(store)
    result = await context_store.context_queries.thread_context(
        job_id=job_id, user_id=user["user_id"]
    )
    read = await context_store.ledger.thread_request_sources(
        job_id=job_id, user_id=user["user_id"], limit=THREAD_TIMING_SOURCES
    )
    if result is None or read is None:
        raise _not_found(job_id)
    sources, more = read
    pages = await read_request_pages(sources, more_sources=more, grants=grants, scope=job_id)
    return assemble_timing(
        result["attempts"], pages.pages, gateway_gap=pages.gap, gateway_started=pages.started
    )


@router.get("/jobs/{job_id}/thread/context/dna", response_model=DnaBands)
async def get_thread_context_dna(
    job_id: str,
    first: int = Query(alias="from", ge=0),
    last: int = Query(alias="to", ge=0),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _ContextStorage | None = Depends(get_storage),
) -> DnaBands:
    """The items each request in ``from``..``to`` carried (recorded estimates).

    At most ``MAX_DNA_REQUESTS`` requests per read: the trend asks for the
    bars it shows, never the whole history.
    """
    if last < first or last - first + 1 > MAX_DNA_REQUESTS:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "type": "invalid_range",
                    "message": f"Ask for 1 to {MAX_DNA_REQUESTS} requests at a time.",
                }
            },
        )
    context_store: _ContextStorage = require_store(store)
    bands = await context_store.context_queries.dna_bands(
        job_id=job_id, user_id=user["user_id"], first=first, last=last
    )
    if bands is None:
        raise _not_found(job_id)
    return bands


#: Characters of one stored event a content read returns.
MAX_CONTENT_WINDOW = 20_000


class EventContent(BaseModel):
    """A window of one stored content event's text."""

    model_config = ConfigDict(extra="forbid")

    event_id: int
    event_type: str
    phase: str | None = None
    offset: int = Field(ge=0)
    text: str
    total_chars: int = Field(ge=0)
    #: The stored event itself was clipped when it was recorded.
    truncated: bool
    next_offset: int | None = None


def _not_found(job_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"error": {"type": "not_found", "message": f"No such request for {job_id}"}},
    )


@router.get("/jobs/{job_id}/thread/context/requests/{event_id}", response_model=RequestComposition)
async def get_request_composition(
    job_id: str,
    event_id: int = Path(ge=1),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _ContextStorage | None = Depends(get_storage),
) -> RequestComposition:
    """What one request of the caller's conversation carried, labelled by kind."""
    context_store: _ContextStorage = require_store(store)
    result = await context_store.context_queries.request_composition(
        job_id=job_id, user_id=user["user_id"], event_id=event_id
    )
    if result is None:
        raise _not_found(job_id)
    return result


@router.get("/jobs/{job_id}/thread/context/content/{event_id}", response_model=EventContent)
async def get_event_content(
    job_id: str,
    event_id: int = Path(ge=1),
    offset: int = Query(0, ge=0, le=10_000_000),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _ContextStorage | None = Depends(get_storage),
) -> EventContent:
    """A window of one stored content event of the caller's conversation.

    The text is what the platform stored, already masked when it was recorded.
    """
    context_store: _ContextStorage = require_store(store)
    row = await context_store.context_queries.event_content(
        job_id=job_id,
        user_id=user["user_id"],
        event_id=event_id,
        offset=offset,
        limit=MAX_CONTENT_WINDOW,
    )
    if row is None:
        raise _not_found(job_id)
    total = int(row["total_chars"] or 0)
    end = offset + len(row["text"] or "")
    return EventContent(
        event_id=event_id,
        event_type=str(row["event_type"]),
        phase=row.get("phase"),
        offset=offset,
        text=str(row["text"] or ""),
        total_chars=total,
        truncated=bool(row["truncated"]),
        next_offset=end if end < total else None,
    )


def _conversation(row: dict[str, Any]) -> ConversationContext:
    return ConversationContext(
        thread_id=row["thread_id"],
        title=row.get("title"),
        repo=row.get("repo"),
        archived=bool(row.get("archived")),
        latest_job_id=row["latest_job_id"],
        turns=int(row["turns"]),
        first_turn_at=row["first_turn_at"],
        last_turn_at=row["last_turn_at"],
        runtimes=sorted(value for value in row.get("runtimes") or [] if value),
        models=sorted(value for value in row.get("models") or [] if value),
        attempts=int(row["attempts"]),
        attempts_profiled=int(row["attempts_profiled"]),
        main_requests=int(row["main_requests"]),
        subagent_requests=int(row["subagent_requests"]),
        peak_prompt_tokens=row.get("peak_prompt_tokens"),
        latest_prompt_tokens=row.get("latest_prompt_tokens"),
        latest_observed_at=row.get("latest_observed_at"),
        context_window=row.get("context_window"),
        compactions=int(row["compactions"]),
    )


def _invalid(message: str) -> HTTPException:
    return HTTPException(
        status_code=422, detail={"error": {"type": "invalid_filter", "message": message}}
    )


def _filters(q: str | None, repo: str | None, temporary: bool, day: date | None) -> InsightsFilters:
    """The list's filters; a day is bound to the range once it is resolved."""
    if repo is not None and temporary:
        raise _invalid("Choose one repository or the temporary workspaces, not both.")
    if repo is not None and not repo.strip():
        raise _invalid("A repository filter needs a repository.")
    since = datetime.combine(day, time(), tzinfo=UTC) if day is not None else None
    return InsightsFilters(
        q=(q or "").strip(),
        repo=repo,
        temporary=temporary,
        day_since=since,
        day_until=since + timedelta(days=1) if since is not None else None,
    )


@router.get("/usage/context", response_model=ContextInsightsPage)
async def read_context_insights(
    period: RangePreset = Query(
        "7d",
        alias="range",
        description="today, 7d, 30d or all, in UTC days; the usage history's own range.",
    ),
    sort: ContextSort = Query("recent"),
    cursor: str | None = Query(None, max_length=2048),
    limit: int = Query(DEFAULT_CONTEXT_PAGE, ge=1, le=MAX_CONTEXT_PAGE),
    q: str | None = Query(
        None,
        max_length=MAX_SEARCH_CHARS,
        description="Title or repository, case-insensitive, over the whole range.",
    ),
    repo: str | None = Query(None, max_length=512, description="One repository's conversations."),
    temporary: bool = Query(False, description="Only conversations without a repository."),
    day: date | None = Query(None, description="One UTC day of the range."),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _ContextStorage | None = Depends(get_storage),
) -> ContextInsightsPage:
    """A ranked page of the owner's conversations with runs in the range.

    Search, project and day narrow the whole range on the server, before it
    is ranked and paged; a cursor is bound to all of them.

    Raises:
        HTTPException: 422 for a cursor from another list or a day outside the
            range, 503 when the page's usage cannot be summed in time; never a
            partial sum.
    """
    context_store: _ContextStorage = require_store(store)
    filters = _filters(q, repo, temporary, day)
    scope = context_cursor_scope(sort, period, filters)
    try:
        after = decode_cursor(cursor, ContextInsightsCursor, scope)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if after is not None:
        # A later page continues the first page's interval, not a new "now".
        resolved = (await resolve_period(context_store, user["user_id"], period, None)).model_copy(
            update={"since": after.since, "until": after.until}
        )
    else:
        resolved = await resolve_period(context_store, user["user_id"], period, None)
    if filters.day_since is not None and not (resolved.since <= filters.day_since < resolved.until):
        # Every preset starts at a UTC midnight, so each of its days is whole.
        raise _invalid("The day is outside the selected range.")
    result = await context_store.context_queries.insights_page(
        user_id=user["user_id"],
        since=resolved.since,
        until=resolved.until,
        sort=sort,
        cursor=after,
        limit=limit,
        refresh_attempts=REFRESH_ATTEMPTS_PER_READ,
        refresh_events=REFRESH_EVENTS_PER_READ,
        pending_cap=PENDING_COUNT_CAP,
        filters=filters,
    )
    rows = result["page"]
    more = len(rows) > limit
    rows = rows[:limit]
    conversations = [_conversation(row) for row in rows]
    if conversations:
        try:
            usage = await owner_usage(
                context_store.admin_usage,
                user_id=user["user_id"],
                since=resolved.since,
                until=resolved.until,
                size=resolved.bucket,
                thread_ids=[conversation.thread_id for conversation in conversations],
            )
        except (UsageQueryTooLarge, UsageQueryUnavailable):
            logger.warning("agent_context_insights_usage_failed")
            raise usage_unavailable() from None
        for conversation in conversations:
            conversation.usage = usage.by_thread.get(conversation.thread_id)
    composition = {row["thread_id"]: row for row in result.get("composition") or []}
    for conversation in conversations:
        found = composition.get(conversation.thread_id)
        conversation.composition = compose([found]) if found is not None else None
    next_cursor = None
    if more and rows:
        last = rows[-1]
        next_cursor = encode_cursor(
            ContextInsightsCursor(
                scope=scope,
                since=resolved.since,
                until=resolved.until,
                number=int(last["sort_number"]),
                at=last["last_turn_at"],
                thread_id=last["thread_id"],
            )
        )
    pending = int(result["pending"])
    return ContextInsightsPage(
        period=resolved,
        sort=sort,
        generated_at=datetime.now(UTC),
        summary=ContextInsightsSummary(**result["summary"]),
        profiling=ContextProfiling(
            pending_attempts=min(pending, PENDING_COUNT_CAP),
            pending_capped=pending > PENDING_COUNT_CAP,
            refreshed=int(result["refreshed"]),
            complete=pending == 0,
        ),
        conversations=conversations,
        next_cursor=next_cursor,
        filters=InsightsFilterEcho(
            q=filters.q,
            repo=filters.repo,
            temporary=filters.temporary,
            day=day,
        ),
        activity=(
            [ContextActivityDay(**row) for row in result["activity"]]
            if after is None and "activity" in result
            else None
        ),
        projects=(
            [ContextProject(**row) for row in result["projects"]]
            if after is None and "projects" in result
            else None
        ),
        composition=compose(composition.values()) if after is None else None,
    )
