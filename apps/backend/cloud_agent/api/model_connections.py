"""Owner routes for their own model connections.

A connection is the owner's own inference endpoint: a name, a Base URL, an
API key and up to eight model ids. Which APIs each model is reached on is
found by verifying it, and verification happens before anything is saved:
creating a connection, and an edit that changes its key or Base URL or adds
models, verifies the models it would list inside the same request and writes
nothing unless every one has an agent that can use it
(``application.model_connection_save``). There is no unverified connection
and no manual declaration of APIs. The key is write-only: it is encrypted
before it is stored, no owner route answers it, and a validation failure
answers which field was wrong without echoing what was sent. Every response
is ``no-store``.

A verification calls the owner's provider with their key: at most three native
checks of two requests per model, each model reserving one of the owner's
hourly checks atomically across replicas before any call. Changing the key or
Base URL replaces every model's verification; that, disabling or deleting a
connection closes every member process still running on its old state. A field
sent with its stored value changes nothing, and a rename, removing models,
enabling or disabling makes no provider call.

Listing a provider's model ids (``discover-models``) is a convenience for
filling in the form, before or after a connection exists. It writes nothing,
spends one hourly check, and says nothing about whether a listed model works:
an id the owner picks is verified like any other when the form is saved.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING, Any, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api._sensitive_routes import SensitiveRoute
from cloud_agent.api.deps import get_grant_client, get_storage, require_store
from cloud_agent.application.model_connection_check import (
    CHECK_MESSAGES,
    CHECKS_PER_HOUR,
    CheckStop,
    discover_models,
)
from cloud_agent.application.model_connection_save import (
    CheckLimitReached,
    EditPlan,
    VerificationRefused,
    VerificationTimedOut,
    kept_checks,
    plan_edit,
    verify_for_save,
)
from cloud_agent.application.model_connection_status import connection_statuses
from cloud_agent.application.model_connections import (
    invalidate_connection,
    model_connection_cipher,
    model_connections_enabled,
    new_connection_id,
)
from cloud_agent.credential_cipher import CredentialCipherError
from cloud_agent.domain.model_connections import (
    ModelConnectionConflict,
    ModelConnectionInvalid,
    clean_base_url,
    clean_models,
    clean_name,
    is_connection_id,
)
from cloud_agent_shared.model_connections import (
    MAX_API_KEY_CHARS,
    MAX_MODELS,
    valid_api_key,
)

if TYPE_CHECKING:
    from cloud_agent.ports import HostPoolRepository, ModelConnectionRepository


class _ConnectionRoute(SensitiveRoute):
    """Answer which field was invalid and why, never the value that was sent."""

    def validation_response(self, exc: Exception) -> JSONResponse:
        errors = exc.errors() if isinstance(exc, RequestValidationError) else []
        fields = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())[1:]),
                "message": str(error.get("msg", "Invalid value."))[:200],
            }
            for error in errors[:10]
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "type": "invalid_model_connection",
                    "message": fields[0]["message"] if fields else "Check the connection fields.",
                    "fields": fields,
                }
            },
        )


router = APIRouter(
    prefix="/v1/agent/model-connections", tags=["model-connections"], route_class=_ConnectionRoute
)


class _Storage(Protocol):
    model_connections: ModelConnectionRepository
    host_pool: HostPoolRepository


def _refused(status: int, kind: str, message: str) -> HTTPException:
    return HTTPException(status, {"error": {"type": kind, "message": message}})


def _clean(cleaner: Any, value: Any) -> Any:
    if value is None:
        return None
    try:
        return cleaner(value)
    except ModelConnectionInvalid as exc:
        raise ValueError(str(exc)) from None


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("name", check_fields=False)
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return _clean(clean_name, value)

    @field_validator("base_url", check_fields=False)
    @classmethod
    def _base_url(cls, value: str | None) -> str | None:
        return _clean(clean_base_url, value)

    @field_validator("models", check_fields=False)
    @classmethod
    def _models(cls, value: list[str] | None) -> list[str] | None:
        return _clean(clean_models, value)

    @field_validator("api_key", check_fields=False)
    @classmethod
    def _api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        key = value.strip()
        if not valid_api_key(key):
            raise ValueError(
                f"An API key is 1 to {MAX_API_KEY_CHARS} printable characters, without spaces."
            )
        return key


class ModelConnectionCreate(_Fields):
    """A new connection, saved only once every model has been verified usable."""

    name: str = Field(..., max_length=200)
    base_url: str = Field(..., max_length=1024)
    api_key: str = Field(..., max_length=MAX_API_KEY_CHARS + 64, repr=False)
    models: list[str] = Field(..., max_length=MAX_MODELS * 4)


class ModelConnectionUpdate(_Fields):
    """Change some fields; an omitted field keeps its value. ``api_key`` rotates the key.

    A new key or Base URL, or an added model, is verified before it is saved.
    """

    name: str | None = Field(None, max_length=200)
    base_url: str | None = Field(None, max_length=1024)
    api_key: str | None = Field(None, max_length=MAX_API_KEY_CHARS + 64, repr=False)
    models: list[str] | None = Field(None, max_length=MAX_MODELS * 4)
    enabled: bool | None = None


class ProtocolResultView(BaseModel):
    """What one native API's check found for a model: a diagnostic."""

    status: str
    category: str
    message: str
    http_status: int | None = None


