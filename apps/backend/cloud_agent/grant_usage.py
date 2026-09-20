"""Validated, credential-free snapshots of the gateway's usage contract.

The gateway sums the rows that know a metric and counts those that do not.
Preserving both halves is essential: a partial sum is useful, but is not an
assertion that all inference has been accounted for. Reports remain revisable
because gateway request logs are written asynchronously.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from cloud_agent.gateway import GatewayError

USAGE_METRICS = ("tokens_in", "tokens_out", "cache_read", "cache_write", "reasoning", "spent_usd")
#: The metrics a snapshot must know for its totals to count as complete.
#: Reasoning is informational: the Gateway records a reasoning breakdown only
#: when an upstream reports a positive one, and its Anthropic-messages surface
#: never carries the breakdown at all, so a null there means "not reported",
#: never zero. It stays in ``unknown_rows`` so the drawer can say so, but it
#: does not turn an otherwise complete snapshot into a partial one.
CORE_METRICS = tuple(name for name in USAGE_METRICS if name != "reasoning")
NonnegativeInt = Annotated[StrictInt, Field(ge=0)]
#: Digits of a decimal amount, without its point. A gateway report is one
#: request's total; a sum over attempts, jobs or administrators is wider.
DECIMAL_DIGITS = 128
#: One report's amount. The gateway contract fixes this width.
REPORT_AMOUNT_CHARS = DECIMAL_DIGITS
#: A derived amount is never rounded, so its width has to hold the exact sum of
#: every report in a selection: one report may carry 128 digits, a selection may
#: hold 50,000 ledger rows, and the integer part can gain five digits from the
#: carry. 128 + 5 + 1 + 128 = 262; 384 leaves room for the checked bound to sit
#: above it rather than on it.
DERIVED_AMOUNT_CHARS = 384
#: Decimal places the page shows. This is display only: the API returns the whole
#: exact amount and the browser truncates nothing.
DISPLAY_AMOUNT_PLACES = 10


#: The amount shape, named once so the two widths cannot drift apart. It has to
#: be spelled inside each alias rather than returned by a helper: a type alias
#: mypy cannot evaluate is a variable, and a variable is not valid as a type.
_AMOUNT_PATTERN = r"^[0-9]+(?:\.[0-9]+)?$"

Money = Annotated[StrictStr, Field(pattern=_AMOUNT_PATTERN, max_length=REPORT_AMOUNT_CHARS)]
DerivedAmount = Annotated[
    StrictStr, Field(pattern=_AMOUNT_PATTERN, max_length=DERIVED_AMOUNT_CHARS)
]
#: A count a browser can hold without losing precision. The gateway's request
#: columns are 32-bit today; this refuses a future widening rather than letting
#: JavaScript round a token count silently.
SafeCount = Annotated[StrictInt, Field(ge=0, le=2**53 - 1)]
Identifier = Annotated[StrictStr, Field(min_length=1, max_length=128)]
#: Identity fields are bounded but never truncated: a page whose row exceeds
#: these is rejected whole rather than silently renamed. An empty request id
#: identifies nothing, so it is refused rather than shown as a row.
RequestId = Annotated[StrictStr, Field(min_length=1, max_length=256)]
RequestModelId = Annotated[StrictStr, Field(max_length=1024)]
GatewayCursor = Annotated[StrictStr, Field(min_length=1, max_length=2048)]

#: The largest body this service parses from the gateway's request-detail page,
#: counted after decompression. ``Content-Length`` is not a control: it can be
#: absent, wrong, or measured before decompression.
MAX_REQUEST_PAGE_BYTES = 256 * 1024


def _utc_instant(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("a timestamp must include a UTC offset")
    return value.astimezone(UTC)


class _UsageModel(BaseModel):
    # Ignore compatible additive fields, but never retain arbitrary gateway
    # metadata (or a mistakenly returned credential) in a public snapshot.
    model_config = ConfigDict(extra="ignore", frozen=True)


class GrantUsageWindow(_UsageModel):
    """The half-open interval selected by gateway request start time."""

    since: datetime
    until: datetime
    basis: Literal["request_started_at"]

    @field_validator("since", "until", mode="before")
    @classmethod
    def aware_instants(cls, value: Any) -> datetime:
        """Require an explicit time zone rather than guessing the server's."""
        return _utc_instant(value)

    @model_validator(mode="after")
    def ordered_window(self) -> GrantUsageWindow:
        """Reject empty and inverted attribution windows."""
        if self.since >= self.until:
            raise ValueError("the usage window must be nonempty")
        return self


