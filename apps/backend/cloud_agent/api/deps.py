"""Dependencies the routers ask for.

The routers moved from the gateway unchanged, so they still ask for the
dependencies they asked for there. Three kinds arrive at this module:

- **Wiring this service owns.** The job store, the source-control runtime
  (GitHub App credentials and GitLab OAuth), the gateway client and the grant
  client are all objects this repository builds; they are read off
  ``app.state``, exactly as the source read them off its own services
  container. ``None`` is a working answer for the optional ones — a deployment
  without a configured database or App is a supported shape, and the routes
  already handle it. A configured App this process could not load is *not*
  ``None``: see :func:`get_agent_app_credentials`. The attribute names
  (``store``, ``gateway``, ``grants``, ``source_control``) are the contract
  with the lifespan; renaming one there without renaming it here degrades
  silently to "not configured" rather than failing.

- **Identity and admin auth, which this service answers from its own world.**
  There is no ``users`` table here and no ``api_keys`` table: the gateway kept
  both. So the browser credential is the session token
  :mod:`cloud_agent.identity` mints — carried in an ``HttpOnly`` cookie, or as a
  bearer for programmatic callers holding one — and the admin door is the
  deployment's ``ADMIN_TOKEN`` or a session with current Gateway agent.admin permission.
  Nothing here consults a user table, because there is none to consult.

  There used to be a third: ``verify_api_key``, resolving an ``hyi-*`` key
  through a ``get_operational_store`` that nothing in this repository ever set.
  **Both were deleted on 2026-08-04.** A dependency that answers 503 in every
  real deployment is dead code that looks alive, and the only way to "fix" it
  from inside this repository is to reach back into the gateway's database.
  Resolving a key to a user needs a gateway contract in ``gateway_contracts/`` and a
  ``GatewayClient`` method; until one exists the session is the only owner
  credential. See MIGRATION.md's deferred register and its discarded-test row.

- **One contract that did not come across, and is honest about it.**
  ``get_log_store`` names a gateway-side table (``api_logs``). Nothing in this
  repository produces it, so it answers ``None`` in every real deployment and
  the code path behind it degrades the way it was written to: job spend is
  reported as *absent* rather than as a fabricated zero.

**Every decision below fails closed.** An unset secret, an unreachable gateway,
an unconfigured store: each is a refusal, never a pass. That is the one property
worth re-reading a diff of this file for.
"""

from __future__ import annotations

import hmac
import os
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

import jwt
from fastapi import Depends, Header, HTTPException, Request

from cloud_agent import identity

#: Whatever a surface narrowed the store to; this only removes the ``None``.
_StoreT = TypeVar("_StoreT")
from cloud_agent.agent_access import AgentAccessDecision
from cloud_agent.api.errors import AgentConfigurationError
from cloud_agent.gateway import GatewayError
from cloud_agent.logging_utils import get_logger
from cloud_agent.source_control_runtime import (
    RETRY_INTERVAL_S,
    GitHubAppState,
    LoadStatus,
    SourceControlRuntime,
    SourceControlUnavailable,
)

if TYPE_CHECKING:
    from cloud_agent.gateway import GatewayClient
    from cloud_agent.grants import GrantClient
    from cloud_agent.ports import AuditRepository
    from cloud_agent.storage.assembly import Storage
    from cloud_agent.workers.job_event_signal import JobEventSignal
    from cloud_agent.workers.mcp_decision_signal import McpDecisionSignal
    from cloud_agent.workers.owner_change_signal import OwnerChangeSignal
    from cloud_agent.workers.work_signal import WorkSignal


class _Audited(Protocol):
    """What recording an admin action reaches for: the assembly's audit port.

    ``Storage`` has no top-level ``record_admin_action`` — it never had one
    after #210 split the store into capabilities — so asking the assembly for
    the method by name found nothing and the trail was skipped in silence.
    Naming the one attribute this needs makes the mistake a type error.
    """

    audit: AuditRepository


# ── Wiring this service owns ───────────────────────────────────────────


