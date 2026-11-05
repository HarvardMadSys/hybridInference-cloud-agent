"""The admin audit trail, read back for the Audit log page.

What is recorded, and how each action is filed, is declared once in
:mod:`cloud_agent.domain.audit`. This route answers "who did what, to whom,
with what result": newest first, a page at a time, by default only what
people did — an administrator's session or break-glass access — and on request
everything, the automation, the control plane's own policy and accounts
changing themselves included. It narrows to one filter, one affected user or
one target.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from cloud_agent.api.deps import get_storage, require_store, verify_admin_access
from cloud_agent.domain.audit import (
    PEOPLE,
    AuditCategory,
    actions_in,
    actor_account_id,
    attribute,
    audit_action,
)
from cloud_agent.ports import AuditLogQueries  # noqa: TC001 — FastAPI resolves this at runtime.
from cloud_agent.schemas import AdminAuditLogEntry, AdminAuditLogResponse, AuditWriteFailures

#: The most entries one page may ask for.
MAX_PAGE = 100

#: How far back the page's "audit writes failed" notice looks.
FAILURE_WINDOW = timedelta(days=7)


def audit_category(action: str) -> AuditCategory | None:
    """The filter that shows ``action``, or ``None`` for an undeclared action."""
    declared = audit_action(action)
    return declared.category if declared is not None else None


class _AdminAuditStorage(Protocol):
    """The one capability this module reaches the storage layer for."""

    audit_log: AuditLogQueries


def get_audit_log_queries(
    store: _AdminAuditStorage | None = Depends(get_storage),
) -> AuditLogQueries:
    """Expose only the audit trail's read to this route."""
    return require_store(store).audit_log


def _no_store(response: Response) -> None:
    """Who did what is current-state information; never serve it from a cache."""
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/admin/agent/audit-log",
    dependencies=[Depends(_no_store), Depends(verify_admin_access)],
)


def _entry(row: dict[str, Any]) -> AdminAuditLogEntry:
    details = row.get("details")
    details = details if isinstance(details, dict) else None
    declared = audit_action(row["action"])
    # A row the previous release wrote during the rollout has no attribution
    # stored; it reads as this release would have written it.
    fallback = attribute(
        row["action"],
        details=details,
        subject_user_id=row.get("target_user_id"),
        success=row["success"],
    )
    return AdminAuditLogEntry(
        id=row["id"],
        action=row["action"],
        category=declared.category if declared is not None else None,
        kind=declared.kind if declared is not None else None,
        actor_id=row.get("actor_id"),
        actor_type=row["actor_type"],
        actor_account_id=actor_account_id(row.get("actor_id")),
        actor_label=row.get("actor_label"),
        actor_deleted=bool(row.get("actor_deleted")),
        admin_ip=row["admin_ip"],
        target_user_id=row.get("target_user_id"),
        subject_label=row.get("subject_label"),
        subject_deleted=bool(row.get("subject_deleted")),
        target_type=row.get("target_type") or fallback.target_type,
        target_id=row.get("target_id") or fallback.target_id,
        target_label=row.get("target_label"),
        details=details,
        success=row["success"],
        outcome=row.get("outcome") or fallback.outcome,
        result=row.get("result"),
        result_at=row.get("result_at"),
        reason=row.get("reason"),
        created_at=row["created_at"],
    )


@router.get("", response_model=AdminAuditLogResponse)
async def list_admin_audit_log(
    category: AuditCategory | None = None,
    actors: Literal["people", "all"] = Query(
        "people",
        description=(
            "``people``: administrators' sessions and break-glass access. ``all`` adds "
            "services, the control plane's own policy and accounts changing themselves."
        ),
    ),
    user: str | None = Query(
        None, max_length=128, description="Only rows about this account or what it owns."
    ),
    target_type: str | None = Query(None, max_length=64),
    target_id: str | None = Query(None, max_length=512),
    before: int | None = Query(None, ge=1, description="Return entries older than this id."),
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    queries: AuditLogQueries = Depends(get_audit_log_queries),
) -> AdminAuditLogResponse:
    """Return recorded actions, newest first, a page at a time."""
    if (target_type is None) != (target_id is None):
        raise HTTPException(
            status_code=422, detail="Pass target_type and target_id together, or neither."
        )
    rows = await queries.list_admin_actions(
        actions=actions_in(category) if category is not None else (),
        actor_types=PEOPLE if actors == "people" else (),
        subject_user_id=user,
        target=(target_type, target_id) if target_type and target_id else None,
        before_id=before,
        # One more than asked for says whether an older page exists without
        # counting the whole table.
        limit=limit + 1,
    )
    page = rows[:limit]
    failures, latest = await queries.write_failures_since(datetime.now(UTC) - FAILURE_WINDOW)
    return AdminAuditLogResponse(
        entries=[_entry(row) for row in page],
        next_before=page[-1]["id"] if len(rows) > limit else None,
        write_failures=AuditWriteFailures(count=failures, latest_at=latest),
    )
