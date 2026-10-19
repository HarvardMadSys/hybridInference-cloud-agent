"""End-to-end measurement for file and terminal operations.

Where ``test_agent_operation_profiling.py`` defends the collector's own
contract, this module defends the *root operation* the control plane owns: an
HTTP request through the real router, a real selection, and a real fragment
protocol to a sandbox manager stand-in that answers exactly the way the sandbox manager does.

The properties under test are the ones the design promises for this first
server-side slice:

* profiling off changes nothing a client can see -- not the body, not a header,
  not an extra call;
* an unselected operation is not measured and does not spend the experiment's
  budget;
* a service-scoped target matches on the facts the route can actually prove;
* one interaction is one measurement, however many requests it takes;
* a record names the fragment it owed and did not get rather than looking
  complete;
* nothing about measurement can fail the file read or the terminal keystroke.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, Field

from cloud_agent import operation_measurement, sandbox_manager_routing
from cloud_agent.operation_measurement import (
    INTERACTION_HEADER,
    LEGACY_MEASUREMENT_HEADER,
    reset as reset_selections,
    root_scope,
)
from cloud_agent.sandbox_manager_client import (
    SandboxManagerClient,
    SandboxManagerError,
    SandboxManagerStream,
)
from cloud_agent_host import operation_call
from cloud_agent_shared import operation_profiling


class _UnusedClient:
    """A client the streaming test never opens; the stream owns no socket here."""

    async def aclose(self) -> None:
        """Close nothing."""
        return None


from tests.servers.test_agent_jobs_api import (
    FakeStorage,
    _attach_shared_workspace,
    _build_app,
    _create_job,
)

SANDBOX_MANAGER_URL = "http://sandbox_manager.test:8092"
SANDBOX_MANAGER_HOST = "host-under-test"
SANDBOX_ID = "asbx_measured"


@pytest.fixture(autouse=True)
def _deployment_secrets(monkeypatch):
    """Provide the signing secrets and the one entitled repository.

    The same environment the agent-jobs route tests run in: the repo allowlist
    is empty by default by design, so a test that creates a job has to declare
    the repository it uses.
    """
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "api-test-secret")
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_TOKEN", "api-test-sandbox-manager-secret")
    monkeypatch.setenv("AGENT_SESSION_SECRET", "session-test-secret-32-bytes-minimum!")
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name,o/n,private/repo")


@pytest.fixture(autouse=True)
async def _running_writer():
    """Run the write queue for each test, as the application's lifespan does.

    Records are offered to a bounded queue and written off the request path, so
    a test that asserts on what was stored has to let the queue run -- which is
    the behaviour under test, not a workaround.
    """
    from cloud_agent import measurement_writer

    writer = measurement_writer.MeasurementWriter()
    measurement_writer.reset_writer(writer)
    try:
        yield writer
    finally:
        await writer.shutdown()
        measurement_writer.reset_writer(None)


async def _drain(writer: Any, count: int) -> None:
    """Wait, briefly, for the queue to have written ``count`` records."""
    for _ in range(200):
        if writer.counts.stored >= count:
            return
        await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def _clean_measurement_state(monkeypatch, tmp_path):
    """Every test starts from a process that has never read the switch."""
    monkeypatch.delenv(operation_profiling.ENV_PROFILING_ENABLED, raising=False)
    monkeypatch.delenv(operation_profiling.ENV_PROFILING_RUN, raising=False)
    monkeypatch.delenv(operation_profiling.ENV_PROFILING_SINK, raising=False)
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(tmp_path / "records.jsonl"))
    operation_profiling.reset_cache()
    reset_selections()
    yield
    operation_profiling.reset_cache()
    reset_selections()


@pytest.fixture
def store() -> FakeStorage:
    """Provide a fresh in-memory store that keeps what it is asked to store."""
    return _PersistingStorage()


@pytest.fixture
def records(tmp_path):
    """Return the measurements emitted so far, draining the exporter first."""
    sink = tmp_path / "records.jsonl"

    def read() -> list[dict[str, Any]]:
        # The sink is the authoritative copy: one exporter publishes to both
        # destinations, so reading the log as well would double-count.
        operation_profiling.flush_sink()
        if not sink.exists():
            return []
        return [json.loads(line) for line in sink.read_text().splitlines() if line]

    return read


def _enable(monkeypatch, **overrides: Any) -> dict[str, Any]:
    """Turn the switch on with a valid run descriptor."""
    run: dict[str, Any] = {
        "run_id": "cp-run-1",
        "operations": ["files.list", "files.open", "terminal.open"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        "max_operations": 10,
    }
    run.update(overrides)
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_RUN, json.dumps(run))
    operation_profiling.reset_cache()
    reset_selections()
    return run


def _enable_admin(monkeypatch, **overrides: Any) -> tuple[dict[str, Any], Any]:
    """Put this process under Admin control with one experiment in force.

    Returns the descriptor and the published snapshot, so a test can publish a
    Stop the way the control plane's sync loop does.
    """
    run: dict[str, Any] = {
        "run_id": "cp-run-1",
        "operations": ["files.list", "files.open", "terminal.open"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        "max_operations": 10,
    }
    run.update(overrides)
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_MODE, operation_profiling.MODE_ADMIN)
    operation_profiling.reset_cache()
    reset_selections()
    published = operation_profiling.policy_store().publish(
        operation_profiling.ControlState(
            mode=operation_profiling.MODE_ADMIN,
            config_version=1,
            run=operation_profiling.parse_run(json.dumps(run)),
        )
    )
    return run, published


def _answer(headers: Any) -> dict[str, Any] | None:
    """Decode the fragment a sandbox manager stand-in echoed, as the real client does."""
    return operation_profiling.answer_from_headers(headers)


def _context(**overrides: Any) -> dict[str, Any]:
    """Build the selection one producer inherits from another."""
    context: dict[str, Any] = {
        "run_id": "cp-run-1",
        "interaction_id": "msm_interaction",
        "measurement_id": "msm_1",
        "operations": ["files.list"],
        "call_id": "c1",
        "expires_at": 2**31,
    }
    context.update(overrides)
    return context


def test_candidate_operations_survive_the_real_header_parser(monkeypatch):
    """A file candidate sent as JSON must not become an unselected sandbox manager call."""
    _enable(monkeypatch, operations=["files.list", "files.open"])
    encoded = json.dumps(
        _context(
            operations=["files.list", "files.open"],
            operation="",
        )
    )

    call = operation_call.open_call(
        encoded,
        operations=("files.list", "files.open"),
    )

    assert call.recording is True
    assert call.call_id == "c1"


class _RecordingClient:
    """A sandbox manager that answers a fragment exactly the way the real sandbox manager does.

    It reuses the sandbox manager's own :func:`open_call` and :class:`SandboxManagerCall`, so
    these tests exercise the wire contract rather than a second implementation
    of it: the request carries a selection, the route validates it against this
    process's switch and run, and the answer echoes the call it belongs to.
    """

    def __init__(
        self,
        *,
        fail: SandboxManagerError | None = None,
        directories: Mapping[str, str] | None = None,
        truncate: bool = False,
        empty_stages: bool = False,
        delay_s: float = 0.0,
    ) -> None:
        self.fail = fail
        #: Answer as a sandbox manager whose own deadline cut collection short.
        self.truncate = truncate
        #: Answer with no stages at all -- a legitimate skip when not truncated,
        #: and "every stage was skipped" when it is.
        self.empty_stages = empty_stages
        #: How long a read takes. A sandbox manager stand-in that answers instantly
        #: cannot show whether a stage encloses the call or follows it.
        self.delay_s = delay_s
        self.calls: list[dict[str, Any]] = []
        #: What a non-empty path turns out to be. A real sandbox manager learns this
        #: from the read; a test states it, which is what makes "a subdirectory
        #: is a listing" testable at all.
        self.directories = dict(directories or {})
        self._descriptor = {
            "id": "term_measured",
            "shell": "/bin/bash",
            "state": "running",
            "cwd": "/workspace",
            "rows": 24,
            "cols": 80,
            "last_seq": 0,
        }

    def kind_for(self, path: str) -> str:
        """Return the kind this read reports for a path."""
        if not path:
            return "directory"
        return "directory" if path in self.directories else "file"

    @staticmethod
    def _file_operation(kind: str) -> str:
        """Return the operation a read's own kind implies, as the sandbox manager does."""
        return "files.list" if kind == "directory" else "files.open"

    def _open(self, context: Any, *operations: str) -> operation_call.SandboxManagerCall:
        """Validate the inherited selection as the sandbox manager route does."""
        call = operation_call.open_call(context, operations=operations)
        with call.stage("admission"):
            pass
        return call

    def with_start_seq(self, _start_seq: int | None):
        return self

    async def register_shared_sandbox(self, _sandbox: dict[str, Any]) -> None:
        """Accept the manifest re-registration a workspace request performs."""
        return None

    async def files(
        self, workspace_id: str, path: str, *, profiling: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Answer a file or directory read."""
        self.calls.append({"op": "files", "workspace_id": workspace_id, "path": path})
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        call = self._open(profiling, *(["files.list", "files.open"] if path else ["files.list"]))
        if self.fail is not None:
            raise self.fail
        if not self.empty_stages:
            with call.stage("exec"):
                pass
        kind = self.kind_for(path)
        body = (
            {"path": path, "kind": "directory", "entries": [], "writable": True}
            if kind == "directory"
            else {"path": path, "kind": "file", "content": "hello", "size": 5}
        )
        # A refused operation leaves the call unnamed: the answer then reports
        # none, and the caller discards the request rather than filing it under
        # the placeholder it started with.
        call.set_operation(self._file_operation(kind))
        answer = call.answer(body)
        if self.truncate:
            answer["profiling_truncated"] = True
        return answer

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
        if self.fail is not None:
            raise self.fail
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
        if self.fail is not None:
            raise self.fail
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
        """Answer a stream in headers, the way the sandbox manager's SSE route does."""
        self.calls.append({"op": "stream_terminal", "workspace_id": workspace_id})
        call = self._open(profiling, "terminal.open")
        call.set_operation("terminal.open")
        with call.stage("stream_attach"):
            pass
        if self.truncate:
            call.recorder.truncated += 1
        response = httpx.Response(200, headers=call.headers(), stream=httpx.ByteStream(b""))
        return SandboxManagerStream(
            _UnusedClient(), response, profiling_answer=_answer(response.headers)
        )


def _use_sandbox_manager(monkeypatch, sandbox_manager: _RecordingClient) -> None:
    """Point every job's resolved sandbox manager at the stand-in."""
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)


