"""What the control plane stores from a producer, and what it refuses.

Two properties are worth more than the rest and neither can be tested from the
producer side alone:

* a record cannot state who owns it. The payload says what was measured, and
  the database row the credential authenticated against says whose history it
  joins -- so a valid token for one job cannot file a sample under another.
* a retry of one record is one sample, while the same id with different
  contents is refused rather than overwritten.

The endpoint is allowed to fail, which is the other half of the design: a
rejected record returns 4xx/5xx to the *producer* and never to the request the
measurement describes, because that request was answered before delivery was
ever attempted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from tests.servers.test_agent_jobs_api import FakeStorage, _build_app, _create_job

from cloud_agent import measurement_ingest
from cloud_agent.measurement_ingest import MeasurementRejected
from cloud_agent.tokens import SCOPE_FULL, mint_worker_token


class _MeasurementStore:
    """An in-memory stand-in for the measurement capability.

    Records what it was handed so a test can assert on the *row*, not only on
    the response: the interesting claim is which owner the control plane
    resolved, and that is not visible in an acknowledgement.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.force: str | None = None

    async def record_operation_measurement(self, *, measurement: Any, run: Any = None) -> str:
        """Store one row unless a test forced a refusal."""
        if self.force is not None:
            return self.force
        existing = next(
            (row for row in self.rows if row["measurement_id"] == measurement["measurement_id"]),
            None,
        )
        if existing is not None:
            if existing["payload_hash"] == measurement["payload_hash"]:
                return "duplicate"
            return "conflict"
        if run is not None:
            self.runs.append(run)
        self.rows.append(dict(measurement))
        return "stored"


@pytest.fixture(autouse=True)
def _deployment_secrets(monkeypatch):
    """The signing secrets and the one entitled repository, as the route tests use."""
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "api-test-secret")
    monkeypatch.setenv("AGENT_WORKSPACE_BROKER_TOKEN", "api-test-broker-secret")
    monkeypatch.setenv("AGENT_SESSION_SECRET", "session-test-secret-32-bytes-minimum!")
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name,o/n,private/repo")


class _MeasuringStorage(FakeStorage):
    """The agent-jobs fake, plus the measurement capability.

    The capability name resolves to the fake itself, so the method has to live
    on it -- a separate object hung off an attribute would never be reached
    through the assembly's capability lookup.
    """

    def __init__(self) -> None:
        super().__init__()
        self.measurements = _MeasurementStore()
        #: Attempts this job is known to have run, by id. A token names one;
        #: only this decides whether it exists and with which generation.
        self.attempts: dict[int, dict[str, Any]] = {
            1: {
                "attempt_id": 1,
                "job_id": "",
                "attempt_no": 1,
                "lease_generation": 1,
                "host_id": "host-e2e",
                "status": "running",
                "thread_sandbox": {"sandbox_id": "asbx_e2e", "host": "host-e2e"},
            }
        }

    async def record_operation_measurement(self, *, measurement: Any, run: Any = None) -> str:
        """Delegate to the in-memory measurement store."""
        return await self.measurements.record_operation_measurement(
            measurement=measurement, run=run
        )

    async def get_attempt(self, *, job_id: str, attempt_id: int) -> dict[str, Any] | None:
        """Return the attempt this job ran, as the history query would.

        A test states which attempts exist; a token naming one that does not is
        exactly the case this lookup is here to refuse.
        """
        del job_id  # the fake holds one job's attempts; the real query scopes it
        return self.attempts.get(attempt_id)


@pytest.fixture
def store() -> FakeStorage:
    """Provide a fresh in-memory store carrying the measurement capability."""
    return _MeasuringStorage()


@pytest_asyncio.fixture()
async def client(store: FakeStorage):
    """Mount the router with auth stubbed, as the agent-jobs route tests do."""
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def _record(**overrides: Any) -> dict[str, Any]:
    """One frozen measurement, shaped as the collector emits it."""
    record: dict[str, Any] = {
        "schema_version": 3,
        "measurement_id": "msm_1",
        "interaction_id": "msm_1",
        "profiling_run_id": "run-1",
        "operation": "attempt.startup",
        "request_kind": "attempt.startup",
        "boundary_version": 1,
        "coverage_scope": "host_startup",
        "start_boundary": "runner.claim.request",
        "end_boundary": "supervisor.prompt.dispatched",
        "producer": "runner",
        "producer_instance_id": "cp-1",
        "duration_us": 1234,
        "outcome": "dispatched",
        "collection_complete": True,
        "end_to_end_complete": False,
        "observed_at": datetime.now(UTC).isoformat(),
        "facts": {"operation": "attempt.startup"},
    }
    record.update(overrides)
    return record


