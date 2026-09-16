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

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from cloud_agent import broker_routing
from cloud_agent.operation_measurement import (
    CONTINUATION_HEADER,
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
        "measurement_id": "msm_1",
        "operation": "files.list",
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

    def __init__(self, *, fail: WorkspaceBrokerError | None = None) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []
        self._descriptor = {
            "id": "term_measured",
            "shell": "/bin/bash",
            "state": "running",
            "cwd": "/workspace",
            "rows": 24,
            "cols": 80,
            "last_seq": 0,
        }

    def _open(self, context: Any, operation: str) -> operation_call.BrokerCall:
        """Validate the inherited selection as the broker route does."""
        call = operation_call.open_call(context, operation=operation)
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
        call = self._open(profiling, "files.open" if path else "files.list")
        if self.fail is not None:
            raise self.fail
        with call.stage("exec"):
            pass
        body = (
            {"path": path, "kind": "directory", "entries": [], "writable": True}
            if not path
            else {"path": path, "kind": "file", "content": "hello", "size": 5}
        )
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
        if self.fail is not None:
            raise self.fail
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
        with call.stage("stream_attach"):
            pass
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
    # Byte-for-byte the pre-existing shape: no `profiling` key at all, and no
    # continuation header, so a client that never heard of this feature sees
    # nothing new.
    assert listing.json() == {
        "path": "",
        "kind": "directory",
        "entries": [],
        "writable": True,
        "binary": False,
        "truncated": False,
        "source": "workspace",
    }
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
    assert block["recorded"] is True
    assert block["measurement_id"].startswith("msm_")

    emitted = records()
    assert len(emitted) == 1
    record = emitted[0]
    assert record["schema_version"] == 2
    assert record["operation"] == "files.open"
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
    read = next(stage for stage in record["stages"] if stage["stage"] == "read")
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
    measurement_id = listing.json()["profiling"]["measurement_id"]
    created = await client.post(
        f"/v1/agent/jobs/{job_id}/terminals",
        json={"rows": 24, "cols": 80},
        headers={CONTINUATION_HEADER: measurement_id},
    )
    streamed = await client.get(
        f"/v1/agent/jobs/{job_id}/terminals/term_measured/stream",
        headers={CONTINUATION_HEADER: measurement_id},
    )

    assert created.status_code == 200
    assert created.json()["profiling"]["measurement_id"] == measurement_id
    assert streamed.status_code == 200
    # The stream's own answer travels in response headers, not in its body.
    assert streamed.headers["x-agent-profiling-measurement-id"] == measurement_id

    emitted = records()
    assert {record["measurement_id"] for record in emitted} == {measurement_id}
    # The first request of an interaction is not a continuation of anything.
    assert [record["facts"].get("continued_requests", 0) for record in emitted] == [0, 1, 2]
    for record in emitted:
        assert record["operation"] == "terminal.open"
        assert record["measurement_complete"] is True
    # Each request's own broker fragment attached to that request's stage.
    by_stage = {stage["stage"]: stage for record in emitted for stage in record["stages"]}
    assert {"list", "create", "stream"} <= set(by_stage)
    for name in ("list", "create", "stream"):
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
    measurement_id = first.json()["profiling"]["measurement_id"]
    second = await client.get(
        f"/v1/agent/jobs/{job_id}/terminals",
        headers={CONTINUATION_HEADER: measurement_id},
    )

    assert second.status_code == 200
    assert second.json()["profiling"]["measurement_id"] == measurement_id
    emitted = records()
    assert len(emitted) == 2
    # Both are real measurements of this interaction, not one empty record.
    for record in emitted:
        assert record["measurement_complete"] is True
        listing = next(item for item in record["stages"] if item["stage"] == "list")
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
    measurement_id = first.json()["profiling"]["measurement_id"]
    foreign = await client.get(
        f"/v1/agent/jobs/{other}/files",
        headers={CONTINUATION_HEADER: measurement_id},
    )

    assert foreign.status_code == 200
    second = foreign.json()["profiling"]["measurement_id"]
    assert second != measurement_id

    by_id = {record["measurement_id"]: record for record in records()}
    assert set(by_id) == {measurement_id, second}
    # The id did not carry the other chat's selection across: the second
    # request was measured, if at all, as its own chat's operation.
    assert by_id[second]["facts"]["job_id"] != by_id[measurement_id]["facts"]["job_id"]


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
        headers={CONTINUATION_HEADER: "not-a-measurement-id"},
    )

    assert listed.status_code == 200
    assert "profiling" in listed.json()
    assert listed.json()["profiling"]["recorded"] is True


# ---------------------------------------------------------------------------
# The broker's half of the protocol
# ---------------------------------------------------------------------------


def test_a_broker_refuses_a_selection_for_another_operation(monkeypatch):
    """A fragment is never filed under an operation this route does not own."""
    _enable(monkeypatch)
    context = _context(operation="terminal.open")

    assert operation_call.open_call(context, operation="files.open").recording is False
    assert operation_call.open_call(context, operation="terminal.open").recording is True


def test_a_broker_refuses_an_operation_its_own_run_does_not_select(monkeypatch):
    """The broker's own run decides, whatever a caller claims was selected."""
    _enable(monkeypatch, operations=["terminal.open"])

    assert (
        operation_call.open_call(_context(operation="files.open"), operation="files.open").recording
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
        assert operation_call.open_call(context, operation="files.open").recording is False

    # Decodable, and refused because the experiment it names has already lapsed.
    expired = json.dumps(_context(expires_at=1))
    assert operation_profiling.parse_context(expired) is not None
    assert operation_call.open_call(expired, operation="files.open").recording is False

    # Decodable, but scoped to an experiment this process is not running.
    foreign = json.dumps(_context(run_id="another-run"))
    assert operation_profiling.parse_context(foreign) is not None
    assert operation_call.open_call(foreign, operation="files.open").recording is False


def test_a_streaming_answer_round_trips_through_headers(monkeypatch):
    """A call whose body is terminal bytes still answers, in headers."""
    _enable(monkeypatch)
    call = operation_call.open_call(
        _context(operation="terminal.open", call_id="c7"), operation="terminal.open"
    )
    with call.stage("stream_attach"):
        pass

    headers = call.headers(extra={"Cache-Control": "no-cache"})

    assert headers["Cache-Control"] == "no-cache"
    assert operation_profiling.answer_from_headers(headers) == {
        "profiling_call_id": "c7",
        "profiling_recorded": True,
        "stages": [{"name": "stream_attach", "duration_us": 0, "outcome": "ok"}],
    }
    # A response that answers nothing leaves the fragment unanswered.
    assert operation_profiling.answer_from_headers({"Cache-Control": "no-cache"}) is None


def test_an_unselected_broker_call_records_and_answers_nothing(monkeypatch):
    """Off means off on the broker too, down to the response headers."""
    call = operation_call.open_call(None, operation="files.open")

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
