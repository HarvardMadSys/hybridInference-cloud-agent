"""Dependencies the routers ask for.

The routers moved from the gateway unchanged, so they still ask for the
dependencies they asked for there. Three kinds arrive at this module:

- **Wiring this service owns.** The job store, the GitHub App credential
  minter, the GitLab OAuth client, the gateway client and the grant client are
  all objects this repository builds; they are read off ``app.state``, exactly
  as the source read them off its own services container. ``None`` is a working
  answer for the optional ones — a deployment without a configured database or
  App is a supported shape, and the routes already handle it. The attribute
  names (``store``, ``gateway``, ``grants``, ``app_credentials``,
  ``gitlab_oauth``) are the contract with ``app.py``'s lifespan; renaming one
  there without renaming it here degrades silently to "not configured" rather
  than failing.

- **Identity and admin auth, which E7 answers from this service's own world.**
  There is no ``users`` table here and no ``api_keys`` table: the gateway kept
  both. So the browser credential is the session token
  :mod:`cloud_agent.identity` mints — carried in an ``HttpOnly`` cookie, or as a
  bearer for programmatic callers holding one — and the admin door is the
  deployment's ``ADMIN_TOKEN`` or a session whose role claim says ``admin``.
  Nothing here consults a user table, because there is none to consult.

  There used to be a third: ``verify_api_key``, resolving an ``hyi-*`` key
  through a ``get_operational_store`` that nothing in this repository ever set.
  **Both were deleted on 2026-08-04.** A dependency that answers 503 in every
  real deployment is dead code that looks alive, and the only way to "fix" it
  from inside this repository is to reach back into the gateway's database.
  Resolving a key to a user needs a gateway contract in ``contracts/`` and a
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
from typing import TYPE_CHECKING, Any

import jwt
from fastapi import Depends, Header, HTTPException, Request

from cloud_agent import identity

if TYPE_CHECKING:
    from cloud_agent.gateway import GatewayClient
    from cloud_agent.grants import GrantClient
    from cloud_agent.storage.store import AgentJobStore


# ── Wiring this service owns ───────────────────────────────────────────


def get_agent_job_store(request: Request) -> AgentJobStore | None:
    """Dependency to obtain the agent job store (if configured)."""
    return getattr(request.app.state, "store", None)


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


def get_agent_app_credentials(request: Request) -> Any | None:
    """Dependency to obtain the GitHub App credential minter (if configured)."""
    return getattr(request.app.state, "app_credentials", None)


def get_agent_gitlab_oauth(request: Request) -> Any | None:
    """Dependency to obtain the configured GitLab.com OAuth client."""
    return getattr(request.app.state, "gitlab_oauth", None)


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


# ── Roles and credential namespaces ────────────────────────────────────

#: Verbatim from the source's ``serving/config/settings.py``. Fail-closed on an
#: unknown *required* role; an unknown *user* role ranks as ``free``.
ROLE_RANK: dict[str, int] = {"free": 0, "pro": 1, "internal": 2, "admin": 3}


#: The statuses the gateway may report that still mean "may use this service".
#: Anything else — suspended, banned, deleted, unknown — is refused, because an
#: allowlist is the only version of this check that fails closed when the
#: gateway grows a new status nobody here has heard of.
ACTIVE_STATUSES = frozenset({"active", "ok", "enabled"})


#: Methods that only read. Everything else is a privileged action, and
#: revalidates the account that is about to perform it.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def assert_still_admin(gateway: Any, user_id: str) -> None:
    """Refuse a session admin the gateway no longer vouches for.

    Args:
        gateway: The gateway's contracts.
        user_id: The account the session names.

    Raises:
        HTTPException: 403 if the account is inactive or no longer an admin,
            503 if the gateway could not be asked — fail closed, because an
            authority that cannot be reached has not granted anything.
    """
    from cloud_agent.gateway import GatewayError
    from cloud_agent.logging_utils import get_logger

    try:
        status = await gateway.user_status(user_id)
    except GatewayError as exc:
        get_logger(__name__).warning(
            "agent_admin_revalidation_unavailable",
            exc_info=True,
            extra={"event": "agent_admin_revalidation_unavailable", "user_id": user_id},
        )
        raise HTTPException(
            status_code=503,
            detail="Cannot confirm this account with the gateway; try again shortly.",
        ) from exc

    if not may_sign_in(status) or str(status.get("role") or "") != "admin":
        raise HTTPException(status_code=403, detail="This account is not an administrator.")


def may_sign_in(status: dict[str, Any]) -> bool:
    """Read the gateway's status record as a yes or a no.

    One reader, used at sign-in and again when a privileged action
    revalidates, so the two cannot come to disagree about what the gateway's
    answer means.

    Written against the shape the client's own tests pin —
    ``{"user_id", "exists", "active", "role"}`` — and tolerant of a record that
    says the same thing with a ``status`` string instead.

    Args:
        status: The gateway's status record.

    Returns:
        Whether the account may use this service. Every branch that cannot find
        an affirmative answer returns ``False``.
    """
    if status.get("exists") is False:
        return False
    active = status.get("active")
    if isinstance(active, bool):
        return active
    named = status.get("status")
    if isinstance(named, str):
        return named.lower() in ACTIVE_STATUSES
    # No recognizable statement either way. Refuse: a record this service
    # cannot read is not a record that granted anything.
    return False


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


def has_role(user_role: str, required: str) -> bool:
    """Whether ``user_role`` meets or exceeds the required role level.

    Args:
        user_role: The role the caller presented.
        required: The role the route demands.

    Returns:
        Whether the caller ranks at or above ``required``. An unknown
        *required* role is never satisfied (a typo must not open a door), and an
        unknown *user* role ranks as ``free``.
    """
    if required not in ROLE_RANK:
        return False
    return ROLE_RANK.get(user_role, 0) >= ROLE_RANK[required]


def looks_like_agent_token(api_key: str | None) -> bool:
    """Whether a credential should be resolved as a worker token.

    Structural, not a validation: it selects which authenticator a credential
    reaches, so that an expired session stays a session failure instead of
    being retried as something else.
    """
    return bool(api_key) and api_key.startswith(AGENT_TOKEN_PREFIX)


def bearer_token(authorization: str | None) -> str | None:
    """Return the bearer credential in an ``Authorization`` header, if any."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[7:].strip() or None


