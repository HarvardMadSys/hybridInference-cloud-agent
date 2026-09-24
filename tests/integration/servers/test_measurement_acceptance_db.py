"""One acceptance chain: real auth, real HTTP, real Postgres, real Admin reads.

The tests beside this one each prove a piece. ``test_measurement_chain_db``
runs the delivery runtime into a real table but supplies the job and attempt
rows from a fake and roots every record by hand. ``test_agent_job_store``
builds real jobs, attempts and sandboxes but never serves a request.
``test_operation_measurement_routes`` drives the real owner routes but over an
in-memory storage double and an ASGI transport, so nothing it stores is
validated or written by the real repository. This module joins the three: a
real uvicorn listener, the real identity path (a minted session cookie and the
real owner guard), real jobs/attempts/sandboxes created through the real
repositories, the real collector, the real delivery runtime, the real
measurement ingest, the real bounded writer and the real Admin API -- all over
one test database.

What that buys, stated as the properties under test:

* a startup record is stored while the model is still running, because it is
  frozen and delivered at the prompt handoff rather than at the turn's exit;
* a startup that failed before dispatch is still delivered, and the exit's
  wait for it is bounded by the configured drain rather than by the
  destination;
* a terminal attach is persisted while its SSE body is still open, which is
  what makes an hour-long stream one short sample instead of a late one;
* file and terminal failures are filed by the classification rules -- an
  ambiguous file kind is refused and counted, not stored under a made-up
  operation, while a terminal failure after selection is stored as failed;
* database congestion costs samples and never the response, and the writer's
  pending/in-flight/drop counts are the ones an operator reads;
* profiling off is no tasks, no deliveries, no writes, and no new fields.

Nothing here re-implements a producer: the broker stand-in answers the real
fragment protocol (:mod:`cloud_agent_host.operation_call`), the writer is
the application's own, and every row is read back through SQL and through the
Admin routes a reader uses.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations

from cloud_agent import broker_routing, measurement_writer, operation_measurement
from cloud_agent.api.deps import get_agent_app_credentials, get_gateway_client
from cloud_agent.app import create_app
from cloud_agent.identity import SESSION_COOKIE, mint_session
from cloud_agent.operation_measurement import INTERACTION_HEADER
from cloud_agent.storage.assembly import Storage
from cloud_agent.tokens import SCOPE_FULL, mint_worker_token
from cloud_agent.workspace_broker_client import WorkspaceBrokerError, WorkspaceBrokerStream
from cloud_agent_host import operation_call, runner as runner_mod
from cloud_agent_host.measurement_delivery import (
    DELIVERY_DRAIN_S,
    DELIVERY_HANDOFF_DRAIN_S,
)
from cloud_agent_host.runner import ClaimedJob, ControlPlane
from cloud_agent_host.shared_protocol import (
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    SHARED_SANDBOX_CAPABILITY,
    SHARED_SANDBOX_HELPER_PINS,
    SHARED_SANDBOX_PROTOCOL,
    SHARED_SANDBOX_RUNTIME_PINS,
    shared_deployment_capability,
)
from cloud_agent_shared import operation_profiling

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    from cloud_agent_shared.operation_profiling import Measurement

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"

#: Every request in this module is made by one real owner and read by one real
#: administrator, both authenticated the way the deployment authenticates them.
USER_ID = "user-acceptance"
ADMIN = {"Authorization": "Bearer acceptance-admin-token"}
REPO = "acceptance/repo"
HOST = "acceptance-host"
CREDENTIAL_HASH = "acceptance-credential-hash"
RUNTIME = "claude-code"
MODEL = "glm-5.1"
SANDBOX_ID = "asbx_acceptance"
TERMINAL_ID = "term_acceptance"
RUN_ID = "acceptance-run-1"
#: The operations this acceptance experiment selects: one startup and the two
#: owner surfaces whose failures have classification rules of their own.
OPERATIONS = frozenset({"attempt.startup", "files.list", "files.open", "terminal.open"})
STARTUP_SINK_NAME = "records.jsonl"
#: Generous, because no test here exists to watch a lease lapse; the reaper the
#: real application starts must not be able to take the attempt away mid-test.
LEASE_TTL_S = 600.0
#: Frozen for the life of the process: the manifest is the experiment's
#: identity, and a descriptor re-derived per call would state a different one.
RUN_EXPIRES_AT = (datetime.now(UTC) + timedelta(minutes=30)).replace(microsecond=0)

_SHARED_HOST_CAPABILITIES = [
    SHARED_SANDBOX_CAPABILITY,
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    shared_deployment_capability("acceptance-deployment"),
]


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


def _dsn(config: Mapping[str, object]) -> str:
    """The asyncpg DSN the application's own pool is built from."""
    return "postgresql://{user}:{password}@{host}:{port}/{database}".format(**config)


async def _reset(pool: Any) -> None:
    """Empty every ``agent_*`` table, before and after each test."""
    async with pool.acquire() as conn:
        await reset_agent_tables(conn)


class _Gateway:
    """The one Gateway answer the owner guard needs, and nothing else."""

    async def agent_access(self, user_id: str) -> dict[str, Any]:
        """Admit this owner with the permission the routes require."""
        return {"user_id": user_id, "allowed": True, "permissions": ["agent.use"]}