class _UsageTotals(_UsageModel):
    """Fields shared by one report's totals and by every sum derived from them."""

    calls: NonnegativeInt
    tokens_in: NonnegativeInt | None
    tokens_out: NonnegativeInt | None
    cache_read: NonnegativeInt | None
    cache_write: NonnegativeInt | None
    reasoning: NonnegativeInt | None


class GrantUsageTotals(_UsageTotals):
    """One report's sums over the rows that reported each metric, unknowns null."""

    spent_usd: Money | None


class GrantUsageDerivedTotals(_UsageTotals):
    """Sums of valid reports, which may be wider than any single report."""

    spent_usd: DerivedAmount | None


class GrantUsageUnknownRows(_UsageModel):
    """How many request rows lack each metric; never a fabricated zero."""

    tokens_in: NonnegativeInt
    tokens_out: NonnegativeInt
    cache_read: NonnegativeInt
    cache_write: NonnegativeInt
    reasoning: NonnegativeInt
    spent_usd: NonnegativeInt


class GrantUsageReport(_UsageModel):
    """The totals-only v1 report stored by the control plane."""

    schema_version: Literal[1]
    source: Literal["gateway_api_logs"]
    grant_id: Identifier
    external_job_id: Identifier
    attribution: Literal["grant_time_window"]
    window: GrantUsageWindow
    as_of: datetime
    finalized: Literal[False]
    totals: GrantUsageTotals
    unknown_rows: GrantUsageUnknownRows
    estimated_calls: NonnegativeInt

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: Any) -> int:
        """Do not accept JSON true or a string as the integer version one."""
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @field_validator("finalized", mode="before")
    @classmethod
    def boolean_finalization(cls, value: Any) -> bool:
        """A v1 report never promises settlement."""
        if type(value) is not bool:
            raise ValueError("finalized must be a boolean")
        return value

    @field_validator("as_of", mode="before")
    @classmethod
    def aware_as_of(cls, value: Any) -> datetime:
        """Retain when the gateway read its still-changing ledger."""
        return _utc_instant(value)

    @model_validator(mode="after")
    def coherent_counts(self) -> GrantUsageReport:
        """Check that partial sums and missing-row counts describe one report."""
        calls = self.totals.calls
        if self.estimated_calls > calls:
            raise ValueError("estimated_calls exceeds calls")
        for name in USAGE_METRICS:
            unknown = getattr(self.unknown_rows, name)
            if unknown > calls:
                raise ValueError("unknown_rows exceeds calls")
            if (getattr(self.totals, name) is None) != (unknown == calls):
                raise ValueError("a metric is null exactly when no row reports it")
        return self


def parse_usage_report(
    payload: Any, *, grant_id: str, since: datetime, until: datetime
) -> GrantUsageReport:
    """Validate a response against the exact grant and interval requested.

    An old gateway may ignore query parameters and answer with job lifetime
    totals. Accepting that successful HTTP response would silently put the
    entire thread's usage on every turn. The version is therefore mandatory.

    Raises:
        GatewayError: If the gateway lacks v1, or returns an invalid report.
    """
    if not isinstance(payload, dict):
        raise GatewayError(
            "gateway returned an invalid usage report", error_type="usage_invalid_response"
        )
    version = payload.get("schema_version")
    if version is None or (type(version) is int and version != 1):
        raise GatewayError(
            "gateway does not support usage schema v1", error_type="usage_unsupported_schema"
        )
    try:
        report = GrantUsageReport.model_validate(payload)
    except ValueError:
        # Validation errors contain input values; those must not reach a
        # stored error or the browser if a gateway returned a secret by mistake.
        raise GatewayError(
            "gateway returned an invalid usage report", error_type="usage_invalid_response"
        ) from None
    if report.grant_id != grant_id or report.window.since != since or report.window.until != until:
        raise GatewayError(
            "gateway returned usage for a different grant or window",
            error_type="usage_response_mismatch",
        )
    return report


class GrantUsageRequest(_UsageModel):
    """One logged gateway request, with no prompt, response or raw metadata.

    A row is one request the gateway admitted, identified by its request ID.
    Streaming chunks are not separate rows, and a retry inside the gateway need
    not appear at all: this is an entry-request log, never a supplier-attempt
    timeline. A null metric is "not reported" and is never rendered as zero.
    """

    request_id: RequestId
    request_started_at: datetime
    logged_at: datetime
    model: RequestModelId | None
    served_model_id: RequestModelId | None
    tokens_in: SafeCount | None
    tokens_out: SafeCount | None
    cache_read: SafeCount | None
    cache_write: SafeCount | None
    reasoning: SafeCount | None
    spent_usd: Money | None
    ttft_ms: SafeCount | None
    latency_ms: SafeCount | None
    status_code: SafeCount | None
    usage_estimated: StrictBool

    @field_validator("request_started_at", "logged_at", mode="before")
    @classmethod
    def aware_instants(cls, value: Any) -> datetime:
        """Require an explicit time zone on both recorded instants."""
        return _utc_instant(value)


