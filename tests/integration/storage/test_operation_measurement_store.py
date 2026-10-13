"""Operation measurements against a real Postgres: identity, idempotence, reads.

Every interesting property of this table is a statement about what the database
does with a repeated or a conflicting write, and none of them can be tested
against a fake without testing the fake:

* a retry of the same measurement must be a no-op, not a second sample, which
  is an ``ON CONFLICT DO NOTHING`` plus a hash comparison;
* the same id with different contents must be a conflict rather than a silent
  overwrite, because last-write-wins would replace a sample with a retry that
  disagrees with it;
* a keyset page must not skip or repeat a row when two share a timestamp, which
  is what the ``measurement_id`` tiebreaker is for;
* and a percentile has to come from the whole experiment rather than whatever
  page a reader happened to load.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from tests.support.db import backdate_measurement_arrival, reset_agent_tables
from tests.support.migrations import apply_migrations

from cloud_agent.storage.repositories.operation_measurements import (
    PostgresOperationMeasurementRepository,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"


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


@pytest_asyncio.fixture
async def store():
    """A repository over the shared test database, left empty afterwards."""
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
    try:
        yield _Store(PostgresOperationMeasurementRepository(pool), pool)
    finally:
        apply_migrations(config)
        await _reset()
        await pool.close()


class _Store:
    """The repository, plus the pool the arrival seam needs.

    The seam is a separate argument rather than an attribute of the repository:
    a test may reach the pool, but not by walking through the object under
    test, which is what the storage boundary forbids.
    """

    def __init__(self, repository: Any, pool: Any) -> None:
        self.repository = repository
        self.pool = pool

    def __getattr__(self, name: str) -> Any:
        """Delegate every capability call to the repository."""
        return getattr(self.repository, name)


#: One experiment's configured expiry, frozen for the whole module but stated
#: relative to now. A helper that re-derived it per call would be manufacturing
#: a different experiment every time, so a test of idempotence would have to
#: loosen the rule it is meant to be checking -- while a calendar literal would
#: describe an experiment that has already ended.
RUN_EXPIRES_AT = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)


def _run(**overrides: object) -> dict[str, object]:
    """One experiment catalogue entry."""
    run: dict[str, object] = {
        "run_id": "run-1",
        "operations": ["files.list", "files.open", "terminal.open"],
        "target": {"sandbox_id": "asbx_1"},
        "expires_at": RUN_EXPIRES_AT,
        "config_version": 1,
        "admission_instances": [{"instance": "cp-1", "max_operations": 200}],
    }
    run.update(overrides)
    return run


def _measurement(**overrides: object) -> dict[str, object]:
    """One frozen measurement, shaped exactly as the control plane freezes it."""
    measurement: dict[str, object] = {
        "measurement_id": "msm_1",
        "interaction_id": "msm_1",
        "profiling_run_id": "run-1",
        "schema_version": 3,
        "owner_id": "user-1",
        "job_id": "ajob_1",
        "thread_id": "athr_1",
        "sandbox_id": "asbx_1",
        "host_id": "host-a",
        "attempt_id": None,
        "lease_generation": None,
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
        "startup_path": None,
        "runtime_id": None,
        "runtime_version": None,
        "execution_profile": None,
        "observed_at": datetime.now(UTC),
        "record": {"schema_version": 3, "operation": "files.list", "stages": []},
        "payload_hash": "hash-1",
    }
    measurement.update(overrides)
    return measurement


async def test_a_retried_measurement_is_one_sample(store):
    """The same record twice is the same sample, not two of them."""
    assert (
        await store.record_operation_measurement(measurement=_measurement(), run=_run()) == "stored"
    )
    assert (
        await store.record_operation_measurement(measurement=_measurement(), run=_run())
        == "duplicate"
    )

    page = await store.list_operation_measurements()
    assert [row["measurement_id"] for row in page] == ["msm_1"]


async def test_the_same_id_with_different_contents_is_a_conflict(store):
    """A reused id must not silently replace the sample it names."""
    await store.record_operation_measurement(measurement=_measurement(), run=_run())

    result = await store.record_operation_measurement(
        measurement=_measurement(payload_hash="hash-2", duration_us=9999),
        run=_run(),
    )
    assert result == "conflict"

    stored = await store.get_operation_measurement("msm_1")
    assert stored is not None
    assert stored["duration_us"] == 1234


async def test_a_measurement_conflict_does_not_create_a_phantom_run(store):
    """A refused sample cannot leave an experiment with no accepted record."""
    await store.record_operation_measurement(measurement=_measurement(), run=_run())

    result = await store.record_operation_measurement(
        measurement=_measurement(
            profiling_run_id="run-phantom",
            payload_hash="different",
        ),
        run=_run(run_id="run-phantom"),
    )

    assert result == "conflict"
    assert await store.get_profiling_run("run-phantom") is None


async def test_the_record_round_trips_through_jsonb(store):
    """What was frozen is what comes back, nested structure included."""
    record = {
        "schema_version": 3,
        "operation": "terminal.open",
        "stages": [
            {"stage": "list", "start_offset_us": 0, "duration_us": 12},
            {
                "stage": "create",
                "start_offset_us": 12,
                "duration_us": 40,
                "children": [{"stage": "admission", "duration_us": 3, "source": "broker"}],
            },
        ],
        "facts": {"terminal_count": 1, "kind": "directory"},
        "missing_fragments": ["broker:c2:create"],
    }
    await store.record_operation_measurement(
        measurement=_measurement(operation="terminal.open", record=record), run=_run()
    )

    stored = await store.get_operation_measurement("msm_1")

    assert stored is not None
    assert stored["record"] == record


async def test_a_page_does_not_skip_a_row_that_shares_a_timestamp(store):
    """Two samples at the same instant are two rows, on two pages if need be.

    The keyset is ``(received_at, measurement_id)``, so the rows have to share
    ``received_at`` -- their *arrival* -- for the tiebreaker to be what the
    test exercises. Making only ``observed_at`` equal would leave the page key
    already unique and the tiebreaker untested.
    """
    moment = datetime.now(UTC)
    for index in range(5):
        await store.record_operation_measurement(
            measurement=_measurement(
                measurement_id=f"msm_{index}",
                payload_hash=f"hash-{index}",
                observed_at=moment,
            ),
            run=_run(),
        )
    await backdate_measurement_arrival(store.pool, moment=moment)

    first = await store.list_operation_measurements(limit=2)
    assert len(first) == 2
    last = first[-1]
    second = await store.list_operation_measurements(
        limit=10, after=(last["received_at"], last["measurement_id"])
    )
    third = await store.list_operation_measurements(
        limit=10, after=(second[-1]["received_at"], second[-1]["measurement_id"])
    )

    seen = [row["measurement_id"] for row in first + second + third]
    assert len(seen) == 5
    assert len(set(seen)) == 5


async def test_the_summary_covers_the_whole_experiment_not_one_page(store):
    """A percentile is about the population, so it is computed in SQL."""
    for index, duration in enumerate((10, 20, 30, 40, 50)):
        await store.record_operation_measurement(
            measurement=_measurement(
                measurement_id=f"msm_{index}",
                payload_hash=f"hash-{index}",
                duration_us=duration,
            ),
            run=_run(),
        )

    summary = await store.summarize_operation_measurements(run_id="run-1")

    assert len(summary) == 1
    row = summary[0]
    assert row["sample_count"] == 5
    assert row["eligible_count"] == 5
    assert row["min_us"] == 10
    assert row["max_us"] == 50
    assert row["p50_us"] == 30
    assert row["group"]["operation"] == "files.list"
    assert row["group"]["coverage_scope"] == "server_request"


async def test_the_summary_keeps_incomparable_samples_apart(store):
    """Boundary versions are not averaged together; they are separate rows."""
    await store.record_operation_measurement(
        measurement=_measurement(measurement_id="msm_a", payload_hash="h-a", duration_us=10),
        run=_run(),
    )
    await store.record_operation_measurement(
        measurement=_measurement(
            measurement_id="msm_b", payload_hash="h-b", duration_us=1000, boundary_version=2
        ),
        run=_run(),
    )

    summary = await store.summarize_operation_measurements(run_id="run-1")

    assert [row["group"]["boundary_version"] for row in summary] == [1, 2]
    assert [row["sample_count"] for row in summary] == [1, 1]


async def test_incomplete_and_failed_samples_are_counted_separately(store):
    """A reader has to see what was excluded, not only what remained.

    The timings are deliberately far apart: if a failed or partial sample were
    leaking into the distribution, the percentiles below could not be 100.
    """
    await store.record_operation_measurement(
        measurement=_measurement(measurement_id="msm_ok", payload_hash="h-1", duration_us=100),
        run=_run(),
    )
    await store.record_operation_measurement(
        measurement=_measurement(
            measurement_id="msm_partial",
            payload_hash="h-2",
            duration_us=20_000,
            collection_complete=False,
            outcome="completed",
        ),
        run=_run(),
    )
    await store.record_operation_measurement(
        measurement=_measurement(
            measurement_id="msm_failed",
            payload_hash="h-3",
            duration_us=10_000,
            outcome="failed",
            collection_complete=True,
        ),
        run=_run(),
    )

    summary = await store.summarize_operation_measurements(run_id="run-1")

    assert summary[0]["sample_count"] == 3
    assert summary[0]["incomplete_count"] == 1
    assert summary[0]["failed_count"] == 1
    # Only the eligible sample is in the distribution. Their timings differ by
    # orders of magnitude, so a p50 that included them could not be 100 -- and
    # counting failures is not the same as keeping them out of the percentile.
    assert summary[0]["eligible_count"] == 1
    assert summary[0]["p50_us"] == 100
    assert summary[0]["p95_us"] == 100
    assert summary[0]["min_us"] == 100
    assert summary[0]["max_us"] == 100


async def test_a_measurement_can_be_filtered_by_what_it_measured(store):
    """The filters the Admin surface offers are the ones the query supports."""
    await store.record_operation_measurement(measurement=_measurement(), run=_run())
    await store.record_operation_measurement(
        measurement=_measurement(
            measurement_id="msm_2",
            payload_hash="h-2",
            operation="files.open",
            request_kind="files.open",
            job_id="ajob_2",
        ),
        run=_run(),
    )

    assert [
        row["measurement_id"]
        for row in await store.list_operation_measurements(operations=["files.open"])
    ] == ["msm_2"]
    assert [
        row["measurement_id"] for row in await store.list_operation_measurements(job_id="ajob_1")
    ] == ["msm_1"]
    assert await store.list_operation_measurements(operations=["terminal.open"]) == []


async def test_an_attempt_and_an_interaction_select_different_rows(store):
    """Two precise keys, and neither is a spelling of the other.

    The Runs detail joins on ``attempt_id`` because a startup record is the
    only kind that carries one; a terminal open has no attempt at all and is
    read by ``interaction_id`` instead. A query that treated the two as
    interchangeable would answer "this attempt's startup" with three terminal
    requests, which is exactly the confusion the Admin page must not create.
    """
    # Two startups for two attempts of one job, sharing no interaction.
    await store.record_operation_measurement(
        measurement=_measurement(
            measurement_id="msm_startup_1",
            interaction_id="msm_startup_1",
            payload_hash="h-s1",
            operation="attempt.startup",
            request_kind="attempt.startup",
            coverage_scope="host_startup",
            attempt_id=101,
            lease_generation=1,
            producer="runner",
            startup_path="new",
        ),
        run=_run(),
    )
    await store.record_operation_measurement(
        measurement=_measurement(
            measurement_id="msm_startup_2",
            interaction_id="msm_startup_2",
            payload_hash="h-s2",
            operation="attempt.startup",
            request_kind="attempt.startup",
            coverage_scope="host_startup",
            attempt_id=202,
            lease_generation=1,
            producer="runner",
            startup_path="new",
        ),
        run=_run(),
    )
    # Three requests of one terminal open, correlated and with no attempt.
    for index, kind in enumerate(("terminal.list", "terminal.create", "terminal.stream_attach")):
        await store.record_operation_measurement(
            measurement=_measurement(
                measurement_id=f"msm_terminal_{index}",
                interaction_id="aint_1",
                payload_hash=f"h-t{index}",
                operation="terminal.open",
                request_kind=kind,
            ),
            run=_run(),
        )

    by_attempt = await store.list_operation_measurements(attempt_id=101)
    assert [row["measurement_id"] for row in by_attempt] == ["msm_startup_1"]
    assert by_attempt[0]["attempt_id"] == 101
    assert by_attempt[0]["coverage_scope"] == "host_startup"

    # The interaction is the only way to read one terminal open whole, and it
    # returns three rows because the open really is three measurements.
    by_interaction = await store.list_operation_measurements(interaction_id="aint_1")
    assert sorted(row["request_kind"] for row in by_interaction) == [
        "terminal.create",
        "terminal.list",
        "terminal.stream_attach",
    ]
    assert all(row["attempt_id"] is None for row in by_interaction)

    # Neither filter answers with the other's rows.
    assert await store.list_operation_measurements(attempt_id=999) == []
    assert await store.list_operation_measurements(interaction_id="msm_startup_1") != []
    assert all(
        row["operation"] == "attempt.startup"
        for row in await store.list_operation_measurements(interaction_id="msm_startup_1")
    )


async def test_the_experiment_catalogue_is_readable(store):
    """A stored run explains which samples belong together and why."""
    await store.record_operation_measurement(measurement=_measurement(), run=_run())

    runs = await store.list_profiling_runs()
    assert [run["run_id"] for run in runs] == ["run-1"]
    assert runs[0]["operations"] == ["files.list", "files.open", "terminal.open"]
    assert runs[0]["target"] == {"sandbox_id": "asbx_1"}
    assert runs[0]["admission_instances"] == [{"instance": "cp-1", "max_operations": 200}]

    one = await store.get_profiling_run("run-1")
    assert one is not None and one["run_id"] == "run-1"
    assert await store.get_profiling_run("run-missing") is None


async def test_catalogue_recency_advances_when_a_new_sample_arrives(store):
    """Newest delivery order must not remain pinned to a run's first sample."""
    await store.record_operation_measurement(measurement=_measurement(), run=_run())
    old = datetime.now(UTC) - timedelta(days=1)
    async with store.pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_operation_profiling_runs SET received_at = $2 WHERE run_id = $1",
            "run-1",
            old,
        )

    await store.record_operation_measurement(
        measurement=_measurement(measurement_id="msm_2", payload_hash="h-2"),
        run=_run(),
    )

    run = await store.get_profiling_run("run-1")
    assert run is not None
    assert run["received_at"] > old