class _UnusedClient:
    """A client the streaming stand-in never opens; the stream owns no socket."""

    async def aclose(self) -> None:
        """Close nothing."""
        return None


class _Broker:
    """A workspace broker that answers the fragment protocol as the real one does.

    It reuses the broker's own :func:`operation_call.open_call` and
    :class:`operation_call.BrokerCall`, so the route under test validates a real
    selection and records a real answer. What is stated here rather than
    performed is the workspace's own content: this process has no container, so
    the read's ``kind`` is the one line a test supplies.
    """

    def __init__(self) -> None:
        #: When set, the next broker call raises it, exactly as a broker that
        #: could not reach the workspace does.
        self.fail: WorkspaceBrokerError | None = None
        self.calls: list[dict[str, Any]] = []
        self.registered: list[dict[str, Any]] = []
        self._descriptor = {
            "id": TERMINAL_ID,
            "shell": "/bin/bash",
            "state": "running",
            "cwd": "/workspace",
            "rows": 24,
            "cols": 80,
            "last_seq": 0,
        }

    def _open(self, context: Any, *operations: str) -> operation_call.BrokerCall:
        """Validate the inherited selection the way the broker's route does."""
        call = operation_call.open_call(context, operations=operations)
        with call.stage("admission"):
            pass
        return call

    async def register_shared_sandbox(self, _sandbox: dict[str, Any]) -> None:
        """Accept the manifest re-registration a workspace request performs."""
        self.registered.append(dict(_sandbox))

    @staticmethod
    def _file_operation(path: str) -> str:
        """Return the operation a read's own kind implies."""
        return "files.open" if path else "files.list"

    def _check(self) -> None:
        """Raise the failure this broker was told to report, if any."""
        if self.fail is not None:
            raise self.fail

    async def files(
        self, workspace_id: str, path: str, *, profiling: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Answer one file or directory read."""
        self.calls.append({"op": "files", "workspace_id": workspace_id, "path": path})
        candidates = ["files.list", "files.open"] if path else ["files.list"]
        call = self._open(profiling, *candidates)
        self._check()
        with call.stage("exec"):
            pass
        kind = "directory" if not path else "file"
        body: dict[str, Any] = (
            {"path": path, "kind": "directory", "entries": [], "writable": True}
            if kind == "directory"
            else {"path": path, "kind": "file", "content": "hello", "size": 5}
        )
        call.set_operation(self._file_operation(path))
        return call.answer(body)

    async def create_terminal(
        self,
        workspace_id: str,
        *,
        rows: int,
        cols: int,
        profiling: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Answer one PTY create."""
        self.calls.append({"op": "create_terminal", "workspace_id": workspace_id})
        call = self._open(profiling, "terminal.open")
        self._check()
        call.set_operation("terminal.open")
        with call.stage("pty_spawn"):
            pass
        return call.answer(dict(self._descriptor))

    async def list_terminals(
        self, workspace_id: str, *, profiling: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Answer one terminal list."""
        self.calls.append({"op": "list_terminals", "workspace_id": workspace_id})
        call = self._open(profiling, "terminal.open")
        self._check()
        call.set_operation("terminal.open")
        with call.stage("list"):
            pass
        return call.answer({"terminals": [dict(self._descriptor)]})

    async def stream_terminal(
        self,
        workspace_id: str,
        terminal_id: str,
        *,
        after: int,
        profiling: dict[str, Any] | None = None,
    ) -> Any:
        """Answer a stream in headers, the way the broker's SSE route does.

        The body is the terminal's own bytes and is deliberately not consumed
        by the caller: the route must have persisted the attach before a single
        frame travels, which is the ordering the acceptance test asserts.
        """
        self.calls.append({"op": "stream_terminal", "workspace_id": workspace_id})
        call = self._open(profiling, "terminal.open")
        self._check()
        call.set_operation("terminal.open")
        with call.stage("stream_attach"):
            pass
        response = httpx.Response(
            200,
            headers=call.headers(),
            stream=httpx.ByteStream(b"data: hello\n\n"),
        )
        return WorkspaceBrokerStream(
            _UnusedClient(),
            response,
            profiling_answer=operation_profiling.answer_from_headers(response.headers),
        )


@dataclass
class _Chain:
    """One live chain: the real app over the real rows, and one broker."""

    pool: Any
    store: Storage
    base_url: str
    broker: _Broker
    job: dict[str, Any]
    claim: dict[str, Any]
    sandbox: dict[str, Any]
    sink: Any

    @property
    def job_id(self) -> str:
        """The job the owner routes address."""
        return str(self.job["id"])

    @property
    def thread_id(self) -> str:
        """The thread the job's retained workspace belongs to."""
        return str(self.job["thread_id"])

    @property
    def attempt_id(self) -> int:
        """The attempt this job's claim created."""
        return int(self.claim["attempt_id"])

    @property
    def lease_generation(self) -> int:
        """The fencing generation of that attempt."""
        return int(self.claim["lease_generation"])

    def session_cookie(self) -> dict[str, str]:
        """A real minted owner session, as the browser would present it."""
        return {SESSION_COOKIE: mint_session(user_id=USER_ID)}

    def worker_token(self) -> str:
        """A real full-scope credential for this attempt, as the runner holds."""
        return mint_worker_token(
            job_id=self.job_id,
            attempt_id=self.attempt_id,
            lease_generation=self.lease_generation,
            scope=SCOPE_FULL,
        )

    def claimed_job(self) -> ClaimedJob:
        """The turn's own view of the claim the delivery runtime sends with."""
        return ClaimedJob(
            job_id=self.job_id,
            thread_id=self.thread_id,
            attempt_id=self.attempt_id,
            attempt_no=int(self.claim["attempt_no"]),
            repo=REPO,
            base_sha=self.job["base_sha"],
            task_prompt=str(self.job["task_prompt"]),
            runtime=RUNTIME,
            model=MODEL,
            worker_token=self.worker_token(),
            sandbox_token="unused-by-measurement-delivery",
            execution_profile=str(self.job["execution_profile"]),
            runtime_version=self.claim.get("runtime_version"),
            thread_sandbox=self.job.get("thread_sandbox"),
        )


def _shared_claim_attestation() -> dict[str, object]:
    """Return the exact process-local fence a shared claim requires."""
    return {
        "sandbox_image_digest": "sha256:acceptance-image",
        "shared_sandbox_protocol": SHARED_SANDBOX_PROTOCOL,
        "runner_capabilities": _SHARED_HOST_CAPABILITIES,
        "runtime_versions": dict(SHARED_SANDBOX_RUNTIME_PINS),
        "helper_versions": dict(SHARED_SANDBOX_HELPER_PINS),
    }


async def _arrange_chain(store: Storage) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Create one real host, job, attempt and retained sandbox.

    This is the production sequence the store tests already exercise: enrol a
    host, heartbeat it online, create the job, claim it under the shared
    attestation, reserve the retained group, and let the turn report its
    workspace ready -- which is the durable barrier the terminal routes fence
    on.
    """
    await store.host_pool.register_host(
        host=HOST,
        credential_hash=CREDENTIAL_HASH,
        capabilities=_SHARED_HOST_CAPABILITIES,
        slots_total=4,
    )
    online = await store.host_pool.record_host_heartbeat(
        host=HOST,
        capabilities=_SHARED_HOST_CAPABILITIES,
        sandbox_image_digest="sha256:acceptance-image",
    )
    assert online is not None and online["status"] == "online"
    job = await store.conversation_commands.create_job(
        user_id=USER_ID,
        repo=REPO,
        task_prompt="measure the acceptance chain",
        runtime=RUNTIME,
        model=MODEL,
        base_sha="a" * 40,
    )
    claim = await store.attempt_commands.claim_job(
        worker_id="acceptance-worker",
        lease_ttl_seconds=LEASE_TTL_S,
        host=HOST,
        host_credential_hash=CREDENTIAL_HASH,
        claim_attestation=_shared_claim_attestation(),
    )
    assert claim is not None
    sandbox = await store.sandbox.reserve_thread_sandbox(
        thread_id=str(job["thread_id"]),
        attempt_id=int(claim["attempt_id"]),
        lease_generation=int(claim["lease_generation"]),
        host=HOST,
        sandbox_id=SANDBOX_ID,
        container_name="freeinference-acceptance",
        runtime_id=RUNTIME,
        runtime_version=claim.get("runtime_version"),
        sandbox_image_digest=claim.get("sandbox_image_digest"),
        execution_profile=str(job["execution_profile"]),
    )
    assert sandbox is not None and sandbox["shared_live"] is True
    assert await store.attempt_commands.append_event(
        attempt_id=int(claim["attempt_id"]),
        lease_generation=int(claim["lease_generation"]),
        event_type="lifecycle",
        payload={"phase": "workspace_ready"},
    )
    refreshed = await store.conversation_queries.get_job(str(job["id"]))
    assert refreshed is not None
    return refreshed, claim, sandbox


async def _serve(app: Any) -> tuple[Any, Any, str]:
    """Serve the real application on an ephemeral port, on the caller's loop."""
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


@asynccontextmanager
async def _running_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, *, profiling: bool
) -> AsyncIterator[_Chain]:
    """Build the whole chain, with the switch on or off, and tear it down."""
    config = _db_config()
    try:
        pool = await asyncpg.create_pool(**config, min_size=1, max_size=5)
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable
    apply_migrations(config)
    await _reset(pool)

    # The application builds its own pool from this variable during the
    # lifespan, and its schema is Alembic's, so the address has to name the
    # same test database this module arranged its rows in.
    monkeypatch.setenv("AGENT_DATABASE_URL", _dsn(config))
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "acceptance-control-secret")
    monkeypatch.setenv("ADMIN_TOKEN", "acceptance-admin-token")
    monkeypatch.setenv("AGENT_SESSION_SECRET", "acceptance-session-secret-32-bytes!")
    monkeypatch.setenv("GATEWAY_ISSUER", "https://gateway.example")
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", REPO)
    monkeypatch.setenv("AGENT_WORKSPACE_BROKER_TOKEN", "acceptance-broker-token")
    sink = tmp_path / STARTUP_SINK_NAME
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    if profiling:
        monkeypatch.setenv(operation_profiling.ENV_PROFILING_ENABLED, "true")
        monkeypatch.setenv(
            operation_profiling.ENV_PROFILING_RUN,
            json.dumps(
                {
                    "run_id": RUN_ID,
                    "operations": sorted(OPERATIONS),
                    "expires_at": RUN_EXPIRES_AT.isoformat(),
                    "max_operations": 64,
                    # Scoped to the retained sandbox this test really created,
                    # so every selection below is proved against a fact the
                    # route read out of Postgres rather than a wildcard.
                    "target": {"sandbox_id": SANDBOX_ID},
                }
            ),
        )
    else:
        monkeypatch.delenv(operation_profiling.ENV_PROFILING_ENABLED, raising=False)
        monkeypatch.delenv(operation_profiling.ENV_PROFILING_RUN, raising=False)
    operation_profiling.reset_cache()
    operation_measurement.reset()
    # This process remembers which measurements it delivered, and which
    # interactions it minted; a test must not inherit the previous one's.
    runner_mod.reset_delivery_state()

    broker = _Broker()
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)
    app = create_app()
    # Only the two external collaborators are replaced. Storage is deliberately
    # not overridden: every route below reads and writes the real repository
    # over the application's own pool.
    app.dependency_overrides[get_gateway_client] = _Gateway
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    server, task, base_url = await _serve(app)
    store = Storage(pool)
    job, claim, sandbox = await _arrange_chain(store)
    chain = _Chain(
        pool=pool,
        store=store,
        base_url=base_url,
        broker=broker,
        job=job,
        claim=claim,
        sandbox=sandbox,
        sink=sink,
    )
    try:
        yield chain
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        measurement_writer.reset_writer(None)
        await _reset(pool)
        await pool.close()
        operation_profiling.reset_cache()
        operation_measurement.reset()
        runner_mod.reset_delivery_state()


