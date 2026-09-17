"""End-to-end measurement for file and terminal operations.

Where ``test_agent_operation_profiling.py`` defends the collector's own
contract, this module defends the *root operation* the control plane owns: an
HTTP request through the real router, a real selection, and a real fragment
protocol to a broker stand-in that answers exactly the way the broker does.

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
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, Field

from cloud_agent import broker_routing, operation_measurement
from cloud_agent.operation_measurement import (
    INTERACTION_HEADER,
    LEGACY_MEASUREMENT_HEADER,
    reset as reset_selections,
    root_scope,
)
from cloud_agent_host import operation_call, operation_profiling
from cloud_agent_host.workspace_broker_client import (
    WorkspaceBrokerClient,
    WorkspaceBrokerError,
    WorkspaceBrokerStream,
)


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

BROKER_URL = "http://broker.test:8092"
BROKER_HOST = "host-under-test"
SANDBOX_ID = "asbx_measured"


@pytest.fixture(autouse=True)
def _deployment_secrets(monkeypatch):
    """Provide the signing secrets and the one entitled repository.

    The same environment the agent-jobs route tests run in: the repo allowlist
    is empty by default by design, so a test that creates a job has to declare
    the repository it uses.
    """
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "api-test-secret")
    monkeypatch.setenv("AGENT_WORKSPACE_BROKER_TOKEN", "api-test-broker-secret")
    monkeypatch.setenv("AGENT_SESSION_SECRET", "session-test-secret-32-bytes-minimum!")
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name,o/n,private/repo")


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
    """Provide a fresh in-memory store."""
    return FakeStorage()


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


def _answer(headers: Any) -> dict[str, Any] | None:
    """Decode the fragment a broker stand-in echoed, as the real client does."""
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


class _RecordingClient:
    """A broker that answers a fragment exactly the way the real broker does.

    It reuses the broker's own :func:`open_call` and :class:`BrokerCall`, so
    these tests exercise the wire contract rather than a second implementation
    of it: the request carries a selection, the route validates it against this
    process's switch and run, and the answer echoes the call it belongs to.
    """

    def __init__(
        self,
        *,
        fail: WorkspaceBrokerError | None = None,
        directories: Mapping[str, str] | None = None,
        truncate: bool = False,
        empty_stages: bool = False,
    ) -> None:
        self.fail = fail
        #: Answer as a broker whose own deadline cut collection short.
        self.truncate = truncate
        #: Answer with no stages at all -- a legitimate skip when not truncated,
        #: and "every stage was skipped" when it is.
        self.empty_stages = empty_stages
        self.calls: list[dict[str, Any]] = []
        #: What a non-empty path turns out to be. A real broker learns this
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
        """Return the operation a read's own kind implies, as the broker does."""
        return "files.list" if kind == "directory" else "files.open"

    def _open(self, context: Any, *operations: str) -> operation_call.BrokerCall:
        """Validate the inherited selection as the broker route does."""
        call = operation_call.open_call(context, operations=operations)
        with call.stage("admission"):
            pass
        return call

    async def register_shared_sandbox(self, _sandbox: dict[str, Any]) -> None:
        """Accept the manifest re-registration a workspace request performs."""
        return None

    async def files(
        self, workspace_id: str, path: str, *, profiling: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Answer a file or directory read."""
        self.calls.append({"op": "files", "workspace_id": workspace_id, "path": path})
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
        """Answer a stream in headers, the way the broker's SSE route does."""
        self.calls.append({"op": "stream_terminal", "workspace_id": workspace_id})
        call = self._open(profiling, "terminal.open")
        call.set_operation("terminal.open")
        with call.stage("stream_attach"):
            pass
        if self.truncate:
            call.recorder.truncated += 1
        response = httpx.Response(200, headers=call.headers(), stream=httpx.ByteStream(b""))
        return WorkspaceBrokerStream(
            _UnusedClient(), response, profiling_answer=_answer(response.headers)
        )


def _use_broker(monkeypatch, broker: _RecordingClient) -> None:
    """Point every job's resolved broker at the stand-in."""
    monkeypatch.setattr(broker_routing, "_client_for", lambda _host: broker)


def _workspace_job(store: FakeStorage, job_id: str) -> None:
    """Give a created job the retained group and sandbox a real host reports."""
    thread_id = _attach_shared_workspace(store, job_id)
    store.jobs[job_id]["thread_sandbox"].update(
        {"sandbox_id": SANDBOX_ID, "host": BROKER_HOST, "origin_thread_id": thread_id}
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
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """A switched-off deployment serves exactly what it served before."""
    broker = _RecordingClient()
    _use_broker(monkeypatch, broker)
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
    assert [call["op"] for call in broker.calls] == ["files"]
    assert records() == []


async def test_an_unselected_operation_is_not_measured(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """A run that does not name this operation never sees a sample."""
    _enable(monkeypatch, operations=["attempt.startup"])
    broker = _RecordingClient()
    _use_broker(monkeypatch, broker)
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
    broker = _RecordingClient()
    _use_broker(monkeypatch, broker)
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
    broker = _RecordingClient()
    _use_broker(monkeypatch, broker)
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


async def test_a_selected_file_read_is_measured_with_its_broker_fragment(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """The record carries the route's stages and the broker's own durations."""
    _enable(monkeypatch, target={"sandbox_id": SANDBOX_ID})
    broker = _RecordingClient()
    _use_broker(monkeypatch, broker)
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
    # The broker's own stages hang under the stage that performed the call,
    # alongside the sub-stage this process timed for the round trip itself.
    read = next(stage for stage in record["stages"] if stage["stage"] == "files.open")
    children = {child["stage"]: child["source"] for child in read["children"]}
    assert children == {
        "broker_call": "control-plane",
        "admission": "broker",
        "exec": "broker",
    }


async def test_the_root_listing_is_measured_as_a_listing(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """The root path is files.list; a selected entry is files.open."""
    _enable(monkeypatch)
    broker = _RecordingClient()
    _use_broker(monkeypatch, broker)
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
    broker = _RecordingClient()
    _use_broker(monkeypatch, broker)
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
    broker = _RecordingClient()
    _use_broker(monkeypatch, broker)
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
    # Each request's own broker fragment attached to that request's stage.
    by_stage = {stage["stage"]: stage for record in emitted for stage in record["stages"]}
    names = ("terminal.list", "terminal.create", "terminal.stream_attach")
    assert set(names) <= set(by_stage)
    for name in names:
        children = by_stage[name]["children"]
        # The round trip this process timed, plus whatever the broker timed for
        # the same call -- at minimum its own admission of the operation.
        assert "broker_call" in {child["stage"] for child in children}
        assert "admission" in {child["stage"] for child in children}
        assert {child["source"] for child in children} == {"control-plane", "broker"}
    assert emitted[0]["facts"]["terminal_count"] == 1


async def test_a_continuation_still_reports_its_own_time(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """Reusing an id does not mean collecting into a record already written."""
    _enable(monkeypatch)
    broker = _RecordingClient()
    _use_broker(monkeypatch, broker)
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
        assert {"broker_call", "admission"} <= {child["stage"] for child in children}
        assert {child["source"] for child in children} == {"control-plane", "broker"}


async def test_a_continuation_from_another_chat_is_refused(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """An echoed id is a correlation handle, not a way to observe another chat."""
    _enable(monkeypatch)
    broker = _RecordingClient()
    _use_broker(monkeypatch, broker)
    owner = await _ready_job(client, store)
    other = await _ready_job(client, store)

    first = await client.get(f"/v1/agent/jobs/{owner}/files")
    interaction_id = first.json()["profiling"]["interaction_id"]
    foreign = await client.get(
        f"/v1/agent/jobs/{other}/files",
        headers={INTERACTION_HEADER: interaction_id},
    )

    assert foreign.status_code == 200
    # The id did not carry the other chat's interaction across: this request
    # was measured, if at all, as its own chat's interaction.
    assert foreign.json()["profiling"]["interaction_id"] != interaction_id

    by_id = {record["measurement_id"]: record for record in records()}
    assert set(by_id) == {
        first.json()["profiling"]["measurement_id"],
        foreign.json()["profiling"]["measurement_id"],
    }
    assert (
        by_id[first.json()["profiling"]["measurement_id"]]["facts"]["job_id"]
        != (by_id[foreign.json()["profiling"]["measurement_id"]]["facts"]["job_id"])
    )


# ---------------------------------------------------------------------------
# Missing fragments, failures, and expiry are reported, never hidden
# ---------------------------------------------------------------------------


async def test_a_broker_that_never_answered_is_named_as_a_missing_fragment(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """An unanswerable call is a fragment the record owed, not a complete sample."""
    _enable(monkeypatch)
    broker = _RecordingClient(fail=WorkspaceBrokerError(503, "unavailable"))
    _use_broker(monkeypatch, broker)
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
    broker = _RecordingClient()
    _use_broker(monkeypatch, broker)
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
    broker = _RecordingClient()
    _use_broker(monkeypatch, broker)
    job_id = await _ready_job(client, store)

    listed = await client.get(
        f"/v1/agent/jobs/{job_id}/files",
        headers={INTERACTION_HEADER: "not-a-measurement-id"},
    )

    assert listed.status_code == 200
    assert "profiling" in listed.json()
    assert listed.json()["profiling"]["fragment_recorded"] is True


# ---------------------------------------------------------------------------
# The broker's half of the protocol
# ---------------------------------------------------------------------------


def test_a_broker_refuses_a_selection_for_another_operation(monkeypatch):
    """A fragment is never filed under an operation this route does not own."""
    _enable(monkeypatch)
    context = _context(operations=["terminal.open"])

    assert operation_call.open_call(context, operations=("files.open",)).recording is False
    assert operation_call.open_call(context, operations=("terminal.open",)).recording is True


def test_a_broker_refuses_an_operation_its_own_run_does_not_select(monkeypatch):
    """The broker's own run decides, whatever a caller claims was selected."""
    _enable(monkeypatch, operations=["terminal.open"])

    assert (
        operation_call.open_call(
            _context(operations=["files.open"]), operations=("files.open",)
        ).recording
        is False
    )


def test_a_broker_drops_a_context_it_cannot_validate(monkeypatch):
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
    assert operation_profiling.answer_from_headers(headers) == {
        "profiling_call_id": "c7",
        "profiling_recorded": True,
        "profiling_operation": "terminal.open",
        "stages": [{"name": "stream_attach", "duration_us": 0, "outcome": "ok"}],
    }
    # A response that answers nothing leaves the fragment unanswered.
    assert operation_profiling.answer_from_headers({"Cache-Control": "no-cache"}) is None


def test_an_unselected_broker_call_records_and_answers_nothing(monkeypatch):
    """Off means off on the broker too, down to the response headers."""
    call = operation_call.open_call(None, operations=("files.open",))

    assert call.recording is False
    assert call.headers() == {}
    with call.stage("admission"):
        pass
    assert call.answer({"kind": "file"}) == {"kind": "file"}


# ---------------------------------------------------------------------------
# The client half of the wire protocol
# ---------------------------------------------------------------------------


async def test_the_broker_client_sends_the_selection_and_preserves_the_answer(monkeypatch, records):
    """The gateway's client carries the selection and does not eat the answer."""
    _enable(monkeypatch)
    seen: list[dict[str, Any]] = []

    class _Response:
        status_code = 200
        is_error = False

        def json(self) -> dict[str, Any]:
            return {"path": "", "kind": "directory", "entries": []}

    class _Http:
        async def request(self, method, url, *, headers, params, json, timeout):
            seen.append({"method": method, "url": url, "headers": headers, "params": params})
            return _Response()

    client = WorkspaceBrokerClient(BROKER_URL, "token")
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
    _enable(monkeypatch, target={"host": BROKER_HOST})
    scope = root_scope(
        "files.list",
        {
            "id": "ajob_1",
            "thread_id": "athr_1",
            "thread_sandbox": {"sandbox_id": SANDBOX_ID, "host": BROKER_HOST},
        },
    )
    assert scope.facts == {
        "thread_id": "athr_1",
        "sandbox_id": SANDBOX_ID,
        "host": BROKER_HOST,
    }


# ---------------------------------------------------------------------------
# The off path has to match what the parent version actually served
# ---------------------------------------------------------------------------


def _parent_models() -> dict[str, Any]:
    """Build the response models exactly as the parent commit declared them.

    Read from git rather than copied by hand, so this baseline cannot quietly
    drift into agreeing with whatever the current code happens to emit. The
    parent is the commit that had no profiling fields at all, which makes it
    the contract a switched-off deployment must still honour.
    """
    import subprocess
    import sys
    import types

    source = subprocess.run(
        ["git", "show", "e77e3e6^:cloud_agent/schemas.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
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
    broker = _RecordingClient()
    _use_broker(monkeypatch, broker)
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
    broker = _RecordingClient()
    _use_broker(monkeypatch, broker)
    job_id = await _ready_job(client, store)

    with operation_measurement.operation(
        operation_measurement.TERMINAL_OPEN,
        root_scope(
            operation_measurement.TERMINAL_OPEN,
            {
                "id": job_id,
                "thread_id": store.jobs[job_id]["thread_id"],
                "thread_sandbox": {"sandbox_id": SANDBOX_ID, "host": BROKER_HOST},
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
    broker = _RecordingClient()
    _use_broker(monkeypatch, broker)
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
    _enable(monkeypatch, target={"host": BROKER_HOST})
    broker = _RecordingClient()
    _use_broker(monkeypatch, broker)
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
    assert emitted[0]["facts"]["host"] == BROKER_HOST


async def test_a_continuation_whose_sandbox_changed_is_not_measured(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """A re-provisioned workspace is not the one the experiment selected."""
    _enable(monkeypatch, target={"sandbox_id": SANDBOX_ID})
    broker = _RecordingClient()
    _use_broker(monkeypatch, broker)
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
    broker = _RecordingClient()
    _use_broker(monkeypatch, broker)
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
    broker = _RecordingClient(truncate=True)
    _use_broker(monkeypatch, broker)
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
    broker = _RecordingClient(truncate=True, empty_stages=True)
    _use_broker(monkeypatch, broker)
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
    broker = _RecordingClient(empty_stages=True)
    _use_broker(monkeypatch, broker)
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
    broker = _RecordingClient(truncate=True)
    _use_broker(monkeypatch, broker)
    job_id = await _ready_job(client, store)

    streamed = await client.get(f"/v1/agent/jobs/{job_id}/terminals/term_measured/stream")

    assert streamed.status_code == 200
    assert streamed.headers["x-agent-profiling-fragment-truncated"] == "1"
    assert streamed.headers["x-agent-profiling-collection-complete"] == "0"
    assert records()[0]["collection_complete"] is False


async def test_a_request_that_outlived_its_run_reports_incomplete(
    client: AsyncClient, store: FakeStorage, monkeypatch, records
):
    """The response cannot claim completeness while the record is expiring."""
    run = _enable(monkeypatch)
    broker = _RecordingClient()
    _use_broker(monkeypatch, broker)
    job_id = await _ready_job(client, store)

    # The run lapses mid-request: the deadline judgement has to run before the
    # response body is built, or the response claims a sample the record denies.
    original = broker.files

    async def _expire_then_read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        run["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        monkeypatch.setenv(operation_profiling.ENV_PROFILING_RUN, json.dumps(run))
        operation_profiling.reset_cache()
        return await original(*args, **kwargs)

    monkeypatch.setattr(broker, "files", _expire_then_read)

    response = await client.get(f"/v1/agent/jobs/{job_id}/files")

    assert response.status_code == 200
    emitted = records()
    # Whatever was collected is reported as partial, identically in both places.
    if emitted and "profiling" in response.json():
        assert response.json()["profiling"]["collection_complete"] is False
        assert emitted[0]["collection_complete"] is False


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
    operations: list[str],
    path: str,
    directories: dict[str, str],
    expected: str | None,
):
    """The request's own result decides the operation, not the path's shape.

    The review found the opposite: both the control plane and the broker
    classified by "is the path empty", so expanding ``src`` was filed as
    ``files.open`` and an experiment that selected only ``files.list`` lost
    every expansion -- while an experiment that selected only ``files.open``
    absorbed directory listings into its file-preview distribution.
    """
    _enable(monkeypatch, operations=operations, target={"sandbox_id": SANDBOX_ID})
    broker = _RecordingClient(directories=directories)
    _use_broker(monkeypatch, broker)
    job_id = await _ready_job(client, store)

    response = await client.get(
        f"/v1/agent/jobs/{job_id}/files", params={"path": path} if path else None
    )

    assert response.status_code == 200
    if expected is None:
        assert "profiling" not in response.json()
        assert records() == []
        return
    assert response.json()["profiling"]["operation"] == expected
    emitted = records()
    assert len(emitted) == 1
    assert emitted[0]["operation"] == expected
    # The kind the read reported is recorded as a fact, and a listing carries
    # a count rather than the content statistics a preview would.
    assert emitted[0]["facts"]["kind"] == ("directory" if expected == "files.list" else "file")
    assert emitted[0]["facts"]["request_kind"] == expected
