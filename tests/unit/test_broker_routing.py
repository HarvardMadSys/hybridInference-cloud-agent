"""Requests reach the enrolled host that owns the retained container.

The bug this prevents is quiet, which is why it gets its own file. With one
With a process-global fallback and several hosts, a file listing for a job on
host B could be answered by host A's broker: not an error, not a 500 — a
plausible directory listing of the wrong container. Everything here is about
the persisted host ownership fence.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.support.storage import AllCapabilities

from cloud_agent import broker_routing
from cloud_agent.broker_routing import broker_for_attempt, broker_for_job, hydrate_shared_broker
from cloud_agent.workspace_broker_client import WorkspaceBrokerError

BROKER_TOKEN_ENV = "AGENT_WORKSPACE_BROKER_TOKEN"


class FakeRoutingStore(AllCapabilities):
    """Answers only the two lookups broker routing performs."""

    def __init__(self, by_attempt: dict[int, dict[str, Any]] | None = None, **by_job: Any) -> None:
        self.by_attempt = by_attempt or {}
        self.by_job = by_job

    async def host_for_attempt(self, *, attempt_id: int) -> dict[str, Any] | None:
        return self.by_attempt.get(attempt_id)

    async def host_for_job(self, *, job_id: str) -> dict[str, Any] | None:
        return self.by_job.get(job_id)


def _base_url(client: Any) -> str:
    """The private base URL a broker client will actually address."""
    return client._base_url


@pytest.fixture(autouse=True)
def _broker_env(monkeypatch):
    monkeypatch.setenv(BROKER_TOKEN_ENV, "shared-broker-token")
    monkeypatch.delenv(broker_routing.ENV_BROKER_HOSTS, raising=False)


async def test_two_hosts_two_brokers():
    """The whole point: each attempt's request goes to its own machine."""
    store = FakeRoutingStore(
        {
            1: {"host": "runner-a", "broker_url": "http://broker-a:8092"},
            2: {"host": "runner-b", "broker_url": "http://broker-b:8092"},
        }
    )

    first = await broker_for_attempt(store, 1)
    second = await broker_for_attempt(store, 2)

    assert _base_url(first) == "http://broker-a:8092"
    assert _base_url(second) == "http://broker-b:8092"


async def test_an_attempt_without_enrolled_host_ownership_has_no_broker():
    assert await broker_for_attempt(FakeRoutingStore(), 7) is None
    assert await broker_for_attempt(FakeRoutingStore(), None) is None


async def test_a_host_without_a_broker_url_is_unroutable():
    store = FakeRoutingStore({1: {"host": "runner-a", "broker_url": None}})

    assert await broker_for_attempt(store, 1) is None


async def test_a_host_with_a_blank_broker_url_is_treated_as_having_none():
    store = FakeRoutingStore({1: {"host": "runner-a", "broker_url": "   "}})

    assert await broker_for_attempt(store, 1) is None


async def test_no_broker_token_means_no_broker(monkeypatch):
    """Fail closed rather than sending unauthenticated requests at a host."""
    monkeypatch.delenv(BROKER_TOKEN_ENV, raising=False)
    store = FakeRoutingStore({1: {"host": "runner-a", "broker_url": "http://broker-a:8092"}})

    assert await broker_for_attempt(store, 1) is None


async def test_the_job_lookup_routes_by_the_current_attempts_host():
    store = FakeRoutingStore(
        **{"job-1": {"host": "runner-b", "broker_url": "http://broker-b:8092"}}
    )

    broker = await broker_for_job(store, "job-1")

    assert _base_url(broker) == "http://broker-b:8092"


async def test_shared_child_workspace_hydrates_its_db_owned_alias_before_use():
    class Broker:
        def __init__(self) -> None:
            self.registered: list[dict[str, Any]] = []

        async def register_shared_sandbox(self, sandbox: dict[str, Any]):
            self.registered.append(sandbox)

    broker = Broker()
    sandbox = {"shared_live": True, "sandbox_id": "asbx_shared"}

    assert await hydrate_shared_broker({"thread_sandbox": sandbox}, broker) is broker
    assert broker.registered == [sandbox]


