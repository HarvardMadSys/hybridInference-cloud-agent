"""Internal sandbox manager for live agent workspaces.

The control plane authenticates workspace owners and forwards only a validated
job id. This service is deliberately separate from it: it can reach the host
Docker daemon, while the public-facing control plane cannot. Commands, Git
inspection, terminals, and Agent CLI transports all execute inside the exact
retained chat container; no host workspace is mounted into either service.

This module assembles the app: it builds the dependencies, owns their startup
and shutdown, and includes the routers of :mod:`cloud_agent_host.manager_api`.
"""

from __future__ import annotations

import argparse
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI

from cloud_agent_host import sandbox_manager_profiling_policy
from cloud_agent_host.chat_sandbox import ChatSandboxManager
from cloud_agent_host.environment_preparation import EnvironmentPreparation
from cloud_agent_host.group_archive_policy import (
    ControllerArchivePolicy,
    GroupArchivePolicy,
    NullArchivePolicy,
    ServiceArchivePolicy,
)
from cloud_agent_host.logging_utils import configure_process_logging, get_logger
from cloud_agent_host.manager_api.archives import ArchiveRoutes
from cloud_agent_host.manager_api.calls import _CommandSlots, _EndpointLifecycleCalls
from cloud_agent_host.manager_api.context import (
    ManagerContext,
    ManagerSettings,
    SupervisorStart,
    authenticator,
)
from cloud_agent_host.manager_api.errors import install_error_handlers
from cloud_agent_host.manager_api.routing import bind
from cloud_agent_host.manager_api.settings import SettingsRoutes
from cloud_agent_host.manager_api.shared_sessions import SharedSessionRoutes
from cloud_agent_host.manager_api.snapshots import SnapshotRoutes
from cloud_agent_host.manager_api.terminal_manager import (
    DEFAULT_MAX_TERMINAL_SESSIONS,
    DEFAULT_TERMINAL_HARD_TTL_S,
    DEFAULT_TERMINAL_IDLE_TTL_S,
    _TerminalManager,
)
from cloud_agent_host.manager_api.terminals import TerminalRoutes
from cloud_agent_host.manager_api.turns import TurnRoutes
from cloud_agent_host.manager_api.workspaces import WorkspaceRoutes
from cloud_agent_host.sandbox import ContainerBackend, build_backend_from_env
from cloud_agent_host.sandbox_archive_runtime import SandboxArchiveController
from cloud_agent_host.sandbox_archives import safe_archive_error_type
from cloud_agent_host.sandbox_checkpoints import (
    DEFAULT_CONTAINERD_CONTENT_DIR,
    SandboxCheckpointCapture,
    SandboxCheckpointStore,
)
from cloud_agent_host.sandbox_deletion_backup import (
    DEFAULT_ARCHIVE_STATE_DIR,
    SandboxDeletionBackupService,
)
from cloud_agent_host.sandbox_incident_routes import install_incident_routes, sandbox_incidents_for
from cloud_agent_host.sandbox_initial_uploads import INITIAL_UPLOAD_DIRNAME
from cloud_agent_host.sandbox_resources import SandboxResourceCollector
from cloud_agent_host.session_lifecycle import DEFAULT_CHAT_IDLE_TIMEOUT_S
from cloud_agent_host.shared_session_supervisor import SharedChatSessionSupervisor
from cloud_agent_host.terminal_session import _TerminalLimits
from cloud_agent_shared.host_settings import (
    HOST_SETTINGS_BY_KEY,
    SANDBOX_MANAGER_SETTING_KEYS,
    HostSetting,
    default_values,
)
from cloud_agent_shared.sandbox_manager_compat import manager_env

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager

    from cloud_agent_host.sandbox import SandboxBackend
    from cloud_agent_host.sandbox_archives import SandboxArchiveService

logger = get_logger(__name__)

_DEFAULT_SETTINGS = default_values(SANDBOX_MANAGER_SETTING_KEYS)
#: The settings whose local values come from create_app's own arguments.
_ARGUMENT_SETTINGS = frozenset(
    {"terminal_idle_ttl_s", "terminal_hard_ttl_s", "terminal_max_sessions", "chat_idle_timeout_s"}
)
#: Owner commands and Git inspection run on a pool of their own, at most this
#: many at once on one sandbox manager and this many for one workspace. One
#: past either limit is refused at once: queued, it would outlast its caller's
#: timeout, and on the loop's shared pool it would hold up every turn's
#: lifecycle calls.
MAX_WORKSPACE_COMMANDS = 16
MAX_WORKSPACE_COMMANDS_PER_WORKSPACE = 4


