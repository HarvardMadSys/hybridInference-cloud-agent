"""Acceptance: an administrator's switch really controls collection.

This walks the whole chain against a real PostgreSQL, a real schema applied by
Alembic, and the real FastAPI applications served on real sockets:

1. `POST /admin/agent/profiling-runs` creates the experiment and moves the
   control row.
2. The control plane's own sync worker reads that row and publishes the
   configuration its routes then collect under -- without a restart and without
   an environment variable changing.
3. A real runner policy sync, pointed at the real control-plane endpoint with a
   real broker client, carries that configuration to a **separately served
   broker application** over HTTP. This is the hop that used to be broken: a
   sync with no broker client left every file and terminal sample missing its
   container sub-stages.
4. A real file read through the workspace route roots a measurement, hands the
   selection to the broker over the real fragment protocol, and stores what came
   back.
5. `GET /admin/agent/profiling-control` reads the same configuration back,
   including the real stored-sample count.
6. Disabling stops it: the next read produces no sample at all, and a
   measurement that was already open when the switch flipped finishes and is
   stored under the experiment that selected it.

What is *simulated* here is stated rather than hidden. There is no container in
this environment, so the broker's own workspace read is the in-process double
the existing measurement acceptance test uses -- it validates selections through
the real `operation_call` code but answers a file read from a fixture. The
parent/child turn handoff on a real fork is covered in
`tests/unit/test_runner_host_service.py`, and the offline bound -- an experiment
ending on a node that never hears anything again -- in
`tests/unit/test_operation_profiling_runtime_control.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import asyncpg
import pytest
import pytest_asyncio
from tests.integration.servers.test_measurement_acceptance_db import (
    ADMIN,
    HOST,
    REPO,
    _arrange_chain,
    _Broker,
    _db_config,
    _dsn,
    _Gateway,
    _reset,
    _serve,
)

from cloud_agent.api.deps import get_agent_app_credentials, get_gateway_client
from cloud_agent.app import create_app
from cloud_agent.application.profiling_control import ProfilingControlService
from cloud_agent.host_pool import hash_secret
from cloud_agent.profiling_control_config import profiling_control_deployment
from cloud_agent.storage.assembly import Storage
from cloud_agent.tokens import mint_host_token
from cloud_agent.workers.profiling_control import ProfilingPolicyWorker
from cloud_agent_profiling import operation_profiling

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

#: The administrator's request id. Fixed here because the idempotency contract
#: is part of what this file proves: the same id and the same body must return
#: the same experiment rather than starting a second one.
START_REQUEST_ID = "acceptance-start-1"
STOP_REQUEST_ID = "acceptance-stop-1"

#: Long enough that nothing in this test races the deadline.
DURATION_SECONDS = 600


class _IdleSupervisor:
    """The smallest supervisor the broker's lifespan will start.

    The workspace read in this chain is answered by the double the measurement
    acceptance test already uses, and the policy route touches no container at
    all -- but the broker refuses to start beside a process backend without a
    supervisor, which is a real product rule this test should not work around.
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


@dataclass
class _ControlChain:
    """Everything the assertions below reach for, assembled."""

    pool: Any
    store: Storage
    base_url: str
    broker: _Broker
    job_id: str
    sandbox_id: str
    service: ProfilingControlService
    worker: ProfilingPolicyWorker
    worker_task: asyncio.Task[None]
    #: The broker's own application, served on its own socket so the runner's
    #: configuration push crosses a real HTTP boundary into it.
    broker_base_url: str
    #: The real host credential this chain's host row was pointed at, so the
    #: runner's own configuration exchange authenticates for real.
    host_credential: str
    #: A real minted owner session, as the browser presents it. The file and
    #: terminal routes are owner routes: an administrator's infrastructure
    #: token authenticates the control surface, not a user's workspace.
    owner_cookie: dict[str, str]