def get_storage(request: Request) -> Storage | None:
    """Dependency to obtain the assembled storage layer (if configured).

    One provider for all sixteen capabilities, and not sixteen providers: a
    handler takes this and annotates the Protocol naming the capabilities it
    uses, so the narrowing is stated where the calls are, next to them.
    """
    return getattr(request.app.state, "storage", None)


def get_gateway_client(request: Request) -> GatewayClient:
    """Dependency to obtain the client for the gateway's internal contracts.

    Not optional: the catalog, the user's status and the MCP registry are all
    answered over HTTP now, so a control plane with no gateway client cannot
    answer for a job at all. An unconfigured client still constructs, and
    refuses at call time naming the variables it needs — which is where the
    operator can act on it, unlike a dependency that returned ``None``.
    """
    client = getattr(request.app.state, "gateway", None)
    if client is None:
        from cloud_agent.gateway import GatewayClient as _GatewayClient

        client = _GatewayClient()
        request.app.state.gateway = client
    return client


def get_grant_client(request: Request) -> GrantClient:
    """Dependency to obtain the client that asks the gateway for grants.

    Built like the gateway client and for the same reason: an unconfigured one
    constructs fine and refuses at the call, naming the two variables it needs.
    A claim that cannot mint a grant hands the job back to the queue rather
    than sending a runner off with no credential — see ``worker_claim``.
    """
    client = getattr(request.app.state, "grants", None)
    if client is None:
        from cloud_agent.grants import GrantClient as _GrantClient

        client = _GrantClient()
        request.app.state.grants = client
    return client


def get_work_signal(request: Request) -> WorkSignal | None:
    """Dependency to obtain the signal idle hosts wait on.

    ``None`` in a process that does not listen for work: the MCP relay, and a
    test application that has not set one.
    """
    return getattr(request.app.state, "work_signal", None)


def get_job_event_signal(request: Request) -> JobEventSignal | None:
    """Dependency to obtain the wake-ups owners' event streams wait on.

    ``None`` where nothing listens for job events: the MCP relay, and a test
    application that has not set one. Streams there read on a clock instead.
    """
    return getattr(request.app.state, "job_event_signal", None)


def get_owner_change_signal(request: Request) -> OwnerChangeSignal | None:
    """Dependency to obtain the per-owner change sets tabs' change streams read.

    ``None`` where nothing listens for owner changes: the MCP relay, and a test
    application that has not set one. Tabs there read on their own clocks.
    """
    return getattr(request.app.state, "owner_change_signal", None)


def get_mcp_decision_signal(request: Request) -> McpDecisionSignal | None:
    """The wake-ups for tool calls the MCP relay holds for their owner's decision.

    ``None`` where nothing listens for decisions: the main listener, which holds
    no calls, and a test application that has not set one. A held call there
    reads its row on a clock instead.
    """
    return getattr(request.app.state, "mcp_decision_signal", None)


def get_source_control(request: Request) -> SourceControlRuntime | None:
    """Dependency to obtain the process's source-control runtime.

    ``None`` only where nothing built one — a test application that injects
    credentials through the dependencies below. The lifespan always does.
    """
    return getattr(request.app.state, "source_control", None)


def source_control_unavailable(provider: str) -> HTTPException:
    """The answer while a configured provider cannot be loaded (issue #47).

    503 with a retry hint, and nothing about why: the reason is an operator's
    concern and is in the ERROR log. It must not read as "not configured" or
    "no access" — both send the reader to fix something that is not broken.
    """
    name = {"github": "GitHub", "gitlab": "GitLab"}.get(provider, provider)
    return HTTPException(
        status_code=503,
        detail={
            "error": {
                "type": "source_control_unavailable",
                "provider": provider,
                "message": f"{name} is temporarily unavailable. Please try again shortly.",
            }
        },
        headers={"Retry-After": str(int(RETRY_INTERVAL_S))},
    )


def get_agent_app_credentials(
    source_control: SourceControlRuntime | None = Depends(get_source_control),
) -> Any | None:
    """Dependency to obtain the GitHub App credential minter (if configured).

    **Strict, and the default for that reason.** A configured App this process
    could not load answers 503 before the handler runs: with ``None`` in hand
    every caller would take the no-App path — the allowlist-only entitlement
    check, the anonymous checkout — which is exactly the misreading issue #47
    was about. A route that must still answer takes
    :func:`get_github_app_state` instead, and says why.
    """
    if source_control is None:
        return None
    try:
        return source_control.github.get()
    except SourceControlUnavailable as exc:
        raise source_control_unavailable(exc.provider) from exc


