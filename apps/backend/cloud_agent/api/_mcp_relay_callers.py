"""Who is calling the MCP relay, and which connections that caller may reach.

Split from :mod:`cloud_agent.api.mcp_relay`. A sandbox's MCP credential is
resolved to the attempt it speaks for on every call (the fence), and a binding
alias to the connection, snapshot and upstream credential that attempt's owner
enabled. Every refusal here is the HTTP error the relay answers with; the
merged endpoint turns a selected connection's refusal into a JSON-RPC error
instead, so one disconnected server does not take the others down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from cloud_agent.api.errors import typed_error
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp import protocol
from cloud_agent.mcp.authorization import visible_capabilities
from cloud_agent.mcp.models import CapabilityKind
from cloud_agent.mcp.net_policy import policy_from_environment
from cloud_agent.mcp.refresh import RefreshRefused, ensure_fresh
from cloud_agent.mcp.registry import binding_from_row, resolve_connection
from cloud_agent.mcp.resources import resource_uri
from cloud_agent.tokens import (
    SCOPE_MCP,
    InvalidAgentToken,
    parse_residency_mcp_token,
    parse_worker_token,
)
from cloud_agent_shared.shared_protocol import is_residency_mcp_credential

if TYPE_CHECKING:
    from cloud_agent.api.mcp_relay import McpRelayStorage

# The relay's own logger: these events kept their name when the code moved.
logger = get_logger("cloud_agent.api.mcp_relay")


@dataclass
class Authorized:
    """The current fenced connection and any dispatch receipts for one request.

    Bundled rather than returned as a tuple because the fields are not
    interchangeable: a caller that swapped the binding for the connection would
    compile and authorize the wrong thing.
    """

    job_id: str
    user_id: str
    thread_id: str
    connection_id: int
    alias: str
    binding: Any
    connection: Any
    snapshot: Any
    #: The decrypted upstream credential, or ``None`` for a connection that
    #: authenticates by position or not at all. Carried because the relay dials
    #: the upstream itself.
    secret: str | None = None
    #: The attempt the credential was resolved to, and its lease: what every
    #: invocation record and fenced write of this request belongs to.
    attempt_id: int | None = None
    lease_generation: int = 0
    residency_generation: int | None = None
    dispatches: list[tuple[int, int]] = field(default_factory=list, compare=False)


class BindingDisabled(HTTPException):
    """A session selection stops all connection work before credentials are read."""

    def __init__(self) -> None:
        """Refuse transport access for an explicitly disabled selection."""
        super().__init__(403, "this MCP connection is not enabled for this session")


def bearer(authorization: str | None) -> str:
    """Extract the sandbox's MCP token, or refuse."""
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        raise typed_error(
            401, "invalid_request_error", "This endpoint takes an MCP-scoped job token."
        )
    return token


def refused(status_code: int, kind: str, message: str) -> HTTPException:
    """Build the JSON error shape every refusal on this surface uses."""
    return typed_error(status_code, kind, message)


def require_relay_store(store: McpRelayStorage | None) -> McpRelayStorage:
    """The relay's store, or the refusal a deployment without a database gets."""
    if store is None:
        raise refused(
            503, "invalid_request_error", "MCP is unavailable without a configured database."
        )
    return store


