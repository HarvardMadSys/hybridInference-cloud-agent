"""Lifespan startup and shutdown release the pool even when a step fails.

After ``create_pool`` returns, every later failure — a startup stage, a request
error, or a background task that already finished with an exception — must
still close the pool and the shared HTTP client. A cleanup failure is reported,
but it must not replace the error that started the teardown.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from cloud_agent.app import lifespan
from cloud_agent.bootstrap import lifespan as lifespan_module
from cloud_agent.measurement_writer import MeasurementWriter, reset_writer
from cloud_agent.settings import reset_settings
from cloud_agent.source_control_runtime import SourceControlRuntime

_EXPECTED_SHUTDOWN_TASKS = (
    "settings_task",
    "mcp_maintenance_task",
    "mcp_catalog_task",
    "reaper_task",
    "snapshot_task",
    "sandbox_deletion_task",
    "ledger_task",
    "capacity_task",
    "agent_memory_task",
    "queue_monitor_task",
    "publish_task",
    "pr_archive_task",
    "profiling_policy_task",
    "compute_resource_task",
    "notification_listener_task",
)

_BEFORE_SETTINGS_TASK = frozenset(
    {
        "storage",
        "initialize_settings",
        "ensure_gateway_namespace",
        "synchronize_settings",
    }
)

_BEFORE_SOURCE = _BEFORE_SETTINGS_TASK | frozenset(
    {
        "async_client",
        "gateway_client",
        "grant_client",
        "source_control",
    }
)

_QUIET_ENV = (
    "GATEWAY_BASE_URL",
    "GATEWAY_ISSUER",
    "GATEWAY_GRANT_DISPATCH_TOKEN",
    "AGENT_GITHUB_TOKEN",
    "AGENT_GITHUB_APP_ID",
    "AGENT_GITHUB_APP_PRIVATE_KEY",
    "AGENT_GITHUB_APP_PRIVATE_KEY_PATH",
    "AGENT_GITHUB_APP_CLIENT_ID",
    "AGENT_GITHUB_APP_CLIENT_SECRET",
    "AGENT_GITHUB_API",
    "AGENT_GITLAB_OAUTH_CLIENT_ID",
    "AGENT_GITLAB_OAUTH_CLIENT_SECRET",
    "AGENT_GITLAB_OAUTH_REDIRECT_URI",
    "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY",
)


class Boom(RuntimeError):
    """Failure injected at one startup or shutdown step."""


@dataclass(frozen=True)
class _Stage:
    name: str
    background: bool
    opens_http: bool
    stops: bool


def _shared(*rows: tuple[str, bool, bool]) -> list[_Stage]:
    return [
        _Stage(name, background, opens_http, stops)
        for name, opens_http, stops in rows
        for background in (False, True)
    ]


def _only(background: bool, *rows: tuple[str, bool, bool]) -> list[_Stage]:
    return [_Stage(name, background, opens_http, stops) for name, opens_http, stops in rows]


_STAGES = [
    *_shared(
        ("storage", False, False),
        ("initialize_settings", False, False),
        ("ensure_gateway_namespace", False, False),
        ("synchronize_settings", False, False),
        ("async_client", False, False),
        ("gateway_client", True, False),
        ("grant_client", True, False),
        ("listen", True, False),
        ("mcp_maintenance", True, True),
        ("measurement_writer", True, True),
        ("reset_writer", True, True),
        ("body", True, True),
    ),
    *_only(
        True,
        ("source_control", True, False),
        ("compute_runtime", True, False),
        ("compute_run", True, False),
        ("mcp_catalog", True, False),
        ("mcp_catalog_run", True, False),
        ("reaper", True, False),
        ("background_wake", True, False),
        ("snapshots", True, False),
        ("sandbox_deletions", True, False),
        ("ledger", True, False),
        ("capacity", True, False),
        ("agent_memory", True, False),
        ("profiling_deployment", True, False),
        ("profiling_service", True, False),
        ("profiling_worker", True, False),
        ("profiling_run", True, False),
        ("queue_monitor", True, False),
        ("work_signal", True, False),
        ("job_event_signal", True, False),
        ("owner_change_signal", True, False),
        ("publisher", True, False),
        ("pr_archiver", True, False),
    ),
    *_only(False, ("mcp_decision_signal", True, False)),
]


@dataclass
class _Prepared:
    app: FastAPI
    pool: _Pool
    clients: list[_Client]
    events: list[str]
    resets: list[int]
    writer_resets: list[int]


class _Pool:
    def __init__(self, events: list[str], error: str | None) -> None:
        self._events = events
        self._error = error
        self.closes = 0

    async def close(self) -> None:
        self.closes += 1
        self._events.append("pool")
        if self._error:
            raise RuntimeError(self._error)


class _Client:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1
        self._events.append("http")


class _IdleWorker:
    def __init__(self, events: list[str], label: str) -> None:
        self._events = events
        self._label = label

    async def run(self) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            self._events.append(self._label)


class _RaisingWorker:
    def __init__(self, stage: str) -> None:
        self._stage = stage

    def run(self) -> None:
        raise Boom(self._stage)


def _boom(stage: str) -> Any:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise Boom(stage)

    return fail


def _idle(events: list[str], label: str) -> Any:
    async def wait(*_args: object, **_kwargs: object) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            events.append(label)

    return wait


def _idle_method(events: list[str], label: str) -> Any:
    async def wait(self: object, *_args: object, **_kwargs: object) -> None:
        del self
        try:
            await asyncio.Event().wait()
        finally:
            events.append(label)

    return wait


@pytest.fixture(autouse=True)
async def _isolated_lifespan_state():
    reset_settings()
    reset_writer(None)
    yield
    leftover = list(lifespan_module._BACKGROUND_TASKS)
    for task in leftover:
        task.cancel()
    if leftover:
        await asyncio.gather(*leftover, return_exceptions=True)
    lifespan_module._BACKGROUND_TASKS.clear()
    reset_settings()
    reset_writer(None)


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stage: str | None = None,
    host_error: str | None = None,
    pool_error: str | None = None,
    maintenance_error: str | None = None,
) -> _Prepared:
    events: list[str] = []
    clients: list[_Client] = []
    resets: list[int] = []
    writer_resets: list[int] = []
    pool = _Pool(events, pool_error)

    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://test-only")
    for name in _QUIET_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr("cloud_agent.settings.initialize_settings", AsyncMock())
    monkeypatch.setattr(
        "cloud_agent.identity_namespace.ensure_gateway_namespace",
        AsyncMock(return_value=None),
    )

    real_reset = reset_settings

    def spy_reset() -> None:
        resets.append(1)
        events.append("reset_settings")
        real_reset()

    monkeypatch.setattr("cloud_agent.settings.reset_settings", spy_reset)

    real_reset_writer = reset_writer

    def spy_reset_writer(writer: MeasurementWriter | None = None) -> None:
        writer_resets.append(1)
        if stage == "reset_writer":
            raise Boom("reset_writer")
        if writer is None:
            events.append("reset_writer")
        real_reset_writer(writer)

    monkeypatch.setattr("cloud_agent.measurement_writer.reset_writer", spy_reset_writer)

    real_shutdown = MeasurementWriter.shutdown

    async def spy_shutdown(self: MeasurementWriter, *args: object, **kwargs: object) -> object:
        events.append("writer")
        return await real_shutdown(self, *args, **kwargs)

    monkeypatch.setattr(MeasurementWriter, "shutdown", spy_shutdown)

    def make_client(*_args: object, **_kwargs: object) -> _Client:
        if stage == "async_client":
            raise Boom("async_client")
        client = _Client(events)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", make_client)

    async def close_host() -> None:
        events.append("host")
        if host_error:
            raise RuntimeError(host_error)

    async def close_sandbox_managers() -> None:
        events.append("sandbox_managers")

    monkeypatch.setattr("cloud_agent.mcp.runtime.close_host", close_host)
    monkeypatch.setattr(
        "cloud_agent.sandbox_manager_routing.close_sandbox_manager_clients", close_sandbox_managers
    )

    real_aclose = SourceControlRuntime.aclose

    async def spy_aclose(self: SourceControlRuntime) -> None:
        events.append("source")
        await real_aclose(self)

    monkeypatch.setattr(SourceControlRuntime, "aclose", spy_aclose)

    for target, label in (
        ("cloud_agent.settings.synchronize_settings", "settings_task"),
        ("cloud_agent.bootstrap.lifespan.reap_expired_attempts", "reaper_task"),
        ("cloud_agent.bootstrap.lifespan.coordinate_shared_snapshots", "snapshot_task"),
        ("cloud_agent.bootstrap.lifespan.coordinate_sandbox_deletions", "sandbox_deletion_task"),
        ("cloud_agent.bootstrap.lifespan.maintain_agent_memories", "agent_memory_task"),
        ("cloud_agent.bootstrap.lifespan.listen", "notification_listener_task"),
        ("cloud_agent.ledger.collect_usage_snapshots", "ledger_task"),
        ("cloud_agent.capacity_history.prune_host_capacity_history", "capacity_task"),
        ("cloud_agent.operational_health.monitor_queue", "queue_monitor_task"),
        ("cloud_agent.publish_worker.publish_loop", "publish_task"),
        ("cloud_agent.workers.pr_archive.pr_archive_loop", "pr_archive_task"),
        ("cloud_agent.mcp.runtime.maintain_host_sessions", "mcp_maintenance_task"),
    ):
        monkeypatch.setattr(target, _idle(events, label))
    monkeypatch.setattr(
        "cloud_agent.mcp.catalog.McpCatalog.run", _idle_method(events, "mcp_catalog_task")
    )
    monkeypatch.setattr(
        "cloud_agent.workers.profiling_control.ProfilingPolicyWorker.run",
        _idle_method(events, "profiling_policy_task"),
    )

    def build_compute(**_kwargs: object) -> tuple[object, _IdleWorker]:
        return object(), _IdleWorker(events, "compute_resource_task")

    monkeypatch.setattr("cloud_agent.bootstrap.compute.build_compute_runtime", build_compute)

    if maintenance_error:

        async def fail_maintenance(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError(maintenance_error)

        monkeypatch.setattr("cloud_agent.mcp.runtime.maintain_host_sessions", fail_maintenance)

    _inject(monkeypatch, stage)
    return _Prepared(FastAPI(), pool, clients, events, resets, writer_resets)


def _inject(monkeypatch: pytest.MonkeyPatch, stage: str | None) -> None:
    if stage is None or stage in {"async_client", "reset_writer", "body"}:
        return
    injections: dict[str, tuple[str, Any]] = {
        "storage": ("cloud_agent.storage.assembly.Storage", _boom(stage)),
        "initialize_settings": ("cloud_agent.settings.initialize_settings", _boom(stage)),
        "ensure_gateway_namespace": (
            "cloud_agent.identity_namespace.ensure_gateway_namespace",
            _boom(stage),
        ),
        "synchronize_settings": ("cloud_agent.settings.synchronize_settings", _boom(stage)),
        "gateway_client": ("cloud_agent.gateway.GatewayClient", _boom(stage)),
        "grant_client": ("cloud_agent.grants.GrantClient", _boom(stage)),
        "source_control": (
            "cloud_agent.source_control_runtime.SourceControlRuntime.from_env",
            _boom(stage),
        ),
        "compute_runtime": ("cloud_agent.bootstrap.compute.build_compute_runtime", _boom(stage)),
        "compute_run": (
            "cloud_agent.bootstrap.compute.build_compute_runtime",
            lambda **_kwargs: (object(), _RaisingWorker(stage)),
        ),
        "mcp_catalog": ("cloud_agent.mcp.catalog.McpCatalog", _boom(stage)),
        "mcp_catalog_run": ("cloud_agent.mcp.catalog.McpCatalog.run", _boom(stage)),
        "reaper": ("cloud_agent.bootstrap.lifespan.reap_expired_attempts", _boom(stage)),
        "background_wake": ("cloud_agent.bootstrap.lifespan.BackgroundWake", _boom(stage)),
        "snapshots": ("cloud_agent.bootstrap.lifespan.coordinate_shared_snapshots", _boom(stage)),
        "sandbox_deletions": (
            "cloud_agent.bootstrap.lifespan.coordinate_sandbox_deletions",
            _boom(stage),
        ),
        "ledger": ("cloud_agent.ledger.collect_usage_snapshots", _boom(stage)),
        "capacity": ("cloud_agent.capacity_history.prune_host_capacity_history", _boom(stage)),
        "agent_memory": ("cloud_agent.bootstrap.lifespan.maintain_agent_memories", _boom(stage)),
        "profiling_deployment": (
            "cloud_agent.profiling_control_config.profiling_control_deployment",
            _boom(stage),
        ),
        "profiling_service": (
            "cloud_agent.application.profiling_control.ProfilingControlService",
            _boom(stage),
        ),
        "profiling_worker": (
            "cloud_agent.workers.profiling_control.ProfilingPolicyWorker",
            _boom(stage),
        ),
        "profiling_run": (
            "cloud_agent.workers.profiling_control.ProfilingPolicyWorker",
            lambda **_kwargs: _RaisingWorker(stage),
        ),
        "queue_monitor": ("cloud_agent.operational_health.monitor_queue", _boom(stage)),
        "work_signal": ("cloud_agent.bootstrap.lifespan.WorkSignal", _boom(stage)),
        "job_event_signal": ("cloud_agent.bootstrap.lifespan.JobEventSignal", _boom(stage)),
        "owner_change_signal": ("cloud_agent.bootstrap.lifespan.OwnerChangeSignal", _boom(stage)),
        "listen": ("cloud_agent.bootstrap.lifespan.listen", _boom(stage)),
        "publisher": ("cloud_agent.publish_worker.publish_loop", _boom(stage)),
        "pr_archiver": ("cloud_agent.workers.pr_archive.pr_archive_loop", _boom(stage)),
        "mcp_maintenance": ("cloud_agent.mcp.runtime.maintain_host_sessions", _boom(stage)),
        "mcp_decision_signal": ("cloud_agent.bootstrap.lifespan.McpDecisionSignal", _boom(stage)),
        "measurement_writer": ("cloud_agent.measurement_writer.MeasurementWriter", _boom(stage)),
    }
    target, replacement = injections[stage]
    monkeypatch.setattr(target, replacement)


def _assert_released(prepared: _Prepared, stage: _Stage, caplog: pytest.LogCaptureFixture) -> None:
    assert prepared.pool.closes == 1
    assert prepared.events.count("pool") == 1
    assert prepared.events.count("host") == 1
    assert prepared.events.count("sandbox_managers") == 1
    assert prepared.events.count("http") == (1 if stage.opens_http else 0)
    expected_source_closes = int(stage.background and stage.name not in _BEFORE_SOURCE)
    assert prepared.events.count("source") == expected_source_closes
    if stage.opens_http:
        assert prepared.events.index("host") < prepared.events.index("http")
        assert prepared.events.index("http") < prepared.events.index("pool")
        assert len(prepared.clients) == 1
        assert prepared.clients[0].closed == 1
    else:
        assert prepared.clients == []
    assert len(prepared.resets) == 1
    assert not lifespan_module._BACKGROUND_TASKS
    settings_task = getattr(prepared.app.state, "settings_task", None)
    if stage.name in _BEFORE_SETTINGS_TASK:
        assert settings_task is None
    else:
        assert settings_task is not None
        assert settings_task.done()
    if stage.name == "reset_writer":
        assert prepared.writer_resets == [1]
    stopped = "cloud agent control plane stopped" in caplog.text
    assert stopped is stage.stops


@pytest.mark.parametrize("stage", _STAGES, ids=lambda stage: f"{stage.name}-bg-{stage.background}")
async def test_startup_failure_still_releases_the_pool_and_http_client(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, stage: _Stage
) -> None:
    caplog.set_level("INFO", logger="cloud_agent.app")
    prepared = _prepare(monkeypatch, stage=stage.name)

    with pytest.raises(Boom) as caught:
        async with lifespan(prepared.app, background=stage.background):
            if stage.name != "body":
                pytest.fail(f"{stage.name} reached the serving loop")
            raise Boom("body")

    assert str(caught.value) == stage.name
    _assert_released(prepared, stage, caplog)


@pytest.mark.parametrize("background", [False, True])
async def test_successful_shutdown_keeps_its_order(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, background: bool
) -> None:
    """The stop sequence is the one operators already have, including its log."""
    caplog.set_level("INFO", logger="cloud_agent.app")
    prepared = _prepare(monkeypatch)
    expected_tasks = (
        list(_EXPECTED_SHUTDOWN_TASKS)
        if background
        else ["settings_task", "mcp_maintenance_task", "notification_listener_task"]
    )

    async with lifespan(prepared.app, background=background):
        await asyncio.sleep(0)
        assert prepared.events == []
        for name in expected_tasks:
            task = getattr(prepared.app.state, name)
            assert task is not None and not task.done()
        absent = [name for name in _EXPECTED_SHUTDOWN_TASKS if name not in expected_tasks]
        assert all(getattr(prepared.app.state, name) is None for name in absent)

    assert lifespan_module._SHUTDOWN_TASK_ATTRS == _EXPECTED_SHUTDOWN_TASKS
    assert prepared.events == [
        "writer",
        "reset_writer",
        *expected_tasks,
        "host",
        "sandbox_managers",
        *(["source"] if background else []),
        "http",
        "pool",
        "reset_settings",
    ]
    assert prepared.pool.closes == 1
    assert prepared.clients[0].closed == 1
    assert len(prepared.resets) == 1
    assert not lifespan_module._BACKGROUND_TASKS
    assert "cloud agent control plane stopped" in caplog.text
    started = (
        "cloud agent control plane started" if background else "cloud agent relay listener started"
    )
    assert started in caplog.text


async def test_pool_close_failure_does_not_mask_settings_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare(monkeypatch, stage="initialize_settings", pool_error="pool close failed")

    with pytest.raises(Boom) as caught:
        async with lifespan(prepared.app, background=False):
            pytest.fail("settings startup failed open")

    assert str(caught.value) == "initialize_settings"
    assert any("pool close failed" in note for note in caught.value.__notes__)
    assert prepared.pool.closes == 1
    assert len(prepared.resets) == 1
    assert prepared.clients == []
    assert prepared.events.count("http") == 0


async def test_shutdown_step_failure_still_closes_http_and_the_pool(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="cloud_agent.app")
    prepared = _prepare(
        monkeypatch,
        host_error="host close failed",
        pool_error="pool close failed",
    )

    with pytest.raises(RuntimeError, match="host close failed"):
        async with lifespan(prepared.app, background=False):
            pass

    assert prepared.events.index("host") < prepared.events.index("http")
    assert prepared.events.index("http") < prepared.events.index("pool")
    assert prepared.pool.closes == 1
    assert prepared.clients[0].closed == 1
    assert len(prepared.resets) == 1
    assert any(
        record.exc_info is not None
        and record.exc_info[1] is not None
        and "pool close failed" in str(record.exc_info[1])
        for record in caplog.records
    )


async def test_shutdown_step_failure_does_not_mask_the_request_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare(monkeypatch, host_error="host close failed", pool_error="pool close failed")

    with pytest.raises(Boom) as caught:
        async with lifespan(prepared.app, background=True):
            raise Boom("request failed")

    assert str(caught.value) == "request failed"
    notes = " ".join(caught.value.__notes__)
    assert "host close failed" in notes
    assert "pool close failed" in notes
    assert prepared.pool.closes == 1
    assert prepared.clients[0].closed == 1


@pytest.mark.parametrize("request_error", [False, True])
async def test_a_failed_background_task_cannot_skip_later_cleanup(
    monkeypatch: pytest.MonkeyPatch, request_error: bool
) -> None:
    prepared = _prepare(monkeypatch, maintenance_error="maintenance crashed")

    if request_error:
        with pytest.raises(Boom) as caught:
            async with lifespan(prepared.app, background=False):
                await asyncio.wait([prepared.app.state.mcp_maintenance_task])
                raise Boom("request failed")
        assert str(caught.value) == "request failed"
        assert any("maintenance crashed" in note for note in caught.value.__notes__)
    else:
        with pytest.raises(RuntimeError, match="maintenance crashed"):
            async with lifespan(prepared.app, background=False):
                await asyncio.wait([prepared.app.state.mcp_maintenance_task])

    assert prepared.app.state.settings_task.cancelled()
    assert prepared.app.state.notification_listener_task.cancelled()
    assert prepared.pool.closes == 1
    assert prepared.clients[0].closed == 1
    assert prepared.events.index("http") < prepared.events.index("pool")
    assert not lifespan_module._BACKGROUND_TASKS


async def test_shutdown_cancellation_joins_send_now_before_closing_resources() -> None:
    """Cancellation is restored only after eager handoffs and later cleanup finish."""
    from cloud_agent.application import send_now

    events: list[str] = []
    unwinding = asyncio.Event()
    release = asyncio.Event()

    async def handoff() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            unwinding.set()
            await release.wait()
            events.append("handoff")

    handoff_task = asyncio.create_task(handoff())
    send_now._hand_overs.add(handoff_task)
    handoff_task.add_done_callback(send_now._hand_overs.discard)
    await asyncio.sleep(0)

    async def close_host() -> None:
        events.append("host")

    async def close_sandbox_managers() -> None:
        events.append("sandbox_managers")

    app = FastAPI()
    shutdown = asyncio.create_task(
        lifespan_module._shutdown_lifespan(
            app,
            pool=_Pool(events, None),
            http=None,
            writer=None,
            writer_installed=False,
            log_stopped=False,
            close_host=close_host,
            close_sandbox_manager_clients=close_sandbox_managers,
            reset_writer=lambda _writer: None,
            reset_settings=lambda: events.append("reset_settings"),
        )
    )
    await asyncio.wait_for(unwinding.wait(), 1)
    shutdown.cancel()
    await asyncio.sleep(0)
    assert not shutdown.done()

    release.set()
    errors = await asyncio.wait_for(shutdown, 1)

    assert len(errors) == 1
    assert isinstance(errors[0], asyncio.CancelledError)
    assert events.index("handoff") < events.index("pool")
    assert events[-1] == "reset_settings"
    with pytest.raises(asyncio.CancelledError):
        lifespan_module._surface_cleanup_errors(None, errors)


async def test_measurement_writer_cancellation_propagates_after_later_cleanup() -> None:
    """A cancelled writer cannot skip tasks, clients, the pool, or registries."""
    events: list[str] = []
    app = FastAPI()
    writer = type(
        "_Writer",
        (),
        {"shutdown": AsyncMock(side_effect=asyncio.CancelledError())},
    )()

    async def close_host() -> None:
        events.append("host")

    async def close_sandbox_managers() -> None:
        events.append("sandbox_managers")

    errors = await lifespan_module._shutdown_lifespan(
        app,
        pool=_Pool(events, None),
        http=_Client(events),
        writer=writer,
        writer_installed=True,
        log_stopped=False,
        close_host=close_host,
        close_sandbox_manager_clients=close_sandbox_managers,
        reset_writer=lambda _writer: events.append("reset_writer"),
        reset_settings=lambda: events.append("reset_settings"),
    )

    assert len(errors) == 1
    assert isinstance(errors[0], asyncio.CancelledError)
    assert events == ["reset_writer", "host", "sandbox_managers", "http", "pool", "reset_settings"]
    with pytest.raises(asyncio.CancelledError):
        lifespan_module._surface_cleanup_errors(None, errors)


async def test_owner_cancellation_while_joining_a_resistant_child_finishes_cleanup() -> None:
    """Owner cancellation is not mistaken for the child's expected cancellation."""
    events: list[str] = []
    first_cancel_seen = asyncio.Event()
    never = asyncio.Event()

    async def resist_first_cancel() -> None:
        try:
            await never.wait()
        except asyncio.CancelledError:
            first_cancel_seen.set()
            await never.wait()

    child = asyncio.create_task(resist_first_cancel())
    app = FastAPI()
    app.state.settings_task = child

    async def close_host() -> None:
        events.append("host")

    async def close_sandbox_managers() -> None:
        events.append("sandbox_managers")

    shutdown = asyncio.create_task(
        lifespan_module._shutdown_lifespan(
            app,
            pool=_Pool(events, None),
            http=_Client(events),
            writer=None,
            writer_installed=False,
            log_stopped=False,
            close_host=close_host,
            close_sandbox_manager_clients=close_sandbox_managers,
            reset_writer=lambda _writer: None,
            reset_settings=lambda: events.append("reset_settings"),
        )
    )
    await asyncio.wait_for(first_cancel_seen.wait(), 1)
    shutdown.cancel()
    errors = await asyncio.wait_for(shutdown, 1)

    assert child.cancelled()
    assert len([error for error in errors if isinstance(error, asyncio.CancelledError)]) == 1
    assert events == ["host", "sandbox_managers", "http", "pool", "reset_settings"]
    with pytest.raises(asyncio.CancelledError):
        lifespan_module._surface_cleanup_errors(None, errors)


