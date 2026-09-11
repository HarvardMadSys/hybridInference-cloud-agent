"""Content-free admin read models and scoped pagination for stored observations."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypeVar

import jwt
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from cloud_agent import identity
from cloud_agent.grant_usage import (
    GatewayCursor,  # noqa: TC001 — Pydantic resolves this at runtime.
)
from cloud_agent.ledger import JobUsageResponse  # noqa: TC001 — Pydantic resolves this at runtime.
from cloud_agent.session_statistics import (
    SessionStatistics,  # noqa: TC001 — Pydantic resolves this at runtime.
)
from cloud_agent_host.event_payloads import MAX_EVENT_PAYLOAD_BYTES


class AdminRun(BaseModel):
    """One user turn, without its prompt, result, raw error or caller metadata."""

    id: str
    thread_id: str | None = None
    title: str | None = None
    turn_no: int
    user_id: str
    repo: str
    runtime: str
    model: str
    reasoning_effort: str | None = None
    execution_profile: str
    state: str
    created_at: datetime
    updated_at: datetime
    hosts: list[str] = Field(default_factory=list)
    physical_hosts: list[str] = Field(
        default_factory=list,
        description="Distinct physical host labels, falling back to runner IDs when unreported.",
    )
    tool_calls: int = 0


class AdminRunList(BaseModel):
    """A stable creation-time window and a keyset page within it."""

    runs: list[AdminRun]
    since: datetime
    until: datetime
    next_cursor: str | None = None


class AdminAttempt(BaseModel):
    """The execution identity needed to scope events and resource measurements."""

    attempt_id: int
    attempt_no: int
    host: str | None = None
    status: str
    started_at: datetime
    finished_at: datetime | None = None


class AdminCapabilities(BaseModel):
    """Declare which admin read surfaces this build serves.

    A boolean capability says the read surface exists. Whether one particular
    execution can be read is answered per source by the source list, so a run
    list never probes the gateway to compute a column.
    """

    llm_requests: bool = True


class AdminRunDetail(BaseModel):
    """Stored activities and usage, with no inferred failure or queue analytics."""

    run: AdminRun
    attempts: list[AdminAttempt]
    usage: JobUsageResponse
    session_statistics: SessionStatistics | None = None
    capabilities: AdminCapabilities = Field(default_factory=AdminCapabilities)


class AdminActivity(BaseModel):
    """An allowlisted event envelope; created_at is the receipt timestamp."""

    id: int
    attempt_id: int
    attempt_no: int
    seq: int
    event_type: str
    created_at: datetime
    tool_use_id: str | None = None
    tool_name: str | None = None
    is_error: bool | None = None
    has_content: bool = False


class AdminActivityPage(BaseModel):
    """Events ordered within attempts, from a fixed upper event ID."""

    events: list[AdminActivity]
    next_cursor: str | None = None


class AdminEventContent(BaseModel):
    """One explicitly requested, audited and byte-bounded event payload."""

    id: int
    attempt_id: int
    seq: int
    event_type: str
    created_at: datetime
    payload: dict[str, Any]
    truncated: bool = False


def bounded_event_content(row: dict[str, Any]) -> AdminEventContent:
    """Cap the complete response, including JSON escapes, envelope and notice."""
    content = AdminEventContent.model_validate(row)
    if len(content.model_dump_json().encode("utf-8")) <= MAX_EVENT_PAYLOAD_BYTES:
        return content
    original = json.dumps(content.payload, ensure_ascii=False)
    original_bytes = len(original.encode("utf-8"))
    content.truncated = True
    content.payload = {
        "preview": "",
        "truncated": True,
        "original_payload_bytes": original_bytes,
        "truncation_notice": "Event exceeds the 256 KiB response limit; showing a preview.",
    }
    low, high = 0, min(len(original), MAX_EVENT_PAYLOAD_BYTES)
    while low < high:
        middle = (low + high + 1) // 2
        content.payload["preview"] = original[:middle]
        if len(content.model_dump_json().encode("utf-8")) <= MAX_EVENT_PAYLOAD_BYTES:
            low = middle
        else:
            high = middle - 1
    content.payload["preview"] = original[:low]
    return content


class RunFilters(BaseModel):
    """Exact-match selectors; none of them searches event or prompt content."""

    user_id: str | None = Field(None, max_length=256)
    repo: str | None = Field(None, max_length=256)
    state: str | None = Field(None, max_length=64)
    runtime: str | None = Field(None, max_length=128)
    model: str | None = Field(None, max_length=256)
    host: str | None = Field(None, max_length=256)


class RunCursor(BaseModel):
    """Creation-time tie breaker, bound to the filters and original window."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    scope: str
    since: AwareDatetime
    until: AwareDatetime
    created_at: AwareDatetime
    job_id: str = Field(min_length=1, max_length=256)


