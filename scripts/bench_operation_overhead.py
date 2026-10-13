#!/usr/bin/env python3
"""Measure what the opt-in operation profiler costs a real business request.

``scripts/bench_startup_profiling_overhead.py`` measures the collector against a
synthetic attempt sequence. This script measures the other half of the claim in
``docs/agents/specs/2026-09-16-operation-measurement-storage-admin-integration-design.zh-CN.md``
§9: that switching the profiler *on*, and persisting what it produces, does not
show up in the latency of the requests users wait on.

What is measured
----------------

Two families, both on the real production path:

Server-request operations (the default; ``--skip-server-ops`` turns them off)
    The real control-plane HTTP routes, served by ``cloud_agent.app.create_app()``
    over a real socket, against a real PostgreSQL database, with an in-process
    sandbox manager stand-in in place of the host network:

    * ``files.list`` (root), ``files.list`` (subdirectory) and ``files.open``
      through ``GET /v1/agent/jobs/{job_id}/files``,
    * ``terminal.list``, ``terminal.create`` and ``terminal.stream_attach``
      through the three ``/terminals`` routes.

    Every cohort sends the same six requests; only the profiler's configuration
    and the destination differ.

``--startup`` (needs Docker)
    The four declared startup paths -- ``new_group``, ``resume_stopped``,
    ``resume_running``, ``reuse_live`` -- produced by a real retained container
    and a real :class:`SharedChatSessionSupervisor`, classified by the real
    ``cloud_agent_host.runner._classify_startup_path``.

Cohorts, and what each one really is
------------------------------------

``bare``
    The same operation with no instrumentation call on the request path. The
    control-plane routes always call the collector seam, so there is no such
    configuration to deploy; for a server-request operation this cohort is
    measured on an *unmeasured sibling route* that walks the same authentication, ownership,
    entitlement and sandbox manager chain (``PUT .../files`` for the file family,
    ``POST .../terminals/{id}/resize`` for the terminal family). It is a floor
    reference for the request path, not the measured operation, and it is never
    used as the baseline for a budget verdict. For ``--startup`` it is the same
    supervisor activation with no collector calls at all, which *is* a real
    configuration.

``off``
    The real route (or the real activation) with the profiler's switch unset.
    ``start_operation`` / ``observe`` return ``None`` and every other collector
    call is a no-op. This is the baseline every delta and the budget use.

``on-local``
    Profiling enabled with ``AGENT_OPERATION_PROFILING_SINK`` set, so the record
    is serialized and exported by the collector's own thread. For ``--startup``
    that is the runner's local mode. For a server-request operation the control
    plane's persistence destination is replaced by a bench-local JSONL file, because no
    deployment switch removes the Postgres repository from this process; the
    collector, the bounded writer queue and the route are the real ones.

``on-persist``
    ``on-local`` plus the real destination: for a server-request operation the
    real ``Storage.operation_measurements`` repository, writing the real
    ``agent_operation_measurements`` table; for ``--startup`` the runner's own
    hand-off delivery, started at the freeze point, posting the frozen record to
    the real ingest route
    (``POST /v1/agent/worker/jobs/{job_id}/measurements``) and into the same
    table.

For ``--startup`` the timed window is the whole measured path, not only the
container work: the measurement is opened, the run's facts are bound, both
supervisor calls run *with the profiling context the runner passes them*, their
answers are recorded as fragments, the record is frozen, and the hand-off
delivery is started -- all between two reads of this process's clock. A window
that began after the first mark and ended before the freeze would have measured
the container and exempted the instrumentation, which is the comparison the
cohorts exist to make. What a path needs as preparation (creating or stopping
the container that defines it, warming a resident for ``reuse_live``) stays
outside the clock, because it is not a cost a startup pays.

Two synthetic arms run on top of ``on-persist`` / ``on-local``:

* ``--slow-db-hold-s`` holds ``LOCK TABLE agent_operation_measurements IN ACCESS
  EXCLUSIVE MODE`` on a second connection while requests are measured, which is
  the "deliberately slow database" scenario in its harshest honest form. The
  slowness is in the database, injected by this script; the business requests,
  the routes and the writer queue are untouched.
* ``--slow-sink-ms`` delays the bench-local destination of the ``on-local``
  cohort by that much per record, standing in for a slow control-plane-side
  destination. It is synthetic and labelled as such.

What this is not
----------------

* Not a micro-benchmark: it does not measure the collector's own call cost
  (``bench_startup_profiling_overhead.py`` does that) and none of its numbers
  substitute for these.
* Not an end-to-end number. A control-plane record's own schema says
  ``end_to_end_complete: false``: the measured span starts after authentication
  and ends when the response object is built, and the browser, the network, the
  host agent and the sandbox are outside it.
* Not a production latency measurement. Client and server share one process and
  one event loop over loopback, so there is no network round trip and no
  multi-replica control plane in these numbers.
* Not a real agent CLI. ``--startup`` stops at the supervisor's activation: the
  resident transport is a stand-in, so no CLI process is spawned and the CLI's
  own initialization is absent from the resident paths. What the container
  really did (``create`` / ``start`` / ``reuse``) is real Docker.

Cohorts run in rounds with the order rotated, so no cohort always pays for a
cold process, and the report prints the per-round p50 next to a measured *noise
floor*: the ``off`` cohort's even rounds against its odd rounds, where no
instrumentation difference exists. A delta smaller than that floor is not a
result, and the report does not pretend otherwise.

Percentiles are nearest-rank over the sorted samples,
``index = min(n-1, max(0, round(f*(n-1))))``. The Admin page computes
``percentile_cont`` in SQL, which interpolates between samples, so a number from
this script will not reproduce the page numerically; the difference is the
percentile definition, not the data.

Examples::

    python scripts/bench_operation_overhead.py
    python scripts/bench_operation_overhead.py --iterations 400 --json /tmp/overhead.json
    python scripts/bench_operation_overhead.py --startup --json /tmp/overhead.json \\
        --markdown /tmp/overhead.md
    python scripts/bench_operation_overhead.py --database ca_overhead_measure_test_db \\
        --no-slow-arms --skip-server-ops --startup
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import platform
import re
import resource
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cloud_agent import measurement_writer, sandbox_manager_routing
from cloud_agent.operation_measurement import reset as reset_selections
from cloud_agent_host import operation_call
from cloud_agent_shared import operation_profiling
from cloud_agent_shared.sandbox_manager_compat import SANDBOX_MANAGER_PRODUCER

if TYPE_CHECKING:
    from collections.abc import Callable

    import asyncpg
    import httpx
    from fastapi import FastAPI

#: The database this script may touch. It has to carry the marker every
#: database-backed test uses, and the check below refuses anything that does
#: not: a benchmark that silently ran against a deployment's database would
#: measure the wrong thing and then truncate it.
DEFAULT_DATABASE = "ca_overhead_measure_test_db"
ALLOWED_DATABASE_PATTERN = "_test_"
DATABASE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")

#: Roles this process plays in the seeded fixture.
BENCH_USER_ID = "user-overhead-bench"
BENCH_REPO = "bench/overhead-repo"
BENCH_HOST = "bench-overhead-runner"
BENCH_HOST_CREDENTIAL = "bench-overhead-credential-hash"
BENCH_SANDBOX_ID = "asbx_overhead_bench"
BENCH_CONTAINER_NAME = "bench-overhead-container"
BENCH_IMAGE_DIGEST = "sha256:overhead-bench-image"
BENCH_SANDBOX_MANAGER_URL = "http://bench-overhead-sandbox-manager:8092"
BENCH_RUNTIME_ID = "claude-code"
BENCH_RUNTIME_VERSION = "2.1.220"
BENCH_ADMIN_TOKEN = "bench-overhead-admin-token"
BENCH_SESSION_SECRET = "bench-overhead-session-secret-32-bytes-minimum"
BENCH_CONTROL_TOKEN_SECRET = "bench-overhead-control-token-secret"
BENCH_SANDBOX_MANAGER_TOKEN = "bench-overhead-sandbox-manager-token"
BENCH_GATEWAY_ISSUER = "https://bench-gateway.example"

#: The terminal id the sandbox manager stand-in answers with. The route validates ids
#: against ``^term_[A-Za-z0-9_-]{1,80}$``.
BENCH_TERMINAL_ID = "term_bench_overhead"

#: Cohort names, in the order the report prints and the script runs them.
COHORTS: tuple[str, ...] = ("bare", "off", "on-local", "on-persist")

#: The measured server-request operations, in report order.
SERVER_OPERATIONS: tuple[tuple[str, str], ...] = (
    ("files.list.root", "GET /files?path= (root listing)"),
    ("files.list.subdir", "GET /files?path=src (subdirectory listing)"),
    ("files.open", "GET /files?path=small.txt (file read)"),
    ("terminal.list", "GET /terminals"),
    ("terminal.create", "POST /terminals"),
    ("terminal.stream_attach", "GET /terminals/{id}/stream (attach handshake)"),
)

#: Which family each operation belongs to. The bare analogue is measured once
#: per family, on a sibling route the profiler does not touch.
OPERATION_FAMILIES: dict[str, str] = {
    "files.list.root": "files",
    "files.list.subdir": "files",
    "files.open": "files",
    "terminal.list": "terminals",
    "terminal.create": "terminals",
    "terminal.stream_attach": "terminals",
}

#: The two unmeasured sibling routes the bare analogue uses, with the sandbox manager
#: call each one makes. Both walk the same authentication, ownership,
#: entitlement and sandbox-manager-resolution chain as the measured routes.
BARE_ANALOGUES: dict[str, str] = {
    "files": "PUT /files (write route -> sandbox manager.write_file)",
    "terminals": "POST /terminals/{id}/resize (-> sandbox manager.resize_terminal)",
}

#: Server-operation run set: the three root operations the control plane selects.
SERVER_RUN_OPERATIONS: tuple[str, ...] = ("files.list", "files.open", "terminal.open")

#: The runner's root operation, and the paths the classifier can name.
STARTUP_OPERATION = "attempt.startup"
#: The residency fence one group's live process is launched under. Constant on
#: purpose: the supervisor reuses a live transport only when this fence is
#: unchanged, so a per-activation value would turn every HOT hit into a restart.
BENCH_RESIDENCY_GENERATION = 1
STARTUP_PATHS: tuple[str, ...] = ("new_group", "resume_stopped", "resume_running", "reuse_live")

#: The agreed budget for a new p95 introduced by the measurement machinery.
BUDGET_FLOOR_MS = 10.0
BUDGET_FRACTION = 0.01

#: Slack over warmup + iterations in the run descriptor's operation budget, so
#: a cohort can never quietly run out mid-run and degrade into the ``off``
#: path. ``_verify_cohort_selected`` fails loudly if it ever does.
RUN_BUDGET_SLACK = 1000
RUN_LIFETIME = timedelta(minutes=30)

#: Docker plumbing for ``--startup``: the same shape the file-open benchmark
#: uses. The manager builds a real group bridge behind real host rules, so it
#: needs a relay network with two listening relays, a pool of its own, and an
#: image carrying ``nft`` for the host-rule helper (the guest image doubles as
#: it). The resolver is never queried; override it where 1.1.1.1 is wrong.
BENCH_IMAGE = "cae-bench-guest:bookworm"
BENCH_NAMESPACE = "cae-overhead-bench"
BENCH_RELAY_NET = "cae-overhead-relays"
BENCH_POOL = "10.251.0.0/16"
BENCH_GATEWAY = "cae-overhead-gateway"
BENCH_GATEWAY_PORT = 8080
BENCH_MCP = "cae-overhead-mcp"
BENCH_MCP_PORT = 8000
BENCH_STARTUP_THREAD = "athr_overhead_bench"


# --------------------------------------------------------------------------
# samples
# --------------------------------------------------------------------------


class Sample:
    """One cohort's samples for one operation, in nanoseconds."""

    def __init__(self, values_ns: list[int]) -> None:
        self.values_ns = sorted(values_ns)

    def percentile_ms(self, fraction: float) -> float:
        """Return the nearest-rank percentile, in milliseconds.

        Nearest-rank, not an interpolating definition: the index is
        ``min(n-1, max(0, round(f*(n-1))))`` over the sorted samples, which is
        the convention the other benchmarks in this directory use.
        """
        if not self.values_ns:
            raise ValueError("no samples")
        index = min(len(self.values_ns) - 1, max(0, round(fraction * (len(self.values_ns) - 1))))
        return self.values_ns[index] / 1e6

    def mean_ms(self) -> float:
        """Return the arithmetic mean, in milliseconds."""
        return statistics.fmean(self.values_ns) / 1e6

    def as_dict(self) -> dict[str, Any]:
        """Return this cohort's summary row."""
        return {
            "n": len(self.values_ns),
            "p50_ms": round(self.percentile_ms(0.50), 3),
            "p95_ms": round(self.percentile_ms(0.95), 3),
            "mean_ms": round(self.mean_ms(), 3),
            "min_ms": round(self.values_ns[0] / 1e6, 3),
            "max_ms": round(self.values_ns[-1] / 1e6, 3),
        }


def _delta(sample: Sample, base: Sample) -> dict[str, float]:
    """Return one cohort's cost over the baseline, in milliseconds and percent."""
    base_p50 = base.percentile_ms(0.50)
    base_p95 = base.percentile_ms(0.95)
    base_mean = base.mean_ms()
    return {
        "p50_ms": round(sample.percentile_ms(0.50) - base_p50, 3),
        "p95_ms": round(sample.percentile_ms(0.95) - base_p95, 3),
        "mean_ms": round(sample.mean_ms() - base_mean, 3),
        "p50_pct": round(100.0 * (sample.percentile_ms(0.50) - base_p50) / base_p50, 2)
        if base_p50
        else 0.0,
        "p95_pct": round(100.0 * (sample.percentile_ms(0.95) - base_p95) / base_p95, 2)
        if base_p95
        else 0.0,
        "mean_pct": round(100.0 * (sample.mean_ms() - base_mean) / base_mean, 2)
        if base_mean
        else 0.0,
    }


def _budget_verdict(off: Sample, measured: Sample) -> dict[str, Any]:
    """Evaluate the agreed new-p95 budget for one instrumented cohort."""
    budget_ms = max(BUDGET_FLOOR_MS, off.percentile_ms(0.95) * BUDGET_FRACTION)
    delta_ms = measured.percentile_ms(0.95) - off.percentile_ms(0.95)
    return {
        "rule": f"max({BUDGET_FLOOR_MS:.0f} ms, off_p95 x {BUDGET_FRACTION:.0%})",
        "budget_ms": round(budget_ms, 3),
        "delta_p95_ms": round(delta_ms, 3),
        "verdict": "pass" if delta_ms <= budget_ms else "fail",
    }


# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------


def _db_config(args: argparse.Namespace) -> dict[str, Any]:
    """Return the connection settings, refusing a database that is not a test one."""
    name = str(args.database)
    if ALLOWED_DATABASE_PATTERN not in name or not DATABASE_NAME_RE.match(name):
        raise SystemExit(
            f"SAFETY: refusing database {name!r}; it must match {DATABASE_NAME_RE.pattern} "
            f"and contain {ALLOWED_DATABASE_PATTERN!r}"
        )
    return {
        "host": args.db_host,
        "port": args.db_port,
        "database": name,
        "user": args.db_user,
        "password": args.db_password,
    }


async def _ensure_database(config: dict[str, Any]) -> bool:
    """Create the test database when it does not exist yet.

    Returns:
        Whether this call created it.
    """
    import asyncpg

    admin = {**config, "database": "postgres"}
    connection = await asyncpg.connect(**admin)
    try:
        exists = await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", config["database"]
        )
        if exists:
            return False
        await connection.execute(f'CREATE DATABASE "{config["database"]}"')
        return True
    finally:
        await connection.close()


