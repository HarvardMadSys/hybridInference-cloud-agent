"""Requests reach the enrolled host that owns the retained container.

The bug this prevents is quiet, which is why it gets its own file. With one
With a process-global fallback and several hosts, a file listing for a job on
host B could be answered by host A's sandbox manager: not an error, not a 500 — a
plausible directory listing of the wrong container. Everything here is about
the persisted host ownership fence.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.support.storage import AllCapabilities

from cloud_agent import sandbox_manager_routing
from cloud_agent.sandbox_manager_client import SandboxManagerError
from cloud_agent.sandbox_manager_routing import (
    hydrate_shared_sandbox_manager,
    sandbox_manager_for_attempt,
    sandbox_manager_for_job,
)

SANDBOX_MANAGER_TOKEN_ENV = "AGENT_SANDBOX_MANAGER_TOKEN"


class FakeRoutingStore(AllCapabilities):
    """Answers only the two lookups sandbox manager routing performs."""

    def __init__(self, by_attempt: dict[int, dict[str, Any]] | None = None, **by_job: Any) -> None:
        self.by_attempt = by_attempt or {}
        self.by_job = by_job

    async def host_for_attempt(self, *, attempt_id: int) -> dict[str, Any] | None:
        return self.by_attempt.get(attempt_id)

    async def host_for_job(self, *, job_id: str) -> dict[str, Any] | None:
        return self.by_job.get(job_id)


def _base_url(client: Any) -> str:
    """The private base URL a sandbox manager client will actually address."""
    return client._base_url


@pytest.fixture(autouse=True)
def _sandbox_manager_env(monkeypatch):
    monkeypatch.setenv(SANDBOX_MANAGER_TOKEN_ENV, "shared-sandbox-manager-token")
    monkeypatch.delenv(sandbox_manager_routing.ENV_SANDBOX_MANAGER_HOSTS, raising=False)


async def test_two_hosts_two_sandbox_managers():
    """The whole point: each attempt's request goes to its own machine."""
    store = FakeRoutingStore(
        {
            1: {"host": "runner-a", "sandbox_manager_url": "http://sandbox-manager-a:8092"},
            2: {"host": "runner-b", "sandbox_manager_url": "http://sandbox-manager-b:8092"},
        }
    )

    first = await sandbox_manager_for_attempt(store, 1)
    second = await sandbox_manager_for_attempt(store, 2)

    assert _base_url(first) == "http://sandbox-manager-a:8092"
    assert _base_url(second) == "http://sandbox-manager-b:8092"


async def test_an_attempt_without_enrolled_host_ownership_has_no_sandbox_manager():
    assert await sandbox_manager_for_attempt(FakeRoutingStore(), 7) is None
    assert await sandbox_manager_for_attempt(FakeRoutingStore(), None) is None


async def test_a_host_without_a_sandbox_manager_url_is_unroutable():
    store = FakeRoutingStore({1: {"host": "runner-a", "sandbox_manager_url": None}})

    assert await sandbox_manager_for_attempt(store, 1) is None


async def test_a_host_with_a_blank_sandbox_manager_url_is_treated_as_having_none():
    store = FakeRoutingStore({1: {"host": "runner-a", "sandbox_manager_url": "   "}})

    assert await sandbox_manager_for_attempt(store, 1) is None


async def test_no_sandbox_manager_token_means_no_sandbox_manager(monkeypatch):
    """Fail closed rather than sending unauthenticated requests at a host."""
    monkeypatch.delenv(SANDBOX_MANAGER_TOKEN_ENV, raising=False)
    store = FakeRoutingStore(
        {1: {"host": "runner-a", "sandbox_manager_url": "http://sandbox-manager-a:8092"}}
    )

    assert await sandbox_manager_for_attempt(store, 1) is None


async def test_the_job_lookup_routes_by_the_current_attempts_host():
    store = FakeRoutingStore(
        **{"job-1": {"host": "runner-b", "sandbox_manager_url": "http://sandbox-manager-b:8092"}}
    )

    sandbox_manager = await sandbox_manager_for_job(store, "job-1")

    assert _base_url(sandbox_manager) == "http://sandbox-manager-b:8092"


async def test_shared_child_workspace_hydrates_its_db_owned_alias_before_use():
    class SandboxManager:
        def __init__(self) -> None:
            self.registered: list[dict[str, Any]] = []

        async def register_shared_sandbox(self, sandbox: dict[str, Any]):
            self.registered.append(sandbox)

    sandbox_manager = SandboxManager()
    sandbox = {"shared_live": True, "sandbox_id": "asbx_shared"}

    assert (
        await hydrate_shared_sandbox_manager({"thread_sandbox": sandbox}, sandbox_manager)
        is sandbox_manager
    )
    assert sandbox_manager.registered == [sandbox]


async def test_missing_shared_membership_has_no_sandbox_manager_route():
    class SandboxManager:
        async def register_shared_sandbox(self, sandbox):  # pragma: no cover - must stay unused
            raise AssertionError(sandbox)

    sandbox_manager = SandboxManager()

    assert await hydrate_shared_sandbox_manager({"thread_sandbox": None}, sandbox_manager) is None