class ActivityCursor(BaseModel):
    """Do not reuse an attempt-local sequence as a job-wide cursor."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    scope: str
    snapshot_id: int = Field(ge=0, le=2**63 - 1)
    attempt_no: int = Field(ge=1, le=2**31 - 1)
    attempt_id: int = Field(ge=1, le=2**63 - 1)
    seq: int = Field(ge=0, le=2**31 - 1)


_Cursor = TypeVar("_Cursor", bound=BaseModel)


def cursor_scope(values: Any) -> str:
    """Bind an opaque cursor to the same query; this is not authentication."""
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def encode_cursor(cursor: BaseModel) -> str:
    """Encode a small, versioned cursor without exposing arbitrary SQL values."""
    return base64.urlsafe_b64encode(cursor.model_dump_json().encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None, schema: type[_Cursor], scope: str) -> _Cursor | None:
    """Reject malformed, oversized or filter-mismatched cursors before SQL."""
    if cursor is None:
        return None
    try:
        if not cursor or len(cursor) > 2048:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        parsed = schema.model_validate_json(raw)
        if getattr(parsed, "scope", None) != scope:
            raise ValueError
        return parsed
    except (ValueError, binascii.Error, ValidationError):
        raise ValueError("Invalid pagination cursor for these filters.") from None


def resolve_run_window(
    *,
    since: datetime | None,
    until: datetime | None,
    cursor: RunCursor | None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Keep a seven-day default stable on later pages; bound each query to a year."""
    if cursor is not None:
        if (since is not None and since != cursor.since) or (
            until is not None and until != cursor.until
        ):
            raise ValueError("The pagination cursor belongs to a different time window.")
        since, until = cursor.since, cursor.until
    until = until or now or datetime.now(UTC)
    since = since or until - timedelta(days=7)
    if since.tzinfo is None or until.tzinfo is None:
        raise ValueError("Time window boundaries must include a time zone.")
    if not since < until or until - since > timedelta(days=366):
        raise ValueError("Use a positive time window of at most 366 days.")
    if cursor is not None and not since <= cursor.created_at < until:
        raise ValueError("The pagination cursor is outside its time window.")
    return since, until


# ── Per-request LLM detail: stored references and one page of rows ──────
#
# The rows themselves live in the gateway's ledger. What this service stores is
# the reference that says which grant and window a turn's requests belong to,
# so these models describe attribution first and content never: no prompt, no
# response, no gateway credential and no raw request metadata crosses here.

#: Reason codes a source can carry. Stable strings: the browser writes its copy
#: from them, so they are part of the admin read contract.
SOURCE_REASON_ATTRIBUTION_UNAVAILABLE = "attribution_unavailable"
SOURCE_REASON_BINDING_CHANGED = "binding_changed"
SOURCE_REASON_INVALID_WINDOW = "invalid_window"

#: Order an attempt that has no reference after every attempt that has one, so
#: a keyset page can never lose it between ``NULL`` and a real ledger id.
SOURCE_LEDGER_SENTINEL = 2**63 - 1

#: How many stored references or request rows one page may carry. The gateway
#: accepts up to 500; this service asks for less because the page is read by a
#: browser and re-validated row by row.
MAX_LLM_REQUEST_PAGE = 100

#: How long a request-page continuation stays usable. Long enough to read a few
#: pages by hand; expiry always means "start again from page one", which is the
#: only way to see rows that were logged late.
REQUEST_CURSOR_TTL = timedelta(minutes=30)

#: The largest continuation this service accepts. The gateway's own opaque
#: cursor travels inside it and is bounded separately by its own contract, so
#: this ceiling is about the signed envelope, not about what it carries.
MAX_REQUEST_CURSOR_BYTES = 4096

#: The largest admin page response this service will serialize. It matches the
#: ceiling the gateway read is bounded by, and a test holds the two together: a
#: page this service could not forward is a page it must not claim to have
#: answered.
MAX_ADMIN_RESPONSE_BYTES = 256 * 1024


class AdminLlmRequestSource(BaseModel):
    """One stored attribution reference for a run's gateway request rows.

    ``availability`` says only that this service holds a reference and a
    configuration it could query. It promises nothing about the gateway being
    reachable; the page that is actually read reports its own state.
    """

    source_id: str | None = None
    attempt_id: int
    attempt_no: int
    window_since: datetime | None = None
    window_until: datetime | None = None
    window_state: Literal["open", "closed", "unknown"] = "unknown"
    availability: Literal["queryable", "pending", "unavailable"] = "unavailable"
    reason: str | None = None


