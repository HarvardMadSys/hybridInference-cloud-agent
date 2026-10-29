"""Local account administration: ``/v1/admin/accounts`` (local mode).

Who may sign in and who may use Agent, nothing else: no API keys, billing,
model roles or Gateway operations. An administrator is an account holding
current local ``agent.admin``, or the deployment's ``ADMIN_TOKEN``; a Gateway
role confers nothing here.

Every action names the version the administrator read and is refused with 409
when the account has changed since. The change, its new version and its audit
row commit in one transaction. An action that takes Agent use away (revoke,
suspend, delete) then stops the account's queued, running and resident work;
if that step fails, the enforcement pass repeats it within its interval.

As in HybridInference (``routers/admin/users.py``, ``signup_domains.py``,
``login_events.py``), an approval or rejection mails the account, the list
searches identifiers and names, an administrator keeps a private note and the
message a suspended account is shown, the signup domain allowlist is edited
here, and sign-in events are purged by age or by account. A deleted account
is resumed (sign-in only; Agent use needs a fresh approval) or erased.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request

from cloud_agent.accounts.mail import AccountMail
from cloud_agent.accounts.policy import approval_policy, parse_signup_domain
from cloud_agent.api._local_auth import (
    LocalAuthRoute,
    account_entry,
    local_accounts,
    local_flows,
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
from cloud_agent.api.errors import typed_error
from cloud_agent.application.owner_termination import terminate_owner_access
from cloud_agent.logging_utils import get_logger
from cloud_agent.schemas.accounts import (
    AccountActionRequest,
    AccountEntry,
    AccountNotesRequest,
    AdminAccountsResponse,
    EraseAccountRequest,
    LoginEventEntry,
    LoginEventsResponse,
    PurgedResponse,
    SignupDomainEntry,
    SignupDomainRequest,
    SignupDomainsResponse,
)

logger = get_logger(__name__)

router = APIRouter(
    prefix="/v1/admin/accounts",
    tags=["admin-accounts"],
    route_class=LocalAuthRoute,
    dependencies=[Depends(require_local_mode)],
)

AdmissionFilter = Literal["not_applied", "pending", "approved", "rejected", "revoked"]
StatusFilter = Literal["active", "suspended", "deleted"]


def _actor_account(admin: str) -> str | None:
    return admin.removeprefix("admin:") if admin.startswith("admin:") else None


@router.get("", response_model=AdminAccountsResponse)
async def list_accounts(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
    status: AdmissionFilter | None = Query(None),
    account_status: StatusFilter | None = Query(None),
    q: str | None = Query(None, max_length=320),
    _admin: str = Depends(verify_admin_access),
) -> AdminAccountsResponse:
    """One page of accounts, newest first, filtered and searched, with counts per state.

    ``q`` matches an email, login name, phone number or name anywhere, or an
    account id by its prefix.
    """
    accounts = local_accounts(request)
    records, total = await accounts.list_accounts(
        admission_status=status,
        limit=limit,
        offset=offset,
        account_status=account_status,
        query=q,
    )
    return AdminAccountsResponse(
        accounts=[account_entry(record) for record in records],
        total=total,
        counts=await accounts.account_counts(),
    )


# ── Static paths, before the parameterized ones ─────────────────────────


@router.delete("/login-events", response_model=PurgedResponse)
async def purge_login_events(
    request: Request,
    older_than_days: int | None = Query(None, ge=1, le=36500),
    account_id: str | None = Query(None, min_length=1, max_length=64),
    admin: str = Depends(verify_admin_access),
) -> PurgedResponse:
    """Purge sign-in events by age (retention) or for one account; exactly one of the two."""
    if (older_than_days is None) == (account_id is None):
        raise typed_error(
            400, "invalid_purge", "Give exactly one of older_than_days or account_id."
        )
    accounts = local_accounts(request)
    deleted = await accounts.purge_login_events(
        older_than_days=older_than_days, account_id=account_id
    )
    await accounts.record_account_audit(
        action="agent.local_login_events.purge",
        actor_id=admin,
        admin_ip=get_client_ip(request),
        account_id=account_id,
        details={"older_than_days": older_than_days, "deleted": deleted},
    )
    return PurgedResponse(deleted=deleted)


def _domains_response(rows: list[Any]) -> SignupDomainsResponse:
    return SignupDomainsResponse(
        domains=[
            SignupDomainEntry(
                domain=row.domain,
                is_wildcard=row.is_wildcard,
                created_at=row.created_at,
                created_by=row.created_by,
            )
            for row in rows
        ],
        approval_policy=approval_policy(),
    )


@router.get("/signup-domains", response_model=SignupDomainsResponse)
async def list_signup_domains(
    request: Request, _admin: str = Depends(verify_admin_access)
) -> SignupDomainsResponse:
    """The signup domain allowlist, and the approval policy that uses it."""
    return _domains_response(await local_accounts(request).list_signup_domains())


@router.post(
    "/signup-domains",
    response_model=SignupDomainsResponse,
    status_code=201,
    dependencies=[Depends(require_json)],
)
async def add_signup_domain(
    body: SignupDomainRequest, request: Request, admin: str = Depends(verify_admin_access)
) -> SignupDomainsResponse:
    """Allow a domain, or a ``*.`` wildcard of its subdomains."""
    try:
        domain, wildcard = parse_signup_domain(body.domain)
    except ValueError as exc:
        raise typed_error(400, "invalid_domain", str(exc)) from None
    accounts = local_accounts(request)
    if not await accounts.add_signup_domain(
        domain=domain, is_wildcard=wildcard, actor_id=admin, admin_ip=get_client_ip(request)
    ):
        raise typed_error(409, "domain_listed", "That domain is already on the list.")
    return _domains_response(await accounts.list_signup_domains())


@router.delete("/signup-domains/{domain}", response_model=SignupDomainsResponse)
async def remove_signup_domain(
    domain: str,
    request: Request,
    wildcard: bool = Query(False),
    admin: str = Depends(verify_admin_access),
) -> SignupDomainsResponse:
    """Remove a domain (``wildcard=true`` for its ``*.`` entry)."""
    accounts = local_accounts(request)
    if not await accounts.remove_signup_domain(
        domain=domain.strip().lower(),
        is_wildcard=wildcard,
        actor_id=admin,
        admin_ip=get_client_ip(request),
    ):
        raise typed_error(404, "not_found", "That domain is not on the list.")
    return _domains_response(await accounts.list_signup_domains())


# ── One account ─────────────────────────────────────────────────────────


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
    reason = (body.reason or "").strip() or None
    result = await local_accounts(request).apply_admin_action(
        account_id=account_id,
        action=body.action,
        reason=reason,
        expected_version=body.expected_version,
        actor_id=admin,
        actor_account_id=_actor_account(admin),
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
    _notify(request, background, body.action, result.account, reason)
    return account_entry(result.account)


def _notify(
    request: Request, background: BackgroundTasks, action: str, account: Any, reason: str | None
) -> None:
    """Mail the outcome of an application to the account, as HybridInference does."""
    if action not in ("approve", "reject") or not account.email:
        return
    flows = local_flows(request, background)
    if action == "approve":
        flows.send(AccountMail(kind="approved", to=account.email, link=flows.link("/login")))
    else:
        flows.send(AccountMail(kind="rejected", to=account.email, details={"reason": reason or ""}))


@router.patch(
    "/{account_id}/notes", response_model=AccountEntry, dependencies=[Depends(require_json)]
)
async def annotate_account(
    account_id: str,
    body: AccountNotesRequest,
    request: Request,
    admin: str = Depends(verify_admin_access),
) -> AccountEntry:
    """Set the private administrator note and the message a suspended account is shown.

    A field left out is unchanged; an empty one is cleared.
    """
    fields = frozenset(body.model_fields_set & {"admin_note", "suspension_message"})
    if not fields:
        raise typed_error(422, "nothing_to_change", "Give admin_note or suspension_message.")
    record = await local_accounts(request).annotate_account(
        account_id=account_id,
        admin_note=(body.admin_note or "").strip() or None,
        suspension_message=(body.suspension_message or "").strip() or None,
        fields=fields,
        expected_version=body.expected_version,
        actor_id=admin,
        actor_account_id=_actor_account(admin),
        admin_ip=get_client_ip(request),
    )
    return account_entry(record)


@router.get("/{account_id}/login-events", response_model=LoginEventsResponse)
async def account_login_events(
    account_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    _admin: str = Depends(verify_admin_access),
) -> LoginEventsResponse:
    """The account's most recent sign-in attempts, newest first."""
    rows = await local_accounts(request).login_events(account_id=account_id, limit=limit)
    return LoginEventsResponse(events=[LoginEventEntry(**row) for row in rows])


@router.post(
    "/{account_id}/erase", response_model=AccountEntry, dependencies=[Depends(require_json)]
)
async def erase_account(
    account_id: str,
    body: EraseAccountRequest,
    request: Request,
    admin: str = Depends(verify_admin_access),
) -> AccountEntry:
    """Erase a deleted account's identity permanently; its id typed out again confirms it."""
    if body.confirm.strip() != account_id:
        raise typed_error(400, "confirmation_mismatch", "Type the account id to confirm.")
    record = await local_accounts(request).erase_account(
        account_id=account_id,
        actor_id=admin,
        actor_account_id=_actor_account(admin),
        admin_ip=get_client_ip(request),
    )
    return account_entry(record)


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
