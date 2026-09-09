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
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from cloud_agent.gateway import GatewayError

USAGE_METRICS = ("tokens_in", "tokens_out", "cache_read", "cache_write", "reasoning", "spent_usd")
NonnegativeInt = Annotated[StrictInt, Field(ge=0)]
Money = Annotated[StrictStr, Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$", max_length=128)]
Identifier = Annotated[StrictStr, Field(min_length=1, max_length=128)]


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


class GrantUsageTotals(_UsageModel):
    """Sums over the rows that reported each metric, with unknowns left null."""

    calls: NonnegativeInt
    tokens_in: NonnegativeInt | None
    tokens_out: NonnegativeInt | None
    cache_read: NonnegativeInt | None
    cache_write: NonnegativeInt | None
    reasoning: NonnegativeInt | None
    spent_usd: Money | None


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
