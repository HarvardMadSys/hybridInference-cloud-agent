"""The control plane's internal assembly: resources, providers, lifecycle.

`app.py` keeps the two listeners and what they are made of; this module builds
what they need. It ran inline in `app.py` before, in the same order, setting the
same ``app.state`` names, and it still does — the move is about where the code
lives, not about when it runs. The database is required: a control plane without
one cannot claim, fence or record anything, so it refuses to start rather than
serving an API that fails at every call.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent.workers.attempts import reap_expired_attempts
from cloud_agent.workers.snapshots import coordinate_shared_snapshots

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

    from cloud_agent.publish_worker import PublishStorage

#: The moved lifespan keeps logging under the module it came from: the
#: deployment's log selection is on ``cloud_agent.app``.
logger = get_logger("cloud_agent.app")


ENV_DATABASE_URL = "AGENT_DATABASE_URL"


ENV_PUBLISH_BASE_BRANCH = "AGENT_PUBLISH_BASE_BRANCH"


ENV_GITHUB_TOKEN = "AGENT_GITHUB_TOKEN"


#: Held so the event loop cannot collect a running background task. Without
#: this the reaper and publisher are eligible for garbage collection the moment
#: nothing else references them, and they stop without a word.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _build_source_control(app: FastAPI) -> None:
    """Attach the source-control runtime: GitHub App and GitLab OAuth.

    Each is optional and each failure is contained: a partial config disables
    that provider rather than booting with a credential path that cannot be
    secured. Disabled is not the same as absent, though, and a GitHub App that
    failed to load is loaded again (issue #47) — so what is attached is the
    runtime, never a client captured at startup.
    """
    from cloud_agent.source_control_runtime import SourceControlRuntime

    app.state.source_control = SourceControlRuntime.from_env(dict(os.environ))


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
            github_app=app.state.source_control.github,
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
    from cloud_agent.application.profiling_control import ProfilingControlService
    from cloud_agent.broker_routing import close_broker_clients
    from cloud_agent.capacity_history import prune_host_capacity_history
    from cloud_agent.gateway import GatewayClient
    from cloud_agent.grants import GrantClient
    from cloud_agent.ledger import collect_usage_snapshots
    from cloud_agent.mcp.catalog import McpCatalog
    from cloud_agent.operational_health import monitor_queue
    from cloud_agent.profiling_control_config import profiling_control_deployment
    from cloud_agent.storage.assembly import Storage
    from cloud_agent.workers.profiling_control import ProfilingPolicyWorker

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
    app.state.compute_commands = None
    app.state.compute_resource_task = None
    if background:
        from cloud_agent.bootstrap.compute import build_compute_runtime

        app.state.compute_commands, compute_worker = build_compute_runtime(
            repository=storage.compute_resources,
            host_pool=storage.host_pool,
        )
        app.state.compute_resource_task = asyncio.create_task(compute_worker.run())
        _BACKGROUND_TASKS.add(app.state.compute_resource_task)
        app.state.compute_resource_task.add_done_callback(_BACKGROUND_TASKS.discard)
        app.state.mcp_catalog = McpCatalog(storage.mcp_catalog, http)
        app.state.mcp_catalog_task = asyncio.create_task(app.state.mcp_catalog.run())
        reaper = asyncio.create_task(
            reap_expired_attempts(
                storage,
                grants=app.state.grants,
                github_app=app.state.source_control.github,
                gateway=app.state.gateway,
            )
        )
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
        # The profiling policy loop runs in every control-plane process, not
        # only the one that owns the reaper: each process roots its own file and
        # terminal requests, so each one has to know what it is allowed to
        # collect. It polls the control row, which is why an idle deployment now
        # has a small, stated background cost instead of claiming zero I/O.
        profiling_service = ProfilingControlService(
            control=storage.profiling_control,
            deployment=profiling_control_deployment(),
            host_pool=storage.host_pool,
        )
        profiling_worker = ProfilingPolicyWorker(service=profiling_service)
        app.state.profiling_control_service = profiling_service
        app.state.profiling_policy_worker = profiling_worker
        app.state.profiling_policy_task = asyncio.create_task(profiling_worker.run())
        _BACKGROUND_TASKS.add(app.state.profiling_policy_task)
        app.state.profiling_policy_task.add_done_callback(_BACKGROUND_TASKS.discard)
        app.state.queue_monitor_task = asyncio.create_task(monitor_queue(storage.host_pool))
        _BACKGROUND_TASKS.add(app.state.queue_monitor_task)
        app.state.queue_monitor_task.add_done_callback(_BACKGROUND_TASKS.discard)
        _start_publisher(app, storage)
        logger.info("cloud agent control plane started")
    else:
        # The relay listener roots no file or terminal request, so it has no
        # configuration to apply and nothing to collect.
        app.state.profiling_control_service = None
        app.state.profiling_policy_worker = None
        app.state.profiling_policy_task = None
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
    # Created per process, but worker tasks remain lazy until a selected
    # file/terminal record is actually offered.
    from cloud_agent.measurement_writer import MeasurementWriter, reset_writer

    local_measurement_writer = MeasurementWriter()
    reset_writer(local_measurement_writer)
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            counts = await local_measurement_writer.shutdown()
            if any(counts.as_dict().get(name) for name in counts.as_dict() if name != "reasons"):
                logger.info(
                    "operation_measurement_writer_summary",
                    extra={
                        "event": "operation_measurement_writer_summary",
                        **counts.as_dict(),
                    },
                )
        reset_writer(None)
        for task in (
            app.state.mcp_maintenance_task,
            app.state.mcp_catalog_task,
            app.state.reaper_task,
            app.state.snapshot_task,
            app.state.ledger_task,
            app.state.capacity_task,
            app.state.queue_monitor_task,
            app.state.publish_task,
            app.state.profiling_policy_task,
            app.state.compute_resource_task,
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
        await app.state.source_control.aclose()
        await http.aclose()
        await pool.close()
        logger.info("cloud agent control plane stopped")