class AdminLlmRequestSourceList(BaseModel):
    """A stable page of stored references, in execution order."""

    job_id: str
    observed_at: datetime
    sources: list[AdminLlmRequestSource]
    next_cursor: str | None = None
    #: Set to ``not_started`` when the run has no execution to attribute at all.
    reason: str | None = None


class AdminLlmRequestWindow(BaseModel):
    """The fixed half-open interval the rows were selected from."""

    since: datetime
    until: datetime
    basis: Literal["request_started_at"] = "request_started_at"


class AdminLlmRequest(BaseModel):
    """One logged gateway request, as safe metadata only."""

    request_id: str
    request_started_at: datetime
    logged_at: datetime
    model: str | None = None
    served_model_id: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    reasoning: int | None = None
    spent_usd: str | None = None
    ttft_ms: int | None = None
    latency_ms: int | None = None
    status_code: int | None = None
    usage_estimated: bool = False


class AdminLlmRequestPage(BaseModel):
    """One page of request rows, with the read time that produced it.

    Each response carries its own ``fetched_at`` and ``gateway_as_of``: pages
    are separate reads, not a frozen snapshot, so a later page must not be
    presented under an earlier page's read time.
    """

    job_id: str
    source_id: str
    attempt_id: int
    attempt_no: int
    window: AdminLlmRequestWindow
    source: Literal["gateway_api_logs"] = "gateway_api_logs"
    fetched_at: datetime
    gateway_as_of: datetime
    pagination_consistency: Literal["live_keyset"] = "live_keyset"
    finalized: Literal[False] = False
    requests: list[AdminLlmRequest]
    next_cursor: str | None = None


class LlmRequestSourceCursor(BaseModel):
    """Keyset cursor over ``(attempt_no, attempt_id, ledger id)``."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    scope: str
    attempt_no: int = Field(ge=1, le=2**31 - 1)
    attempt_id: int = Field(ge=1, le=2**63 - 1)
    ledger_key: int = Field(ge=0, le=2**63 - 1)


class LlmRequestCursor(BaseModel):
    """A signed continuation binding one page to one exact query."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    admin: str = Field(min_length=1, max_length=256)
    job_id: str = Field(min_length=1, max_length=256)
    source_id: str = Field(min_length=1, max_length=32)
    fingerprint: str = Field(min_length=1, max_length=128)
    since: AwareDatetime
    until: AwareDatetime
    limit: int = Field(ge=1, le=MAX_LLM_REQUEST_PAGE)
    request_started_at: AwareDatetime
    request_id: str = Field(min_length=1, max_length=256)
    gateway_cursor: GatewayCursor | None = None


class RequestCursorError(ValueError):
    """A continuation that cannot be resumed, and why.

    ``expired`` and ``changed`` are recoverable by restarting from the first
    page; ``invalid`` is not a continuation of this query at all.
    """

    def __init__(self, reason: Literal["invalid", "expired", "changed"]) -> None:
        """Record the machine reason; the message never carries the cursor."""
        super().__init__(reason)
        self.reason = reason


class RequestCursorTooLong(ValueError):
    """A continuation this service would refuse on the way back in.

    Signing always inflates: JWT escapes non-ASCII identifiers and appends a
    signature, so a page whose identifiers are all legal can still produce a
    cursor past ``MAX_REQUEST_CURSOR_BYTES``. Issuing one would hand a browser a
    link that fails on the very next page, so the page that would have produced
    it is refused instead.
    """


def expected_gateway_job_id(*, thread_id: str | None) -> str | None:
    """Return the attribution key a stored reference implies, if it implies one.

    Ledger references are captured from a thread residency grant, which is
    minted with ``external_job_id = thread:{thread_id}``. A job with no thread
    cannot have one, and this answers ``None`` rather than guessing a
    job-scoped key: a second attribution shape has to extend this deliberately
    instead of being silently accepted by a permissive check.
    """
    return f"thread:{thread_id}" if thread_id else None