def _manifest(**overrides: Any) -> dict[str, Any]:
    """One experiment manifest, as the descriptor states it."""
    run: dict[str, Any] = {
        "run_id": "run-1",
        "operations": ["attempt.startup"],
        "target": {"sandbox_id": "asbx_1"},
        "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
        "config_version": 1,
        "admission_instances": [{"instance": "cp-1", "max_operations": 200}],
    }
    run.update(overrides)
    return run


def _token_for(job: dict[str, Any], attempt_id: int = 1, generation: int = 1) -> str:
    """Mint the runner's own full-scope capability token for one attempt."""
    return mint_worker_token(
        job_id=str(job["id"]),
        attempt_id=attempt_id,
        lease_generation=generation,
        scope=SCOPE_FULL,
    )


async def _claimed_job(client: AsyncClient, store: FakeStorage) -> dict[str, Any]:
    """Create a job and return it, with the attempt its tokens will name."""
    job_id = await _create_job(client)
    job = store.jobs[job_id]
    store.attempts[1]["job_id"] = job_id
    return job


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_a_measurement_is_stored_under_the_row_it_authenticated_against(
    client: AsyncClient, store: FakeStorage
):
    """Ownership comes from the database, not from the payload."""
    job = await _claimed_job(client, store)

    response = await client.post(
        f"/v1/agent/worker/jobs/{job['id']}/measurements",
        json={"record": _record(), "run": _manifest()},
        headers={"Authorization": f"Bearer {_token_for(job)}"},
    )

    assert response.status_code == 200
    assert response.json() == {"result": "stored", "measurement_id": "msm_1", "reason": None}
    row = store.measurements.rows[0]
    assert row["job_id"] == job["id"]
    assert row["thread_id"] == job["thread_id"]
    assert row["owner_id"] == job["user_id"]
    assert row["attempt_id"] == 1
    assert row["lease_generation"] == 1
    assert store.measurements.runs[0]["run_id"] == "run-1"


async def test_a_payload_cannot_claim_a_different_owner(client: AsyncClient, store: FakeStorage):
    """A record naming its own owner is refused, not believed and not corrected.

    The producer states what it measured; only the row its credential resolved
    says whose history the sample joins. A record that carries those fields is
    not one this deployment stores, so it is refused rather than silently
    rewritten -- which would leave the producer believing its own values had
    been kept.
    """
    job = await _claimed_job(client, store)
    other = store.jobs[await _create_job(client)]

    response = await client.post(
        f"/v1/agent/worker/jobs/{job['id']}/measurements",
        json={
            "record": _record(
                job_id=str(other["id"]),
                owner_id="someone-else",
                thread_id="athr_somewhere_else",
            ),
            "run": _manifest(),
        },
        headers={"Authorization": f"Bearer {_token_for(job)}"},
    )

    # Refused outright rather than quietly rewritten: a record that names an
    # owner is not one this deployment stores, and accepting it after
    # correcting the fields would leave the producer believing its own values
    # had been kept.
    assert response.status_code == 422
    assert "unknown record field" in response.json()["detail"]["error"]["message"]
    assert store.measurements.rows == []


async def test_a_retry_of_one_record_is_one_sample(client: AsyncClient, store: FakeStorage):
    """Delivering the same record twice stores one row and says so."""
    job = await _claimed_job(client, store)
    body = {"record": _record(), "run": _manifest()}
    headers = {"Authorization": f"Bearer {_token_for(job)}"}

    first = await client.post(
        f"/v1/agent/worker/jobs/{job['id']}/measurements", json=body, headers=headers
    )
    second = await client.post(
        f"/v1/agent/worker/jobs/{job['id']}/measurements", json=body, headers=headers
    )

    assert first.json()["result"] == "stored"
    assert second.json()["result"] == "duplicate"
    assert len(store.measurements.rows) == 1