class AgentPathView(BaseModel):
    """How one agent reaches a model: directly, or through the member's helper."""

    runtime: str
    mode: Literal["direct", "translated"]
    client_protocol: str
    upstream_protocol: str


class ModelStatusView(BaseModel):
    """One listed model as its owner sees it.

    ``status`` is ``ready``, ``failed``, ``unsupported`` or (a backstop that a
    saved model never shows) ``unverified``; ``runtimes`` are the agents a task
    may run it with now, exactly what the execution gates accept. ``paths``
    and ``protocols`` are read-only diagnostics.
    """

    model: str
    status: Literal["ready", "unverified", "failed", "unsupported"]
    category: str | None = None
    message: str = ""
    runtimes: list[str]
    paths: list[AgentPathView] = Field(default_factory=list)
    protocols: dict[str, ProtocolResultView] = Field(default_factory=dict)
    checked_at: str | None = None


class VerificationRefusal(BaseModel):
    """Why a save was refused: every verified model's status; nothing was written."""

    type: Literal["model_connection_verification_failed"]
    message: str
    models: list[ModelStatusView]


class VerificationRefusalBody(BaseModel):
    """The 422 a save answers when a model it would list has no usable agent."""

    error: VerificationRefusal


class ModelConnectionView(BaseModel):
    """A connection as its owner sees it: everything but the key."""

    id: str
    name: str
    base_url: str
    models: list[str]
    enabled: bool
    #: Always true for a live connection; the key itself is never answered.
    has_api_key: bool
    #: Every agent at least one listed model can run with now.
    runtimes: list[str]
    model_status: list[ModelStatusView]
    created_at: str
    updated_at: str


class ModelConnectionList(BaseModel):
    """The owner's connections, and whether this deployment can store keys at all."""

    configured: bool
    #: Whether the deployment has turned connections on. Off, existing ones
    #: are listed and can be disabled or deleted, but not added or re-enabled.
    enabled: bool = True
    #: How many models one owner may verify (or discover) per hour.
    checks_per_hour: int = CHECKS_PER_HOUR
    #: How many models one connection may list.
    max_models: int = MAX_MODELS
    connections: list[ModelConnectionView]


class DiscoveredModels(BaseModel):
    """The ids a provider's ``GET /models`` listed; a convenience, not a check."""

    models: list[str]


class DiscoverModelsRequest(_Fields):
    """Where to list model ids, with which key: the form's, before anything is saved.

    Exactly one of ``api_key`` (the key the owner just entered) and
    ``connection_id`` (the owner's saved connection, whose stored key is used
    only while ``base_url`` is still that connection's Base URL). A key that
    was entered is never replaced by a stored one.
    """

    base_url: str = Field(..., max_length=1024)
    api_key: str | None = Field(None, max_length=MAX_API_KEY_CHARS + 64, repr=False)
    connection_id: str | None = Field(None, max_length=200)

    @model_validator(mode="after")
    def _one_key(self) -> DiscoverModelsRequest:
        if (self.api_key is None) == (self.connection_id is None):
            raise ValueError("Send either an API key or the saved connection, not both.")
        return self