def request_source_fingerprint(row: dict[str, Any]) -> str:
    """Fingerprint one stored reference so a continuation cannot drift off it.

    Everything a later page must not silently switch from is hashed together:
    the ledger row, its attempt, the gateway installation, the grant, the
    residency generation and the window start. The live end of a still-running
    window is deliberately excluded — it advances with the clock, and treating
    that as a change would fail every running turn's second page.
    """
    window_since = row.get("window_since")
    values = [
        row.get("ledger_id"),
        row.get("attempt_id"),
        row.get("binding_id"),
        row.get("grant_id"),
        row.get("residency_generation"),
        window_since.astimezone(UTC).isoformat() if window_since is not None else None,
    ]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def llm_request_source_view(
    row: dict[str, Any],
    *,
    current_binding_id: str,
    expected_external_job_id: str | None,
) -> AdminLlmRequestSource:
    """Describe one stored reference without promising gateway reachability.

    An attempt with no reference keeps its own row: silently dropping it would
    read as "this execution made no requests" rather than "this execution was
    never attributed".
    """
    since = row.get("window_since")
    until = row.get("window_until")
    state: Literal["open", "closed", "unknown"] = "unknown"
    if since is not None and until is not None:
        state = "open" if row.get("window_open") else "closed"

    def view(
        availability: Literal["queryable", "pending", "unavailable"], reason: str | None
    ) -> AdminLlmRequestSource:
        return AdminLlmRequestSource(
            source_id=str(row["ledger_id"]) if row.get("ledger_id") is not None else None,
            attempt_id=row["attempt_id"],
            attempt_no=row["attempt_no"],
            window_since=since,
            window_until=until,
            window_state=state,
            availability=availability,
            reason=reason,
        )

    if row.get("ledger_id") is None:
        return view("unavailable", SOURCE_REASON_ATTRIBUTION_UNAVAILABLE)
    if row.get("binding_id") != current_binding_id:
        # History from another gateway installation. Reading it here would mean
        # sending this installation's reference to a service that never issued
        # it, so the source is listed and refused instead.
        return view("unavailable", SOURCE_REASON_BINDING_CHANGED)
    if expected_external_job_id is None:
        return view("unavailable", SOURCE_REASON_ATTRIBUTION_UNAVAILABLE)
    if since is None or until is None or since >= until:
        return view("pending", SOURCE_REASON_INVALID_WINDOW)
    return view("queryable", None)


def encode_llm_request_cursor(
    cursor: LlmRequestCursor, *, issued_at: datetime | None = None
) -> str:
    """Sign one continuation envelope with this service's own session secret.

    Raises:
        RequestCursorTooLong: If the signed envelope is larger than this
            service accepts as input, so a cursor it could not resume is never
            minted in the first place.
        identity.IdentityNotConfigured: If this deployment cannot sign.
    """
    now = issued_at or datetime.now(UTC)
    claims = {
        **cursor.model_dump(mode="json"),
        "iat": int(now.timestamp()),
        "exp": int((now + REQUEST_CURSOR_TTL).timestamp()),
    }
    token = jwt.encode(claims, identity.admin_llm_request_cursor_secret(), algorithm="HS256")
    if len(token.encode("utf-8")) > MAX_REQUEST_CURSOR_BYTES:
        raise RequestCursorTooLong("the continuation would exceed its own input limit")
    return token


def response_body_bytes(page: AdminLlmRequestPage) -> int:
    """Size of an admin page as it will actually be serialized, in UTF-8 bytes.

    Measured on the finished model, after the continuation is attached and with
    the same encoder the response uses. A ceiling checked against the gateway's
    model plus a guessed headroom is not a ceiling over what the browser
    receives: identifiers, the window and a signed cursor all ride along.
    """
    body = json.dumps(page.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    return len(body.encode("utf-8"))


def decode_llm_request_cursor(
    token: str,
    *,
    admin: str,
    job_id: str,
    source_id: str,
    fingerprint: str,
    limit: int,
) -> LlmRequestCursor:
    """Verify a continuation and check it belongs to this exact request.

    Signature verification is the smallest part. Every page re-authenticates
    the administrator and re-reads the stored reference, so a cursor carries no
    authority of its own; what it carries is the query it continues, and one
    minted for another administrator, job, source, page size or stored
    reference is refused instead of answered.

    Raises:
        RequestCursorError: ``expired`` when the envelope ran out of time,
            ``changed`` when the stored reference it was minted against is no
            longer the one in front of us, and ``invalid`` for a malformed,
            oversized or differently scoped cursor.
        identity.IdentityNotConfigured: If this deployment cannot sign.
    """
    if not token or len(token.encode("utf-8")) > MAX_REQUEST_CURSOR_BYTES:
        raise RequestCursorError("invalid")
    try:
        claims = jwt.decode(
            token,
            identity.admin_llm_request_cursor_secret(),
            algorithms=["HS256"],
            options={"require": ["iat", "exp"]},
        )
    except jwt.ExpiredSignatureError:
        raise RequestCursorError("expired") from None
    except jwt.PyJWTError:
        raise RequestCursorError("invalid") from None
    try:
        # The envelope's own lifetime claims are checked by the decoder above;
        # they are not part of the query this cursor describes.
        cursor = LlmRequestCursor.model_validate(
            {key: value for key, value in claims.items() if key not in ("iat", "exp")}
        )
    except ValidationError:
        raise RequestCursorError("invalid") from None
    if (
        cursor.admin != admin
        or cursor.job_id != job_id
        or cursor.source_id != source_id
        or cursor.limit != limit
    ):
        raise RequestCursorError("invalid")
    if cursor.fingerprint != fingerprint:
        raise RequestCursorError("changed")
    return cursor
