"""Dependencies the routers ask for.

Two kinds arrive at this module:

- **Wiring this service owns.** The job store, the source-control runtime
  (GitHub App credentials and GitLab OAuth), the account directory and the
  shared HTTP pool are all objects this repository builds; they are read off
  ``app.state``. ``None`` is a working answer for the optional ones: a
  deployment without a configured App is a supported shape, and the routes
  already handle it. A configured App this process could not load is *not*
  ``None``: see :func:`get_agent_app_credentials`. The attribute names
  (``storage``, ``owners``, ``source_control``, ``http``) are the contract
  with the lifespan.

- **Identity and admin auth.** The browser credential is an account session,
  a server-side row in this database carried in an ``HttpOnly`` cookie or as
  a bearer of the session's own shape. Agent permissions are read from the
  account on every protected request. The admin door is the deployment's
  ``ADMIN_TOKEN`` or a session whose account currently holds ``agent.admin``.

**Every decision below fails closed.** An unset secret, an unreadable account
store, an unconfigured store: each is a refusal, never a pass. That is the one
property worth re-reading a diff of this file for.
"""

from __future__ import annotations

import hmac
import os
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from fastapi import Depends, Header, HTTPException, Request

from cloud_agent.accounts.directory import (
    LocalDirectoryUnavailable,
    LocalOwnerDirectory,
    OwnerDirectoryError,
)
from cloud_agent.agent_access import AgentAccessDecision
from cloud_agent.api.errors import AgentConfigurationError
from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import number_setting, setting_env
from cloud_agent.source_control_runtime import (
    GitHubAppState,
    LoadStatus,
    SourceControlRuntime,
    SourceControlUnavailable,
)

#: Whatever a surface narrowed the store to; this only removes the ``None``.
_StoreT = TypeVar("_StoreT")

if TYPE_CHECKING:
    import httpx

    from cloud_agent.ports import AuditRepository
    from cloud_agent.ports.accounts import SessionOwner
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


class _NoDirectory:
    """The directory of an application with no store: every question is refused.

    Not a verdict on anyone: :class:`LocalDirectoryUnavailable` reads as 503,
    so a request that needed an account is refused and one admitted by
    ``ADMIN_TOKEN`` never asks.
    """

    async def agent_access(self, user_id: str) -> AgentAccessDecision:
        raise LocalDirectoryUnavailable("no account store is configured")

    async def user_email(self, user_id: str) -> str:
        raise LocalDirectoryUnavailable("no account store is configured")


def get_owner_directory(request: Request) -> LocalOwnerDirectory | _NoDirectory:
    """Dependency to obtain the account directory every owner check reads.

    The lifespan installs it as ``app.state.owners``. An application that
    did not (a test application with only a store) gets one over its store,
    built once. With no store there is no account to admit: the directory it
    gets refuses every question with 503 rather than admitting anyone.
    """
    owners = getattr(request.app.state, "owners", None)
    if owners is not None:
        return owners
    storage = get_storage(request)
    accounts = getattr(storage, "local_accounts", None) if storage is not None else None
    if accounts is None:
        return _NoDirectory()
    owners = LocalOwnerDirectory(accounts)
    request.app.state.owners = owners
    return owners


def get_http_client(request: Request) -> httpx.AsyncClient | None:
    """Dependency to obtain the process's shared outbound connection pool.

    Lent to calls that would otherwise open a client of their own. ``None`` in
    a test application that has not set one: those callers then open a client
    for the call.
    """
    return getattr(request.app.state, "http", None)


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

    ``None`` in the MCP relay, which has no source-control work, and in a test
    application that injects credentials through the dependencies below.
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
        headers={"Retry-After": str(int(number_setting("github_reload_interval_s")))},
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


# ── Agent permissions ──────────────────────────────────────────────────