@pytest_asyncio.fixture
async def chain(monkeypatch, tmp_path) -> AsyncIterator[_Chain]:
    """The whole chain with profiling on, as a deployment runs it."""
    async with _running_chain(monkeypatch, tmp_path, profiling=True) as live:
        yield live


@pytest_asyncio.fixture
async def chain_off(monkeypatch, tmp_path) -> AsyncIterator[_Chain]:
    """The same chain with the switch off, which must change nothing visible."""
    async with _running_chain(monkeypatch, tmp_path, profiling=False) as live:
        yield live


def _selected_startup(chain: _Chain) -> Measurement:
    """Root one startup the way the runner does, bound to this real attempt."""
    measurement = operation_profiling.start_operation(
        operation=operation_profiling.STARTUP_OPERATION,
        host=HOST,
        worker_id="acceptance-runner",
    )
    assert measurement is not None, "the experiment must select the startup"
    assert operation_profiling.bind(
        measurement,
        job_id=chain.job_id,
        attempt_id=chain.attempt_id,
        thread_id=chain.thread_id,
        sandbox_id=SANDBOX_ID,
        host=HOST,
        runtime_id=RUNTIME,
        runtime_version=chain.claim.get("runtime_version"),
        execution_profile=str(chain.job["execution_profile"]),
    )
    return measurement