async def test_a_job_without_enrolled_host_ownership_has_no_sandbox_manager():
    assert await sandbox_manager_for_job(FakeRoutingStore(), "job-unknown") is None
    assert await sandbox_manager_for_attempt(None, 1) is None
    assert await sandbox_manager_for_job(None, "job-1") is None


async def test_every_sandbox_manager_shares_the_one_token_today():
    """Recorded as a test because it is a limitation, not a design.

    Host rows carry a URL and no credential, so a compromised host's sandbox manager
    token opens every other host's sandbox manager. Making that per-host is a
    secret-distribution change (issue at enrollment, rotate, deliver), not a
    routing one. This test is here so the day it changes, it changes visibly.
    """
    store = FakeRoutingStore(
        {
            1: {"host": "runner-a", "sandbox_manager_url": "http://sandbox-manager-a:8092"},
            2: {"host": "runner-b", "sandbox_manager_url": "http://sandbox-manager-b:8092"},
        }
    )

    first = await sandbox_manager_for_attempt(store, 1)
    second = await sandbox_manager_for_attempt(store, 2)

    assert first._headers == second._headers


@pytest.mark.parametrize("route", ["job", "attempt", "host", "probe"])
async def test_live_sandbox_manager_policy_blocks_stored_urls_and_retires_cached_pools(
    monkeypatch, route
):
    """A retained host address must not outlive the policy allowing its credential."""
    row = {"host": "runner-policy", "sandbox_manager_url": "https://excluded.example:8092"}
    store = FakeRoutingStore({1: row}, **{"job-policy": row})
    health_calls: list[str] = []

    async def health(self, *, timeout=3):
        health_calls.append(self._base_url)

    monkeypatch.setattr(sandbox_manager_routing.SandboxManagerClient, "health", health)
    try:
        assert (
            await sandbox_manager_routing.probe_sandbox_manager(
                row["host"], row["sandbox_manager_url"]
            )
            is None
        )
        pool = sandbox_manager_routing._POOLS[row["host"]][1]
        assert row["host"] in sandbox_manager_routing._PROBE_CACHE
        monkeypatch.setenv(sandbox_manager_routing.ENV_SANDBOX_MANAGER_HOSTS, "approved.example")

        if route == "job":
            assert await sandbox_manager_for_job(store, "job-policy") is None
        elif route == "attempt":
            assert await sandbox_manager_for_attempt(store, 1) is None
        elif route == "host":
            assert sandbox_manager_routing.sandbox_manager_for_host(row) is None
        else:
            assert await sandbox_manager_routing.probe_sandbox_manager(
                row["host"], row["sandbox_manager_url"]
            ) == ("sandbox manager is unavailable under the current configuration")
        assert row["host"] not in sandbox_manager_routing._POOLS
        assert row["host"] not in sandbox_manager_routing._PROBE_CACHE
        assert health_calls == [row["sandbox_manager_url"]]
        await sandbox_manager_routing.close_sandbox_manager_clients()
        assert pool.is_closed
    finally:
        await sandbox_manager_routing.close_sandbox_manager_clients()


class TestAdvertisedSandboxManagerUrl:
    """What a runner *reports* is not always what it *uses*.

    Both readings are of one address until the control plane moves to another
    machine. Then the compose service name the runner dials is unresolvable
    from there, and reporting it verbatim stores an address the control plane
    cannot reach — 503 on every terminal, file listing and git status, and jobs
    that die at the first one. The first real job on staging failed three
    times, once per attempt, on exactly this.
    """

    def test_it_reports_the_url_it_uses_when_nothing_says_otherwise(self, monkeypatch) -> None:
        """The co-located shape, and the default: one address, no drift."""
        from cloud_agent_host import runner

        monkeypatch.delenv("AGENT_SANDBOX_MANAGER_ADVERTISE_URL", raising=False)
        monkeypatch.setenv("AGENT_SANDBOX_MANAGER_URL", "http://sandbox-manager:8092")

        assert runner._sandbox_manager_url_from_env() == "http://sandbox-manager:8092"

    def test_an_advertised_url_wins(self, monkeypatch) -> None:
        """The remote shape: dial locally, advertise the tunnel's far end."""
        from cloud_agent_host import runner

        monkeypatch.setenv("AGENT_SANDBOX_MANAGER_URL", "http://sandbox-manager:8092")
        monkeypatch.setenv("AGENT_SANDBOX_MANAGER_ADVERTISE_URL", "http://172.24.0.1:18092")

        assert runner._sandbox_manager_url_from_env() == "http://172.24.0.1:18092"

    def test_a_blank_advertised_url_is_not_an_override(self, monkeypatch) -> None:
        """Compose passes the empty string when the variable is unset.

        `${AGENT_SANDBOX_MANAGER_ADVERTISE_URL:-}` reaches the container as a
        defined, empty variable rather than an absent one. Treating that as an
        override would report no sandbox manager at all on every co-located deployment.
        """
        from cloud_agent_host import runner

        monkeypatch.setenv("AGENT_SANDBOX_MANAGER_URL", "http://sandbox-manager:8092")
        monkeypatch.setenv("AGENT_SANDBOX_MANAGER_ADVERTISE_URL", "   ")

        assert runner._sandbox_manager_url_from_env() == "http://sandbox-manager:8092"


