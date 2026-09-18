"""The control plane as its own service.

In the gateway this was forty lines inside a much larger ``bootstrap`` that
also built routers, adapters and observability. Here it is the whole process,
and the lifecycle is the same three things it always was: a store, a reaper,
and a publisher.

**The reaper is what makes a vanished sandbox recoverable.** It supersedes
attempts whose lease expired and requeues, fails or cancels the job per the
store's policy. It never dies on an error, because a transient database blip
must not permanently stop reaping — a stopped reaper is invisible until jobs
start hanging.

**The snapshot coordinator** asks an owning workspace broker for short,
quiescent Git captures. It has its own schedule so a slow Docker/Git operation
cannot delay grant renewal or attempt recovery.

**The publisher is the only component holding a git credential**, which is
precisely what keeps the sandbox credential-free. A deployment without one
simply never publishes, rather than failing to start.

All three are background tasks held in a module-level set: a task the loop can
garbage-collect mid-run is a task that stops silently.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from cloud_agent.logging_utils import configure_process_logging, get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from cloud_agent.publish_worker import PublishStorage


from cloud_agent.workers.attempts import reap_expired_attempts
from cloud_agent.workers.snapshots import coordinate_shared_snapshots

logger = get_logger(__name__)

ENV_DATABASE_URL = "AGENT_DATABASE_URL"
ENV_PUBLISH_BASE_BRANCH = "AGENT_PUBLISH_BASE_BRANCH"
ENV_GITHUB_TOKEN = "AGENT_GITHUB_TOKEN"

#: The admin observation reads. Their bodies are time-varying and describe
#: other users' runs, so no intermediary may store one.
ADMIN_READ_PREFIX = "/admin/agent/runs"


def install_admin_no_store(app: FastAPI, *, prefix: str = ADMIN_READ_PREFIX) -> None:
    """Keep admin read responses, refusals included, out of every cache.

    A route dependency can set a header on a response the route returns, but
    FastAPI builds a fresh response for a raised refusal and for its own
    validation errors. These handlers wrap the defaults instead of replacing
    them, and are registered here rather than as middleware: middleware would
    also sit in front of the event streams, which are long-lived by design.
    """
    prefix = prefix.rstrip("/")

    async def with_no_store(request: Request, exc: Exception) -> Response:
        response: Response
        if isinstance(exc, RequestValidationError):
            response = await request_validation_exception_handler(request, exc)
        else:
            response = await http_exception_handler(request, exc)  # type: ignore[arg-type]
        if request.url.path == prefix or request.url.path.startswith(f"{prefix}/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.add_exception_handler(StarletteHTTPException, with_no_store)
    app.add_exception_handler(RequestValidationError, with_no_store)


#: Held so the event loop cannot collect a running background task. Without
#: this the reaper and publisher are eligible for garbage collection the moment
#: nothing else references them, and they stop without a word.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _build_source_control(app: FastAPI) -> None:
    """Attach GitHub App and GitLab OAuth clients, where configured.

    Each is optional and each failure is contained: a partial config disables
    that provider rather than booting with a credential path that cannot be
    secured.
    """
    app.state.app_credentials = None
    app.state.gitlab_oauth = None

    try:
        from cloud_agent.github_app import AppConfig, GitHubAppCredentials

        app_config = AppConfig.from_env(dict(os.environ))
        if app_config is not None:
            app.state.app_credentials = GitHubAppCredentials(app_config)
    except Exception:
        logger.warning("GitHub App config present but unusable", exc_info=True)

    try:
        from cloud_agent.source_control import (
            GitLabOAuthClient,
            GitLabOAuthConfig,
            SourceControlCipher,
        )

        gitlab_config = GitLabOAuthConfig.from_env(dict(os.environ))
        if gitlab_config is not None:
            app.state.gitlab_oauth = GitLabOAuthClient(gitlab_config, cipher=SourceControlCipher())
    except Exception:
        logger.warning("GitLab OAuth config present but unusable", exc_info=True)


def _start_publisher(app: FastAPI, store: PublishStorage) -> None:
    """Start the publish loop, idle if no git credential is configured."""
    from cloud_agent.publish_worker import GitHubCredential, publish_loop

    static_token = os.getenv(ENV_GITHUB_TOKEN, "")

    def credential_provider() -> GitHubCredential | None:
        """Mint the credential for one publish, or None if unconfigured.

        Read per call rather than captured, so rotating the token does not
        need a restart.
        """
        token = os.getenv(ENV_GITHUB_TOKEN, static_token)
        return GitHubCredential(token) if token else None

    task = asyncio.create_task(
        publish_loop(
            store,
            credential_provider=credential_provider,
            app_credentials=app.state.app_credentials,
            base_branch=os.getenv(ENV_PUBLISH_BASE_BRANCH, "dev"),
        )
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    app.state.publish_task = task


@asynccontextmanager
async def lifespan(app: FastAPI, *, background: bool = True) -> AsyncIterator[None]:
    """Open the store, optionally start the reaper and publisher, tear down.

    Args:
        app: The application whose state is being built.
        background: Whether this process owns the reaper and the publisher.
            False for a second listener sharing the database — see the comment
            below for why that is not merely wasteful.

    The database is required. A control plane without one cannot claim, fence
    or record anything, so it refuses to start rather than serving an API that
    fails at every call.
    """
    import asyncpg
    import httpx

    from cloud_agent.api.mcp_relay import close_host, maintain_host_sessions
    from cloud_agent.broker_routing import close_broker_clients
    from cloud_agent.capacity_history import prune_host_capacity_history
    from cloud_agent.gateway import GatewayClient
    from cloud_agent.grants import GrantClient
    from cloud_agent.ledger import collect_usage_snapshots
    from cloud_agent.mcp.catalog import McpCatalog
    from cloud_agent.operational_health import monitor_queue
    from cloud_agent.storage.assembly import Storage

    dsn = os.environ.get(ENV_DATABASE_URL, "")
    if not dsn:
        raise RuntimeError(f"{ENV_DATABASE_URL} must be set")

    pool = await asyncpg.create_pool(dsn)
    storage = Storage(pool)
    # Schema is Alembic's. There is no initialize step to run: the store's
    # no-op one went with the store, and the DDL and backfills it used to
    # carry have been migrations since 0001.
    try:
        from cloud_agent.identity import ENV_ISSUER, gateway_issuer
        from cloud_agent.identity_namespace import ensure_gateway_namespace

        configured = bool(os.environ.get("GATEWAY_BASE_URL") or os.environ.get(ENV_ISSUER))
        issuer = gateway_issuer() if configured else None
        app.state.gateway_issuer = await ensure_gateway_namespace(pool, issuer)
    except Exception:
        await pool.close()
        raise

    app.state.storage = storage
    # One pool, shared by both gateway clients. Each call used to build its own
    # client and pay a connect — and a TLS handshake, where the gateway is
    # behind TLS — and admission is not an occasional call: `require_agent_owner`
    # is a dependency of every owner route, terminal input and resize included.
    # Both clients pass their own deadline per request, so this one carries no
    # default timeout of its own.
    http = httpx.AsyncClient()
    app.state.http = http
    app.state.gateway = GatewayClient(client=http)
    # The sandbox's credential is minted at the gateway now, so the claim
    # path and the renewal/revocation passes share that pool rather than each
    # building their own per call. They take the same seam; until this was
    # wired, the comment here described sharing that was not happening.
    app.state.grants = GrantClient(client=http)
    _build_source_control(app)

    # **The background work belongs to exactly one process.** The reaper
    # renews and revokes grants and supersedes lapsed attempts; the publisher
    # opens pull requests. A second listener on this same database — the MCP
    # relay is one — running its own copies would renew every grant twice per
    # interval, double the load it puts on the gateway, and have two workers
    # racing for the same publish.
    #
    # So this is a parameter of the lifespan rather than a property of it, and
    # `create_mcp_app` says no.
    app.state.mcp_catalog = None
    app.state.mcp_catalog_task = None
    if background:
        app.state.mcp_catalog = McpCatalog(storage.mcp_catalog, http)
        app.state.mcp_catalog_task = asyncio.create_task(app.state.mcp_catalog.run())
        reaper = asyncio.create_task(reap_expired_attempts(storage, grants=app.state.grants))
        snapshot_coordinator = asyncio.create_task(coordinate_shared_snapshots(storage))
        ledger_collector = asyncio.create_task(
            collect_usage_snapshots(storage.ledger, app.state.grants)
        )
        # Host capacity history keeps filling whether or not anybody has the
        # admin page open — that is the whole reason it is stored at all rather
        # than accumulated in the browser. The points are written by the
        # heartbeats that accept them; what runs here is the retention that
        # bounds the table, in its own task, so a slow delete cannot delay a
        # heartbeat or the reaper.
        capacity_history = asyncio.create_task(prune_host_capacity_history(storage.host_capacity))
        _BACKGROUND_TASKS.add(reaper)
        _BACKGROUND_TASKS.add(snapshot_coordinator)
        _BACKGROUND_TASKS.add(ledger_collector)
        _BACKGROUND_TASKS.add(capacity_history)
        reaper.add_done_callback(_BACKGROUND_TASKS.discard)
        snapshot_coordinator.add_done_callback(_BACKGROUND_TASKS.discard)
        ledger_collector.add_done_callback(_BACKGROUND_TASKS.discard)
        capacity_history.add_done_callback(_BACKGROUND_TASKS.discard)
        app.state.reaper_task = reaper
        app.state.snapshot_task = snapshot_coordinator
        app.state.ledger_task = ledger_collector
        app.state.capacity_task = capacity_history
        app.state.queue_monitor_task = asyncio.create_task(monitor_queue(storage.host_pool))
        _BACKGROUND_TASKS.add(app.state.queue_monitor_task)
        app.state.queue_monitor_task.add_done_callback(_BACKGROUND_TASKS.discard)
        _start_publisher(app, storage)
        logger.info("cloud agent control plane started")
    else:
        app.state.reaper_task = None
        app.state.snapshot_task = None
        app.state.ledger_task = None
        app.state.capacity_task = None
        app.state.queue_monitor_task = None
        app.state.publish_task = None
        logger.info("cloud agent relay listener started")

    # Each listener owns upstream sockets in its own process, even when it
    # does not run the control plane's grant reaper or publisher.
    app.state.mcp_maintenance_task = asyncio.create_task(maintain_host_sessions(storage))
    try:
        yield
    finally:
        for task in (
            app.state.mcp_maintenance_task,
            app.state.mcp_catalog_task,
            app.state.reaper_task,
            app.state.snapshot_task,
            app.state.ledger_task,
            app.state.capacity_task,
            app.state.queue_monitor_task,
            app.state.publish_task,
        ):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await close_host()
        # Close what was opened, innermost first: the per-broker pools, the
        # GitHub App's own client, then the shared gateway pool.
        await close_broker_clients()
        credentials = getattr(app.state, "app_credentials", None)
        if credentials is not None:
            await credentials.aclose()
        await http.aclose()
        await pool.close()
        logger.info("cloud agent control plane stopped")


def create_app() -> FastAPI:
    """Build the control plane application."""
    configure_process_logging()

    from cloud_agent.api import (
        admin_conversations,
        admin_hosts,
        admin_llm_requests,
        admin_runs,
        admin_usage,
        hosts,
        mcp_connections,
        mcp_relay,
        routes,
        service_jobs,
        session,
        skills,
        uploads,
        usage,
    )

    app = FastAPI(
        title="Cloud Agent",
        description="Cloud coding agents on sandboxed hosts",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(routes.router)
    app.include_router(usage.router)
    # Registered before the relay, and never on the relay's own listener. The
    # order matters: the relay's ``/{server_name}`` catch-all would otherwise
    # match a connection-management path and answer it as an MCP request. The
    # listener separation matters more: adding a connection or approving a call
    # is owner-facing work, and a sandbox must not be able to reach it at all.
    app.include_router(skills.router)
    app.include_router(uploads.router)
    app.include_router(mcp_connections.router)
    app.include_router(mcp_relay.router)
    app.include_router(hosts.router)
    app.include_router(admin_hosts.router)
    app.include_router(admin_conversations.router)
    app.include_router(admin_runs.router)
    app.include_router(admin_usage.router)
    # Per-request LLM detail reads a second service and stays in its own module
    # for the same reason it is a separate page load: the default run detail
    # must not acquire a gateway I/O path.
    app.include_router(admin_llm_requests.router)
    install_admin_no_store(app)
    # The gateway alert relay's way in: machine-to-machine on-call jobs, pinned
    # to one configured service account. Its own module because its caller is a
    # service holding a dispatch token, not a browser holding a session.
    app.include_router(service_jobs.router)
    # The web app's way in. Mounted alongside the others rather than inside
    # ``routes.py`` because it authenticates nothing on entry — it is the route
    # that *creates* the credential every other route requires.
    app.include_router(session.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness only — deliberately not readiness.

        A readiness probe here would have to decide what a degraded control
        plane is (no gateway? no git credential?), and every answer to that
        belongs to whoever operates it.
        """
        return {"status": "ok"}

    return app