def _view(row: dict[str, Any]) -> ModelConnectionView:
    statuses = connection_statuses(row)
    return ModelConnectionView(
        id=row["id"],
        name=row["name"],
        base_url=row["base_url"],
        models=list(row["models"]),
        enabled=bool(row["enabled"]),
        has_api_key=row.get("api_key_ciphertext") is not None,
        runtimes=sorted({runtime for status in statuses for runtime in status["runtimes"]}),
        model_status=[ModelStatusView(**status) for status in statuses],
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


def _cipher() -> Any:
    try:
        return model_connection_cipher()
    except CredentialCipherError:
        raise _refused(
            503,
            "model_connections_unconfigured",
            "Model connections need the deployment's credential encryption key.",
        ) from None


async def _live(store: _Storage, user_id: str, connection_id: str) -> dict[str, Any]:
    row = (
        await store.model_connections.get_model_connection(
            user_id=user_id, connection_id=connection_id
        )
        if is_connection_id(connection_id)
        else None
    )
    if row is None or row.get("deleted_at") is not None:
        raise _refused(404, "model_connection_not_found", "Model connection not found.")
    return row


def _require_enabled() -> None:
    if not model_connections_enabled():
        raise _refused(
            403,
            "model_connections_disabled",
            "This deployment has not turned on model connections.",
        )


def _taken(name: str) -> HTTPException:
    return _refused(
        409, "model_connection_name_taken", f"You already have a connection named {name}."
    )


@router.get("", response_model=ModelConnectionList)
async def list_connections(
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _Storage | None = Depends(get_storage),
) -> ModelConnectionList:
    """The owner's live connections."""
    rows = await require_store(store).model_connections.list_model_connections(
        user_id=user["user_id"]
    )
    try:
        model_connection_cipher()
        configured = True
    except CredentialCipherError:
        configured = False
    return ModelConnectionList(
        configured=configured,
        enabled=model_connections_enabled(),
        connections=[_view(row) for row in rows],
    )


_VERIFY_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {
        "model": VerificationRefusalBody,
        "description": "A model has no agent that can use it; nothing was saved.",
    },
    429: {"description": "The owner's hourly checks cannot cover the models to verify."},
    504: {"description": "Verification outlasted the save's bound; nothing was saved."},
}


async def _verified(
    store: _Storage, user_id: str, *, base_url: str, api_key: str, models: list[str]
) -> dict[str, dict[str, Any]]:
    """Every model's facts at this endpoint, or the HTTP refusal that saves nothing."""
    try:
        return await verify_for_save(
            store.model_connections,
            user_id=user_id,
            base_url=base_url,
            api_key=api_key,
            models=models,
        )
    except CheckLimitReached:
        raise _refused(
            429,
            "model_connection_check_limit",
            "Too many verifications in the last hour. Nothing was saved; try again later.",
        ) from None
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


async def _name_taken(store: _Storage, user_id: str, name: str, *, other_than: str = "") -> bool:
    """Whether another live connection of the owner has ``name``, before spending a check."""
    rows = await store.model_connections.list_model_connections(user_id=user_id)
    return any(row["name"].lower() == name.lower() and row["id"] != other_than for row in rows)


@router.post("", response_model=ModelConnectionView, status_code=201, responses=_VERIFY_RESPONSES)
async def create_connection(
    body: ModelConnectionCreate,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _Storage | None = Depends(get_storage),
) -> ModelConnectionView:
    """Verify every model, then add the connection; nothing is stored if one fails.

    The key is encrypted only after verification succeeds, so a refused
    create leaves no connection and no key behind.
    """
    _require_enabled()
    store = require_store(store)
    cipher = _cipher()
    if await _name_taken(store, user["user_id"], body.name):
        raise _taken(body.name)
    checks = await _verified(
        store, user["user_id"], base_url=body.base_url, api_key=body.api_key, models=body.models
    )
    try:
        row = await store.model_connections.create_model_connection(
            user_id=user["user_id"],
            values={
                "id": new_connection_id(),
                "name": body.name,
                "base_url": body.base_url,
                "models": body.models,
                "api_key_ciphertext": cipher.encrypt(body.api_key),
                "model_checks": checks,
            },
        )
    except ModelConnectionConflict:
        raise _taken(body.name) from None
    return _view(row)


