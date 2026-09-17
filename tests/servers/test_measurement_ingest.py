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

from cloud_agent import measurement_ingest
from cloud_agent.measurement_ingest import MeasurementRejected
from cloud_agent.tokens import SCOPE_FULL, mint_worker_token
from tests.servers.test_agent_jobs_api import FakeStorage, _build_app, _create_job


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

    async def record_operation_measurement(self, *, measurement: Any, run: Any = None) -> str:
        """Delegate to the in-memory measurement store."""
        return await self.measurements.record_operation_measurement(
            measurement=measurement, run=run
        )


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
        "operation": "files.list",
        "request_kind": "files.list",
        "boundary_version": 1,
        "coverage_scope": "server_request",
        "producer": "control-plane",
        "producer_instance_id": "cp-1",
        "duration_us": 1234,
        "outcome": "completed",
        "collection_complete": True,
        "end_to_end_complete": False,
        "observed_at": datetime.now(UTC).isoformat(),
        "facts": {"operation": "files.list", "kind": "directory", "entry_count": 3},
    }
    record.update(overrides)
    return record


def _manifest(**overrides: Any) -> dict[str, Any]:
    """One experiment manifest, as the descriptor states it."""
    run: dict[str, Any] = {
        "run_id": "run-1",
        "operations": ["files.list", "files.open"],
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
    """Create a job and return it, as the store holds it."""
    job_id = await _create_job(client)
    return store.jobs[job_id]


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
    """A field in the record naming someone else is ignored, not believed.

    The producer states what it measured. Only the row its credential resolved
    says whose history the sample joins, which is what stops a valid token for
    one job from filing a sample under another's.
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
            )
        },
        headers={"Authorization": f"Bearer {_token_for(job)}"},
    )

    assert response.status_code == 200
    row = store.measurements.rows[0]
    assert row["job_id"] == job["id"]
    assert row["owner_id"] == job["user_id"]
    assert row["thread_id"] == job["thread_id"]


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
        json={"record": _record()},
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
        json={"record": _record()},
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
        json={"record": _record()},
        headers=headers,
    )

    response = await client.post(
        f"/v1/agent/worker/jobs/{job['id']}/measurements",
        json={"record": _record(duration_us=9999)},
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


def test_a_stored_record_keeps_no_more_than_the_whitelist_allows():
    """Validation strips before hashing, so the hash describes what is stored."""
    stored = measurement_ingest.validate_measurement(
        _record(facts={"kind": "file", "content": "secret"})
    )

    assert stored.record["facts"] == {"kind": "file"}
    assert stored.payload_hash == measurement_ingest.canonical_hash(stored.record)
    assert "secret" in json.dumps(_record(facts={"kind": "file", "content": "secret"}))
    assert "secret" not in json.dumps(stored.record)