def create_mcp_app() -> FastAPI:
    """Build a listener that serves the MCP relay and nothing else.

    **A sandbox is the least trusted thing in this system, and this listener is
    one of only two fixed agent-phase endpoints it may reach.** Pointed at the
    main application, its tunnel also reaches jobs, sessions, host registration
    and admin — every route the control plane has. Those refuse an
    ``mcp``-scoped token, so
    nothing was exploitable through them today; but the entire trusted control
    plane sat one authorization bug away from code nobody in this repository
    wrote, and that is a reachability decision rather than an authorization
    one.

    So the separation is structural. This application does not *contain* the
    other routes, which is a stronger statement than any of them refusing:
    there is no path prefix to get wrong in a proxy, no rule to leave off a new
    router, and a route added to ``create_app`` tomorrow is not silently
    exposed here.

    Same process image, same database, same lifespan — it is a second
    listener, not a second service. The cost is one more port; the alternative
    was a path-restricted proxy, which is the same policy written where it can
    be misconfigured and where nothing tests it.
    """
    configure_process_logging()

    from cloud_agent.api import mcp_relay

    app = FastAPI(
        title="Cloud Agent — MCP relay",
        description="The only surface a sandbox may reach",
        version="0.1.0",
        # No reaper and no publisher: they belong to the control-plane process,
        # and a second copy would renew every grant twice and race the same
        # publish. This listener still maintains its own upstream MCP sockets.
        lifespan=lambda app: lifespan(app, background=False),
    )
    app.include_router(mcp_relay.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness, so the tunnel's health check has something to ask."""
        return {"status": "ok"}

    return app