async def test_primary_error_survives_owner_cancellation_during_cleanup() -> None:
    """A cancellation in teardown is recorded once without replacing the primary."""
    events: list[str] = []
    primary = Boom("primary")
    first_cancel_seen = asyncio.Event()
    never = asyncio.Event()

    async def resist_first_cancel() -> None:
        try:
            await never.wait()
        except asyncio.CancelledError:
            first_cancel_seen.set()
            await never.wait()

    child = asyncio.create_task(resist_first_cancel())
    app = FastAPI()
    app.state.settings_task = child

    async def unwind_primary() -> None:
        try:
            raise primary
        finally:
            errors = await lifespan_module._shutdown_lifespan(
                app,
                pool=_Pool(events, None),
                http=_Client(events),
                writer=None,
                writer_installed=False,
                log_stopped=False,
                close_host=lambda: None,
                close_sandbox_manager_clients=lambda: None,
                reset_writer=lambda _writer: None,
                reset_settings=lambda: events.append("reset_settings"),
            )
            lifespan_module._surface_cleanup_errors(primary, errors)

    cleanup = asyncio.create_task(unwind_primary())
    await asyncio.wait_for(first_cancel_seen.wait(), 1)
    cleanup.cancel()
    with pytest.raises(Boom) as caught:
        await asyncio.wait_for(cleanup, 1)

    assert caught.value is primary
    cancellation_notes = [note for note in primary.__notes__ if "CancelledError" in note]
    assert len(cancellation_notes) == 1
    assert child.cancelled()
    assert events[-2:] == ["pool", "reset_settings"]