async def authorize_fence(
    authorization: str | None,
    store: McpRelayStorage | None,
) -> dict[str, Any]:
    """Resolve the token to the job it was minted for, or refuse.

    Split from the per-alias resolution below because the host endpoint serves
    every binding of one job from a single URL: it needs the fence once and the
    bindings many times, and re-reading the fence per tool would make a
    revocation take effect at a different moment for each tool in one listing.

    """
    token = bearer(authorization)
    residency_generation = None

    if is_residency_mcp_credential(token):
        # A resident process's credential: it names the chat's residency, not
        # an attempt, because the process serves turn after turn. It parses
        # only as that, so it is MCP-only by construction, like the scope
        # check below.
        try:
            residency = parse_residency_mcp_token(token)
            residency_generation = residency["residency_generation"]
        except InvalidAgentToken as exc:
            raise refused(401, "invalid_request_error", str(exc)) from exc
        store = require_relay_store(store)
        # Which turn it speaks for is decided now, on every call: the one the
        # residency is serving. Between turns, or once the residency rotated,
        # expired or was released, there is none and nothing is authorized.
        claims = await store.credential_queries.resolve_residency_attempt(
            thread_id=residency["thread_id"],
            residency_generation=residency["residency_generation"],
        )
        if claims is None:
            raise refused(
                401, "invalid_request_error", "This chat has no running turn for tool calls."
            )
    else:
        # COMPAT(remove-after=2026-11-30): no host reads the claim's mcp_token;
        # this branch goes with its mint in api/worker.py.
        try:
            claims = parse_worker_token(token)
        except InvalidAgentToken as exc:
            raise refused(401, "invalid_request_error", str(exc)) from exc

        # Positive, as the module docstring explains: a token minted before
        # scopes reads as `full`, and a runner's full-scope token buys no tools.
        if claims.get("scope") != SCOPE_MCP:
            raise refused(403, "insufficient_scope", "This endpoint requires an MCP-scoped token.")
        store = require_relay_store(store)

    # The fence, read fresh on every call. The claims name an attempt; this
    # asks whether that attempt is still the job's current one, still running,
    # with an unexpired lease. So revocation is automatic -- the instant the
    # reaper supersedes the attempt or the owner cancels, the same credential
    # stops buying tools, with no key to remember to revoke.
    return await recheck_fence({**claims, "residency_generation": residency_generation}, store)


def residency_fence(identity: dict[str, Any]) -> dict[str, int]:
    """Pass the additional fence only for a residency credential."""
    generation = identity.get("residency_generation")
    return {} if generation is None else {"residency_generation": int(generation)}


async def recheck_fence(captured: dict[str, Any], store: McpRelayStorage) -> dict[str, Any]:
    """Revalidate the original attempt and residency, never resolve another turn."""
    identity = await store.credential_queries.resolve_model_credential(
        job_id=captured["job_id"],
        attempt_id=captured["attempt_id"],
        lease_generation=captured["lease_generation"],
        **residency_fence(captured),
    )
    if identity is None:
        raise refused(
            401, "invalid_request_error", "This job token is no longer valid for tool calls."
        )
    # The attempt travels with the identity, so every record and write below
    # is fenced to the turn resolved here rather than re-read from the token.
    return {
        **identity,
        "attempt_id": int(captured["attempt_id"]),
        "lease_generation": int(captured["lease_generation"]),
        **residency_fence(captured),
    }


async def recheck_authorized(authorized: Authorized, store: McpRelayStorage) -> None:
    """Check a selected connection's captured caller after an asynchronous wait."""
    await recheck_fence(
        {
            "job_id": authorized.job_id,
            "attempt_id": authorized.attempt_id,
            "lease_generation": authorized.lease_generation,
            "residency_generation": authorized.residency_generation,
        },
        store,
    )


