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

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, PlainTextResponse

from cloud_agent.logging_utils import configure_process_logging, get_logger

logger = get_logger(__name__)


#: The admin observation reads. Their bodies are time-varying and describe
#: other users' runs, so no intermediary may store one.
ADMIN_READ_PREFIXES = (
    "/admin/agent/audit-log",
    "/admin/agent/runs",
    "/admin/agent/steps",
    "/admin/agent/measurements",
    "/admin/agent/measurement-writer",
    "/admin/agent/profiling-runs",
    "/admin/agent/settings",
)


def install_admin_no_store(
    app: FastAPI, *, prefixes: tuple[str, ...] = ADMIN_READ_PREFIXES
) -> None:
    """Keep admin read responses, refusals included, out of every cache.

    A route dependency can set a header on a response the route returns, but
    FastAPI builds a fresh response for a raised refusal and for its own
    validation errors. These handlers wrap the defaults instead of replacing
    them, and are registered here rather than as middleware: middleware would
    also sit in front of the event streams, which are long-lived by design.
    """
    normalized = tuple(prefix.rstrip("/") for prefix in prefixes)

    async def with_no_store(request: Request, exc: Exception) -> Response:
        response: Response
        if isinstance(exc, RequestValidationError):
            if request.url.path.rstrip("/") == "/admin/agent/settings":
                # Validation errors normally echo invalid input. Settings can
                # contain credentials, including in a malformed request body.
                response = JSONResponse(
                    status_code=422,
                    content={
                        "detail": [
                            {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
                            for error in exc.errors()
                        ]
                    },
                )
            else:
                response = await request_validation_exception_handler(request, exc)
        else:
            response = await http_exception_handler(request, exc)  # type: ignore[arg-type]
        if any(
            request.url.path == prefix or request.url.path.startswith(f"{prefix}/")
            for prefix in normalized
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    async def unexpected_with_no_store(request: Request, exc: Exception) -> Response:
        protected = any(
            request.url.path == prefix or request.url.path.startswith(f"{prefix}/")
            for prefix in normalized
        )
        logger.error(
            "unhandled_request_error",
            exc_info=exc,
            extra={"event": "unhandled_request_error", "path": request.url.path},
        )
        return PlainTextResponse(
            "Internal Server Error",
            status_code=500,
            headers={"Cache-Control": "no-store"} if protected else None,
        )

    app.add_exception_handler(StarletteHTTPException, with_no_store)
    app.add_exception_handler(RequestValidationError, with_no_store)
    app.add_exception_handler(Exception, unexpected_with_no_store)


from cloud_agent.bootstrap.lifespan import lifespan


def create_app() -> FastAPI:
    """Build the control plane application."""
    configure_process_logging()

    from cloud_agent.api import (
        admin_audit,
        admin_compute,
        admin_conversations,
        admin_hosts,
        admin_llm_requests,
        admin_measurements,
        admin_profiling_control,
        admin_runs,
        admin_sandboxes,
        admin_settings,
        admin_steps,
        admin_usage,
        compute_registration,
        host_profiling,
        hosts,
        mcp_connections,
        routes,
        service_jobs,
        session,
        site_config,
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
    # Owner-facing, and never on the relay's own listener: adding a connection
    # or approving a call is not sandbox work. The relay itself is the reverse
    # — only ``create_mcp_app`` serves it. This application sits behind the
    # public edge, so a sandbox's ``mcp`` token carried out and replayed there
    # must find no tool route at all, not one that merely refuses it.
    app.include_router(skills.router)
    app.include_router(uploads.router)
    app.include_router(mcp_connections.router)
    app.include_router(hosts.router)
    # Host policy exchange. A sibling of the host routes rather than part of
    # them: it is the profiling feature's own channel, it shares the same
    # credential, and it should be removable without editing the host API.
    app.include_router(host_profiling.router)
    app.include_router(admin_hosts.router)
    app.include_router(admin_compute.router)
    # A new compute node reports its SSH host keys here with a one-time token,
    # before the control plane connects to it. Public by necessity: the node
    # has no other credential yet.
    app.include_router(compute_registration.router)
    app.include_router(admin_conversations.router)
    app.include_router(admin_runs.router)
    app.include_router(admin_sandboxes.router)
    app.include_router(admin_settings.router)
    app.include_router(admin_usage.router)
    app.include_router(admin_measurements.router)
    # Start/Stop for the same feature, in its own module so the write path's
    # transaction and audit imports do not load on the read-only page's route
    # table. Same guard, same prefix, same no-store: it is one surface.
    app.include_router(admin_profiling_control.router)
    # Per-request LLM detail reads a second service and stays in its own module
    # for the same reason it is a separate page load: the default run detail
    # must not acquire a gateway I/O path.
    app.include_router(admin_llm_requests.router)
    # Steps read the same gateway detail across a page of turns, so they sit
    # beside it rather than in the runs list's module.
    app.include_router(admin_steps.router)
    # Read-only, and its own module: the audit trail is written by every admin
    # handler and read by this one page.
    app.include_router(admin_audit.router)
    install_admin_no_store(app)
    # The gateway alert relay's way in: machine-to-machine on-call jobs, pinned
    # to one configured service account. Its own module because its caller is a
    # service holding a dispatch token, not a browser holding a session.
    app.include_router(service_jobs.router)
    # The web app's way in. Mounted alongside the others rather than inside
    # ``routes.py`` because it authenticates nothing on entry — it is the route
    # that *creates* the credential every other route requires.
    app.include_router(session.router)
    app.include_router(site_config.router)

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
    main application, its tunnel would reach jobs, sessions, host registration
    and admin — every route the control plane has. Those refuse an
    ``mcp``-scoped token, so nothing was exploitable through them; but the
    entire trusted control plane sat one authorization bug away from code
    nobody in this repository wrote, and that is a reachability decision rather
    than an authorization one.

    So the separation is structural. This application does not *contain* the
    other routes, which is a stronger statement than any of them refusing:
    there is no path prefix to get wrong in a proxy, no rule to leave off a new
    router, and a route added to ``create_app`` tomorrow is not silently
    exposed here.

    **It runs the other way too: this is the only application that serves the
    relay.** ``create_app`` sits behind the public edge, where a tool route
    would answer an ``mcp`` token carried out of a sandbox and replayed from
    anywhere. Keeping tool calls here narrows replay to hosts that already
    reach a relay tunnel. It does not bind a token to one sandbox, so the
    relay's scope, approval and fence checks still carry that weight.

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