def _workspace_job(store: FakeStorage, job_id: str) -> None:
    """Give a created job the retained group and sandbox a real host reports."""
    thread_id = _attach_shared_workspace(store, job_id)
    store.jobs[job_id]["thread_sandbox"].update(
        {"sandbox_id": SANDBOX_ID, "host": SANDBOX_MANAGER_HOST, "origin_thread_id": thread_id}
    )


@pytest_asyncio.fixture()
async def client(store: FakeStorage):
    """Mount the agent-jobs router with auth stubbed to a fixed owner."""
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


async def _ready_job(client: AsyncClient, store: FakeStorage) -> str:
    """Create a job whose retained workspace is attached and reachable."""
    job_id = await _create_job(client)
    _workspace_job(store, job_id)
    return job_id


# ---------------------------------------------------------------------------
# Off, and unselected, are both no-ops a client can see
# ---------------------------------------------------------------------------


async def test_profiling_off_leaves_the_payload_and_the_call_untouched(
    client: AsyncClient, store: FakeStorage, monkeypatch, records, _running_writer
):
    """A switched-off deployment serves exactly what it served before."""
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    listing = await client.get(f"/v1/agent/jobs/{job_id}/files")

    assert listing.status_code == 200
    # Byte-for-byte the shape the parent commit served: no `profiling` key, and
    # every business field -- including the ones that are legitimately null --
    # exactly as it was. A client that never heard of this feature sees nothing
    # new.
    body = listing.json()
    assert body == _parent_workspace_shape(
        {
            "path": "",
            "kind": "directory",
            "entries": [],
            "writable": True,
            "source": "workspace",
        }
    )
    assert set(body) >= {"content", "size", "status", "omitted_reason"}
    assert body["content"] is None and body["size"] is None
    assert [call["op"] for call in sandbox_manager.calls] == ["files"]
    assert records() == []
    assert _running_writer.running is False