async def _run_row_version(pool: Any) -> tuple[str, str]:
    """Return the run row's ``xmin`` and ``xmax``.

    A rewrite of the row gives it a new ``xmin``; a ``SELECT ... FOR UPDATE``
    stamps the locker in ``xmax``. Together they say whether a write touched the
    row at all, which no column value can.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT xmin::text AS xmin, xmax::text AS xmax "
            "FROM agent_operation_profiling_runs WHERE run_id = $1",
            "run-1",
        )
    assert row is not None
    return row["xmin"], row["xmax"]


async def test_a_sample_from_a_listed_instance_neither_locks_nor_rewrites_its_run(store):
    """A run's samples must not queue behind each other on its catalogue row.

    Regression: every sample took ``FOR UPDATE`` on the run row, rewrote its
    admission list even when nothing was added, and rewrote it again to bump
    ``received_at`` -- so all the samples of a run serialized on one row lock
    held to commit, and each left two dead versions of the row behind.
    """
    first = await store.record_operation_measurement(measurement=_measurement(), run=_run())
    assert first == "stored"
    # As if recency had just been bumped -- stamped ahead of anything the
    # samples below can be, however slowly this machine runs them, so the
    # once-a-second bump cannot be what this test observes.
    async with store.pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_operation_profiling_runs "
            "SET received_at = NOW() + INTERVAL '1 hour' WHERE run_id = $1",
            "run-1",
        )
    before = await _run_row_version(store.pool)

    for index in range(3):
        result = await store.record_operation_measurement(
            measurement=_measurement(measurement_id=f"msm_more_{index}", payload_hash=f"h-{index}"),
            run=_run(),
        )
        assert result == "stored"

    assert await _run_row_version(store.pool) == before
    run = await store.get_profiling_run("run-1")
    assert run is not None
    assert run["admission_instances"] == [{"instance": "cp-1", "max_operations": 200}]


async def test_a_measurement_without_a_run_row_is_still_stored(store):
    """A producer that never sent its manifest still contributes its sample."""
    assert (
        await store.record_operation_measurement(measurement=_measurement(), run=None) == "stored"
    )

    assert await store.get_profiling_run("run-1") is None
    stored = await store.get_operation_measurement("msm_1")
    assert stored is not None
    assert stored["profiling_run_id"] == "run-1"


async def test_a_distribution_with_no_eligible_sample_reports_no_percentile(store):
    """No eligible sample means a null percentile, not a number from the rest."""
    await store.record_operation_measurement(
        measurement=_measurement(
            measurement_id="msm_a",
            payload_hash="h-a",
            duration_us=10_000,
            outcome="failed",
            collection_complete=True,
        ),
        run=_run(),
    )
    await store.record_operation_measurement(
        measurement=_measurement(
            measurement_id="msm_b",
            payload_hash="h-b",
            duration_us=20_000,
            outcome="completed",
            collection_complete=False,
        ),
        run=_run(),
    )

    summary = await store.summarize_operation_measurements(run_id="run-1")

    assert summary[0]["sample_count"] == 2
    assert summary[0]["eligible_count"] == 0
    assert summary[0]["p50_us"] is None
    assert summary[0]["p95_us"] is None
    assert summary[0]["min_us"] is None
    assert summary[0]["max_us"] is None


async def test_the_four_startup_paths_are_never_averaged_together(store):
    """A new group and a live reuse are different operations, not one mean.

    Folding them produced a p50 of 500500 µs from a 1 s reuse and a 1 s
    creation: a number that describes neither.
    """
    for index, (path, duration) in enumerate(
        (("new_group", 1_000_000), ("reuse_live", 1_000), ("resume_stopped", 2_000))
    ):
        await store.record_operation_measurement(
            measurement=_measurement(
                measurement_id=f"msm_{index}",
                payload_hash=f"h-{index}",
                operation="attempt.startup",
                request_kind="attempt.startup",
                coverage_scope="host_startup",
                duration_us=duration,
                startup_path=path,
            ),
            run=_run(operations=["attempt.startup"]),
        )

    summary = await store.summarize_operation_measurements(run_id="run-1")

    by_path = {row["group"]["startup_path"]: row for row in summary}
    assert set(by_path) == {"new_group", "reuse_live", "resume_stopped"}
    assert by_path["new_group"]["p50_us"] == 1_000_000
    assert by_path["reuse_live"]["p50_us"] == 1_000


async def test_startup_runtimes_and_execution_profiles_are_never_mixed(store):
    """A percentile must describe one comparable runtime configuration."""
    for index, (runtime, version, profile) in enumerate(
        (
            ("claude-code", "2.0.0", "untrusted-kata-v1"),
            ("codex", "1.4.0", "trusted-v1"),
        )
    ):
        await store.record_operation_measurement(
            measurement=_measurement(
                measurement_id=f"msm_runtime_{index}",
                payload_hash=f"runtime-{index}",
                operation="attempt.startup",
                request_kind="attempt.startup",
                coverage_scope="host_startup",
                startup_path="new_group",
                runtime_id=runtime,
                runtime_version=version,
                execution_profile=profile,
            ),
            run=_run(operations=["attempt.startup"]),
        )

    summary = await store.summarize_operation_measurements(run_id="run-1")

    assert {
        (
            row["group"]["runtime_id"],
            row["group"]["runtime_version"],
            row["group"]["execution_profile"],
        )
        for row in summary
    } == {
        ("claude-code", "2.0.0", "untrusted-kata-v1"),
        ("codex", "1.4.0", "trusted-v1"),
    }


async def test_a_caller_cannot_group_away_a_required_dimension(store):
    """The dimensions that decide comparability are not the caller's to remove."""
    await store.record_operation_measurement(measurement=_measurement(), run=_run())

    summary = await store.summarize_operation_measurements(run_id="run-1", group_by=[])

    group = summary[0]["group"]
    for required in (
        "operation",
        "request_kind",
        "boundary_version",
        "coverage_scope",
        "runtime_id",
        "runtime_version",
        "execution_profile",
    ):
        assert required in group

    with pytest.raises(ValueError):
        await store.summarize_operation_measurements(run_id="run-1", group_by=["nonsense"])