def _frozen_startup(
    chain: _Chain, *, outcome: str = "dispatched", failure_stage: str | None = None
) -> Measurement:
    """A startup frozen at its own boundary, as a completed startup is."""
    measurement = _selected_startup(chain)
    operation_profiling.mark(measurement, "prompt_dispatch")
    operation_profiling.finish(measurement, outcome=outcome, failure_stage=failure_stage)
    return measurement


async def _row(pool: Any, measurement_id: str) -> Any:
    """One stored measurement row, or ``None``."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM agent_operation_measurements WHERE measurement_id = $1",
            measurement_id,
        )


async def _await_row(pool: Any, measurement_id: str, *, timeout_s: float = 10.0) -> Any:
    """Wait, briefly, for the writer's queue to land one row in Postgres."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        row = await _row(pool, measurement_id)
        if row is not None:
            return row
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"measurement {measurement_id} was never stored")
        await asyncio.sleep(0.02)


async def _await_writer(
    writer: measurement_writer.MeasurementWriter,
    ready: Callable[[measurement_writer.MeasurementWriter], bool],
    *,
    timeout_s: float = 5.0,
) -> None:
    """Wait for the writer to reach the state the next assertion is about."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not ready(writer):
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(
                "the measurement writer did not reach "
                f"pending={writer.pending} in_flight={writer.in_flight} in time"
            )
        await asyncio.sleep(0.01)


async def _measurement_count(pool: Any) -> int:
    """How many measurements are stored, whatever they are."""
    async with pool.acquire() as conn:
        return int(await conn.fetchval("SELECT COUNT(*) FROM agent_operation_measurements"))


async def _measurement_rows(pool: Any) -> list[Any]:
    """Every stored measurement row, which is what the assertions join over."""
    async with pool.acquire() as conn:
        return list(await conn.fetch("SELECT * FROM agent_operation_measurements"))


async def _await_stored(
    pool: Any, query: str, *args: Any, expected: int, timeout_s: float = 10.0
) -> None:
    """Wait, briefly, for the writer's queue to land ``expected`` matching rows."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        async with pool.acquire() as conn:
            stored = int(await conn.fetchval(query, *args))
        if stored >= expected:
            return
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"{stored} rows matched; expected {expected}")
        await asyncio.sleep(0.02)