async def test_missing_shared_membership_has_no_broker_route():
    class Broker:
        async def register_shared_sandbox(self, sandbox):  # pragma: no cover - must stay unused
            raise AssertionError(sandbox)

    broker = Broker()

    assert await hydrate_shared_broker({"thread_sandbox": None}, broker) is None


async def test_a_job_without_enrolled_host_ownership_has_no_broker():
    assert await broker_for_job(FakeRoutingStore(), "job-unknown") is None
    assert await broker_for_attempt(None, 1) is None
    assert await broker_for_job(None, "job-1") is None


async def test_every_broker_shares_the_one_token_today():
    """Recorded as a test because it is a limitation, not a design.

    Host rows carry a URL and no credential, so a compromised host's broker
    token opens every other host's broker. Making that per-host is a
    secret-distribution change (issue at enrollment, rotate, deliver), not a
    routing one. This test is here so the day it changes, it changes visibly.
    """
    store = FakeRoutingStore(
        {
            1: {"host": "runner-a", "broker_url": "http://broker-a:8092"},
            2: {"host": "runner-b", "broker_url": "http://broker-b:8092"},
        }
    )

    first = await broker_for_attempt(store, 1)
    second = await broker_for_attempt(store, 2)

    assert first._headers == second._headers


@pytest.mark.parametrize("route", ["job", "attempt", "host", "probe"])
async def test_live_broker_policy_blocks_stored_urls_and_retires_cached_pools(monkeypatch, route):
    """A retained host address must not outlive the policy allowing its credential."""
    row = {"host": "runner-policy", "broker_url": "https://excluded.example:8092"}
    store = FakeRoutingStore({1: row}, **{"job-policy": row})
    health_calls: list[str] = []

    async def health(self, *, timeout=3):
        health_calls.append(self._base_url)

    monkeypatch.setattr(broker_routing.WorkspaceBrokerClient, "health", health)
    try:
        assert await broker_routing.probe_broker(row["host"], row["broker_url"]) is None
        pool = broker_routing._POOLS[row["host"]][1]
        assert row["host"] in broker_routing._PROBE_CACHE
        monkeypatch.setenv(broker_routing.ENV_BROKER_HOSTS, "approved.example")

        if route == "job":
            assert await broker_for_job(store, "job-policy") is None
        elif route == "attempt":
            assert await broker_for_attempt(store, 1) is None
        elif route == "host":
            assert broker_routing.broker_for_host(row) is None
        else:
            assert await broker_routing.probe_broker(row["host"], row["broker_url"]) == (
                "workspace broker is unavailable under the current configuration"
            )
        assert row["host"] not in broker_routing._POOLS
        assert row["host"] not in broker_routing._PROBE_CACHE
        assert health_calls == [row["broker_url"]]
        await broker_routing.close_broker_clients()
        assert pool.is_closed
    finally:
        await broker_routing.close_broker_clients()


class TestAdvertisedBrokerUrl:
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

        monkeypatch.delenv("AGENT_WORKSPACE_BROKER_ADVERTISE_URL", raising=False)
        monkeypatch.setenv("AGENT_WORKSPACE_BROKER_URL", "http://workspace-broker:8092")

        assert runner._broker_url_from_env() == "http://workspace-broker:8092"

    def test_an_advertised_url_wins(self, monkeypatch) -> None:
        """The remote shape: dial locally, advertise the tunnel's far end."""
        from cloud_agent_host import runner

        monkeypatch.setenv("AGENT_WORKSPACE_BROKER_URL", "http://workspace-broker:8092")
        monkeypatch.setenv("AGENT_WORKSPACE_BROKER_ADVERTISE_URL", "http://172.24.0.1:18092")

        assert runner._broker_url_from_env() == "http://172.24.0.1:18092"

    def test_a_blank_advertised_url_is_not_an_override(self, monkeypatch) -> None:
        """Compose passes the empty string when the variable is unset.

        `${AGENT_WORKSPACE_BROKER_ADVERTISE_URL:-}` reaches the container as a
        defined, empty variable rather than an absent one. Treating that as an
        override would report no broker at all on every co-located deployment.
        """
        from cloud_agent_host import runner

        monkeypatch.setenv("AGENT_WORKSPACE_BROKER_URL", "http://workspace-broker:8092")
        monkeypatch.setenv("AGENT_WORKSPACE_BROKER_ADVERTISE_URL", "   ")

        assert runner._broker_url_from_env() == "http://workspace-broker:8092"


