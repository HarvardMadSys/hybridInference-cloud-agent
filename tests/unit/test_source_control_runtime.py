"""A GitHub App that failed to load is loaded again, and never read as absent.

Issue #47: on 2026-08-07 the control plane started before its App key belonged
to the container user. The read failed with ``PermissionError``, the lifespan
kept ``None``, and six minutes later — ownership fixed — nothing looked again.
These tests hold the runtime that replaced that one-shot load to three things:
the three states stay apart, a repaired key is picked up by the *same* process,
and the log says what happened once rather than every thirty seconds.

File permissions are simulated at the exact read boundary, not with ``chmod``:
a test run as root reads a mode-000 file anyway, and would pass for the wrong
reason.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
import threading
from typing import TYPE_CHECKING, Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from cloud_agent import github_app
from cloud_agent.github_app import AppConfig, GitHubAppCredentials
from cloud_agent.source_control_runtime import (
    GitHubAppProvider,
    GitHubAppState,
    GitLabOAuthLoad,
    SourceControlRuntime,
    SourceControlUnavailable,
)

if TYPE_CHECKING:
    from pathlib import Path

_FAILED = "source_control_load_failed"
_RECOVERED = "source_control_load_recovered"


@pytest.fixture(scope="module")
def private_pem() -> str:
    """A real key, so that loading signs for real rather than trusting a string."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


class _Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _KeyReads:
    """``open`` as the App config sees it, with a switch for "not yet ours".

    Counts every read of the key path, which is how a test tells a retry that
    really re-opened the file from one that answered from memory.
    """

    def __init__(self, key_path: Path) -> None:
        self.key_path = str(key_path)
        self.denied = False
        self.reads = 0

    def __call__(self, file: Any, *args: Any, **kwargs: Any) -> Any:
        if str(file) == self.key_path:
            self.reads += 1
            if self.denied:
                raise PermissionError(13, "Permission denied", self.key_path)
        return builtins.open(file, *args, **kwargs)


def _events(caplog: pytest.LogCaptureFixture, event: str, provider: str = "github") -> list[Any]:
    return [
        record
        for record in caplog.records
        if getattr(record, "event", None) == event and getattr(record, "provider", None) == provider
    ]


def _app_env(key_path: Path, **extra: str) -> dict[str, str]:
    return {
        "AGENT_GITHUB_APP_ID": "12345",
        "AGENT_GITHUB_APP_PRIVATE_KEY_PATH": str(key_path),
        **extra,
    }


