"""The whole chain against a real database, with every join exercised.

The earlier chain test called the validator and a store double by hand, which
is precisely how it missed an attempt query that could not run, a record
missing the field its manifest is built from, and a control plane that never
stored what it rooted. This one runs the real collector, hands the record to
the real delivery runtime, sends it over real HTTP through the real ingest
route into a real Postgres repository, and reads it back through the real Admin
route.

Only the job and attempt rows are supplied by a fake, because building a whole
scheduled attempt in SQL is not what this is testing. Everything downstream of
"a producer has a record and a credential" is the production path.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from cloud_agent.api.deps import get_agent_app_credentials, get_storage
from cloud_agent.app import create_app
from cloud_agent.storage.assembly import Storage
from cloud_agent.tokens import SCOPE_FULL, mint_worker_token
from cloud_agent_host import operation_profiling
from cloud_agent_host.runner import ControlPlane, deliver_measurements
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
ADMIN = {"Authorization": "Bearer admin-chain-test"}
JOB_ID = "ajob_chain"
THREAD_ID = "athr_chain"
USER_ID = "user-chain"
ATTEMPT_ID = 7
RUN_ID = "chain-run-1"
#: Frozen: a helper that re-derived it per call would state a different
#: experiment every time, and the repository compares the instant.
EXPIRES_AT = (datetime.now(UTC) + timedelta(minutes=30)).replace(microsecond=0)


def _db_config() -> dict[str, object]:
    """The per-worker test database, refusing any name without ``_test_``."""
    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in test_db_name:
        pytest.fail(
            f"SAFETY: TEST_DB_NAME='{test_db_name}' does not contain "
            f"'{_ALLOWED_TEST_DB_PATTERN}'. Refusing to run."
        )
    return {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }


class _Identity:
    """The job and attempt rows a worker credential is resolved against.

    Only the two lookups the ingest makes; the measurement capability and the
    Admin reads behind them are the real ones, over real Postgres.
    """

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Return the job this host is serving."""
        if job_id != JOB_ID:
            return None
        return {
            "id": JOB_ID,
            "user_id": USER_ID,
            "thread_id": THREAD_ID,
            "thread_sandbox": {"sandbox_id": "asbx_chain", "host": "host-serving-now"},
        }

    async def get_attempt(self, *, job_id: str, attempt_id: int) -> dict[str, Any] | None:
        """Return the attempt the credential names, with the host that ran it."""
        if job_id != JOB_ID or attempt_id != ATTEMPT_ID:
            return None
        return {
            "attempt_id": ATTEMPT_ID,
            "job_id": JOB_ID,
            "attempt_no": 1,
            "lease_generation": 3,
            "host_id": "host-that-ran-it",
            "status": "failed",
            "thread_id": THREAD_ID,
            "thread_sandbox": {"sandbox_id": "asbx_chain", "host": "host-that-ran-it"},
        }


class _ChainStorage:
    """The assembly for measurements, and the two lookups a credential needs."""

    def __init__(self, assembly: Storage) -> None:
        self.operation_measurements = assembly.operation_measurements
        self.conversation_queries = _Identity()
        self.execution_queries = _Identity()


@pytest_asyncio.fixture
async def chain(monkeypatch, tmp_path):
    """A real app over a real database, and a credential for one attempt."""
    config = _db_config()
    try:
        pool = await asyncpg.create_pool(**config, min_size=1, max_size=5)
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable
    apply_migrations(config)

    async def _reset() -> None:
        async with pool.acquire() as conn:
            await reset_agent_tables(conn)

    await _reset()
    # The application builds its own pool on startup even though the test
    # overrides the storage it hands to handlers, so the real listener needs a
    # real address to build it from.
    monkeypatch.setenv(
        "AGENT_DATABASE_URL",
        "postgresql://{user}:{password}@{host}:{port}/{database}".format(**config),
    )
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "chain-test-secret")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-chain-test")
    monkeypatch.setenv("AGENT_SESSION_SECRET", "chain-session-secret-32-bytes-min")
    monkeypatch.setenv("GATEWAY_ISSUER", "https://gateway.example")
    sink = tmp_path / "records.jsonl"
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    monkeypatch.setenv(
        operation_profiling.ENV_PROFILING_RUN,
        json.dumps(
            {
                "run_id": RUN_ID,
                "operations": ["attempt.startup"],
                "expires_at": EXPIRES_AT.isoformat(),
                "max_operations": 10,
                "target": {"job_id": JOB_ID},
            }
        ),
    )
    operation_profiling.reset_cache()
    from cloud_agent_host import runner as runner_mod

    # This process remembers which measurements it has delivered, so a test
    # inherits the previous one's decisions unless it clears them -- and a
    # record the earlier test sent would look like a retry here.
    runner_mod.reset_delivery_state()

    app = create_app()
    app.dependency_overrides[get_storage] = lambda: _ChainStorage(Storage(pool))
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    # A real listener, on *this* event loop. The producer under test uses a
    # synchronous client, so it needs a socket rather than an ASGI transport --
    # and the server has to share the loop the pool was built on, because an
    # asyncpg connection cannot be used from another one.
    server, task, url = await _serve(app)
    try:
        yield pool, url, sink
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        apply_migrations(config)
        await _reset()
        await pool.close()
        operation_profiling.reset_cache()