def get_github_app_state(
    source_control: SourceControlRuntime | None = Depends(get_source_control),
) -> GitHubAppState:
    """Dependency to obtain the App's credentials and load status, together.

    The lenient counterpart of :func:`get_agent_app_credentials`: ``None`` for
    the credentials while the App is unavailable, never a 503. For the surfaces
    that describe the state rather than act on it — the Integrations page, the
    composer's config, a disconnect.

    One reading, not two dependencies: read separately, a reload finishing in
    between hands a route ``None`` beside ``ready``, and the page then says
    GitHub is not configured. A test application with no runtime overrides
    this, or gets an App that is not configured.
    """
    if source_control is None:
        return GitHubAppState(credentials=None, load_status="unconfigured")
    return source_control.github.state()


def get_agent_gitlab_oauth(
    source_control: SourceControlRuntime | None = Depends(get_source_control),
) -> Any | None:
    """Dependency to obtain the configured GitLab.com OAuth client.

    ``None`` when GitLab is unconfigured or failed to load, as before: GitLab is
    not offered to connect, and it is never retried (see
    :mod:`cloud_agent.source_control_runtime`).
    """
    return None if source_control is None else source_control.gitlab.client


def get_gitlab_load_status(
    source_control: SourceControlRuntime | None = Depends(get_source_control),
    gitlab_oauth: Any | None = Depends(get_agent_gitlab_oauth),
) -> LoadStatus:
    """Dependency to obtain GitLab's load status.

    Read beside the client without a shared snapshot, and safely: GitLab is
    loaded once, so neither can change after startup.
    """
    if source_control is not None:
        return source_control.gitlab.load_status
    return "ready" if gitlab_oauth is not None else "unconfigured"


# ── The gateway-side contract that did not move ────────────────────────


def get_log_store(request: Request) -> Any | None:
    """Dependency to obtain the log store (if configured).

    Job spend was summed from the gateway's ``api_logs`` ledger, and that
    ledger stayed there — MIGRATION.md's "What stays in HybridInference" says
    so explicitly. Nothing in this repository sets ``app.state.log_store``, so
    this answers ``None`` and ``_job_usage`` omits spend and token totals from
    the job response. Absent, deliberately, rather than a fabricated zero: a
    zero would read as "this job was free".
    """
    return getattr(request.app.state, "log_store", None)


# ── Agent permissions and credential namespaces ────────────────────────


async def resolve_agent_access(
    gateway: Any, user_id: str, *, reject_unavailable_subject: bool = False
) -> AgentAccessDecision:
    """Read current Agent permissions, with no session or role fallback.

    Login additionally rejects an unavailable subject. A policy denial for an
    active account may still establish identity for the access-required page.
    """
    try:
        decision = AgentAccessDecision.model_validate(await gateway.agent_access(user_id))
        if decision.user_id != user_id:
            raise ValueError("Agent access subject mismatch")
        return decision
    except GatewayError as exc:
        if exc.status_code == 403 and exc.error_type == "subject_unavailable":
            if reject_unavailable_subject:
                raise HTTPException(status_code=403, detail="This account cannot sign in.") from exc
            return AgentAccessDecision(user_id=user_id, allowed=False, permissions=[])
        if exc.status_code in (401, 404) or exc.error_type == "gateway_not_configured":
            get_logger(__name__).warning(
                "agent_access_configuration_error",
                extra={"event": "agent_access_configuration_error", "status_code": exc.status_code},
            )
            raise AgentConfigurationError() from None
    except ValueError:
        pass
    get_logger(__name__).warning(
        "agent_access_unavailable", extra={"event": "agent_access_unavailable", "user_id": user_id}
    )
    raise HTTPException(
        status_code=503,
        detail="Cannot confirm Agent access with the gateway; try again shortly.",
    )