@router.patch("/{connection_id}", response_model=ModelConnectionView, responses=_VERIFY_RESPONSES)
async def update_connection(
    connection_id: str,
    body: ModelConnectionUpdate,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _Storage | None = Depends(get_storage),
    grants: Any = Depends(get_grant_client),
) -> ModelConnectionView:
    """Edit, rotate the key of, enable or disable a connection.

    A new key or Base URL verifies every model the connection would list, and
    added models are verified; the edit is committed only if all pass and the
    connection did not change meanwhile (409), so a failure leaves the
    previous configuration, key, results and running processes as they were.
    A field sent with the value it already has is not a change: a rename keeps
    the credential, every running process and every model's verification.
    """
    store = require_store(store)
    user_id = user["user_id"]
    row = await _live(store, user_id, connection_id)
    new_key = (
        body.api_key if body.api_key is not None and not _same_key(row, body.api_key) else None
    )
    plan = plan_edit(
        row,
        requested=body.model_dump(exclude_unset=True, exclude={"api_key"}),
        new_key=new_key,
    )
    if plan.verify or plan.changes.get("enabled") is True:
        # Turning the feature off stops anything that would make a connection
        # usable again; disabling, deleting, renaming and removing models still work.
        _require_enabled()
    updated = (
        await _commit_verified(store, user_id, row, plan)
        if plan.verify
        else await _commit_unverified(store, user_id, connection_id, plan.changes)
    )
    await invalidate_connection(store, grants, user_id=user_id, connection_id=connection_id)
    return _view(updated)


async def _commit_verified(
    store: _Storage, user_id: str, row: dict[str, Any], plan: EditPlan
) -> dict[str, Any]:
    """Verify what ``plan`` adds at the endpoint it would use, then commit it guarded by ``row``."""
    name = plan.changes.get("name")
    if name is not None and await _name_taken(store, user_id, name, other_than=row["id"]):
        raise _taken(name)
    models = list(plan.changes.get("models", row["models"]))
    fresh = await _verified(
        store,
        user_id,
        base_url=plan.changes.get("base_url", row["base_url"]),
        api_key=plan.new_key if plan.new_key is not None else _key(row),
        models=plan.verify,
    )
    checks = fresh if plan.endpoint_changed else {**kept_checks(row, models), **fresh}
    changes = dict(plan.changes)
    if plan.new_key is not None:
        changes["api_key_ciphertext"] = _cipher().encrypt(plan.new_key)
    try:
        updated = await store.model_connections.commit_verified_update(
            user_id=user_id,
            connection_id=row["id"],
            expected=row,
            changes=changes,
            model_checks=checks,
        )
    except ModelConnectionConflict:
        raise _taken(str(name)) from None
    if updated is None:
        await _live(store, user_id, row["id"])
        raise _changed()
    return updated


async def _commit_unverified(
    store: _Storage, user_id: str, connection_id: str, changes: dict[str, Any]
) -> dict[str, Any]:
    """A rename, removed models, enabling or disabling: no provider call."""
    try:
        updated = await store.model_connections.update_model_connection(
            user_id=user_id, connection_id=connection_id, changes=changes
        )
    except ModelConnectionConflict:
        raise _taken(str(changes.get("name"))) from None
    if updated is None:
        await _live(store, user_id, connection_id)
        raise _changed()
    return updated


def _changed() -> HTTPException:
    return _refused(
        409,
        "model_connection_changed",
        "The connection changed while this was being saved. Nothing was saved; try again.",
    )


def _same_key(row: dict[str, Any], key: str) -> bool:
    """Whether ``key`` is the stored one, so re-entering it rotates nothing."""
    try:
        stored = _cipher().decrypt(str(row["api_key_ciphertext"]))
    except (CredentialCipherError, HTTPException):
        return False
    return hmac.compare_digest(stored.encode(), key.encode())


