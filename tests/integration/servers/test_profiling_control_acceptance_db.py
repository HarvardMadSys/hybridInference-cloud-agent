"""Acceptance: an administrator's Start and Stop really control collection.

This walks the whole chain against a real PostgreSQL, a real schema applied by
Alembic, and the real FastAPI applications served on real sockets:

1. `POST /admin/agent/profiling-runs` creates the experiment and moves the
   control row.
2. The control plane's own policy worker reads that row and publishes the
   snapshot its routes then collect under -- without a restart and without an
   environment variable changing.
3. A real file read through the workspace route roots a measurement, hands the
   selection to the broker over the real fragment protocol, and stores what came
   back.
4. `GET /admin/agent/profiling-control` reads the same state back, including the
   real stored-sample count.
5. A Stop stops it: the next read produces no sample at all, while everything
   already stored stays readable.

What is *simulated* here is stated rather than hidden. There is no container in
this environment, so the broker is the in-process double the existing
measurement acceptance test uses -- it validates selections through the real
`operation_call` code but answers a workspace read from a fixture. The runner's
parent/child handoff, the broker's own policy endpoint and the offline lease
are covered in `tests/unit/test_profiling_control_application.py`,
`tests/unit/test_operation_profiling_runtime_control.py` and
`tests/servers/test_profiling_control_api.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
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
from cloud_agent.profiling_control_config import profiling_control_deployment
from cloud_agent.storage.assembly import Storage
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
    # Admin control, and *only* Admin control: the legacy switch is left off and
    # a stale descriptor is deliberately left behind to prove it is ignored.
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_MODE, operation_profiling.MODE_ADMIN)
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
    monkeypatch.setenv("AGENT_OPERATION_PROFILING_DEPLOYMENT", "acceptance")
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
    )
    try:
        yield chain
    finally:
        worker_task.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
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


async def test_start_collects_reads_a_real_sample_and_stop_ends_it(
    control_chain: _ControlChain,
) -> None:
    """The whole acceptance: Start, collect, read back, Stop, stay stopped."""
    import httpx

    chain = control_chain
    async with httpx.AsyncClient(base_url=chain.base_url, headers=ADMIN) as admin:
        # -- 1. Off, and honest about it --------------------------------
        before_response = await admin.get("/admin/agent/profiling-control")
        assert before_response.status_code == 200, before_response.text
        before = before_response.json()
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
                "duration_seconds": DURATION_SECONDS,
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
        assert body["control"]["budget_scope"] == "producer_instance"

        # A retry with the same id and body is the same experiment, not a
        # second one -- and does not reset the deadline.
        retried = await admin.post(
            "/admin/agent/profiling-runs",
            json={
                "request_id": START_REQUEST_ID,
                "expected_revision": 0,
                "operations": ["files.list", "files.open", "attempt.startup"],
                "target": {"host": HOST},
                "duration_seconds": DURATION_SECONDS,
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
        assert published.policy.control_revision == 1
        # The runtime lease is bounded, so an offline node stops on its own.
        assert published.authorization_ttl_s > 0
        assert published.authorization_ttl_s <= 15.0
        authorization = operation_profiling.policy_store().authorization()
        assert authorization is not None
        assert authorization.deadline_mono < published.run.deadline_mono

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
        assert live["status"] in {"collecting", "partially_active", "starting"}
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
        assert stopped.json()["control"]["status"] == "stopped"
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
        assert final["status"] == "stopped"
        assert final["run"] is None
        assert final["last_stopped_run"]["run_id"] == run_id
        assert final["last_stopped_run"]["stop_reason"]
        # No *active* run means no live sample count: the number describes the
        # experiment in force, and there is none.
        assert final["stored_samples"] == 0


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