class TestSandboxManagerProbe:
    """Finding out at enrolment instead of at the first job.

    Both ways the first remote host was misconfigured were invisible until a
    job needed the sandbox manager: an address that resolved nowhere, and a shared token
    that did not match. Six minutes and three attempts to surface, with `503`
    as the only clue.
    """

    @pytest.fixture(autouse=True)
    def _token_and_clean_cache(self, monkeypatch):
        monkeypatch.setenv(SANDBOX_MANAGER_TOKEN_ENV, "shared-sandbox-manager-token")
        sandbox_manager_routing._PROBE_CACHE.clear()
        yield
        sandbox_manager_routing._PROBE_CACHE.clear()

    @staticmethod
    def _health(monkeypatch, outcome):
        calls = {"n": 0}

        async def health(self, *, timeout: float = 3) -> None:
            calls["n"] += 1
            if outcome is not None:
                raise outcome

        monkeypatch.setattr(sandbox_manager_routing.SandboxManagerClient, "health", health)
        return calls

    @pytest.mark.asyncio
    async def test_a_reachable_sandbox_manager_is_no_error(self, monkeypatch) -> None:
        self._health(monkeypatch, None)
        assert (
            await sandbox_manager_routing.probe_sandbox_manager("h1", "http://sandbox_manager:8092")
            is None
        )

    @pytest.mark.asyncio
    async def test_an_unreachable_sandbox_manager_is_reported_with_its_address(
        self, monkeypatch
    ) -> None:
        """The address is in the message because the address is the mistake."""
        self._health(monkeypatch, SandboxManagerError(503, "unreachable: [Errno -3] no DNS"))

        error = await sandbox_manager_routing.probe_sandbox_manager(
            "h1", "http://sandbox-manager:8092"
        )

        assert error is not None
        assert "sandbox-manager" in error

    @pytest.mark.asyncio
    async def test_a_refused_credential_is_reported_too(self, monkeypatch) -> None:
        """Why the probe uses an authenticated endpoint.

        The second misconfiguration was a sandbox manager token that did not match. A
        probe against something unauthenticated would have answered "fine" and
        left it to the first job to find out.
        """
        self._health(monkeypatch, SandboxManagerError(401, "invalid sandbox manager credential"))

        error = await sandbox_manager_routing.probe_sandbox_manager(
            "h1", "http://sandbox_manager:8092"
        )

        assert error is not None
        assert "credential" in error

    @pytest.mark.asyncio
    async def test_it_does_not_probe_on_every_heartbeat(self, monkeypatch) -> None:
        """Heartbeats are liveness. They must not wait on a second service."""
        calls = self._health(monkeypatch, None)

        for _ in range(5):
            assert (
                await sandbox_manager_routing.probe_sandbox_manager(
                    "h1", "http://sandbox_manager:8092"
                )
                is None
            )

        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_a_changed_address_is_probed_again_immediately(self, monkeypatch) -> None:
        """The cooldown must not outlive the thing it cached an answer about."""
        calls = self._health(monkeypatch, None)

        await sandbox_manager_routing.probe_sandbox_manager("h1", "http://sandbox_manager:8092")
        await sandbox_manager_routing.probe_sandbox_manager("h1", "http://somewhere-else:8092")

        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_a_failure_is_not_cached(self, monkeypatch) -> None:
        """A sandbox manager still starting must be able to become healthy again."""
        calls = self._health(monkeypatch, SandboxManagerError(503, "starting"))

        await sandbox_manager_routing.probe_sandbox_manager("h1", "http://sandbox_manager:8092")
        await sandbox_manager_routing.probe_sandbox_manager("h1", "http://sandbox_manager:8092")

        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_no_sandbox_manager_url_is_a_readiness_fault(self, monkeypatch) -> None:
        """Every retained host must advertise the sandbox manager that owns its chats."""
        calls = self._health(monkeypatch, None)
        assert await sandbox_manager_routing.probe_sandbox_manager("h1", None) == (
            "host did not advertise a sandbox manager URL"
        )
        assert calls["n"] == 0

    @pytest.mark.asyncio
    async def test_no_control_plane_sandbox_manager_credential_is_a_readiness_fault(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv(SANDBOX_MANAGER_TOKEN_ENV, raising=False)
        calls = self._health(monkeypatch, None)

        assert await sandbox_manager_routing.probe_sandbox_manager(
            "h1", "http://sandbox_manager:8092"
        ) == ("control plane has no sandbox manager credential")
        assert calls["n"] == 0