# ── Identity — E7 ──────────────────────────────────────────────────────


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
    """Verify this service's session and return the current user context.

    The gateway verified its own access JWT here against its ``users`` table.
    There is no users table on this side, so the session token *is* the
    context: it was minted at ``/v1/session/login`` from a gateway identity
    token, after the gateway was asked whether that user may sign in at all.

    Deliberately **not** re-asking the gateway on every request. A per-request
    status call would put the whole owner surface behind the gateway's
    availability and fail closed on every page load during an outage; the
    account check happens once, at login, and the cookie's own expiry bounds
    how long a stale answer can survive.

    Args:
        request: The incoming request, for its cookies.
        authorization: The ``Authorization`` header, if the caller sent one.

    Returns:
        ``{"user_id", "email", "user_name", "role", "is_admin",
        "authenticated"}`` — the keys ``routes.py`` and ``/v1/session/me``
        read. Everything in it comes from the session's own claims, which is
        the only thing this dependency can prove: ``/me`` and ``/login``
        therefore answer with the same values for the same session, rather than
        login reporting a name that ``/me`` could only ever report as null.

    Raises:
        HTTPException: 401 when no valid session is presented, 503 when this
            deployment cannot verify sessions at all.
    """
    token = session_token(request, authorization)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Missing authentication token. Use 'Authorization: Bearer {token}' header.",
        )

    try:
        claims = identity.verify_session(token)
    except identity.IdentityNotConfigured as exc:
        # A deployment with no session secret cannot verify anyone. 503, not
        # 401: the caller did nothing wrong and retrying with another
        # credential will not help.
        raise HTTPException(status_code=503, detail=str(exc)) from None
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

    role = str(claims.get("role") or "free")
    return {
        "user_id": user_id,
        "email": claims.get("email"),
        # Carried in the session because the session is what ``/me`` can prove.
        # Absent from the claims — every deployment today, since neither
        # gateway contract reports a display name — reads as ``None`` here and
        # at login alike.
        "user_name": claims.get("user_name"),
        "role": role,
        "is_admin": role == "admin",
        "authenticated": True,
    }


