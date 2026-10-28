"""Owner routes for their own model connections.

A connection is the owner's own inference endpoint: a name, a Base URL, an
API key, the protocols it offers (OpenAI Chat Completions or Responses,
Anthropic Messages) and the model ids it serves. The key
is write-only: it is encrypted before it is stored, no owner route answers it,
and a validation failure answers which field was wrong without echoing what was
sent. Every response is ``no-store``.

A check calls the owner's provider with their key, so it runs only when they
ask, and each one reserves one of the owner's hourly checks atomically across
replicas. Only the count is bounded: an owner's checks are not serialized and
may overlap. Changing the key, Base URL or protocols, disabling or deleting a
connection closes every member process still running on its old state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api._sensitive_routes import SensitiveRoute
from cloud_agent.api.deps import get_grant_client, get_storage, require_store
from cloud_agent.application.model_connection_check import (
    CHECK_MESSAGES,
    CHECKS_PER_HOUR,
    CheckStop,
    check_connection,
    discover_models,
)
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
    clean_protocols,
    is_connection_id,
)
from cloud_agent_shared.model_connections import (
    COMPAT_UPSTREAMS,
    MAX_API_KEY_CHARS,
    PROTOCOLS,
    runtime_plans,
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

    @field_validator("protocols", check_fields=False)
    @classmethod
    def _protocols(cls, value: list[str] | None) -> list[str] | None:
        return _clean(clean_protocols, value)

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
    """A new connection; every field is required."""

    name: str = Field(..., max_length=200)
    base_url: str = Field(..., max_length=1024)
    api_key: str = Field(..., max_length=MAX_API_KEY_CHARS + 64, repr=False)
    protocols: list[str] = Field(..., max_length=len(PROTOCOLS))
    models: list[str] = Field(..., max_length=256)


class ModelConnectionUpdate(_Fields):
    """Change some fields; an omitted field keeps its value. ``api_key`` rotates the key."""

    name: str | None = Field(None, max_length=200)
    base_url: str | None = Field(None, max_length=1024)
    api_key: str | None = Field(None, max_length=MAX_API_KEY_CHARS + 64, repr=False)
    protocols: list[str] | None = Field(None, max_length=len(PROTOCOLS))
    models: list[str] | None = Field(None, max_length=256)
    enabled: bool | None = None


class ModelConnectionCheckRequest(BaseModel):
    """Which model and protocol to check."""

    model_config = ConfigDict(extra="forbid")
    model: str = Field(..., min_length=1, max_length=200)
    protocol: str = Field(..., max_length=32)


class ModelConnectionView(BaseModel):
    """A connection as its owner sees it: everything but the key."""

    id: str
    name: str
    base_url: str
    protocols: list[str]
    models: list[str]
    enabled: bool
    #: Always true for a live connection; the key itself is never answered.
    has_api_key: bool
    #: The agents that can run on this connection, from the protocols it offers.
    runtimes: list[str]
    #: Those of ``runtimes`` that run through the compatibility helper; the
    #: connection's own ``protocols`` stay what its upstream offers.
    translated_runtimes: list[str] = Field(default_factory=list)
    last_check: dict[str, Any] | None = None
    created_at: str
    updated_at: str


class ModelConnectionList(BaseModel):
    """The owner's connections, and whether this deployment can store keys at all."""

    configured: bool
    #: Whether the deployment has turned connections on. Off, existing ones
    #: are listed and can be disabled or deleted, but not added or re-enabled.
    enabled: bool = True
    connections: list[ModelConnectionView]


class ModelConnectionCheckView(BaseModel):
    """One check's result, as stored on the connection."""

    status: str
    category: str
    message: str
    #: The client protocol tested: what the agent's CLI speaks.
    protocol: str
    #: What the request went out as; differs on a translated check.
    upstream_protocol: str = ""
    #: ``direct`` or ``translated``.
    mode: str = "direct"
    model: str
    streaming: bool
    tool_call: bool
    final_answer: bool
    http_status: int | None = None
    checked_at: str
    #: False when the connection changed while the check ran: the result then
    #: describes a configuration that no longer exists and was not kept.
    recorded: bool = True


class DiscoveredModels(BaseModel):
    """The ids a provider's ``GET /models`` listed; a convenience, not a check."""

    models: list[str]