class TestBrokerProbe:
    """Finding out at enrolment instead of at the first job.

    Both ways the first remote host was misconfigured were invisible until a
    job needed the broker: an address that resolved nowhere, and a shared token
    that did not match. Six minutes and three attempts to surface, with `503`
    as the only clue.
    """

    @pytest.fixture(autouse=True)
    def _token_and_clean_cache(self, monkeypatch):
        monkeypatch.setenv(BROKER_TOKEN_ENV, "shared-broker-token")
        broker_routing._PROBE_CACHE.clear()
        yield
        broker_routing._PROBE_CACHE.clear()

    @staticmethod
    def _health(monkeypatch, outcome):
        calls = {"n": 0}

        async def health(self, *, timeout: float = 3) -> None:
            calls["n"] += 1
            if outcome is not None:
                raise outcome

        monkeypatch.setattr(broker_routing.WorkspaceBrokerClient, "health", health)
        return calls

    @pytest.mark.asyncio
    async def test_a_reachable_broker_is_no_error(self, monkeypatch) -> None:
        self._health(monkeypatch, None)
        assert await broker_routing.probe_broker("h1", "http://broker:8092") is None

    @pytest.mark.asyncio
    async def test_an_unreachable_broker_is_reported_with_its_address(self, monkeypatch) -> None:
        """The address is in the message because the address is the mistake."""
        self._health(monkeypatch, WorkspaceBrokerError(503, "unreachable: [Errno -3] no DNS"))

        error = await broker_routing.probe_broker("h1", "http://workspace-broker:8092")

        assert error is not None
        assert "workspace-broker" in error

    @pytest.mark.asyncio
    async def test_a_refused_credential_is_reported_too(self, monkeypatch) -> None:
        """Why the probe uses an authenticated endpoint.

        The second misconfiguration was a broker token that did not match. A
        probe against something unauthenticated would have answered "fine" and
        left it to the first job to find out.
        """
        self._health(monkeypatch, WorkspaceBrokerError(401, "invalid workspace broker credential"))

        error = await broker_routing.probe_broker("h1", "http://broker:8092")

        assert error is not None
        assert "credential" in error

    @pytest.mark.asyncio
    async def test_it_does_not_probe_on_every_heartbeat(self, monkeypatch) -> None:
        """Heartbeats are liveness. They must not wait on a second service."""
        calls = self._health(monkeypatch, None)

        for _ in range(5):
            assert await broker_routing.probe_broker("h1", "http://broker:8092") is None

        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_a_changed_address_is_probed_again_immediately(self, monkeypatch) -> None:
        """The cooldown must not outlive the thing it cached an answer about."""
        calls = self._health(monkeypatch, None)

        await broker_routing.probe_broker("h1", "http://broker:8092")
        await broker_routing.probe_broker("h1", "http://somewhere-else:8092")

        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_a_failure_is_not_cached(self, monkeypatch) -> None:
        """A broker still starting must be able to become healthy again."""
        calls = self._health(monkeypatch, WorkspaceBrokerError(503, "starting"))

        await broker_routing.probe_broker("h1", "http://broker:8092")
        await broker_routing.probe_broker("h1", "http://broker:8092")

        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_no_broker_url_is_a_readiness_fault(self, monkeypatch) -> None:
        """Every retained host must advertise the broker that owns its chats."""
        calls = self._health(monkeypatch, None)
        assert await broker_routing.probe_broker("h1", None) == (
            "host did not advertise a workspace broker URL"
        )
        assert calls["n"] == 0

    @pytest.mark.asyncio
    async def test_no_control_plane_broker_credential_is_a_readiness_fault(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv(BROKER_TOKEN_ENV, raising=False)
        calls = self._health(monkeypatch, None)

        assert await broker_routing.probe_broker("h1", "http://broker:8092") == (
            "control plane has no workspace broker credential"
        )
        assert calls["n"] == 0