def _chat_idle_timeout_from_env() -> float:
    """Read the one canonical chat-group inactivity clock."""
    raw = os.environ.get("AGENT_CHAT_IDLE_TIMEOUT_S")
    return float(raw) if raw else DEFAULT_CHAT_IDLE_TIMEOUT_S


def _accepted_local_value(definition: HostSetting, raw: str | None) -> int | None:
    """A legacy environment value ``definition`` accepts, or None to keep the default."""
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if definition.accepts(value) else None


def _archive_parts(
    retained_manager: ChatSandboxManager | None,
    archive_service: SandboxArchiveService | None,
    archive_controller: SandboxArchiveController | None,
) -> tuple[SandboxArchiveController | None, SandboxCheckpointStore | None]:
    """The archive controller this app uses, and the checkpoint store it built for it."""
    if archive_service is not None and archive_controller is not None:
        raise ValueError("sandbox archive service and controller are mutually exclusive")
    if archive_service is not None or archive_controller is not None or retained_manager is None:
        return archive_controller, None
    # Checkpoint receipts and their temporary exports share the durable,
    # Sandbox Manager-only archive state directory; no sandbox mounts it.
    state_dir = Path(os.environ.get("AGENT_SANDBOX_ARCHIVE_STATE_DIR") or DEFAULT_ARCHIVE_STATE_DIR)
    checkpoint_store = SandboxCheckpointStore(state_dir / "checkpoints")
    checkpoint_capture = SandboxCheckpointCapture(
        docker_binary=retained_manager.backend.docker_binary,
        spool_dir=state_dir / "checkpoint-spool",
        store=checkpoint_store,
        # A read-only mount of containerd's content store lets a
        # checkpoint copy only its new layer; without it, the capture
        # streams the whole image through `docker image save`.
        content_dir=Path(
            os.environ.get("AGENT_CONTAINERD_CONTENT_DIR") or DEFAULT_CONTAINERD_CONTENT_DIR
        ),
    )
    controller = SandboxArchiveController(
        retained_manager,
        namespace=retained_manager.backend.sandbox_namespace,
        checkpoint_capture=checkpoint_capture,
        initial_upload_dir=state_dir / INITIAL_UPLOAD_DIRNAME,
    )
    return controller, checkpoint_store


def _shared_supervisor(
    injected: SharedChatSessionSupervisor | None,
    retained_manager: ChatSandboxManager | None,
    *,
    idle_timeout_s: float,
    archive_service: SandboxArchiveService | None,
    archive_controller: SandboxArchiveController | None,
) -> SharedChatSessionSupervisor:
    """The supervisor this app serves, holding the app's archive policy."""
    archive_policy: GroupArchivePolicy = (
        ControllerArchivePolicy(archive_controller)
        if archive_controller is not None
        else ServiceArchivePolicy(archive_service)
        if archive_service is not None
        else NullArchivePolicy()
    )
    if injected is None:
        if retained_manager is None:
            raise ValueError("shared-live chat sandboxes require the container or kata backend")
        return SharedChatSessionSupervisor(
            retained_manager,
            idle_timeout_s=idle_timeout_s,
            archive_policy=archive_policy,
        )
    if (archive_service or archive_controller) is not None and (
        injected.archive_policy != archive_policy
    ):
        # An injected supervisor must already hold this app's archive: clean
        # stops would otherwise never reach the archive it starts and closes.
        raise ValueError("an injected shared supervisor must hold the sandbox archive policy")
    return injected