class _HangingSupervisor:
    """A model that has not finished: the turn is still in flight."""

    def __init__(self, stream: _HangingStream, started: threading.Event) -> None:
        self.stream = stream
        self.started = started

    def start_shared_turn(self, *_args: Any, **_kwargs: Any) -> Any:
        """Hand back a stream that has not produced a frame yet."""
        self.started.set()
        return self.stream

    def interrupt_shared(self, *_args: Any, **_kwargs: Any) -> None:
        """Nothing to interrupt: this stand-in has no process."""
        return None


class _HangingStream:
    """A turn whose frames have been asked for and not yet produced."""

    def __init__(self) -> None:
        #: Set when the reader thread is inside ``frames()`` and blocked, which
        #: is the proof that the startup was frozen and delivered before the
        #: model produced anything.
        self.frames_entered = threading.Event()
        self.release = threading.Event()

    def frames(self) -> Any:
        """Block until the test is done with the assertion."""
        self.frames_entered.set()
        self.release.wait(timeout=30)
        return iter(({"kind": "result", "exit_code": 0},))

    def close(self) -> None:
        """Release nothing."""
        return None


class _IdleHeart:
    """A lease that is alive and never asks to cancel."""

    lease_lost = False
    cancel_requested = False


# ---------------------------------------------------------------------------
# 1. The startup record is stored while the model is still running
# ---------------------------------------------------------------------------


async def test_the_startup_record_is_stored_while_the_turn_is_still_running(chain: _Chain):
    """The handoff freezes and sends the sample; the turn may run for an hour.

    This is the property the whole switch exists for, and it can only be
    observed on a real chain: the record has to cross the real delivery
    runtime, the real HTTP ingest and the real repository while the model is
    demonstrably still working.
    """
    measurement = _selected_startup(chain)
    operation_profiling.mark(measurement, "prompt_dispatch")
    turn_started = threading.Event()
    stream = _HangingStream()
    supervisor = _HangingSupervisor(stream, turn_started)
    job = chain.claimed_job()

    def _run_turn() -> None:
        runner_mod._stream_shared_agent_turn(
            supervisor=supervisor,
            job=job,
            control=ControlPlane(chain.base_url, job.worker_token),
            heart=_IdleHeart(),
            lease_token="acceptance-lease",
            timeout_s=60.0,
            measurement=measurement,
            on_frozen=lambda frozen: runner_mod._enqueue_delivery(
                frozen,
                job=job,
                base_url=chain.base_url,
                drain_s=DELIVERY_HANDOFF_DRAIN_S,
            ),
        )

    # Nothing has been stored yet: the row asserted below exists because the
    # handoff delivered it, not because an earlier request left one behind.
    assert await _measurement_count(chain.pool) == 0
    turn = threading.Thread(target=_run_turn, daemon=True, name="acceptance-turn")
    turn.start()
    try:
        assert turn_started.wait(timeout=10), "the turn must have started"
        assert stream.frames_entered.wait(timeout=10), "the model must be mid-turn"
        row = await _await_row(chain.pool, measurement.measurement_id)

        # The ordering under test: the row is already there while the turn
        # thread is alive, the reader is blocked inside the model's frames, and
        # nothing has been released. No sleep decided this -- the frame request
        # is what the sample had to overtake.
        assert turn.is_alive(), "the turn was still running when its startup was stored"
        assert not stream.release.is_set()
        assert row["outcome"] == "dispatched"
        assert row["operation"] == "attempt.startup"
        assert row["owner_id"] == USER_ID
        assert row["job_id"] == chain.job_id
        assert row["thread_id"] == chain.thread_id
        assert row["sandbox_id"] == SANDBOX_ID
        assert row["host_id"] == HOST
        assert row["attempt_id"] == chain.attempt_id
        assert row["coverage_scope"] == "host_startup"
        assert row["producer"] == "runner"
        # The startup's own end boundary is the handoff, so the record names it
        # rather than ending wherever the model stopped.
        record = row["record"] if isinstance(row["record"], dict) else json.loads(row["record"])
        assert "prompt_dispatch" in [mark["name"] for mark in record["milestones"]]
    finally:
        stream.release.set()
        turn.join(timeout=5)


# ---------------------------------------------------------------------------
# 2. A failed startup is still delivered, and the exit wait is bounded
# ---------------------------------------------------------------------------