async def resolve_binding(
    identity: dict[str, Any],
    alias: str,
    store: McpRelayStorage,
    *,
    refresh_credentials: bool = True,
) -> Authorized:
    """Resolve one binding alias into the server this job may reach."""
    job_id = identity["job_id"]
    matching = [item for item in identity.get("mcp_bindings") or [] if item.get("alias") == alias]
    if len(matching) > 1:
        raise refused(
            403,
            "permission_error",
            f"More than one MCP connection uses alias {alias!r}. Save a unique selection in Tools.",
        )
    binding_row = matching[0] if matching else None
    if binding_row is None:
        # The session's own bindings, not a catalogue. A token is scoped to the
        # job it was minted for, and a job reaches exactly the connections its
        # conversation enabled.
        logger.warning(
            "agent_mcp_binding_not_granted",
            extra={
                "event": "agent_mcp_binding_not_granted",
                "job_id": job_id,
                "alias": alias,
            },
        )
        raise refused(
            403,
            "permission_error",
            f"This session is not bound to an MCP connection named {alias!r}.",
        )

    if (
        binding_row.get("deleted_at") is not None
        or binding_row.get("owner_id") != identity["user_id"]
    ):
        raise refused(404, "not_found", f"No MCP connection named {alias!r} is bound here.")

    if not binding_row.get("enabled", True):
        raise BindingDisabled()

    connection_row = await store.mcp.get_mcp_connection(
        connection_id=int(binding_row["connection_id"])
    )
    if connection_row is None:
        raise refused(503, "server_error", f"The {alias!r} MCP connection no longer exists.")
    if (
        not connection_row.get("enabled", True)
        or connection_row.get("status") in {"disabled", "error"}
        or connection_row.get("deleted_at") is not None
    ):
        raise refused(503, "server_error", f"The {alias!r} MCP connection is not usable.")
    if str(connection_row.get("transport")) == "stdio":
        # A program runs in the task's sandbox, behind the sandbox's own MCP
        # gateway, and this listener has nothing to run it with. Refused here
        # rather than forwarded to an address that does not exist, so the agent
        # is told where the server is instead of reporting one that cannot be
        # reached.
        logger.warning(
            "agent_mcp_stdio_not_runnable",
            extra={"event": "agent_mcp_stdio_not_runnable", "job_id": job_id, "alias": alias},
        )
        raise refused(
            503,
            "server_error",
            f"The {alias!r} MCP connection is a program that runs in the chat's sandbox, "
            "not through this relay.",
        )

    try:
        # Before the credential is read, so a token that aged out during a long
        # job is renewed instead of being sent and refused. Under the deployment
        # policy, the same one authorization, Test and the dial use: a token
        # endpoint those doors admit must not be refused only when a job asks.
        if refresh_credentials:
            connection_row = dict(
                await ensure_fresh(store.mcp, row=connection_row, policy=policy_from_environment())
            )
    except RefreshRefused as exc:
        logger.warning(
            "agent_mcp_refresh_refused",
            extra={
                "event": "agent_mcp_refresh_refused",
                "job_id": job_id,
                "alias": alias,
                "kind": exc.kind,
            },
        )
        raise refused(
            503,
            "server_error",
            f"The {alias!r} MCP connection needs to be reconnected.",
        ) from exc

    resolved = await resolve_connection(store.mcp, row=connection_row)
    if not resolved.usable:
        # Every method, not just ``tools/call``. ``decide`` covers the call path,
        # but ``initialize``, ``tools/list`` and the session verbs would
        # otherwise still be forwarded -- to a server this owner has turned off,
        # or with no credential at all when the deployment lost its encryption
        # key. A relay that dials a disconnected connection is a disconnect that
        # did not happen.
        logger.warning(
            "agent_mcp_connection_unusable",
            extra={
                "event": "agent_mcp_connection_unusable",
                "job_id": job_id,
                "alias": alias,
                "kind": resolved.error_kind.value,
            },
        )
        raise refused(503, "server_error", f"The {alias!r} MCP connection is not usable.")
    binding = binding_from_row(binding_row)
    return Authorized(
        job_id=job_id,
        user_id=identity["user_id"],
        thread_id=identity["thread_id"],
        connection_id=int(binding_row["connection_id"]),
        alias=alias,
        binding=binding,
        connection=resolved.connection,
        snapshot=resolved.snapshot,
        secret=resolved.credential.get("secret"),
        attempt_id=identity.get("attempt_id"),
        lease_generation=int(identity.get("lease_generation") or 0),
        residency_generation=identity.get("residency_generation"),
    )