async def resolve_agent_access(
    owners: Any, user_id: str, *, reject_unavailable_subject: bool = False
) -> AgentAccessDecision:
    """Read the account's current Agent permissions, with no session or role fallback.

    An account that cannot sign in (unknown, unverified, suspended, deleted)
    answers as a denial, or a 403 where ``reject_unavailable_subject`` asks
    for one. A store that cannot answer is 503: no verdict, so no admission.
    """
    try:
        decision = AgentAccessDecision.model_validate(await owners.agent_access(user_id))
        if decision.user_id != user_id:
            raise ValueError("Agent access subject mismatch")
        return decision
    except OwnerDirectoryError as exc:
        if exc.status_code == 403 and exc.error_type == "subject_unavailable":
            if reject_unavailable_subject:
                raise HTTPException(status_code=403, detail="This account cannot sign in.") from exc
            return AgentAccessDecision(user_id=user_id, allowed=False, permissions=[])
    except ValueError:
        pass
    get_logger(__name__).warning(
        "agent_access_unavailable", extra={"event": "agent_access_unavailable", "user_id": user_id}
    )
    raise HTTPException(
        status_code=503,
        detail="Cannot confirm Agent access; try again shortly.",
    )


async def require_agent_access(
    owners: Any, user_id: str, permission: str = "agent.use"
) -> AgentAccessDecision:
    """Require an explicit current permission before accessing Agent data."""
    return _permitted(await resolve_agent_access(owners, user_id), permission)


def _permitted(decision: AgentAccessDecision, permission: str) -> AgentAccessDecision:
    """Return ``decision`` if it grants ``permission``; refuse the request otherwise."""
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
#: except for the entry a proxy this deployment trusts wrote into it, so
#: believing it without being told to would let anyone write any address into
#: the audit trail.
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


async def get_current_user(
    request: Request,
    authorization: str | None = Header(None),
    storage: Any = Depends(get_storage),
) -> dict[str, Any]:
    """Verify the account session a request presents.

    Agent permissions are resolved separately on every protected request; see
    :func:`local_session_owner` for the session itself. ``storage`` is the
    store the session is read from; a direct caller passes the request's own.
    """
    return await _local_current_user(request, authorization, storage)


async def verify_admin_access(
    request: Request,
    authorization: str | None = Header(None),
    owners: Any = Depends(get_owner_directory),
    storage: Any = Depends(get_storage),
) -> str:
    """Authenticate an infrastructure token or a current Agent administrator.

    ``ADMIN_TOKEN`` is the operator's recovery door. A browser session needs
    its account's current ``agent.admin`` for reads and writes alike.
    """
    presented = bearer_token(authorization)
    configured = (os.environ.get(ENV_ADMIN_TOKEN) or "").strip()
    # Bytes, not text: ``compare_digest`` raises on a non-ASCII ``str``, and a
    # header value is whatever latin-1 the caller sent. A refusal, not a 500.
    if configured and presented and hmac.compare_digest(presented.encode(), configured.encode()):
        return "admin-token"
    return await _local_admin(request, authorization, owners, storage)


# ── Account sessions ───────────────────────────────────────────────────


#: The account a tab is showing. A request naming one is refused with 409
#: ``account_switched`` once the browser's active account is another, so a tab
#: left open on one account never acts as the next. Streams, which cannot set
#: a header, name it in the query instead.
LOCAL_ACCOUNT_HEADER = "x-agent-account"
LOCAL_ACCOUNT_QUERY = "agent_account"


def pinned_local_account(request: Request) -> str | None:
    """The account the request says it acts as, if it names one."""
    from urllib.parse import parse_qs

    value = request.headers.get(LOCAL_ACCOUNT_HEADER)
    if not value:
        query = parse_qs((request.scope.get("query_string") or b"").decode("latin-1"))
        value = (query.get(LOCAL_ACCOUNT_QUERY) or [""])[0]
    return value.strip() or None


async def local_session_owner(
    request: Request, authorization: str | None, storage: Any = None
) -> SessionOwner | None:
    """The live account session a request presents, read from this database.

    The session cookie, or a bearer of the session's shape; anything else
    finds nothing. The cookie names a browser,
    and the request acts as its active account (or the account it pins, which
    must be the active one). One read per request: the answer is kept on the
    request, never across requests.

    Raises:
        HTTPException: 503 when the session store cannot be read; 409
            ``account_switched`` when the request pins another account.
    """
    from cloud_agent.accounts.session_cookie import presented_local_token

    token = presented_local_token(request.cookies, bearer_token(authorization))
    if token is None:
        return None
    kept = getattr(request.state, "local_session", None)
    if kept is not None and kept[0] == token:
        return kept[1]
    owner = await _read_local_session(request, token, storage)
    request.state.local_session = (token, owner)
    return owner


