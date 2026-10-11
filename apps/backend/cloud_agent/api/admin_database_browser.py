"""Admin-only reviewed schema browsing and structured, read-only queries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from cloud_agent.admin_database import (
    REVIEWED_TABLES,
    DatabaseQuery,
    DatabaseQueryInvalid,
    DatabaseQueryResult,
    DatabaseQueryTimeout,
    DatabaseQueryUnavailable,
    DatabaseTables,
)
from cloud_agent.api.deps import (
    get_client_ip,
    get_storage,
    log_admin_action,
    require_store,
    verify_admin_access,
)

if TYPE_CHECKING:
    from cloud_agent.ports import AuditRepository
    from cloud_agent.storage.admin_database_queries import AdminDatabaseQueries


class _Storage(Protocol):
    admin_database: AdminDatabaseQueries
    audit: AuditRepository


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/admin/agent/database",
    tags=["admin-database-browser"],
    dependencies=[Depends(_no_store), Depends(verify_admin_access)],
)


@router.get("/tables", response_model=DatabaseTables)
async def list_database_tables(store: _Storage | None = Depends(get_storage)) -> DatabaseTables:
    """Return reviewed public-table metadata, never unrestricted schema data."""
    try:
        return await require_store(store).admin_database.list_tables()
    except DatabaseQueryTimeout:
        raise HTTPException(
            status_code=408, detail="Database read timed out. Try a narrower query."
        ) from None
    except DatabaseQueryUnavailable:
        raise HTTPException(
            status_code=503, detail="Database browser is temporarily unavailable."
        ) from None


@router.post("/query", response_model=DatabaseQueryResult)
async def query_database_table(
    body: DatabaseQuery,
    request: Request,
    store: _Storage | None = Depends(get_storage),
    actor_id: str = Depends(verify_admin_access),
) -> DatabaseQueryResult:
    """Read a bounded projection and audit identifiers and outcome only."""
    reviewed = REVIEWED_TABLES.get(body.table, ())
    details: dict[str, Any] = {
        "table": body.table if reviewed else None,
        "filter_columns": [item.column for item in body.filters if item.column in reviewed],
        "operators": [item.operator for item in body.filters],
        "order_by": body.order_by if body.order_by in reviewed else None,
        "order_direction": body.order_direction,
        "limit": body.limit,
        "offset": body.offset,
        "count": 0,
    }
    success = False
    try:
        result = await require_store(store).admin_database.query(body)
        details.update(columns=result.columns, count=len(result.rows), has_more=result.has_more)
        success = True
        return result
    except DatabaseQueryInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except DatabaseQueryTimeout:
        raise HTTPException(
            status_code=408, detail="Database read timed out. Try a narrower query."
        ) from None
    except DatabaseQueryUnavailable:
        raise HTTPException(
            status_code=503, detail="Database browser is temporarily unavailable."
        ) from None
    finally:
        await log_admin_action(
            store,
            get_client_ip(request),
            "agent.database.query",
            details=details,
            success=success,
            actor_id=actor_id,
        )