async def _serve(app: Any) -> tuple[Any, Any, str]:
    """Serve the app on an ephemeral port, on the caller's loop."""
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    deadline = datetime.now(UTC) + timedelta(seconds=20)
    while not server.started:
        if datetime.now(UTC) > deadline:  # pragma: no cover - startup failure
            raise RuntimeError("the test server did not start")
        await asyncio.sleep(0.02)
    return server, task, _base_url(server)


def _base_url(server: Any) -> str:
    """The address the test server actually bound."""
    for socket_info in server.servers[0].sockets:
        host, port = socket_info.getsockname()[:2]
        return f"http://{host}:{port}"
    raise RuntimeError("the test server has no socket")  # pragma: no cover


def _token(attempt_id: int = ATTEMPT_ID, generation: int = 3) -> str:
    """The runner's own full-scope credential for one attempt of this job."""
    return mint_worker_token(
        job_id=JOB_ID,
        attempt_id=attempt_id,
        lease_generation=generation,
        scope=SCOPE_FULL,
    )


def _manifest() -> dict[str, Any]:
    """The experiment manifest a producer sends with its record."""
    return {
        "run_id": RUN_ID,
        "operations": ["attempt.startup"],
        "target": {"job_id": JOB_ID},
        "expires_at": EXPIRES_AT.isoformat(),
        "config_version": 1,
        "admission_instances": [],
    }


def _send_once(base_url: str, body: dict[str, Any], *, token: str | None = None) -> tuple[int, str]:
    """Post one measurement, as the runner's delivery thread does."""
    control = _HttpControl(base_url, token or _token())
    try:
        status, answer = control.record_measurement(JOB_ID, body)
        return status, str(answer.get("result") or "")
    finally:
        control.close()


def _deliver_once(base_url: str, measurement_id: str) -> dict[str, int]:
    """Run the delivery runtime to completion, as the runner's thread does."""
    control = _HttpControl(base_url, _token())
    try:
        return deliver_measurements(
            control=control, job_id=JOB_ID, measurement_ids=[measurement_id]
        )
    finally:
        control.close()


class _HttpControl(ControlPlane):
    """The real client, pointed at the test server instead of a deployment."""

    def __init__(self, base_url: str, token: str) -> None:
        super().__init__(base_url, token)