async def test_a_second_manifest_under_one_run_id_is_refused(store):
    """A different experiment cannot claim an id the catalogue already defines."""
    assert (
        await store.record_operation_measurement(
            measurement=_measurement(), run=_run(target={"host": "host-a"})
        )
        == "stored"
    )

    result = await store.record_operation_measurement(
        measurement=_measurement(measurement_id="msm_2", payload_hash="h-2"),
        run=_run(target={"host": "host-b"}),
    )

    assert result == "run_conflict"
    # The sample was not stored: a record filed under a catalogue entry that
    # describes something else is worse than a missing sample.
    assert await store.get_operation_measurement("msm_2") is None
    run = await store.get_profiling_run("run-1")
    assert run is not None and run["target"] == {"host": "host-a"}


async def test_a_run_that_changed_what_it_selects_is_refused(store):
    """Every part that says what the experiment is is compared, not only the target."""
    await store.record_operation_measurement(measurement=_measurement(), run=_run())

    for changed in (
        _run(operations=["terminal.open"]),
        _run(config_version=2),
    ):
        result = await store.record_operation_measurement(
            measurement=_measurement(measurement_id="msm_x", payload_hash="h-x"),
            run=changed,
        )
        assert result == "run_conflict"


async def test_an_experiment_that_ends_later_is_a_different_experiment(store):
    """Extending the deadline must not be absorbed into an existing run.

    Accepting it would file samples of a day-long experiment under a catalogue
    entry that ends in half an hour, and leave every later writer disagreeing
    with the deadline the ingest and the Admin catalogue read.
    """
    await store.record_operation_measurement(measurement=_measurement(), run=_run())

    result = await store.record_operation_measurement(
        measurement=_measurement(measurement_id="msm_2", payload_hash="h-2"),
        run=_run(expires_at=RUN_EXPIRES_AT + timedelta(days=1)),
    )

    assert result == "run_conflict"
    assert await store.get_operation_measurement("msm_2") is None
    run = await store.get_profiling_run("run-1")
    assert run is not None and run["expires_at"] == RUN_EXPIRES_AT


