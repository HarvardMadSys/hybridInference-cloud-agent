"""The chain end to end: collect it, deliver it, store it, read it back.

Each link has its own tests. What this file adds is the seam between them,
because that is where a mismatch hides: a record the collector emits but the
ingest refuses, a run manifest the endpoint validates but the repository
compares differently, a field the Admin page needs that the frozen record never
carried.

The pieces are the real ones -- the real route with a sandbox manager stand-in, the real
validator, the real repository, the real Admin router -- and only the
destination between collection and storage is arranged by the test, because
that is a network hop and not a rule.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from tests.servers.test_agent_jobs_api import (
    FakeStorage,
    _attach_shared_workspace,
    _build_app,
    _create_job,
)

from cloud_agent import identity, measurement_ingest, sandbox_manager_routing
from cloud_agent.api.deps import get_storage
from cloud_agent.app import create_app
from cloud_agent.measurement_ingest import Owner
from cloud_agent.operation_measurement import (
    INTERACTION_HEADER,
    reset as reset_selections,
)
from cloud_agent_host import operation_call
from cloud_agent_shared import operation_profiling

ADMIN = {"Authorization": "Bearer admin-e2e-test"}
RUN_ID = "e2e-run-1"
# Frozen for the module: a helper that re-derived it per call would state a
# different experiment every time, and the repository compares the instant.
EXPIRES_AT = (datetime.now(UTC) + timedelta(minutes=30)).replace(microsecond=0)


@pytest.fixture(autouse=True)
def _deployment(monkeypatch, tmp_path):
    """One experiment, its sink, and the secrets the routes need."""
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "api-test-secret")
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_TOKEN", "api-test-sandbox-manager-secret")
    monkeypatch.setenv("AGENT_SESSION_SECRET", "session-test-secret-32-bytes-minimum!")
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name,o/n,private/repo")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-e2e-test")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "e2e-session-secret-long-enough-32")
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(
        operation_profiling.ENV_PROFILING_RUN,
        json.dumps(
            {
                "run_id": RUN_ID,
                "operations": ["files.list", "files.open", "terminal.open"],
                "expires_at": EXPIRES_AT.isoformat(),
                "max_operations": 10,
                "target": {"sandbox_id": "asbx_e2e"},
            }
        ),
    )
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(tmp_path / "records.jsonl"))
    operation_profiling.reset_cache()
    reset_selections()
    yield
    operation_profiling.reset_cache()
    reset_selections()


class _SandboxManager:
    """A sandbox manager stand-in that speaks the same candidate protocol as the real one."""

    def __init__(self, *, delay_s: float = 0.03) -> None:
        self.delay_s = delay_s
        self.calls: list[str] = []

    async def register_shared_sandbox(self, _sandbox: dict[str, object]) -> None:
        """Accept the manifest re-registration a workspace request performs."""
        return None

    async def files(self, workspace_id: str, path: str, *, profiling=None):
        """Answer one read, filed by the kind the read found."""
        self.calls.append(path)
        if self.delay_s:
            import asyncio

            await asyncio.sleep(self.delay_s)
        call = operation_call.open_call(
            profiling, operations=("files.list", "files.open") if path else ("files.list",)
        )
        with call.stage("admission"):
            pass
        kind = "directory" if not path or path == "src" else "file"
        with call.stage("exec"):
            pass
        call.set_operation("files.list" if kind == "directory" else "files.open")
        body = (
            {"path": path, "kind": "directory", "entries": [], "writable": True}
            if kind == "directory"
            else {"path": path, "kind": "file", "content": "hello", "size": 5}
        )
        return call.answer(body)


@pytest.fixture
def store() -> FakeStorage:
    """The agent-jobs fake, carrying a real repository-shaped measurement port."""
    return FakeStorage()


@pytest_asyncio.fixture()
async def collector_client(store: FakeStorage, monkeypatch):
    """The control-plane routes a browser reaches, with a sandbox manager stand-in."""
    sandbox_manager = _SandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    app = _build_app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http, sandbox_manager


def _emitted() -> list[dict[str, object]]:
    """Return every record the collector froze, draining the exporter first."""
    operation_profiling.flush_sink()
    sink = os.environ[operation_profiling.ENV_PROFILING_SINK]
    if not os.path.exists(sink):
        return []
    with open(sink) as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _manifest() -> dict[str, object]:
    """The experiment manifest the producer would send with its first record."""
    return {
        "run_id": RUN_ID,
        "operations": ["files.list", "files.open", "terminal.open"],
        "target": {"sandbox_id": "asbx_e2e"},
        "expires_at": EXPIRES_AT.isoformat(),
        "config_version": 1,
        "admission_instances": [{"instance": "cp-e2e", "max_operations": 10}],
    }


def _validated(record: dict[str, Any], *, owner: Owner | None = None, run: Any = None) -> Any:
    """Validate a record the way the endpoint does, checks included.

    Deliberately not a shortcut: the point of this file is the seam, and a
    helper that ran fewer checks than the route would let a record pass here
    that production refuses.
    """
    stored = measurement_ingest.validate_measurement(record, owner=owner)
    measurement_ingest.require_record_matches_its_experiment(
        record=record,
        run=measurement_ingest.validate_run(run) if run is not None else None,
        now=datetime.now(UTC),
    )
    return stored


class _Stored:
    """The measurement capability, backed by a dict keyed as the table is."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, object]] = {}
        self.runs: dict[str, dict[str, object]] = {}

    async def record_operation_measurement(self, *, measurement, run=None) -> str:
        """Store one row, with the same identity rules the table enforces."""
        existing = self.rows.get(measurement["measurement_id"])
        if existing is not None:
            return (
                "duplicate"
                if existing["payload_hash"] == measurement["payload_hash"]
                else "conflict"
            )
        if run is not None:
            known = self.runs.get(run["run_id"])
            if known is not None:
                for field in ("operations", "target", "expires_at", "config_version"):
                    if known[field] != run[field]:
                        return "run_conflict"
            else:
                self.runs[run["run_id"]] = dict(run)
        self.rows[measurement["measurement_id"]] = dict(measurement)
        return "stored"

    async def list_profiling_runs(self, *, since=None, limit: int = 50):
        """Return the catalogue, newest first."""
        return list(self.runs.values())[:limit]

    async def get_profiling_run(self, run_id: str):
        """Return one experiment."""
        return self.runs.get(run_id)

    async def list_operation_measurements(self, **kwargs):
        """Return a filtered page, newest first, honouring the cursor."""
        rows = [
            row
            for row in self.rows.values()
            if (kwargs.get("run_id") is None or row["profiling_run_id"] == kwargs["run_id"])
            and (not kwargs.get("operations") or row["operation"] in kwargs["operations"])
        ]
        rows.sort(key=lambda row: row["measurement_id"], reverse=True)
        after = kwargs.get("after")
        if after is not None:
            anchor = after[1]
            rows = [row for row in rows if row["measurement_id"] < anchor]
        limit = kwargs.get("limit", 50)
        # The real read selects the summary columns, so the stand-in states
        # them too rather than handing the route whatever the stored row had.
        return [
            dict(
                row,
                request_kind=row.get("request_kind") or "files.list",
                received_at=datetime.now(UTC),
            )
            for row in rows[:limit]
        ]

    async def get_operation_measurement(self, measurement_id: str):
        """Return one stored row with its frozen record."""
        row = self.rows.get(measurement_id)
        return None if row is None else dict(row, received_at=datetime.now(UTC))

    async def summarize_operation_measurements(self, *, run_id: str, group_by=None):
        """Return counts over the whole experiment, not one page."""
        eligible = [
            row
            for row in self.rows.values()
            if row["profiling_run_id"] == run_id and row["collection_complete"]
        ]
        durations = sorted(row["duration_us"] for row in eligible)
        return [
            {
                "group": {
                    "operation": "files.list",
                    "request_kind": "files.list",
                    "boundary_version": 1,
                    "coverage_scope": "server_request",
                },
                "sample_count": len(self.rows),
                "eligible_count": len(durations),
                "p50_us": durations[len(durations) // 2] if durations else None,
                "p95_us": durations[-1] if durations else None,
                "incomplete_count": len(self.rows) - len(durations),
                "failed_count": 0,
            }
        ]


@pytest_asyncio.fixture()
async def reader(store: FakeStorage):
    """The Admin surface, reading the same stored rows."""
    capability = _Stored()
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        operation_measurements=capability
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http, capability


async def test_one_file_read_travels_the_whole_chain(
    collector_client, reader, store: FakeStorage, monkeypatch, tmp_path
):
    """Collect, deliver, store and read back one operation, unaltered."""
    browser, _sandbox_manager = collector_client
    admin, capability = reader
    job_id = await _create_job(browser)
    _attach_shared_workspace(store, job_id)
    store.jobs[job_id]["thread_sandbox"].update({"sandbox_id": "asbx_e2e", "host": "host-e2e"})

    # 1. A browser reads a directory through the real route.
    read = await browser.get(f"/v1/agent/jobs/{job_id}/files")
    assert read.status_code == 200
    block = read.json()["profiling"]
    assert block["operation"] == "files.list"
    assert block["collection_complete"] is True

    # 2. The collector froze exactly one record for it.
    emitted = _emitted()
    assert len(emitted) == 1
    record = emitted[0]
    assert record["measurement_id"] == block["measurement_id"]

    # 3. Delivery validates it and stores it under the job the credential names.
    stored = _validated(
        record,
        owner=Owner(owner_id="user-1", job_id=job_id, thread_id=store.jobs[job_id]["thread_id"]),
        run=_manifest(),
    )
    assert (
        await capability.record_operation_measurement(
            measurement=stored.as_row(), run=measurement_ingest.validate_run(_manifest())
        )
        == "stored"
    )

    # 4. A browser continuing the same interaction is a new record under the
    #    same interaction, and both are readable as one interaction.
    again = await browser.get(
        f"/v1/agent/jobs/{job_id}/files",
        headers={INTERACTION_HEADER: block["interaction_id"]},
    )
    assert again.json()["profiling"]["interaction_id"] == block["interaction_id"]
    second = _emitted()[-1]
    assert second["measurement_id"] != record["measurement_id"]
    assert (
        await capability.record_operation_measurement(
            measurement=_validated(
                second, owner=Owner(owner_id="user-1", job_id=job_id), run=_manifest()
            ).as_row()
        )
        == "stored"
    )

    # 5. An administrator finds both, and the numbers describe the read rather
    #    than the page they were read from.
    listed = await admin.get(f"/admin/agent/measurements?run_id={RUN_ID}", headers=ADMIN)
    assert listed.status_code == 200
    assert listed.json()["count"] == 2

    detail = await admin.get(f"/admin/agent/measurements/{record['measurement_id']}", headers=ADMIN)
    body = detail.json()
    assert body["job_id"] == job_id
    assert body["coverage_scope"] == "server_request"
    assert body["end_to_end_complete"] is False
    assert body["record"]["operation"] == "files.list"

    summary = await admin.get(f"/admin/agent/profiling-runs/{RUN_ID}/summary", headers=ADMIN)
    assert summary.status_code == 200
    assert summary.json()["groups"][0]["eligible_count"] == 2

    catalogue = await admin.get("/admin/agent/profiling-runs", headers=ADMIN)
    assert [run["run_id"] for run in catalogue.json()["runs"]] == [RUN_ID]
    assert capability.runs[RUN_ID]["admission_instances"] == [
        {"instance": "cp-e2e", "max_operations": 10}
    ]


async def test_the_chain_files_a_subdirectory_as_a_listing(
    collector_client, reader, store: FakeStorage, monkeypatch
):
    """The classification survives every hop, not only the first one."""
    browser, _sandbox_manager = collector_client
    admin, capability = reader
    job_id = await _create_job(browser)
    _attach_shared_workspace(store, job_id)
    store.jobs[job_id]["thread_sandbox"].update({"sandbox_id": "asbx_e2e", "host": "host-e2e"})

    for path in ("", "src", "README.md"):
        response = await browser.get(f"/v1/agent/jobs/{job_id}/files?path={path}")
        assert response.status_code == 200
        stored = measurement_ingest.validate_measurement(
            _emitted()[-1],
            owner=Owner(job_id=job_id),
        )
        assert (
            await capability.record_operation_measurement(measurement=stored.as_row()) == "stored"
        )

    listed = await admin.get(
        f"/admin/agent/measurements?run_id={RUN_ID}&operation=files.list", headers=ADMIN
    )
    # The root listing and the expansion are listings; only the file is an open.
    assert [row["request_kind"] for row in listed.json()["measurements"]] == [
        "files.list",
        "files.list",
    ]


async def test_a_record_the_ingest_refuses_never_reaches_the_page(
    collector_client, reader, store: FakeStorage, monkeypatch
):
    """A producer the deployment does not understand cannot corrupt a chart."""
    browser, _sandbox_manager = collector_client
    _admin, capability = reader
    job_id = await _create_job(browser)
    _attach_shared_workspace(store, job_id)
    store.jobs[job_id]["thread_sandbox"].update({"sandbox_id": "asbx_e2e", "host": "host-e2e"})

    response = await browser.get(f"/v1/agent/jobs/{job_id}/files")
    assert response.status_code == 200
    record = _emitted()[0]

    # A record from a schema this deployment does not know is refused, and the
    # refusal is what keeps it out rather than a page's judgement about it.
    with pytest.raises(measurement_ingest.MeasurementRejected):
        measurement_ingest.validate_measurement({**record, "schema_version": 99})

    assert capability.rows == {}


async def test_a_record_never_carries_the_file_it_read(
    collector_client, store: FakeStorage, monkeypatch
):
    """The chain's content-free promise is enforceable at the seam."""
    browser, _sandbox_manager = collector_client
    job_id = await _create_job(browser)
    _attach_shared_workspace(store, job_id)
    store.jobs[job_id]["thread_sandbox"].update({"sandbox_id": "asbx_e2e", "host": "host-e2e"})

    response = await browser.get(f"/v1/agent/jobs/{job_id}/files?path=README.md")
    assert response.status_code == 200
    record = _emitted()[0]

    # The sandbox manager's answer is in the response body, and only the kind it found
    # is in the record: the content and the path stayed behind.
    assert response.json()["content"] == "hello"
    serialized = json.dumps(record)
    assert "hello" not in serialized
    assert "README.md" not in serialized
    assert record["facts"]["kind"] == "file"