def _view(row: dict[str, Any]) -> ModelConnectionView:
    protocols = list(row["protocols"])
    plans = runtime_plans(protocols)
    return ModelConnectionView(
        id=row["id"],
        name=row["name"],
        base_url=row["base_url"],
        protocols=protocols,
        models=list(row["models"]),
        enabled=bool(row["enabled"]),
        has_api_key=row.get("api_key_ciphertext") is not None,
        runtimes=sorted(plans),
        translated_runtimes=sorted(r for r, plan in plans.items() if plan.translated),
        last_check=row.get("last_check"),
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


@router.post("", response_model=ModelConnectionView, status_code=201)
async def create_connection(
    body: ModelConnectionCreate,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _Storage | None = Depends(get_storage),
) -> ModelConnectionView:
    """Add a connection; its key is encrypted before anything stores it."""
    _require_enabled()
    store = require_store(store)
    ciphertext = _cipher().encrypt(body.api_key)
    try:
        row = await store.model_connections.create_model_connection(
            user_id=user["user_id"],
            values={
                "id": new_connection_id(),
                "name": body.name,
                "base_url": body.base_url,
                "protocols": body.protocols,
                "models": body.models,
                "api_key_ciphertext": ciphertext,
            },
        )
    except ModelConnectionConflict:
        raise _taken(body.name) from None
    return _view(row)


@router.patch("/{connection_id}", response_model=ModelConnectionView)
async def update_connection(
    connection_id: str,
    body: ModelConnectionUpdate,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _Storage | None = Depends(get_storage),
    grants: Any = Depends(get_grant_client),
) -> ModelConnectionView:
    """Edit, rotate the key of, enable or disable a connection."""
    store = require_store(store)
    await _live(store, user["user_id"], connection_id)
    if body.enabled or body.api_key is not None or body.base_url or body.protocols:
        # Turning the feature off stops anything that would make a connection
        # usable again; disabling, deleting and renaming still work.
        _require_enabled()
    changes: dict[str, Any] = {
        field: value
        for field, value in body.model_dump(exclude_unset=True).items()
        if value is not None and field != "api_key"
    }
    if body.api_key is not None:
        changes["api_key_ciphertext"] = _cipher().encrypt(body.api_key)
    try:
        row = await store.model_connections.update_model_connection(
            user_id=user["user_id"], connection_id=connection_id, changes=changes
        )
    except ModelConnectionConflict:
        raise _taken(str(changes.get("name"))) from None
    if row is None:
        raise _refused(404, "model_connection_not_found", "Model connection not found.")
    await invalidate_connection(store, grants, user_id=user["user_id"], connection_id=connection_id)
    return _view(row)


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


async def _reserve(store: _Storage, user_id: str, connection_id: str) -> dict[str, Any]:
    """Take one of the owner's hourly checks, atomically, and the connection to check.

    Counted before anything dials out, in the store and under a per-owner lock,
    so neither another replica nor a cancelled request can go past the limit.
    The lock is released before the call: it bounds the count, not concurrency.
    """
    reserved: dict[str, Any] = (
        await store.model_connections.reserve_model_connection_check(
            user_id=user_id, connection_id=connection_id, limit=CHECKS_PER_HOUR, seconds=3600
        )
        if is_connection_id(connection_id)
        else {"error": "not_found"}
    )
    if reserved.get("error") == "limited":
        raise _refused(
            429,
            "model_connection_check_limit",
            "Too many checks in the last hour. Try again later.",
        )
    connection = reserved.get("connection")
    if not isinstance(connection, dict):
        raise _refused(404, "model_connection_not_found", "Model connection not found.")
    return connection


def _check_upstream(client: str, protocols: list[str]) -> str | None:
    """What a check of ``client`` goes out as on ``protocols``: native first, else translated.

    The same rule as ``protocol_plan``: a protocol the connection declares is
    tested directly, never through the helper; Responses or Anthropic Messages
    on a Chat Completions connection is tested through it.
    """
    if client in protocols:
        return client
    upstream = COMPAT_UPSTREAMS.get(client)
    return upstream if upstream is not None and upstream in protocols else None


def _key(row: dict[str, Any]) -> str:
    try:
        return _cipher().decrypt(row["api_key_ciphertext"])
    except CredentialCipherError:
        raise _refused(503, "model_connections_unconfigured", "The key cannot be read.") from None


@router.post("/{connection_id}/check", response_model=ModelConnectionCheckView)
async def check(
    connection_id: str,
    body: ModelConnectionCheckRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _Storage | None = Depends(get_storage),
) -> ModelConnectionCheckView:
    """Stream a tool call and its result through the connection, for one model and protocol.

    Spends the owner's own quota upstream. Answers what the check found; an
    unreachable or refusing provider is a result, not an error. The result is
    kept on the connection only if nothing it depends on -- key, Base URL,
    protocols, models -- changed while it ran (``recorded``).
    """
    _require_enabled()
    store = require_store(store)
    row = await _live(store, user["user_id"], connection_id)
    if _check_upstream(body.protocol, row["protocols"]) is None:
        raise _refused(400, "invalid_request", "This connection does not offer that protocol.")
    if body.model not in row["models"]:
        raise _refused(400, "invalid_request", "That model is not listed on this connection.")
    # What is checked is the row the reservation read, and what it is
    # recorded against is that row's revision -- never whatever is there later.
    checked = await _reserve(store, user["user_id"], connection_id)
    upstream = _check_upstream(body.protocol, checked["protocols"])
    if upstream is None or body.model not in checked["models"]:
        raise _refused(409, "model_connection_changed", "The connection changed. Try again.")
    result = await check_connection(
        base_url=checked["base_url"],
        api_key=_key(checked),
        protocol=body.protocol,
        model=body.model,
        upstream_protocol=upstream,
    )
    recorded = await store.model_connections.record_model_connection_check(
        user_id=user["user_id"],
        connection_id=connection_id,
        config_revision=int(checked["config_revision"]),
        result=result.as_dict(),
    )
    return ModelConnectionCheckView(**result.as_dict(), recorded=recorded)


@router.post("/{connection_id}/discover-models", response_model=DiscoveredModels)
async def discover(
    connection_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _Storage | None = Depends(get_storage),
) -> DiscoveredModels:
    """List the ids the provider's model list offers, for the owner to choose from.

    ``GET /models`` under the Base URL, or ``GET /v1/models`` with Messages
    authentication for a connection that offers only Anthropic Messages.
    """
    _require_enabled()
    store = require_store(store)
    await _live(store, user["user_id"], connection_id)
    checked = await _reserve(store, user["user_id"], connection_id)
    try:
        models = await discover_models(
            base_url=checked["base_url"],
            api_key=_key(checked),
            protocols=checked["protocols"],
        )
    except CheckStop as stop:
        raise _refused(
            422, f"model_connection_{stop.category}", CHECK_MESSAGES[stop.category]
        ) from None
    except TimeoutError:
        raise _refused(422, "model_connection_unreachable", CHECK_MESSAGES["unreachable"]) from None
    return DiscoveredModels(models=models)
