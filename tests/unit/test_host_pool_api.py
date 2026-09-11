"""Enrollment, heartbeat and the pool's admin surface, over HTTP.

The store here is a fake, because what these tests are about is the *contract*:
a replayed enrollment token is refused, a revoked credential stops working, a
busy host is not removable by accident, and an illegal status change is a 409
rather than a silent no-op. The database-backed half — atomicity, the reaper,
slot arithmetic under concurrency — lives in the ``dbtest`` tier.

The admin gate itself is deliberately not the subject: ``verify_admin_access``
is overridden and ``log_admin_action``/``get_client_ip`` are patched, the same
shape ``_build_app`` uses for the moved router. That is what makes these tests
honest about *this* surface while saying nothing about the gate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import admin_hosts, hosts
from cloud_agent.api.deps import get_grant_client, get_storage, verify_admin_access
from cloud_agent.broker_routing import (
    ENV_BROKER_HOSTS,
    BrokerUrlRefused,
    validate_broker_url,
)
from cloud_agent.host_pool import (
    HOST_DISABLED,
    HOST_DRAINING,
    HOST_ENROLLING,
    HOST_OFFLINE,
    HOST_ONLINE,
    hash_secret,
    normalize_capabilities,
    transition_allowed,
)
from tests.support.storage import AllCapabilities

ADMIN = {"Authorization": "Bearer test-admin"}


class FakeHostPoolStore(AllCapabilities):
    """The host-pool capability, in memory.

    Mirrors the store's *decisions*, not its SQL: single-use token consumption,
    the transition table gate, and the busy check on removal all live here in
    the same place they live there, so a route that stops consulting one of
    them fails a test rather than passing a different one.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, dict[str, Any]] = {}
        self.active_host: str | None = None
        self.runtime_versions: dict[str, str] = {}
        self.establish_answer: dict[str, Any] | None = None
        self.linked: list[tuple[str, int, str]] = []
        self.link_ok = True
        self.beat_answers: dict[tuple[str, int], str | None] = {}
        self.release_answer: str | None = None
        self.release_calls: list[tuple[str, int, str, int | None]] = []
        self.sandbox_observations: list[dict[str, Any]] = []
        self.sandbox_stop_due: dict[str, Any] | None = None
        #: History rows per host, as the capacity-history route would read
        #: them out of ``agent_host_capacity_history``.
        self.history: dict[str, list[dict[str, Any]]] = {}

    # -- enrollment tokens

    async def issue_enroll_token(
        self, *, token_hash: str, host: str, ttl_seconds: float, issued_by: str | None = None
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        self.tokens[token_hash] = {"host": host, "expires_at": now + timedelta(seconds=ttl_seconds)}
        return {"expires_at": self.tokens[token_hash]["expires_at"], "created_at": now}

    async def consume_enroll_token(self, *, token_hash: str, host: str) -> bool:
        entry = self.tokens.get(token_hash)
        if entry is None or entry.get("consumed") or entry["expires_at"] <= datetime.now(UTC):
            return False
        # The name is part of the predicate in the store, so a mismatch here
        # must also leave the token unspent rather than burning it.
        if entry["host"] != host:
            return False
        entry["consumed"] = True
        return True

    # -- host rows

    def seed(self, host: str, **overrides: Any) -> dict[str, Any]:
        now = datetime.now(UTC)
        row = {
            "host": host,
            "last_worker_id": None,
            "first_seen_at": now,
            "last_seen_at": now,
            "status": HOST_ONLINE,
            "labels": {},
            "capabilities": [],
            "slots_total": None,
            "slots_used": 0,
            "runner_version": None,
            "broker_url": None,
            "last_heartbeat_at": None,
            "sandbox_runtime": None,
            "sandbox_image_digest": None,
            "runtime_versions": None,
            "runtime_versions_available": None,
            "capacity_snapshot": None,
            "capacity_received_at": None,
            "capacity_sampled_at": None,
            "capacity_sample_id": None,
            "capacity_epoch": uuid4(),
            "enrolled": False,
            "credential_hash": None,
        }
        row.update(overrides)
        self.rows[host] = row
        return row

    async def register_host(
        self,
        *,
        host: str,
        credential_hash: str,
        capabilities: list[str] | None = None,
        slots_total: int | None = None,
        broker_url: str | None = None,
        runner_version: str | None = None,
        labels: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.seed(
            host,
            status=HOST_ENROLLING,
            capabilities=normalize_capabilities(capabilities),
            slots_total=slots_total,
            broker_url=broker_url,
            runner_version=runner_version,
            labels=labels or {},
            credential_hash=credential_hash,
            enrolled=True,
            # Re-enrollment rotates the epoch and clears the observation, the
            # same two facts the real upsert writes.
            capacity_epoch=uuid4(),
        )

    async def authenticate_host(self, *, host: str, credential_hash: str) -> dict[str, Any] | None:
        row = self.rows.get(host)
        if row is None or row["status"] == HOST_DISABLED:
            return None
        if not row["credential_hash"] or row["credential_hash"] != credential_hash:
            return None
        return dict(row)

    async def record_host_heartbeat(
        self,
        *,
        host: str,
        credential_hash: str | None = None,
        capacity: dict[str, Any] | None = None,
        capacity_sample_age_ms: int | None = None,
        physical_host: str | None = None,
        slots_total: int | None = None,
        runner_version: str | None = None,
        broker_url: str | None = None,
        capabilities: list[str] | None = None,
        sandbox_runtime: str | None = None,
        sandbox_image_digest: str | None = None,
        runtime_versions: dict[str, str] | None = None,
        runtime_versions_available: dict[str, list[str]] | None = None,
    ) -> dict[str, Any] | None:
        row = self.rows.get(host)
        if row is None:
            return None
        if row["status"] in (HOST_ENROLLING, HOST_OFFLINE):
            row["status"] = HOST_ONLINE
        row["last_heartbeat_at"] = datetime.now(UTC)
        for field, value in (
            ("slots_total", slots_total),
            ("runner_version", runner_version),
            ("broker_url", broker_url),
            # "" and {} overwrite — only None means "not reported", exactly
            # the COALESCE the real store applies.
            ("sandbox_runtime", sandbox_runtime),
            ("sandbox_image_digest", sandbox_image_digest),
            ("runtime_versions", runtime_versions),
            ("runtime_versions_available", runtime_versions_available),
        ):
            if value is not None:
                row[field] = value
        if physical_host is not None:
            row["labels"]["physical_host"] = physical_host
        if capabilities is not None:
            row["capabilities"] = normalize_capabilities(capabilities)
        if capacity is not None and capacity_sample_age_ms is not None:
            self._apply_capacity(
                row,
                credential_hash=credential_hash,
                capacity=capacity,
                sample_age_ms=capacity_sample_age_ms,
            )
        return dict(row)

    def _apply_capacity(
        self,
        row: dict[str, Any],
        *,
        credential_hash: str | None,
        capacity: dict[str, Any],
        sample_age_ms: int,
    ) -> None:
        """Mirror the store's three acceptance conditions, in memory.

        A stale credential, a sample this host has already stored, and an
        observation older than the one on the row are each refused; the test
        tier that proves it in SQL lives in the ``dbtest`` suite.
        """
        if credential_hash is None or row["credential_hash"] != credential_hash:
            return
        sample_id = str(capacity["sample_id"])
        if row["capacity_sample_id"] == sample_id:
            return
        received_at = datetime.now(UTC)
        sampled_at = received_at - timedelta(milliseconds=sample_age_ms)
        if row["capacity_sampled_at"] is not None and row["capacity_sampled_at"] >= sampled_at:
            return
        row["capacity_snapshot"] = capacity
        row["capacity_received_at"] = received_at
        row["capacity_sampled_at"] = sampled_at
        row["capacity_sample_id"] = sample_id

    async def get_host(self, *, host: str) -> dict[str, Any] | None:
        row = self.rows.get(host)
        return None if row is None else dict(row)

    async def set_host_status(self, *, host: str, status: str) -> str:
        row = self.rows.get(host)
        if row is None:
            return "unknown"
        if not transition_allowed(row["status"], status):
            return "invalid"
        row["status"] = status
        return "ok"

    async def remove_host(self, *, host: str, force: bool = False) -> str:
        row = self.rows.get(host)
        if row is None:
            return "unknown"
        if row["slots_used"] > 0 and not force:
            return "busy"
        row["status"] = HOST_DISABLED
        row["credential_hash"] = None
        row["enrolled"] = False
        return "ok"

    async def runner_pool(
        self,
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, str], datetime]:
        rows = [{**row, "is_active": row["host"] == self.active_host} for row in self.rows.values()]
        return rows, self.active_host, dict(self.runtime_versions), datetime.now(UTC)

    async def host_capacity_history(
        self, *, host: str, range_seconds: int
    ) -> dict[str, Any] | None:
        row = self.rows.get(host)
        if row is None:
            return None
        until = datetime.now(UTC).replace(second=0, microsecond=0)
        return {
            "capacity_epoch": row["capacity_epoch"],
            "since": until - timedelta(seconds=range_seconds),
            "until": until,
            "observed_at": datetime.now(UTC),
            "rows": self.history.get(host, []),
        }

    async def set_runtime_version(self, *, runtime: str, version: str | None) -> bool:
        if version is None:
            self.runtime_versions.pop(runtime, None)
            return True
        offered = (
            version in ((row.get("runtime_versions_available") or {}).get(runtime) or [])
            for row in self.rows.values()
        )
        if not any(offered):
            return False
        self.runtime_versions[runtime] = version
        return True

    # -- thread residencies
    #
    # Scripted, not simulated: the earn-gate, generation fencing and host
    # scoping are SQL decisions pinned by the ``dbtest`` tier. What these
    # answers exercise is the endpoint's *grant choreography* around whatever
    # the store said — mint on establish, revoke the predecessor, renew on the
    # beat that extends, revoke on release.

    async def establish_residency(self, **_kwargs: Any) -> dict[str, Any] | None:
        return self.establish_answer

    async def link_residency_grant(self, *, thread_id: str, generation: int, grant_id: str) -> bool:
        self.linked.append((thread_id, generation, grant_id))
        return self.link_ok

    async def heartbeat_residency(
        self, *, thread_id: str, generation: int, host: str, ttl_seconds: float
    ) -> str | None:
        return self.beat_answers.get((thread_id, generation))

    async def release_residency(self, *, thread_id: str, generation: int, host: str) -> str | None:
        self.release_calls.append((thread_id, generation, host, None))
        return self.release_answer

    async def release_shared_residency_if_quiescent(
        self,
        *,
        thread_id: str,
        generation: int,
        process_generation: int,
        host: str,
        sandbox_id: str,
    ) -> str | None:
        self.release_calls.append((thread_id, generation, host, process_generation))
        return self.release_answer

    async def reconcile_thread_sandbox_observation(self, **kwargs: Any) -> str | None:
        self.sandbox_observations.append(dict(kwargs))
        return "running" if kwargs["running"] else "stopped"

    async def due_thread_sandbox_stop(self, **_kwargs: Any) -> dict[str, Any] | None:
        return self.sandbox_stop_due


class RecordingGrants:
    """The grant client's shape, recording every call; mint can be scripted."""

    def __init__(self) -> None:
        self.minted: list[dict[str, Any]] = []
        self.renewed: list[str] = []
        self.revoked: list[str] = []
        self.fail_mint = False

    async def mint(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail_mint:
            raise RuntimeError("gateway is down")
        self.minted.append(kwargs)
        n = len(self.minted)
        return {"grant_id": f"agr-{n}", "token": f"tok-{n}"}

    async def renew(self, grant_id: str) -> dict[str, Any]:
        self.renewed.append(grant_id)
        return {}

    async def revoke(self, grant_id: str) -> bool:
        self.revoked.append(grant_id)
        return True


def _build_app(store: FakeHostPoolStore) -> FastAPI:
    app = FastAPI()
    app.include_router(hosts.router)
    app.include_router(admin_hosts.router)
    app.dependency_overrides[get_storage] = lambda: store
    # Supplied rather than left real because the subject here is the pool, not
    # the admin gate; ``test_api_auth_deps.py`` is where the gate is pinned.
    app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
    return app


@pytest_asyncio.fixture()
async def client(monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "unit-test-control-secret")
    monkeypatch.setenv("AGENT_WORKSPACE_BROKER_TOKEN", "unit-test-broker-secret")

    async def _no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(admin_hosts, "log_admin_action", _no_audit)
    monkeypatch.setattr(admin_hosts, "get_client_ip", lambda _request: "127.0.0.1")

    store = FakeHostPoolStore()
    transport = ASGITransport(app=_build_app(store))
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client, store


@pytest_asyncio.fixture()
async def residency_client(monkeypatch):
    """Like ``client``, plus a recording grant client behind the dependency."""
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "unit-test-control-secret")
    monkeypatch.setenv("AGENT_WORKSPACE_BROKER_TOKEN", "unit-test-broker-secret")

    async def _no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(admin_hosts, "log_admin_action", _no_audit)
    monkeypatch.setattr(admin_hosts, "get_client_ip", lambda _request: "127.0.0.1")

    store = FakeHostPoolStore()
    grants = RecordingGrants()
    app = _build_app(store)
    app.dependency_overrides[get_grant_client] = lambda: grants
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client, store, grants


def _registration_body(token: str, name: str, **overrides) -> dict[str, Any]:
    """Build the minimum retained-host enrollment payload."""
    return {
        "enroll_token": token,
        "name": name,
        "slots_total": 3,
        "broker_url": "http://broker.test:8092",
        **overrides,
    }


async def _enroll(client, store, name="runner-a", **body) -> str:
    """Issue a token for ``name``, redeem it, and return the host credential."""
    issued = await client.post(
        "/admin/agent/runner-hosts/enroll-token", json={"host": name}, headers=ADMIN
    )
    assert issued.status_code == 200
    response = await client.post(
        "/v1/agent/host/register",
        json=_registration_body(issued.json()["enroll_token"], name, **body),
    )
    assert response.status_code == 200, response.text
    return response.json()["credential"]


# ── G2: enrollment ─────────────────────────────────────────────────────


async def test_enrollment_returns_a_credential_and_starts_the_host_enrolling(client):
    async_client, store = client

    credential = await _enroll(async_client, store, slots_total=2, capabilities=["kata"])

    assert credential
    row = store.rows["runner-a"]
    # Not online. Registration proves somebody held a valid token, not that the
    # machine is up — the heartbeat is what says that.
    assert row["status"] == HOST_ENROLLING
    assert row["capabilities"] == ["kata"]
    assert row["slots_total"] == 2


def test_only_the_hash_of_a_credential_is_kept():
    """Guarded here as well as in the store: the row must not be the secret."""
    store = FakeHostPoolStore()
    row = store.seed("runner-a", credential_hash=hash_secret("secret-value"))
    assert "secret-value" not in str(row)


async def test_a_replayed_enrollment_token_is_refused(client):
    """Single-use, or a token pasted into a runbook enrols a fleet.

    Both halves present the *same* name, which is what isolates single-use now
    that the token names its host: a second name would be refused by the
    binding and this test would pass without proving anything about replay.
    """
    async_client, _ = client
    issued = await async_client.post(
        "/admin/agent/runner-hosts/enroll-token", json={"host": "runner-a"}, headers=ADMIN
    )
    token = issued.json()["enroll_token"]

    first = await async_client.post(
        "/v1/agent/host/register", json=_registration_body(token, "runner-a")
    )
    second = await async_client.post(
        "/v1/agent/host/register", json=_registration_body(token, "runner-a")
    )

    assert first.status_code == 200
    assert second.status_code == 401
    assert (
        "runner-a" not in second.text or second.json()["detail"]["error"]["type"] == "unauthorized"
    )


async def test_an_invalid_broker_url_does_not_spend_the_enrollment_token(client):
    """A retryable host configuration typo must not require a new admin token."""
    async_client, store = client
    issued = await async_client.post(
        "/admin/agent/runner-hosts/enroll-token", json={"host": "runner-a"}, headers=ADMIN
    )
    token = issued.json()["enroll_token"]

    refused = await async_client.post(
        "/v1/agent/host/register",
        json=_registration_body(token, "runner-a", broker_url="file:///etc/passwd"),
    )
    retry = await async_client.post(
        "/v1/agent/host/register", json=_registration_body(token, "runner-a")
    )

    assert refused.status_code == 400
    assert refused.json()["detail"]["error"]["type"] == "invalid_broker_url"
    assert retry.status_code == 200
    assert store.rows["runner-a"]["broker_url"] == "http://broker.test:8092"


async def test_a_typo_cannot_evict_a_live_host(client):
    """The eviction the name binding exists to prevent.

    Registration upserts. Before the token named its host, a machine being set
    up as ``runner-b`` that was typed as ``runner-a`` revoked the running
    ``runner-a``'s credential and reset its row to ``enrolling`` — so a live
    machine holding two attempts stopped authenticating, stopped heartbeating
    and stopped being claimable, while still running the work it had leased.

    The token now names the machine an admin decided to enrol, so the typo is
    refused and the incumbent is untouched.
    """
    async_client, store = client
    store.seed(
        "runner-a",
        status=HOST_ONLINE,
        credential_hash=hash_secret("runner-a-lives-here"),
        enrolled=True,
        slots_used=2,
        slots_total=4,
    )
    issued = await async_client.post(
        "/admin/agent/runner-hosts/enroll-token", json={"host": "runner-b"}, headers=ADMIN
    )
    token = issued.json()["enroll_token"]

    typo = await async_client.post(
        "/v1/agent/host/register", json=_registration_body(token, "runner-a")
    )

    assert typo.status_code == 401
    incumbent = store.rows["runner-a"]
    assert incumbent["status"] == HOST_ONLINE
    assert incumbent["credential_hash"] == hash_secret("runner-a-lives-here")
    assert incumbent["slots_used"] == 2
    # And the token survives the typo, so the machine it was minted for can
    # still use it — a spent token would mean re-issuing one per mistake.
    retry = await async_client.post(
        "/v1/agent/host/register", json=_registration_body(token, "runner-b")
    )
    assert retry.status_code == 200
    assert store.rows["runner-b"]["status"] == HOST_ENROLLING


async def test_a_token_admits_only_the_name_it_was_issued_for(client):
    """Even when that name belongs to nobody: the token is the authority."""
    async_client, store = client
    issued = await async_client.post(
        "/admin/agent/runner-hosts/enroll-token", json={"host": "runner-b"}, headers=ADMIN
    )

    response = await async_client.post(
        "/v1/agent/host/register",
        json=_registration_body(issued.json()["enroll_token"], "runner-c"),
    )

    assert response.status_code == 401
    assert store.rows == {}


async def test_the_issued_token_reports_the_host_it_is_bound_to(client):
    """The operator has to be able to see what they just authorized."""
    async_client, _ = client

    issued = await async_client.post(
        "/admin/agent/runner-hosts/enroll-token", json={"host": "runner-b"}, headers=ADMIN
    )

    assert issued.json()["host"] == "runner-b"


async def test_an_enrollment_token_request_must_name_a_host(client):
    """No default, and no 'any machine' token to leave lying around."""
    async_client, _ = client

    assert (
        await async_client.post("/admin/agent/runner-hosts/enroll-token", headers=ADMIN)
    ).status_code == 422


async def test_a_rebuilt_machine_re_enrols_under_its_own_name(client):
    """The case the upsert exists for, and which refusing names in use breaks.

    A machine that was rebuilt comes back as itself: the admin issues a token
    naming it, and the row is rewritten with a fresh credential. That is a
    decision somebody made, which is the whole difference from the typo above.
    """
    async_client, store = client
    old = await _enroll(async_client, store, name="runner-a")

    new = await _enroll(async_client, store, name="runner-a")

    assert old != new
    assert store.rows["runner-a"]["credential_hash"] == hash_secret(new)


async def test_an_expired_enrollment_token_is_refused(client):
    async_client, store = client
    issued = await async_client.post(
        "/admin/agent/runner-hosts/enroll-token",
        json={"host": "runner-a", "ttl_seconds": 1},
        headers=ADMIN,
    )
    token = issued.json()["enroll_token"]
    # Age it rather than sleeping: the property is the expiry check, not the clock.
    store.tokens[hash_secret(token)]["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)

    response = await async_client.post(
        "/v1/agent/host/register", json=_registration_body(token, "runner-a")
    )

    assert response.status_code == 401


async def test_an_invented_enrollment_token_is_refused(client):
    async_client, _ = client

    response = await async_client.post(
        "/v1/agent/host/register", json=_registration_body("made-up", "runner-a")
    )

    assert response.status_code == 401


async def test_the_enrollment_token_is_never_shown_twice(client):
    """Two calls must mint two tokens; there is no 'show me that one again'."""
    async_client, _ = client

    first = await async_client.post(
        "/admin/agent/runner-hosts/enroll-token", json={"host": "runner-a"}, headers=ADMIN
    )
    second = await async_client.post(
        "/admin/agent/runner-hosts/enroll-token", json={"host": "runner-a"}, headers=ADMIN
    )

    assert first.json()["enroll_token"] != second.json()["enroll_token"]


@pytest.mark.parametrize(
    "path",
    [
        "/admin/agent/runner-hosts/enroll-token",
        "/admin/agent/runner-hosts/{host}/drain",
        "/admin/agent/runner-hosts/{host}/undrain",
        "/admin/agent/runner-hosts/{host}/remove",
    ],
)
def test_every_pool_admin_route_declares_the_admin_gate(path):
    """Asserted structurally, because the gate itself is still E7's placeholder.

    Sending an unauthenticated request would only prove that a
    ``NotImplementedError`` propagates. What is checkable now — and what would
    actually go wrong if a new route were added without thinking — is whether
    ``verify_admin_access`` is in the route's dependency tree at all. Minting
    enrollment tokens or revoking a host from an unauthenticated call is the
    failure this catches.
    """
    # Read off the router itself: this FastAPI version keeps included routers
    # nested under the app rather than flattening them into ``app.routes``.
    route = next(r for r in admin_hosts.router.routes if getattr(r, "path", None) == path)

    assert any(
        dependency.call is verify_admin_access for dependency in route.dependant.dependencies
    ), path


# ── G3: heartbeat ──────────────────────────────────────────────────────


async def test_the_first_heartbeat_puts_an_enrolling_host_online(client):
    async_client, store = client
    credential = await _enroll(async_client, store)

    response = await async_client.post(
        "/v1/agent/host/heartbeat", json={}, headers={"Authorization": f"Bearer {credential}"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == HOST_ONLINE
    assert store.rows["runner-a"]["last_heartbeat_at"] is not None


async def test_a_heartbeat_brings_an_offline_host_back(client):
    """A machine returning from a partition needs no operator."""
    async_client, store = client
    credential = await _enroll(async_client, store)
    store.rows["runner-a"]["status"] = HOST_OFFLINE

    response = await async_client.post(
        "/v1/agent/host/heartbeat", json={}, headers={"Authorization": f"Bearer {credential}"}
    )

    assert response.json()["status"] == HOST_ONLINE


async def test_a_heartbeat_does_not_undo_a_drain(client):
    """Otherwise drain is a suggestion the host can decline by staying alive."""
    async_client, store = client
    credential = await _enroll(async_client, store)
    store.rows["runner-a"]["status"] = HOST_DRAINING

    response = await async_client.post(
        "/v1/agent/host/heartbeat", json={}, headers={"Authorization": f"Bearer {credential}"}
    )

    assert response.json()["status"] == HOST_DRAINING


async def test_a_heartbeat_only_overwrites_what_it_carries(client):
    """A liveness-only beat must not blank what enrollment declared."""
    async_client, store = client
    credential = await _enroll(async_client, store, slots_total=4, capabilities=["kata"])

    await async_client.post(
        "/v1/agent/host/heartbeat", json={}, headers={"Authorization": f"Bearer {credential}"}
    )

    assert store.rows["runner-a"]["slots_total"] == 4
    assert store.rows["runner-a"]["capabilities"] == ["kata"]


async def test_a_heartbeat_can_update_capacity_and_version(client):
    async_client, store = client
    credential = await _enroll(async_client, store, slots_total=1)

    response = await async_client.post(
        "/v1/agent/host/heartbeat",
        json={"slots_total": 6, "runner_version": "1.4.0", "broker_url": "http://broker-a:8092"},
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.json()["slots_total"] == 6
    assert store.rows["runner-a"]["runner_version"] == "1.4.0"
    assert store.rows["runner-a"]["broker_url"] == "http://broker-a:8092"


async def test_a_heartbeat_carries_the_version_stamp_to_the_admin_page(client):
    """What runs on a host — runtime, image, CLI versions — is heartbeat-borne.

    ``sandbox_runtime: ""`` is the interesting value: it is the host positively
    attesting shared-kernel runc, and it must survive to the admin surface
    rather than collapsing into "never reported".
    """
    async_client, store = client
    credential = await _enroll(async_client, store)

    await async_client.post(
        "/v1/agent/host/heartbeat",
        json={
            "sandbox_runtime": "",
            "sandbox_image_digest": "sha256:abc123",
            "runtime_versions": {"claude-code": "1.2.3"},
        },
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert store.rows["runner-a"]["sandbox_runtime"] == ""
    assert store.rows["runner-a"]["sandbox_image_digest"] == "sha256:abc123"
    assert store.rows["runner-a"]["runtime_versions"] == {"claude-code": "1.2.3"}
    listed = (await async_client.get("/admin/agent/runner-hosts", headers=ADMIN)).json()
    (shown,) = [h for h in listed["hosts"] if h["host"] == "runner-a"]
    assert shown["sandbox_runtime"] == ""
    assert shown["sandbox_image_digest"] == "sha256:abc123"
    assert shown["runtime_versions"] == {"claude-code": "1.2.3"}


async def test_physical_server_metadata_round_trips_without_changing_identity(client):
    async_client, store = client
    credential = await _enroll(async_client, store)
    auth = {"Authorization": f"Bearer {credential}"}
    response = await async_client.post(
        "/v1/agent/host/heartbeat", json={"physical_host": "gpu-box"}, headers=auth
    )
    assert response.status_code == 200
    await async_client.post("/v1/agent/host/heartbeat", json={}, headers=auth)
    listed = (await async_client.get("/admin/agent/runner-hosts", headers=ADMIN)).json()
    (shown,) = [h for h in listed["hosts"] if h["host"] == "runner-a"]
    assert shown["labels"]["physical_host"] == "gpu-box"
    assert (
        await store.authenticate_host(host="gpu-box", credential_hash=hash_secret(credential))
        is None
    )


async def test_a_stampless_heartbeat_keeps_the_last_stamp(client):
    """A runner predating the stamp fields must not blank a recorded one."""
    async_client, store = client
    credential = await _enroll(async_client, store)
    await async_client.post(
        "/v1/agent/host/heartbeat",
        json={"sandbox_runtime": "io.containerd.kata.v2", "runtime_versions": {}},
        headers={"Authorization": f"Bearer {credential}"},
    )

    await async_client.post(
        "/v1/agent/host/heartbeat", json={}, headers={"Authorization": f"Bearer {credential}"}
    )

    assert store.rows["runner-a"]["sandbox_runtime"] == "io.containerd.kata.v2"
    assert store.rows["runner-a"]["runtime_versions"] == {}


async def test_a_heartbeat_without_a_credential_is_refused(client):
    async_client, _ = client

    assert (await async_client.post("/v1/agent/host/heartbeat", json={})).status_code == 401


async def test_a_revoked_credential_stops_working(client):
    """The point of storing a hash: revocation has to act on something."""
    async_client, store = client
    credential = await _enroll(async_client, store)
    auth = {"Authorization": f"Bearer {credential}"}
    assert (
        await async_client.post("/v1/agent/host/heartbeat", json={}, headers=auth)
    ).status_code == 200

    await async_client.post("/admin/agent/runner-hosts/runner-a/remove", headers=ADMIN)

    assert (
        await async_client.post("/v1/agent/host/heartbeat", json={}, headers=auth)
    ).status_code == 401


async def test_a_superseded_credential_stops_working(client):
    """Re-enrolling a rebuilt machine must invalidate the old copy."""
    async_client, store = client
    old = await _enroll(async_client, store)
    new = await _enroll(async_client, store)

    stale = await async_client.post(
        "/v1/agent/host/heartbeat", json={}, headers={"Authorization": f"Bearer {old}"}
    )
    fresh = await async_client.post(
        "/v1/agent/host/heartbeat", json={}, headers={"Authorization": f"Bearer {new}"}
    )

    assert stale.status_code == 401
    assert fresh.status_code == 200


async def test_a_deployment_with_no_signing_secret_refuses_rather_than_500s(client, monkeypatch):
    """The secret is what verifies the credential; without it nothing does.

    Raising out of the dependency would turn a configuration problem into a
    stack trace on every heartbeat, and a 500 reads as "the control plane is
    broken" rather than "this credential does not authenticate here" — which
    is what it is.
    """
    async_client, store = client
    credential = await _enroll(async_client, store)
    monkeypatch.delenv("AGENT_CONTROL_TOKEN_SECRET", raising=False)

    response = await async_client.post(
        "/v1/agent/host/heartbeat", json={}, headers={"Authorization": f"Bearer {credential}"}
    )

    assert response.status_code == 401


async def test_another_hosts_credential_does_not_authenticate_this_one(client):
    async_client, store = client
    await _enroll(async_client, store, name="runner-a")
    credential_b = await _enroll(async_client, store, name="runner-b")

    response = await async_client.post(
        "/v1/agent/host/heartbeat", json={}, headers={"Authorization": f"Bearer {credential_b}"}
    )

    # It authenticates runner-b, and only runner-b.
    assert response.json()["host"] == "runner-b"


# ── G4: the claim takes its host from the credential ───────────────────


class ClaimRecorder(FakeHostPoolStore):
    """A pool store that also records what ``claim_job`` was asked for.

    Returns ``None`` from the claim so the route stops there — everything after
    it (token minting, entitlement, clone credentials) belongs to other tasks
    and would only make this test about them.
    """

    def __init__(self) -> None:
        super().__init__()
        self.claims: list[dict[str, Any]] = []

    async def claim_job(
        self,
        *,
        worker_id: str,
        lease_ttl_seconds: float,
        host: str,
        host_credential_hash: str,
        claim_attestation: dict[str, Any] | None = None,
        expected_deployment_sha: str | None = None,
    ):
        self.claims.append(
            {
                "worker_id": worker_id,
                "host": host,
                "host_credential_hash": host_credential_hash,
                "claim_attestation": claim_attestation,
                "expected_deployment_sha": expected_deployment_sha,
            }
        )
        return None


@pytest_asyncio.fixture()
async def claim_client(monkeypatch):
    """The worker router using its real enrolled-host claim authenticator."""
    from cloud_agent.api import routes
    from cloud_agent.api.deps import get_agent_app_credentials

    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "unit-test-control-secret")
    monkeypatch.setenv("AGENT_WORKSPACE_BROKER_TOKEN", "unit-test-broker-secret")
    monkeypatch.setenv("AGENT_DEPLOYMENT_SHA", "deployment-under-test")

    store = ClaimRecorder()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_agent_app_credentials] = lambda: None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client, store


async def test_a_host_credential_names_the_claiming_machine(claim_client):
    """The claim reads the machine name only from its enrolled credential."""
    async_client, store = claim_client
    from cloud_agent.tokens import mint_host_token

    credential = mint_host_token(host="runner-a")
    store.seed("runner-a", credential_hash=hash_secret(credential), enrolled=True)

    response = await async_client.post(
        "/v1/agent/worker/claim",
        json={
            "worker_id": "w1",
            "attestation": {
                "sandbox_runtime": "io.containerd.kata.v2",
                "host_kernel": "host",
                "guest_kernel": "guest",
            },
        },
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200
    assert store.claims == [
        {
            "worker_id": "w1",
            "host": "runner-a",
            "host_credential_hash": hash_secret(credential),
            "expected_deployment_sha": "deployment-under-test",
            "claim_attestation": {
                "sandbox_runtime": "io.containerd.kata.v2",
                "sandbox_image_digest": None,
                "runtime_versions": {},
                # Present even when the request omitted it: the claim checks a
                # stored selection against this, and an absent key would be
                # indistinguishable from "this host offers nothing".
                "runtime_versions_available": {},
                # Non-CLI helpers (currently the Claude Agent SDK fork
                # helper) are attested separately from runtime binaries.
                "helper_versions": {},
                # Shared-live claims carry a process-local protocol/version
                # fence; omitted values normalize to safe non-attestation.
                "shared_sandbox_protocol": None,
                "runner_capabilities": [],
                "host_kernel": "host",
                "guest_kernel": "guest",
            },
        }
    ]


async def test_request_body_host_identity_is_rejected(claim_client):
    """A stale or malicious caller cannot supply a host name in JSON."""
    async_client, store = claim_client
    from cloud_agent.tokens import mint_host_token

    credential = mint_host_token(host="runner-a")
    store.seed("runner-a", credential_hash=hash_secret(credential), enrolled=True)

    response = await async_client.post(
        "/v1/agent/worker/claim",
        json={
            "worker_id": "w1",
            "host": "runner-b",
        },
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 422
    assert store.claims == []


async def test_pool_wide_token_is_not_a_claim_credential(claim_client):
    """Pool-wide bearer strings no longer open the cross-tenant claim route."""
    async_client, store = claim_client

    response = await async_client.post(
        "/v1/agent/worker/claim",
        json={"worker_id": "w1"},
        headers={"Authorization": "Bearer pool-wide-token"},
    )

    assert response.status_code == 401
    assert store.claims == []


async def test_a_revoked_host_credential_cannot_claim(claim_client):
    """Revocation has to reach the claim, not just the heartbeat."""
    async_client, store = claim_client
    from cloud_agent.tokens import mint_host_token

    credential = mint_host_token(host="runner-a")
    store.seed("runner-a", credential_hash=None, status=HOST_DISABLED)

    response = await async_client.post(
        "/v1/agent/worker/claim",
        json={"worker_id": "w1"},
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 401
    assert store.claims == []


async def test_a_host_credential_for_an_unknown_host_cannot_claim(claim_client):
    """Signed by us, naming nothing — a forgotten or never-registered machine."""
    async_client, store = claim_client
    from cloud_agent.tokens import mint_host_token

    response = await async_client.post(
        "/v1/agent/worker/claim",
        json={"worker_id": "w1"},
        headers={"Authorization": f"Bearer {mint_host_token(host='ghost')}"},
    )

    assert response.status_code == 401
    assert store.claims == []


# ── G6: drain, undrain, remove ─────────────────────────────────────────


async def test_draining_stops_new_claims_without_touching_the_running_ones(client):
    async_client, store = client
    store.seed("runner-a", status=HOST_ONLINE, slots_used=2, slots_total=4)

    response = await async_client.post("/admin/agent/runner-hosts/runner-a/drain", headers=ADMIN)

    assert response.status_code == 200
    assert store.rows["runner-a"]["status"] == HOST_DRAINING
    # The attempts it is holding are untouched — that is the difference between
    # draining and removing.
    assert store.rows["runner-a"]["slots_used"] == 2


async def test_a_drained_host_can_be_put_back(client):
    async_client, store = client
    store.seed("runner-a", status=HOST_DRAINING)

    response = await async_client.post("/admin/agent/runner-hosts/runner-a/undrain", headers=ADMIN)

    assert response.status_code == 200
    assert store.rows["runner-a"]["status"] == HOST_ONLINE


async def test_an_illegal_transition_is_a_409_naming_the_current_state(client):
    """Draining a disabled host is refused, and the refusal says why."""
    async_client, store = client
    store.seed("runner-a", status=HOST_DISABLED)

    response = await async_client.post("/admin/agent/runner-hosts/runner-a/drain", headers=ADMIN)

    assert response.status_code == 409
    assert HOST_DISABLED in response.json()["detail"]


async def test_draining_an_unknown_host_is_a_404(client):
    async_client, _ = client

    response = await async_client.post("/admin/agent/runner-hosts/nope/drain", headers=ADMIN)

    assert response.status_code == 404


async def test_removing_a_busy_host_is_refused(client):
    """Its running attempts belong to a machine that would just have vanished."""
    async_client, store = client
    store.seed("runner-a", slots_used=1)

    response = await async_client.post("/admin/agent/runner-hosts/runner-a/remove", headers=ADMIN)

    assert response.status_code == 409
    assert "force=true" in response.json()["detail"]
    assert store.rows["runner-a"]["status"] == HOST_ONLINE


async def test_removing_an_idle_host_revokes_it(client):
    async_client, store = client
    store.seed("runner-a", credential_hash="abc", enrolled=True)

    response = await async_client.post("/admin/agent/runner-hosts/runner-a/remove", headers=ADMIN)

    assert response.status_code == 200
    assert store.rows["runner-a"]["status"] == HOST_DISABLED
    assert store.rows["runner-a"]["credential_hash"] is None


async def test_forced_removal_takes_a_busy_host_out_anyway(client):
    async_client, store = client
    store.seed("runner-a", slots_used=2, credential_hash="abc")

    response = await async_client.post(
        "/admin/agent/runner-hosts/runner-a/remove?force=true", headers=ADMIN
    )

    assert response.status_code == 200
    assert store.rows["runner-a"]["status"] == HOST_DISABLED
    assert store.rows["runner-a"]["credential_hash"] is None


async def test_removing_leaves_the_row_so_attempts_still_name_their_machine(client):
    """``remove`` is not ``forget``. 'Where did this job run' stays answerable."""
    async_client, store = client
    store.seed("runner-a")

    await async_client.post("/admin/agent/runner-hosts/runner-a/remove", headers=ADMIN)

    assert "runner-a" in store.rows


async def test_the_pool_listing_carries_the_new_fields(client):
    async_client, store = client
    store.seed(
        "runner-a",
        status=HOST_DRAINING,
        capabilities=["kata"],
        slots_total=4,
        slots_used=1,
        broker_url="http://broker-a:8092",
        enrolled=True,
    )

    body = (await async_client.get("/admin/agent/runner-hosts", headers=ADMIN)).json()

    row = body["hosts"][0]
    assert row["status"] == HOST_DRAINING
    assert row["capabilities"] == ["kata"]
    assert (row["slots_total"], row["slots_used"]) == (4, 1)
    assert row["broker_url"] == "http://broker-a:8092"
    assert row["enrolled"] is True


@pytest.mark.parametrize(
    "row",
    [
        # Exactly the shape #1158's tests build, and the shape a pre-migration
        # row deserializes to. It has to render.
        {
            "host": "legacy",
            "last_worker_id": "w-1",
            "first_seen_at": datetime.now(UTC),
            "last_seen_at": datetime.now(UTC),
            "is_active": False,
        }
    ],
)
def test_a_row_without_any_pool_columns_still_renders(row):
    item = admin_hosts._to_item(row)

    assert item.status == HOST_ONLINE
    assert item.enrolled is False
    assert item.slots_used == 0
    assert item.slots_total is None


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "gopher://x/", "not-a-url", "ftp://host-a/"],
)
def test_a_broker_url_that_is_not_http_is_refused(url: str) -> None:
    """**The control plane sends the pool's shared token to this address.**

    A host proposes its own broker URL and nothing checked it, so a compromised
    host — or one with a typo — could name anything and be handed a credential
    good for *every* broker in the pool. The scheme check is the structural
    half: a `file:` URL is not a broker, and the client would do something
    surprising with it.
    """
    with pytest.raises(BrokerUrlRefused):
        validate_broker_url(url)


def test_an_allowlist_confines_the_pool_to_named_machines(monkeypatch) -> None:
    """Names, not patterns: a pattern is a thing to get subtly wrong, and the
    set of machines in a pool is small and known.

    Opt-in, because turning it on is a deployment's decision — unconfigured
    behaves as it did, which is what keeps an upgrade from breaking every
    existing pool.
    """
    monkeypatch.setenv(ENV_BROKER_HOSTS, "host-a,host-b")
    assert validate_broker_url("http://host-b:8081") == "http://host-b:8081"
    with pytest.raises(BrokerUrlRefused):
        validate_broker_url("http://attacker.example:8081")

    monkeypatch.delenv(ENV_BROKER_HOSTS)
    assert validate_broker_url("http://anything:8081") == "http://anything:8081"


# ── removed host-level residency control plane ─────────────────────────


async def test_legacy_host_residency_mutations_are_not_executable(residency_client):
    """Only attempt-fenced worker routes may create or release member grants."""
    client, store, grants = residency_client
    credential = await _enroll(client, store)
    headers = {"Authorization": f"Bearer {credential}"}

    establish = await client.post(
        "/v1/agent/host/residencies",
        json={"thread_id": "th-1", "runtime_id": "claude-code"},
        headers=headers,
    )
    release = await client.post(
        "/v1/agent/host/residencies/release",
        json={"thread_id": "th-1", "generation": 1},
        headers=headers,
    )

    assert establish.status_code == 404
    assert release.status_code == 404
    assert grants.minted == []
    assert grants.revoked == []


async def test_the_beat_that_extends_a_residency_renews_its_grant(residency_client):
    client, store, grants = residency_client
    credential = await _enroll(client, store)
    store.beat_answers = {
        ("th-linked", 1): "agr-1",  # extended, grant linked → renew
        ("th-grantless", 1): "",  # extended, workspace-only → nothing to renew
        # th-lost absent → not extended → reported lost
    }

    response = await client.post(
        "/v1/agent/host/heartbeat",
        json={
            "residencies": [
                {"thread_id": "th-linked", "generation": 1},
                {"thread_id": "th-grantless", "generation": 1},
                {"thread_id": "th-lost", "generation": 3},
            ]
        },
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200, response.text
    assert grants.renewed == ["agr-1"]
    assert response.json()["residencies_lost"] == [{"thread_id": "th-lost", "generation": 3}]


async def test_host_heartbeat_reconciles_exact_retained_docker_observations(residency_client):
    client, store, _grants = residency_client
    credential = await _enroll(client, store)

    response = await client.post(
        "/v1/agent/host/heartbeat",
        json={
            "retained_sandboxes": [
                {
                    "thread_id": "athr_1",
                    "sandbox_id": "asbx_1",
                    "container_id": "0123456789abcdef",
                    "generation": 3,
                    "activation_generation": 7,
                    "running": True,
                    "activity_active": False,
                    "idle_remaining_seconds": 1799.5,
                    "shared_live": True,
                    "members": [],
                }
            ]
        },
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200, response.text
    assert store.sandbox_observations == [
        {
            "thread_id": "athr_1",
            "host": "runner-a",
            "sandbox_id": "asbx_1",
            "container_id": "0123456789abcdef",
            "sandbox_generation": 3,
            "running": True,
            "activity_active": False,
            "idle_remaining_seconds": 1799.5,
            "members": [],
        }
    ]


async def test_host_heartbeat_rejects_a_non_shared_sandbox_observation(residency_client):
    client, store, _grants = residency_client
    credential = await _enroll(client, store)

    response = await client.post(
        "/v1/agent/host/heartbeat",
        json={
            "retained_sandboxes": [
                {
                    "thread_id": "athr_old",
                    "sandbox_id": "asbx_old",
                    "container_id": "0123456789abcdef",
                    "generation": 1,
                    "activation_generation": 1,
                    "running": True,
                    "shared_live": False,
                }
            ]
        },
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 422
    assert store.sandbox_observations == []


async def test_host_heartbeat_returns_only_durable_due_idle_stop_fences(residency_client):
    client, store, _grants = residency_client
    credential = await _enroll(client, store)
    store.sandbox_stop_due = {
        "thread_id": "athr_1",
        "generation": 3,
        "activation_generation": 8,
        "reason": "idle",
    }

    response = await client.post(
        "/v1/agent/host/heartbeat",
        json={
            "retained_sandboxes": [
                {
                    "thread_id": "athr_1",
                    "sandbox_id": "asbx_1",
                    "container_id": "0123456789abcdef",
                    "generation": 3,
                    "activation_generation": 8,
                    "running": True,
                }
            ]
        },
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["retained_sandboxes_stop"] == [
        {**store.sandbox_stop_due, "shared_live": True}
    ]


async def test_host_heartbeat_accepts_a_generation_zero_fork_target_idle_stop(residency_client):
    """A broker-reconstructed pre-turn fork target has no activation yet."""
    client, store, _grants = residency_client
    credential = await _enroll(client, store)
    store.sandbox_stop_due = {
        "thread_id": "athr_fork",
        "generation": 1,
        "activation_generation": 0,
        "reason": "idle",
    }

    response = await client.post(
        "/v1/agent/host/heartbeat",
        json={
            "retained_sandboxes": [
                {
                    "thread_id": "athr_fork",
                    "sandbox_id": "asbx_fork",
                    "container_id": "0123456789abcdef",
                    "generation": 1,
                    "activation_generation": 0,
                    "running": True,
                }
            ]
        },
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["retained_sandboxes_stop"] == [
        {**store.sandbox_stop_due, "shared_live": True}
    ]


async def test_stopped_shared_group_releases_and_revokes_each_member_residency(residency_client):
    client, store, grants = residency_client
    credential = await _enroll(client, store)
    store.release_answer = "agr-stopped"

    response = await client.post(
        "/v1/agent/host/heartbeat",
        json={
            "retained_sandboxes": [
                {
                    "thread_id": "athr_source",
                    "sandbox_id": "asbx_shared",
                    "container_id": "0123456789abcdef",
                    "generation": 1,
                    "activation_generation": 2,
                    "running": False,
                    "shared_live": True,
                    "members": [
                        {
                            "thread_id": "athr_source",
                            "native_session_id": "native-source",
                            "process_generation": 11,
                            "residency_generation": 7,
                            "resident_state": "stopped",
                            "resident_slot_id": 0,
                        },
                        {
                            "thread_id": "athr_child",
                            "native_session_id": "native-child",
                            "process_generation": 12,
                            "residency_generation": 8,
                            "resident_state": "stopped",
                            "resident_slot_id": 1,
                        },
                    ],
                }
            ]
        },
        headers={"Authorization": f"Bearer {credential}"},
    )

    assert response.status_code == 200, response.text
    assert store.release_calls == [
        ("athr_source", 7, "runner-a", 11),
        ("athr_child", 8, "runner-a", 12),
    ]
    assert grants.renewed == []
    assert grants.revoked == ["agr-stopped", "agr-stopped"]
    assert response.json()["residencies_lost"] == []


# ── residency grant minting ────────────────────────────────────────────
#
# ``_mint_residency_grant`` is the only caller of ``link_residency_grant`` and
# the only place a residency credential is minted. The heartbeat cases above
# exercise the endpoint choreography around it; these pin what the mint does
# with the gateway's answer itself, which no HTTP case can reach.


def _residency_row() -> dict[str, Any]:
    """The residency row ``establish_residency`` hands the mint."""
    return {
        "thread_id": "athr_1",
        "generation": 4,
        "establishing_attempt_id": "aatt_9",
        "user_id": "user-1",
        "model": "claude-sonnet-4",
    }


async def test_a_grant_with_no_token_is_revoked_rather_than_abandoned() -> None:
    """A named grant with no credential is dead weight until its TTL expires.

    The gateway answering with a ``grant_id`` but no ``token`` is not a shape
    anything can use, so the mint hands the grant back rather than leaving it
    live with no owner. The caller reads ``None`` as "no credential" and
    releases the residency it just established.
    """
    store = FakeHostPoolStore()
    grants = RecordingGrants()
    grants.mint = AsyncMock(return_value={"grant_id": "agr-orphan", "token": ""})

    credential = await hosts._mint_residency_grant(store, grants, row=_residency_row())

    assert credential is None
    assert grants.revoked == ["agr-orphan"]
    assert store.linked == []


async def test_a_refused_revoke_still_reports_no_credential() -> None:
    """The cleanup is best-effort, not a new way for the mint to fail.

    Before the revoke existed this path made no network call at all, so a
    gateway that refuses the revoke must not turn "there is no credential"
    into a 500.
    """
    store = FakeHostPoolStore()
    grants = RecordingGrants()
    grants.mint = AsyncMock(return_value={"grant_id": "agr-orphan", "token": ""})
    grants.revoke = AsyncMock(side_effect=RuntimeError("gateway is down"))

    credential = await hosts._mint_residency_grant(store, grants, row=_residency_row())

    assert credential is None


async def test_an_answer_without_a_grant_id_has_nothing_to_revoke() -> None:
    """An empty ``grant_id`` names nothing, so nothing is handed to revoke."""
    store = FakeHostPoolStore()
    grants = RecordingGrants()
    grants.mint = AsyncMock(return_value={"grant_id": "", "token": "tok-1"})

    credential = await hosts._mint_residency_grant(store, grants, row=_residency_row())

    assert credential is None
    assert grants.revoked == []
    assert store.linked == []


# ── Host capacity: the optional block and the history endpoint ─────────
#
# The wire rules themselves live in ``test_capacity_protocol.py``; what these
# cover is the part only an HTTP round trip can show — that a monitoring field
# never becomes a reason to refuse a heartbeat, that what the store accepted is
# what the admin page reads back, and that the history endpoint answers gaps
# rather than inventing values for them.


def _capacity_block(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "schema_version": 1,
        "sample_id": "00000000-0000-4000-8000-000000000001",
        "sample_age_ms": 120,
        "linux_boot_id": "00000000-0000-4000-8000-000000000002",
        "source": "linux_host_proc_and_docker_list",
        "cpu_percent": 37.2,
        "cpu_logical_count": 32,
        "cpu_window_seconds": 30.01,
        "memory_total_bytes": 128 * 1024**3,
        "memory_available_bytes": 86 * 1024**3,
        "inventory": {
            "namespace": "staging-agent",
            "status": "available",
            "running": 3,
            "stopped": 7,
            "other_states": {
                "created": 0,
                "paused": 0,
                "restarting": 0,
                "removing": 0,
                "dead": 0,
            },
            "reason": None,
        },
        "missing": {"cpu": None, "memory": None},
    }
    block.update(overrides)
    return block


async def _beat(async_client, credential: str, **body: Any):
    return await async_client.post(
        "/v1/agent/host/heartbeat",
        json=body,
        headers={"Authorization": f"Bearer {credential}"},
    )


async def test_a_heartbeat_carries_capacity_to_the_admin_page(client):
    async_client, store = client
    credential = await _enroll(async_client, store)

    response = await _beat(async_client, credential, capacity=_capacity_block())
    assert response.status_code == 200

    listed = (await async_client.get("/admin/agent/runner-hosts", headers=ADMIN)).json()
    assert listed["observed_at"] is not None
    (shown,) = listed["hosts"]
    assert shown["capacity_epoch"]
    capacity = shown["capacity"]
    assert capacity["cpu_percent"] == pytest.approx(37.2)
    assert capacity["cpu_logical_count"] == 32
    assert capacity["memory_total_bytes"] == 128 * 1024**3
    # The percentage is derived server-side from the same helper the chart
    # uses, so the table and the curve cannot disagree.
    assert capacity["memory_used_bytes"] == 42 * 1024**3
    assert capacity["memory_used_percent"] == pytest.approx(32.8125)
    assert capacity["inventory"]["running"] == 3
    assert capacity["inventory"]["stopped"] == 7
    assert capacity["freshness"] == "fresh"
    assert capacity["sample_age_seconds"] == pytest.approx(0.12, abs=1.0)


async def test_an_unusable_capacity_block_never_refuses_a_heartbeat(client):
    """A gap in a graph must not cost a host that is alive."""
    async_client, store = client
    credential = await _enroll(async_client, store)

    response = await _beat(
        async_client,
        credential,
        slots_total=5,
        capacity={"schema_version": 99, "sample_id": "not-a-uuid"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "online"
    assert store.rows["runner-a"]["slots_total"] == 5
    listed = (await async_client.get("/admin/agent/runner-hosts", headers=ADMIN)).json()
    assert listed["hosts"][0]["capacity"] is None


async def test_a_host_that_never_reported_capacity_is_not_reported_as_idle(client):
    """An older runner omits the block; the page must say so, not say zero."""
    async_client, store = client
    await _enroll(async_client, store)

    listed = (await async_client.get("/admin/agent/runner-hosts", headers=ADMIN)).json()
    (shown,) = listed["hosts"]
    assert shown["capacity"] is None
    assert shown["capacity_epoch"]


async def test_a_replayed_sample_does_not_refresh_its_age(client):
    """The same observation sent twice is still the same observation."""
    async_client, store = client
    credential = await _enroll(async_client, store)

    await _beat(async_client, credential, capacity=_capacity_block())
    first = store.rows["runner-a"]["capacity_sampled_at"]
    await _beat(async_client, credential, capacity=_capacity_block(sample_age_ms=1))

    assert store.rows["runner-a"]["capacity_sampled_at"] == first


async def test_an_older_observation_cannot_overwrite_a_newer_one(client):
    """Replicas report out of order; the younger reading is the true one."""
    async_client, store = client
    credential = await _enroll(async_client, store)

    await _beat(async_client, credential, capacity=_capacity_block(sample_age_ms=100))
    younger = store.rows["runner-a"]["capacity_sampled_at"]
    await _beat(
        async_client,
        credential,
        capacity=_capacity_block(
            sample_id="00000000-0000-4000-8000-000000000009", sample_age_ms=60_000
        ),
    )

    assert store.rows["runner-a"]["capacity_sampled_at"] == younger
    assert store.rows["runner-a"]["capacity_snapshot"]["sample_age_ms"] == 100


async def test_a_newer_observation_is_accepted(client):
    async_client, store = client
    credential = await _enroll(async_client, store)

    await _beat(async_client, credential, capacity=_capacity_block(sample_age_ms=60_000))
    stale = store.rows["runner-a"]["capacity_sampled_at"]
    await _beat(
        async_client,
        credential,
        capacity=_capacity_block(
            sample_id="00000000-0000-4000-8000-000000000009", sample_age_ms=100
        ),
    )

    assert store.rows["runner-a"]["capacity_sampled_at"] > stale


async def test_the_pool_response_is_not_cached(client):
    async_client, store = client
    await _enroll(async_client, store)

    response = await async_client.get("/admin/agent/runner-hosts", headers=ADMIN)

    assert response.headers["cache-control"] == "no-store"


async def test_capacity_history_of_an_unknown_host_is_not_found(client):
    async_client, _store = client
    response = await async_client.get(
        "/admin/agent/runner-hosts/nobody/capacity-history", headers=ADMIN
    )
    assert response.status_code == 404


async def test_capacity_history_refuses_a_range_it_does_not_serve(client):
    """A substituted default would draw a window the label does not name."""
    async_client, store = client
    await _enroll(async_client, store)

    response = await async_client.get(
        "/admin/agent/runner-hosts/runner-a/capacity-history?range=7d", headers=ADMIN
    )

    assert response.status_code == 422
    assert "1h" in response.text


async def test_capacity_history_returns_an_explicit_gap_per_missing_minute(client):
    async_client, store = client
    await _enroll(async_client, store)
    await _beat(
        async_client,
        credential := store.rows["runner-a"]["credential_hash"] and "",
    )

    until = datetime.now(UTC).replace(second=0, microsecond=0)
    store.history["runner-a"] = [
        {
            "bucket_at": until - timedelta(minutes=3),
            "sampled_at": until - timedelta(minutes=3) + timedelta(seconds=20),
            "linux_boot_id": "00000000-0000-4000-8000-000000000002",
            "cpu_percent": 37.2,
            "cpu_logical_count": 32,
            "cpu_window_seconds": 30.0,
            "cpu_missing_reason": None,
            "memory_total_bytes": 128 * 1024**3,
            "memory_available_bytes": 86 * 1024**3,
            "memory_missing_reason": None,
        }
    ]

    response = await async_client.get(
        "/admin/agent/runner-hosts/runner-a/capacity-history?range=1h", headers=ADMIN
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["resolution_seconds"] == 60
    assert body["semantics"] == "sampled_observation"
    assert body["capacity_epoch"]
    assert len(body["points"]) == 61
    observed = [point for point in body["points"] if point["sampled_at"] is not None]
    assert len(observed) == 1
    assert observed[0]["cpu_percent"] == pytest.approx(37.2)
    assert observed[0]["memory_used_percent"] == pytest.approx(32.8125)
    # Everything else is a hole, and a hole is null rather than zero.
    assert all(
        point["cpu_percent"] is None for point in body["points"] if point["sampled_at"] is None
    )
    assert credential == ""