async def test_a_late_measurement_is_accepted_after_its_attempt_ended(
    client: AsyncClient, store: FakeStorage
):
    """A finished attempt may still report what it measured.

    The interesting startup samples come from attempts that failed quickly, and
    they arrive after the attempt is over. Refusing them for want of a live
    lease would discard exactly the samples this exists to collect.
    """
    job = await _claimed_job(client, store)
    store.jobs[job["id"]]["state"] = "failed"

    response = await client.post(
        f"/v1/agent/worker/jobs/{job['id']}/measurements",
        json={"record": _record(), "run": _manifest()},
        headers={"Authorization": f"Bearer {_token_for(job)}"},
    )

    assert response.status_code == 200
    assert response.json()["result"] == "stored"


# ---------------------------------------------------------------------------
# Refusals the producer must not retry
# ---------------------------------------------------------------------------


async def test_a_token_for_another_job_is_refused(client: AsyncClient, store: FakeStorage):
    """A credential is scoped to the job in the path."""
    job = await _claimed_job(client, store)
    other = store.jobs[await _create_job(client)]

    response = await client.post(
        f"/v1/agent/worker/jobs/{other['id']}/measurements",
        json={"record": _record(), "run": _manifest()},
        headers={"Authorization": f"Bearer {_token_for(job)}"},
    )

    assert response.status_code == 409


async def test_an_unusable_record_is_refused_with_a_reason(client: AsyncClient, store: FakeStorage):
    """A record this deployment does not store is a 422, not a silent success."""
    job = await _claimed_job(client, store)
    headers = {"Authorization": f"Bearer {_token_for(job)}"}

    for record in (
        _record(schema_version=99),
        _record(operation="something.unknown"),
        _record(outcome="not-an-outcome"),
        _record(duration_us=-1),
        _record(measurement_id=""),
    ):
        response = await client.post(
            f"/v1/agent/worker/jobs/{job['id']}/measurements",
            json={"record": record},
            headers=headers,
        )
        assert response.status_code == 422, record
        assert response.json()["detail"]["error"]["type"] == "measurement_rejected"
    assert store.measurements.rows == []