@pytest_asyncio.fixture
async def control_chain(monkeypatch, tmp_path) -> AsyncIterator[_ControlChain]:
    """The whole control plane, in Admin control mode, over a real database."""
    config = _db_config()
    try:
        pool = await asyncpg.create_pool(**config, min_size=1, max_size=5)
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable
    from tests.support.migrations import apply_migrations

    apply_migrations(config)
    await _reset(pool)

    monkeypatch.setenv("AGENT_DATABASE_URL", _dsn(config))
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "acceptance-control-secret")
    monkeypatch.setenv("ADMIN_TOKEN", "acceptance-admin-token")
    monkeypatch.setenv("AGENT_SESSION_SECRET", "acceptance-session-secret-32-bytes!")
    monkeypatch.setenv("GATEWAY_ISSUER", "https://gateway.example")
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", REPO)
    monkeypatch.setenv("AGENT_WORKSPACE_BROKER_TOKEN", "acceptance-broker-token")
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(tmp_path / "records.jsonl"))
    # The product default needs no opt-in mode or namespace. A stale descriptor
    # without the legacy switch must not silently start a static experiment.
    monkeypatch.delenv(operation_profiling.ENV_PROFILING_MODE, raising=False)
    monkeypatch.delenv(operation_profiling.ENV_PROFILING_ENABLED, raising=False)
    monkeypatch.setenv(
        operation_profiling.ENV_PROFILING_RUN,
        json.dumps(
            {
                "run_id": "stale-environment-run",
                "operations": ["attempt.startup"],
                "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
                "max_operations": 8,
            }
        ),
    )
    monkeypatch.delenv("AGENT_OPERATION_PROFILING_DEPLOYMENT", raising=False)
    operation_profiling.reset_cache()
    operation_profiling.reset_policy()

    from cloud_agent import broker_routing, operation_measurement
    from cloud_agent_host import runner as runner_mod

    operation_measurement.reset()
    runner_mod.reset_delivery_state()

    broker = _Broker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
    app = create_app()
    app.dependency_overrides[get_gateway_client] = _Gateway
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    server, task, base_url = await _serve(app)
    store = Storage(pool)
    job, _claim, sandbox = await _arrange_chain(store)
    # The chain enrolls its host with a placeholder hash, which is enough for
    # every test that never presents a credential. This one does present one --
    # the runner's own configuration exchange -- so the row is pointed at the
    # hash of a real minted token. Without this the exchange would answer 401
    # and the test would be proving the stub rather than the route.
    host_credential = mint_host_token(host=HOST)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_runner_hosts SET credential_hash = $1 WHERE host = $2",
            hash_secret(host_credential),
            HOST,
        )

    # The broker application, served on its own socket. It is built through its
    # own factory with the same token the runner would present, so the push
    # below is the real route with the real credential check in front of it.
    from cloud_agent_host.broker_profiling_policy import reset_applied_state
    from cloud_agent_host.sandbox import ProcessBackend
    from cloud_agent_host.workspace_broker import create_app as create_broker_app

    reset_applied_state()
    broker_app = create_broker_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        token="acceptance-broker-token",
        shared_session_supervisor=_IdleSupervisor(),  # type: ignore[arg-type]
    )
    broker_server, broker_task, broker_base_url = await _serve(broker_app)
    chain_job = job

    # The policy loop, driven exactly as the lifespan drives it -- but with the
    # wait cut short, so the test does not spend thirty seconds proving that a
    # timer fires.
    service = ProfilingControlService(
        control=store.profiling_control,
        deployment=profiling_control_deployment(),
        host_pool=store.host_pool,
    )
    worker = ProfilingPolicyWorker(service=service)
    worker._wait = _no_wait  # type: ignore[method-assign]
    worker_task = asyncio.create_task(worker.run())
    await asyncio.sleep(0)

    from tests.integration.servers.test_measurement_acceptance_db import USER_ID

    from cloud_agent.identity import SESSION_COOKIE, mint_session

    chain = _ControlChain(
        pool=pool,
        store=store,
        base_url=base_url,
        broker=broker,
        job_id=str(chain_job["id"]),
        sandbox_id=str(sandbox["sandbox_id"]),
        service=service,
        worker=worker,
        worker_task=worker_task,
        owner_cookie={SESSION_COOKIE: mint_session(user_id=USER_ID)},
        broker_base_url=broker_base_url,
        host_credential=host_credential,
    )
    try:
        yield chain
    finally:
        worker_task.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        broker_server.should_exit = True
        await asyncio.wait_for(broker_task, timeout=10)
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        from cloud_agent import measurement_writer

        measurement_writer.reset_writer(None)
        await _reset(pool)
        await pool.close()
        operation_profiling.reset_cache()
        operation_profiling.reset_policy()


