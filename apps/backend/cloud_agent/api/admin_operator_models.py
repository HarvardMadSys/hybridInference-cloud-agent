"""Admin routes for the models this deployment provides.

A provided connection is the deployment's own inference endpoint: a name, a
Base URL, the deployment's key and up to eight model ids, offered to every
admitted owner without any setup of theirs. It is saved the way an owner's
connection is (``api.model_connections``): creating one, and an edit that
changes its key or Base URL or adds models, verifies the models it would list
inside the same request and writes nothing unless every one has an agent that
can use it. The key is write-only, encrypted under the settings root key in a
domain of its own, and leaves the control plane in no claim, residency,
response, log or audit row: turns reach the provider through the model relay.

A new connection is disabled. It can be enabled once every listed model has a
default allowance: how many requests each owner may send it per UTC day
(``null`` unlimited, ``0`` only owners given an allowance of their own). An
administrator may give one account its own number for one model.

A new Base URL, disabling and deleting close every member process still
running on the old state. A new key does not: no process holds the key.

Every write is recorded in the admin audit trail; a key is never in it.
"""

from __future__ import annotations

import hmac
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import Field, field_validator

from cloud_agent.api.deps import (
    get_client_ip,
    get_storage,
    log_admin_action,
    require_store,
    verify_admin_access,
)
from cloud_agent.api.model_connections import (
    _VERIFY_RESPONSES,
    ModelStatusView,
    _ConnectionRoute,
    _Fields,
    _refused,
)
from cloud_agent.application.accounts import normalize_email
from cloud_agent.application.model_connection_save import (
    VerificationRefused,
    VerificationTimedOut,
    kept_checks,
    plan_edit,
    verify_endpoint,
)
from cloud_agent.application.model_connection_status import connection_statuses
from cloud_agent.application.model_connections import (
    model_connections_enabled,
    operator_connection_cipher,
)
from cloud_agent.application.operator_models import (
    invalidate_operator_connection,
    new_operator_connection_id,
)
from cloud_agent.crypto.credential_cipher import CredentialCipherError
from cloud_agent.domain.model_connections import ModelConnectionConflict, ModelConnectionInvalid
from cloud_agent.domain.operator_models import (
    DEFAULT_USER_CONCURRENCY,
    OperatorLimitsMissing,
    clean_default_model,
    clean_end_user_header,
)
from cloud_agent.schemas import (
    OperatorModelConnectionList,
    OperatorModelConnectionView,
    OperatorModelLimit,
    OperatorModelLimitWrite,
    OperatorModelUsageResponse,
    OperatorModelUsageRow,
    OperatorModelUserLimitWrite,
    OperatorModelUserList,
    OperatorModelUserRow,
)
from cloud_agent_shared.model_connections import (
    MAX_API_KEY_CHARS,
    MAX_MODELS,
    is_operator_connection_id,
)

if TYPE_CHECKING:
    from cloud_agent.application.model_connection_save import EditPlan
    from cloud_agent.ports import (
        AuditRepository,
        HostPoolRepository,
        LocalAccountRepository,
        OperatorModelRepository,
    )

#: The most usage rows one answer carries, and the longest range it covers.
_MAX_USAGE_ROWS = 2000
_MAX_USAGE_DAYS = 92


class _Storage(Protocol):
    audit: AuditRepository
    host_pool: HostPoolRepository
    local_accounts: LocalAccountRepository
    operator_models: OperatorModelRepository


router = APIRouter(
    prefix="/admin/agent/operator-model-connections",
    tags=["operator-model-connections"],
    route_class=_ConnectionRoute,
    dependencies=[Depends(verify_admin_access)],
)


