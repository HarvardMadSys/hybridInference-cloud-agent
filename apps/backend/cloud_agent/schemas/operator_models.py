"""Models the deployment provides, as an administrator sets them up.

A *provided* connection is the deployment's own inference endpoint, offered to
every admitted owner without any setup of theirs. Its key never leaves the
control plane, and every owner has a daily request allowance per model
(``docs/agents/specs/2026-11-12-platform-model-relay.zh-CN.md``). What an owner
is offered is ``schemas.config.ProvidedModelSource``.
"""

from __future__ import annotations

from datetime import (
    date,  # noqa: TC003 — Pydantic resolves annotations at runtime.
    datetime,  # noqa: TC003 — Pydantic resolves annotations at runtime.
)
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: The most an allowance or a concurrency cap is ever set to.
MAX_DAILY_REQUESTS = 10_000_000
MAX_CONCURRENCY = 1024


class OperatorModelLimit(BaseModel):
    """The default allowance of one model, and its cap on requests in flight across owners."""

    #: Requests each owner may send per UTC day; ``None`` is unlimited and
    #: ``0`` offers the model only to owners given an allowance of their own.
    user_daily_requests: int | None = Field(None, ge=0, le=MAX_DAILY_REQUESTS)
    #: Requests in flight across all owners; ``None`` is unlimited. "Global" is
    #: across owners, not across processes: see the description.
    global_concurrency: int | None = Field(
        None,
        ge=1,
        le=MAX_CONCURRENCY,
        description=(
            "Requests to this model in flight across all owners. Counted by each "
            "model relay process on its own requests: a deployment that runs "
            "several relay processes allows this many in each."
        ),
    )


class OperatorModelConnectionView(BaseModel):
    """One provided connection, as an administrator reads it. Never its key."""

    id: str
    name: str
    base_url: str
    models: list[str]
    default_model: str | None = None
    is_default: bool = False
    enabled: bool = False
    #: The header each relayed request names its owner in, by an opaque tag.
    end_user_header: str | None = None
    #: Requests one owner may have in flight at once, in each relay process.
    user_concurrency: int = Field(
        4,
        description=(
            "Requests one owner may have in flight at once. Counted by each model "
            "relay process on its own requests."
        ),
    )
    #: Each model's default allowance; a model absent here has none set yet,
    #: and the connection cannot be enabled until every model has one.
    limits: dict[str, OperatorModelLimit] = Field(default_factory=dict)
    #: Each model's verification, in the shape an owner's connection reports
    #: (``api.model_connections.ModelStatusView``).
    model_status: list[dict[str, Any]] = Field(default_factory=list)
    #: Since when the provider has refused the stored key; ``None`` is healthy.
    key_refused_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OperatorModelConnectionList(BaseModel):
    """Every provided connection, and whether any host can run a turn on one."""

    connections: list[OperatorModelConnectionView] = Field(default_factory=list)
    #: Enrolled hosts that reach the model relay. Zero means a turn on a
    #: provided connection waits until one is configured.
    relay_hosts: int = 0
    #: Whether owner and provided connections are switched on at all.
    feature_enabled: bool = True


class _Write(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OperatorModelLimitWrite(OperatorModelLimit):
    """Set one model's default allowance and concurrency cap."""

    model_config = ConfigDict(extra="forbid")
    model: str = Field(..., min_length=1, max_length=256)


class OperatorModelUserLimitWrite(_Write):
    """Give one account an allowance of its own on one model."""

    #: The account's id, or its email address.
    account: str = Field(..., min_length=1, max_length=320)
    model: str = Field(..., min_length=1, max_length=256)
    #: ``None`` is unlimited; ``0`` withholds the model from this account.
    daily_requests: int | None = Field(None, ge=0, le=MAX_DAILY_REQUESTS)


class OperatorModelUserRow(BaseModel):
    """One account with an allowance of its own, or with use today."""

    account_id: str
    email: str = ""
    #: The account's own allowances, per model; a model absent here follows
    #: the model's default. ``None`` is unlimited.
    limits: dict[str, int | None] = Field(default_factory=dict)
    #: Requests accepted today, per model.
    used_today: dict[str, int] = Field(default_factory=dict)


class OperatorModelUserList(BaseModel):
    """A page of accounts, by account id."""

    users: list[OperatorModelUserRow] = Field(default_factory=list)
    #: Pass as ``cursor`` for the next page; ``None`` on the last.
    next_cursor: str | None = None


class OperatorModelUsageRow(BaseModel):
    """Requests the model relay accepted from one account for one model on one UTC day."""

    day: date
    account_id: str
    email: str = ""
    model: str
    requests: int


class OperatorModelUsageResponse(BaseModel):
    """Accepted requests per day, account and model, newest day first."""

    rows: list[OperatorModelUsageRow] = Field(default_factory=list)
    #: True when more rows matched than are answered.
    truncated: bool = False


__all__ = [
    "MAX_CONCURRENCY",
    "MAX_DAILY_REQUESTS",
    "OperatorModelConnectionList",
    "OperatorModelConnectionView",
    "OperatorModelLimit",
    "OperatorModelLimitWrite",
    "OperatorModelUsageResponse",
    "OperatorModelUsageRow",
    "OperatorModelUserLimitWrite",
    "OperatorModelUserList",
    "OperatorModelUserRow",
]