@pytest.mark.parametrize(
    ("background", "attribute", "target"),
    [
        (True, "publish_task", "cloud_agent.publish_worker.publish_loop"),
        (False, "mcp_maintenance_task", "cloud_agent.mcp.runtime.maintain_host_sessions"),
    ],
    ids=["publisher", "mcp-maintenance"],
)
async def test_a_background_task_that_dies_is_reported_when_it_dies(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    background: bool,
    attribute: str,
    target: str,
) -> None:
    """A loop that stops with an error says so then, not first at shutdown.

    Until shutdown re-raises it, nothing else would: the process goes on
    serving with no publisher, or no reaper, and nothing in its log says why.
    """
    caplog.set_level("INFO", logger="cloud_agent.app")
    prepared = _prepare(monkeypatch)

    async def crash(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"{attribute} crashed")

    monkeypatch.setattr(target, crash)

    with pytest.raises(RuntimeError, match=f"{attribute} crashed"):
        async with lifespan(prepared.app, background=background):
            await asyncio.wait([getattr(prepared.app.state, attribute)])
            await asyncio.sleep(0)
            died = [
                record
                for record in caplog.records
                if getattr(record, "event", "") == "agent_background_task_failed"
            ]
            assert len(died) == 1, "a background task died without a word"
            assert died[0].levelname == "ERROR"
            assert died[0].exc_info is not None
            assert f"{attribute} crashed" in str(died[0].exc_info[1])

    assert prepared.pool.closes == 1