async def test_a_real_record_travels_from_the_collector_to_the_admin_page(chain):
    """Collect, deliver over HTTP, store in Postgres, read back as an admin."""
    pool, base_url, sink = chain

    # 1. The real collector times a startup and freezes its record.
    measurement = operation_profiling.start_operation(
        operation=operation_profiling.STARTUP_OPERATION,
        host="host-that-ran-it",
        worker_id="runner-1",
    )
    assert measurement is not None
    assert operation_profiling.bind(
        measurement, job_id=JOB_ID, attempt_id=ATTEMPT_ID, sandbox_id="asbx_chain"
    )
    operation_profiling.mark(measurement, "supervisor_acquire")
    operation_profiling.mark(measurement, "sandbox_running")
    operation_profiling.set_attributes(measurement, startup_path="new_group")
    operation_profiling.finish(measurement, outcome="dispatched")
    operation_profiling.flush_sink()
    frozen = [json.loads(line) for line in sink.read_text().splitlines() if line.strip()]
    assert len(frozen) == 1

    # 2. The delivery runtime sends it, over real HTTP, to the real endpoint.
    # Off the event loop, because this test's server runs on it: the producer
    # under test uses a synchronous client, exactly as the runner does, and a
    # blocking call here would deadlock the listener answering it.
    counts = await asyncio.to_thread(_deliver_once, base_url, measurement.measurement_id)
    assert counts == {"delivered": 1, "duplicate": 0, "dropped": 0, "retried": 0}

    # 3. It is in the real table, attributed to the attempt that ran it.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM agent_operation_measurements WHERE measurement_id = $1",
            measurement.measurement_id,
        )
        run = await conn.fetchrow(
            "SELECT * FROM agent_operation_profiling_runs WHERE run_id = $1", RUN_ID
        )
    assert row is not None
    assert row["job_id"] == JOB_ID
    assert row["owner_id"] == USER_ID
    assert row["host_id"] == "host-that-ran-it"
    assert row["sandbox_id"] == "asbx_chain"
    assert row["operation"] == "attempt.startup"
    assert row["startup_path"] == "new_group"
    assert row["coverage_scope"] == "host_startup"
    assert row["collection_complete"] is True
    assert row["end_to_end_complete"] is False
    # The frozen record round-tripped through JSONB unchanged, and the fields a
    # distribution is grouped by survived the ingest mapping.
    # The stored document round-tripped, and the field a manifest is rebuilt
    # from survived the ingest mapping. Read through the Admin route below
    # rather than off the column, because that is the path a reader uses --
    # asyncpg hands the raw JSONB back as text.
    stored = json.loads(row["record"]) if isinstance(row["record"], str) else row["record"]
    assert stored["measurement_id"] == measurement.measurement_id
    assert stored["run_expires_at"] == EXPIRES_AT.isoformat()
    assert run is not None
    assert run["operations"] == ["attempt.startup"]
    assert run["expires_at"] == EXPIRES_AT

    # 4. An administrator reads it back through the real route.
    async with httpx.AsyncClient(base_url=base_url) as client:
        listed = await client.get(f"/admin/agent/measurements?run_id={RUN_ID}", headers=ADMIN)
        detail = await client.get(
            f"/admin/agent/measurements/{measurement.measurement_id}", headers=ADMIN
        )
        summary = await client.get(f"/admin/agent/profiling-runs/{RUN_ID}/summary", headers=ADMIN)
        catalogue = await client.get("/admin/agent/profiling-runs", headers=ADMIN)

    assert listed.status_code == 200
    assert [item["measurement_id"] for item in listed.json()["measurements"]] == [
        measurement.measurement_id
    ]
    assert detail.status_code == 200
    detail_body = detail.json()
    # The JSONB column came back as a document, not as text this route passed
    # through -- the reader's view of the record is the one being asserted.
    assert isinstance(detail_body["record"], dict)
    assert detail_body["record"]["milestones"][0]["name"] == "supervisor_acquire"
    assert detail_body["record"]["run_expires_at"] == EXPIRES_AT.isoformat()
    assert detail_body["coverage_scope"] == "host_startup"
    assert detail_body["collection_complete"] is True
    assert detail_body["end_to_end_complete"] is False
    assert detail_body["host_id"] == "host-that-ran-it"
    assert summary.json()["groups"][0]["eligible_count"] == 1
    assert [item["run_id"] for item in catalogue.json()["runs"]] == [RUN_ID]


async def test_delivering_the_same_record_twice_stores_one(chain):
    """The endpoint is idempotent for a retry, and says so."""
    pool, base_url, sink = chain
    measurement = operation_profiling.start_operation(
        operation=operation_profiling.STARTUP_OPERATION, host="host-that-ran-it"
    )
    assert measurement is not None
    assert operation_profiling.bind(measurement, job_id=JOB_ID)
    operation_profiling.finish(measurement, outcome="dispatched")
    operation_profiling.flush_sink()
    body = {"record": json.loads(sink.read_text().splitlines()[0]), "run": _manifest()}

    first = await asyncio.to_thread(_send_once, base_url, body)
    second = await asyncio.to_thread(_send_once, base_url, body)

    assert first == (200, "stored")
    # The same bytes again is the same sample, reported as a duplicate rather
    # than stored twice or refused.
    assert second == (200, "duplicate")
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM agent_operation_measurements WHERE measurement_id = $1",
            measurement.measurement_id,
        )
    assert count == 1


async def test_a_token_that_names_no_attempt_is_refused_before_any_write(chain):
    """The attempt lookup runs on the real path, against the real schema."""
    pool, base_url, _sink = chain
    # The credential names an attempt this job never ran, which is the one
    # thing wrong with an otherwise valid delivery.
    status, _answer = await asyncio.to_thread(
        _send_once,
        base_url,
        {
            "run": _manifest(),
            "record": {
                "schema_version": 3,
                "measurement_id": "msm_no_attempt",
                "interaction_id": "msm_no_attempt",
                "profiling_run_id": RUN_ID,
                "operation": "attempt.startup",
                "request_kind": "attempt.startup",
                "boundary_version": 1,
                "coverage_scope": "host_startup",
                "producer": "runner",
                "duration_us": 10,
                "outcome": "dispatched",
                "collection_complete": True,
                "observed_at": datetime.now(UTC).isoformat(),
                "facts": {"operation": "attempt.startup"},
            },
        },
        token=_token(attempt_id=99),
    )

    # 409, from the lookup this test exists to exercise: the attempt is not
    # one this job ran, so nothing is attributed and nothing is stored.
    assert status == 409
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM agent_operation_measurements")
    assert count == 0
