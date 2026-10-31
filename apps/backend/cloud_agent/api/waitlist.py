"""The signed-in account's Agent application: ``/v1/waitlist`` (local mode).

Identity only: a verified account reads and submits its application whatever
its admission says, which is the point of the page. Submitting changes the
application and nothing else; the body names a purpose and cannot name a
status. A pending application is not rewritten by a repeat, a rejected or
revoked one is submitted afresh only when its owner asks, and nothing the
account queued before is started again by a new approval.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from cloud_agent.api._local_auth import (
    LocalAuthRoute,
    local_accounts,
    require_json,
    waitlist_entry,
)
from cloud_agent.api.deps import get_current_user, local_session_owner
from cloud_agent.logging_utils import get_logger
from cloud_agent.schemas.accounts import WaitlistEntry, WaitlistSubmitRequest

logger = get_logger(__name__)

router = APIRouter(
    prefix="/v1/waitlist",
    tags=["waitlist"],
    route_class=LocalAuthRoute,
)


@router.get("", response_model=WaitlistEntry)
async def read_application(
    request: Request, user: dict[str, Any] = Depends(get_current_user)
) -> WaitlistEntry:
    """The caller's application, as of this request."""
    owner = await local_session_owner(request, request.headers.get("authorization"))
    assert owner is not None and owner.account.id == user["user_id"]
    return waitlist_entry(owner.account)


@router.post("", response_model=WaitlistEntry, dependencies=[Depends(require_json)])
async def submit_application(
    body: WaitlistSubmitRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> WaitlistEntry:
    """Apply, or apply again after a rejection or revocation."""
    record = await local_accounts(request).submit_application(
        account_id=user["user_id"], purpose=body.purpose.strip()
    )
    logger.info(
        "agent_local_application_submitted",
        extra={"event": "agent_local_application_submitted", "user_id": record.id},
    )
    return waitlist_entry(record)


__all__ = ["router"]