async def _no_wait(interval: float) -> None:
    """Replace the worker's inter-pass sleep with a yield.

    The loop is the same loop; only the thirty-second timer is removed, because
    a test that waits out an interval proves the interval rather than the
    feature.
    """
    await asyncio.sleep(0)


async def _await_policy(*, run_id: str | None, timeout_s: float = 5.0) -> None:
    """Wait for this process's published policy to name the wanted run."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        published = operation_profiling.policy_store().published()
        current = published.run.run_id if published is not None and published.run else None
        if current == run_id:
            return
        if loop.time() > deadline:
            pytest.fail(f"policy never reached run {run_id!r} (it is {current!r})")
        await asyncio.sleep(0.01)


async def _measurement_count(chain: _ControlChain, run_id: str) -> int:
    """Return how many rows this experiment has stored."""
    async with chain.pool.acquire() as conn:
        return int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM agent_operation_measurements WHERE profiling_run_id = $1",
                run_id,
            )
            or 0
        )


@pytest.mark.parametrize("duration_seconds", [600, None])
async def test_start_collects_reads_a_real_sample_and_stop_ends_it(
    control_chain: _ControlChain,
    duration_seconds: int | None,
) -> None:
    """The whole acceptance: Start, collect, read back, Stop, stay stopped."""
    import httpx

    chain = control_chain
    async with httpx.AsyncClient(base_url=chain.base_url, headers=ADMIN) as admin:
        # -- 1. Off, and honest about it --------------------------------
        before_response = await admin.get("/admin/agent/profiling-control")
        assert before_response.status_code == 200, before_response.text
        before = before_response.json()
        assert before["mode"] == operation_profiling.MODE_ADMIN
        assert before["deployment"] == "default"
        assert before["configured"] is True
        assert before["status"] == "off"
        assert before["run"] is None
        assert before["run_active"] is False
        assert before["last_stopped_run"] is None
        assert before["revision"] == 0
        assert before["stored_samples"] == 0
        # The stale environment descriptor is still in the process's
        # environment and must not have been used: admin mode is the only
        # control source.
        assert operation_profiling.active_run() is None

        # -- 2. Start ---------------------------------------------------
        started = await admin.post(
            "/admin/agent/profiling-runs",
            json={
                "request_id": START_REQUEST_ID,
                "expected_revision": 0,
                "operations": ["files.list", "files.open", "attempt.startup"],
                "target": {"host": HOST},
                "duration_seconds": duration_seconds,
            },
        )
        assert started.status_code == 201, started.text
        body = started.json()
        run_id = body["run_id"]
        assert body["revision"] == 1
        assert body["control"]["run"]["run_id"] == run_id
        # The experiment is visible with zero samples: the point of persisting
        # it before anything is collected.
        assert body["control"]["stored_samples"] == 0
        assert body["control"]["budget_scope"] == (
            "unlimited" if duration_seconds is None else "producer_instance"
        )

        # A retry with the same id and body is the same experiment, not a
        # second one -- and does not reset the deadline.
        retried = await admin.post(
            "/admin/agent/profiling-runs",
            json={
                "request_id": START_REQUEST_ID,
                "expected_revision": 0,
                "operations": ["files.list", "files.open", "attempt.startup"],
                "target": {"host": HOST},
                "duration_seconds": duration_seconds,
            },
        )
        assert retried.status_code == 201, retried.text
        assert retried.json()["run_id"] == run_id
        assert retried.json()["revision"] == 1

        # -- 3. The control plane's own policy loop picks it up ---------
        await _await_policy(run_id=run_id)
        published = operation_profiling.policy_store().published()
        assert published is not None and published.run is not None
        assert published.run.run_id == run_id
        assert published.run.source == operation_profiling.SOURCE_ADMIN
        assert published.state.config_version == 1
        # No lease: the experiment's own absolute deadline is what ends
        # collection, including on a node that never hears anything again.
        assert published.run.deadline_mono > time.monotonic()

        # -- 3b. The runner's own sync carries it to the broker --------
        # Real client, real endpoints, real credential: the control plane's
        # exchange, then the broker's own policy route. This is the wiring that
        # a sync built without a broker client silently skipped.
        from cloud_agent_host.broker_profiling_policy import applied_state
        from cloud_agent_host.profiling_policy import ProfilingPolicySync
        from cloud_agent_host.session_supervisor_client import SessionSupervisorClient

        broker_client = SessionSupervisorClient(
            chain.broker_base_url, "acceptance-broker-token", timeout=10
        )
        sync = ProfilingPolicySync(
            base_url=chain.base_url,
            credential=chain.host_credential,
            host=HOST,
            broker=broker_client,
        )
        try:
            # Off the event loop: the sync is synchronous, and both servers are
            # tasks on this loop. Calling it inline would block the loop that
            # has to answer the request it is waiting for.
            interval = await asyncio.to_thread(sync._sync_once)
        finally:
            sync.stop()
        assert interval == 5.0, "an experiment in force is polled quickly"
        state = sync.state()
        assert state["run_id"] == run_id
        assert state["applied_revision"] == 1
        assert state["broker_ok"] is True, state["broker_detail"]
        # The broker really received it: its own applied state names the run.
        assert applied_state()["revision"] == 1
        assert applied_state()["run_id"] == run_id

        # -- 4. A real file read roots a real measurement ---------------
        async with httpx.AsyncClient(base_url=chain.base_url, cookies=chain.owner_cookie) as owner:
            read = await owner.get(
                f"/v1/agent/jobs/{chain.job_id}/files", params={"path": "README.md"}
            )
        assert read.status_code == 200, read.text
        metadata = read.json().get("profiling")
        assert metadata is not None, "the read must report the selection it was measured under"
        measurement_id = metadata["measurement_id"]

        # The broker really answered a fragment for this measurement: the
        # double validates the inherited selection through the broker's own
        # `open_call`, so a refused fragment would have left the record partial.
        assert metadata["fragment_recorded"] is True

        from tests.integration.servers.test_measurement_acceptance_db import _await_row

        row = await _await_row(chain.pool, measurement_id)
        assert row["profiling_run_id"] == run_id
        assert row["producer"] == "control-plane"
        assert row["operation"] in {"files.list", "files.open"}
        record = json.loads(row["record"])
        assert record["run_source"] == operation_profiling.SOURCE_ADMIN
        assert record["control_revision"] == 1

        # -- 5. The page reads the same truth ---------------------------
        live = (await admin.get("/admin/agent/profiling-control")).json()
        assert live["run"]["run_id"] == run_id
        assert live["stored_samples"] == 1
        assert live["status"] == "on"
        assert live["run_active"] is True
        if duration_seconds is None:
            assert live["run"]["expires_at"] is None
            assert live["run"]["continuous"] is True
            assert record["run_expires_at"] is None
        else:
            assert live["run"]["seconds_remaining"] > 0
        # The one sample is readable through the history API, which is what
        # "preserve history" means.
        history = await admin.get("/admin/agent/measurements", params={"run_id": run_id})
        assert history.status_code == 200
        assert any(
            item["measurement_id"] == measurement_id for item in history.json()["measurements"]
        )

        # -- 6. Stop ----------------------------------------------------
        stopped = await admin.post(
            f"/admin/agent/profiling-runs/{run_id}/stop",
            json={"request_id": STOP_REQUEST_ID},
        )
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["revision"] == 2
        assert stopped.json()["control"]["status"] == "off"
        # Nothing is in force, and the stopped experiment is still named beside
        # it -- so the page can say *what* was stopped rather than showing the
        # same blank "Off" as a deployment that has never collected. The two
        # fields are separate because a reader that confused them would keep
        # collecting for an experiment that was already over.
        assert stopped.json()["control"]["run"] is None
        assert stopped.json()["control"]["run_active"] is False
        assert stopped.json()["control"]["last_stopped_run"]["run_id"] == run_id
        assert stopped.json()["control"]["last_stopped_run"]["stop_reason"]

        # The policy loop publishes the Stop, and the collector stops.
        await _await_policy(run_id=None)

        # A Stop with a new request id on the same run is idempotent rather
        # than a second command: there is nothing left to stop.
        again = await admin.post(
            f"/admin/agent/profiling-runs/{run_id}/stop",
            json={"request_id": "acceptance-stop-2"},
        )
        assert again.status_code == 200, again.text
        assert again.json()["revision"] == 2

        # -- 7. Reading after the Stop adds nothing ---------------------
        async with httpx.AsyncClient(base_url=chain.base_url, cookies=chain.owner_cookie) as owner:
            after = await owner.get(
                f"/v1/agent/jobs/{chain.job_id}/files", params={"path": "README.md"}
            )
        assert after.status_code == 200, after.text
        assert after.json().get("profiling") is None, (
            "a stopped experiment must not select anything, and must not report "
            "a measurement block for a request it did not measure"
        )
        # The already-stored sample is untouched: stopping is not deleting.
        assert await _measurement_count(chain, run_id) == 1
        final = (await admin.get("/admin/agent/profiling-control")).json()
        assert final["status"] == "off"
        assert final["run"] is None
        assert final["last_stopped_run"]["run_id"] == run_id
        assert final["last_stopped_run"]["stop_reason"]
        # No *active* run means no live sample count: the number describes the
        # experiment in force, and there is none.
        assert final["stored_samples"] == 0


@pytest.mark.parametrize("duration_seconds", [600, None])
async def test_a_measurement_open_when_the_switch_flips_still_lands(
    control_chain: _ControlChain,
    duration_seconds: int | None,
) -> None:
    """Disabling stops selecting; it does not cancel or truncate work in flight.

    This is the simplified Stop contract, end to end: a request roots its
    measurement while the experiment is on, the administrator switches it off
    through the real route, and that request still finishes through the real
    in-process persistence path -- stored, complete, and filed under the
    experiment that selected it.
    """
    import httpx
    from tests.integration.servers.test_measurement_acceptance_db import _await_row

    from cloud_agent.api._operation_measurements import record_measurement
    from cloud_agent.operation_measurement import operation as measured_operation, root_scope

    chain = control_chain
    job = await chain.store.conversation_queries.get_job(chain.job_id)
    assert job is not None
    async with httpx.AsyncClient(base_url=chain.base_url, headers=ADMIN) as admin:
        started = await admin.post(
            "/admin/agent/profiling-runs",
            json={
                "request_id": "acceptance-open-start",
                "expected_revision": 0,
                "operations": ["files.open"],
                "target": {"host": HOST},
                "duration_seconds": duration_seconds,
            },
        )
        assert started.status_code == 201, started.text
        run_id = started.json()["run_id"]
        await _await_policy(run_id=run_id)

        # A request enters while the experiment is on. Its selection is made
        # here, inside the block, and the block stays open across the Stop.
        selection = None
        scope = root_scope("files.open", job)
        with measured_operation("files.open", scope) as measure:
            assert measure is not None, "the experiment must select this request"
            selection = measure
            measurement_id = measure.measurement_id
            measure.mark("read")
            # What the route does once its read reports the kind: a path starts
            # as a candidate over listing and opening, and only the result can
            # say which it was.
            assert measure.confirm("files.open", request_kind="files.open") is True

            stopped = await admin.post(
                f"/admin/agent/profiling-runs/{run_id}/stop",
                json={"request_id": "acceptance-open-stop"},
            )
            assert stopped.status_code == 200, stopped.text
            await _await_policy(run_id=None)

            # The switch is off: a request entering now is not selected.
            assert (
                operation_profiling.start_operation(
                    operation="files.open",
                    producer="control-plane",
                    host=HOST,
                )
                is None
            )
            # -- the later stages of a selection already made -----------
            # This is the hop the review found broken: the attempt was selected
            # before the switch, and its container work happens *after* it. The
            # fragment must still be collected, under the experiment that
            # selected the attempt, or the stored record is silently partial.
            # The stage the fragment belongs to, marked the way the runner
            # marks it before it calls the broker.
            measure.mark("supervisor_acquire")
            fragment = measure.begin_call("supervisor_acquire")
            context = measure.context(fragment)
            assert context is not None
            assert context["run"]["run_id"] == run_id
            recorder = operation_profiling.start_stage_recording(context, producer="broker")
            assert recorder is not None, "a selected operation keeps its later stages"
            assert recorder.operation == "files.open"
            with recorder.stage("admission"):
                pass
            assert measure.record_answer(
                "supervisor_acquire",
                fragment,
                {
                    "profiling_call_id": fragment,
                    "profiling_recorded": True,
                    "stages": list(recorder.stages),
                },
            )

            # The request's own work succeeded, which is what the route states
            # before it builds its response.
            measure.outcome = "completed"

        assert selection is not None
        # The request that was already running finishes through the real
        # in-process persistence path, under the experiment that selected it and
        # with nothing recorded against it.
        record_measurement(store=chain.store, job=job, measure=selection)

    row = await _await_row(chain.pool, measurement_id)
    assert row["profiling_run_id"] == run_id
    assert row["outcome"] == "completed"
    # Both the root record and the fragment that arrived after the switch are
    # accounted for: nothing is missing, so the sample is usable.
    assert row["collection_complete"] is True
    record = json.loads(row["record"])
    assert record["profiling_run_id"] == run_id
    assert "profiling_stopped" not in record.get("facts", {})
    assert "missing_fragments" not in record
    stages = [item for group in record["stages"] for item in group.get("children", [])]
    assert [item["stage"] for item in stages] == ["admission"]


async def test_a_second_start_cannot_overlap_the_first(
    control_chain: _ControlChain,
) -> None:
    """Exactly one experiment at a time, and the loser learns the revision.

    Two administrators pressing Start is the ordinary way this is reached, so
    the refusal has to be a status the page can recover from: 409 carrying the
    revision it must re-read, not a generic failure.
    """
    import httpx

    chain = control_chain
    async with httpx.AsyncClient(base_url=chain.base_url, headers=ADMIN) as admin:
        first = await admin.post(
            "/admin/agent/profiling-runs",
            json={
                "request_id": "acceptance-first",
                "expected_revision": 0,
                "operations": ["attempt.startup"],
                "target": {"host": HOST},
                "duration_seconds": DURATION_SECONDS,
            },
        )
        assert first.status_code == 201, first.text
        second = await admin.post(
            "/admin/agent/profiling-runs",
            json={
                "request_id": "acceptance-second",
                "expected_revision": 0,
                "operations": ["files.open"],
                "target": {"host": HOST},
                "duration_seconds": DURATION_SECONDS,
            },
        )
        assert second.status_code == 409, second.text
        error = second.json()["detail"]["error"]
        assert error["type"] == "already_active"
        assert error["revision"] == 1


async def test_a_start_in_a_deployment_without_admin_control_is_refused(
    control_chain: _ControlChain, monkeypatch
) -> None:
    """A deployment not in Admin mode says so instead of accepting a Start."""
    import httpx

    monkeypatch.setenv(operation_profiling.ENV_PROFILING_MODE, operation_profiling.MODE_ENVIRONMENT)
    operation_profiling.reset_cache()
    try:
        async with httpx.AsyncClient(base_url=control_chain.base_url, headers=ADMIN) as admin:
            refused = await admin.post(
                "/admin/agent/profiling-runs",
                json={
                    "request_id": "acceptance-env-mode",
                    "expected_revision": 0,
                    "operations": ["attempt.startup"],
                    "target": {"host": HOST},
                    "duration_seconds": DURATION_SECONDS,
                },
            )
            assert refused.status_code == 409, refused.text
            assert refused.json()["detail"]["error"]["type"] == "not_admin_mode"
            status = (await admin.get("/admin/agent/profiling-control")).json()
            # Not "off": this deployment cannot be controlled from here at all,
            # and the page must be able to say which of the two it is.
            assert status["status"] == "unmanaged"
            assert status["mode"] == operation_profiling.MODE_ENVIRONMENT
    finally:
        monkeypatch.setenv(operation_profiling.ENV_PROFILING_MODE, operation_profiling.MODE_ADMIN)
        operation_profiling.reset_cache()


async def test_an_unknown_host_or_operation_is_refused_before_anything_starts(
    control_chain: _ControlChain,
) -> None:
    """A request that could not measure anything is refused, not half-applied."""
    import httpx

    chain = control_chain
    async with httpx.AsyncClient(base_url=chain.base_url, headers=ADMIN) as admin:
        unknown_operation = await admin.post(
            "/admin/agent/profiling-runs",
            json={
                "request_id": "acceptance-bad-op",
                "expected_revision": 0,
                "operations": ["files.delete"],
                "target": {"host": HOST},
                "duration_seconds": DURATION_SECONDS,
            },
        )
        assert unknown_operation.status_code == 422, unknown_operation.text
        assert "files.delete" in unknown_operation.json()["detail"]["error"]["message"]

        unknown_host = await admin.post(
            "/admin/agent/profiling-runs",
            json={
                "request_id": "acceptance-bad-host",
                "expected_revision": 0,
                "operations": ["files.open"],
                "target": {"host": "no-such-host"},
                "duration_seconds": DURATION_SECONDS,
            },
        )
        assert unknown_host.status_code == 422, unknown_host.text
        assert "no-such-host" in unknown_host.json()["detail"]["error"]["message"]

        too_long = await admin.post(
            "/admin/agent/profiling-runs",
            json={
                "request_id": "acceptance-too-long",
                "expected_revision": 0,
                "operations": ["files.open"],
                "target": {"host": HOST},
                "duration_seconds": 3600,
            },
        )
        assert too_long.status_code == 422, too_long.text

        # Nothing above moved the control row: a refusal is not a partial Start.
        state = (await admin.get("/admin/agent/profiling-control")).json()
        assert state["status"] == "off"
        assert state["revision"] == 0
        assert state["run"] is None


async def test_an_ordinary_user_cannot_control_collection(
    control_chain: _ControlChain,
) -> None:
    """The control surface is admin-only, and says so rather than 404-ing."""
    import httpx

    async with httpx.AsyncClient(base_url=control_chain.base_url) as anonymous:
        read = await anonymous.get("/admin/agent/profiling-control")
        assert read.status_code in {401, 403}
        write = await anonymous.post(
            "/admin/agent/profiling-runs",
            json={
                "request_id": "acceptance-anon",
                "expected_revision": 0,
                "operations": ["attempt.startup"],
                "target": {},
                "duration_seconds": DURATION_SECONDS,
            },
        )
        assert write.status_code in {401, 403}
    assert os.environ.get("ADMIN_TOKEN") == "acceptance-admin-token"