async def test_an_id_that_already_names_other_contents_is_a_conflict(
    client: AsyncClient, store: FakeStorage
):
    """The answer is an outcome the producer can act on, not a silent drop."""
    job = await _claimed_job(client, store)
    headers = {"Authorization": f"Bearer {_token_for(job)}"}
    await client.post(
        f"/v1/agent/worker/jobs/{job['id']}/measurements",
        json={"record": _record(), "run": _manifest()},
        headers=headers,
    )

    response = await client.post(
        f"/v1/agent/worker/jobs/{job['id']}/measurements",
        json={"record": _record(duration_us=9999), "run": _manifest()},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["result"] == "conflict"
    assert response.json()["reason"] == "this measurement id already names different contents"
    assert store.measurements.rows[0]["duration_us"] == 1234


async def test_a_second_manifest_for_one_run_is_reported_as_a_run_conflict(
    client: AsyncClient, store: FakeStorage
):
    """The producer is told the experiment id is taken, and does not retry."""
    job = await _claimed_job(client, store)
    store.measurements.force = "run_conflict"

    response = await client.post(
        f"/v1/agent/worker/jobs/{job['id']}/measurements",
        json={"record": _record(), "run": _manifest(target={"host": "other"})},
        headers={"Authorization": f"Bearer {_token_for(job)}"},
    )

    assert response.status_code == 200
    assert response.json()["result"] == "run_conflict"
    assert "different experiment" in response.json()["reason"]


# ---------------------------------------------------------------------------
# The validation rules, at the unit level
# ---------------------------------------------------------------------------


def test_an_unknown_schema_version_is_named_as_such():
    """Not knowing what a record means is different from it being malformed."""
    with pytest.raises(MeasurementRejected, match="unsupported schema_version"):
        measurement_ingest.validate_measurement(_record(schema_version=2))


def test_facts_are_kept_by_whitelist_not_by_denylist():
    """An unrecognised fact name is dropped, which is the direction that fails closed."""
    kept = measurement_ingest.sanitize_facts(
        {
            "kind": "file",
            "entry_count": 2,
            "content": "the whole file",
            "path": "src/secret.py",
            "prompt": "what the user asked",
            "terminal_input": "rm -rf /",
            "env": {"TOKEN": "x"},
            "nested": {"anything": 1},
        }
    )

    assert kept == {"kind": "file", "entry_count": 2}


def test_a_canonical_hash_ignores_key_order():
    """Two spellings of one document must compare equal, or a retry looks like a conflict."""
    assert measurement_ingest.canonical_hash({"a": 1, "b": 2}) == (
        measurement_ingest.canonical_hash({"b": 2, "a": 1})
    )
    assert measurement_ingest.canonical_hash({"a": 1}) != (
        measurement_ingest.canonical_hash({"a": 2})
    )


def test_a_record_over_its_byte_budget_is_refused():
    """The whole-document bound catches what the per-collection caps do not."""
    oversized = _record(facts={"kind": "x" * measurement_ingest.MAX_RECORD_BYTES})

    with pytest.raises(MeasurementRejected, match="exceeds"):
        measurement_ingest.validate_measurement(oversized)


def test_a_record_over_its_collection_caps_is_refused():
    """The collector's own ceilings, not a second copy of the same numbers."""
    too_many_stages = _record(stages=[{"stage": "x"}] * (measurement_ingest.MAX_SPANS + 1))

    with pytest.raises(MeasurementRejected, match="stages exceeds"):
        measurement_ingest.validate_measurement(too_many_stages)


def test_an_offset_timestamp_is_normalized_to_utc():
    """The stored instant is one value however the producer spelled it."""
    beijing = timezone(timedelta(hours=8))
    moment = datetime(2026, 1, 2, 12, 0, tzinfo=beijing)

    stored = measurement_ingest.validate_measurement(_record(observed_at=moment.isoformat()))

    assert stored.observed_at == datetime(2026, 1, 2, 4, 0, tzinfo=UTC)


def test_a_naive_timestamp_is_refused():
    """A timezone-less instant cannot be compared with anything stored."""
    with pytest.raises(MeasurementRejected, match="timezone"):
        measurement_ingest.validate_measurement(_record(observed_at="2026-01-02T12:00:00"))


def test_a_manifest_naming_an_unknown_operation_is_refused():
    """A run this deployment cannot interpret cannot be catalogued."""
    with pytest.raises(MeasurementRejected, match="unknown operations"):
        measurement_ingest.validate_run(_manifest(operations=["teleport.open"]))


@pytest.mark.parametrize(
    "changes",
    [
        {"target": {"unknown": "value"}},
        {"target": {"host": None}},
        {"config_version": "two"},
        {"admission_instances": ["not-an-object"]},
        {"admission_instances": [{"instance": "cp-1", "max_operations": 0}]},
    ],
)
def test_a_malformed_run_manifest_is_refused(changes: dict[str, Any]):
    """Bad manifests are terminal 422s, never uncaught conversion errors."""
    with pytest.raises(MeasurementRejected):
        measurement_ingest.validate_run(_manifest(**changes))


def test_a_stored_record_keeps_no_more_than_the_whitelist_allows():
    """Validation strips before hashing, so the hash describes what is stored."""
    stored = measurement_ingest.validate_measurement(
        _record(facts={"kind": "file", "content": "secret"})
    )

    assert stored.record["facts"] == {"kind": "file"}
    assert stored.payload_hash == measurement_ingest.canonical_hash(stored.record)
    assert "secret" in json.dumps(_record(facts={"kind": "file", "content": "secret"}))
    assert "secret" not in json.dumps(stored.record)


# ---------------------------------------------------------------------------
# The fields a distribution is grouped by have to survive the mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("facts", "field", "expected"),
    [
        # The collector states these inside facts, and a reader that only
        # looked at the top level would file every startup path as null...
        ({"startup_path": "reuse_live"}, "startup_path", "reuse_live"),
        ({"startup_path": "new_group"}, "startup_path", "new_group"),
        # ...and every terminal request as the same empty string.
        ({"request_kind": "terminal.create"}, "request_kind", "terminal.create"),
        ({"request_kind": "terminal.stream_attach"}, "request_kind", "terminal.stream_attach"),
        ({"coverage_scope": "host_startup"}, "coverage_scope", "host_startup"),
        ({"runtime_id": "claude-code"}, "runtime_id", "claude-code"),
        ({"runtime_version": "2.1.0"}, "runtime_version", "2.1.0"),
        ({"execution_profile": "untrusted-kata-v1"}, "execution_profile", "untrusted-kata-v1"),
    ],
)
def test_a_grouping_field_is_read_from_facts_when_it_is_only_there(
    facts: dict[str, Any], field: str, expected: str
):
    """The four startup paths must not all collapse into one null group.

    The record carries the field *only* in facts here, which is what the
    collector emits: it states these as facts about the operation, and a reader
    that looked only at the top level filed every startup path as null and
    every terminal request as the same empty string.
    """
    if field == "request_kind":
        record = _record(
            operation="terminal.open",
            producer="control-plane",
            request_kind=expected,
            coverage_scope="server_request",
            outcome="completed",
            start_boundary="control_plane.operation.selected",
            end_boundary="control_plane.business_result.validated",
            facts={"operation": "terminal.open", **facts},
        )
    else:
        record = _record(
            operation="attempt.startup",
            producer="runner",
            request_kind="attempt.startup",
            coverage_scope="host_startup",
            facts={"operation": "attempt.startup", **facts},
        )
    record.pop(field, None)

    stored = measurement_ingest.validate_measurement(record)

    assert getattr(stored, field) == expected