async def require_agent_access(
    gateway: Any, user_id: str, permission: str = "agent.use"
) -> AgentAccessDecision:
    """Require an explicit current Gateway permission before accessing Agent data."""
    decision = await resolve_agent_access(gateway, user_id)
    if not decision.permits(permission):
        raise HTTPException(
            status_code=403,
            detail=(
                "Agent administration has not been granted for this account."
                if permission == "agent.admin"
                else "Agent access has not been granted for this account."
            ),
        )
    return decision


#: The namespace :func:`cloud_agent.tokens.mint_worker_token` mints into. Kept
#: as a literal rather than imported, because the token module's prefix is
#: private; ``test_the_agent_token_prefix_still_matches_what_tokens_mints``
#: fails the moment the two drift.
AGENT_TOKEN_PREFIX = "ajt."

#: The namespace :func:`cloud_agent.tokens.mint_host_token` mints into.
HOST_TOKEN_PREFIX = "aht."

ENV_ADMIN_TOKEN = "ADMIN_TOKEN"

#: Off by default. An ``X-Forwarded-For`` header is caller-controlled input
#: unless a proxy this deployment trusts overwrote it, so believing it without
#: being told to would let anyone write any address into the audit trail.
ENV_TRUST_PROXY_HEADERS = "AGENT_TRUST_PROXY_HEADERS"


def looks_like_agent_token(api_key: str | None) -> bool:
    """Whether a credential should be resolved as a worker token.

    Structural, not a validation: it selects which authenticator a credential
    reaches, so that an expired session stays a session failure instead of
    being retried as something else.
    """
    return bool(api_key and api_key.startswith(AGENT_TOKEN_PREFIX))