def test_a_deployment_without_an_app_id_is_unconfigured_even_with_the_compose_key_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The compose file always sets the key path, and mounts /dev/null there.

    Read as intent, that default would turn every deployment without an App
    into a permanently failing one — 503 on every job, an ERROR every retry.
    """
    dev_null = tmp_path / "github-app.pem"
    dev_null.write_text("")
    provider = GitHubAppProvider(
        {"AGENT_GITHUB_APP_ID": "  ", "AGENT_GITHUB_APP_PRIVATE_KEY_PATH": str(dev_null)}
    )

    assert provider.load_status == "unconfigured"
    assert provider.get() is None
    assert provider.state() == GitHubAppState(credentials=None, load_status="unconfigured")
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


def test_an_unreadable_key_recovers_in_the_same_process_once_it_can_be_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    private_pem: str,
) -> None:
    """The 2026-08-07 sequence: unreadable at start, chowned later, no restart."""
    caplog.set_level(logging.INFO, logger="cloud_agent.app")
    key = tmp_path / "github-app.pem"
    key.write_text(private_pem)
    reads = _KeyReads(key)
    reads.denied = True
    monkeypatch.setattr(github_app, "open", reads, raising=False)
    clock = _Clock()

    provider = GitHubAppProvider(_app_env(key), clock=clock)

    assert provider.load_status == "unavailable"
    with pytest.raises(SourceControlUnavailable):
        provider.get()
    [failure] = _events(caplog, _FAILED)
    assert failure.levelname == "ERROR"
    assert failure.reason == "key_file_unreadable"

    reads.denied = False  # the chown: ctime moves, mtime does not
    clock.advance(29.9)
    with pytest.raises(SourceControlUnavailable):
        provider.get()
    assert reads.reads == 1, "a retry before the interval would hammer the file"

    clock.advance(0.2)
    credentials = provider.get()

    assert isinstance(credentials, GitHubAppCredentials)
    assert provider.load_status == "ready"
    assert reads.reads == 2, "the retry must open the file again, not trust a stat"
    assert provider.get() is credentials, "a loaded App is reused, token cache and all"
    [recovered] = _events(caplog, _RECOVERED)
    assert recovered.levelname == "INFO"
    assert len(_events(caplog, _FAILED)) == 1


@pytest.mark.parametrize(
    ("broken", "reason"),
    [
        (None, "key_file_unreadable"),
        ("", "key_missing"),
        ("this is not a key\n", "key_invalid"),
    ],
    ids=["missing", "empty", "not-a-key"],
)
def test_a_key_repaired_in_place_is_picked_up(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    private_pem: str,
    broken: str | None,
    reason: str,
) -> None:
    """A missing mount, an empty file and garbage are all fixable without a restart."""
    key = tmp_path / "github-app.pem"
    if broken is not None:
        key.write_text(broken)
    clock = _Clock()

    provider = GitHubAppProvider(_app_env(key), clock=clock)

    assert provider.load_status == "unavailable"
    assert [record.reason for record in _events(caplog, _FAILED)] == [reason]

    key.write_text(private_pem)
    clock.advance(30)

    assert isinstance(provider.get(), GitHubAppCredentials)
    assert provider.load_status == "ready"


def test_an_inline_key_wins_over_the_file_and_is_never_swapped_for_it(
    tmp_path: Path, private_pem: str
) -> None:
    """The precedence the one-shot load had, and no quiet fallback when it fails."""
    missing = tmp_path / "absent.pem"
    escaped = private_pem.replace("\n", "\\n")  # as it arrives through an env var

    inline = GitHubAppProvider(_app_env(missing, AGENT_GITHUB_APP_PRIVATE_KEY=escaped))
    assert inline.load_status == "ready", "the file is not read when a key is inline"

    good_file = tmp_path / "github-app.pem"
    good_file.write_text(private_pem)
    bad_inline = GitHubAppProvider(
        _app_env(good_file, AGENT_GITHUB_APP_PRIVATE_KEY="not-a-key"), clock=_Clock()
    )
    assert bad_inline.load_status == "unavailable", "an invalid inline key is not replaced"


def test_attempts_are_spaced_and_never_run_twice_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, private_pem: str
) -> None:
    """Every request asks while the App is down; only one of them may load it."""
    key = tmp_path / "github-app.pem"
    clock = _Clock()
    provider = GitHubAppProvider(_app_env(key), clock=clock)
    calls = 0
    entered = threading.Event()
    release = threading.Event()
    real_from_env = AppConfig.from_env.__func__

    def slow_from_env(cls: type[AppConfig], env: dict[str, str]) -> AppConfig | None:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(5), "the test never released the slow load"
        return real_from_env(cls, env)

    for _ in range(50):
        with pytest.raises(SourceControlUnavailable):
            provider.get()

    monkeypatch.setattr(AppConfig, "from_env", classmethod(slow_from_env))
    key.write_text(private_pem)
    clock.advance(30)
    results: list[Any] = []
    loader = threading.Thread(target=lambda: results.append(provider.get()))
    loader.start()
    assert entered.wait(5)

    # A second caller while the first is loading: answered at once, not queued.
    with pytest.raises(SourceControlUnavailable):
        provider.get()
    release.set()
    loader.join(5)

    assert calls == 1
    assert isinstance(results[0], GitHubAppCredentials)
    assert provider.get() is results[0]


def test_a_reading_taken_during_a_reload_never_pairs_no_credentials_with_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, private_pem: str
) -> None:
    """Credentials and status read apart could describe two different moments.

    A request that found another caller mid-reload got no credentials — and,
    reading the status once that reload had finished, ``ready`` beside them.
    The Integrations page renders that pair as "Admin setup required". The
    lenient dependency now answers both from one reading, so they agree.
    """
    from cloud_agent.api.deps import get_github_app_state

    key = tmp_path / "github-app.pem"
    clock = _Clock()
    provider = GitHubAppProvider(_app_env(key), clock=clock)
    runtime = SourceControlRuntime(github=provider, gitlab=GitLabOAuthLoad({}))
    entered = threading.Event()
    release = threading.Event()
    real_from_env = AppConfig.from_env.__func__

    def slow_from_env(cls: type[AppConfig], env: dict[str, str]) -> AppConfig | None:
        entered.set()
        assert release.wait(5), "the test never released the slow load"
        return real_from_env(cls, env)

    monkeypatch.setattr(AppConfig, "from_env", classmethod(slow_from_env))
    key.write_text(private_pem)
    clock.advance(30)
    loader = threading.Thread(target=provider.get)
    loader.start()
    assert entered.wait(5)

    during = get_github_app_state(runtime)  # the reload holds the lock: answered at once
    release.set()
    loader.join(5)

    assert during == GitHubAppState(credentials=None, load_status="unavailable")
    assert provider.load_status == "ready", "a status read now would disagree with `during`"
    after = get_github_app_state(runtime)
    assert after.load_status == "ready"
    assert isinstance(after.credentials, GitHubAppCredentials)


def test_the_log_says_it_once_per_reason_and_never_prints_the_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, private_pem: str
) -> None:
    """An ERROR every thirty seconds would bury the one line that explains it."""
    caplog.set_level(logging.INFO, logger="cloud_agent.app")
    marker = "c2VjcmV0LWtleS1tYXRlcmlhbA"
    key = tmp_path / "github-app.pem"
    key.write_text(f"not a key {marker}\n")
    clock = _Clock()
    provider = GitHubAppProvider(_app_env(key), clock=clock)

    for _ in range(5):
        clock.advance(30)
        provider.state()
    key.unlink()  # a different fault: the mount went away
    for _ in range(3):
        clock.advance(30)
        provider.state()
    key.write_text(private_pem)
    clock.advance(30)
    provider.state()
    provider.state()

    failures = _events(caplog, _FAILED)
    assert [record.reason for record in failures] == ["key_invalid", "key_file_unreadable"]
    assert all(record.levelname == "ERROR" for record in failures)
    [recovered] = _events(caplog, _RECOVERED)
    assert recovered.unavailable_seconds == pytest.approx(270.0)
    assert str(key) in caplog.text, "the path is what an operator needs"
    assert marker not in caplog.text
    assert private_pem.splitlines()[1] not in caplog.text


def test_gitlab_is_loaded_once_and_its_failure_leaves_github_alone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, private_pem: str
) -> None:
    """GitLab reads only the environment, which a running process cannot change."""
    key = tmp_path / "github-app.pem"
    key.write_text(private_pem)

    runtime = SourceControlRuntime.from_env(
        _app_env(key, AGENT_GITLAB_OAUTH_CLIENT_ID="gitlab-client-only")
    )

    assert runtime.gitlab.load_status == "unavailable"
    assert runtime.gitlab.client is None
    [failure] = _events(caplog, _FAILED, provider="gitlab")
    assert failure.levelname == "ERROR"
    assert failure.reason == "invalid_configuration"
    assert not hasattr(runtime.gitlab, "get"), "nothing to retry: the answer cannot change"
    assert runtime.github.load_status == "ready"


def test_gitlab_unconfigured_and_ready_are_what_they_were() -> None:
    """The two states the one-shot load already had, unchanged."""
    assert GitLabOAuthLoad({}).load_status == "unconfigured"
    ready = GitLabOAuthLoad(
        {
            "AGENT_GITLAB_OAUTH_CLIENT_ID": "id",
            "AGENT_GITLAB_OAUTH_CLIENT_SECRET": "secret",
            "AGENT_GITLAB_OAUTH_REDIRECT_URI": "https://agents.example/callback",
            "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY": "a-test-encryption-key",
        }
    )
    assert ready.load_status == "ready"
    assert ready.client is not None


async def test_saved_provider_settings_replace_credentials_and_close_retired_pool(
    monkeypatch: pytest.MonkeyPatch, private_pem: str
) -> None:
    from cloud_agent import source_control_runtime

    values: dict[str, str] = {}
    monkeypatch.setattr(source_control_runtime, "settings_environment", lambda: dict(values))
    runtime = SourceControlRuntime.from_env()
    provider = runtime.github
    assert provider.get() is None
    assert runtime.gitlab.client is None
    values.update(
        AGENT_GITHUB_APP_ID="first-app",
        AGENT_GITHUB_APP_PRIVATE_KEY=private_pem,
    )
    first = provider.get()
    assert first is not None
    async with first._http() as pool:
        values["AGENT_GITHUB_APP_ID"] = "second-app"
        second = provider.get()
        assert second is not None and second is not first
        assert second._config.app_id == "second-app"
        assert runtime.github is provider
        assert not pool.is_closed, "saving settings must let an in-flight call finish"
    assert pool.is_closed
    values.update(
        AGENT_GITLAB_OAUTH_CLIENT_ID="client",
        AGENT_GITLAB_OAUTH_CLIENT_SECRET="secret",
        AGENT_GITLAB_OAUTH_REDIRECT_URI="https://agents.example/callback",
        AGENT_SOURCE_CONTROL_ENCRYPTION_KEY="test-encryption-key",
    )
    first_gitlab = runtime.gitlab.client
    assert first_gitlab is not None
    values["AGENT_GITLAB_OAUTH_CLIENT_SECRET"] = "rotated-secret"
    second_gitlab = runtime.gitlab.client
    assert second_gitlab is not None and second_gitlab is not first_gitlab
    assert second_gitlab.config.client_secret == "rotated-secret"
    values["AGENT_GITHUB_APP_PRIVATE_KEY"] = "invalid-key"
    with pytest.raises(SourceControlUnavailable):
        provider.get()
    values["AGENT_GITHUB_APP_ID"] = ""
    assert provider.get() is None
    values["AGENT_GITLAB_OAUTH_CLIENT_SECRET"] = ""
    assert runtime.gitlab.client is None
    assert runtime.gitlab.load_status == "unavailable"
    await runtime.aclose()


def test_failed_app_reload_uses_current_interval_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, private_pem: str
) -> None:
    from cloud_agent import source_control_runtime

    values = {"github_reload_interval_s": 30.0}
    monkeypatch.setattr(
        source_control_runtime,
        "number_setting",
        lambda name, default=None: values.get(name, default),
    )
    path = tmp_path / "later.pem"
    clock = _Clock()
    provider = GitHubAppProvider(_app_env(path), clock=clock)
    path.write_text(private_pem)
    clock.advance(2)
    with pytest.raises(SourceControlUnavailable):
        provider.get()
    values["github_reload_interval_s"] = 1
    assert provider.get() is not None


async def test_closing_releases_the_client_and_loads_nothing_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, private_pem: str
) -> None:
    """Shutdown closes what the App opened, and a late ask cannot open more."""
    key = tmp_path / "github-app.pem"
    key.write_text(private_pem)
    loaded = GitHubAppProvider(_app_env(key))
    credentials = loaded.get()
    assert credentials is not None
    async with credentials._http() as client:
        pass

    await loaded.aclose()

    assert client.is_closed

    clock = _Clock()
    failing = GitHubAppProvider(_app_env(tmp_path / "absent.pem"), clock=clock)
    await failing.aclose()
    reads = _KeyReads(tmp_path / "absent.pem")
    monkeypatch.setattr(github_app, "open", reads, raising=False)
    clock.advance(60)
    with pytest.raises(SourceControlUnavailable):
        failing.get()
    assert reads.reads == 0


async def test_the_lifespan_hands_the_loops_the_provider_not_a_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, private_pem: str
) -> None:
    """The half of issue #47 a fix to ``app.state`` alone would have missed.

    The publisher and the reaper were given the credentials the lifespan had at
    startup and kept them for as long as they ran. Asserted on the real
    lifespan, because the bug was in the wiring and nowhere else.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    import asyncpg

    from cloud_agent.app import lifespan

    key = tmp_path / "github-app.pem"
    key.write_text(private_pem)
    for name, value in _app_env(key).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://test-only")
    monkeypatch.delenv("GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("GATEWAY_ISSUER", raising=False)
    monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(return_value=AsyncMock()))
    monkeypatch.setattr(
        "cloud_agent.identity_namespace.ensure_gateway_namespace", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("cloud_agent.settings.initialize_settings", AsyncMock(return_value=None))

    started: dict[str, dict[str, Any]] = {}

    def recording(name: str) -> Any:
        async def loop(*args: Any, **kwargs: Any) -> None:
            started[name] = kwargs
            await asyncio.Event().wait()

        return loop

    async def idle(*args: Any, **kwargs: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr("cloud_agent.bootstrap.lifespan.reap_expired_attempts", recording("reaper"))
    monkeypatch.setattr("cloud_agent.publish_worker.publish_loop", recording("publisher"))
    monkeypatch.setattr(
        "cloud_agent.bootstrap.lifespan.coordinate_sandbox_deletions", recording("sandbox_deletion")
    )
    monkeypatch.setattr("cloud_agent.workers.pr_archive.pr_archive_loop", recording("archiver"))
    monkeypatch.setattr(
        "cloud_agent.bootstrap.lifespan.maintain_agent_memories", recording("memory")
    )
    for loop_path in (
        "cloud_agent.settings.synchronize_settings",
        # This wiring test uses a fake pool; its notification listener must
        # also stay idle instead of resolving the test-only database hostname.
        "cloud_agent.bootstrap.lifespan.listen",
        "cloud_agent.bootstrap.lifespan.coordinate_shared_snapshots",
        "cloud_agent.ledger.collect_usage_snapshots",
        "cloud_agent.capacity_history.prune_host_capacity_history",
        "cloud_agent.operational_health.monitor_queue",
        "cloud_agent.mcp.runtime.maintain_host_sessions",
        "cloud_agent.mcp.catalog.McpCatalog.run",
        "cloud_agent.workers.profiling_control.ProfilingPolicyWorker.run",
    ):
        monkeypatch.setattr(loop_path, idle)

    compute_stopped = asyncio.Event()

    async def compute_loop() -> None:
        started["compute"] = {}
        try:
            await asyncio.Event().wait()
        finally:
            compute_stopped.set()

    compute_commands = object()
    compute_builder = Mock(return_value=(compute_commands, SimpleNamespace(run=compute_loop)))
    monkeypatch.setattr("cloud_agent.bootstrap.compute.build_compute_runtime", compute_builder)

    app = FastAPI()
    async with lifespan(app, background=True):
        runtime = app.state.source_control
        await asyncio.sleep(0)
        assert isinstance(runtime, SourceControlRuntime)
        assert app.state.compute_commands is compute_commands
        compute_builder.assert_called_once_with(
            repository=app.state.storage.compute_resources,
            host_pool=app.state.storage.host_pool,
            http=app.state.http,
        )
        assert "compute" in started
        assert not app.state.compute_resource_task.done()
        assert "sandbox_deletion" in started
        assert not app.state.sandbox_deletion_task.done()
        assert "memory" in started
        assert not app.state.agent_memory_task.done()
        assert not hasattr(app.state, "app_credentials")
        assert started["reaper"]["github_app"] is runtime.github
        assert started["publisher"]["github_app"] is runtime.github
        assert started["archiver"]["github_app"] is runtime.github
        assert started["archiver"]["client"] is app.state.http
        credentials = runtime.github.get()
        assert credentials is not None
        async with credentials._http() as client:
            pass

    assert client.is_closed, "the App's client outlived the application that owned it"
    assert compute_stopped.is_set()
    assert app.state.compute_resource_task.cancelled()
    assert app.state.sandbox_deletion_task.cancelled()
    assert app.state.pr_archive_task.cancelled()
    assert app.state.agent_memory_task.cancelled()

    # The MCP listener shares the database but must not start another scheduler.
    compute_builder.reset_mock()
    relay_runtime_builder = Mock(
        side_effect=AssertionError("the MCP relay must not initialize source control")
    )
    monkeypatch.setattr(
        "cloud_agent.source_control_runtime.SourceControlRuntime.from_env",
        relay_runtime_builder,
    )
    relay = FastAPI()
    async with lifespan(relay, background=False):
        await asyncio.sleep(0)
        assert relay.state.source_control is None
        assert relay.state.compute_commands is None
        assert relay.state.compute_resource_task is None
        assert relay.state.sandbox_deletion_task is None
        assert relay.state.pr_archive_task is None
        assert relay.state.agent_memory_task is None
        compute_builder.assert_not_called()
        relay_runtime_builder.assert_not_called()


def test_only_the_routes_that_describe_the_state_tolerate_an_unloaded_app() -> None:
    """Strict is the default, so a new route cannot quietly read "no App".

    ``get_agent_app_credentials`` answers 503 while the App is unavailable; the
    lenient variant answers ``None``. Only the routes that describe the state —
    and must keep answering during an outage — may take the lenient one.
    """
    from cloud_agent.api import routes
    from cloud_agent.api.deps import get_github_app_state

    def calls(dependant: Any) -> set[Any]:
        found = set()
        for dependency in dependant.dependencies:
            found.add(dependency.call)
            found |= calls(dependency)
        return found

    lenient = sorted(
        f"{method} {route.path}"
        for surface in routes.SURFACE_ROUTERS
        for route in surface.routes
        if get_github_app_state in calls(route.dependant)
        for method in route.methods
    )

    assert lenient == [
        "DELETE /v1/agent/github/connect/{installation_id}",
        "DELETE /v1/agent/integrations/github",
        "DELETE /v1/agent/integrations/github/connections/{installation_id}",
        "DELETE /v1/agent/integrations/gitlab/connections/{connection_id}",
        "GET /v1/agent/config",
        "GET /v1/agent/integrations",
    ]