def _account_switched() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "account_switched",
                "message": "This browser switched to another account. Reload to continue.",
            }
        },
    )


async def _read_local_session(
    request: Request, token: str, storage: Any = None
) -> SessionOwner | None:
    from cloud_agent.accounts.config import LocalAuthNotConfigured
    from cloud_agent.accounts.tokens import browser_hash, looks_like_browser_token, session_hash
    from cloud_agent.domain.accounts import may_sign_in
    from cloud_agent.ports.accounts import SessionOwner

    storage = require_store(storage if storage is not None else get_storage(request))
    try:
        browser = None
        if looks_like_browser_token(token):
            browser = await storage.local_accounts.browser_state(browser_hash(token))
            request.state.local_browser = browser
        if browser is not None:
            owner = None
            pinned = pinned_local_account(request)
            if pinned is not None and pinned != browser.active_account_id:
                raise _account_switched()
            member = browser.member(browser.active_account_id)
            if member is not None and member.session_live:
                owner = SessionOwner(session_id=member.session_id, account=member.account)
        else:
            owner = await storage.local_accounts.session_owner(session_hash(token))
    except HTTPException:
        raise
    except LocalAuthNotConfigured:
        raise AgentConfigurationError() from None
    except Exception:
        get_logger(__name__).warning(
            "agent_local_session_unavailable",
            exc_info=True,
            extra={"event": "agent_local_session_unavailable"},
        )
        raise HTTPException(
            status_code=503, detail="Cannot confirm your session; try again shortly."
        ) from None
    if owner is None or not may_sign_in(owner.account):
        return None
    return owner


async def _local_current_user(
    request: Request, authorization: str | None, storage: Any = None
) -> dict[str, Any]:
    owner = await local_session_owner(request, authorization, storage)
    if owner is not None:
        return {"user_id": owner.account.id, "authenticated": True}
    from cloud_agent.accounts.session_cookie import presented_local_token

    if presented_local_token(request.cookies, bearer_token(authorization)) is None:
        raise HTTPException(status_code=401, detail="Missing authentication token.")
    raise HTTPException(status_code=401, detail="Invalid authentication token.")


async def require_local_admission(request: Request, user_id: str) -> None:
    """Refuse an identity-only route to an account without ``agent.use``."""
    await require_agent_access(get_owner_directory(request), user_id)


async def _local_admin(
    request: Request, authorization: str | None, owners: Any, storage: Any = None
) -> str:
    """Admit a session whose account currently holds ``agent.admin``."""
    user = await _local_current_user(request, authorization, storage)
    await require_agent_access(owners, user["user_id"], "agent.admin")
    return f"admin:{user['user_id']}"


# ── The admin audit trail ──────────────────────────────────────────────


def get_client_ip(request: Request) -> str:
    """Return the best-effort originating client IP for a request.

    The socket peer, unless this deployment says a proxy in front of it
    rewrites ``X-Forwarded-For`` — see ``AGENT_TRUST_PROXY_HEADERS``. Believing
    the header unconditionally would let any caller write any address into the
    audit trail, which is worse than recording the proxy's address honestly.

    Even then only the **rightmost** entry is believed: it is the one the
    nearest proxy wrote, whether that proxy overwrites the header or appends
    to it. Everything to its left arrived from the caller, and an appending
    proxy such as nginx's ``$proxy_add_x_forwarded_for`` passes it through
    verbatim, so the leftmost entry is whatever the caller typed. Repeated
    header lines count as one list in order: HAProxy appends a line of its own
    rather than editing the caller's, and the first line is then all theirs.
    """
    if (setting_env(ENV_TRUST_PROXY_HEADERS) or "").strip().lower() in ("1", "true", "yes"):
        forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
        nearest_hop = forwarded.split(",")[-1].strip()
        if nearest_hop:
            return nearest_hop
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

    Rows go to the ``agent_admin_audit_log`` table, created with the rest of the schema by
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