def _lifespan(
    sandbox: SandboxBackend, context: ManagerContext
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Start the app's dependencies in order, and stop them in reverse."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Docker owns every chat workspace and provider state. The sandbox manager keeps
        # only an in-memory index reconstructed from container labels/PID files.
        sandbox.preflight()
        expected_docker_root = (os.environ.get("AGENT_RETAINED_DOCKER_DATA_ROOT") or "").strip()
        if expected_docker_root:
            if context.retained_manager is None:
                raise ValueError("retained DockerRootDir validation requires a container backend")
            app.state.retained_docker_data_root = context.retained_manager.require_docker_data_root(
                expected_docker_root
            )
        archive_boundary = context.archive_boundary
        archive_boundary_started = False
        terminals_started = False
        resource_collector: SandboxResourceCollector | None = None
        try:
            # A replacement sandbox manager cannot reattach a previous one's
            # Docker execs. Reconciliation stops each running group it
            # discovers for the first time, so those resident CLIs and terminal
            # PTYs end with their container; nothing reaps them one by one.
            # Keep startup inside the cleanup guard: a failed Docker
            # reconciliation must not leak the terminal manager's background
            # thread from a failed ASGI lifespan.
            if archive_boundary is not None:
                archive_boundary.start()
                archive_boundary_started = True
            # A dynamic controller begins at UNKNOWN. Reconciliation can stop
            # and release retained containers, so wait for the runner to
            # deliver an explicit enabled or disabled database policy first.
            if context.archive_controller is None:
                context.supervisor_start()
            context.terminals.start()
            terminals_started = True
            resource_collector = SandboxResourceCollector(
                on_reading=context.incidents.observe_reading
            )
            resource_collector.start()
            context.incidents.start()
            app.state.sandbox_resource_collector = resource_collector
            yield
        finally:
            await _shut_down(
                app,
                context,
                archive_boundary_started=archive_boundary_started,
                terminals_started=terminals_started,
                resource_collector=resource_collector,
            )

    return lifespan


async def _shut_down(
    app: FastAPI,
    context: ManagerContext,
    *,
    archive_boundary_started: bool,
    terminals_started: bool,
    resource_collector: SandboxResourceCollector | None,
) -> None:
    """Stop what the lifespan started, each step attempted whatever the last one did."""
    cleanup_failures: list[tuple[str, str]] = []

    def cleanup(name: str, function: Callable[[], None]) -> None:
        try:
            function()
        except Exception as exc:
            error_type = safe_archive_error_type(exc)
            logger.error(
                "sandbox manager shutdown cleanup failed",
                extra={
                    "event": "sandbox_manager_cleanup_failed",
                    "component": name,
                    "error_type": error_type,
                },
            )
            # Do not retain an archive/provider exception for the final
            # ASGI lifespan failure: raising from it would make the raw
            # message and its complete cause chain visible again.
            cleanup_failures.append((name, error_type))

    # A canceled request still owns its executor call. Drain every
    # admitted mutation before closing supervisors or archive clients.
    await context.endpoint_calls.seal_and_wait()
    cleanup("command_pool", lambda: context.command_pool.shutdown(wait=False))
    app.state.sandbox_resource_collector = None
    if resource_collector is not None:
        cleanup("resource_collector", resource_collector.close)
    cleanup("incidents", context.incidents.close)
    if terminals_started:
        cleanup("terminals", context.terminals.close)
    if context.supervisor_start.ready:
        supervisor = context.supervisor
        archive_boundary_configured = (
            context.archive_service is not None or context.archive_controller is not None
        )
        if archive_boundary_configured or supervisor.has_pending_archive_work():
            cleanup("shared_supervisor_quiesce", supervisor.quiesce)
            cleanup(
                "shared_sandbox_stop_all",
                lambda: supervisor.stop_all_for_shutdown(
                    include_running=archive_boundary_configured
                ),
            )
        cleanup("shared_supervisor", supervisor.close)
    if archive_boundary_started:
        assert context.archive_boundary is not None
        cleanup("sandbox_archive_service", context.archive_boundary.close)
    if cleanup_failures:
        component, error_type = cleanup_failures[0]
        safe_cause = RuntimeError(f"{component} cleanup failed ({error_type})")
        raise RuntimeError("sandbox manager shutdown cleanup failed") from safe_cause


def create_app(
    *,
    backend: SandboxBackend | None = None,
    token: str | None = None,
    terminal_idle_ttl_s: float | None = None,
    terminal_hard_ttl_s: float | None = None,
    terminal_max_sessions: int | None = None,
    shared_session_supervisor: SharedChatSessionSupervisor | None = None,
    sandbox_archive_service: SandboxArchiveService | None = None,
    sandbox_archive_controller: SandboxArchiveController | None = None,
    session_idle_timeout_s: float | None = None,
) -> FastAPI:
    """Create the internal sandbox manager app with injectable dependencies for tests."""
    sandbox = backend or build_backend_from_env(keep_network_helper=True)
    environment_preparation = EnvironmentPreparation()
    expected_token = token if token is not None else manager_env("AGENT_SANDBOX_MANAGER_TOKEN", "")
    idle_ttl_s = (
        terminal_idle_ttl_s
        if terminal_idle_ttl_s is not None
        else float(os.environ.get("AGENT_TERMINAL_IDLE_TTL_SECONDS", DEFAULT_TERMINAL_IDLE_TTL_S))
    )
    hard_ttl_s = (
        terminal_hard_ttl_s
        if terminal_hard_ttl_s is not None
        else float(os.environ.get("AGENT_TERMINAL_HARD_TTL_SECONDS", DEFAULT_TERMINAL_HARD_TTL_S))
    )
    max_sessions = (
        terminal_max_sessions
        if terminal_max_sessions is not None
        else int(os.environ.get("AGENT_TERMINAL_MAX_SESSIONS", DEFAULT_MAX_TERMINAL_SESSIONS))
    )
    if idle_ttl_s <= 0 or hard_ttl_s <= 0:
        raise ValueError("terminal TTL values must be positive")
    if not 1 <= max_sessions <= 4096:
        raise ValueError("terminal global session limit must be between 1 and 4096")
    chat_idle_timeout_s = (
        session_idle_timeout_s
        if session_idle_timeout_s is not None
        else _chat_idle_timeout_from_env()
    )
    if chat_idle_timeout_s <= 0:
        raise ValueError("chat sandbox idle timeout must be positive")
    retained_manager = (
        ChatSandboxManager(sandbox) if isinstance(sandbox, ContainerBackend) else None
    )
    archive_controller, checkpoint_store = _archive_parts(
        retained_manager, sandbox_archive_service, sandbox_archive_controller
    )
    shared_supervisor = _shared_supervisor(
        shared_session_supervisor,
        retained_manager,
        idle_timeout_s=chat_idle_timeout_s,
        archive_service=sandbox_archive_service,
        archive_controller=archive_controller,
    )
    terminals = _TerminalManager(
        idle_ttl_s=idle_ttl_s,
        hard_ttl_s=hard_ttl_s,
        max_sessions=max_sessions,
    )
    # Legacy environment values are bootstrap observations only. The first
    # authenticated settings push replaces all of them with admin defaults.
    local_settings: dict[str, float] = {
        **_DEFAULT_SETTINGS,
        "terminal_idle_ttl_s": idle_ttl_s,
        "terminal_hard_ttl_s": hard_ttl_s,
        "terminal_max_sessions": max_sessions,
        "chat_idle_timeout_s": chat_idle_timeout_s,
    }
    for key in SANDBOX_MANAGER_SETTING_KEYS - _ARGUMENT_SETTINGS:
        definition = HOST_SETTINGS_BY_KEY[key]
        value = _accepted_local_value(definition, os.environ.get(definition.env))
        if value is not None:
            local_settings[key] = value
    context = ManagerContext(
        supervisor=shared_supervisor,
        supervisor_start=SupervisorStart(shared_supervisor),
        archive_service=sandbox_archive_service,
        archive_controller=archive_controller,
        retained_manager=retained_manager,
        terminals=terminals,
        endpoint_calls=_EndpointLifecycleCalls(),
        command_pool=ThreadPoolExecutor(
            max_workers=MAX_WORKSPACE_COMMANDS, thread_name_prefix="workspace-command"
        ),
        command_slots=_CommandSlots(
            total=MAX_WORKSPACE_COMMANDS, per_workspace=MAX_WORKSPACE_COMMANDS_PER_WORKSPACE
        ),
        environment_preparation=environment_preparation,
        settings=ManagerSettings(local_settings),
        authenticate=authenticator(expected_token),
        incidents=sandbox_incidents_for(lambda: shared_supervisor, retained_manager),
    )
    # No schema or docs routes: they are served outside the routes' auth
    # dependency, and nothing reads them.
    app = FastAPI(
        title="Agent sandbox manager",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan(sandbox, context),
    )
    install_error_handlers(app)
    app.state.terminal_manager = terminals
    app.state.sandbox_checkpoint_store = checkpoint_store
    app.state.sandbox_deletion_archive_service = SandboxDeletionBackupService(shared_supervisor)
    app.state.sandbox_resource_collector = None
    # Until a policy arrives, this sandbox manager holds none, which reads as "not
    # collecting". That is what stops a sandbox manager that started before its runner
    # from briefly measuring for an experiment nobody told it about.
    sandbox_manager_profiling_policy.install_policy_provider()
    applied_settings = context.settings.applied
    terminals.configure(
        idle_ttl_s=idle_ttl_s,
        hard_ttl_s=hard_ttl_s,
        max_sessions=max_sessions,
        max_sessions_per_workspace=int(applied_settings["terminal_max_sessions_per_workspace"]),
        limits=_TerminalLimits.from_settings(applied_settings),
    )
    install_incident_routes(app, context.incidents, context.authenticate)
    for routes in (
        SettingsRoutes(context, app.state),
        SharedSessionRoutes(context, app.state),
        SnapshotRoutes(context),
        ArchiveRoutes(context, app.state),
        TurnRoutes(context),
        WorkspaceRoutes(context),
        TerminalRoutes(context),
    ):
        bind(app.router, routes, authenticate=context.authenticate)
    return app


def main(argv: list[str] | None = None) -> int:
    """Run the sandbox manager on the private Compose network."""
    # This process had no logging configuration at all: uvicorn sets up its own
    # loggers and declares no `root`, so every `logger.info(...)` here — the
    # terminal and workspace events among them — reached no handler, and
    # anything at WARNING or above came out through `logging.lastResort` as a
    # bare message. The runner's `main()` has always done this; the sandbox manager was
    # simply missed.
    configure_process_logging()
    parser = argparse.ArgumentParser(description="Serve durable agent workspaces.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8092)
    args = parser.parse_args(argv)
    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["create_app"]