def test_a_stated_top_level_field_wins_over_the_same_name_in_facts():
    """The fallback never overrides evidence stated where it belongs."""
    stored = measurement_ingest.validate_measurement(
        _record(
            operation="terminal.open",
            producer="control-plane",
            coverage_scope="server_request",
            request_kind="terminal.list",
            outcome="completed",
            start_boundary="control_plane.operation.selected",
            end_boundary="control_plane.business_result.validated",
            facts={"operation": "terminal.open", "request_kind": "terminal.create"},
        )
    )

    assert stored.request_kind == "terminal.list"


@pytest.mark.parametrize(
    "evidence",
    [
        {"truncated": True},
        {"missing_fragments": ["broker:c1:read"]},
        {"missing_producers": ["broker"]},
        {"truncated_fragments": ["broker:c1"]},
        {"dropped_spans": 1},
        {"profiling_stopped": "expired"},
        {"measurement_complete": False},
        {"boundary_frozen": False},
    ],
)
def test_a_complete_summary_cannot_contradict_the_record(evidence: dict[str, Any]):
    """A partial record must never enter the successful percentile population."""
    with pytest.raises(MeasurementRejected, match="contradict"):
        measurement_ingest.validate_measurement(_record(**evidence))


def test_run_ids_are_safe_admin_path_segments():
    """A descriptor accepted for storage must have a reachable summary URL."""
    with pytest.raises(MeasurementRejected, match="run_id"):
        measurement_ingest.validate_run(_manifest(run_id="run/one"))

    with pytest.raises(MeasurementRejected, match="measurement_id"):
        measurement_ingest.validate_measurement(_record(measurement_id="msm/one"))


@pytest.mark.parametrize(
    "changes",
    [
        {"coverage_scope": "browser_interaction"},
        {"end_to_end_complete": True},
        {"producer": "control-plane"},
        {"start_boundary": None},
        {"end_boundary": None},
        {"outcome": "completed"},
        {"stages": [{"stage": {"not": "text"}, "duration_us": 1}]},
        {"stages": [{"stage": "read", "duration_us": 1, "children": None}]},
        {"milestones": [{"name": "ready", "offset_us": 1, "source": {"not": "text"}}]},
    ],
)
def test_impossible_server_records_are_rejected(changes: dict[str, Any]):
    """Stored rows must have the runtime shape the Admin page relies on."""
    with pytest.raises(MeasurementRejected):
        measurement_ingest.validate_measurement(_record(**changes))


# ---------------------------------------------------------------------------
# The content whitelist covers the whole record, not only facts
# ---------------------------------------------------------------------------


def test_a_top_level_field_this_deployment_does_not_know_is_refused():
    """A prompt at the top level must not reach the table."""
    with pytest.raises(MeasurementRejected, match="unknown record field"):
        measurement_ingest.validate_measurement(_record(prompt="what the user typed"))


def test_a_credential_inside_a_stage_is_refused():
    """The promise is about what is stored, not about which object it hid in."""
    record = _record(stages=[{"stage": "read", "duration_us": 5, "token": "ajt.secret.value"}])

    with pytest.raises(MeasurementRejected, match="unknown stages entry field"):
        measurement_ingest.validate_measurement(record)