async def test_a_failed_startup_is_delivered_within_the_bounded_exit_wait(chain: _Chain):
    """A startup that never dispatched is a sample, and waiting for it is bounded.

    The record is frozen as failed and handed to the delivery runtime with the
    same call and the same bound ``run_turn``'s exit sweep uses. The
    destination is deliberately stalled -- the measurements table is locked --
    so the drain bound is observable: the enqueue returns at the bound with the
    send still in flight, and the record still lands once the destination can
    answer.
    """
    measurement = _frozen_startup(chain, outcome="failed", failure_stage="sandbox_start")

    lock = await chain.pool.acquire()
    transaction = lock.transaction()
    await transaction.start()
    await lock.execute("LOCK TABLE agent_operation_measurements IN ACCESS EXCLUSIVE MODE")
    try:
        started = asyncio.get_running_loop().time()
        await asyncio.to_thread(
            runner_mod._enqueue_delivery,
            measurement,
            job=chain.claimed_job(),
            base_url=chain.base_url,
            drain_s=DELIVERY_DRAIN_S,
        )
        waited = asyncio.get_running_loop().time() - started

        # The bounded wait is the configured drain, not the destination's
        # answer: it returned while the send was still blocked, and the counter
        # says the send outlived it.
        assert waited >= DELIVERY_DRAIN_S * 0.8, "the drain must actually have been waited"
        assert waited <= DELIVERY_DRAIN_S + 1.0, "the exit wait outlived its bound"
        assert runner_mod._DELIVERY_DRAIN_TIMEOUTS == 1
    finally:
        await transaction.rollback()
        await chain.pool.release(lock)

    row = await _await_row(chain.pool, measurement.measurement_id)
    assert row["outcome"] == "failed"
    assert row["operation"] == "attempt.startup"
    assert row["attempt_id"] == chain.attempt_id
    # The stage the startup failed at travels inside the frozen record; the
    # table's summary columns carry the outcome, not the boundary.
    record = row["record"] if isinstance(row["record"], dict) else json.loads(row["record"])
    assert record["failure_stage"] == "sandbox_start"


# ---------------------------------------------------------------------------
# 3. A terminal attach is stored while the SSE stream stays open
# ---------------------------------------------------------------------------


async def test_a_terminal_attach_is_stored_while_the_stream_is_still_open(chain: _Chain):
    """Persisting the attach before the terminal's bytes is what keeps it short.

    An SSE body may stay open for an hour. The measurement is the attach
    handshake, not the stream's lifetime, so the row has to exist while the
    response body is still unread -- which is asserted on the live response
    object, not after closing it.
    """
    async with httpx.AsyncClient(base_url=chain.base_url, cookies=chain.session_cookie()) as client:
        listing = await client.get(f"/v1/agent/jobs/{chain.job_id}/terminals")
        assert listing.status_code == 200
        interaction_id = listing.json()["profiling"]["interaction_id"]
        created = await client.post(
            f"/v1/agent/jobs/{chain.job_id}/terminals",
            json={"rows": 24, "cols": 80},
            headers={INTERACTION_HEADER: interaction_id},
        )
        assert created.status_code == 200

        async with client.stream(
            "GET",
            f"/v1/agent/jobs/{chain.job_id}/terminals/{TERMINAL_ID}/stream",
            headers={INTERACTION_HEADER: interaction_id},
        ) as response:
            assert response.status_code == 200
            assert response.headers["x-agent-profiling-interaction-id"] == interaction_id
            measurement_id = response.headers["x-agent-profiling-measurement-id"]
            # The body is the terminal's own bytes and has not been read: the
            # route answered in headers precisely so this can be asserted while
            # the stream lives.
            assert response.is_stream_consumed is False
            assert response.is_closed is False

            row = await _await_row(chain.pool, measurement_id)

            assert response.is_stream_consumed is False, (
                "the attach must be stored before the SSE body is consumed"
            )

    assert row["request_kind"] == "terminal.stream_attach"
    assert row["operation"] == "terminal.open"
    assert row["interaction_id"] == interaction_id
    assert row["job_id"] == chain.job_id
    assert row["owner_id"] == USER_ID
    assert row["sandbox_id"] == SANDBOX_ID
    assert row["outcome"] == "completed"
    assert row["coverage_scope"] == "server_request"
    # A terminal request is not an attempt's startup, and the row says so.
    assert row["attempt_id"] is None


# ---------------------------------------------------------------------------
# 4. Failures are filed by the classification rules
# ---------------------------------------------------------------------------


async def test_failed_file_and_terminal_requests_follow_the_classification_rules(chain: _Chain):
    """Two failures, two rules, and neither is a made-up operation.

    A non-root read that fails before the broker reported a ``kind`` could be a
    listing or an open, so filing it under either would corrupt that
    operation's failure rate. It is refused and counted instead. A terminal
    request that fails *after* its operation was selected is a real failed
    sample and is stored as one.
    """
    writer = measurement_writer.writer()
    async with httpx.AsyncClient(base_url=chain.base_url, cookies=chain.session_cookie()) as client:
        chain.broker.fail = WorkspaceBrokerError(503, "workspace unavailable")

        ambiguous = await client.get(
            f"/v1/agent/jobs/{chain.job_id}/files", params={"path": "README.md"}
        )

        assert ambiguous.status_code == 503
        # The refused request is not reported to the client either: a response
        # block would claim a sample under a name the run never asked for.
        assert "profiling" not in ambiguous.json()
        # Nothing was stored for the ambiguous read: no files.open row, and in
        # fact no row at all, because that was the only request so far.
        assert await _measurement_count(chain.pool) == 0
        assert writer.counts.rejected == 1
        assert writer.counts.reasons.get("ambiguous_file_kind") == 1
        assert writer.counts.stored == 0
        assert writer.pending == 0

        failed_terminal = await client.get(f"/v1/agent/jobs/{chain.job_id}/terminals")

        assert failed_terminal.status_code == 503

    # A terminal failure happens after the operation is known, so it is a
    # sample: stored, named, and reported as failed rather than dropped.
    await _await_stored(chain.pool, "SELECT COUNT(*) FROM agent_operation_measurements", expected=1)
    rows = await _measurement_rows(chain.pool)
    assert len(rows) == 1
    assert rows[0]["operation"] == "terminal.open"
    assert rows[0]["request_kind"] == "terminal.list"
    assert rows[0]["outcome"] == "failed"
    assert rows[0]["collection_complete"] is False
    assert writer.counts.stored == 1


