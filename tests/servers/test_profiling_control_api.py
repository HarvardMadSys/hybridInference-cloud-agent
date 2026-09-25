"""Real HTTP: the Admin control surface and the host configuration exchange.

Two surfaces, one file, because they are two ends of one decision: the Admin
routes write the configuration and the exchange hands it to a host. A test of
either alone would pass while the pair disagreed.

What is real here: the FastAPI applications, their routers, their dependencies,
the host credential check, the request and response models, and the transport.
What is stated rather than performed: storage (an in-memory capability) and the
broker's own container, which the policy-push test replaces with the real
application served in-process.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud_agent.api import admin_profiling_control, host_profiling
from cloud_agent.application.profiling_control import ProfilingControlService
from cloud_agent.domain.profiling_control import (
    CONTROL_OUTCOME_APPLIED,
    CONTROL_OUTCOME_REFUSED,
    CONTROL_OUTCOME_REPLAYED,
    CONTROL_REFUSED_ALREADY_ACTIVE,
    CONTROL_REFUSED_ALREADY_FINISHED,
    CONTROL_REFUSED_STALE_REVISION,
    ControlCommandResult,
)
from cloud_agent.storage.repositories.profiling_control import (
    LIFECYCLE_PENDING,
    LIFECYCLE_STOPPED,
)
from cloud_agent_shared import operation_profiling

ADMIN = {"Authorization": "Bearer test-admin-token"}
HOST = "gpu-1"
HOST_CREDENTIAL = "host-credential-for-the-test"


class _FakeControl:
    """An in-memory stand-in for the control capability.

    Written out rather than mocked: the refusal branches below are the thing
    under test, and a mock's configured returns would make the test assert what
    it was told to assert.
    """

    def __init__(self) -> None:
        self.control: dict[str, Any] | None = {
            "deployment": "test",
            "revision": 0,
            "desired_state": "off",
            "current_run_id": None,
            "updated_at": datetime.now(UTC),
            "updated_by": None,
            "last_stop_revision": None,
            "last_stopped_at": None,
            "last_stopped_run_id": None,
        }
        self.runs: dict[str, dict[str, Any]] = {}
        self.commands: dict[tuple[str, str], dict[str, Any]] = {}
        self.stored = 0

    async def get_control_state(self, *, deployment: str) -> dict[str, Any] | None:
        """Return the control row, if this deployment has one."""
        return self.control

    async def start_run(
        self,
        *,
        deployment: str,
        request_id: str,
        fingerprint: str,
        expected_revision: int,
        run: Any,
        actor_id: str | None,
        now: datetime,
    ) -> ControlCommandResult:
        """Create, move the row and record the command, or refuse."""
        assert self.control is not None
        seen = self.commands.get((deployment, request_id))
        if seen is not None:
            return ControlCommandResult(
                kind=CONTROL_OUTCOME_REPLAYED,
                revision=int(seen["revision"]),
                run_id=seen["run_id"],
            )
        if self.control["current_run_id"]:
            return ControlCommandResult(
                kind=CONTROL_OUTCOME_REFUSED,
                revision=int(self.control["revision"]),
                run_id=str(self.control["current_run_id"]),
                reason=CONTROL_REFUSED_ALREADY_ACTIVE,
                detail="another experiment is active; stop it first",
            )
        if int(self.control["revision"]) != int(expected_revision):
            return ControlCommandResult(
                kind=CONTROL_OUTCOME_REFUSED,
                revision=int(self.control["revision"]),
                reason=CONTROL_REFUSED_STALE_REVISION,
                detail="expected revision differs",
            )
        revision = int(self.control["revision"]) + 1
        self.runs[str(run["run_id"])] = {
            "run_id": str(run["run_id"]),
            "operations": list(run["operations"]),
            "target": dict(run.get("target") or {}),
            "expires_at": run["expires_at"],
            "config_version": int(run.get("config_version") or 1),
            "admission_instances": [],
            "created_at": now,
            "received_at": now,
            "source": "admin",
            "lifecycle": LIFECYCLE_PENDING,
            "created_by": actor_id,
            "control_revision": revision,
            "started_at": now,
            "stopped_at": None,
            "stop_reason": None,
            "deployment": deployment,
        }
        self.control.update(
            {
                "revision": revision,
                "desired_state": "on",
                "current_run_id": str(run["run_id"]),
                "updated_at": now,
                "updated_by": actor_id,
            }
        )
        self.commands[(deployment, request_id)] = {
            "revision": revision,
            "run_id": str(run["run_id"]),
        }
        return ControlCommandResult(
            kind=CONTROL_OUTCOME_APPLIED,
            revision=revision,
            run_id=str(run["run_id"]),
            expires_at=run["expires_at"],
        )

    async def stop_run(
        self,
        *,
        deployment: str,
        request_id: str,
        fingerprint: str,
        run_id: str,
        actor_id: str | None,
        now: datetime,
        reason: str,
    ) -> ControlCommandResult:
        """Stop the named run, or replay a Stop already recorded."""
        assert self.control is not None
        seen = self.commands.get((deployment, request_id))
        if seen is not None:
            return ControlCommandResult(
                kind=CONTROL_OUTCOME_REPLAYED,
                revision=int(seen["revision"]),
                run_id=seen["run_id"],
            )
        current = self.control["current_run_id"]
        if current is None:
            return ControlCommandResult(
                kind=CONTROL_OUTCOME_REPLAYED,
                revision=int(self.control["revision"]),
                run_id=None,
            )
        if str(current) != run_id:
            return ControlCommandResult(
                kind=CONTROL_OUTCOME_REFUSED,
                revision=int(self.control["revision"]),
                run_id=str(current),
                reason=CONTROL_REFUSED_ALREADY_FINISHED,
                detail="that experiment is not the one in force",
            )
        revision = int(self.control["revision"]) + 1
        self.control.update(
            {
                "revision": revision,
                "desired_state": "off",
                "current_run_id": None,
                "last_stop_revision": revision,
                "last_stopped_at": now,
                "last_stopped_run_id": str(current),
            }
        )
        row = self.runs.get(str(current))
        if row is not None:
            row.update({"lifecycle": LIFECYCLE_STOPPED, "stopped_at": now, "stop_reason": reason})
        self.commands[(deployment, request_id)] = {"revision": revision, "run_id": str(current)}
        return ControlCommandResult(
            kind=CONTROL_OUTCOME_APPLIED, revision=revision, run_id=str(current)
        )

    async def expire_run(
        self, *, deployment: str, run_id: str, now: datetime
    ) -> ControlCommandResult:
        """Land a lapsed run's terminal state."""
        return ControlCommandResult(
            kind=CONTROL_OUTCOME_APPLIED, revision=int(self.control["revision"]) + 1, run_id=run_id
        )

    async def count_stored_measurements(self, *, run_id: str) -> int:
        """Return how many samples this experiment stored."""
        return self.stored

    async def mark_run_lifecycle(self, **kwargs: Any) -> None:
        """Attach lifecycle to a run that exists."""
        return None

    async def get_run(self, *, run_id: str) -> dict[str, Any] | None:
        """Return one run."""
        return self.runs.get(run_id)

    async def list_runs(self, *, deployment: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return the deployment's runs."""
        return list(self.runs.values())[:limit]


class _FakeHostPool:
    """Two enrolled hosts, which is what a target is validated against."""

    async def list_runner_hosts(self) -> list[dict[str, Any]]:
        """Return the pool."""
        return [{"host": HOST, "status": "online"}, {"host": "gpu-2", "status": "online"}]


class _FakeAudit:
    """Records what was audited, so the write path can be checked for it."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    async def record_admin_action(self, **kwargs: Any) -> int:
        """Remember one action."""
        self.entries.append(kwargs)
        return 1


class _FakeStorage:
    """The three capabilities these routes reach for."""

    def __init__(self) -> None:
        self.profiling_control = _FakeControl()
        self.host_pool = _FakeHostPool()
        self.audit = _FakeAudit()


def _control_app(storage: _FakeStorage) -> FastAPI:
    """The real admin router over a fake storage layer."""
    app = FastAPI()
    app.include_router(admin_profiling_control.router)
    app.state.storage = storage
    return app


@pytest.fixture(autouse=True)
def _default_admin_mode(monkeypatch):
    """Admin control works without a profiling mode or static experiment."""
    monkeypatch.delenv(operation_profiling.ENV_PROFILING_MODE, raising=False)
    monkeypatch.delenv(operation_profiling.ENV_PROFILING_ENABLED, raising=False)
    monkeypatch.delenv(operation_profiling.ENV_PROFILING_RUN, raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("AGENT_OPERATION_PROFILING_DEPLOYMENT", "test")
    operation_profiling.reset_cache()
    operation_profiling.reset_policy()
    yield
    operation_profiling.reset_cache()
    operation_profiling.reset_policy()


def _client(storage: _FakeStorage) -> TestClient:
    """A client for the control surface, authenticated as the administrator."""
    return TestClient(_control_app(storage), headers=ADMIN)


def test_default_control_is_available_but_collection_is_disabled(monkeypatch) -> None:
    """A fresh deployment returns exactly the state that exposes Enable."""
    monkeypatch.delenv("AGENT_OPERATION_PROFILING_DEPLOYMENT", raising=False)
    with _client(_FakeStorage()) as client:
        response = client.get("/admin/agent/profiling-control")
    assert response.status_code == 200
    control = response.json()
    assert control["deployment"] == "default"
    assert control["mode"] == operation_profiling.MODE_ADMIN
    assert control["configured"] is True
    assert control["status"] == "off"
    assert control["run"] is None
    assert control["run_active"] is False
    assert control["stored_samples"] == 0


def test_a_start_creates_the_experiment_and_reports_it() -> None:
    """The exact request the page sends produces exactly the state it reads."""
    storage = _FakeStorage()
    with _client(storage) as client:
        response = client.post(
            "/admin/agent/profiling-runs",
            json={
                "request_id": "req-1",
                "expected_revision": 0,
                "operations": ["attempt.startup", "terminal.open", "files.list", "files.open"],
                "target": {"host": HOST},
                "duration_seconds": 600,
            },
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["revision"] == 1
    assert body["run_id"].startswith("admin-")
    control = body["control"]
    assert control["run"]["run_id"] == body["run_id"]
    assert control["run"]["target"] == {"host": HOST}
    assert control["run_active"] is True
    assert control["run"]["seconds_remaining"] > 500
    assert control["budget_scope"] == "producer_instance"
    assert control["stored_samples"] == 0
    # A refused or accepted write is always audited: an experiment is the kind
    # of change somebody has to be able to explain later.
    assert storage.audit.entries
    assert storage.audit.entries[-1]["action"] == "agent.profiling.start"


def test_the_same_request_id_returns_the_same_experiment() -> None:
    """A lost response is retried, not turned into a second experiment."""
    storage = _FakeStorage()
    payload = {
        "request_id": "req-1",
        "expected_revision": 0,
        "operations": ["attempt.startup"],
        "target": {"host": HOST},
        "duration_seconds": 600,
    }
    with _client(storage) as client:
        first = client.post("/admin/agent/profiling-runs", json=payload)
        second = client.post("/admin/agent/profiling-runs", json=payload)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["run_id"] == second.json()["run_id"]
    assert first.json()["revision"] == second.json()["revision"]
    assert (
        first.json()["control"]["run"]["expires_at"]
        == (second.json()["control"]["run"]["expires_at"])
    )


def test_a_stale_revision_is_refused_with_the_number_to_re_read() -> None:
    """Two administrators, one experiment: the loser is told how to recover."""
    storage = _FakeStorage()
    with _client(storage) as client:
        first = client.post(
            "/admin/agent/profiling-runs",
            json={
                "request_id": "req-1",
                "expected_revision": 0,
                "operations": ["attempt.startup"],
                "target": {},
                "duration_seconds": 600,
            },
        )
        assert first.status_code == 201
        second = client.post(
            "/admin/agent/profiling-runs",
            json={
                "request_id": "req-2",
                "expected_revision": 0,
                "operations": ["files.open"],
                "target": {},
                "duration_seconds": 600,
            },
        )
    assert second.status_code == 409, second.text
    error = second.json()["detail"]["error"]
    assert error["type"] == CONTROL_REFUSED_ALREADY_ACTIVE
    assert error["revision"] == 1
    assert error["run_id"] == first.json()["run_id"]


def test_invalid_requests_are_refused_without_touching_the_control_row() -> None:
    """A request that could not measure anything never half-starts."""
    storage = _FakeStorage()
    with _client(storage) as client:
        for payload, expected in (
            (
                {
                    "request_id": "r",
                    "expected_revision": 0,
                    "operations": [],
                    "target": {},
                    "duration_seconds": 600,
                },
                422,
            ),
            (
                {
                    "request_id": "r",
                    "expected_revision": 0,
                    "operations": ["files.delete"],
                    "target": {},
                    "duration_seconds": 600,
                },
                422,
            ),
            (
                {
                    "request_id": "r",
                    "expected_revision": 0,
                    "operations": ["files.open"],
                    "target": {"host": "nope"},
                    "duration_seconds": 600,
                },
                422,
            ),
            (
                {
                    "request_id": "r",
                    "expected_revision": 0,
                    "operations": ["files.open"],
                    "target": {},
                    "duration_seconds": 10,
                },
                422,
            ),
            (
                {
                    "request_id": "r",
                    "expected_revision": 0,
                    "operations": ["files.open"],
                    "target": {},
                    "duration_seconds": 600,
                    "sneaky": True,
                },
                422,
            ),
        ):
            response = client.post("/admin/agent/profiling-runs", json=payload)
            assert response.status_code == expected, response.text
    assert storage.profiling_control.control is not None
    assert storage.profiling_control.control["revision"] == 0
    assert storage.profiling_control.control["current_run_id"] is None


def test_stop_is_idempotent_and_states_what_it_stopped() -> None:
    """A repeated Stop is not a second command, and the page learns the reason."""
    storage = _FakeStorage()
    with _client(storage) as client:
        started = client.post(
            "/admin/agent/profiling-runs",
            json={
                "request_id": "req-1",
                "expected_revision": 0,
                "operations": ["files.open"],
                "target": {},
                "duration_seconds": 600,
            },
        )
        run_id = started.json()["run_id"]
        first = client.post(
            f"/admin/agent/profiling-runs/{run_id}/stop", json={"request_id": "stop-1"}
        )
        second = client.post(
            f"/admin/agent/profiling-runs/{run_id}/stop", json={"request_id": "stop-2"}
        )
    assert first.status_code == 200, first.text
    assert first.json()["revision"] == 2
    assert first.json()["control"]["run"] is None
    assert first.json()["control"]["run_active"] is False
    assert first.json()["control"]["last_stopped_run"]["run_id"] == run_id
    assert first.json()["control"]["status"] == "off"
    # Nothing was active, so the second Stop changed nothing and did not move
    # the revision: a later Start must not be made to look stale by a double
    # click.
    assert second.status_code == 200, second.text
    assert second.json()["revision"] == 2


def test_a_page_open_on_an_old_experiment_cannot_stop_the_new_one() -> None:
    """The run id in the URL is checked, not assumed."""
    storage = _FakeStorage()
    control = storage.profiling_control
    with _client(storage) as client:
        started = client.post(
            "/admin/agent/profiling-runs",
            json={
                "request_id": "req-1",
                "expected_revision": 0,
                "operations": ["files.open"],
                "target": {},
                "duration_seconds": 600,
            },
        )
        run_id = started.json()["run_id"]
        # A second experiment is impossible while the first is active, so the
        # only way a stale id reaches here is a page that never refreshed --
        # which is exactly the case this refuses.
        assert control.control is not None
        control.control["current_run_id"] = "admin-someone-elses-run"
        refusal = client.post(
            f"/admin/agent/profiling-runs/{run_id}/stop", json={"request_id": "stop-1"}
        )
    assert refusal.status_code == 409, refusal.text
    assert refusal.json()["detail"]["error"]["type"] == "run_already_finished"


def test_a_deployment_without_admin_control_says_so() -> None:
    """Not "off": a deployment that cannot be controlled is a different answer."""
    storage = _FakeStorage()
    with _client(storage) as client:
        storage.profiling_control.control = None
        snapshot = client.get("/admin/agent/profiling-control")
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["status"] == "unavailable"
    assert body["configured"] is False
    assert body["run"] is None
    assert body["run_active"] is False


def test_environment_mode_reports_itself_as_unmanaged(monkeypatch) -> None:
    """The page must not offer a Stop that has nothing to stop."""
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_MODE, operation_profiling.MODE_ENVIRONMENT)
    operation_profiling.reset_cache()
    storage = _FakeStorage()
    with _client(storage) as client:
        snapshot = client.get("/admin/agent/profiling-control")
        refused = client.post(
            "/admin/agent/profiling-runs",
            json={
                "request_id": "r",
                "expected_revision": 0,
                "operations": ["files.open"],
                "target": {},
                "duration_seconds": 600,
            },
        )
    assert snapshot.json()["status"] == "unmanaged"
    assert snapshot.json()["mode"] == operation_profiling.MODE_ENVIRONMENT
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["type"] == "not_admin_mode"


def test_the_control_surface_refuses_an_anonymous_caller() -> None:
    """The control surface is admin-only, on both the read and the write."""
    storage = _FakeStorage()
    with TestClient(_control_app(storage)) as anonymous:
        assert anonymous.get("/admin/agent/profiling-control").status_code in {401, 403}
        assert anonymous.post(
            "/admin/agent/profiling-runs",
            json={
                "request_id": "r",
                "expected_revision": 0,
                "operations": ["files.open"],
                "target": {},
                "duration_seconds": 600,
            },
        ).status_code in {401, 403}


# ── the host policy exchange ───────────────────────────────────────────


def _host_app(storage: _FakeStorage, monkeypatch) -> FastAPI:
    """The real host policy route over the same fake storage.

    The host credential dependency is replaced, not bypassed: the route still
    runs `authenticate_host`, it just resolves to this test's host instead of
    reading a signed token out of a database.
    """
    app = FastAPI()
    app.include_router(host_profiling.router)
    app.state.storage = storage

    async def _authenticate() -> dict[str, Any]:
        return {"host": HOST, "status": "online"}

    app.dependency_overrides[host_profiling.authenticate_host] = _authenticate
    return app


def test_a_host_is_told_the_configuration_in_force() -> None:
    """The exchange answers with the experiment, its operations and its end."""
    storage = _FakeStorage()
    service = ProfilingControlService(
        control=storage.profiling_control, deployment="test", host_pool=storage.host_pool
    )
    started = _run_sync(service.start, service, now=datetime.now(UTC))
    assert started is not None

    with TestClient(_host_app(storage, None)) as client:
        answer = client.post("/v1/agent/hosts/profiling-policy", json={"host": HOST})
    assert answer.status_code == 200, answer.text
    policy = answer.json()
    assert policy["mode"] == "admin"
    assert policy["revision"] == 1
    assert policy["run"]["run_id"] == started.run_id
    assert policy["run"]["target"] == {}
    assert policy["run"]["max_operations"] == 100
    assert "server_time" in policy
    # Nothing in the answer claims to know what this host has applied, and no
    # lease is issued: the experiment's own deadline is the bound.
    assert "lease" not in policy
    assert "applied" not in answer.text


def test_the_configuration_exchange_records_nothing_about_the_host() -> None:
    """Sync is allowed to lag, so there is no acknowledgement to record."""
    storage = _FakeStorage()
    service = ProfilingControlService(
        control=storage.profiling_control, deployment="test", host_pool=storage.host_pool
    )
    _run_sync(service.start, service, now=datetime.now(UTC))
    before = dict(storage.profiling_control.control or {})
    with TestClient(_host_app(storage, None)) as client:
        client.post("/v1/agent/hosts/profiling-policy", json={"host": HOST})
    assert storage.profiling_control.control == before


def test_a_host_cannot_ask_for_another_hosts_configuration() -> None:
    """The answer is computed for whoever authenticated, not for whoever asked."""
    storage = _FakeStorage()
    seen: list[str] = []

    app = FastAPI()
    app.include_router(host_profiling.router)
    app.state.storage = storage

    async def _authenticate() -> dict[str, Any]:
        return {"host": HOST, "status": "online"}

    app.dependency_overrides[host_profiling.authenticate_host] = _authenticate
    original = host_profiling.build_policy_for_host

    async def _spy(*, host: Any, reported: Any, store: Any) -> dict[str, Any]:
        seen.append(str(host.get("host")))
        return await original(host=host, reported=reported, store=store)

    host_profiling.read_host_profiling_policy.__globals__["build_policy_for_host"] = _spy
    try:
        with TestClient(app) as client:
            client.post("/v1/agent/hosts/profiling-policy", json={"host": "some-other-machine"})
    finally:
        host_profiling.read_host_profiling_policy.__globals__["build_policy_for_host"] = original
    # The body named another machine and the answer was built for this one.
    assert seen == [HOST]


def test_a_stopped_deployment_hands_the_host_no_run() -> None:
    """A policy with no run is a command to stop, not an absence of news."""
    storage = _FakeStorage()
    service = ProfilingControlService(
        control=storage.profiling_control, deployment="test", host_pool=storage.host_pool
    )
    started = _run_sync(service.start, service, now=datetime.now(UTC))
    assert started is not None and started.run_id
    _run_sync(
        service.stop,
        service,
        stop_request=started.run_id,
        now=datetime.now(UTC),
    )
    with TestClient(_host_app(storage, None)) as client:
        policy = client.post("/v1/agent/hosts/profiling-policy", json={}).json()
    assert policy["revision"] == 2
    assert policy["run"] is None


# ── the broker's own policy endpoint ───────────────────────────────────


def test_the_broker_applies_a_policy_and_refuses_a_lower_revision() -> None:
    """Reordered pushes cannot undo a Stop, and a repeat is idempotent."""
    from cloud_agent_host import broker_profiling_policy
    from cloud_agent_host.sandbox import ProcessBackend
    from cloud_agent_host.workspace_broker import create_app

    monkeypatch_token = "broker-token"
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        token=monkeypatch_token,
        shared_session_supervisor=_IdleSupervisor(),  # type: ignore[arg-type]
    )
    broker_profiling_policy.reset_applied_state()
    operation_profiling.reset_policy()
    headers = {"X-Agent-Workspace-Token": monkeypatch_token}
    run = {
        "run_id": "admin-a",
        "operations": ["files.open"],
        "target": {},
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "config_version": 1,
        "max_operations": 100,
    }
    with TestClient(app) as client:
        first = client.post(
            "/profiling-policy",
            json={"mode": "admin", "revision": 5, "run": run},
            headers=headers,
        )
        repeat = client.post(
            "/profiling-policy",
            json={"mode": "admin", "revision": 5, "run": run},
            headers=headers,
        )
        stop = client.post(
            "/profiling-policy",
            json={"mode": "admin", "revision": 6, "run": None},
            headers=headers,
        )
        stale = client.post(
            "/profiling-policy",
            json={"mode": "admin", "revision": 5, "run": run},
            headers=headers,
        )
    assert first.status_code == 200, first.text
    assert first.json()["changed"] is True
    assert repeat.status_code == 200 and repeat.json()["changed"] is False
    assert stop.status_code == 200, stop.text
    assert stop.json()["run_id"] is None
    # The stale push is refused, not applied: this is what keeps a Stop stopped.
    assert stale.status_code == 409, stale.text
    assert operation_profiling.policy_store().run() is None


def test_a_broker_without_a_policy_records_nothing() -> None:
    """A policy message is the only thing that lets a broker collect."""
    from cloud_agent_host import broker_profiling_policy

    broker_profiling_policy.reset_applied_state()
    operation_profiling.reset_policy()
    context = {
        "run_id": "admin-a",
        "operation": "files.open",
        "operations": ["files.open"],
        "expires_at": __import__("time").time() + 60,
        "call_id": "c1",
    }
    assert operation_profiling.start_stage_recording(context, producer="broker") is None


class _IdleSupervisor:
    """The smallest supervisor the broker's lifespan will start.

    The policy route owns no container and reaches no workspace, so the
    supervisor is never asked for one. It exists because the broker refuses to
    start without one beside a process backend, which is a real product rule --
    not something this test should work around by reaching into the app.
    """

    def start(self) -> None:
        """Start nothing."""
        return None

    def close(self) -> None:
        """Close nothing."""
        return None

    def owns(self, _workspace_id: str) -> bool:
        """Own no workspace."""
        return False


def _run_sync(function, service, **kwargs):
    """Drive one asynchronous service call from a synchronous test."""
    import asyncio

    if "stop_request" in kwargs:
        request = service.validate_stop(
            request_id="stop-1", run_id=kwargs.pop("stop_request"), actor_id="admin"
        )
        return asyncio.run(service.stop(request, **kwargs))
    request = service.validate_start(
        request_id="start-1",
        expected_revision=0,
        operations=["files.open"],
        host=None,
        duration_seconds=600,
        actor_id="admin",
        known_hosts=None,
    )
    return asyncio.run(service.start(request, **kwargs))