def test_an_attribute_may_still_hold_a_value_with_an_unknown_name():
    """Attributes are the one place a producer names its own dimensions."""
    stored = measurement_ingest.validate_measurement(
        _record(
            stages=[
                {
                    "stage": "read",
                    "duration_us": 5,
                    "attributes": {"bytes_bucket": "10k-100k"},
                }
            ]
        )
    )

    assert stored.record["stages"][0]["attributes"] == {"bytes_bucket": "10k-100k"}


def test_nested_children_are_walked_too():
    """A credential one level deeper is still a credential."""
    record = _record(
        stages=[
            {
                "stage": "create",
                "duration_us": 5,
                "children": [{"stage": "admission", "duration_us": 1, "prompt": "x"}],
            }
        ]
    )

    with pytest.raises(MeasurementRejected, match="unknown stages entry field"):
        measurement_ingest.validate_measurement(record)


# ---------------------------------------------------------------------------
# A record has to belong to the experiment it arrived with
# ---------------------------------------------------------------------------


def _now() -> Any:
    """The instant these checks are evaluated at."""
    return datetime.now(UTC)


def test_a_record_from_another_run_is_refused():
    """One experiment's samples must not land in another's distribution."""
    with pytest.raises(MeasurementRejected, match="not the manifest's"):
        measurement_ingest.require_record_matches_its_experiment(
            record=_record(profiling_run_id="run-other"),
            run=measurement_ingest.validate_run(_manifest()),
            now=_now(),
        )


def test_a_record_for_an_operation_the_run_did_not_select_is_refused():
    """A sample in a series the experiment never asked for is not evidence."""
    with pytest.raises(MeasurementRejected, match="does not select operation"):
        measurement_ingest.require_record_matches_its_experiment(
            record=_record(operation="terminal.open"),
            run=measurement_ingest.validate_run(_manifest(operations=["files.list"])),
            now=_now(),
        )


def test_a_record_that_matches_its_run_is_accepted():
    """The ordinary case is not refused by the checks above."""
    measurement_ingest.require_record_matches_its_experiment(
        record=_record(),
        run=measurement_ingest.validate_run(_manifest()),
        now=_now(),
    )


def test_a_record_delivered_long_after_the_experiment_ended_is_refused():
    """A stopped experiment must not keep growing without bound."""
    ended = _now() - timedelta(seconds=measurement_ingest.DELIVERY_GRACE_S + 60)

    with pytest.raises(MeasurementRejected, match="ended more than"):
        measurement_ingest.require_record_matches_its_experiment(
            record=_record(),
            run=measurement_ingest.validate_run(_manifest(expires_at=ended)),
            now=_now(),
        )


def test_a_record_delivered_inside_the_grace_period_is_accepted():
    """Delivery is asynchronous, so a short window is part of the contract."""
    just_ended = _now() - timedelta(seconds=measurement_ingest.DELIVERY_GRACE_S - 60)

    measurement_ingest.require_record_matches_its_experiment(
        record=_record(),
        run=measurement_ingest.validate_run(_manifest(expires_at=just_ended)),
        now=_now(),
    )


# ---------------------------------------------------------------------------
# The attempt a credential names has to be one this job ran
# ---------------------------------------------------------------------------


async def test_a_token_naming_an_attempt_that_never_existed_is_refused(
    client: AsyncClient, store: FakeStorage
):
    """A signed token is not evidence that the attempt it names is real."""
    job = await _claimed_job(client, store)
    store.attempts.pop(1)

    response = await client.post(
        f"/v1/agent/worker/jobs/{job['id']}/measurements",
        json={"record": _record(), "run": _manifest()},
        headers={"Authorization": f"Bearer {_token_for(job)}"},
    )

    assert response.status_code == 409
    assert store.measurements.rows == []


async def test_a_token_from_a_superseded_generation_is_refused(
    client: AsyncClient, store: FakeStorage
):
    """The generation is checked against the attempt, not taken on trust."""
    job = await _claimed_job(client, store)
    store.attempts[1]["lease_generation"] = 5

    response = await client.post(
        f"/v1/agent/worker/jobs/{job['id']}/measurements",
        json={"record": _record(), "run": _manifest()},
        headers={"Authorization": f"Bearer {_token_for(job, generation=1)}"},
    )

    assert response.status_code == 409
    assert store.measurements.rows == []