def _apply_schema(config: dict[str, Any]) -> None:
    """Apply the migrations the way a deploy does, so the tables are real."""
    from tests.support.migrations import apply_migrations

    apply_migrations(config)


async def _truncate(pool: asyncpg.Pool) -> None:
    """Empty every ``agent_*`` table and re-seed the policy singleton."""
    from tests.support.db import reset_agent_tables

    async with pool.acquire() as connection:
        await reset_agent_tables(connection)


def _dsn(config: dict[str, Any]) -> str:
    """Return the ``AGENT_DATABASE_URL`` form of the same settings."""
    return (
        f"postgresql://{config['user']}:{config['password']}"
        f"@{config['host']}:{config['port']}/{config['database']}"
    )


async def _postgres_measurement_footprint(pool: asyncpg.Pool, run_id: str) -> dict[str, Any]:
    """Return what one cohort's records cost the table, at the payload level."""
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT COUNT(*) AS rows,
                   COALESCE(SUM(octet_length(record::text)), 0) AS record_bytes,
                   COALESCE(SUM(pg_column_size(record)), 0) AS jsonb_bytes
            FROM agent_operation_measurements
            WHERE profiling_run_id = $1
            """,
            run_id,
        )
        table_bytes = await connection.fetchval(
            "SELECT pg_total_relation_size('agent_operation_measurements')"
        )
        startup_paths = await connection.fetch(
            """
            SELECT startup_path, COUNT(*) AS count
            FROM agent_operation_measurements
            WHERE profiling_run_id = $1 AND startup_path IS NOT NULL
            GROUP BY startup_path ORDER BY startup_path
            """,
            run_id,
        )
    return {
        "rows": int(row["rows"]),
        "record_bytes": int(row["record_bytes"]),
        "jsonb_bytes": int(row["jsonb_bytes"]),
        "table_total_bytes": int(table_bytes),
        "startup_paths": {str(item["startup_path"]): int(item["count"]) for item in startup_paths},
    }


# --------------------------------------------------------------------------
# the sandbox manager stand-in
# --------------------------------------------------------------------------


class BenchSandboxManager:
    """A sandbox manager that answers the measurement protocol exactly as the real one does.

    It reuses the sandbox manager's own :func:`operation_call.open_call`, so the fragment
    handshake under test is the production one and not a second implementation:
    the control plane offers a selection, this validates it against the same
    switch and run the route used, and the answer carries the call id it belongs
    to. Only the container work is stood in -- nothing here talks to a host.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._descriptor = {
            "id": BENCH_TERMINAL_ID,
            "shell": "/bin/bash",
            "state": "running",
            "cwd": "/workspace",
            "rows": 24,
            "cols": 80,
            "last_seq": 0,
        }

    async def register_shared_sandbox(self, _sandbox: dict[str, Any]) -> None:
        """Accept the manifest re-registration a shared workspace request makes."""
        return None

    def _open(self, context: Any, *operations: str) -> operation_call.SandboxManagerCall:
        """Validate the inherited selection the way the sandbox manager route does."""
        call = operation_call.open_call(context, operations=operations)
        with call.stage("admission"):
            pass
        return call

    async def files(
        self, workspace_id: str, path: str, *, profiling: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Answer a directory listing or a file read."""
        self.calls.append({"op": "files", "workspace_id": workspace_id, "path": path})
        call = self._open(profiling, *(["files.list", "files.open"] if path else ["files.list"]))
        with call.stage("exec"):
            pass
        directory = not path or path == "src"
        body = (
            {"path": path, "kind": "directory", "entries": [], "writable": True}
            if directory
            else {"path": path, "kind": "file", "content": "hello", "size": 5}
        )
        call.set_operation("files.list" if directory else "files.open")
        return call.answer(body)

    async def write_file(self, workspace_id: str, path: str, content: str) -> dict[str, Any]:
        """Answer the unmeasured sibling route's write."""
        self.calls.append({"op": "write_file", "workspace_id": workspace_id, "path": path})
        return {"path": path, "kind": "file", "content": content, "size": len(content)}

    async def list_terminals(
        self, workspace_id: str, *, profiling: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Answer one terminal list."""
        self.calls.append({"op": "list_terminals", "workspace_id": workspace_id})
        call = self._open(profiling, "terminal.open")
        call.set_operation("terminal.open")
        with call.stage("list"):
            pass
        return call.answer({"terminals": [dict(self._descriptor)]})

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
        call.set_operation("terminal.open")
        with call.stage("pty_spawn"):
            pass
        return call.answer(dict(self._descriptor))

    async def resize_terminal(
        self, workspace_id: str, terminal_id: str, *, rows: int, cols: int
    ) -> dict[str, Any]:
        """Answer the unmeasured sibling route's resize."""
        self.calls.append({"op": "resize_terminal", "workspace_id": workspace_id})
        return {**self._descriptor, "rows": rows, "cols": cols}

    async def stream_terminal(
        self,
        workspace_id: str,
        terminal_id: str,
        *,
        after: int,
        profiling: dict[str, Any] | None = None,
    ) -> Any:
        """Answer a stream attach in headers, the way the sandbox manager's SSE route does."""
        import httpx

        from cloud_agent.sandbox_manager_client import SandboxManagerStream

        self.calls.append({"op": "stream_terminal", "workspace_id": workspace_id})
        call = self._open(profiling, "terminal.open")
        call.set_operation("terminal.open")
        with call.stage("stream_attach"):
            pass
        response = httpx.Response(200, headers=call.headers(), stream=httpx.ByteStream(b""))
        return SandboxManagerStream(
            _ClosedClient(),
            response,
            profiling_answer=operation_profiling.answer_from_headers(response.headers),
        )


class _ClosedClient:
    """A client the stream stand-in never opens, so closing it is a no-op."""

    async def aclose(self) -> None:
        """Close nothing."""
        return None


def install_sandbox_manager(sandbox_manager: BenchSandboxManager) -> None:
    """Point every resolved sandbox manager at the stand-in, as the route tests do."""
    sandbox_manager_routing._client_for = lambda _host: sandbox_manager  # type: ignore[assignment]


def uninstall_sandbox_manager(original: Any) -> None:
    """Restore the real sandbox manager factory."""
    sandbox_manager_routing._client_for = original  # type: ignore[assignment]


# --------------------------------------------------------------------------
# destinations
# --------------------------------------------------------------------------


class LocalMeasurementSink:
    """The OperationMeasurements write half, backed by a local JSONL file.

    This is the ``on-local`` destination for a control-plane-rooted request.
    There is no deployment switch that removes the Postgres repository from this
    process, so "enabled, local sink only" is produced here by giving the app a
    storage view whose measurement destination is a file. Everything else in the
    cohort -- the collector, the route, the bounded writer queue and the worker
    task that makes this call -- is the production code.
    """

    def __init__(self, path: Path, *, delay_s: float = 0.0) -> None:
        self.path = path
        self.delay_s = delay_s
        self.written = 0
        self.bytes = 0
        self._lock = threading.Lock()

    async def record_operation_measurement(self, *, measurement: Any, run: Any = None) -> str:
        """Append one prepared record, off the event loop, and report it stored."""
        line = json.dumps(
            {"measurement": measurement, "run": run}, separators=(",", ":"), default=str
        )
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        await asyncio.to_thread(self._append, line)
        return "stored"

    def _append(self, line: str) -> None:
        """Append one line under the lock, so concurrent writers cannot interleave."""
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self.written += 1
            self.bytes += len(line.encode("utf-8"))

    def as_dict(self, expected: int) -> dict[str, Any]:
        """Return the local destination's counts."""
        return {
            "path": str(self.path),
            "records_expected": expected,
            "records_written": self.written,
            "records_dropped": max(0, expected - self.written),
            "bytes": self.bytes,
            "bytes_per_record": round(self.bytes / self.written, 1) if self.written else 0.0,
            "synthetic_per_record_delay_ms": self.delay_s * 1000.0,
        }


class DestinationStorage:
    """A storage view that replaces only the measurement write destination."""

    def __init__(self, storage: Any, destination: Any) -> None:
        self._storage = storage
        self.operation_measurements = destination

    def __getattr__(self, name: str) -> Any:
        """Delegate every other capability to the real storage assembly."""
        return getattr(self._storage, name)


class BenchGateway:
    """Gateway admission that always grants the one benchmark user ``agent.use``."""

    async def agent_access(self, user_id: str) -> dict[str, Any]:
        """Return the decision the owner routes require."""
        return {"user_id": user_id, "allowed": True, "permissions": ["agent.use"]}


# --------------------------------------------------------------------------
# the control plane
# --------------------------------------------------------------------------


@dataclass
class BenchSession:
    """Everything the two measurement sections share."""

    pool: Any
    url: str
    client: Any
    sandbox_manager: BenchSandboxManager
    db_config: dict[str, Any]
    job_id: str
    thread_id: str
    attempt_id: int
    lease_generation: int
    execution_profile: str
    workspace_id: str
    storage: Any
    worker_token: str
    tempdir: tempfile.TemporaryDirectory[str]
    created_database: bool
    docker: dict[str, Any] = field(default_factory=dict)
    server: Any = None
    server_task: Any = None

    def path(self, name: str) -> Path:
        """Return a fresh path inside this run's temporary directory."""
        return Path(self.tempdir.name) / name


async def _serve(app: FastAPI) -> tuple[Any, Any, str]:
    """Serve the app on an ephemeral port, on the caller's event loop."""
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover - startup failure
            raise RuntimeError("the benchmark control plane did not start")
        await asyncio.sleep(0.02)
    for socket_info in server.servers[0].sockets:
        host, port = socket_info.getsockname()[:2]
        return server, task, f"http://{host}:{port}"
    raise RuntimeError("the benchmark control plane has no socket")  # pragma: no cover


def _quiet_logging() -> None:
    """Keep the measured system's own logging out of the way.

    ``httpx`` logs every request at INFO and the control plane logs its own
    lifecycle; neither is part of what is being measured. The collector's INFO
    line is filtered here too, which means the log destination is not exercised
    in these cohorts: the JSONL sink is the destination this benchmark counts.
    """
    import logging

    logging.getLogger().setLevel(logging.WARNING)
    for name in ("httpx", "httpcore", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # The application configures logging for ``cloud_agent`` at INFO; the
    # benchmark's report is the output, so its lifecycle lines are silenced.
    logging.getLogger("cloud_agent").setLevel(logging.WARNING)
    for module in (operation_profiling, operation_call):
        logging.getLogger(module.__name__).setLevel(logging.WARNING)


def _open_session_environment(config: dict[str, Any], args: argparse.Namespace) -> None:
    """Install the deployment's own environment before the app is built."""
    os.environ.update(
        {
            "AGENT_DATABASE_URL": _dsn(config),
            "AGENT_CONTROL_TOKEN_SECRET": BENCH_CONTROL_TOKEN_SECRET,
            "ADMIN_TOKEN": BENCH_ADMIN_TOKEN,
            "AGENT_SESSION_SECRET": BENCH_SESSION_SECRET,
            "GATEWAY_ISSUER": BENCH_GATEWAY_ISSUER,
            "AGENT_SANDBOX_MANAGER_TOKEN": BENCH_SANDBOX_MANAGER_TOKEN,
            "AGENT_REPO_ALLOWLIST": BENCH_REPO,
            "AGENT_OPERATION_PROFILING_ENABLED": "",
        }
    )
    os.environ.pop(operation_profiling.ENV_PROFILING_ENABLED, None)
    os.environ.pop(operation_profiling.ENV_PROFILING_RUN, None)
    os.environ.pop(operation_profiling.ENV_PROFILING_SINK, None)


async def _seed_workspace_rows(storage: Any) -> dict[str, Any]:
    """Create the real job, attempt, host and sandbox rows a workspace request needs.

    The shape is the one ``tests/integration/storage/test_agent_job_store.py``
    builds for a shared-live chat: an enrolled host that advertises the shared
    sandbox capability, a claimed job, a reserved retained group, and the
    lifecycle event that publishes ``workspace_ready``.
    """
    from cloud_agent_host.shared_protocol import (
        NATIVE_FORK_AT_ANCHOR_CAPABILITY,
        SHARED_SANDBOX_CAPABILITY,
        SHARED_SANDBOX_HELPER_PINS,
        SHARED_SANDBOX_RUNTIME_PINS,
        shared_deployment_capability,
    )

    capabilities = [
        SHARED_SANDBOX_CAPABILITY,
        NATIVE_FORK_AT_ANCHOR_CAPABILITY,
        shared_deployment_capability("bench-overhead-deployment"),
    ]
    await storage.host_pool.register_host(
        host=BENCH_HOST,
        credential_hash=BENCH_HOST_CREDENTIAL,
        capabilities=capabilities,
        slots_total=8,
    )
    online = await storage.host_pool.record_host_heartbeat(
        host=BENCH_HOST,
        capabilities=capabilities,
        sandbox_image_digest=BENCH_IMAGE_DIGEST,
        sandbox_manager_url=BENCH_SANDBOX_MANAGER_URL,
    )
    if online is None:  # pragma: no cover - fixture failure
        raise RuntimeError("the benchmark host did not come online")
    job = await storage.conversation_commands.create_job(
        user_id=BENCH_USER_ID,
        repo=BENCH_REPO,
        task_prompt="benchmark the operation profiler",
        runtime=BENCH_RUNTIME_ID,
        model="bench-model",
        base_sha="a" * 40,
    )
    claim = await storage.attempt_commands.claim_job(
        worker_id="bench-overhead-worker",
        lease_ttl_seconds=3600,
        host=BENCH_HOST,
        host_credential_hash=BENCH_HOST_CREDENTIAL,
        claim_attestation={
            "sandbox_image_digest": BENCH_IMAGE_DIGEST,
            "runner_capabilities": capabilities,
            "runtime_versions": dict(SHARED_SANDBOX_RUNTIME_PINS),
            "helper_versions": dict(SHARED_SANDBOX_HELPER_PINS),
        },
    )
    if claim is None:  # pragma: no cover - fixture failure
        raise RuntimeError("the benchmark job could not be claimed")
    sandbox = await storage.sandbox.reserve_thread_sandbox(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host=BENCH_HOST,
        sandbox_id=BENCH_SANDBOX_ID,
        container_name=BENCH_CONTAINER_NAME,
        runtime_id=BENCH_RUNTIME_ID,
        runtime_version=claim.get("runtime_version"),
        sandbox_image_digest=claim.get("sandbox_image_digest"),
        execution_profile=job["execution_profile"],
    )
    if sandbox is None:  # pragma: no cover - fixture failure
        raise RuntimeError("the benchmark sandbox could not be reserved")
    running = await storage.sandbox.mark_thread_sandbox_running(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        sandbox_generation=sandbox["generation"],
        activation_generation=sandbox["activation_generation"],
        container_id=f"container-{BENCH_SANDBOX_ID}",
        context_status="new",
    )
    if running is None:  # pragma: no cover - fixture failure
        raise RuntimeError("the benchmark sandbox did not reach running")
    await storage.attempt_commands.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="lifecycle",
        payload={"phase": "workspace_ready"},
    )
    if not await storage.sandbox.thread_workspace_ready(thread_id=str(job["thread_id"])):
        raise RuntimeError("the benchmark workspace never became ready")
    return {
        "job_id": str(job["id"]),
        "thread_id": str(job["thread_id"]),
        "attempt_id": int(claim["attempt_id"]),
        "lease_generation": int(claim["lease_generation"]),
        "execution_profile": str(job["execution_profile"]),
        "workspace_id": str(job["thread_id"]),
    }


async def _open_session(config: dict[str, Any], args: argparse.Namespace) -> BenchSession:
    """Build the real app over the real database and serve it on a real socket."""
    import asyncpg
    import httpx

    from cloud_agent.api.deps import get_agent_app_credentials, get_gateway_client
    from cloud_agent.app import create_app
    from cloud_agent.identity import SESSION_COOKIE, mint_session
    from cloud_agent.tokens import SCOPE_FULL, mint_worker_token

    _open_session_environment(config, args)
    created = await _ensure_database(config)
    _apply_schema(config)
    pool = await asyncpg.create_pool(**config, min_size=1, max_size=10)
    await _truncate(pool)

    app = create_app()
    app.dependency_overrides[get_gateway_client] = lambda: BenchGateway()
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    server, task, url = await _serve(app)
    storage = app.state.storage
    rows = await _seed_workspace_rows(storage)
    session_token = mint_session(user_id=BENCH_USER_ID)
    client = httpx.AsyncClient(
        base_url=url,
        cookies={SESSION_COOKIE: session_token},
        timeout=httpx.Timeout(120.0),
    )
    session = BenchSession(
        pool=pool,
        url=url,
        client=client,
        sandbox_manager=BenchSandboxManager(),
        db_config=config,
        storage=storage,
        tempdir=tempfile.TemporaryDirectory(prefix="operation-overhead-bench-"),
        created_database=created,
        server=server,
        server_task=task,
        worker_token=mint_worker_token(
            job_id=rows["job_id"],
            attempt_id=rows["attempt_id"],
            lease_generation=rows["lease_generation"],
            scope=SCOPE_FULL,
        ),
        **rows,
    )
    return session


async def _close_session(session: BenchSession) -> None:
    """Stop the server, close the pool, and wipe the rows this run created."""
    await session.client.aclose()
    if session.server is not None:
        session.server.should_exit = True
    if session.server_task is not None:
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(session.server_task, timeout=30)
    try:
        await _truncate(session.pool)
    finally:
        await session.pool.close()
    session.tempdir.cleanup()


# --------------------------------------------------------------------------
# server-request cohorts
# --------------------------------------------------------------------------


def _configure_profiling(
    *,
    cohort: str,
    run_id: str,
    sink_path: Path,
    budget: int,
    operations: tuple[str, ...],
    target: dict[str, str],
) -> None:
    """Point the profiler at this cohort's configuration and drop its caches."""
    for name in (
        operation_profiling.ENV_PROFILING_ENABLED,
        operation_profiling.ENV_PROFILING_RUN,
        operation_profiling.ENV_PROFILING_SINK,
    ):
        os.environ.pop(name, None)
    if cohort != "off" and cohort != "bare":
        os.environ[operation_profiling.ENV_PROFILING_ENABLED] = "true"
        os.environ[operation_profiling.ENV_PROFILING_SINK] = str(sink_path)
        os.environ[operation_profiling.ENV_PROFILING_RUN] = json.dumps(
            {
                "run_id": run_id,
                "operations": list(operations),
                "expires_at": (datetime.now(UTC) + RUN_LIFETIME).isoformat(),
                "max_operations": budget,
                "target": target,
            },
            separators=(",", ":"),
        )
    # Both caches are per process, so every cohort has to clear them.
    operation_profiling.reset_cache()
    reset_selections()


def _clear_profiling_environment() -> None:
    """Leave the process as it was found and stop the collector's exporter."""
    for name in (
        operation_profiling.ENV_PROFILING_ENABLED,
        operation_profiling.ENV_PROFILING_RUN,
        operation_profiling.ENV_PROFILING_SINK,
    ):
        os.environ.pop(name, None)
    operation_profiling.close_sink()
    operation_profiling.reset_cache()
    reset_selections()


async def _send_operation(client: httpx.AsyncClient, job_id: str, label: str) -> httpx.Response:
    """Send one measured request through the real route."""
    if label == "files.list.root":
        return await client.get(f"/v1/agent/jobs/{job_id}/files", params={"path": ""})
    if label == "files.list.subdir":
        return await client.get(f"/v1/agent/jobs/{job_id}/files", params={"path": "src"})
    if label == "files.open":
        return await client.get(f"/v1/agent/jobs/{job_id}/files", params={"path": "small.txt"})
    if label == "terminal.list":
        return await client.get(f"/v1/agent/jobs/{job_id}/terminals")
    if label == "terminal.create":
        return await client.post(
            f"/v1/agent/jobs/{job_id}/terminals", json={"rows": 24, "cols": 80}
        )
    if label == "terminal.stream_attach":
        return await client.get(
            f"/v1/agent/jobs/{job_id}/terminals/{BENCH_TERMINAL_ID}/stream", params={"after": 0}
        )
    raise SystemExit(f"unknown server operation {label!r}")


async def _send_bare_analogue(client: httpx.AsyncClient, job_id: str, family: str) -> Any:
    """Send the unmeasured sibling request that stands in for a bare cohort."""
    if family == "files":
        return await client.put(
            f"/v1/agent/jobs/{job_id}/files",
            params={"path": "bench.txt"},
            json={"content": "bench"},
        )
    if family == "terminals":
        return await client.post(
            f"/v1/agent/jobs/{job_id}/terminals/{BENCH_TERMINAL_ID}/resize",
            json={"rows": 40, "cols": 120},
        )
    raise SystemExit(f"unknown operation family {family!r}")


def _sink_record_count(path: Path) -> tuple[int, int]:
    """Return how many records and bytes the collector's own JSONL sink holds."""
    if not path.exists():
        return 0, 0
    with path.open(encoding="utf-8") as handle:
        lines = [line for line in handle if line.strip()]
    return len(lines), path.stat().st_size


def _read_sink_records(path: Path) -> list[dict[str, Any]]:
    """Return the records the collector's JSONL sink holds."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records


async def _writer_status(client: httpx.AsyncClient) -> dict[str, Any]:
    """Read this replica's writer queue state through the real admin route."""
    response = await client.get(
        "/admin/agent/measurement-writer",
        headers={"Authorization": f"Bearer {BENCH_ADMIN_TOKEN}"},
    )
    if response.status_code != 200:  # pragma: no cover - fixture failure
        raise RuntimeError(f"measurement-writer status returned {response.status_code}")
    return dict(response.json())


async def _await_writer_drained(writer: Any, *, timeout_s: float) -> None:
    """Wait, bounded, for the writer's queue and in-flight writes to finish."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if writer.pending == 0 and writer.in_flight == 0:
            return
        await asyncio.sleep(0.02)


@dataclass
class CohortResult:
    """One cohort's samples, counters and background cost for one section."""

    samples: dict[str, list[int]]
    drops: dict[str, Any]
    background: dict[str, Any]
    delivery: dict[str, Any] = field(default_factory=dict)
    recorded_paths: dict[str, int] = field(default_factory=dict)


async def _run_server_cohort(
    session: BenchSession,
    *,
    cohort: str,
    args: argparse.Namespace,
    round_index: int = 0,
    drain_between_operations: bool = True,
) -> CohortResult:
    """Run every server operation once per cohort and collect the real counters."""
    run_id = f"bench-server-{cohort}-r{round_index}-{uuid.uuid4().hex[:8]}".replace("_", "-")
    sink_path = session.path(f"sink-{cohort}-r{round_index}.jsonl")
    total = args.warmup + args.iterations
    destination: LocalMeasurementSink | None = None
    if cohort in {"on-local", "slow-sink"}:
        # Only the slow-destination arm carries the synthetic delay; the
        # baseline on-local cohort writes as fast as the file allows.
        delay_s = args.slow_sink_ms / 1000.0 if cohort == "slow-sink" else 0.0
        destination = LocalMeasurementSink(
            session.path(f"local-{cohort}-r{round_index}.jsonl"), delay_s=delay_s
        )

    _configure_profiling(
        cohort="on-local" if cohort == "slow-sink" else cohort,
        run_id=run_id,
        sink_path=sink_path,
        budget=total * len(SERVER_OPERATIONS) + RUN_BUDGET_SLACK,
        operations=SERVER_RUN_OPERATIONS,
        target={"job_id": session.job_id},
    )
    writer = measurement_writer.MeasurementWriter()
    measurement_writer.reset_writer(writer)
    original_sandbox_manager = sandbox_manager_routing._client_for
    install_sandbox_manager(session.sandbox_manager)
    storage = session.storage
    view: Any = DestinationStorage(storage, destination) if destination is not None else storage
    _install_storage_override(session, view)

    samples: dict[str, list[int]] = {label: [] for label, _ in SERVER_OPERATIONS}
    for family in BARE_ANALOGUES:
        samples[f"bare:{family}"] = []
    cpu_before = time.process_time()
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    threads_before = threading.active_count()
    drain_waits: list[dict[str, Any]] = []
    try:
        for label, _description in SERVER_OPERATIONS:
            for _ in range(args.warmup):
                response = await _send_operation(session.client, session.job_id, label)
                _require_ok(response, label)
            for _ in range(args.iterations):
                started = time.perf_counter_ns()
                response = await _send_operation(session.client, session.job_id, label)
                elapsed = time.perf_counter_ns() - started
                _require_ok(response, label)
                samples[label].append(elapsed)
            # Between operations the queue is allowed to finish what this
            # operation produced. Without it each cohort would stack one
            # synthetic burst on top of the last, and a drop would say more
            # about the benchmark's total request rate than about the
            # destination the cohort is meant to describe. The slow-destination
            # arm turns this off on purpose: sustained overload is what it is
            # there to produce.
            drain_started = time.monotonic()
            if drain_between_operations:
                await _await_writer_drained(writer, timeout_s=args.drain_timeout_s)
            drain_waits.append(
                {
                    "operation": label,
                    "seconds": round(time.monotonic() - drain_started, 4),
                    "waited": drain_between_operations,
                }
            )
        if cohort == "bare":
            for family in BARE_ANALOGUES:
                for _ in range(args.warmup):
                    await _send_bare_analogue(session.client, session.job_id, family)
                for _ in range(args.iterations):
                    started = time.perf_counter_ns()
                    response = await _send_bare_analogue(session.client, session.job_id, family)
                    elapsed = time.perf_counter_ns() - started
                    _require_ok(response, f"bare:{family}")
                    samples[f"bare:{family}"].append(elapsed)
        # Drain both destinations before counting, so a shortfall is a real
        # drop and not a queue still in flight.
        operation_profiling.flush_sink()
        await _await_writer_drained(writer, timeout_s=max(10.0, args.slow_sink_ms / 1000.0 * total))
        status = await _writer_status(session.client)
        counts = dict(status["counts"])
        pending = int(status["pending"])
        in_flight = int(status["in_flight"])
        await writer.shutdown(timeout_s=10.0)
        operation_profiling.close_sink()
    finally:
        _install_storage_override(session, storage)
        uninstall_sandbox_manager(original_sandbox_manager)

    # The bare and off cohorts have no collector call at all, so they owe no
    # records; the instrumented cohorts owe one per measured request.
    measured_ops = (
        len(SERVER_OPERATIONS) if cohort in {"on-local", "on-persist", "slow-sink"} else 0
    )
    expected = total * measured_ops
    if cohort in {"on-local", "on-persist", "slow-sink"}:
        _verify_cohort_selected(cohort, counts, expected)
    sink_records, sink_bytes = _sink_record_count(sink_path)
    drops: dict[str, Any] = {
        "writer": counts,
        "writer_pending_at_end": pending,
        "writer_in_flight_at_end": in_flight,
        "writer_running_at_end": bool(status["running"]),
        "sink_records_expected": expected,
        "sink_records_written": sink_records,
        "sink_records_dropped": max(0, expected - sink_records),
        "sink_bytes": sink_bytes,
        "drain_waits_s": drain_waits,
        "drain_timeout_s": args.drain_timeout_s,
        "destination": "local JSONL (bench-local capability)"
        if destination is not None
        else (
            "Postgres agent_operation_measurements"
            if cohort == "on-persist"
            else "none (profiler off)"
        ),
    }
    if destination is not None:
        drops["local_destination"] = destination.as_dict(expected)
    if cohort in {"on-persist", "slow-db"}:
        drops["postgres"] = await _postgres_measurement_footprint(session.pool, run_id)

    cpu_after = time.process_time()
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # One number, and it is the whole process: ``time.process_time`` sums every
    # thread, so there is no per-thread split to report here. The earlier
    # revision subtracted "CPU inside the request-await windows" and called the
    # remainder background-thread cost; both halves were process-wide, the
    # windows also carried the peer's work on this shared loop, and the
    # remainder also carried warmup, preparation and the draining sweeps. It was
    # not a measurement of the exporter or the writer. What is measurable is the
    # total process CPU one configuration spends on the same request set, which
    # is what is reported and what the cohort-to-cohort delta is computed from.
    background = {
        "process_cpu_s": round(cpu_after - cpu_before, 4),
        "cpu_scope": "whole process, every thread, over this cohort's warmup and timed rounds",
        "threads_before": threads_before,
        "threads_after": threading.active_count(),
        "peak_rss_mb_before": round(_rss_mb(rss_before), 2),
        "peak_rss_mb_after": round(_rss_mb(rss_after), 2),
        "sink_bytes_written": sink_bytes,
        "not_measured": [
            "network bytes: a loopback HTTP exchange has no portable per-socket counter here",
            "per-thread CPU: resource.RUSAGE_THREAD is unavailable on this platform, so the "
            "collector's exporter thread and the writer's tasks cannot be charged separately "
            "from the request path; only the cohort's total process CPU is measured",
            "CPU attributable to the measurement machinery alone: the delta against `off` is "
            "the whole configuration's extra process CPU, and the client and server share this "
            "process, so part of any delta is the load generator's own extra work",
            "WAL, index and disk bytes: the Postgres number is payload bytes only",
            "container CPU and memory: no container is involved in a server-request cohort",
        ],
    }
    return CohortResult(samples=samples, drops=drops, background=background)


#: The counters ``WriteCounts.as_dict()`` reports as plain integers.
COUNTER_FIELDS: tuple[str, ...] = (
    "stored",
    "duplicate",
    "rejected",
    "conflict",
    "dropped_full",
    "dropped_timeout",
    "dropped_shutdown",
    "failed",
)


def _merge_writer_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Add up one counter block per round into the cohort's total."""
    totals = dict.fromkeys(COUNTER_FIELDS, 0)
    reasons: dict[str, int] = {}
    for row in rows:
        for name in COUNTER_FIELDS:
            totals[name] += int(row.get(name, 0))
        for name, count in (row.get("reasons") or {}).items():
            reasons[name] = reasons.get(name, 0) + int(count)
    return {**totals, "reasons": reasons}


def _requests_in_window(args: argparse.Namespace, *, rounds: int) -> int:
    """Return how many requests one cohort's CPU window really contained.

    Warmup is inside the window: the clock starts before it and stops after the
    last timed request. Dividing that CPU by the timed requests alone overstates
    the per-record cost by ``iterations / (warmup + iterations)`` -- 13.3% at the
    defaults -- because the same CPU is divided by fewer requests than produced
    it. `bare` sends two analogue rounds' worth on top, which is why its CPU is
    reported but never compared.
    """
    return rounds * (args.warmup + args.iterations) * len(SERVER_OPERATIONS)


def _merge_background(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Add up one cohort's process CPU across its rounds."""
    return {
        "rounds": len(rows),
        "process_cpu_s": round(sum(row["process_cpu_s"] for row in rows), 4),
        "cpu_scope": rows[-1]["cpu_scope"],
        "sink_bytes_written": sum(int(row["sink_bytes_written"]) for row in rows),
        "peak_rss_mb_before": min(row["peak_rss_mb_before"] for row in rows),
        "peak_rss_mb_after": max(row["peak_rss_mb_after"] for row in rows),
        "per_round_process_cpu_s": [row["process_cpu_s"] for row in rows],
        "not_measured": rows[-1]["not_measured"],
    }


def _noise_floor(off_rounds: list[dict[str, Any]], label: str) -> dict[str, Any] | None:
    """Measure the system's own run-to-run variation, using ``off`` against itself.

    The profiler's effect in this benchmark is around the same size as the
    scheduler noise of a loopback client and server sharing one event loop. A
    delta is only worth reading if it is larger than this number, which is why
    the same cohort's even and odd rounds are compared with each other: no
    instrumentation difference exists between them.
    """
    even: list[int] = []
    odd: list[int] = []
    for row in off_rounds:
        values = row["raw"].get(label)
        if not values:
            continue
        (even if row["round"] % 2 == 0 else odd).extend(values)
    if not even or not odd:
        return None
    first, second = Sample(even), Sample(odd)
    return {
        "what": "the `off` cohort's even rounds against its odd rounds (no difference)",
        "even_n": len(even),
        "odd_n": len(odd),
        "delta_p50_ms": round(second.percentile_ms(0.50) - first.percentile_ms(0.50), 3),
        "delta_p95_ms": round(second.percentile_ms(0.95) - first.percentile_ms(0.95), 3),
        "abs_delta_p50_ms": round(abs(second.percentile_ms(0.50) - first.percentile_ms(0.50)), 3),
        "abs_delta_p95_ms": round(abs(second.percentile_ms(0.95) - first.percentile_ms(0.95)), 3),
    }


def _install_storage_override(session: BenchSession, view: Any) -> None:
    """Point ``get_storage`` at one destination for the duration of a cohort."""
    from cloud_agent.api.deps import get_storage

    session.server.config.app.dependency_overrides[get_storage] = lambda: view


def _require_ok(response: httpx.Response, label: str) -> None:
    """Fail loudly when a cohort's request did not take the business path."""
    if response.status_code != 200:
        raise SystemExit(
            f"{label}: expected 200, got {response.status_code}: {response.text[:300]}"
        )


def _verify_cohort_selected(cohort: str, counts: dict[str, Any], expected: int) -> None:
    """Fail loudly if the run stopped selecting mid-cohort.

    Without this a descriptor that expired or ran out of budget would silently
    turn an instrumented cohort into a second measurement of ``off``, and the
    table would look perfectly reasonable.

    A dropped record is not a failure: the queue is bounded on purpose, and a
    record that was dropped was still selected, counted and reported. What must
    hold is that every request the cohort measured is accounted for somewhere --
    stored, duplicated, refused, or dropped with a reason.
    """
    accounted = sum(
        int(counts.get(name, 0))
        for name in (
            "stored",
            "duplicate",
            "rejected",
            "conflict",
            "dropped_full",
            "dropped_timeout",
            "dropped_shutdown",
            "failed",
        )
    )
    if accounted == 0:
        raise SystemExit(
            f"{cohort}: the profiling run selected nothing, so this cohort measured the "
            "disabled path (expired descriptor, exhausted max_operations, or a target mismatch)"
        )
    if accounted < expected:
        raise SystemExit(
            f"{cohort}: {expected} requests were measured and only {accounted} records were "
            f"accounted for (counts: {counts})"
        )


def _rss_mb(value: int) -> float:
    """Return ``ru_maxrss`` in MiB, on either unit convention.

    Linux reports kilobytes and macOS bytes; the value is the process's peak,
    so it is reported as an absolute rather than as a per-cohort delta.
    """
    if platform.system() == "Darwin":
        return value / (1024.0 * 1024.0)
    return value / 1024.0


# --------------------------------------------------------------------------
# synthetic arms
# --------------------------------------------------------------------------


async def _acquire_table_lock(config: dict[str, Any]) -> Any:
    """Hold an ACCESS EXCLUSIVE lock on the measurements table.

    The slowness is synthetic and lives entirely in the database: the table is
    locked by a second connection this script owns, so the writer queue's two
    workers block in ``INSERT`` exactly as they would against a database that is
    congested. Nothing in the routes, the collector or the writer is modified.
    """
    import asyncpg

    connection = await asyncpg.connect(**config)
    transaction = connection.transaction()
    await transaction.start()
    await connection.execute("LOCK TABLE agent_operation_measurements IN ACCESS EXCLUSIVE MODE")
    return connection, transaction


async def _release_table_lock(handle: Any) -> None:
    """Release the synthetic lock."""
    connection, transaction = handle
    await transaction.rollback()
    await connection.close()


async def _run_slow_database_arm(session: BenchSession, args: argparse.Namespace) -> dict[str, Any]:
    """Measure ``on-persist`` while the destination table is locked."""
    run_id = f"bench-slow-db-{uuid.uuid4().hex[:8]}"
    sink_path = session.path("sink-slow-db.jsonl")
    iterations = max(1, args.slow_iterations)
    _configure_profiling(
        cohort="on-persist",
        run_id=run_id,
        sink_path=sink_path,
        budget=iterations * 4 + RUN_BUDGET_SLACK,
        operations=SERVER_RUN_OPERATIONS,
        target={"job_id": session.job_id},
    )
    writer = measurement_writer.MeasurementWriter()
    measurement_writer.reset_writer(writer)
    original_sandbox_manager = sandbox_manager_routing._client_for
    install_sandbox_manager(session.sandbox_manager)
    _install_storage_override(session, session.storage)
    samples: dict[str, list[int]] = {label: [] for label, _ in SERVER_OPERATIONS}
    lock = await _acquire_table_lock(session.db_config)
    lock_started = time.monotonic()
    try:
        for label, _description in SERVER_OPERATIONS:
            for _ in range(iterations):
                started = time.perf_counter_ns()
                response = await _send_operation(session.client, session.job_id, label)
                samples[label].append(time.perf_counter_ns() - started)
                _require_ok(response, label)
        during = await _writer_status(session.client)
        # Hold the lock for the configured span even though the requests are
        # already answered: the queue and the two writer workers must sit in the
        # congestion long enough to reach their own bounds (the count bound, and
        # the writer's 10 s timeout) for those drops to be observed rather than
        # assumed.
        remaining = args.slow_db_hold_s - (time.monotonic() - lock_started)
        if remaining > 0:
            await asyncio.sleep(remaining)
        late = await _writer_status(session.client)
    finally:
        await _release_table_lock(lock)
    released_at = time.monotonic()
    # The lock is gone; the writers should now make progress. Wait for the
    # queue to settle so recovery is a measurement and not an assumption.
    recovered = await _writer_status(session.client)
    deadline = released_at + max(15.0, _slow_drain_budget(args))
    while time.monotonic() < deadline:
        if int(recovered["pending"]) == 0 and int(recovered["in_flight"]) == 0:
            break
        await asyncio.sleep(0.05)
        recovered = await _writer_status(session.client)
    recovered_after_s = time.monotonic() - released_at
    operation_profiling.flush_sink()
    await _await_writer_drained(writer, timeout_s=60.0)
    after = await _writer_status(session.client)
    await writer.shutdown(timeout_s=10.0)
    operation_profiling.close_sink()
    _install_storage_override(session, session.storage)
    uninstall_sandbox_manager(original_sandbox_manager)
    sink_records, sink_bytes = _sink_record_count(sink_path)
    return {
        "synthetic": True,
        "what_was_slowed": "PostgreSQL, by an ACCESS EXCLUSIVE lock this script held on "
        "agent_operation_measurements from a second connection",
        "hold_s": args.slow_db_hold_s,
        "iterations_per_operation": iterations,
        "requests_answered": iterations * len(SERVER_OPERATIONS),
        "samples": {label: Sample(values).as_dict() for label, values in samples.items()},
        "writer_during_lock": dict(during["counts"]),
        "writer_queue_during_lock": {
            "pending": int(during["pending"]),
            "in_flight": int(during["in_flight"]),
            "queued_bytes": int(during["queued_bytes"]),
        },
        "writer_at_end_of_hold": dict(late["counts"]),
        "writer_queue_at_end_of_hold": {
            "pending": int(late["pending"]),
            "in_flight": int(late["in_flight"]),
            "queued_bytes": int(late["queued_bytes"]),
        },
        "writer_after_release": dict(after["counts"]),
        "writer_queue_after_release": {
            "pending": int(after["pending"]),
            "in_flight": int(after["in_flight"]),
        },
        "recovered_after_s": round(recovered_after_s, 3),
        "recovery_budget_s": max(15.0, _slow_drain_budget(args)),
        "sink_records_written": sink_records,
        "sink_bytes": sink_bytes,
        "postgres": await _postgres_measurement_footprint(session.pool, run_id),
    }


def _slow_drain_budget(args: argparse.Namespace) -> float:
    """Return how long the slow-database arm waits for the queue to recover."""
    return max(15.0, args.slow_db_hold_s + 10.0)


async def _run_slow_sink_arm(session: BenchSession, args: argparse.Namespace) -> dict[str, Any]:
    """Measure ``on-local`` against a destination delayed per record.

    The delay is synthetic and lives in the bench-local destination stand-in for
    a slow control-plane-side writer, not in production code: the request path,
    the collector and the bounded queue are untouched, which is the claim under
    test.
    """
    result = await _run_server_cohort(
        session, cohort="slow-sink", args=args, drain_between_operations=False
    )
    return {
        "synthetic": True,
        "what_was_slowed": (
            f"the bench-local on-local destination, delayed {args.slow_sink_ms:.1f} ms per "
            "record, and left to accumulate across operations so consecutive bursts stack in "
            "the bounded queue. This is a stand-in for a slow control-plane-side destination, "
            "not the control plane's ingest endpoint: a route-rooted record is persisted "
            "in-process, so there is no HTTP ingest hop in this path to delay. The request "
            "path, the collector and the writer queue are untouched"
        ),
        "drains_between_operations": False,
        "iterations": args.iterations,
        "samples": {
            label: Sample(values).as_dict() for label, values in result.samples.items() if values
        },
        "drops": result.drops,
        "background": result.background,
    }


# --------------------------------------------------------------------------
# startup paths
# --------------------------------------------------------------------------


def _docker(*argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one docker command."""
    result = subprocess.run(["docker", *argv], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise SystemExit(f"docker {' '.join(argv)} failed: {result.stderr.strip()[:300]}")
    return result


def docker_available() -> dict[str, Any]:
    """Return what the local Docker daemon reports, or why it cannot be used."""
    try:
        probe = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}} {{.Driver}} {{.OSType}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    if probe.returncode != 0:
        return {"available": False, "reason": probe.stderr.strip()[:300] or "docker info failed"}
    return {"available": True, "info": probe.stdout.strip()}


def _build_bench_image() -> None:
    """Build the bookworm guest image the startup container runs."""
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory(prefix="cae-overhead-image-") as directory:
        Path(directory, "Dockerfile").write_text(
            "FROM debian:bookworm-slim\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends python3 \\\n"
            "    nftables && rm -rf /var/lib/apt/lists/*\n",
            encoding="utf-8",
        )
        _docker("build", "-q", "-t", BENCH_IMAGE, directory)


class StartupSandbox:
    """The Docker plumbing one real retained chat container needs."""

    def __init__(self) -> None:
        from cloud_agent_host.chat_sandbox import (
            ChatSandboxManager,
            ChatSandboxSpec,
            RetainedChatBackend,
        )
        from cloud_agent_host.sandbox import ContainerBackend
        from cloud_agent_shared.egress import network_from_env

        self.network = network_from_env(
            {
                "AGENT_SANDBOX_NAMESPACE": BENCH_NAMESPACE,
                "AGENT_EGRESS_CHAT_NETWORK_POOL": BENCH_POOL,
                "AGENT_EGRESS_DNS_SERVER": os.environ.get("CAE_BENCH_DNS_SERVER", "1.1.1.1"),
                "AGENT_RELAY_NETWORK": BENCH_RELAY_NET,
                "AGENT_GATEWAY_URL": f"http://{BENCH_GATEWAY}:{BENCH_GATEWAY_PORT}",
                "AGENT_SANDBOX_MCP_URL": f"http://{BENCH_MCP}:{BENCH_MCP_PORT}",
            }
        )
        self.manager = ChatSandboxManager(
            ContainerBackend(
                image=BENCH_IMAGE,
                sandbox_network=self.network,
                sandbox_namespace=BENCH_NAMESPACE,
                network_helper_image=BENCH_IMAGE,
            )
        )
        self.spec = ChatSandboxSpec(
            thread_id=BENCH_STARTUP_THREAD,
            sandbox_id="asbx_overhead_startup",
            generation=1,
            runtime_id=BENCH_RUNTIME_ID,
            runtime_version=BENCH_RUNTIME_VERSION,
            shared_live=True,
            memory_limit="1g",
            cpu_limit="1",
            resident_slot_capacity=2,
        )
        self.backend = RetainedChatBackend(self.manager, self.spec)

    @property
    def container_name(self) -> str:
        """Return the container this sandbox owns, named by the spec's own rule."""
        return self.spec.container_name

    def _relay(self, name: str, port: int) -> None:
        # Listening, because the guest check dials both relays before the
        # sandbox counts as started.
        _docker("rm", "-f", name, check=False)
        _docker(
            "run",
            "-d",
            "--name",
            name,
            "--hostname",
            name,
            "--network",
            BENCH_RELAY_NET,
            "--network-alias",
            name,
            BENCH_IMAGE,
            "python3",
            "-m",
            "http.server",
            str(port),
        )

    def up(self) -> None:
        """Create the relay network and its two listening relay stand-ins."""
        _docker("network", "create", "--internal", BENCH_RELAY_NET, check=False)
        self._relay(BENCH_GATEWAY, BENCH_GATEWAY_PORT)
        self._relay(BENCH_MCP, BENCH_MCP_PORT)

    def remove_container(self) -> None:
        """Remove the chat container so the next activation must create one."""
        _docker("rm", "-f", self.container_name, check=False)

    def down(self) -> None:
        """Remove the container, the relays, every network and the host rules."""
        from cloud_agent_host.chat_sandbox import CHAT_NAMESPACE_LABEL

        _docker("rm", "-f", self.container_name, check=False)
        for name in (BENCH_GATEWAY, BENCH_MCP):
            _docker("rm", "-f", name, check=False)
        listed = _docker(
            "network",
            "ls",
            "--filter",
            f"label={CHAT_NAMESPACE_LABEL}={BENCH_NAMESPACE}",
            "--format",
            "{{.Name}}",
            check=False,
        )
        for name in listed.stdout.split():
            _docker("network", "rm", name, check=False)
        _docker("network", "rm", BENCH_RELAY_NET, check=False)
        # The run installed this namespace's own tables; nothing else uses them.
        for family in ("inet", "bridge"):
            _docker(
                "run",
                "--rm",
                "--network",
                "host",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "NET_ADMIN",
                "--entrypoint",
                "nft",
                BENCH_IMAGE,
                "delete",
                "table",
                family,
                self.network.table,
                check=False,
            )


class BenchTransport:
    """A stand-in for the resident Agent CLI process.

    The supervisor's own code decides ``resident_action`` by asking this object
    whether it is alive and compatible; what is stood in is the CLI process, not
    the supervisor. No agent CLI and no model is ever started, so the CLI's own
    initialization is absent from the resident paths -- a limitation this script
    reports rather than hides.
    """

    retire_after_turn = False

    def __init__(self, launch: Any) -> None:
        self.launch = launch
        self.native_session_id = launch.native_session_id or "native-bench-overhead"
        self.alive = True

    def compatible_with(self, launch: Any) -> bool:
        """Mirror ``ResidentTransport.compatible_with`` without a credential compare."""
        from dataclasses import replace

        return (
            replace(
                self.launch,
                credential=launch.credential,
                residency_generation=launch.residency_generation,
            )
            == launch
        )

    def stderr_text(self) -> str:
        """Return no diagnostic; the stand-in has no stderr."""
        return ""

    @property
    def exit_code(self) -> None:
        """The stand-in never exits."""
        return None

    def close(self) -> None:
        """Mark the stand-in dead so the supervisor opens a new one."""
        self.alive = False


class BenchTransportFactory:
    """Open :class:`BenchTransport` instances in place of a real CLI session."""

    def __init__(self) -> None:
        self.opened: list[Any] = []

    def open(self, _spec: Any, launch: Any) -> BenchTransport:
        """Return a ready stand-in transport for this launch."""
        transport = BenchTransport(launch)
        self.opened.append(launch)
        return transport

    def fork_native(self, *_args: Any, **_kwargs: Any) -> str:
        """Refuse: native fork is outside this measurement."""
        raise NotImplementedError("native fork is not part of the startup overhead benchmark")


def _startup_manifest(*, native_session_id: str | None) -> list[Any]:
    """Return the control plane's member manifest for the one bench member."""
    from cloud_agent_host.shared_session_supervisor import SharedMemberManifest

    return [
        SharedMemberManifest(
            thread_id=BENCH_STARTUP_THREAD,
            native_session_id=native_session_id,
            context_status="live" if native_session_id else "new",
            resident_slot_id=0,
            activation_generation=1,
            process_generation=1,
            residency_generation=1,
            last_settled_job_id=None,
        )
    ]


def _startup_launch(*, native_session_id: str | None) -> Any:
    """Return the resident launch one activation carries.

    ``residency_generation`` is this group's fence and stays constant across
    activations; ``credential`` is the renewable residency grant the supervisor
    deliberately excludes from transport compatibility.
    """
    from cloud_agent_host.resident_transport import ResidentLaunch

    return ResidentLaunch(
        thread_id=BENCH_STARTUP_THREAD,
        runtime_id=BENCH_RUNTIME_ID,
        runtime_version=BENCH_RUNTIME_VERSION,
        native_session_id=native_session_id,
        model="bench-model",
        reasoning_effort=None,
        gateway_base_url="http://gateway.invalid",
        credential="bench-residency-credential",
        slot_id=0,
        residency_generation=BENCH_RESIDENCY_GENERATION,
    )


@dataclass
class StartupPathResult:
    """What one path's activation really did, per cohort."""

    intended: str
    container_actions: dict[str, int] = field(default_factory=dict)
    resident_actions: dict[str, int] = field(default_factory=dict)


def _activate_once(
    sandbox: StartupSandbox,
    supervisor: Any,
    *,
    expected_container_id: str | None,
    has_settled_context: bool,
    native_session_id: str | None,
    activation_generation: int,
    measurement: Any,
    on_frozen: Callable[[Any], None] | None = None,
) -> tuple[str, str]:
    """Run one real activation on the path the runner actually walks.

    Everything the startup pays for is here, in the runner's own order: the
    marks, the fragment registrations, the two supervisor calls *carrying the
    profiling context to the sandbox manager*, their answers recorded as fragments, the
    facts, the freeze and the handoff enqueue. The caller owns the clock, so the
    collection and the background enqueue are inside the measured window rather
    than beside it.

    The profiling context is the part that was missing: without
    ``measurement_context`` the supervisor and the container manager take their
    unprofiled branch, so an ``on`` cohort measured a startup that had not in
    fact collected the container-side sub-stages the runner collects.

    The answers are converted with ``dataclasses.asdict`` because that is what
    the runner receives: it talks to a sandbox manager over HTTP and gets a mapping back,
    and ``record_fragment`` will only accept a mapping. Passing the dataclass
    itself silently answered nothing, which left every record with
    ``missing_fragments`` and ``collection_complete: false`` -- a benchmark that
    had not collected what it claimed to be measuring.
    """
    operation_profiling.mark(measurement, "supervisor_acquire")
    acquire_call = operation_profiling.expect_fragment(
        measurement, SANDBOX_MANAGER_PRODUCER, "supervisor_acquire"
    )
    lease = supervisor.acquire(
        sandbox.spec,
        thread_id=BENCH_STARTUP_THREAD,
        owner=f"bench-attempt:{activation_generation}",
        activation_generation=activation_generation,
        has_settled_context=has_settled_context,
        manifest=_startup_manifest(native_session_id=native_session_id),
        expected_container_id=expected_container_id,
        profiling=operation_profiling.measurement_context(measurement, acquire_call),
    )
    operation_profiling.record_fragment(
        measurement,
        producer=SANDBOX_MANAGER_PRODUCER,
        call_id=acquire_call,
        stage="supervisor_acquire",
        response=asdict(lease),
    )
    operation_profiling.mark(measurement, "resident_start")
    resident_call = operation_profiling.expect_fragment(
        measurement, SANDBOX_MANAGER_PRODUCER, "resident_start"
    )
    resident = supervisor.ensure_resident(
        BENCH_STARTUP_THREAD,
        lease.token,
        _startup_launch(native_session_id=native_session_id),
        profiling=operation_profiling.measurement_context(measurement, resident_call),
    )
    operation_profiling.record_fragment(
        measurement,
        producer=SANDBOX_MANAGER_PRODUCER,
        call_id=resident_call,
        stage="resident_start",
        response=asdict(resident),
    )
    container_action = str(getattr(lease, "container_action", "unknown"))
    resident_action = str(getattr(resident, "resident_action", "unknown"))
    path = _classify(container_action, resident_action)
    operation_profiling.set_attributes(
        measurement,
        startup_path=path,
        container_action=container_action,
        resident_action=resident_action,
    )
    # The freeze and the handoff queue are on the measured path too: the runner
    # pays for the record it serializes and for starting the send before the
    # prompt goes out, and a benchmark that stopped before them would report a
    # cost the runner does not pay.
    _finish_collector(measurement)
    if on_frozen is not None:
        on_frozen(measurement)
    return container_action, resident_action


def _start_collector(cohort: str) -> Any:
    """Open one collector measurement, or nothing at all for the bare cohort.

    For a real runner this is the switch deciding whether an attempt is part of
    an experiment; ``bare`` is the configuration where no collector call exists
    on the path at all.
    """
    if cohort == "bare":
        return None
    return operation_profiling.start_operation(
        operation=STARTUP_OPERATION,
        host=BENCH_HOST,
        worker_id="bench-overhead-worker",
    )


def _finish_collector(measurement: Any) -> None:
    """Close one startup measurement with the runner's own outcome word."""
    operation_profiling.finish(measurement, outcome="dispatched")


def _measured_activation(
    sandbox: StartupSandbox,
    supervisor: Any,
    *,
    cohort: str,
    session: BenchSession,
    on_frozen: Callable[[Any], None] | None,
    expected_container_id: str | None,
    has_settled_context: bool,
    native_session_id: str | None,
    activation_generation: int,
) -> tuple[int, str, str, Any]:
    """Time the whole measured startup path, collection and handoff included.

    The clock is this process's own wall clock and it brackets everything the
    runner does for one attempt: opening the measurement, binding the facts the
    route resolved, both supervisor calls with their profiling context, the
    fragment answers, the facts, the freeze and the hand-off enqueue. Starting
    it after the first mark and stopping it before the last two would have
    charged the container's work to the cohort but not the instrumentation's,
    which is the number the comparison exists to produce.
    """
    started = time.perf_counter_ns()
    measurement = _start_collector(cohort)
    if measurement is not None:
        bound = operation_profiling.bind(
            measurement,
            job_id=session.job_id,
            attempt_id=session.attempt_id,
            sandbox_id=BENCH_SANDBOX_ID,
            runtime_id=BENCH_RUNTIME_ID,
            runtime_version=BENCH_RUNTIME_VERSION,
            execution_profile=session.execution_profile,
        )
        if not bound:  # pragma: no cover - fixture failure
            raise SystemExit("the startup measurement fell out of the run's scope")
    container_action, resident_action = _activate_once(
        sandbox,
        supervisor,
        expected_container_id=expected_container_id,
        has_settled_context=has_settled_context,
        native_session_id=native_session_id,
        activation_generation=activation_generation,
        measurement=measurement,
        on_frozen=on_frozen,
    )
    elapsed = time.perf_counter_ns() - started
    return elapsed, container_action, resident_action, measurement


def _activate_path(
    sandbox: StartupSandbox,
    path: str,
    *,
    cohort: str,
    session: BenchSession,
    on_frozen: Callable[[Any], None] | None = None,
    iteration: int,
    timed: bool = True,
) -> tuple[int, str, str, Any]:
    """Produce one declared startup path with a real container action.

    Each path is *prepared* into the state that forces its container action --
    always outside the clock, because a container this benchmark stopped on
    purpose is not a cost the startup pays -- and then activated by the same call
    the runner makes, so the classifier sees a real ``create`` / ``start`` /
    ``reuse`` rather than a timeline this script labelled.

    ``timed`` exists for the warmup: a warmup activation runs the same code with
    no measurement open, and its duration is discarded.
    """
    from cloud_agent_host.shared_session_supervisor import SharedChatSessionSupervisor

    def run(supervisor: Any, **kwargs: Any) -> tuple[int, str, str, Any]:
        if not timed:
            _activate_once(
                sandbox,
                supervisor,
                measurement=None,
                on_frozen=None,
                activation_generation=iteration + 1,
                **kwargs,
            )
            return 0, "", "", None
        return _measured_activation(
            sandbox,
            supervisor,
            cohort=cohort,
            session=session,
            on_frozen=on_frozen,
            activation_generation=iteration + 1,
            **kwargs,
        )

    if path == "new_group":
        # No supervisor state, no container: the activation must create both.
        sandbox.remove_container()
        supervisor = SharedChatSessionSupervisor(
            sandbox.manager, idle_timeout_s=3600, transport_factory=BenchTransportFactory()
        )
        try:
            return run(
                supervisor,
                expected_container_id=None,
                has_settled_context=False,
                native_session_id=None,
            )
        finally:
            supervisor.close()

    if path == "resume_stopped":
        # The container exists, has run on its group bridge, and is stopped --
        # the state an idle group is left in. The preparation is untimed and
        # uses the platform's own calls; only the activation below is measured.
        sandbox.remove_container()
        sandbox.manager.create(sandbox.spec)
        sandbox.manager.start(sandbox.spec)
        sandbox.manager.stop(sandbox.spec)
        stopped = sandbox.manager.inspect(sandbox.container_name)
        if stopped is None or stopped.running:  # pragma: no cover - preparation failure
            raise SystemExit("resume_stopped preparation left the container running")
        resume = SharedChatSessionSupervisor(
            sandbox.manager, idle_timeout_s=3600, transport_factory=BenchTransportFactory()
        )
        try:
            return run(
                resume,
                expected_container_id=stopped.container_id,
                has_settled_context=True,
                native_session_id=None,
            )
        finally:
            resume.close()

    if path == "resume_running":
        # A replacement sandbox manager: the container is running and durable, the local
        # process pipe is gone, and the native session has to be resumed.
        inspection = _ensure_running_container(sandbox)
        supervisor = SharedChatSessionSupervisor(
            sandbox.manager, idle_timeout_s=3600, transport_factory=BenchTransportFactory()
        )
        try:
            return run(
                supervisor,
                expected_container_id=inspection.container_id,
                has_settled_context=True,
                native_session_id="native-bench-resume",
            )
        finally:
            supervisor.close()

    if path == "reuse_live":
        # Prepare a live resident transport in this supervisor, settle it the way
        # a completed turn does, and then measure the next activation, which must
        # find the same container and the same process. The warm activation is
        # preparation and is deliberately unprofiled: it is not the startup under
        # measurement, and profiling it would spend the run's budget on it.
        prepared = _ensure_running_container(sandbox)
        supervisor = SharedChatSessionSupervisor(
            sandbox.manager, idle_timeout_s=3600, transport_factory=BenchTransportFactory()
        )
        warm = supervisor.acquire(
            sandbox.spec,
            thread_id=BENCH_STARTUP_THREAD,
            owner="bench-warmup",
            activation_generation=iteration + 1,
            has_settled_context=True,
            manifest=_startup_manifest(native_session_id=None),
            expected_container_id=prepared.container_id,
        )
        supervisor.ensure_resident(
            BENCH_STARTUP_THREAD,
            warm.token,
            _startup_launch(native_session_id=None),
        )
        supervisor.settle(
            BENCH_STARTUP_THREAD,
            warm.token,
            job_id="ajob_overhead_bench",
            context_status="live",
        )
        try:
            return run(
                supervisor,
                expected_container_id=prepared.container_id,
                has_settled_context=True,
                native_session_id=None,
            )
        finally:
            supervisor.close()

    raise SystemExit(f"unknown startup path {path!r}")


def _ensure_running_container(sandbox: StartupSandbox) -> Any:
    """Return a running container on its group bridge, creating it if needed.

    This is the untimed preparation for the two paths whose container is already
    running: the platform's own ``create`` -> ``start`` sequence, exactly as a
    settlement leaves a group.
    """
    inspection = sandbox.manager.inspect(sandbox.container_name)
    if inspection is None:
        sandbox.manager.create(sandbox.spec)
    return sandbox.manager.start(sandbox.spec)


async def _run_startup_section(session: BenchSession, args: argparse.Namespace) -> dict[str, Any]:
    """Measure the four startup paths, per cohort, on a real container."""
    from cloud_agent_host import runner as runner_mod
    from cloud_agent_host.runner import ClaimedJob

    sandbox = StartupSandbox()
    results: dict[str, dict[str, Any]] = {}
    paths: dict[str, StartupPathResult] = {
        path: StartupPathResult(intended=path) for path in STARTUP_PATHS
    }
    samples: dict[str, dict[str, list[int]]] = {
        path: {cohort: [] for cohort in COHORTS} for path in STARTUP_PATHS
    }
    # The runner's own hand-off closure, so `on-persist` pays for the freeze and
    # for starting the send at the same point the runner does. The alternative
    # this replaces -- collecting every record and delivering the batch after the
    # cohort -- measured a startup that deferred its own delivery, which no
    # runner does.
    bench_job = ClaimedJob(
        job_id=session.job_id,
        thread_id=session.thread_id,
        attempt_id=session.attempt_id,
        attempt_no=1,
        repo=BENCH_REPO,
        base_sha=None,
        task_prompt="",
        runtime=BENCH_RUNTIME_ID,
        model="bench-model",
        worker_token=session.worker_token,
        sandbox_token="",
        execution_profile=session.execution_profile,
    )

    def handoff(_frozen: Any) -> None:
        runner_mod._enqueue_delivery(
            _frozen,
            job=bench_job,
            base_url=session.url,
            drain_s=runner_mod.DELIVERY_HANDOFF_DRAIN_S,
        )

    try:
        sandbox.up()
        for cohort in COHORTS:
            run_id = f"bench-startup-{cohort}-{uuid.uuid4().hex[:8]}".replace("_", "-")
            # Per cohort, not shared: a cohort that did not deliver must not
            # report the previous one's counts.
            delivery = {}
            sink_path = session.path(f"sink-startup-{cohort}.jsonl")
            budget = args.startup_iterations * len(STARTUP_PATHS) * 4 + RUN_BUDGET_SLACK
            _configure_profiling(
                cohort=cohort,
                run_id=run_id,
                sink_path=sink_path,
                budget=budget,
                operations=(STARTUP_OPERATION,),
                target={"job_id": session.job_id},
            )
            runner_mod.reset_delivery_state()
            on_frozen = handoff if cohort == "on-persist" else None
            #: Rows the control plane had accepted while this cohort was still
            #: activating. A delivery this script cannot serve is not a
            #: delivery, so the count is checked before the cohort ends rather
            #: than only after the join.
            received_during_cohort = 0
            for path in STARTUP_PATHS:
                print(
                    f"startup cohort {cohort}: {path} "
                    f"(warmup {args.startup_warmup}, timed {args.startup_iterations}) ..."
                )
                # The first activation of a path in this process pays for cold
                # state this script owns -- the Docker client, the phase
                # networks, the import of the supervisor path -- and charging
                # that to whichever cohort runs first would be a confound.
                for _ in range(args.startup_warmup):
                    await asyncio.to_thread(
                        _activate_path,
                        sandbox,
                        path,
                        cohort=cohort,
                        session=session,
                        iteration=0,
                        timed=False,
                    )
                for iteration in range(args.startup_iterations):
                    # In a worker thread: the control plane this script serves
                    # runs on this same event loop, so activating here would
                    # block the ingest endpoint the hand-off is posting to. The
                    # sends would queue up unserved until the cohort ended, and
                    # `on-persist` would be measuring a destination that is
                    # switched off for the length of the measurement.
                    (
                        elapsed,
                        container_action,
                        resident_action,
                        _measurement,
                    ) = await asyncio.to_thread(
                        _activate_path,
                        sandbox,
                        path,
                        cohort=cohort,
                        session=session,
                        on_frozen=on_frozen,
                        iteration=iteration,
                    )
                    classified = _classify(container_action, resident_action)
                    if classified != path:
                        raise SystemExit(
                            f"{path}: the real classifier reported {classified!r} for "
                            f"container_action={container_action!r}, "
                            f"resident_action={resident_action!r}; refusing to report a path "
                            "that was not produced"
                        )
                    samples[path][cohort].append(elapsed)
                    paths[path].container_actions[container_action] = (
                        paths[path].container_actions.get(container_action, 0) + 1
                    )
                    paths[path].resident_actions[resident_action] = (
                        paths[path].resident_actions.get(resident_action, 0) + 1
                    )
                    if cohort == "on-persist":
                        received_during_cohort = max(
                            received_during_cohort,
                            (await _postgres_measurement_footprint(session.pool, run_id))["rows"],
                        )
            if cohort == "on-persist":
                expected_so_far = args.startup_iterations * len(STARTUP_PATHS)
                if received_during_cohort == 0:
                    raise SystemExit(
                        "on-persist collected nothing while the cohort was activating: the "
                        "hand-off sends had no listener, so the startup measured a "
                        "persistence path that was not running"
                    )
                delivery = dict(delivery or {})
                delivery["rows_visible_before_cohort_end"] = received_during_cohort
                if received_during_cohort >= expected_so_far:
                    delivery["all_rows_visible_before_cohort_end"] = True
            operation_profiling.flush_sink()
            records = _read_sink_records(sink_path)
            recorded_counts: dict[str, int] = {}
            if cohort in {"on-local", "on-persist"}:
                expected = args.startup_iterations * len(STARTUP_PATHS)
                if len(records) != expected:
                    raise SystemExit(
                        f"{cohort}: the collector recorded {len(records)} startup samples and "
                        f"{expected} were measured; the run stopped selecting mid-cohort"
                    )
                for record in records:
                    recorded_path = _recorded_startup_path(record)
                    if recorded_path not in STARTUP_PATHS:
                        raise SystemExit(
                            f"{cohort}: the collector recorded {recorded_path!r}, which is not "
                            f"one of the measured paths {list(STARTUP_PATHS)}"
                        )
                    recorded_counts[recorded_path] = recorded_counts.get(recorded_path, 0) + 1
                short = [
                    name
                    for name in STARTUP_PATHS
                    if recorded_counts.get(name, 0) != args.startup_iterations
                ]
                if short:
                    raise SystemExit(
                        f"{cohort}: the collector did not record {short} the number of times "
                        f"they were measured ({args.startup_iterations} each)"
                    )
                if cohort == "on-persist":
                    # The sends were started at each handoff. Wait for the ones
                    # still in flight, then read the runner's own counters; the
                    # delivered/duplicate/dropped names are the runner's, so a
                    # shortfall here is the delivery runtime's answer and not
                    # this script's bookkeeping.
                    waited = await _await_handoff_deliveries(runner_mod, expected=expected)
                    delivery = {**(delivery or {}), **waited}
                    stored = await _postgres_measurement_footprint(session.pool, run_id)
                    wanted = dict.fromkeys(STARTUP_PATHS, args.startup_iterations)
                    if stored["rows"] != expected or stored["startup_paths"] != wanted:
                        raise SystemExit(
                            f"on-persist: {stored['rows']} startup rows reached Postgres "
                            f"({stored['startup_paths']}) and {expected} were measured ({wanted})"
                        )
                    if int(delivery.get("delivered", 0)) != expected:
                        raise SystemExit(
                            f"on-persist: the handoff delivery reported {delivery} for "
                            f"{expected} records"
                        )
            operation_profiling.close_sink()
            results[cohort] = {
                "run_id": run_id,
                "recorded_startup_paths": recorded_counts,
                "sink_records_written": len(records),
                "sink_bytes": sink_path.stat().st_size if sink_path.exists() else 0,
                "delivery": delivery if cohort == "on-persist" else None,
                "postgres": (
                    await _postgres_measurement_footprint(session.pool, run_id)
                    if cohort == "on-persist"
                    else None
                ),
            }
    finally:
        _clear_profiling_environment()
        sandbox.down()

    return {
        "docker": session.docker,
        "container": sandbox.container_name,
        "image": BENCH_IMAGE,
        "iterations_per_path": args.startup_iterations,
        "warmup_per_path": args.startup_warmup,
        "paths": {
            path: {
                "intended_startup_path": paths[path].intended,
                "container_action_counts": paths[path].container_actions,
                "resident_action_counts": paths[path].resident_actions,
                "cohorts": {
                    cohort: Sample(values).as_dict() for cohort, values in samples[path].items()
                },
                "deltas_vs_off": {
                    cohort: _delta(Sample(values), Sample(samples[path]["off"]))
                    for cohort, values in samples[path].items()
                    if cohort != "off"
                },
            }
            for path in STARTUP_PATHS
        },
        "cohorts": results,
        "recorded_startup_path_counts": {
            cohort: result["recorded_startup_paths"] for cohort, result in results.items()
        },
        "boundary": (
            "supervisor acquire + resident hand-off (claim -> prompt dispatch); the resident CLI "
            "is a stand-in transport, so no CLI process starts and the CLI's own initialization "
            "is outside these numbers. The container actions are real Docker."
        ),
        "percentile_note": (
            f"p95 over {args.startup_iterations} samples is the largest sample under the "
            "nearest-rank rule, not an estimate of a 95th percentile: read the p50 here, and "
            "raise --startup-iterations if a tail estimate is what is wanted. The deltas on "
            "new_group and resume_stopped are dominated by Docker create/start, which is "
            "hundreds of milliseconds against a collector cost of a fraction of one."
        ),
    }


def _recorded_startup_path(record: dict[str, Any]) -> str:
    """Return the startup path a frozen record carries.

    The collector keeps it among the measurement's facts, and the ingest reads
    it from either place; reading the facts first is how the sink is read.
    """
    facts = record.get("facts")
    if isinstance(facts, dict) and facts.get("startup_path"):
        return str(facts["startup_path"])
    return str(record.get("startup_path") or "")


async def _await_handoff_deliveries(runner_mod: Any, *, expected: int) -> dict[str, Any]:
    """Wait for the sends the handoff started, and report the runner's counts.

    The enqueue at the freeze point deliberately does not wait -- that is the
    runner's own ``DELIVERY_HANDOFF_DRAIN_S`` behaviour, and charging a
    synchronous send to the startup would measure something the runner does not
    do. So the cohort has to join those threads afterwards, off the event loop,
    before the destination's row count means anything.
    """

    def _join() -> dict[str, Any]:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            state = runner_mod._IN_FLIGHT_DELIVERIES
            if not state:
                break
            for thread in list(state.values()):
                thread.join(timeout=1.0)
        return {
            "delivered": len(runner_mod._DELIVERED_MEASUREMENT_IDS),
            "dropped": int(runner_mod._DELIVERY_DROPPED_COUNT),
            "drain_timeouts": int(runner_mod._DELIVERY_DRAIN_TIMEOUTS),
            "expected": expected,
        }

    return await asyncio.to_thread(_join)


def _classify(container_action: str, resident_action: str) -> str:
    """Call the real startup-path classifier."""
    from cloud_agent_host.runner import _classify_startup_path

    return _classify_startup_path(container_action, resident_action)


# --------------------------------------------------------------------------
# payload and report
# --------------------------------------------------------------------------


def build_payload(
    *,
    args: argparse.Namespace,
    config: dict[str, Any],
    environment: dict[str, Any],
    server: dict[str, Any] | None,
    arms: dict[str, Any],
    startup: dict[str, Any] | None,
    skipped: list[str],
) -> dict[str, Any]:
    """Assemble the machine-readable report, caveats included."""
    return {
        "what": (
            "the added latency the opt-in operation profiler costs real control-plane "
            "requests and real sandbox startup activations, per cohort and per operation"
        ),
        "not_what": (
            "end-to-end latency: the browser, the network, the host agent and the sandbox are "
            "outside every number here, and no agent CLI or model ran"
        ),
        "caveats": [
            "client and server share one process and one event loop over loopback, so no "
            "network round trip is in these numbers and every cohort pays the same HTTP costs",
            "the sandbox manager is an in-process stand-in speaking the real fragment protocol; no "
            "container work is behind a file or terminal request here",
            "the `bare` cohort for a server operation is an unmeasured sibling route on the "
            "same app, not the measured operation with its instrumentation removed; the "
            "control-plane routes always call the collector seam, and this script does not "
            "edit production code to remove it",
            "`on-local` for a server operation replaces the persistence destination with a "
            "bench-local JSONL file, because no deployment switch removes the Postgres "
            "repository from the control plane process. That stand-in appends through "
            "`asyncio.to_thread`, which contends with the event loop differently from the "
            "real asyncpg writer, so the few hundred microseconds by which `on-persist` "
            "sometimes beats `on-local` here is not a claim that persisting is cheaper than "
            "the local sink",
            "percentiles are nearest-rank over the sorted samples "
            "(index = min(n-1, max(0, round(f*(n-1))))); the Admin page computes "
            "percentile_cont in SQL, which interpolates, so a number here will not reproduce "
            "the page numerically",
            "per-cohort peak RSS is a process high-water mark and is reported as an absolute, "
            "not as a delta; CPU is the whole process because time.process_time() sums every "
            "thread, so the collector's exporter and the writer's tasks cannot be charged "
            "separately from the request path -- the delta against `off` is the extra process "
            "CPU the configuration cost, not a measurement of one named thread",
            "network bytes are not measured: there is no portable per-socket counter for the "
            "loopback HTTP this script uses, and the numbers that are reported for the "
            "destinations are payload bytes, not WAL, index or disk bytes",
            "the run descriptor's max_operations budget is per process; a deployment on more "
            "than one control-plane replica would record each replica's share instead",
            "the request rate here is one request every few milliseconds, sustained, which is "
            "far above a human browsing session's. Between operations a cohort waits for the "
            "bounded writer queue to empty, so a drop describes that cohort's destination "
            "rather than the benchmark's cumulative volume; the synthetic arms deliberately "
            "saturate the queue to show the designed drop behaviour",
            "the browser's X-Agent-Interaction-Id continuation is not sent, so the extra "
            "lookup a continued terminal request pays is not in these samples",
            "absolute latencies drift between runs with the machine's load: two consecutive "
            "runs of this script moved one operation's `off` p50 by about half a millisecond. "
            "Compare cohorts within one report, read the delta against the printed noise "
            "floor, and do not compare a number here with a number from another machine or "
            "another day as if it were the same measurement",
        ],
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": environment,
        "database": {
            "name": config["database"],
            "created_by_this_run": environment.get("database_created_by_this_run"),
            "test_database_guard": ALLOWED_DATABASE_PATTERN,
        },
        "cohorts": list(COHORTS),
        "baseline": "off",
        "sections_not_measured": skipped,
        "server_operations": server,
        "arms": arms,
        "startup": startup,
    }


def _environment_manifest(
    *,
    args: argparse.Namespace,
    config: dict[str, Any],
    postgres: dict[str, Any],
    docker: dict[str, Any],
    created_database: bool,
    cohort_order: list[str],
) -> dict[str, Any]:
    """Describe the machine and the fixed inputs, so the numbers can be compared."""
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "postgres": postgres,
        "database_host": f"{config['host']}:{config['port']}",
        "database_created_by_this_run": created_database,
        "docker": docker,
        "iterations": args.iterations,
        "warmup": args.warmup,
        "startup_iterations": args.startup_iterations,
        "startup_warmup": args.startup_warmup,
        "startup_requested": bool(args.startup),
        "rounds": args.rounds,
        "cohort_order": cohort_order,
        "clock": "time.perf_counter_ns",
        "percentile_algorithm": "nearest-rank",
        "budget_rule": f"max({BUDGET_FLOOR_MS:.0f} ms, off_p95 x {BUDGET_FRACTION:.0%})",
        "run_descriptor_budget_slack": RUN_BUDGET_SLACK,
        "slow_db_hold_s": args.slow_db_hold_s,
        "slow_sink_ms": args.slow_sink_ms,
    }


async def _postgres_manifest(pool: asyncpg.Pool) -> dict[str, Any]:
    """Return the server version and the settings that shape write latency."""
    async with pool.acquire() as connection:
        version = await connection.fetchval("SHOW server_version")
        settings = await connection.fetch(
            """
            SELECT name, setting FROM pg_settings
            WHERE name IN ('shared_buffers', 'synchronous_commit', 'fsync', 'max_connections',
                           'wal_writer_delay')
            ORDER BY name
            """
        )
    return {
        "server_version": str(version),
        "settings": {str(row["name"]): str(row["setting"]) for row in settings},
    }


def _print_server_table(payload: dict[str, Any]) -> list[str]:
    """Print the per-operation, per-cohort table and return it as markdown."""
    server = payload.get("server_operations")
    if not server:
        return []
    print("\nserver operations, milliseconds (baseline: `off`)")
    header = (
        f"{'operation':<22} {'cohort':<11} {'n':>5} {'p50':>8} {'p95':>8} {'mean':>8} "
        f"{'min':>8} {'max':>8} {'d p95':>9} {'d p95 %':>8} {'budget':>8}  verdict"
    )
    print(header)
    lines = [
        "| operation | cohort | n | p50 | p95 | mean | min | max | delta p95 | delta p95 % | "
        "budget | verdict |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for label, rows in server["operations"].items():
        first = True
        for cohort in COHORTS:
            row = rows["cohorts"].get(cohort)
            if row is None:
                continue
            delta = rows["deltas_vs_off"].get(cohort)
            verdict = rows["budget"].get(cohort)
            name = label if first else ""
            first = False
            p95_delta = f"{delta['p95_ms']:+.3f}" if delta else "-"
            p95_pct = f"{delta['p95_pct']:+.2f}" if delta else "-"
            budget = f"{verdict['budget_ms']:.2f}" if verdict else "-"
            outcome = verdict["verdict"] if verdict else "-"
            print(
                f"{name:<22} {cohort:<11} {row['n']:>5} {row['p50_ms']:>8.3f} {row['p95_ms']:>8.3f} "
                f"{row['mean_ms']:>8.3f} {row['min_ms']:>8.3f} {row['max_ms']:>8.3f} "
                f"{p95_delta:>9} {p95_pct:>8} {budget:>8}  {outcome}"
            )
            lines.append(
                f"| {name} | {cohort} | {row['n']} | {row['p50_ms']:.3f} | {row['p95_ms']:.3f} | "
                f"{row['mean_ms']:.3f} | {row['min_ms']:.3f} | {row['max_ms']:.3f} | "
                f"{p95_delta} | {p95_pct} | {budget} | {outcome} |"
            )
    print("\nnoise floor: the `off` cohort's even rounds against its odd rounds, where no")
    print("instrumentation difference exists; a delta smaller than this is not a result")
    print(f"{'operation':<22} {'abs d p50':>10} {'abs d p95':>10}   (even n / odd n)")
    lines.append("")
    lines.append("| operation | noise floor abs delta p50 | noise floor abs delta p95 |")
    lines.append("| --- | ---: | ---: |")
    for label, rows in server["operations"].items():
        floor = rows.get("noise_floor")
        if not floor:
            continue
        print(
            f"{label:<22} {floor['abs_delta_p50_ms']:>10.3f} {floor['abs_delta_p95_ms']:>10.3f}"
            f"   ({floor['even_n']} / {floor['odd_n']})"
        )
        lines.append(
            f"| {label} | {floor['abs_delta_p50_ms']:.3f} | {floor['abs_delta_p95_ms']:.3f} |"
        )

    print("\nper-round p50, milliseconds: a delta this small is only believable if it")
    print("repeats round to round (cohorts rotate position each round)")
    print(f"{'operation':<22} {'off':<22} {'on-local':<22} {'on-persist':<22}")
    lines.append("")
    lines.append(
        "| operation | off p50 per round | on-local p50 per round | on-persist p50 per round |"
    )
    lines.append("| --- | --- | --- | --- |")
    for label, rows in server["operations"].items():
        cells = {}
        for cohort in ("off", "on-local", "on-persist"):
            values = [row["p50_ms"] for row in rows["per_round"].get(cohort, [])]
            cells[cohort] = " ".join(f"{value:.2f}" for value in values) or "-"
        print(f"{label:<22} {cells['off']:<22} {cells['on-local']:<22} {cells['on-persist']:<22}")
        lines.append(f"| {label} | {cells['off']} | {cells['on-local']} | {cells['on-persist']} |")

    print("\nbare analogue, per operation family (an unmeasured sibling route, not this")
    print("operation with its instrumentation removed; a floor reference, never a baseline)")
    print(f"{'family':<11} {'route':<52} {'n':>5} {'p50':>8} {'p95':>8}")
    lines.append("")
    lines.append("| family | bare analogue route | n | p50 | p95 |")
    lines.append("| --- | --- | ---: | ---: | ---: |")
    seen: set[str] = set()
    for rows in server["operations"].values():
        family = rows["family"]
        if family in seen:
            continue
        seen.add(family)
        analogue = rows["bare_analogue"]
        print(
            f"{family:<11} {analogue['route']:<52} {analogue['n']:>5} {analogue['p50_ms']:>8.3f} "
            f"{analogue['p95_ms']:>8.3f}"
        )
        lines.append(
            f"| {family} | {analogue['route']} | {analogue['n']} | {analogue['p50_ms']:.3f} | "
            f"{analogue['p95_ms']:.3f} |"
        )
    return lines


def _print_startup_table(payload: dict[str, Any]) -> list[str]:
    """Print the startup-path table and return it as markdown."""
    startup = payload.get("startup")
    if not startup:
        return []
    print("\nstartup paths, milliseconds (real container, real classifier)")
    print(f"{'path':<16} {'cohort':<11} {'n':>5} {'p50':>9} {'p95':>9} {'d p50':>9} {'d p95':>9}")
    lines = [
        "| startup path | cohort | n | p50 | p95 | delta p50 | delta p95 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for path, rows in startup["paths"].items():
        first = True
        for cohort in COHORTS:
            row = rows["cohorts"][cohort]
            delta = rows["deltas_vs_off"].get(cohort)
            name = path if first else ""
            first = False
            p50_delta = f"{delta['p50_ms']:+.3f}" if delta else "-"
            p95_delta = f"{delta['p95_ms']:+.3f}" if delta else "-"
            print(
                f"{name:<16} {cohort:<11} {row['n']:>5} {row['p50_ms']:>9.3f} {row['p95_ms']:>9.3f} "
                f"{p50_delta:>9} {p95_delta:>9}"
            )
            lines.append(
                f"| {name} | {cohort} | {row['n']} | {row['p50_ms']:.3f} | {row['p95_ms']:.3f} | "
                f"{p50_delta} | {p95_delta} |"
            )
        container = rows["container_action_counts"]
        resident = rows["resident_action_counts"]
        recorded = startup["recorded_startup_path_counts"]
        print(f"  real actions: container={container} resident={resident}   recorded: {recorded}")
        lines.append(f"| {path} actions | container={container} resident={resident} | | | | | |")
    print(f"note: {startup['percentile_note']}")
    lines.append("")
    lines.append(f"Note: {startup['percentile_note']}")
    for cohort, row in startup["cohorts"].items():
        if cohort not in {"on-local", "on-persist"}:
            continue
        detail = f"{cohort}: {row['sink_records_written']} sink records, {row['sink_bytes']} bytes"
        if row.get("delivery") is not None:
            detail += f", delivery {row['delivery']}"
        if row.get("postgres") is not None:
            detail += (
                f", postgres rows {row['postgres']['rows']} "
                f"({row['postgres']['record_bytes']} record bytes)"
            )
        print(f"  {detail}")
        lines.append(f"- {detail}")
    return lines


def _print_drops(payload: dict[str, Any]) -> list[str]:
    """Print the drop counters and the background cost each cohort paid.

    Returns:
        The same two tables as markdown.
    """
    server = payload.get("server_operations")
    lines: list[str] = []
    if not server:
        return lines
    print("\ndrop counts and local sink, per cohort")
    print(
        f"{'cohort':<11} {'sink':>12} {'writer stored':>14} {'dropped_full':>13} "
        f"{'dropped_timeout':>16} {'rejected':>9} {'failed':>7}  destination"
    )
    lines.append("")
    lines.append("## Drops and destinations")
    lines.append("")
    lines.append(
        "| cohort | sink written/expected | writer stored | dropped_full | dropped_timeout | "
        "rejected | failed | destination |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    for cohort, row in server["cohorts"].items():
        drops = row["drops"]
        writer = drops["writer"]
        sink = f"{drops['sink_records_written']}/{drops['sink_records_expected']}"
        print(
            f"{cohort:<11} {sink:>12} "
            f"{writer['stored']:>14} {writer['dropped_full']:>13} {writer['dropped_timeout']:>16} "
            f"{writer['rejected']:>9} {writer['failed']:>7}  {drops['destination']}"
        )
        lines.append(
            f"| {cohort} | {sink} | {writer['stored']} | {writer['dropped_full']} | "
            f"{writer['dropped_timeout']} | {writer['rejected']} | {writer['failed']} | "
            f"{drops['destination']} |"
        )
    print(
        f"{'cohort':<11} {'process cpu s':>14} {'vs off s':>10} {'per record us':>14} "
        f"{'peak rss MiB':>13} {'rounds':>7}"
    )
    lines.append("")
    lines.append("## Process CPU at equal work")
    lines.append("")
    lines.append("| cohort | rounds | process CPU s | vs `off` s | per record µs | peak RSS MiB |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    off_cpu = server["cohorts"].get("off", {}).get("background", {}).get("process_cpu_s")
    records = server.get("requests_per_cohort") or 0
    print(
        "\nprocess CPU each cohort spent on the same requests, whole process, all threads"
        f" ({records} requests per cohort: warmup and timed together)"
    )
    for cohort, row in server["cohorts"].items():
        cost = row["background"]
        cpu = float(cost["process_cpu_s"])
        comparable = off_cpu is not None and cohort not in {"bare", "off"}
        delta = f"{cpu - float(off_cpu):+.3f}" if comparable else "—"
        # `bare` is excluded from the comparison and says so: it sends two extra
        # sibling requests per round, so its CPU is not the same work. `off` is
        # the baseline itself, so its delta is nothing rather than zero.
        per_record = (
            f"{(cpu - float(off_cpu)) / records * 1e6:+.1f}" if comparable and records else "n/a"
        )
        print(
            f"{cohort:<11} {cpu:>14.3f} {delta:>10} {per_record:>14} "
            f"{cost['peak_rss_mb_after']:>13.1f} {cost.get('rounds', 1):>7}"
        )
        lines.append(
            f"| {cohort} | {cost.get('rounds', 1)} | {cpu:.3f} | {delta} | "
            f"{per_record} | {cost['peak_rss_mb_after']:.1f} |"
        )
    print(
        "  (time.process_time() sums every thread, so this is the whole process -- request path, "
        "collector exporter and control-plane writer together. Per-thread attribution is not "
        "available on this platform, so the delta against off is the configuration's extra "
        "process CPU and not a measurement of one named background thread. The per-record "
        "column divides by every request the CPU window contained -- warmup included, because "
        "warmup ran inside it. `bare` sends two extra sibling requests per round and is "
        "therefore not comparable.)"
    )
    lines.append("")
    lines.append(
        "One number per cohort, and it is the whole process: `time.process_time()` sums every "
        "thread, so the collector's exporter and the writer's tasks cannot be charged separately "
        "from the request path here. The delta against `off` is the configuration's extra process "
        "CPU on the same request set; because the client and server share this process, part of "
        "it is the load generator's own extra work. The per-record figure divides by the whole "
        "window's request count (warmup plus timed), which is what the CPU was spent on. `bare` "
        "is not comparable: it sends two extra sibling requests per round."
    )
    lines.append("")
    lines.append(
        "Not measured: network bytes (no portable per-socket counter for loopback HTTP); "
        "per-thread CPU (`resource.RUSAGE_THREAD` is unavailable here); WAL/index/disk bytes "
        "(only payload bytes are reported); container CPU and memory (no container is involved "
        "in a server-request cohort)."
    )
    return lines


def report(payload: dict[str, Any]) -> list[str]:
    """Print the tables a human reads, and return the markdown form."""
    environment = payload["environment"]
    print(
        f"\noperation-profiling business overhead   python {environment['python']}   "
        f"{environment['platform']}   cpus {environment['cpu_count']}"
    )
    print(
        f"postgres {environment['postgres']['server_version']}   "
        f"docker {'yes' if environment['docker'].get('available') else 'no'}   "
        f"iterations {environment['iterations']} (warmup {environment['warmup']})   "
        f"sections measured: {', '.join(environment['cohort_order'])}"
    )
    print(
        f"percentiles: nearest-rank   budget: {environment['budget_rule']}   "
        f"rounds: {environment['rounds']}   database: {payload['database']['name']}"
    )
    if payload.get("server_operations"):
        print(
            "cohort order per round: "
            + "; ".join(
                f"r{index + 1}=" + ",".join(order)
                for index, order in enumerate(payload["server_operations"]["cohort_order_by_round"])
            )
        )
    markdown: list[str] = ["# Operation-profiling business overhead", ""]
    markdown.append(
        f"- python {environment['python']} on {environment['platform']}, cpus "
        f"{environment['cpu_count']}"
    )
    markdown.append(
        f"- postgres {environment['postgres']['server_version']}, docker "
        f"{'used' if environment['docker'].get('available') else 'unavailable'}, "
        f"iterations {environment['iterations']} (warmup {environment['warmup']})"
    )
    markdown.append(
        f"- percentiles: nearest-rank; budget: {environment['budget_rule']}; baseline: `off`; "
        f"rounds: {environment['rounds']}"
    )
    markdown.append("")
    markdown += _print_server_table(payload)
    markdown += _print_startup_table(payload)
    markdown += _print_drops(payload)

    arms = payload.get("arms") or {}
    if arms:
        markdown.append("")
        markdown.append("## Synthetic arms")
    for name, arm in arms.items():
        if not arm:
            continue
        print(f"\n{name}: {arm['what_was_slowed']}")
        markdown.append("")
        markdown.append(f"### {name}")
        markdown.append("")
        markdown.append(f"Synthetic: {arm['what_was_slowed']}")
        if "samples" in arm:
            print(f"{'operation':<22} {'n':>5} {'p50':>9} {'p95':>9} {'max':>9}")
            markdown.append("")
            markdown.append("| operation | n | p50 | p95 | max |")
            markdown.append("| --- | ---: | ---: | ---: | ---: |")
            for label, row in arm["samples"].items():
                print(
                    f"{label:<22} {row['n']:>5} {row['p50_ms']:>9.3f} {row['p95_ms']:>9.3f} "
                    f"{row['max_ms']:>9.3f}"
                )
                markdown.append(
                    f"| {label} | {row['n']} | {row['p50_ms']:.3f} | {row['p95_ms']:.3f} | "
                    f"{row['max_ms']:.3f} |"
                )
        if name == "slow_sink":
            drops = arm["drops"]
            destination = drops.get("local_destination") or {}
            detail = (
                f"  writer: {drops['writer']}   local destination: "
                f"{destination.get('records_written', 0)}/{destination.get('records_expected', 0)} "
                f"written, {destination.get('bytes', 0)} bytes"
            )
            print(detail)
            markdown.append("")
            markdown.append(f"- writer counts: `{drops['writer']}`")
            markdown.append(
                f"- local destination: {destination.get('records_written', 0)}/"
                f"{destination.get('records_expected', 0)} records written, "
                f"{destination.get('bytes', 0)} bytes"
            )
        if name == "slow_db":
            markdown.append("")
            markdown.append(
                f"- requests answered while the table was locked: {arm['requests_answered']}"
            )
            markdown.append(f"- writer during the lock: `{arm['writer_during_lock']}`")
            markdown.append(f"- writer at the end of the hold: `{arm['writer_at_end_of_hold']}`")
            markdown.append(
                f"- after release: `{arm['writer_after_release']}`, recovered after "
                f"{arm['recovered_after_s']}s (budget {arm['recovery_budget_s']}s)"
            )
            markdown.append(
                f"- Postgres rows for the arm: {arm['postgres']['rows']} "
                f"({arm['postgres']['record_bytes']} payload bytes)"
            )
            print(
                f"  requests answered: {arm['requests_answered']}   writer during lock: "
                f"{arm['writer_during_lock']}   queue during lock: "
                f"pending={arm['writer_queue_during_lock']['pending']} "
                f"in_flight={arm['writer_queue_during_lock']['in_flight']}"
            )
            print(
                f"  at end of hold: {arm['writer_at_end_of_hold']}   queue "
                f"pending={arm['writer_queue_at_end_of_hold']['pending']} "
                f"in_flight={arm['writer_queue_at_end_of_hold']['in_flight']}"
            )
            print(
                f"  after release: {arm['writer_after_release']}   recovered after "
                f"{arm['recovered_after_s']}s (budget {arm['recovery_budget_s']}s)   "
                f"postgres rows {arm['postgres']['rows']}, record bytes "
                f"{arm['postgres']['record_bytes']}"
            )
    if payload.get("sections_not_measured"):
        print("\nnot measured: " + "; ".join(payload["sections_not_measured"]))
    print(
        "\nWhat this measures: real control-plane HTTP requests and real sandbox activations, "
        "per cohort.\nWhat it does not: end-to-end latency, the browser, the network, a real "
        "sandbox manager container,\nor any agent CLI. `bare` for a server operation is an unmeasured "
        "sibling route, not the\nsame operation with the collector removed.\n"
        "Percentiles are nearest-rank; the Admin page's SQL uses percentile_cont, which "
        "interpolates."
    )
    return markdown


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description=__doc__ or "", formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=150,
        help=(
            "timed requests per operation per cohort per round; the pooled sample is this "
            "times --rounds (default: 150)"
        ),
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=20,
        help="untimed requests before each operation's timed run (default: 20)",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=4,
        help=(
            "how many times each server-request cohort runs, with the cohort order rotated "
            "each round so no cohort always pays for a cold or a hot process; samples are "
            "pooled across rounds and also reported per round (default: 4)"
        ),
    )
    parser.add_argument(
        "--startup",
        action="store_true",
        help=(
            "also measure the four startup paths on a real retained container; needs a "
            "working Docker daemon, and without one the script exits non-zero"
        ),
    )
    parser.add_argument(
        "--skip-server-ops",
        action="store_true",
        help="skip the server-request section (useful with --startup and a small run)",
    )
    parser.add_argument(
        "--startup-iterations",
        type=int,
        default=6,
        help=(
            "timed activations per startup path per cohort (default: 6; at this sample size "
            "the p95 is the largest sample under nearest-rank, so read the p50)"
        ),
    )
    parser.add_argument(
        "--startup-warmup",
        type=int,
        default=1,
        help=(
            "untimed activations per startup path per cohort, to absorb the cold state the "
            "first activation of a path pays for (default: 1)"
        ),
    )
    parser.add_argument(
        "--no-slow-arms",
        action="store_true",
        help="skip the synthetic slow-database and slow-destination arms",
    )
    parser.add_argument(
        "--slow-db-hold-s",
        type=float,
        default=12.0,
        help=(
            "how long the synthetic ACCESS EXCLUSIVE lock on the measurements table is held "
            "while on-persist requests are measured; longer than the writer's 10 s write "
            "timeout exercises the timeout drop path (default: 12.0)"
        ),
    )
    parser.add_argument(
        "--slow-hold-recovery-s",
        type=float,
        default=20.0,
        help="how long the slow-database arm waits for the queue to recover (default: 20.0)",
    )
    parser.add_argument(
        "--slow-sink-ms",
        type=float,
        default=20.0,
        help=(
            "per-record delay injected into the bench-local on-local destination for the "
            "slow-destination arm; high enough at this request rate that the bounded queue "
            "must drop, which is the behaviour that arm exists to show (default: 20.0)"
        ),
    )
    parser.add_argument(
        "--drain-timeout-s",
        type=float,
        default=15.0,
        help=(
            "how long a cohort waits for the measurement writer queue to empty between "
            "operations (default: 15.0)"
        ),
    )
    parser.add_argument(
        "--slow-iterations",
        type=int,
        default=60,
        help="requests per operation in the slow-database arm (default: 60)",
    )
    parser.add_argument(
        "--build-image",
        action="store_true",
        help="rebuild the guest image the startup container runs before measuring",
    )
    parser.add_argument("--json", metavar="PATH", help="write the full report as JSON")
    parser.add_argument("--markdown", metavar="PATH", help="write the report tables as markdown")
    parser.add_argument("--database", default=DEFAULT_DATABASE, help="test database name")
    parser.add_argument("--db-host", default=os.environ.get("TEST_DB_HOST", "localhost"))
    parser.add_argument("--db-port", type=int, default=int(os.environ.get("TEST_DB_PORT", "5432")))
    parser.add_argument("--db-user", default=os.environ.get("TEST_DB_USER", "postgres"))
    parser.add_argument("--db-password", default=os.environ.get("TEST_DB_PASSWORD", "postgres"))
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    """Run the requested sections and write the report."""
    config = _db_config(args)
    docker = docker_available()
    if args.startup and not docker["available"]:
        print(
            "SAFETY: --startup was requested but no working Docker daemon is available "
            f"({docker['reason']}); no startup path was measured: " + ", ".join(STARTUP_PATHS),
            file=sys.stderr,
        )
        return 2
    if args.startup and (args.build_image or not _image_present()):
        print(f"building the benchmark guest image {BENCH_IMAGE} ...")
        _build_bench_image()

    cohort_order: list[str] = []
    if not args.skip_server_ops:
        cohort_order += list(COHORTS) + ([] if args.no_slow_arms else ["slow-sink", "slow-db"])
    if args.startup:
        cohort_order += [f"startup:{cohort}" for cohort in COHORTS]

    session = await _open_session(config, args)
    _quiet_logging()
    session.docker = docker
    postgres = await _postgres_manifest(session.pool)
    server_section: dict[str, Any] | None = None
    arms: dict[str, Any] = {}
    startup_section: dict[str, Any] | None = None
    skipped: list[str] = []
    try:
        if not args.skip_server_ops:
            server_section, arms = await _run_server_section(session, args)
        else:
            skipped.append("server-request operations (--skip-server-ops)")
        if args.startup:
            startup_section = await _run_startup_section(session, args)
        else:
            skipped.append(
                "--startup was not requested, so no startup path was measured: "
                + ", ".join(STARTUP_PATHS)
            )
    finally:
        _clear_profiling_environment()
        await _close_session(session)

    environment = _environment_manifest(
        args=args,
        config=config,
        postgres=postgres,
        docker=docker,
        created_database=session.created_database,
        cohort_order=cohort_order,
    )
    payload = build_payload(
        args=args,
        config=config,
        environment=environment,
        server=server_section,
        arms=arms,
        startup=startup_section,
        skipped=skipped,
    )
    markdown = report(payload)
    if args.json:
        destination = Path(args.json)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"\nwrote {destination}")
    if args.markdown:
        destination = Path(args.markdown)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("\n".join(markdown) + "\n", encoding="utf-8")
        print(f"wrote {destination}")
    return 0


def _image_present() -> bool:
    """Whether the benchmark guest image already exists locally."""
    result = _docker("image", "inspect", BENCH_IMAGE, check=False)
    return result.returncode == 0


async def _global_warmup(session: BenchSession, args: argparse.Namespace) -> None:
    """Warm the process before the first measured cohort.

    The app's background workers, the connection pool and the HTTP client are
    all cold on the first request, and charging that to whichever cohort runs
    first would be a confound rather than a result. This pass is untimed and
    runs with the profiler off.
    """
    _configure_profiling(
        cohort="off",
        run_id="bench-warmup",
        sink_path=session.path("sink-warmup.jsonl"),
        budget=1,
        operations=SERVER_RUN_OPERATIONS,
        target={"job_id": session.job_id},
    )
    original_sandbox_manager = sandbox_manager_routing._client_for
    install_sandbox_manager(session.sandbox_manager)
    _install_storage_override(session, session.storage)
    try:
        for label, _description in SERVER_OPERATIONS:
            for _ in range(args.warmup):
                _require_ok(await _send_operation(session.client, session.job_id, label), label)
        for family in BARE_ANALOGUES:
            for _ in range(args.warmup):
                _require_ok(
                    await _send_bare_analogue(session.client, session.job_id, family),
                    f"bare:{family}",
                )
    finally:
        uninstall_sandbox_manager(original_sandbox_manager)
    print(f"warmed up with {args.warmup} requests per operation")


async def _run_server_section(
    session: BenchSession, args: argparse.Namespace
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run every server-request cohort and evaluate the budget per operation.

    Cohorts run in rounds, and the order rotates each round, so a cohort is not
    always the one paying for a cold process or a hot one. Samples are pooled
    across rounds for the table and kept per round as well, because a delta this
    size is only believable if it is stable round to round.
    """
    rounds = max(1, args.rounds)
    await _global_warmup(session, args)
    pooled: dict[str, dict[str, list[int]]] = {cohort: {} for cohort in COHORTS}
    per_round: dict[str, list[dict[str, Any]]] = {cohort: [] for cohort in COHORTS}
    order_log: list[list[str]] = []
    for round_index in range(rounds):
        order = [COHORTS[(index + round_index) % len(COHORTS)] for index in range(len(COHORTS))]
        order_log.append(order)
        for cohort in order:
            print(f"round {round_index + 1}/{rounds}: server cohort {cohort} ...")
            result = await _run_server_cohort(
                session, cohort=cohort, args=args, round_index=round_index
            )
            for label, values in result.samples.items():
                pooled[cohort].setdefault(label, []).extend(values)
            per_round[cohort].append(
                {
                    "round": round_index,
                    "drops": result.drops,
                    "background": result.background,
                    "samples": {
                        label: Sample(values).as_dict()
                        for label, values in result.samples.items()
                        if values
                    },
                    # Kept in memory for the noise floor; stripped before the
                    # payload, where the pooled raw samples are the ones stored.
                    "raw": {label: values for label, values in result.samples.items() if values},
                }
            )
    operations: dict[str, Any] = {}
    for label, description in SERVER_OPERATIONS:
        cohorts = {cohort: pooled[cohort][label] for cohort in COHORTS if pooled[cohort].get(label)}
        off = Sample(cohorts["off"])
        family = OPERATION_FAMILIES[label]
        bare_values = pooled["bare"].get(f"bare:{family}", [])
        operations[label] = {
            "description": description,
            "family": family,
            "cohorts": {cohort: Sample(values).as_dict() for cohort, values in cohorts.items()},
            "per_round": {
                cohort: [
                    round_row["samples"][label]
                    for round_row in per_round[cohort]
                    if label in round_row["samples"]
                ]
                for cohort in COHORTS
            },
            "per_round_p50_spread_ms": {
                cohort: round(
                    max(row["p50_ms"] for row in rows) - min(row["p50_ms"] for row in rows),
                    3,
                )
                for cohort, rows in (
                    (
                        cohort,
                        [
                            round_row["samples"][label]
                            for round_row in per_round[cohort]
                            if label in round_row["samples"]
                        ],
                    )
                    for cohort in COHORTS
                )
                if rows
            },
            "deltas_vs_off": {
                cohort: _delta(Sample(values), off)
                for cohort, values in cohorts.items()
                if cohort in {"on-local", "on-persist"}
            },
            "budget": {
                cohort: _budget_verdict(off, Sample(values))
                for cohort, values in cohorts.items()
                if cohort in {"on-local", "on-persist"}
            },
            "bare_analogue": {
                "route": BARE_ANALOGUES[family],
                "note": (
                    "an unmeasured sibling route on the same app: same authentication, "
                    "ownership, entitlement and sandbox manager chain, no instrumentation call "
                    "anywhere. It is a floor reference, not this operation measured bare."
                ),
                **(Sample(bare_values).as_dict() if bare_values else {}),
            },
            "raw_samples_ns": dict(cohorts),
            "raw_bare_samples_ns": bare_values,
            "noise_floor": _noise_floor(per_round["off"], label),
        }
    cohort_blocks: dict[str, Any] = {}
    for cohort in COHORTS:
        rows = per_round[cohort]
        sink_expected = sum(int(row["drops"]["sink_records_expected"]) for row in rows)
        sink_written = sum(int(row["drops"]["sink_records_written"]) for row in rows)
        sink_bytes = sum(int(row["drops"]["sink_bytes"]) for row in rows)
        merged: dict[str, Any] = {
            "rounds": len(rows),
            "drain_waits_s": [row["drops"]["drain_waits_s"] for row in rows],
            "writer": _merge_writer_counts([row["drops"]["writer"] for row in rows]),
            "writer_pending_at_end": max(
                int(row["drops"]["writer_pending_at_end"]) for row in rows
            ),
            "writer_in_flight_at_end": max(
                int(row["drops"]["writer_in_flight_at_end"]) for row in rows
            ),
            "sink_records_expected": sink_expected,
            "sink_records_written": sink_written,
            "sink_records_dropped": max(0, sink_expected - sink_written),
            "sink_bytes": sink_bytes,
            "destination": rows[-1]["drops"]["destination"],
            "per_round": [
                {
                    "round": row["round"],
                    "writer": row["drops"]["writer"],
                    "sink_records_written": row["drops"]["sink_records_written"],
                    "sink_records_expected": row["drops"]["sink_records_expected"],
                    "local_destination": row["drops"].get("local_destination"),
                    "postgres": row["drops"].get("postgres"),
                }
                for row in rows
            ],
        }
        locals_seen = [
            row["drops"]["local_destination"]
            for row in rows
            if row["drops"].get("local_destination")
        ]
        if locals_seen:
            merged["local_destination"] = {
                "records_expected": sum(int(item["records_expected"]) for item in locals_seen),
                "records_written": sum(int(item["records_written"]) for item in locals_seen),
                "bytes": sum(int(item["bytes"]) for item in locals_seen),
                "synthetic_per_record_delay_ms": locals_seen[-1]["synthetic_per_record_delay_ms"],
            }
        posts_seen = [row["drops"]["postgres"] for row in rows if row["drops"].get("postgres")]
        if posts_seen:
            merged["postgres"] = {
                "rows": sum(int(item["rows"]) for item in posts_seen),
                "record_bytes": sum(int(item["record_bytes"]) for item in posts_seen),
                "jsonb_bytes": sum(int(item["jsonb_bytes"]) for item in posts_seen),
                "table_total_bytes": posts_seen[-1]["table_total_bytes"],
                "startup_paths": {},
            }
        cohort_blocks[cohort] = {
            "drops": merged,
            "background": _merge_background([row["background"] for row in rows]),
            "rounds_detail": [
                {key: value for key, value in row.items() if key != "raw"} for row in rows
            ],
        }
    arms: dict[str, Any] = {}
    if not args.no_slow_arms:
        print("running synthetic slow-destination arm (on-local) ...")
        arms["slow_sink"] = await _run_slow_sink_arm(session, args)
        print(
            f"running synthetic slow-database arm (ACCESS EXCLUSIVE lock for "
            f"{args.slow_db_hold_s:.0f}s) ..."
        )
        arms["slow_db"] = await _run_slow_database_arm(session, args)
    return (
        {
            "rounds": rounds,
            "cohort_order_by_round": order_log,
            "operations": operations,
            "cohorts": cohort_blocks,
            "requests_per_cohort": _requests_in_window(args, rounds=rounds),
        },
        arms,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark and leave without waiting on the control plane's threads."""
    args = parse_args(argv)
    if args.iterations < 1 or args.warmup < 0 or args.startup_iterations < 1:
        print("iterations must be positive and warmup must not be negative", file=sys.stderr)
        return 2
    if args.startup_warmup < 0 or args.drain_timeout_s < 0:
        print("warmup counts and drain timeouts must not be negative", file=sys.stderr)
        return 2
    if args.slow_iterations < 1:
        print("--slow-iterations must be positive", file=sys.stderr)
        return 2
    if args.rounds < 1:
        print("--rounds must be positive", file=sys.stderr)
        return 2
    try:
        code = asyncio.run(_run(args))
    except SystemExit as exc:
        # The safety refusals and the "this cohort measured the wrong thing"
        # guards leave by raising: their sentence is the whole message, and a
        # traceback around it only buries it.
        if exc.code is not None and not isinstance(exc.code, int):
            print(exc.code, file=sys.stderr)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(exc.code if isinstance(exc.code, int) else 1)
    except BaseException:
        # Same exit rule on the failure path: the traceback is what a reader
        # needs, and waiting for the control plane's non-daemon threads is not.
        import traceback

        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
    # The in-process control plane leaves non-daemon worker threads behind, and
    # the event loop's default executor joins them on the way out; the report is
    # written and flushed by now, so leave rather than block the terminal.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    main()