async def test_an_unselected_operation_is_not_measured(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """A run that does not name this operation never sees a sample."""
    _enable(monkeypatch, operations=["attempt.startup"])
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    listing = await client.get(f"/v1/agent/jobs/{job_id}/files")

    assert listing.status_code == 200
    assert "profiling" not in listing.json()
    assert records() == []


async def test_another_chats_target_does_not_select_this_one(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """A job-scoped experiment does not measure traffic from every other chat."""
    _enable(monkeypatch, target={"job_id": "ajob_somewhere_else"})
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    listing = await client.get(f"/v1/agent/jobs/{job_id}/files")

    assert listing.status_code == 200
    assert "profiling" not in listing.json()
    assert records() == []


async def test_a_target_the_route_cannot_prove_never_matches(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """An unknown sandbox is a mismatch, not a wildcard."""
    _enable(monkeypatch, target={"sandbox_id": SANDBOX_ID})
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    # A job with no retained sandbox manifest: the route cannot prove the id.
    job_id = await _create_job(client)
    _attach_shared_workspace(store, job_id)

    listing = await client.get(f"/v1/agent/jobs/{job_id}/files")

    assert listing.status_code == 200
    assert "profiling" not in listing.json()
    assert records() == []


# ---------------------------------------------------------------------------
# A selected request is measured end to end
# ---------------------------------------------------------------------------


async def test_a_selected_file_read_is_measured_with_its_sandbox_manager_fragment(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """The record carries the route's stages and the sandbox manager's own durations."""
    _enable(monkeypatch, target={"sandbox_id": SANDBOX_ID})
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    response = await client.get(f"/v1/agent/jobs/{job_id}/files", params={"path": "README.md"})

    assert response.status_code == 200
    block = response.json()["profiling"]
    assert block["fragment_recorded"] is True
    assert block["measurement_id"].startswith("msm_")

    emitted = records()
    assert len(emitted) == 1
    record = emitted[0]
    assert record["schema_version"] == 3
    assert record["operation"] == "files.open"
    # The record states how far its own boundaries reach, so a reader cannot
    # present a server request as the user's whole wait.
    assert record["coverage_scope"] == "server_request"
    assert record["boundary_version"] == 1
    assert record["end_to_end_complete"] is False
    assert record["producer"] == "control-plane"
    assert record["producers"] == ["control-plane", "broker"]
    assert record["outcome"] == "completed"
    assert record["measurement_complete"] is True
    assert record["measurement_id"] == block["measurement_id"]
    # v1's name is a claim about a startup, so a file read never makes it.
    assert "startup_outcome" not in record
    assert record["facts"]["kind"] == "file"
    assert record["facts"]["job_id"]
    assert record["facts"]["sandbox_id"] == SANDBOX_ID
    # The read is timed as one stage named before the call was made, so the
    # request's whole duration belongs to it and the round trip hangs under it.
    read = next(stage for stage in record["stages"] if stage["stage"] == "read")
    children = {child["stage"]: child["source"] for child in read["children"]}
    assert children == {
        "sandbox_manager_call": "control-plane",
        "admission": "broker",
        "exec": "broker",
    }


async def test_the_root_listing_is_measured_as_a_listing(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """The root path is files.list; a selected entry is files.open."""
    _enable(monkeypatch)
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    listing = await client.get(f"/v1/agent/jobs/{job_id}/files")
    opened = await client.get(f"/v1/agent/jobs/{job_id}/files", params={"path": "a.txt"})

    assert listing.status_code == 200 and opened.status_code == 200
    assert [record["operation"] for record in records()] == ["files.list", "files.open"]


async def test_the_budget_is_only_spent_by_a_request_that_matches(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """A non-matching chat cannot consume the experiment's whole allowance."""
    _enable(monkeypatch, target={"sandbox_id": SANDBOX_ID}, max_operations=1)
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    measured = await _ready_job(client, store)
    other = await _create_job(client)
    _attach_shared_workspace(store, other)

    for _ in range(3):
        assert (await client.get(f"/v1/agent/jobs/{other}/files")).status_code == 200
    selected = await client.get(f"/v1/agent/jobs/{measured}/files")
    refused = await client.get(f"/v1/agent/jobs/{measured}/files")

    assert selected.status_code == 200 and refused.status_code == 200
    assert "profiling" in selected.json()
    assert "profiling" not in refused.json()
    assert len(records()) == 1


# ---------------------------------------------------------------------------
# One interaction, one measurement
# ---------------------------------------------------------------------------


async def test_one_interaction_correlates_its_requests_under_one_id(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """A terminal open is a list, a create and a stream -- one correlatable id.

    Each request is its own platform operation with its own duration, so each
    emits its own record; what they share is the id the browser echoes, which
    is what lets the three be read as one interaction.
    """
    _enable(monkeypatch)
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    listing = await client.get(f"/v1/agent/jobs/{job_id}/terminals")
    interaction_id = listing.json()["profiling"]["interaction_id"]
    first_measurement = listing.json()["profiling"]["measurement_id"]
    created = await client.post(
        f"/v1/agent/jobs/{job_id}/terminals",
        json={"rows": 24, "cols": 80},
        headers={INTERACTION_HEADER: interaction_id},
    )
    streamed = await client.get(
        f"/v1/agent/jobs/{job_id}/terminals/term_measured/stream",
        headers={INTERACTION_HEADER: interaction_id},
    )

    assert created.status_code == 200
    assert created.json()["profiling"]["interaction_id"] == interaction_id
    assert streamed.status_code == 200
    # The stream's own answer travels in response headers, not in its body.
    assert streamed.headers["x-agent-profiling-interaction-id"] == interaction_id

    # One interaction, and a separate immutable record for every request in it.
    measurements = {
        listing.json()["profiling"]["measurement_id"],
        created.json()["profiling"]["measurement_id"],
        streamed.headers["x-agent-profiling-measurement-id"],
    }
    assert len(measurements) == 3
    assert first_measurement in measurements

    emitted = records()
    assert {record["measurement_id"] for record in emitted} == measurements
    assert {record["facts"]["interaction_id"] for record in emitted} == {interaction_id}
    # A list, a create and a stream are three different requests, so a
    # distribution over them can name which one it is measuring.
    assert {record["facts"]["request_kind"] for record in emitted} == {
        "terminal.list",
        "terminal.create",
        "terminal.stream_attach",
    }
    for record in emitted:
        assert record["operation"] == "terminal.open"
        assert record["collection_complete"] is True
    # Each request's own sandbox manager fragment attached to that request's stage.
    by_stage = {stage["stage"]: stage for record in emitted for stage in record["stages"]}
    names = ("terminal.list", "terminal.create", "terminal.stream_attach")
    assert set(names) <= set(by_stage)
    for name in names:
        children = by_stage[name]["children"]
        # The round trip this process timed, plus whatever the sandbox manager timed for
        # the same call -- at minimum its own admission of the operation.
        assert "sandbox_manager_call" in {child["stage"] for child in children}
        assert "admission" in {child["stage"] for child in children}
        assert {child["source"] for child in children} == {"control-plane", "broker"}
    assert emitted[0]["facts"]["terminal_count"] == 1


async def test_a_continuation_still_reports_its_own_time(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """Reusing an id does not mean collecting into a record already written."""
    _enable(monkeypatch)
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    first = await client.get(f"/v1/agent/jobs/{job_id}/terminals")
    interaction_id = first.json()["profiling"]["interaction_id"]
    second = await client.get(
        f"/v1/agent/jobs/{job_id}/terminals",
        headers={INTERACTION_HEADER: interaction_id},
    )

    assert second.status_code == 200
    # Same interaction, different record: a continuation is a new request.
    assert second.json()["profiling"]["interaction_id"] == interaction_id
    assert (
        second.json()["profiling"]["measurement_id"] != first.json()["profiling"]["measurement_id"]
    )
    emitted = records()
    assert len(emitted) == 2
    # Both are real measurements of this interaction, not one empty record.
    for record in emitted:
        assert record["collection_complete"] is True
        listing = next(item for item in record["stages"] if item["stage"] == "terminal.list")
        children = listing["children"]
        assert {"sandbox_manager_call", "admission"} <= {child["stage"] for child in children}
        assert {child["source"] for child in children} == {"control-plane", "broker"}


async def test_a_continuation_from_another_chat_is_refused(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """An echoed id is a correlation handle, not a way to observe another chat."""
    _enable(monkeypatch)
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    owner = await _ready_job(client, store)
    other = await _ready_job(client, store)

    first = await client.get(f"/v1/agent/jobs/{owner}/files")
    interaction_id = first.json()["profiling"]["interaction_id"]
    foreign = await client.get(
        f"/v1/agent/jobs/{other}/files",
        headers={INTERACTION_HEADER: interaction_id},
    )

    assert foreign.status_code == 200
    # The reference did not carry the other chat's interaction across, and it
    # is not silently replaced by a fresh one either: a request that names an
    # interaction this process cannot honour is still served, and is not
    # measured. Sampling it would present a new interaction as though the one
    # the caller named had continued.
    assert "profiling" not in foreign.json()
    assert [record["measurement_id"] for record in records()] == [
        first.json()["profiling"]["measurement_id"]
    ]


# ---------------------------------------------------------------------------
# Missing fragments, failures, and expiry are reported, never hidden
# ---------------------------------------------------------------------------


async def test_a_sandbox_manager_that_never_answered_is_named_as_a_missing_fragment(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """An unanswerable call is a fragment the record owed, not a complete sample."""
    _enable(monkeypatch)
    sandbox_manager = _RecordingClient(fail=SandboxManagerError(503, "unavailable"))
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    response = await client.get(f"/v1/agent/jobs/{job_id}/files")

    assert response.status_code == 503
    record = records()[0]
    assert record["outcome"] == "failed"
    assert record["measurement_complete"] is False
    assert record["missing_producers"] == ["broker"]
    assert record["missing_fragments"] == ["broker:c1:read"]


async def test_an_expired_run_collects_nothing_further(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """Past its deadline the run stops selecting, and business carries on."""
    run = _enable(monkeypatch)
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    run["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_RUN, json.dumps(run))
    operation_profiling.reset_cache()

    response = await client.get(f"/v1/agent/jobs/{job_id}/files")

    assert response.status_code == 200
    assert "profiling" not in response.json()
    assert records() == []


async def test_a_malformed_selection_disables_collection_not_the_read(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """Measurement metadata must never turn a file read into an error."""
    _enable(monkeypatch)
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    listed = await client.get(
        f"/v1/agent/jobs/{job_id}/files",
        headers={INTERACTION_HEADER: "not-a-measurement-id"},
    )

    assert listed.status_code == 200
    # An unusable reference disables collection for this request and nothing
    # else: the read is served exactly as it would have been, and no fresh
    # interaction is invented to stand in for the one that was named.
    assert "profiling" not in listed.json()
    assert records() == []

    # A request naming no interaction is an ordinary new one and is measured
    # normally -- the rule is about references, not about traffic.
    fresh = await client.get(f"/v1/agent/jobs/{job_id}/files")
    assert fresh.status_code == 200
    assert fresh.json()["profiling"]["fragment_recorded"] is True


# ---------------------------------------------------------------------------
# The sandbox manager's half of the protocol
# ---------------------------------------------------------------------------


def test_a_sandbox_manager_refuses_a_selection_for_another_operation(monkeypatch):
    """A fragment is never filed under an operation this route does not own."""
    _enable(monkeypatch)
    context = _context(operations=["terminal.open"])

    assert operation_call.open_call(context, operations=("files.open",)).recording is False
    assert operation_call.open_call(context, operations=("terminal.open",)).recording is True


def test_a_sandbox_manager_refuses_an_operation_its_own_run_does_not_select(monkeypatch):
    """The sandbox manager's own run decides, whatever a caller claims was selected."""
    _enable(monkeypatch, operations=["terminal.open"])

    assert (
        operation_call.open_call(
            _context(operations=["files.open"]), operations=("files.open",)
        ).recording
        is False
    )


def test_a_sandbox_manager_drops_a_context_it_cannot_validate(monkeypatch):
    """Every unusable selection answers "not collecting" and nothing else."""
    _enable(monkeypatch)
    undecodable = [
        None,
        "",
        "not json",
        json.dumps(["a", "list"]),
        json.dumps({"run_id": "cp-run-1"}),  # no expiry
        json.dumps({"run_id": "cp-run-1", "expires_at": "soon"}),
        "x" * (operation_profiling.MAX_CONTEXT_CHARS + 1),
    ]
    for context in undecodable:
        assert operation_profiling.parse_context(context) is None
        assert operation_call.open_call(context, operations=("files.open",)).recording is False

    # Decodable, and refused because the experiment it names has already lapsed.
    expired = json.dumps(_context(expires_at=1))
    assert operation_profiling.parse_context(expired) is not None
    assert operation_call.open_call(expired, operations=("files.open",)).recording is False

    # Decodable, but scoped to an experiment this process is not running.
    foreign = json.dumps(_context(run_id="another-run"))
    assert operation_profiling.parse_context(foreign) is not None
    assert operation_call.open_call(foreign, operations=("files.open",)).recording is False


def test_a_streaming_answer_round_trips_through_headers(monkeypatch):
    """A call whose body is terminal bytes still answers, in headers."""
    _enable(monkeypatch)
    call = operation_call.open_call(
        _context(operations=["terminal.open"], call_id="c7"),
        operations=("terminal.open",),
    )
    # The route files the call once its own response exists, exactly as the
    # real streaming route does.
    call.set_operation("terminal.open")
    with call.stage("stream_attach"):
        pass

    headers = call.headers(extra={"Cache-Control": "no-cache"})

    assert headers["Cache-Control"] == "no-cache"
    answer = operation_profiling.answer_from_headers(headers)
    assert answer is not None
    assert answer["profiling_call_id"] == "c7"
    assert answer["profiling_recorded"] is True
    assert answer["profiling_operation"] == "terminal.open"
    # The stage is what the contract promises; its duration is whatever the
    # clock on this machine measured. Pinning it to a number made this pass
    # locally and fail on CI, where an empty `with` body can round to zero.
    (stage,) = answer["stages"]
    assert stage["name"] == "stream_attach"
    assert stage["outcome"] == "ok"
    assert isinstance(stage["duration_us"], int) and stage["duration_us"] >= 0
    # A response that answers nothing leaves the fragment unanswered.
    assert operation_profiling.answer_from_headers({"Cache-Control": "no-cache"}) is None


def test_an_unselected_sandbox_manager_call_records_and_answers_nothing(monkeypatch):
    """Off means off on the sandbox manager too, down to the response headers."""
    call = operation_call.open_call(None, operations=("files.open",))

    assert call.recording is False
    assert call.headers() == {}
    with call.stage("admission"):
        pass
    assert call.answer({"kind": "file"}) == {"kind": "file"}


# ---------------------------------------------------------------------------
# The client half of the wire protocol
# ---------------------------------------------------------------------------


async def test_the_sandbox_manager_client_sends_the_selection_and_preserves_the_answer(
    monkeypatch, records
):
    """The gateway's client carries the selection and does not eat the answer."""
    _enable(monkeypatch)
    seen: list[dict[str, Any]] = []

    class _Http:
        @asynccontextmanager
        async def stream(self, method, url, *, headers, params, json, timeout):
            seen.append({"method": method, "url": url, "headers": headers, "params": params})
            yield httpx.Response(200, json={"path": "", "kind": "directory", "entries": []})

    client = SandboxManagerClient(SANDBOX_MANAGER_URL, "token")
    monkeypatch.setattr(client, "_http", _http_for(_Http()))

    body = await client.files("athr_1", "", profiling=_context())

    assert body == {"path": "", "kind": "directory", "entries": []}
    assert seen[0]["headers"]["X-Agent-Operation-Profiling"]
    assert seen[0]["headers"]["X-Agent-Workspace-Token"] == "token"


def _http_for(http: Any):
    """Return an async context manager yielding one fake HTTP client."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _open(_timeout: float):
        yield http

    return _open


def test_the_outcome_vocabulary_covers_both_kinds_of_root_operation():
    """One schema, and neither producer borrows the other's words."""
    # A startup's fate and a platform operation's endpoint both report here.
    assert {"dispatched", "failed", "incomplete"} <= operation_profiling.OUTCOMES
    assert {"completed", "superseded"} <= operation_profiling.OUTCOMES


def test_a_scoped_run_reports_the_facts_the_route_proved(monkeypatch, records):
    """The record names the scope it matched, so a baseline can be filtered."""
    _enable(monkeypatch, target={"host": SANDBOX_MANAGER_HOST})
    scope = root_scope(
        "files.list",
        {
            "id": "ajob_1",
            "thread_id": "athr_1",
            "thread_sandbox": {"sandbox_id": SANDBOX_ID, "host": SANDBOX_MANAGER_HOST},
        },
    )
    assert scope.facts == {
        "thread_id": "athr_1",
        "sandbox_id": SANDBOX_ID,
        "host": SANDBOX_MANAGER_HOST,
    }


# ---------------------------------------------------------------------------
# The off path has to match what the parent version actually served
# ---------------------------------------------------------------------------


#: The parent commit's three response models, copied verbatim from
#: ``git show 6556746e5a99:apps/backend/cloud_agent/schemas.py``.
_PARENT_SCHEMAS = (
    Path(__file__).resolve().parents[1] / "fixtures" / "parent_schemas_6556746e5a99.py.txt"
)


def _parent_models() -> dict[str, Any]:
    """Build the response models exactly as the parent commit declared them.

    Copied from that commit verbatim rather than rewritten by hand, so this
    baseline cannot quietly drift into agreeing with whatever the current code
    happens to emit. The parent is the commit that had no profiling fields at
    all, which makes it the contract a switched-off deployment must still
    honour. The copy is vendored under ``tests/fixtures`` instead of read with
    ``git show``, because a shallow clone does not have that commit.

    It is the parent of the squash commit that landed the profiling fields
    (#258), not ``e77e3e6^`` on the feature branch: the three models are
    byte-identical in both, and only the first stays reachable from ``dev``
    once that branch is deleted.
    """
    import sys
    import types

    source = _PARENT_SCHEMAS.read_text(encoding="utf-8")
    wanted = ["AgentWorkspaceEntry", "AgentWorkspaceResponse", "AgentTerminalSessionResponse"]
    chunks = []
    for name in wanted:
        match = re.search(
            rf"^class {name}\(BaseModel\):.*?(?=^class |\Z)", source, re.MULTILINE | re.DOTALL
        )
        assert match is not None, name
        chunks.append(match.group(0))
    # The models are pure field declarations, but they are not trusted to stay
    # that way: only annotations and assignments are executed, so a future
    # parent cannot smuggle behaviour into the baseline.
    module = types.ModuleType("parent_schemas")
    module.__dict__.update({"BaseModel": BaseModel, "Field": Field, "Literal": Literal})
    sys.modules["parent_schemas"] = module
    exec(compile("\n".join(chunks), "parent_schemas", "exec"), module.__dict__)
    return {name: getattr(module, name) for name in wanted}


def _parent_workspace_shape(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the HTTP shape the parent served for one directory listing."""
    models = _parent_models()
    return models["AgentWorkspaceResponse"](**payload).model_dump()


# ---------------------------------------------------------------------------
# R1: overlapping requests are independent measurements
# ---------------------------------------------------------------------------


async def test_overlapping_requests_each_get_their_own_measurement(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """Two requests of one interaction never share a collector.

    The review found the opposite: a browser that fired two requests with the
    same continuation id got one shared ``Selection``, both calls minted
    ``c1``, the first ``finish`` closed the record and the second request's
    answer was dropped -- while the record still claimed to be complete. Here
    both requests must produce their own record, their own budget slot, their
    own call id and their own fragment.
    """
    _enable(monkeypatch, max_operations=10)
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    first = await client.get(f"/v1/agent/jobs/{job_id}/terminals")
    interaction_id = first.json()["profiling"]["interaction_id"]

    # Both requests are in flight at once, as a browser's overlapping fetches
    # are. Neither sees the other's collector.
    listing, created = await asyncio.gather(
        client.get(
            f"/v1/agent/jobs/{job_id}/terminals",
            headers={INTERACTION_HEADER: interaction_id},
        ),
        client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
            headers={INTERACTION_HEADER: interaction_id},
        ),
    )

    assert listing.status_code == 200 and created.status_code == 200
    measurements = {
        first.json()["profiling"]["measurement_id"],
        listing.json()["profiling"]["measurement_id"],
        created.json()["profiling"]["measurement_id"],
    }
    # Three requests, three records -- not two requests collapsing into one.
    assert len(measurements) == 3

    emitted = records()
    assert {record["measurement_id"] for record in emitted} == measurements
    assert len(emitted) == 3
    for record in emitted:
        assert record["collection_complete"] is True
        assert not record.get("missing_fragments")
    # Each request's answer was recorded against the call that request made,
    # so every record names a fragment it actually received.
    assert {tuple(record["producers"]) for record in emitted} == {("control-plane", "broker")}
    # Each record accounts for exactly the one call its own request made.
    # Call ids are per measurement, so the ids coincide; what must not happen
    # is one request's answer landing in another request's record.
    for record in emitted:
        calls = {
            child["call_id"]
            for stage in record["stages"]
            for child in stage.get("children", [])
            if child.get("call_id")
        }
        assert calls == {"c1"}
        assert record["timing_sources"] == ["control-plane", "broker"]


async def test_an_answer_is_recorded_against_the_call_that_made_it(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """A second call in one request cannot overwrite the first call's answer."""
    _enable(monkeypatch)
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    with operation_measurement.operation(
        operation_measurement.TERMINAL_OPEN,
        root_scope(
            operation_measurement.TERMINAL_OPEN,
            {
                "id": job_id,
                "thread_id": store.jobs[job_id]["thread_id"],
                "thread_sandbox": {"sandbox_id": SANDBOX_ID, "host": SANDBOX_MANAGER_HOST},
            },
            service=operation_measurement.ROUTE_TERMINALS,
        ),
    ) as measure:
        assert measure is not None
        first_call = measure.begin_call("first")
        second_call = measure.begin_call("second")
        assert first_call != second_call
        # Answer only the second: the first stays a fragment this record owed.
        assert measure.record_answer(
            "second",
            second_call,
            {
                "profiling_call_id": second_call,
                "profiling_recorded": True,
                "profiling_operation": operation_measurement.TERMINAL_OPEN,
            },
        )
        measure.outcome = "completed"
        block = measure.metadata()

    # The call that answered is reported as recorded; the one that did not is
    # named, and the record as a whole is incomplete because of it.
    assert block["fragment_recorded"] is True
    assert block["collection_complete"] is False
    record = records()[0]
    assert sorted(record["missing_fragments"]) == [f"broker:{first_call}:first"]


async def test_a_legacy_measurement_id_header_still_correlates(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """A client that echoes the old header keeps working through the migration."""
    _enable(monkeypatch)
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    first = await client.get(f"/v1/agent/jobs/{job_id}/terminals")
    block = first.json()["profiling"]
    second = await client.get(
        f"/v1/agent/jobs/{job_id}/terminals",
        headers={LEGACY_MEASUREMENT_HEADER: block["measurement_id"]},
    )

    assert second.status_code == 200
    # Resolved through the server-side table, and reported as the interaction
    # it always was -- never reused as the new request's own record id.
    assert second.json()["profiling"]["interaction_id"] == block["interaction_id"]
    assert second.json()["profiling"]["measurement_id"] != block["measurement_id"]


# ---------------------------------------------------------------------------
# R2: a continuation is re-validated against this request's own facts
# ---------------------------------------------------------------------------


async def test_a_continuation_whose_host_changed_is_not_measured(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """A host that moved ends the interaction instead of being papered over."""
    _enable(monkeypatch, target={"host": SANDBOX_MANAGER_HOST})
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    first = await client.get(f"/v1/agent/jobs/{job_id}/files")
    interaction_id = first.json()["profiling"]["interaction_id"]
    # The chat's workspace moved to another machine between the two requests.
    store.jobs[job_id]["thread_sandbox"]["host"] = "another-host"

    second = await client.get(
        f"/v1/agent/jobs/{job_id}/files",
        headers={INTERACTION_HEADER: interaction_id},
    )

    assert second.status_code == 200
    assert "profiling" not in second.json()
    # The first request is the only sample, and it is the machine that served it.
    emitted = records()
    assert len(emitted) == 1
    assert emitted[0]["facts"]["host"] == SANDBOX_MANAGER_HOST


async def test_a_continuation_whose_sandbox_changed_is_not_measured(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """A re-provisioned workspace is not the one the experiment selected."""
    _enable(monkeypatch, target={"sandbox_id": SANDBOX_ID})
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    first = await client.get(f"/v1/agent/jobs/{job_id}/files")
    interaction_id = first.json()["profiling"]["interaction_id"]
    store.jobs[job_id]["thread_sandbox"]["sandbox_id"] = "asbx_replaced"

    second = await client.get(
        f"/v1/agent/jobs/{job_id}/files",
        headers={INTERACTION_HEADER: interaction_id},
    )

    assert second.status_code == 200
    assert "profiling" not in second.json()
    assert len(records()) == 1


async def test_a_continuation_whose_scope_is_no_longer_provable_is_not_measured(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """A fact that can no longer be resolved is a mismatch, not a pass."""
    _enable(monkeypatch, target={"sandbox_id": SANDBOX_ID})
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    first = await client.get(f"/v1/agent/jobs/{job_id}/files")
    interaction_id = first.json()["profiling"]["interaction_id"]
    # The retained sandbox manifest is gone, so this route cannot prove the id.
    store.jobs[job_id]["thread_sandbox"].pop("sandbox_id")

    second = await client.get(
        f"/v1/agent/jobs/{job_id}/files",
        headers={INTERACTION_HEADER: interaction_id},
    )

    assert second.status_code == 200
    assert "profiling" not in second.json()
    emitted = records()
    assert len(emitted) == 1
    # The old facts were not copied forward to fill the gap.
    assert emitted[0]["facts"]["sandbox_id"] == SANDBOX_ID


# ---------------------------------------------------------------------------
# R4: a truncated fragment is visible in the response, not only in the log
# ---------------------------------------------------------------------------


async def test_a_truncated_fragment_is_reported_to_the_client(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """The public answer and the record agree that collection was cut short.

    The review found the opposite: ``record_answer`` kept the truncation inside
    the measurement, but the response builder only passed ``recorded``, so a
    client was told ``truncated: false`` while the record said the sample was
    incomplete. A browser or an Admin view that trusted the response would have
    reported a floor as a duration.
    """
    _enable(monkeypatch)
    sandbox_manager = _RecordingClient(truncate=True)
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    response = await client.get(f"/v1/agent/jobs/{job_id}/files")

    assert response.status_code == 200
    block = response.json()["profiling"]
    assert block["fragment_recorded"] is True
    assert block["fragment_truncated"] is True
    assert block["collection_complete"] is False

    record = records()[0]
    # The response and the record say the same thing about the same call.
    assert record["truncated"] is True
    assert record["collection_complete"] is False
    assert record["truncated_fragments"] == ["broker:c1"]


async def test_an_empty_truncated_answer_is_not_a_complete_one(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """No stages plus a truncation is "measured nothing", not "nothing to time"."""
    _enable(monkeypatch)
    sandbox_manager = _RecordingClient(truncate=True, empty_stages=True)
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    response = await client.get(f"/v1/agent/jobs/{job_id}/files")

    block = response.json()["profiling"]
    assert block["fragment_recorded"] is True
    assert block["fragment_truncated"] is True
    assert block["collection_complete"] is False


async def test_a_normal_empty_answer_is_complete(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """A call that collected and had nothing to time is still a complete one."""
    _enable(monkeypatch)
    sandbox_manager = _RecordingClient(empty_stages=True)
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    response = await client.get(f"/v1/agent/jobs/{job_id}/files")

    block = response.json()["profiling"]
    assert block["fragment_recorded"] is True
    assert block["fragment_truncated"] is False
    assert block["collection_complete"] is True
    assert records()[0]["collection_complete"] is True


async def test_a_streamed_truncation_reaches_the_client_headers(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """A stream answers in headers, and its truncation has to travel too."""
    _enable(monkeypatch)
    sandbox_manager = _RecordingClient(truncate=True)
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    streamed = await client.get(f"/v1/agent/jobs/{job_id}/terminals/term_measured/stream")

    assert streamed.status_code == 200
    assert streamed.headers["x-agent-profiling-fragment-truncated"] == "1"
    assert streamed.headers["x-agent-profiling-collection-complete"] == "0"
    assert records()[0]["collection_complete"] is False


async def test_a_request_that_outlived_its_run_reports_incomplete(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """The response cannot claim completeness while the record is expiring.

    The boundary the request crosses here is the **experiment's own deadline**,
    which is the only thing that truncates work already selected. Swapping the
    deployment's configuration mid-request used to look like the same thing and
    is not: a selection is bound to the experiment that made it, so a later
    configuration -- a Disable, or the next experiment -- leaves it alone and
    this request would now be complete on purpose.
    """
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    # Advance only the measurement modules' clocks, leaving asyncio and the
    # exporter thread's waits on real time. Job admission cannot spend the run.
    clock_offset = 0.0
    clock = SimpleNamespace(
        monotonic=lambda: time.monotonic() + clock_offset,
        monotonic_ns=lambda: time.monotonic_ns() + int(clock_offset * 1_000_000_000),
        time=lambda: time.time() + clock_offset,
        sleep=time.sleep,
    )
    monkeypatch.setattr(operation_profiling, "time", clock)
    monkeypatch.setattr(operation_measurement, "time", clock)
    run = _enable(monkeypatch)
    selected_run = operation_profiling.active_run()
    assert selected_run is not None

    # The run lapses mid-request: the deadline judgement has to run before the
    # response body is built, or the response claims a sample the record denies.
    original = sandbox_manager.files

    async def _outlive_the_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal clock_offset
        clock_offset = selected_run.deadline_mono - time.monotonic() + 1.0
        return await original(*args, **kwargs)

    monkeypatch.setattr(sandbox_manager, "files", _outlive_the_run)

    response = await client.get(f"/v1/agent/jobs/{job_id}/files")

    assert response.status_code == 200
    assert run["expires_at"]
    emitted = records()
    # Whatever was collected is reported as partial, identically in both places.
    assert "profiling" in response.json(), "the request was selected before it lapsed"
    assert response.json()["profiling"]["collection_complete"] is False
    assert emitted[0]["collection_complete"] is False


async def test_a_disable_does_not_truncate_an_in_flight_request(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """Disabling collection stops selecting; it does not cut a request short.

    Only Admin control changes configuration at runtime, so that is the mode
    this happens in: the sandbox manager's own copy of the configuration is replaced
    mid-request, which is exactly what a Disable does. The request keeps its own
    experiment and its own deadline and finishes complete -- and the sandbox manager,
    which has just been told to stop selecting, still answers the fragment this
    request had already started.
    """
    _enable_admin(monkeypatch)
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    original = sandbox_manager.files

    async def _disable_then_read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        # The whole deployment stops selecting, including the sandbox manager's copy.
        operation_profiling.policy_store().publish(
            operation_profiling.ControlState(
                mode=operation_profiling.MODE_ADMIN,
                config_version=2,
                run=None,
                stop_reason="stopped",
            )
        )
        return await original(*args, **kwargs)

    monkeypatch.setattr(sandbox_manager, "files", _disable_then_read)

    response = await client.get(f"/v1/agent/jobs/{job_id}/files")

    assert response.status_code == 200
    block = response.json()["profiling"]
    assert block is not None, "the selection was made before the change"
    assert block["fragment_recorded"] is True, "the fragment still belonged to it"
    assert block["collection_complete"] is True
    assert records()[0]["collection_complete"] is True
    # And a request arriving after the Disable is not selected at all.
    operation_profiling.reset_cache()
    sandbox_manager.files = original
    after = await client.get(f"/v1/agent/jobs/{job_id}/files")
    assert "profiling" not in after.json()


# ---------------------------------------------------------------------------
# R5: a subdirectory is a listing, and only a file is an open
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("operations", "path", "directories", "expected"),
    [
        # A run that selects only listings must still measure an expansion.
        (["files.list"], "", {}, "files.list"),
        (["files.list"], "src", {"src": "directory"}, "files.list"),
        # ...and must not measure a file preview it did not ask for.
        (["files.list"], "README.md", {}, None),
        (["files.list", "files.open"], "", {}, "files.list"),
        (["files.list", "files.open"], "src", {"src": "directory"}, "files.list"),
        (["files.list", "files.open"], "README.md", {}, "files.open"),
        # A run that selects only previews must not absorb directory listings.
        (["files.open"], "README.md", {}, "files.open"),
        (["files.open"], "src", {"src": "directory"}, None),
        (["files.open"], "", {}, None),
    ],
)
async def test_a_subdirectory_is_a_listing_and_a_file_is_an_open(
    client: AsyncClient,
    store: FakeStorage,
    monkeypatch,
    records,
    _running_writer,
    operations: list[str],
    path: str,
    directories: dict[str, str],
    expected: str | None,
):
    """The request's own result decides the operation, not the path's shape.

    The review found the opposite: both the control plane and the sandbox manager
    classified by "is the path empty", so expanding ``src`` was filed as
    ``files.open`` and an experiment that selected only ``files.list`` lost
    every expansion -- while an experiment that selected only ``files.open``
    absorbed directory listings into its file-preview distribution.
    """
    _enable(monkeypatch, operations=operations, target={"sandbox_id": SANDBOX_ID})
    sandbox_manager = _RecordingClient(directories=directories)
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    response = await client.get(
        f"/v1/agent/jobs/{job_id}/files", params={"path": path} if path else None
    )

    assert response.status_code == 200
    if expected is None:
        assert "profiling" not in response.json()
        assert records() == []
        await asyncio.sleep(0.02)
        assert store.rows == []
        return
    assert response.json()["profiling"]["operation"] == expected
    emitted = records()
    assert len(emitted) == 1
    assert emitted[0]["operation"] == expected
    # The kind the read reported is recorded as a fact, and a listing carries
    # a count rather than the content statistics a preview would.
    assert emitted[0]["facts"]["kind"] == ("directory" if expected == "files.list" else "file")
    assert emitted[0]["facts"]["request_kind"] == expected
    await _drain(_running_writer, 1)
    assert [row["operation"] for row in store.rows] == [expected]


# ---------------------------------------------------------------------------
# A read's whole duration belongs to the stage that started with it
# ---------------------------------------------------------------------------


async def test_a_slow_read_is_charged_to_the_stage_that_encloses_it(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """The stage a request is timed as starts before the call, not after it.

    Timed the other way round, a 50 ms read produced a stage of 84 µs with a
    50 ms sandbox manager child hanging under it and a 1 µs "sandbox_manager_call" beside it --
    the total was right and the attribution was wrong, which is the one thing
    this feature exists to answer.
    """
    _enable(monkeypatch)
    delay_s = 0.05
    sandbox_manager = _RecordingClient(delay_s=delay_s)
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    response = await client.get(f"/v1/agent/jobs/{job_id}/files", params={"path": "README.md"})

    assert response.status_code == 200
    record = records()[0]
    read = next(stage for stage in record["stages"] if stage["stage"] == "read")

    # The stage covers the injected delay, and it begins at the start of the
    # request rather than at the moment the response arrived.
    assert read["duration_us"] >= delay_s * 1_000_000
    assert read["start_offset_us"] < delay_s * 1_000_000
    # The round trip this process measured is charged to the same stage, and it
    # too covers the delay.
    round_trip = next(
        child for child in read["children"] if child["stage"] == "sandbox_manager_call"
    )
    assert round_trip["duration_us"] >= delay_s * 1_000_000
    # The sandbox manager's own work is a child of the stage, not a sibling of it.
    assert "exec" in {child["stage"] for child in read["children"]}
    # No stage was created after the call returned: the read is the only one.
    assert [stage["stage"] for stage in record["stages"]] == ["read"]
    # The stage's duration is not the whole record's -- the record also ends
    # after the response is built -- but it is the same order of magnitude
    # rather than four orders below it.
    assert read["duration_us"] >= record["duration_us"] * 0.5


async def test_the_read_stage_is_named_before_the_kind_is_known(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """One stage name whatever the result was, so the two remain comparable."""
    _enable(monkeypatch)
    sandbox_manager = _RecordingClient(directories={"src": "directory"})
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    listing = await client.get(f"/v1/agent/jobs/{job_id}/files", params={"path": "src"})
    opened = await client.get(f"/v1/agent/jobs/{job_id}/files", params={"path": "README.md"})

    assert listing.status_code == 200 and opened.status_code == 200
    emitted = records()
    assert [record["operation"] for record in emitted] == ["files.list", "files.open"]
    for record in emitted:
        assert [stage["stage"] for stage in record["stages"]] == ["read"]


# ---------------------------------------------------------------------------
# An unusable continuation reference is not a licence to sample afresh
# ---------------------------------------------------------------------------


async def test_an_evicted_reference_is_not_replaced_by_a_fresh_interaction(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """A reference the process no longer holds stops collection, not continues it."""
    _enable(monkeypatch)
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    first = await client.get(f"/v1/agent/jobs/{job_id}/files")
    interaction_id = first.json()["profiling"]["interaction_id"]
    # The selection table is bounded and TTL'd; an eviction is what a browser
    # sees when it keeps a tab open past either bound.
    reset_selections()

    second = await client.get(
        f"/v1/agent/jobs/{job_id}/files",
        headers={INTERACTION_HEADER: interaction_id},
    )

    assert second.status_code == 200
    assert "profiling" not in second.json()
    assert [record["measurement_id"] for record in records()] == [
        first.json()["profiling"]["measurement_id"]
    ]


async def test_a_continuation_whose_host_moved_is_refused_without_a_target(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """An unfiltered run does not license recording a different machine.

    The interaction was established on one host and one sandbox. A run that
    does not filter by them still cannot treat a request served by a different
    machine as a continuation, because the record would attribute this
    request's time to the machine that served the first one.
    """
    _enable(monkeypatch)
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    first = await client.get(f"/v1/agent/jobs/{job_id}/files")
    interaction_id = first.json()["profiling"]["interaction_id"]
    store.jobs[job_id]["thread_sandbox"].update({"host": "moved-host", "sandbox_id": "asbx_moved"})

    second = await client.get(
        f"/v1/agent/jobs/{job_id}/files",
        headers={INTERACTION_HEADER: interaction_id},
    )

    assert second.status_code == 200
    assert "profiling" not in second.json()
    emitted = records()
    assert len(emitted) == 1
    assert emitted[0]["facts"]["host"] == SANDBOX_MANAGER_HOST
    assert emitted[0]["facts"]["sandbox_id"] == SANDBOX_ID


async def test_a_job_scoped_run_continues_the_interaction_it_started(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """The chat id lives on the scope, and that is where the check reads it.

    Read from the facts instead, a run scoped by ``job_id`` rejected every
    legitimate continuation -- then started a fresh interaction, so the two
    requests of one terminal open could never be correlated.
    """
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)
    # The run is scoped by the chat this route resolves, which is the case that
    # used to be rejected: the id lives on the scope, not in its facts.
    _enable(monkeypatch, target={"job_id": job_id})

    first = await client.get(f"/v1/agent/jobs/{job_id}/terminals")
    assert first.status_code == 200
    interaction_id = first.json()["profiling"]["interaction_id"]
    second = await client.get(
        f"/v1/agent/jobs/{job_id}/terminals",
        headers={INTERACTION_HEADER: interaction_id},
    )

    assert second.status_code == 200
    assert second.json()["profiling"]["interaction_id"] == interaction_id
    assert (
        second.json()["profiling"]["measurement_id"]
        != (first.json()["profiling"]["measurement_id"])
    )
    assert len(records()) == 2


# ---------------------------------------------------------------------------
# The control plane stores what it roots, without calling itself
# ---------------------------------------------------------------------------


class _PersistingStorage(FakeStorage):
    """The route fake, plus the measurement capability the store reaches for."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []

    async def record_operation_measurement(self, *, measurement: Any, run: Any = None) -> str:
        """Keep one row, as the repository would."""
        if run is not None:
            self.runs.append(run)
        self.rows.append(dict(measurement))
        return "stored"


async def test_a_file_read_is_stored_by_the_control_plane(
    client: AsyncClient, store: FakeStorage, monkeypatch, _running_writer
):
    """The record a browser's request produced reaches the table.

    The control plane roots these operations itself, so it persists them
    in-process: routing them through its own endpoint would be an HTTP call
    from a service to itself, with a second failure mode and a second
    serialization of a record it already holds.
    """
    _enable(monkeypatch, target={"sandbox_id": SANDBOX_ID})
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    response = await client.get(f"/v1/agent/jobs/{job_id}/files", params={"path": "README.md"})

    assert response.status_code == 200
    await _drain(_running_writer, 1)
    assert len(store.rows) == 1
    row = store.rows[0]
    # The same identity the client was handed, not a second one minted for
    # storage.
    assert row["measurement_id"] == response.json()["profiling"]["measurement_id"]
    assert row["operation"] == "files.open"
    assert row["request_kind"] == "files.open"
    assert row["coverage_scope"] == "server_request"
    assert row["job_id"] == job_id
    assert row["owner_id"] == store.jobs[job_id]["user_id"]
    # The experiment travels with it, so the catalogue can explain the sample.
    assert store.runs and store.runs[0]["run_id"] == "cp-run-1"


async def test_a_terminal_open_is_stored_once_per_request(
    client: AsyncClient, store: FakeStorage, monkeypatch, _running_writer
):
    """A list and a create are two records, each with its own request kind."""
    _enable(monkeypatch)
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    listing = await client.get(f"/v1/agent/jobs/{job_id}/terminals")
    interaction = listing.json()["profiling"]["interaction_id"]
    await client.post(
        f"/v1/agent/jobs/{job_id}/terminals",
        json={"rows": 24, "cols": 80},
        headers={INTERACTION_HEADER: interaction},
    )

    await _drain(_running_writer, 2)
    assert [row["request_kind"] for row in store.rows] == ["terminal.list", "terminal.create"]
    assert {row["operation"] for row in store.rows} == {"terminal.open"}
    # One interaction, two records, two measurements.
    assert len({row["measurement_id"] for row in store.rows}) == 2


async def test_a_terminal_attach_is_stored_while_its_stream_is_still_open(
    client: AsyncClient, store: FakeStorage, monkeypatch, _running_writer
):
    """A long-lived SSE must not postpone its short attach measurement."""
    release = asyncio.Event()

    class _HeldBytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            await release.wait()
            if False:  # pragma: no cover - makes this an async generator
                yield b""

        async def aclose(self) -> None:
            """Release no external resource."""

    class _HeldSandboxManager(_RecordingClient):
        async def stream_terminal(
            self,
            workspace_id: str,
            terminal_id: str,
            *,
            after: int,
            profiling: dict[str, Any] | None = None,
        ) -> Any:
            del terminal_id, after
            self.calls.append({"op": "stream_terminal", "workspace_id": workspace_id})
            call = self._open(profiling, "terminal.open")
            call.set_operation("terminal.open")
            with call.stage("stream_attach"):
                pass
            response = httpx.Response(
                200,
                headers=call.headers(),
                stream=_HeldBytes(),
            )
            return SandboxManagerStream(
                _UnusedClient(),
                response,
                profiling_answer=_answer(response.headers),
            )

    _enable(monkeypatch)
    _use_sandbox_manager(monkeypatch, _HeldSandboxManager())
    job_id = await _ready_job(client, store)
    request = asyncio.create_task(
        client.get(f"/v1/agent/jobs/{job_id}/terminals/term_measured/stream")
    )
    try:
        await _drain(_running_writer, 1)
        assert [row["request_kind"] for row in store.rows] == ["terminal.stream_attach"]
        assert not request.done(), "the stream itself is intentionally still open"
    finally:
        release.set()
    response = await request
    assert response.status_code == 200


async def test_a_request_that_was_not_selected_stores_nothing(
    client: AsyncClient, store: FakeStorage, monkeypatch, _running_writer
):
    """Off means no row, not an empty one."""
    sandbox_manager = _RecordingClient()
    _use_sandbox_manager(monkeypatch, sandbox_manager)
    job_id = await _ready_job(client, store)

    response = await client.get(f"/v1/agent/jobs/{job_id}/files")

    assert response.status_code == 200
    # Nothing was selected, so nothing was even offered.
    await asyncio.sleep(0.05)
    assert store.rows == []
    assert store.runs == []


async def test_a_request_that_failed_is_stored_too(
    client: AsyncClient, store: FakeStorage, monkeypatch, _running_writer
):
    """A failed request is the sample an operator most wants.

    It is the only one that says where the time went before the failure.
    Recording only successes leaves the distribution describing a system that
    never fails -- and the 503 here is a business failure the route answers,
    not a broken measurement.
    """
    _enable(monkeypatch)
    _use_sandbox_manager(
        monkeypatch, _RecordingClient(fail=SandboxManagerError(503, "unavailable"))
    )
    job_id = await _ready_job(client, store)

    response = await client.get(f"/v1/agent/jobs/{job_id}/files")

    assert response.status_code == 503
    await _drain(_running_writer, 1)
    assert len(store.rows) == 1
    row = store.rows[0]
    assert row["outcome"] == "failed"
    assert row["operation"] == "files.list"
    assert row["job_id"] == job_id
    # An incomplete sample is stored as incomplete, never dressed up as whole.
    assert row["collection_complete"] is False
    assert row["end_to_end_complete"] is False


async def test_a_failed_non_root_read_is_not_misfiled_as_list_or_open(
    client: AsyncClient, store: FakeStorage, monkeypatch, _running_writer
):
    """Without a returned kind, neither candidate operation is evidence."""
    _enable(monkeypatch)
    _use_sandbox_manager(
        monkeypatch,
        _RecordingClient(fail=SandboxManagerError(503, "unavailable")),
    )
    job_id = await _ready_job(client, store)

    response = await client.get(
        f"/v1/agent/jobs/{job_id}/files",
        params={"path": "unknown"},
    )

    assert response.status_code == 503
    await asyncio.sleep(0.02)
    assert store.rows == []
    assert _running_writer.counts.reasons["ambiguous_file_kind"] == 1


async def test_response_validation_failure_cannot_be_stored_as_success(
    store: FakeStorage, monkeypatch, _running_writer
):
    """A malformed sandbox manager answer is a failed operation, not a percentile sample."""

    class _InvalidTerminal(_RecordingClient):
        async def create_terminal(
            self,
            workspace_id: str,
            *,
            rows: int,
            cols: int,
            profiling: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            del workspace_id, rows, cols
            call = self._open(profiling, "terminal.open")
            call.set_operation("terminal.open")
            return call.answer(
                {
                    **self._descriptor,
                    "id": "invalid-id",
                }
            )

    _enable(monkeypatch)
    _use_sandbox_manager(monkeypatch, _InvalidTerminal())
    async with AsyncClient(
        transport=ASGITransport(app=_build_app(store), raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        job_id = await _ready_job(client, store)
        response = await client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )

    assert response.status_code == 500
    await _drain(_running_writer, 1)
    assert len(store.rows) == 1
    assert store.rows[0]["outcome"] == "failed"
    assert store.rows[0]["record"]["failure_stage"] == "terminal.create"


async def test_a_queue_with_no_room_drops_the_record_and_keeps_answering(
    client: AsyncClient, store: FakeStorage, monkeypatch, _running_writer
):
    """Congestion costs coverage, never the request.

    The write is the diagnostic; the file was read and the terminal answered
    regardless. A full queue drops and counts, which is what stops a slow
    database from turning every request into another pending write.
    """
    _enable(monkeypatch)
    _use_sandbox_manager(monkeypatch, _RecordingClient())
    job_id = await _ready_job(client, store)
    # A zero-capacity writer models a destination whose bounded queue has no
    # room. It remains running so this tests fullness, not shutdown.
    _running_writer._max_count = 0
    _running_writer.counts.dropped_full = 0

    for _ in range(4):
        response = await client.get(f"/v1/agent/jobs/{job_id}/files")
        assert response.status_code == 200
        assert response.json()["profiling"]["measurement_id"]

    # Every offer was refused by the count bound and recorded as such.
    assert _running_writer.counts.dropped_full == 4
    assert store.rows == []