async def test_an_experiment_that_ends_sooner_is_a_different_experiment(store):
    """Shortening the deadline is a refusal too, not a silent narrowing."""
    await store.record_operation_measurement(measurement=_measurement(), run=_run())

    result = await store.record_operation_measurement(
        measurement=_measurement(measurement_id="msm_2", payload_hash="h-2"),
        run=_run(expires_at=RUN_EXPIRES_AT - timedelta(minutes=5)),
    )

    assert result == "run_conflict"


async def test_one_instant_spelled_two_ways_is_one_experiment(store):
    """The comparison is on the instant, not on its ISO spelling."""
    await store.record_operation_measurement(measurement=_measurement(), run=_run())

    # The same moment, written with an offset instead of in UTC.
    shifted = RUN_EXPIRES_AT.astimezone(timezone(timedelta(hours=8)))
    result = await store.record_operation_measurement(
        measurement=_measurement(measurement_id="msm_2", payload_hash="h-2"),
        run=_run(expires_at=shifted),
    )

    assert result == "stored"


async def test_a_second_admission_instance_joins_the_manifest(store):
    """Two replicas running one experiment are two instances of one manifest."""
    await store.record_operation_measurement(
        measurement=_measurement(),
        run=_run(admission_instances=[{"instance": "cp-1", "max_operations": 200}]),
    )

    assert (
        await store.record_operation_measurement(
            measurement=_measurement(measurement_id="msm_2", payload_hash="h-2"),
            run=_run(admission_instances=[{"instance": "cp-2", "max_operations": 200}]),
        )
        == "stored"
    )

    run = await store.get_profiling_run("run-1")
    assert run is not None
    assert [item["instance"] for item in run["admission_instances"]] == ["cp-1", "cp-2"]


