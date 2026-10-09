"""The admin audit trail, read back for the Audit log page.

Every admin handler records what it did through
:func:`cloud_agent.api.deps.log_admin_action`. Until this route nothing read
those rows back, so "who changed this, and when" had no answer short of a
database shell. This is that answer: newest first, a page at a time, filtered
by what the change was about.

Only changes, and reads of a user's content, are shown. The Servers page used
to record each of its polling reads as an action — 11,566 of the 11,582 rows
staging wrote in the week before this route — and those rows stay in the
table but never on the page.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from fastapi import APIRouter, Depends, Query, Response

from cloud_agent.api.deps import get_storage, require_store, verify_admin_access
from cloud_agent.ports import AuditLogQueries  # noqa: TC001 — FastAPI resolves this at runtime.
from cloud_agent.schemas import AdminAuditLogEntry, AdminAuditLogResponse

AuditCategory = Literal["settings", "servers", "measurements", "content"]

#: The Audit log's filters and the actions each covers. A name ending in ``.``
#: is a prefix; any other is one exact action. ``settings`` comes first: a
#: server's own settings are recorded under ``agent.runner_host.`` but read as
#: a settings change, and an action belongs to the first filter that covers it.
AUDIT_CATEGORIES: dict[AuditCategory, tuple[str, ...]] = {
    "settings": (
        "agent.host_settings.",
        "agent.runtime_version.",
        "agent.runner_host.set_settings",
    ),
    "servers": ("agent.runner_host.", "agent.compute."),
    "measurements": ("agent.profiling.",),
    "content": ("agent.run.event_content.",),
}

#: Reads the Servers page polls, recorded as actions before this release. They
#: are not changes, so the Audit log never shows the rows already written.
POLLING_READS: tuple[str, ...] = (
    "agent.compute.providers.list",
    "agent.compute.connections.list",
    "agent.compute.servers.list",
    "agent.compute.operations.list",
    "agent.compute.operation.read",
    "agent.compute.allocation.retirement_impact",
)

#: The most entries one page may ask for.
MAX_PAGE = 100


def _covers(pattern: str, action: str) -> bool:
    return action.startswith(pattern) if pattern.endswith(".") else action == pattern


def audit_category(action: str) -> AuditCategory | None:
    """The first filter that covers ``action``, or ``None`` when none does."""
    for category, patterns in AUDIT_CATEGORIES.items():
        if any(_covers(pattern, action) for pattern in patterns):
            return category
    return None


def _excluded_for(category: AuditCategory | None) -> tuple[str, ...]:
    """What a filter leaves out besides polling reads.

    An exact action another filter claims — the per-server settings change
    under ``agent.runner_host.`` — stays out of the broader prefix's filter,
    so every action appears under exactly one.
    """
    if category is None:
        return POLLING_READS
    claimed = tuple(
        pattern
        for other, patterns in AUDIT_CATEGORIES.items()
        if other != category
        for pattern in patterns
        if not pattern.endswith(".")
    )
    return POLLING_READS + claimed


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


router = APIRouter(prefix="/admin/agent/audit-log", dependencies=[Depends(_no_store)])


def _entry(row: dict[str, Any]) -> AdminAuditLogEntry:
    details = row.get("details")
    return AdminAuditLogEntry(
        id=row["id"],
        action=row["action"],
        category=audit_category(row["action"]),
        actor_id=row.get("actor_id"),
        admin_ip=row["admin_ip"],
        target_user_id=row.get("target_user_id"),
        details=details if isinstance(details, dict) else None,
        success=row["success"],
        created_at=row["created_at"],
    )


@router.get("", response_model=AdminAuditLogResponse)
async def list_admin_audit_log(
    category: AuditCategory | None = None,
    before: int | None = Query(None, ge=1, description="Return entries older than this id."),
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    _admin: str = Depends(verify_admin_access),
    queries: AuditLogQueries = Depends(get_audit_log_queries),
) -> AdminAuditLogResponse:
    """Return recorded admin actions, newest first, a page at a time."""
    rows = await queries.list_admin_actions(
        actions=AUDIT_CATEGORIES[category] if category is not None else (),
        excluded_actions=_excluded_for(category),
        before_id=before,
        # One more than asked for says whether an older page exists without
        # counting the whole table.
        limit=limit + 1,
    )
    page = rows[:limit]
    return AdminAuditLogResponse(
        entries=[_entry(row) for row in page],
        next_before=page[-1]["id"] if len(rows) > limit else None,
    )