# ---------------------------------------------------------------------------
# 5. Congestion costs samples, never the response
# ---------------------------------------------------------------------------


async def test_congestion_keeps_answering_and_the_queue_counts_are_correct(chain: _Chain):
    """A locked table must not become a locked control plane.

    The measurements table is held in ``ACCESS EXCLUSIVE`` mode while real
    requests are served. Each response still arrives promptly; the bounded
    writer reports exactly one in-flight write, a full queue and the drops that
    follow from it; and once the lock is released the accepted records land.
    """
    # Two records may wait while one is being written: the bound is on
    # ``pending + in_flight``, which is what makes the third and later offers
    # drops rather than an unbounded queue.
    max_count = 3
    writer = measurement_writer.MeasurementWriter(
        max_count=max_count, max_concurrent=1, timeout_s=15.0
    )
    measurement_writer.reset_writer(writer)
    lock = await chain.pool.acquire()
    transaction = lock.transaction()
    await transaction.start()
    await lock.execute("LOCK TABLE agent_operation_measurements IN ACCESS EXCLUSIVE MODE")
    try:
        async with httpx.AsyncClient(
            base_url=chain.base_url, cookies=chain.session_cookie()
        ) as client:
            slowest = 0.0
            for index in range(5):
                started = asyncio.get_running_loop().time()
                response = await client.get(f"/v1/agent/jobs/{chain.job_id}/files")
                slowest = max(slowest, asyncio.get_running_loop().time() - started)
                assert response.status_code == 200
                assert response.json()["kind"] == "directory"
                if index == 0:
                    # The first record has to be off the queue and blocked in
                    # the database before the rest are offered, or which of
                    # them is dropped would be a race rather than a bound.
                    await _await_writer(writer, lambda item: item.in_flight == 1)

            assert slowest < 1.0, "a locked diagnostics table slowed the business response"

            async with httpx.AsyncClient(base_url=chain.base_url, headers=ADMIN) as admin_client:
                status = await admin_client.get("/admin/agent/measurement-writer")

            assert status.status_code == 200
            body = status.json()
            assert body["scope"] == "this_control_plane_process"
            assert body["running"] is True
            assert body["pending"] == 2
            assert body["in_flight"] == 1
            assert body["counts"]["stored"] == 0
            assert body["counts"]["dropped_full"] == 2
            assert body["counts"]["dropped_timeout"] == 0
            assert body["counts"]["failed"] == 0
            assert body["counts"]["reasons"].get("queue_count") == 2
            # The bounds the deployment configured are the bounds in force.
            assert body["pending"] <= max_count
            assert body["in_flight"] <= 1
    finally:
        await transaction.rollback()
        await chain.pool.release(lock)

    await _await_writer(writer, lambda item: item.counts.stored == 3, timeout_s=15)
    assert writer.pending == 0
    assert writer.in_flight == 0
    assert writer.counts.dropped_full == 2
    assert await _measurement_count(chain.pool) == 3
    # This test installed its own writer, so it also stops it: a worker left
    # waiting on a queue nobody owns would outlive the loop that made it.
    await writer.shutdown(timeout_s=2)


# ---------------------------------------------------------------------------
# 6. Off means off, down to the counters and the table
# ---------------------------------------------------------------------------