async def test_an_instance_reporting_a_different_budget_is_refused(store):
    """A budget that changed mid-run is a different experiment, not a merge."""
    await store.record_operation_measurement(
        measurement=_measurement(),
        run=_run(admission_instances=[{"instance": "cp-1", "max_operations": 200}]),
    )

    result = await store.record_operation_measurement(
        measurement=_measurement(measurement_id="msm_2", payload_hash="h-2"),
        run=_run(admission_instances=[{"instance": "cp-1", "max_operations": 500}]),
    )

    assert result == "run_conflict"
    run = await store.get_profiling_run("run-1")
    assert run is not None
    assert run["admission_instances"] == [{"instance": "cp-1", "max_operations": 200}]


# ---------------------------------------------------------------------------
# Two writers meeting on a run that does not exist yet
# ---------------------------------------------------------------------------


async def _simultaneously(first: Any, second: Any) -> list[Any]:
    """Run two writes concurrently and return both outcomes.

    Sequential calls cannot show a first-insert race: by the second call the
    row exists, which is the case ``FOR UPDATE`` already handled. These are two
    coroutines over one pool, so they take their own connections and reach the
    insert together.
    """
    return list(await asyncio.gather(first, second))


async def test_two_writers_creating_one_run_at_once_both_succeed(store):
    """Compatible manifests racing to create a run do not raise at each other.

    ``SELECT ... FOR UPDATE`` cannot lock a row that does not exist, so both
    writers used to read nothing and then race the primary key -- one of them
    surfacing a UniqueViolation to a caller that had been promised stored,
    duplicate or a conflict. This is the ordinary case of several producers
    starting an experiment, not an exotic one.
    """
    first, second = await _simultaneously(
        store.record_operation_measurement(
            measurement=_measurement(measurement_id="msm_a", payload_hash="h-a"),
            run=_run(admission_instances=[{"instance": "cp-1", "max_operations": 200}]),
        ),
        store.record_operation_measurement(
            measurement=_measurement(measurement_id="msm_b", payload_hash="h-b"),
            run=_run(admission_instances=[{"instance": "cp-2", "max_operations": 200}]),
        ),
    )

    assert first == "stored" and second == "stored"
    # Both samples survive, and the manifest names both admission instances.
    assert await store.get_operation_measurement("msm_a") is not None
    assert await store.get_operation_measurement("msm_b") is not None
    run = await store.get_profiling_run("run-1")
    assert run is not None
    assert sorted(item["instance"] for item in run["admission_instances"]) == ["cp-1", "cp-2"]


