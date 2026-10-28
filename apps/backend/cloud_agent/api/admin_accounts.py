"""Local account administration: ``/v1/admin/accounts`` (local mode).

Who may sign in and who may use Agent, nothing else: no API keys, billing,
model roles or Gateway operations. An administrator is an account holding
current local ``agent.admin``, or the deployment's ``ADMIN_TOKEN``; a Gateway
role confers nothing here.

Every action names the version the administrator read and is refused with 409
when the account has changed since. The change, its new version and its audit
row commit in one transaction. An action that takes Agent use away (revoke,
suspend) then stops the account's queued, running and resident work; if that
step fails, the enforcement pass repeats it within its interval.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request

from cloud_agent.api._local_auth import (
    LocalAuthRoute,
    account_entry,
    local_accounts,
    require_json,
    require_local_mode,
)
from cloud_agent.api.deps import (
    get_client_ip,
    get_grant_client,
    get_storage,
    require_store,
    verify_admin_access,
)
from cloud_agent.application.owner_termination import terminate_owner_access
from cloud_agent.logging_utils import get_logger
from cloud_agent.schemas.accounts import AccountActionRequest, AccountEntry, AdminAccountsResponse

logger = get_logger(__name__)

router = APIRouter(
    prefix="/v1/admin/accounts",
    tags=["admin-accounts"],
    route_class=LocalAuthRoute,
    dependencies=[Depends(require_local_mode)],
)

AdmissionFilter = Literal["not_applied", "pending", "approved", "rejected", "revoked"]


@router.get("", response_model=AdminAccountsResponse)
async def list_accounts(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
    status: AdmissionFilter | None = Query(None),
    _admin: str = Depends(verify_admin_access),
) -> AdminAccountsResponse:
    """One page of accounts, newest first, optionally by application status."""
    records, total = await local_accounts(request).list_accounts(
        admission_status=status, limit=limit, offset=offset
    )
    return AdminAccountsResponse(
        accounts=[account_entry(record) for record in records], total=total
    )


@router.patch("/{account_id}", response_model=AccountEntry, dependencies=[Depends(require_json)])
async def change_account(
    account_id: str,
    body: AccountActionRequest,
    request: Request,
    background: BackgroundTasks,
    admin: str = Depends(verify_admin_access),
    grants: Any = Depends(get_grant_client),
) -> AccountEntry:
    """Apply one action at the version the administrator read."""
    actor_account = admin.removeprefix("admin:") if admin.startswith("admin:") else None
    result = await local_accounts(request).apply_admin_action(
        account_id=account_id,
        action=body.action,
        reason=(body.reason or "").strip() or None,
        expected_version=body.expected_version,
        actor_id=admin,
        actor_account_id=actor_account,
        admin_ip=get_client_ip(request),
    )
    if result.edit.ends_access:
        # Committed already. The pass runs once the answer is sent, and the
        # enforcement pass repeats whatever it leaves undone.
        background.add_task(
            _terminate,
            require_store(get_storage(request)),
            grants,
            account_id,
            result.account.version,
        )
    return account_entry(result.account)


async def _terminate(storage: Any, grants: Any, account_id: str, generation: int) -> None:
    try:
        await terminate_owner_access(storage, grants, account_id, generation=generation)
    except Exception:
        logger.warning(
            "agent_owner_access_termination_deferred",
            exc_info=True,
            extra={"event": "agent_owner_access_termination_deferred", "user_id": account_id},
        )


__all__ = ["router"]