async def resolve_live_binding(
    identity: dict[str, Any],
    alias: str,
    store: McpRelayStorage,
    *,
    attempt_id: int | None,
    lease_generation: int,
) -> Authorized:
    """Refresh only the selected connection, then recheck its live authority.

    OAuth can spend seconds on the network. The binding and attempt read before
    that wait cannot authorize dispatch after the user changes or revokes them.
    The second resolution reads current rows without another network refresh.
    """
    if attempt_id is None:
        raise refused(401, "permission_error", "This MCP session is no longer authorized.")
    captured = {
        **identity,
        "attempt_id": attempt_id,
        "lease_generation": lease_generation,
    }
    previous = await resolve_binding(await recheck_fence(captured, store), alias, store)
    current_identity = await recheck_fence(captured, store)
    current = await resolve_binding(current_identity, alias, store, refresh_credentials=False)
    if (
        current.connection_id != previous.connection_id
        or current.binding != previous.binding
        or current.connection.config_revision != previous.connection.config_revision
        or current.connection.authorization_epoch != previous.connection.authorization_epoch
        or current.connection.capability_revision != previous.connection.capability_revision
    ):
        raise refused(
            403, "permission_error", "MCP authorization changed while refreshing credentials."
        )
    return current


async def refresh_catalogue_entry(
    entry: tuple[Authorized, Any], identity: dict[str, Any], store: McpRelayStorage
) -> tuple[Authorized, Any]:
    """Refresh the chosen tool or prompt and use its current capability record."""
    previous, capability = entry
    current = await resolve_live_binding(
        identity,
        previous.alias,
        store,
        attempt_id=identity["attempt_id"],
        lease_generation=identity["lease_generation"],
    )
    if current.connection_id != previous.connection_id:
        raise refused(403, "permission_error", "The selected MCP connection changed.")
    latest = (
        next(
            (
                item
                for item in current.snapshot.of_kind(capability.kind)
                if item.name == capability.name
            ),
            None,
        )
        if current.snapshot is not None
        else None
    )
    if latest is None:
        raise refused(403, "permission_error", "This MCP capability is no longer available.")
    return current, latest


async def host_catalogue(
    identity: dict[str, Any],
    store: McpRelayStorage,
    kind: CapabilityKind = CapabilityKind.TOOL,
) -> dict[str, tuple[Authorized, Any]]:
    """Every capability of one kind this job may use, keyed by what it is called.

    One builder for all three surfaces, because they differ only in what the
    client uses as the key: a tool and a prompt by name, a resource by URI.
    Building them separately is how a surface ends up with its own idea of which
    bindings are reachable, and the design is explicit that resources and
    prompts get their own *scopes* -- not their own resolution rules.

    A binding that cannot be resolved is skipped rather than fatal. One
    disconnected server must not take the others out of the catalogue -- the
    agent would lose every tool it does have, which is a far worse failure than
    the one that caused it.

    Routing is a lookup in this map, not a string split. An alias may itself
    contain the separator, so splitting would send ``a__b__tool`` to the wrong
    server; the map is built from the catalogue, so a name that is not in it is
    genuinely not callable.
    """
    catalogue: dict[str, tuple[Authorized, Any]] = {}
    for binding_row in identity.get("mcp_bindings") or []:
        alias = str(binding_row.get("alias") or "")
        if not alias:
            continue
        try:
            # Discovery snapshots are local. Listing or routing one connection
            # must never contact another connection's OAuth endpoint.
            authorized = await resolve_binding(identity, alias, store, refresh_credentials=False)
        except HTTPException as exc:
            logger.warning(
                "agent_mcp_host_binding_unavailable",
                extra={
                    "event": "agent_mcp_host_binding_unavailable",
                    "job_id": identity["job_id"],
                    "alias": alias,
                    "detail": str(exc.detail),
                },
            )
            continue
        available = authorized.snapshot.of_kind(kind) if authorized.snapshot else ()
        for capability in visible_capabilities(authorized.binding, available, kind):
            # Connection-qualified URIs keep resource identity stable when
            # other connections advertise the same upstream URI or template.
            key = (
                resource_uri(authorized.connection_id, capability.name)
                if kind in (CapabilityKind.RESOURCE, CapabilityKind.RESOURCE_TEMPLATE)
                else protocol.presented_name(alias, capability.name)
            )
            catalogue.setdefault(key, (authorized, capability))
    return catalogue