async def test_two_incompatible_manifests_at_once_admit_exactly_one(store):
    """Only one experiment may own a run id, however the race resolves."""
    results = await _simultaneously(
        store.record_operation_measurement(
            measurement=_measurement(measurement_id="msm_a", payload_hash="h-a"),
            run=_run(target={"host": "host-a"}),
        ),
        store.record_operation_measurement(
            measurement=_measurement(measurement_id="msm_b", payload_hash="h-b"),
            run=_run(target={"host": "host-b"}),
        ),
    )

    assert sorted(results) == ["run_conflict", "stored"]
    run = await store.get_profiling_run("run-1")
    assert run is not None
    # The catalogue describes the winner, and it kept exactly its own sample.
    winner = "msm_a" if run["target"] == {"host": "host-a"} else "msm_b"
    loser = "msm_b" if winner == "msm_a" else "msm_a"
    assert await store.get_operation_measurement(winner) is not None
    assert await store.get_operation_measurement(loser) is None


async def test_the_same_measurement_retried_at_once_is_one_sample(store):
    """Concurrent retries of one record leave one row and no error either."""
    queued_run = _run()
    results = await _simultaneously(
        store.record_operation_measurement(measurement=_measurement(), run=queued_run),
        store.record_operation_measurement(measurement=_measurement(), run=queued_run),
    )

    assert sorted(results) == ["duplicate", "stored"]
    page = await store.list_operation_measurements()
    assert [row["measurement_id"] for row in page] == ["msm_1"]


@pytest.mark.parametrize(
    "first_expiry, second_expiry", [(None, RUN_EXPIRES_AT), (RUN_EXPIRES_AT, None)]
)
async def test_continuous_and_timed_manifests_cannot_share_an_identity(
    store, first_expiry, second_expiry
):
    """A null expiry is an immutable choice, not a missing comparison field."""
    initial = _run(expires_at=first_expiry)
    assert (
        await store.record_operation_measurement(measurement=_measurement(), run=initial)
        == "stored"
    )
    assert (
        await store.record_operation_measurement(measurement=_measurement(), run=initial)
        == "duplicate"
    )
    assert (
        await store.record_operation_measurement(
            measurement=_measurement(measurement_id="msm_changed", payload_hash="changed"),
            run=_run(expires_at=second_expiry),
        )
        == "run_conflict"
    )
    assert await store.get_operation_measurement("msm_changed") is None
    stored = await store.get_profiling_run("run-1")
    assert stored["expires_at"] == first_expiry