def bearer_token(authorization: str | None) -> str | None:
    """Return the bearer credential in an ``Authorization`` header, if any."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[7:].strip() or None


# ── Identity ───────────────────────────────────────────────────────────


def session_token(request: Request, authorization: str | None = None) -> str | None:
    """Find this service's session credential on a request.

    The cookie is preferred: it is what the web app sends, it is ``HttpOnly``,
    and it is the only credential the browser holds. A bearer is accepted too,
    for the programmatic caller that has one and for the router's existing
    credential dispatch — a session token is this service's own credential
    whichever way it travels, and reading it from a header weakens nothing that
    reading it from a cookie does not.
    """
    cookie = request.cookies.get(identity.SESSION_COOKIE)
    if cookie:
        return cookie
    presented = bearer_token(authorization)
    if presented and not looks_like_agent_token(presented):
        return presented
    return None


async def get_current_user(
    request: Request,
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """Verify the minimal session identity.

    Agent permissions are resolved separately at the Gateway on every
    protected request. A role in an old session never grants Agent access.
    """
    token = session_token(request, authorization)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Missing authentication token. Use 'Authorization: Bearer {token}' header.",
        )

    try:
        claims = identity.verify_session(token)
    except identity.IdentityNotConfigured:
        # A deployment with no session secret cannot verify anyone. 503, not
        # 401: the caller did nothing wrong and retrying with another
        # credential will not help.
        raise AgentConfigurationError() from None
    except identity.IdentityError as exc:
        cause = exc.__cause__ if exc.__cause__ is not None else exc
        if isinstance(cause, jwt.ExpiredSignatureError):
            # An expired session must stay an expired session. The dispatch in
            # ``authenticate_agent_owner`` exists so this is never retried as
            # an API key, and the message is what tells a client which it was.
            raise HTTPException(
                status_code=401,
                detail="Token has expired. Please refresh your token or login again.",
            ) from None
        raise HTTPException(status_code=401, detail="Invalid authentication token.") from None

    user_id = str(claims.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload.")

    return {"user_id": user_id, "authenticated": True}


async def verify_admin_access(
    request: Request,
    authorization: str | None = Header(None),
    gateway: Any = Depends(get_gateway_client),
) -> str:
    """Authenticate an infrastructure token or a current Agent administrator.

    ADMIN_TOKEN remains independent of the Gateway for local operations.
    Browser sessions require a fresh agent.admin decision for reads and writes;
    their identity/profile claims confer no administrative authority.
    """
    presented = bearer_token(authorization)
    configured = (os.environ.get(ENV_ADMIN_TOKEN) or "").strip()
    if configured and presented and hmac.compare_digest(presented, configured):
        return "admin-token"

    token = session_token(request, authorization)
    if token:
        try:
            claims = identity.verify_session(token)
        except identity.IdentityNotConfigured:
            raise AgentConfigurationError() from None
        except identity.IdentityError:
            claims = None
        if claims is not None:
            user_id = str(claims.get("sub") or "")
            if user_id:
                await require_agent_access(gateway, user_id, "agent.admin")
                return f"admin:{user_id}"

    if not presented and token is None:
        raise HTTPException(
            status_code=401,
            detail="Missing authentication. Provide a JWT or admin token via Authorization header.",
        )
    raise HTTPException(status_code=401, detail="Invalid authentication token.")


# ── The admin audit trail ──────────────────────────────────────────────


def get_client_ip(request: Request) -> str:
    """Return the best-effort originating client IP for a request.

    The socket peer, unless this deployment says a proxy in front of it
    rewrites ``X-Forwarded-For`` — see ``AGENT_TRUST_PROXY_HEADERS``. Believing
    the header unconditionally would let any caller write any address into the
    audit trail, which is worse than recording the proxy's address honestly.
    """
    if (os.environ.get(ENV_TRUST_PROXY_HEADERS) or "").strip().lower() in ("1", "true", "yes"):
        forwarded = request.headers.get("x-forwarded-for") or ""
        first_hop = forwarded.split(",")[0].strip()
        if first_hop:
            return first_hop
        real_ip = (request.headers.get("x-real-ip") or "").strip()
        if real_ip:
            return real_ip
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


async def log_admin_action(
    store: _Audited | None,
    admin_ip: str,
    action: str,
    target_user_id: str | None = None,
    details: dict[str, Any] | None = None,
    success: bool = True,
    *,
    actor_id: str,
) -> None:
    """Append one admin action to this service's audit trail.

    The gateway's ``admin_audit_log`` stayed with the gateway, so this service
    grew its own rather than a documented no-op: the ``agent_admin_audit_log``
    table, created with the rest of the schema by
    ``migrations/versions/0001_schema_baseline.py``. The
    actions logged here revoke a machine's credential, retarget every future
    claim and remove hosts from the pool; a service that owns that surface and
    keeps no record of who used it has no answer to the only question asked
    after an incident.

    **Never raises, and never undoes the action it is recording.** The write
    has already happened by the time this is called, so a failure here is
    logged and swallowed: turning an audit-write failure into a 500 would tell
    the operator their drain failed when it did not.

    Args:
        store: The agent job store, or ``None`` on a deployment with no
            database. The row goes through ``store.audit``, the capability
            ``Storage`` exposes; #210 removed the top-level method this used to
            look for, and the lookup kept answering "no such method" to a store
            that had one, so nothing was recorded at all.
        admin_ip: Best-effort caller address, from :func:`get_client_ip`.
        action: Dotted action name, e.g. ``agent.runner_host.drain``.
        actor_id: The authenticated administrator, from ``verify_admin_access``
            — the answer to "who", which the address alone never was.
        target_user_id: The user acted upon, where there is one.
        details: Structured context. Never a credential — this trail is read by
            more people than hold the things it would otherwise contain.
        success: Whether the action succeeded.
    """
    from cloud_agent.logging_utils import get_logger

    if store is None:
        return
    try:
        await store.audit.record_admin_action(
            action=action,
            actor_id=actor_id,
            admin_ip=admin_ip,
            target_user_id=target_user_id,
            details=details,
            success=success,
        )
    except Exception:
        get_logger(__name__).warning(
            "agent_admin_audit_write_failed",
            exc_info=True,
            extra={"event": "agent_admin_audit_write_failed", "action": action},
        )


def require_store(store: _StoreT | None) -> _StoreT:
    """Return the store or fail with a clear 503 when the DB is absent.

    Shared by the surfaces that answer with this shape. A router that answers
    with a different refusal keeps its own, so no response body changes.
    """
    if store is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "unavailable",
                    "message": "Agent jobs require a configured database.",
                }
            },
        )
    return store