async def verify_admin_access(
    request: Request,
    authorization: str | None = Header(None),
    gateway: Any = Depends(get_gateway_client),
) -> str:
    """Authenticate an admin caller, returning an identifier for the audit log.

    Two doors, and both of them are this service's own:

    - ``ADMIN_TOKEN``, the machine-to-machine credential. Compared in constant
      time, and an **unset** value opens nothing — otherwise a deployment that
      forgot to configure it would be an open admin surface, which is the
      failure mode this whole file is written against.
    - A session whose role claim is ``admin``. That is how the dashboard
      authenticates, and the role comes from the gateway's own answer at login
      rather than from anything the browser can edit.

    **A session admin is re-checked at the gateway before it may change
    anything**, the same way ``require_agent_owner`` re-checks an owner. The
    role in the session was true when it was minted and says nothing about a
    demotion since; without this, an administrator the gateway suspends or
    downgrades keeps the power to drain hosts and pin runtime versions for the
    rest of the session's life. The ``ADMIN_TOKEN`` door is deliberately left
    alone: it is a machine credential with no user behind it to go stale, and
    it must keep working on a deployment that has no gateway configured.

    Reads are not re-checked, for the reason they are not on the owner surface:
    an outage would otherwise close every admin page, and the session's own
    expiry already bounds how stale a read can be.

    **A missing ``AGENT_SESSION_SECRET`` is answered the same way here as at
    :func:`get_current_user`: 503.** It used to be a 401, because the broader
    ``IdentityError`` was caught and an unverifiable session simply did not
    open the door — which sent an operator looking for a credential problem
    that did not exist, while the same misconfiguration was diagnosed correctly
    one function away. The admin *token* is checked first and is unaffected: a
    deployment that authenticates machines only keeps working with no session
    secret at all, because that path returns before this one is reached.

    Args:
        request: The incoming request, for its cookies.
        authorization: The ``Authorization`` header, if the caller sent one.

    Returns:
        ``"admin-token"`` or ``"admin:<user_id>"``, for the audit trail.

    Raises:
        HTTPException: 401 when neither door opens, 503 when this deployment
            cannot verify sessions at all.
    """
    presented = bearer_token(authorization)
    configured = (os.environ.get(ENV_ADMIN_TOKEN) or "").strip()
    if configured and presented and hmac.compare_digest(presented, configured):
        return "admin-token"

    token = session_token(request, authorization)
    if token:
        try:
            claims = identity.verify_session(token)
        except identity.IdentityNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except identity.IdentityError:
            claims = None
        if claims is not None and str(claims.get("role") or "") == "admin":
            user_id = str(claims.get("sub") or "")
            if request.method.upper() not in SAFE_METHODS:
                await assert_still_admin(gateway, user_id)
            return f"admin:{user_id}"

    if not presented and token is None:
        raise HTTPException(
            status_code=401,
            detail="Missing authentication. Provide a JWT or admin token via Authorization header.",
        )
    raise HTTPException(status_code=401, detail="Invalid authentication token.")


# ── The admin audit trail — E7 ─────────────────────────────────────────


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
    store: Any,
    admin_ip: str,
    action: str,
    target_user_id: str | None = None,
    details: dict[str, Any] | None = None,
    success: bool = True,
) -> None:
    """Append one admin action to this service's audit trail.

    The gateway's ``admin_audit_log`` stayed with the gateway, so this service
    grew its own — migration ``0005_grants_and_audit`` — rather than a
    documented no-op. The
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
            database. A store that predates ``record_admin_action`` is treated
            the same way — the trail is skipped, not fabricated.
        admin_ip: Best-effort caller address, from :func:`get_client_ip`.
        action: Dotted action name, e.g. ``agent.runner_host.drain``.
        target_user_id: The user acted upon, where there is one.
        details: Structured context. Never a credential — this trail is read by
            more people than hold the things it would otherwise contain.
        success: Whether the action succeeded.
    """
    from cloud_agent.logging_utils import get_logger

    recorder = getattr(store, "record_admin_action", None)
    if recorder is None:
        return
    try:
        await recorder(
            action=action,
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