class _OperatorFields(_Fields):
    @field_validator("end_user_header", check_fields=False)
    @classmethod
    def _end_user_header(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            # An empty string is kept: on an edit it clears the header.
            return clean_end_user_header(value) or ""
        except ModelConnectionInvalid as exc:
            raise ValueError(str(exc)) from None


class OperatorModelConnectionCreate(_OperatorFields):
    """A new provided connection, saved disabled once every model has been verified."""

    name: str = Field(..., max_length=200)
    base_url: str = Field(..., max_length=1024)
    #: Write-only: encrypted before it is stored and never answered.
    api_key: str = Field(..., max_length=MAX_API_KEY_CHARS + 64, repr=False)
    models: list[str] = Field(..., max_length=MAX_MODELS * 4)
    #: The model an owner with no history starts on; one of ``models``.
    default_model: str | None = Field(None, max_length=256)
    #: A header each relayed request names its owner in, by an opaque tag.
    end_user_header: str | None = Field(None, max_length=64)
    #: Requests one owner may have in flight at once.
    user_concurrency: int = Field(DEFAULT_USER_CONCURRENCY, ge=1, le=64)


class OperatorModelConnectionUpdate(_OperatorFields):
    """Change some fields; an omitted field keeps its value. ``api_key`` rotates the key.

    A new key or Base URL, or an added model, is verified before it is saved.
    An empty ``default_model`` or ``end_user_header`` clears it.
    """

    name: str | None = Field(None, max_length=200)
    base_url: str | None = Field(None, max_length=1024)
    api_key: str | None = Field(None, max_length=MAX_API_KEY_CHARS + 64, repr=False)
    models: list[str] | None = Field(None, max_length=MAX_MODELS * 4)
    default_model: str | None = Field(None, max_length=256)
    is_default: bool | None = None
    enabled: bool | None = None
    end_user_header: str | None = Field(None, max_length=64)
    user_concurrency: int | None = Field(None, ge=1, le=64)


def _view(row: dict[str, Any], limits: dict[str, dict[str, Any]]) -> OperatorModelConnectionView:
    return OperatorModelConnectionView(
        id=row["id"],
        name=row["name"],
        base_url=row["base_url"],
        models=list(row["models"]),
        default_model=row.get("default_model"),
        is_default=bool(row.get("is_default")),
        enabled=bool(row["enabled"]),
        end_user_header=row.get("end_user_header"),
        user_concurrency=int(row["user_concurrency"]),
        limits={
            model: OperatorModelLimit(**limit)
            for model, limit in limits.items()
            if model in row["models"]
        },
        model_status=[
            ModelStatusView(**status).model_dump() for status in connection_statuses(row)
        ],
        key_refused_at=row.get("key_refused_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _answer(store: _Storage, row: dict[str, Any]) -> OperatorModelConnectionView:
    limits = await store.operator_models.operator_limits(connection_ids=[row["id"]])
    return _view(row, limits[row["id"]])


def _cipher() -> Any:
    try:
        return operator_connection_cipher()
    except CredentialCipherError:
        raise _refused(
            503,
            "operator_model_connections_unconfigured",
            "Provided models need the deployment's settings encryption key.",
        ) from None


async def _live(store: _Storage, connection_id: str) -> dict[str, Any]:
    row = (
        await store.operator_models.get_operator_connection(connection_id=connection_id)
        if is_operator_connection_id(connection_id)
        else None
    )
    if row is None or row.get("deleted_at") is not None:
        raise _refused(
            404, "operator_model_connection_not_found", "Provided model connection not found."
        )
    return row


def _require_enabled() -> None:
    if not model_connections_enabled():
        raise _refused(
            403,
            "model_connections_disabled",
            "This deployment has model connections switched off.",
        )


def _taken(name: str) -> HTTPException:
    return _refused(
        409,
        "operator_model_connection_name_taken",
        f"A provided connection is already named {name}.",
    )


def _changed() -> HTTPException:
    return _refused(
        409,
        "operator_model_connection_changed",
        "The connection changed while this was being saved. Nothing was saved; try again.",
    )


def _invalid(message: str) -> HTTPException:
    return _refused(422, "invalid_model_connection", message)


async def _name_taken(store: _Storage, name: str, *, other_than: str = "") -> bool:
    rows = await store.operator_models.list_operator_connections()
    return any(row["name"].lower() == name.lower() and row["id"] != other_than for row in rows)


async def _verified(*, base_url: str, api_key: str, models: list[str]) -> dict[str, dict[str, Any]]:
    """Every model's facts at this endpoint, or the HTTP refusal that saves nothing."""
    try:
        return await verify_endpoint(base_url=base_url, api_key=api_key, models=models)
    except VerificationTimedOut:
        raise _refused(
            504,
            "model_connection_verification_timeout",
            "Verification took too long. Nothing was saved; check the Base URL and try again.",
        ) from None
    except VerificationRefused as refused:
        raise HTTPException(
            422,
            {
                "error": {
                    "type": "model_connection_verification_failed",
                    "message": "A model could not be verified for any agent. Nothing was saved.",
                    "models": [
                        ModelStatusView(**status).model_dump() for status in refused.statuses
                    ],
                }
            },
        ) from None


async def _audit(
    request: Request,
    store: _Storage,
    actor: str,
    action: str,
    row: dict[str, Any],
    details: dict[str, Any] | None = None,
    *,
    target_user_id: str | None = None,
) -> None:
    await log_admin_action(
        store,
        get_client_ip(request),
        action,
        target_user_id,
        {"connection_id": row["id"], "name": row["name"], **(details or {})},
        actor_id=actor,
    )


@router.get("", response_model=OperatorModelConnectionList)
async def list_connections(
    store: _Storage | None = Depends(get_storage),
) -> OperatorModelConnectionList:
    """Every provided connection, with its limits and each model's verification."""
    store = require_store(store)
    rows = await store.operator_models.list_operator_connections()
    limits = await store.operator_models.operator_limits(connection_ids=[row["id"] for row in rows])
    return OperatorModelConnectionList(
        connections=[_view(row, limits[row["id"]]) for row in rows],
        relay_hosts=await store.operator_models.model_relay_host_count(),
        feature_enabled=model_connections_enabled(),
    )


@router.post(
    "",
    response_model=OperatorModelConnectionView,
    status_code=201,
    responses=_VERIFY_RESPONSES,
)
async def create_connection(
    request: Request,
    body: OperatorModelConnectionCreate,
    actor: str = Depends(verify_admin_access),
    store: _Storage | None = Depends(get_storage),
) -> OperatorModelConnectionView:
    """Verify every model, then add the connection, disabled; nothing is stored if one fails."""
    _require_enabled()
    store = require_store(store)
    cipher = _cipher()
    try:
        default_model = clean_default_model(body.default_model, body.models)
    except ModelConnectionInvalid as exc:
        raise _invalid(str(exc)) from None
    if await _name_taken(store, body.name):
        raise _taken(body.name)
    checks = await _verified(base_url=body.base_url, api_key=body.api_key, models=body.models)
    try:
        row = await store.operator_models.create_operator_connection(
            values={
                "id": new_operator_connection_id(),
                "name": body.name,
                "base_url": body.base_url,
                "models": body.models,
                "default_model": default_model,
                "api_key_ciphertext": cipher.encrypt(body.api_key),
                "end_user_header": body.end_user_header or None,
                "user_concurrency": body.user_concurrency,
                "model_checks": checks,
            }
        )
    except ModelConnectionConflict:
        raise _taken(body.name) from None
    await _audit(
        request,
        store,
        actor,
        "agent.operator_model_connection.create",
        row,
        {"base_url": row["base_url"], "models": row["models"]},
    )
    return await _answer(store, row)


def _same_key(row: dict[str, Any], key: str) -> bool:
    """Whether ``key`` is the stored one, so re-entering it rotates nothing."""
    try:
        stored = _cipher().decrypt(str(row["api_key_ciphertext"]))
    except (CredentialCipherError, HTTPException):
        return False
    return hmac.compare_digest(stored.encode(), key.encode())


def _settings_changes(
    row: dict[str, Any], body: OperatorModelConnectionUpdate, models: list[str]
) -> dict[str, Any]:
    """The fields of an edit that never need a verification, where they differ from ``row``."""
    sent = body.model_fields_set
    wanted: dict[str, Any] = {}
    try:
        if "default_model" in sent:
            wanted["default_model"] = clean_default_model(body.default_model, models)
        elif row.get("default_model") not in models:
            # The default model is being removed from the list.
            wanted["default_model"] = None
    except ModelConnectionInvalid as exc:
        raise _invalid(str(exc)) from None
    if "end_user_header" in sent:
        wanted["end_user_header"] = body.end_user_header or None
    for name in ("user_concurrency", "is_default", "enabled"):
        value = getattr(body, name)
        if value is not None:
            wanted[name] = value
    return {name: value for name, value in wanted.items() if value != row.get(name)}


@router.patch(
    "/{connection_id}", response_model=OperatorModelConnectionView, responses=_VERIFY_RESPONSES
)
async def update_connection(
    request: Request,
    connection_id: str,
    body: OperatorModelConnectionUpdate,
    actor: str = Depends(verify_admin_access),
    store: _Storage | None = Depends(get_storage),
) -> OperatorModelConnectionView:
    """Edit, rotate the key of, enable or disable a provided connection.

    A new key or Base URL verifies every model the connection would list, and
    added models are verified; the edit is committed only if all pass and the
    connection did not change meanwhile (409). A field sent with the value it
    already has is not a change. Enabling is refused (409) while a listed
    model has no default allowance.
    """
    store = require_store(store)
    row = await _live(store, connection_id)
    new_key = (
        body.api_key if body.api_key is not None and not _same_key(row, body.api_key) else None
    )
    plan = plan_edit(
        row,
        requested=body.model_dump(exclude_unset=True, include={"name", "base_url", "models"}),
        new_key=new_key,
    )
    models = list(plan.changes.get("models", row["models"]))
    settings = _settings_changes(row, body, models)
    if plan.verify or settings.get("enabled") is True:
        _require_enabled()
    if settings.get("enabled") is True:
        # Before any provider call or write: an edit that also verifies must
        # not commit that part and then be refused the enabling.
        await _require_limits(store, connection_id, models)
    name = plan.changes.get("name")
    if name is not None and await _name_taken(store, name, other_than=row["id"]):
        raise _taken(name)
    try:
        updated = await _commit(store, row, plan, settings)
    except ModelConnectionConflict:
        raise _taken(str(name)) from None
    except OperatorLimitsMissing as missing:
        raise _limits_missing(missing.models) from None
    await invalidate_operator_connection(store, connection_id=connection_id)
    await _audit_update(request, store, actor, updated, plan, settings)
    return await _answer(store, updated)


def _limits_missing(models: list[str]) -> HTTPException:
    return _refused(
        409,
        "operator_model_limits_missing",
        "Set a default allowance for every model before enabling: " + ", ".join(models),
    )


async def _require_limits(store: _Storage, connection_id: str, models: list[str]) -> None:
    """Refuse enabling while a model the connection would list has no default allowance."""
    limits = await store.operator_models.operator_limits(connection_ids=[connection_id])
    missing = [model for model in models if model not in limits[connection_id]]
    if missing:
        raise _limits_missing(missing)


async def _commit(
    store: _Storage, row: dict[str, Any], plan: EditPlan, settings: dict[str, Any]
) -> dict[str, Any]:
    """Apply an edit: what it adds is verified first, the rest needs no provider call.

    Enabling and the default flag have rules of their own and are applied
    after a verified part committed, so a refused enable leaves a verified
    change in place and is answered as such.
    """
    connection_id = row["id"]
    late: dict[str, Any] = {
        name: settings[name] for name in ("is_default", "enabled") if name in settings
    }
    changes = {**plan.changes, **settings}
    if plan.verify:
        changes = {name: value for name, value in changes.items() if name not in late}
        models = list(plan.changes.get("models", row["models"]))
        fresh = await _verified(
            base_url=plan.changes.get("base_url", row["base_url"]),
            api_key=plan.new_key if plan.new_key is not None else _key(row),
            models=plan.verify,
        )
        checks = fresh if plan.endpoint_changed else {**kept_checks(row, models), **fresh}
        if plan.new_key is not None:
            changes["api_key_ciphertext"] = _cipher().encrypt(plan.new_key)
        updated = await store.operator_models.commit_verified_operator_update(
            connection_id=connection_id, expected=row, changes=changes, model_checks=checks
        )
        changes = late
    else:
        updated = row
    if updated is not None and changes:
        updated = await store.operator_models.update_operator_connection(
            connection_id=connection_id, changes=changes
        )
    if updated is None:
        await _live(store, connection_id)
        raise _changed()
    return updated


def _key(row: dict[str, Any]) -> str:
    try:
        return str(_cipher().decrypt(row["api_key_ciphertext"]))
    except CredentialCipherError:
        raise _refused(
            503, "operator_model_connections_unconfigured", "The stored key cannot be read."
        ) from None


async def _audit_update(
    request: Request,
    store: _Storage,
    actor: str,
    row: dict[str, Any],
    plan: EditPlan,
    settings: dict[str, Any],
) -> None:
    """One row per kind of change: the key, enabling or disabling, and everything else."""
    if plan.new_key is not None:
        await _audit(
            request,
            store,
            actor,
            "agent.operator_model_connection.rotate_key",
            row,
            {"api_key": "[redacted]"},
        )
    if "enabled" in settings:
        await _audit(
            request,
            store,
            actor,
            "agent.operator_model_connection.enable"
            if settings["enabled"]
            else "agent.operator_model_connection.disable",
            row,
        )
    changed = sorted((set(plan.changes) | set(settings)) - {"enabled"})
    if changed:
        await _audit(
            request,
            store,
            actor,
            "agent.operator_model_connection.update",
            row,
            {"changed": changed},
        )


@router.delete("/{connection_id}", status_code=204)
async def delete_connection(
    request: Request,
    connection_id: str,
    actor: str = Depends(verify_admin_access),
    store: _Storage | None = Depends(get_storage),
) -> Response:
    """Delete a provided connection: its key is erased and its running processes are closed.

    Turns that name it are refused from now on, never moved elsewhere.
    """
    store = require_store(store)
    row = await _live(store, connection_id)
    await store.operator_models.delete_operator_connection(connection_id=connection_id)
    await invalidate_operator_connection(store, connection_id=connection_id)
    await _audit(request, store, actor, "agent.operator_model_connection.delete", row)
    return Response(status_code=204)


def _not_listed() -> HTTPException:
    return _refused(
        404, "operator_model_not_listed", "That model is not listed on this connection."
    )


@router.put("/{connection_id}/limits", response_model=OperatorModelConnectionView)
async def set_limit(
    request: Request,
    connection_id: str,
    body: OperatorModelLimitWrite,
    actor: str = Depends(verify_admin_access),
    store: _Storage | None = Depends(get_storage),
) -> OperatorModelConnectionView:
    """Set one model's default allowance and its cap on requests in flight across owners."""
    store = require_store(store)
    row = await _live(store, connection_id)
    written = await store.operator_models.set_operator_limit(
        connection_id=connection_id,
        model=body.model,
        user_daily_requests=body.user_daily_requests,
        global_concurrency=body.global_concurrency,
    )
    if not written:
        raise _not_listed()
    await _audit(
        request,
        store,
        actor,
        "agent.operator_model_connection.set_limit",
        row,
        {
            "model": body.model,
            "user_daily_requests": body.user_daily_requests,
            "global_concurrency": body.global_concurrency,
        },
    )
    return await _answer(store, row)


async def _emails(store: _Storage, account_ids: list[str]) -> dict[str, str]:
    """Each account's email where it has one; an unreadable directory answers none."""
    if not account_ids:
        return {}
    try:
        return await store.local_accounts.account_emails(sorted(set(account_ids)))
    except Exception:
        return {}


@router.get("/{connection_id}/users", response_model=OperatorModelUserList)
async def list_users(
    connection_id: str,
    cursor: str | None = Query(None, max_length=64),
    limit: int = Query(100, ge=1, le=500),
    store: _Storage | None = Depends(get_storage),
) -> OperatorModelUserList:
    """Accounts with an allowance of their own on this connection, or with use today."""
    store = require_store(store)
    await _live(store, connection_id)
    rows = await store.operator_models.list_operator_users(
        connection_id=connection_id, cursor=cursor, limit=limit + 1
    )
    page = rows[:limit]
    emails = await _emails(store, [row["user_id"] for row in page])
    return OperatorModelUserList(
        users=[
            OperatorModelUserRow(
                account_id=row["user_id"],
                email=emails.get(row["user_id"], ""),
                limits=row["limits"],
                used_today=row["used_today"],
            )
            for row in page
        ],
        next_cursor=page[-1]["user_id"] if len(rows) > limit and page else None,
    )


async def _account_id(store: _Storage, account: str) -> str:
    """The account an id or an email address names, or 404."""
    value = account.strip()
    found: str | None = None
    if "@" in value:
        try:
            found = await store.local_accounts.identifier_owner("email", normalize_email(value)[1])
        except ValueError:
            found = None
    else:
        record = await store.local_accounts.get_account(value)
        found = record.id if record is not None else None
    if found is None:
        raise _refused(404, "account_not_found", "No account has that id or email address.")
    return found


@router.put("/{connection_id}/user-limits", response_model=OperatorModelUserRow)
async def set_user_limit(
    request: Request,
    connection_id: str,
    body: OperatorModelUserLimitWrite,
    actor: str = Depends(verify_admin_access),
    store: _Storage | None = Depends(get_storage),
) -> OperatorModelUserRow:
    """Give one account its own allowance on one model, in place of the model's default."""
    store = require_store(store)
    row = await _live(store, connection_id)
    account_id = await _account_id(store, body.account)
    written = await store.operator_models.set_operator_user_limit(
        connection_id=connection_id,
        user_id=account_id,
        model=body.model,
        daily_requests=body.daily_requests,
    )
    if not written:
        raise _not_listed()
    await _audit(
        request,
        store,
        actor,
        "agent.operator_model_user_limit.set",
        row,
        {"model": body.model, "daily_requests": body.daily_requests},
        target_user_id=account_id,
    )
    emails = await _emails(store, [account_id])
    return OperatorModelUserRow(
        account_id=account_id,
        email=emails.get(account_id, ""),
        limits={body.model: body.daily_requests},
    )


@router.delete("/{connection_id}/user-limits", status_code=204)
async def clear_user_limit(
    request: Request,
    connection_id: str,
    account_id: str = Query(..., min_length=1, max_length=64),
    model: str = Query(..., min_length=1, max_length=256),
    actor: str = Depends(verify_admin_access),
    store: _Storage | None = Depends(get_storage),
) -> Response:
    """Return one account to a model's default allowance."""
    store = require_store(store)
    row = await _live(store, connection_id)
    removed = await store.operator_models.clear_operator_user_limit(
        connection_id=connection_id, user_id=account_id, model=model
    )
    if not removed:
        raise _refused(
            404, "operator_model_user_limit_not_found", "That account has no allowance of its own."
        )
    await _audit(
        request,
        store,
        actor,
        "agent.operator_model_user_limit.clear",
        row,
        {"model": model},
        target_user_id=account_id,
    )
    return Response(status_code=204)


@router.get("/{connection_id}/usage", response_model=OperatorModelUsageResponse)
async def usage(
    connection_id: str,
    start: date | None = Query(None, alias="from"),
    end: date | None = Query(None, alias="to"),
    store: _Storage | None = Depends(get_storage),
) -> OperatorModelUsageResponse:
    """Requests the model relay accepted, per UTC day, account and model.

    Counted outside the sandbox, one per inference request the relay let
    through; not tokens and not a bill. The last seven days when no range is
    given, and at most ``_MAX_USAGE_DAYS`` of them.
    """
    store = require_store(store)
    await _live(store, connection_id)
    today = datetime.now(UTC).date()
    last = end or today
    first = start or last - timedelta(days=6)
    if first > last or (last - first).days >= _MAX_USAGE_DAYS:
        raise _refused(
            422, "invalid_usage_range", f"Choose a range of at most {_MAX_USAGE_DAYS} days."
        )
    rows = await store.operator_models.operator_usage(
        connection_id=connection_id, start=first, end=last, limit=_MAX_USAGE_ROWS + 1
    )
    page = rows[:_MAX_USAGE_ROWS]
    emails = await _emails(store, [str(row["user_id"]) for row in page])
    return OperatorModelUsageResponse(
        rows=[
            OperatorModelUsageRow(
                day=row["day"],
                account_id=str(row["user_id"]),
                email=emails.get(str(row["user_id"]), ""),
                model=str(row["model"]),
                requests=int(row["requests"]),
            )
            for row in page
        ],
        truncated=len(rows) > _MAX_USAGE_ROWS,
    )
