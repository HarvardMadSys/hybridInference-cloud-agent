"""Administrator mail to accounts: ``/v1/admin/account-mail`` (local mode).

Adapted from HybridInference ``routers/admin/broadcast.py`` at
``8b49872d1dcbed5c4bc8132b026b6740c3437aa7`` (MIT License, Copyright (c)
2026 Harvard SEAS): preview the audience and the message, send it to
yourself as a test, create it (now or at a time), list it, read each
recipient's outcome, and cancel it while it waits. The audience is chosen
by account state and Agent application state, and only verified addresses
receive it; see :mod:`cloud_agent.accounts.broadcast`.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request

from cloud_agent.accounts.broadcast import RenderedBroadcast, render_broadcast
from cloud_agent.accounts.mail import AccountMail
from cloud_agent.api._local_auth import (
    LocalAuthRoute,
    account_mailer,
    local_accounts,
    require_json,
    require_local_mode,
)
from cloud_agent.api.deps import get_client_ip, verify_admin_access
from cloud_agent.api.errors import typed_error
from cloud_agent.schemas.accounts import (
    BroadcastDetailResponse,
    BroadcastEntry,
    BroadcastPreviewRequest,
    BroadcastPreviewResponse,
    BroadcastRecipientEntry,
    BroadcastsResponse,
    CreateBroadcastRequest,
    OkResponse,
)

router = APIRouter(
    prefix="/v1/admin/account-mail",
    tags=["admin-accounts"],
    route_class=LocalAuthRoute,
    dependencies=[Depends(require_local_mode)],
)


def _render(body: BroadcastPreviewRequest) -> RenderedBroadcast:
    try:
        return render_broadcast(
            template_key=body.template_key,
            template_vars=body.template_vars,
            subject=body.subject,
            body_markdown=body.body_markdown,
        )
    except ValueError as exc:
        raise typed_error(422, "invalid_message", str(exc)) from None


def _require_mail(request: Request) -> None:
    if not account_mailer(request).configured:
        raise typed_error(503, "email_unavailable", "Account mail is not configured.")


@router.post(
    "/preview", response_model=BroadcastPreviewResponse, dependencies=[Depends(require_json)]
)
async def preview(
    body: BroadcastPreviewRequest, request: Request, _admin: str = Depends(verify_admin_access)
) -> BroadcastPreviewResponse:
    """How many accounts it would reach now, and the rendered message; sends nothing."""
    rendered = _render(body)
    count = await local_accounts(request).broadcast_audience(
        statuses=list(body.target_statuses), admissions=list(body.target_admissions)
    )
    return BroadcastPreviewResponse(
        recipient_count=count, subject=rendered.subject, html=rendered.html, text=rendered.text
    )


@router.post("/test", response_model=OkResponse, dependencies=[Depends(require_json)])
async def send_test(
    body: BroadcastPreviewRequest,
    request: Request,
    admin: str = Depends(verify_admin_access),
) -> OkResponse:
    """Send the message to the requesting administrator's own verified address only."""
    _require_mail(request)
    rendered = _render(body)
    account_id = admin.removeprefix("admin:") if admin.startswith("admin:") else None
    account = await local_accounts(request).get_account(account_id) if account_id else None
    if account is None or not (account.email and account.email_verified):
        raise typed_error(
            400,
            "no_address",
            "Your account has no verified email address to send a test to.",
        )
    sent = await account_mailer(request).send(
        AccountMail(
            kind="broadcast",
            to=account.email,
            details={
                "subject": f"[TEST] {rendered.subject}",
                "html": rendered.html,
                "text": rendered.text,
            },
        )
    )
    if not sent:
        raise typed_error(502, "send_failed", "The test message was not accepted for delivery.")
    return OkResponse()


@router.post(
    "", response_model=BroadcastEntry, status_code=201, dependencies=[Depends(require_json)]
)
async def create(
    body: CreateBroadcastRequest,
    request: Request,
    background: BackgroundTasks,
    admin: str = Depends(verify_admin_access),
) -> BroadcastEntry:
    """Fix the recipients now and send at ``scheduled_at``, or within the next pass."""
    _require_mail(request)
    rendered = _render(body)
    accounts = local_accounts(request)
    broadcast_id = secrets.token_hex(16)
    await accounts.create_broadcast(
        broadcast_id=broadcast_id,
        subject=rendered.subject,
        body_html=rendered.html,
        body_text=rendered.text,
        template_key=body.template_key,
        statuses=list(body.target_statuses),
        admissions=list(body.target_admissions),
        scheduled_at=body.scheduled_at,
        created_by=admin,
        admin_ip=get_client_ip(request),
    )
    detail = await accounts.broadcast_detail(broadcast_id=broadcast_id, limit=1, offset=0)
    assert detail is not None
    return BroadcastEntry(**detail[0])


@router.get("", response_model=BroadcastsResponse)
async def list_all(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
    _admin: str = Depends(verify_admin_access),
) -> BroadcastsResponse:
    """Every broadcast, newest first."""
    rows, total = await local_accounts(request).list_broadcasts(limit=limit, offset=offset)
    return BroadcastsResponse(broadcasts=[BroadcastEntry(**row) for row in rows], total=total)


@router.get("/{broadcast_id}", response_model=BroadcastDetailResponse)
async def detail(
    broadcast_id: str,
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=1_000_000),
    _admin: str = Depends(verify_admin_access),
) -> BroadcastDetailResponse:
    """One broadcast and each recipient's outcome."""
    found = await local_accounts(request).broadcast_detail(
        broadcast_id=broadcast_id, limit=limit, offset=offset
    )
    if found is None:
        raise typed_error(404, "not_found", "No such message.")
    row, recipients, total = found
    return BroadcastDetailResponse(
        broadcast=BroadcastEntry(**row),
        recipients=[BroadcastRecipientEntry(**recipient) for recipient in recipients],
        total_recipients=total,
    )


@router.delete("/{broadcast_id}", response_model=OkResponse)
async def cancel(
    broadcast_id: str, request: Request, admin: str = Depends(verify_admin_access)
) -> OkResponse:
    """Cancel a message that is still waiting to be sent."""
    status = await local_accounts(request).cancel_broadcast(
        broadcast_id=broadcast_id, actor_id=admin, admin_ip=get_client_ip(request)
    )
    if status is None:
        raise typed_error(404, "not_found", "No such message.")
    if status != "cancelled":
        raise typed_error(409, "not_cancellable", f"A {status} message cannot be cancelled.")
    return OkResponse()


__all__ = ["router"]