@router.delete("/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _Storage | None = Depends(get_storage),
    grants: Any = Depends(get_grant_client),
) -> Response:
    """Delete a connection: its key is erased and its running processes are closed.

    Turns that name it are refused as deleted from now on, never moved elsewhere.
    """
    store = require_store(store)
    await _live(store, user["user_id"], connection_id)
    await store.model_connections.delete_model_connection(
        user_id=user["user_id"], connection_id=connection_id
    )
    await invalidate_connection(store, grants, user_id=user["user_id"], connection_id=connection_id)
    return Response(status_code=204)


def _key(row: dict[str, Any]) -> str:
    try:
        return _cipher().decrypt(row["api_key_ciphertext"])
    except CredentialCipherError:
        raise _refused(503, "model_connections_unconfigured", "The key cannot be read.") from None


async def _listing(store: _Storage, user_id: str, *, base_url: str, api_key: str) -> list[str]:
    """The provider's model ids at ``base_url`` with ``api_key``; nothing is written.

    One of the owner's hourly checks is reserved, atomically across replicas,
    before the provider is called. Only a failure's category is answered,
    never what the provider sent.
    """
    reserved = await store.model_connections.reserve_model_connection_checks(
        user_id=user_id, count=1, limit=CHECKS_PER_HOUR, seconds=3600
    )
    if not reserved:
        raise _refused(
            429,
            "model_connection_check_limit",
            "Too many checks in the last hour. Try again later.",
        )
    try:
        return await discover_models(base_url=base_url, api_key=api_key)
    except CheckStop as stop:
        raise _refused(
            422, f"model_connection_{stop.category}", CHECK_MESSAGES[stop.category]
        ) from None
    except TimeoutError:
        raise _refused(422, "model_connection_unreachable", CHECK_MESSAGES["unreachable"]) from None


async def _saved_key_for(store: _Storage, user_id: str, connection_id: str, base_url: str) -> str:
    """The owner's stored key, only for the exact Base URL it is saved with."""
    row = await _live(store, user_id, connection_id)
    if base_url != row["base_url"]:
        # An edited Base URL is a different endpoint: the saved key never goes there.
        raise _refused(
            422,
            "model_connection_key_required",
            "Enter the API key to list models at a changed Base URL.",
        )
    return _key(row)


@router.post("/discover-models", response_model=DiscoveredModels)
async def discover_for_form(
    body: DiscoverModelsRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _Storage | None = Depends(get_storage),
) -> DiscoveredModels:
    """List the provider's model ids for the add or edit form, saving nothing.

    With ``api_key``, the listing uses exactly that key at ``base_url``. With
    ``connection_id``, it uses the owner's saved key, and only when
    ``base_url`` is the one saved with it (422 ``model_connection_key_required``
    otherwise). Same bounds as verification: public IPv4 HTTPS, pinned, no
    redirects, a bounded read, at most two requests and one hourly check. The
    listing implies nothing about whether a model works; a picked id is
    verified when the form is saved.
    """
    _require_enabled()
    store = require_store(store)
    user_id = user["user_id"]
    api_key = (
        body.api_key
        if body.api_key is not None
        else await _saved_key_for(store, user_id, str(body.connection_id), body.base_url)
    )
    return DiscoveredModels(
        models=await _listing(store, user_id, base_url=body.base_url, api_key=api_key)
    )


# COMPAT(remove-after=2026-12-15): the settings page before PR #1029 lists models
# only for a saved connection through this route; remove once no deployed
# frontend predates the form's own listing above.
@router.post("/{connection_id}/discover-models", response_model=DiscoveredModels)
async def discover(
    connection_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _Storage | None = Depends(get_storage),
) -> DiscoveredModels:
    """List the ids the provider's model list offers for a saved connection.

    ``GET /models`` under the Base URL and, only if that listing is missing or
    refused, ``GET /v1/models`` with Messages authentication: at most two
    requests and one hourly check. Picked ids are added by an edit, which
    verifies them before saving.
    """
    _require_enabled()
    store = require_store(store)
    row = await _live(store, user["user_id"], connection_id)
    return DiscoveredModels(
        models=await _listing(store, user["user_id"], base_url=row["base_url"], api_key=_key(row))
    )