class GrantUsageRequestPage(GrantUsageReport):
    """One window's totals plus a page of that window's request rows.

    The totals cover the whole window rather than the returned page. They are
    carried because the gateway reports them together and are deliberately not
    treated as a page subtotal: this service neither shows them beside a page
    nor writes them back to a stored snapshot.
    """

    requests: list[GrantUsageRequest]
    next_cursor: GatewayCursor | None


def parse_usage_request_page(
    payload: Any,
    *,
    grant_id: str,
    since: datetime,
    until: datetime,
    limit: int,
    external_job_id: str,
    cursor: str | None = None,
    after: tuple[datetime, str] | None = None,
) -> GrantUsageRequestPage:
    """Validate one request page against the exact query that asked for it.

    A totals-only success must not be read as an empty page: an older gateway
    that ignores ``detail=requests`` would otherwise report "no inference" for
    a window it never looked at.

    Args:
        payload: The decoded gateway response.
        grant_id: The grant the query named.
        since: Inclusive lower bound that was sent.
        until: Exclusive upper bound that was sent, echoed and fixed per page.
        limit: The page size that was asked for.
        external_job_id: The attribution key the reference implies.
        cursor: The gateway cursor this page was asked to continue from, if any.
        after: The last ``(request_started_at, request_id)`` of the previous
            page, kept locally so continuation never depends on decoding the
            gateway's opaque cursor.

    Returns:
        The validated page.

    Raises:
        GatewayError: ``usage_unsupported_schema`` if the gateway does not
            speak v1, ``request_details_unsupported`` if its v1 response has no
            ``requests``/``next_cursor``, ``usage_response_mismatch`` if it
            describes another grant, window or job, and
            ``usage_invalid_response`` for an internally inconsistent page.
    """
    if not isinstance(payload, dict):
        raise GatewayError(
            "gateway returned an invalid usage report", error_type="usage_invalid_response"
        )
    version = payload.get("schema_version")
    if version is None or (type(version) is int and version != 1):
        raise GatewayError(
            "gateway does not support usage schema v1", error_type="usage_unsupported_schema"
        )
    if "requests" not in payload or "next_cursor" not in payload:
        raise GatewayError(
            "gateway does not return per-request usage detail",
            error_type="request_details_unsupported",
        )
    try:
        page = GrantUsageRequestPage.model_validate(payload)
    except ValueError:
        raise GatewayError(
            "gateway returned an invalid usage report", error_type="usage_invalid_response"
        ) from None
    if page.grant_id != grant_id or page.window.since != since or page.window.until != until:
        raise GatewayError(
            "gateway returned usage for a different grant or window",
            error_type="usage_response_mismatch",
        )
    if page.external_job_id != external_job_id:
        raise GatewayError(
            "gateway attributed the request page to a different job",
            error_type="usage_response_mismatch",
        )
    _check_request_page(page, limit=limit, since=since, until=until, cursor=cursor, after=after)
    return page


def _check_request_page(
    page: GrantUsageRequestPage,
    *,
    limit: int,
    since: datetime,
    until: datetime,
    cursor: str | None,
    after: tuple[datetime, str] | None,
) -> None:
    """Reject a page that is not a strict continuation of one exact query."""

    def invalid(message: str) -> GatewayError:
        return GatewayError(message, error_type="usage_invalid_response")

    if len(page.requests) > limit:
        raise invalid("gateway returned more rows than were requested")
    seen: set[str] = set()
    previous: tuple[datetime, str] | None = None
    for row in page.requests:
        if row.request_id in seen:
            raise invalid("gateway repeated a request id within one page")
        seen.add(row.request_id)
        if not since <= row.request_started_at < until:
            raise invalid("gateway returned a row outside the requested window")
        key = (row.request_started_at, row.request_id)
        if (previous is not None and key <= previous) or (after is not None and key <= after):
            raise invalid("gateway returned rows out of order")
        previous = key
    if previous is None and page.next_cursor is not None:
        raise invalid("gateway continued an empty page")
    if page.next_cursor is not None and page.next_cursor == cursor:
        raise invalid("gateway repeated the cursor it was given")