async def test_profiling_off_collects_nothing_and_writes_nothing(chain_off: _Chain):
    """The switch is the promise: no selection, no task, no delivery, no row.

    The same requests the other tests drive are served here, byte-for-byte the
    shape a deployment without the switch serves, and every observable piece of
    the pipeline stays empty -- including the process singleton's counters and
    the delivery runtime's state.
    """
    chain = chain_off
    assert (
        operation_profiling.start_operation(
            operation=operation_profiling.STARTUP_OPERATION, host=HOST
        )
        is None
    )
    writer = measurement_writer.writer()
    assert writer.running is False, "off must not leave a writer pool running"

    async with httpx.AsyncClient(base_url=chain.base_url, cookies=chain.session_cookie()) as client:
        listing = await client.get(f"/v1/agent/jobs/{chain.job_id}/files")
        opened = await client.get(
            f"/v1/agent/jobs/{chain.job_id}/files", params={"path": "README.md"}
        )
        terminals = await client.get(f"/v1/agent/jobs/{chain.job_id}/terminals")
        created = await client.post(
            f"/v1/agent/jobs/{chain.job_id}/terminals", json={"rows": 24, "cols": 80}
        )
        async with client.stream(
            "GET",
            f"/v1/agent/jobs/{chain.job_id}/terminals/{TERMINAL_ID}/stream",
        ) as streamed:
            assert streamed.status_code == 200
            assert [chunk async for chunk in streamed.aiter_bytes()], (
                "the terminal bytes must still be relayed with profiling off"
            )

    for response in (listing, opened, terminals, created):
        assert response.status_code == 200
        assert "profiling" not in response.json()
    # The streaming route is the one that answers in headers, so it is the one
    # where an accidental profiling header would show up with the switch off.
    assert "x-agent-profiling-measurement-id" not in streamed.headers

    assert writer.counts.as_dict() == {
        "stored": 0,
        "duplicate": 0,
        "rejected": 0,
        "conflict": 0,
        "dropped_full": 0,
        "dropped_timeout": 0,
        "dropped_shutdown": 0,
        "failed": 0,
        "reasons": {},
    }
    assert not runner_mod._DELIVERED_MEASUREMENT_IDS
    assert not runner_mod._IN_FLIGHT_DELIVERIES
    assert not runner_mod._STARTING_DELIVERIES
    assert not runner_mod._DELIVERY_SIZES
    assert runner_mod._DELIVERY_PENDING_BYTES == 0
    assert runner_mod._DELIVERY_DROPPED_COUNT == 0
    assert runner_mod._DELIVERY_DRAIN_TIMEOUTS == 0
    assert await _measurement_count(chain.pool) == 0
    assert not chain.sink.exists(), "off must not open a sink"


# ---------------------------------------------------------------------------
# The Admin read-back of what the chain stored
# ---------------------------------------------------------------------------


async def test_the_admin_reads_back_the_startup_and_the_whole_interaction(chain: _Chain):
    """The two precise selectors answer with their own rows, and not each other's.

    ``attempt_id`` names one execution's startup; ``interaction_id`` names
    every request of one terminal open. Reading them back through the real
    Admin routes is what proves the chain stored rows a reader can actually
    join, rather than rows that merely exist.
    """
    measurement = _frozen_startup(chain)
    await asyncio.to_thread(
        runner_mod._enqueue_delivery,
        measurement,
        job=chain.claimed_job(),
        base_url=chain.base_url,
        drain_s=DELIVERY_HANDOFF_DRAIN_S,
    )
    startup_row = await _await_row(chain.pool, measurement.measurement_id)
    assert startup_row["outcome"] == "dispatched"

    async with httpx.AsyncClient(base_url=chain.base_url, cookies=chain.session_cookie()) as client:
        listing = await client.get(f"/v1/agent/jobs/{chain.job_id}/terminals")
        interaction_id = listing.json()["profiling"]["interaction_id"]
        created = await client.post(
            f"/v1/agent/jobs/{chain.job_id}/terminals",
            json={"rows": 24, "cols": 80},
            headers={INTERACTION_HEADER: interaction_id},
        )
        streamed = await client.get(
            f"/v1/agent/jobs/{chain.job_id}/terminals/{TERMINAL_ID}/stream",
            headers={INTERACTION_HEADER: interaction_id},
        )
        assert created.status_code == 200 and streamed.status_code == 200
        await streamed.aread()

    # The writes happen off the request path, so the read-back waits for the
    # three rows this interaction produced rather than assuming they are there
    # the moment the last response returned.
    await _await_stored(
        chain.pool,
        "SELECT COUNT(*) FROM agent_operation_measurements WHERE interaction_id = $1",
        interaction_id,
        expected=3,
    )

    async with httpx.AsyncClient(base_url=chain.base_url, headers=ADMIN) as admin:
        by_attempt = await admin.get(
            "/admin/agent/measurements",
            params={"run_id": RUN_ID, "attempt_id": chain.attempt_id},
        )
        by_interaction = await admin.get(
            "/admin/agent/measurements",
            params={"run_id": RUN_ID, "interaction_id": interaction_id},
        )

    assert by_attempt.status_code == 200
    attempt_page = by_attempt.json()
    assert [item["measurement_id"] for item in attempt_page["measurements"]] == [
        measurement.measurement_id
    ]
    (startup,) = attempt_page["measurements"]
    assert startup["operation"] == "attempt.startup"
    assert startup["request_kind"] == "attempt.startup"
    assert startup["attempt_id"] == chain.attempt_id
    assert startup["lease_generation"] == chain.lease_generation
    assert startup["coverage_scope"] == "host_startup"
    assert startup["outcome"] == "dispatched"

    assert by_interaction.status_code == 200
    interaction_page = by_interaction.json()
    kinds = sorted(item["request_kind"] for item in interaction_page["measurements"])
    assert kinds == ["terminal.create", "terminal.list", "terminal.stream_attach"]
    assert {item["interaction_id"] for item in interaction_page["measurements"]} == {interaction_id}
    assert {item["operation"] for item in interaction_page["measurements"]} == {"terminal.open"}
    # A terminal open has no attempt, and the attempt selector did not answer
    # with the terminal requests either: the two keys are not spellings of one.
    assert all(item["attempt_id"] is None for item in interaction_page["measurements"])
    assert measurement.measurement_id not in {
        item["measurement_id"] for item in interaction_page["measurements"]
    }