async def test_attribution_comes_from_the_attempt_that_ran_it(
    client: AsyncClient, store: FakeStorage
):
    """A late record names the machine that served it, not the current one.

    The chat's sandbox has since moved to another host. Reading attribution
    from the job's *current* sandbox would file this attempt's startup against
    a machine that never ran it.
    """
    job = await _claimed_job(client, store)
    store.attempts[1]["host_id"] = "host-that-ran-it"
    store.jobs[job["id"]]["thread_sandbox"] = {
        "sandbox_id": "asbx_current",
        "host": "host-serving-now",
    }

    response = await client.post(
        f"/v1/agent/worker/jobs/{job['id']}/measurements",
        json={"record": _record(), "run": _manifest()},
        headers={"Authorization": f"Bearer {_token_for(job)}"},
    )

    assert response.status_code == 200
    row = store.measurements.rows[0]
    assert row["host_id"] == "host-that-ran-it"
    assert row["sandbox_id"] == "asbx_e2e"


async def test_a_record_for_another_run_is_refused_by_the_route(
    client: AsyncClient, store: FakeStorage
):
    """The cross-check runs on the real path, not only in the unit above."""
    job = await _claimed_job(client, store)

    response = await client.post(
        f"/v1/agent/worker/jobs/{job['id']}/measurements",
        json={"record": _record(profiling_run_id="run-other"), "run": _manifest()},
        headers={"Authorization": f"Bearer {_token_for(job)}"},
    )

    assert response.status_code == 422
    assert store.measurements.rows == []


async def test_a_named_producer_cannot_exempt_itself_from_the_manifest(
    client: AsyncClient, store: FakeStorage
):
    """The internal-caller exemption is decided by the path, not the payload.

    A record's ``producer`` is a field the sender chose. Trusting it would let
    any holder of a worker token skip the scope, the selected-operation and the
    deadline checks together by writing one word into the payload.
    """
    job = await _claimed_job(client, store)

    response = await client.post(
        f"/v1/agent/worker/jobs/{job['id']}/measurements",
        json={"record": _record(producer="control-plane")},
        headers={"Authorization": f"Bearer {_token_for(job)}"},
    )

    assert response.status_code == 422
    assert "runner-rooted" in response.json()["detail"]["error"]["message"]
    assert store.measurements.rows == []


async def test_a_runner_record_without_its_manifest_is_refused(
    client: AsyncClient, store: FakeStorage
):
    """Run identity and expiry cannot be skipped by omitting the envelope."""
    job = await _claimed_job(client, store)
    response = await client.post(
        f"/v1/agent/worker/jobs/{job['id']}/measurements",
        json={"record": _record()},
        headers={"Authorization": f"Bearer {_token_for(job)}"},
    )

    assert response.status_code == 422
    assert "manifest" in response.json()["detail"]["error"]["message"]
    assert store.measurements.rows == []


async def test_worker_http_cannot_submit_a_control_plane_file_record(
    client: AsyncClient, store: FakeStorage
):
    """The producer name is constrained by the authenticated endpoint."""
    job = await _claimed_job(client, store)
    response = await client.post(
        f"/v1/agent/worker/jobs/{job['id']}/measurements",
        json={
            "record": _record(
                operation="files.list",
                request_kind="files.list",
                coverage_scope="server_request",
                producer="control-plane",
                outcome="completed",
                start_boundary="control_plane.operation.selected",
                end_boundary="control_plane.business_result.validated",
            ),
            "run": _manifest(operations=["files.list"]),
        },
        headers={"Authorization": f"Bearer {_token_for(job)}"},
    )

    assert response.status_code == 422
    assert store.measurements.rows == []


async def test_invalid_manifest_config_version_is_a_422(client: AsyncClient, store: FakeStorage):
    """A bad operator manifest is rejected, not leaked as an internal error."""
    job = await _claimed_job(client, store)
    response = await client.post(
        f"/v1/agent/worker/jobs/{job['id']}/measurements",
        json={"record": _record(), "run": _manifest(config_version="two")},
        headers={"Authorization": f"Bearer {_token_for(job)}"},
    )

    assert response.status_code == 422
    assert store.measurements.rows == []
