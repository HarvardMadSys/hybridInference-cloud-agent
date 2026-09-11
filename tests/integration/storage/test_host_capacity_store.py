"""Host capacity against a real Postgres: the guard, the epoch, and the minute.

The interesting parts of this feature are all statements about concurrent
writers, and none of them can be tested against a fake without testing the
fake:

* a capacity block is accepted only if the row still holds the credential the
  request authenticated with — which is a predicate that has to lose a race
  against a re-enrollment, not a check the caller performs;
* an out-of-order replica must not overwrite a younger observation, which is a
  comparison inside the UPDATE;
* one minute keeps exactly one observation per host, which is a primary key and
  an ``ON CONFLICT DO NOTHING``;
* and ``ALTER TABLE ... DEFAULT gen_random_uuid()`` must give every row that
  predates the column *its own* epoch rather than one shared value.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
import pytest_asyncio

from cloud_agent.capacity_protocol import (
    CapacityHistoryPointWrite,
    HostCapacityReport,
    bucket_for,
)
from cloud_agent.host_pool import HOST_ONLINE, hash_secret
from cloud_agent.storage.assembly import Storage
from cloud_agent.storage.repositories.host_capacity import PostgresHostCapacityRepository
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_EPOCH_BEFORE_CAPACITY = "0004_residency_on_membership"


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
async def db():
    """A clean store over the shared test database, left empty afterwards."""
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
        yield pool
    finally:
        # One test walks the schema backwards; the shared per-worker database
        # is expected at head by every other test whatever happened here.
        apply_migrations(config)
        await _reset()
        await pool.close()


@pytest_asyncio.fixture
async def store(db):
    """Everything else this test needs, to enrol a host and beat for it."""
    return Storage(db)


@pytest_asyncio.fixture
async def capacity(db):
    """The capability under test, on the repository that now owns it."""
    return PostgresHostCapacityRepository(db)


def capacity_block(
    sample_id: str = "00000000-0000-4000-8000-000000000001",
    *,
    age_ms: int = 120,
    cpu_percent: float = 37.2,
) -> dict:
    """A validated block, exactly the shape the API hands the store."""
    report = HostCapacityReport.model_validate(
        {
            "schema_version": 1,
            "sample_id": sample_id,
            "sample_age_ms": age_ms,
            "linux_boot_id": "00000000-0000-4000-8000-000000000002",
            "source": "linux_host_proc_and_docker_list",
            "cpu_percent": cpu_percent,
            "cpu_logical_count": 32,
            "cpu_window_seconds": 30.0,
            "memory_total_bytes": 128 * 1024**3,
            "memory_available_bytes": 86 * 1024**3,
            "inventory": {
                "namespace": "staging-agent",
                "status": "available",
                "running": 3,
                "stopped": 7,
                "other_states": {
                    "created": 0,
                    "paused": 0,
                    "restarting": 0,
                    "removing": 0,
                    "dead": 0,
                },
                "reason": None,
            },
            "missing": {"cpu": None, "memory": None},
        }
    )
    return report.model_dump(mode="json")


async def _enrol(store: Storage, host: str = "runner-a", *, credential: str | None = None) -> str:
    """Register a host and return the credential hash it now holds."""
    digest = hash_secret(credential or f"cred-{host}")
    await store.host_pool.register_host(
        host=host, credential_hash=digest, slots_total=3, broker_url="http://broker:8092"
    )
    return digest


async def _beat(
    store: Storage,
    host: str,
    digest: str,
    *,
    block: dict | None = None,
    age_ms: int | None = None,
) -> None:
    """One heartbeat, with or without a capacity block."""
    await store.host_pool.record_host_heartbeat(
        host=host,
        credential_hash=digest,
        capacity=block,
        capacity_sample_age_ms=age_ms,
        slots_total=3,
    )


# ── Writing the latest observation ────────────────────────────────────


async def test_a_block_round_trips_with_the_servers_own_timestamps(store):
    digest = await _enrol(store)
    await _beat(store, "runner-a", digest, block=capacity_block(), age_ms=5000)

    (host,) = await store.host_pool.list_runner_hosts()
    assert host["capacity_epoch"] is not None
    assert host["capacity_snapshot"]["cpu_percent"] == pytest.approx(37.2)
    # sampled_at is derived from the server's receipt time, not the runner's
    # clock: the two come from one clock that cannot jump.
    assert host["capacity_received_at"] is not None
    age = (host["capacity_received_at"] - host["capacity_sampled_at"]).total_seconds()
    assert age == pytest.approx(5.0, abs=0.5)


async def test_a_replayed_sample_does_not_refresh_its_timestamps(store):
    digest = await _enrol(store)
    await _beat(store, "runner-a", digest, block=capacity_block(), age_ms=1000)
    (first,) = await store.host_pool.list_runner_hosts()

    await _beat(store, "runner-a", digest, block=capacity_block(), age_ms=1)
    (second,) = await store.host_pool.list_runner_hosts()

    assert second["capacity_sampled_at"] == first["capacity_sampled_at"]
    assert second["capacity_received_at"] == first["capacity_received_at"]


async def test_an_older_observation_cannot_overwrite_a_younger_one(store):
    digest = await _enrol(store)
    await _beat(store, "runner-a", digest, block=capacity_block(age_ms=100), age_ms=100)
    (younger,) = await store.host_pool.list_runner_hosts()

    await _beat(
        store,
        "runner-a",
        digest,
        block=capacity_block(
            "00000000-0000-4000-8000-000000000009", age_ms=60_000, cpu_percent=9.9
        ),
        age_ms=60_000,
    )
    (after,) = await store.host_pool.list_runner_hosts()

    assert after["capacity_sampled_at"] == younger["capacity_sampled_at"]
    assert after["capacity_snapshot"]["cpu_percent"] == pytest.approx(37.2)


async def test_a_late_write_from_a_replaced_credential_is_refused(store):
    """The race the credential predicate exists for.

    Credential A authenticates, an operator re-enrolls the name under
    credential B, and A's request arrives afterwards. Checking "is this still
    the right credential" outside the write would let exactly that request
    repopulate the row with the previous machine's numbers. The liveness half
    of the same heartbeat still lands: the host is real either way.
    """
    stale_digest = await _enrol(store, "runner-a")
    fresh_digest = await _enrol(store, "runner-a", credential="cred-runner-a-rebuilt")
    assert stale_digest != fresh_digest

    await _beat(store, "runner-a", stale_digest, block=capacity_block(), age_ms=120)

    row = await store.host_pool.get_host(host="runner-a")
    assert row is not None
    (listed,) = await store.host_pool.list_runner_hosts()
    assert listed["capacity_snapshot"] is None
    assert listed["capacity_sampled_at"] is None
    assert listed["last_heartbeat_at"] is not None

    # And the credential that is actually held still works.
    await _beat(store, "runner-a", fresh_digest, block=capacity_block(), age_ms=120)
    (listed,) = await store.host_pool.list_runner_hosts()
    assert listed["capacity_snapshot"] is not None


async def test_a_heartbeat_without_a_block_leaves_the_last_one_alone(store):
    digest = await _enrol(store)
    await _beat(store, "runner-a", digest, block=capacity_block(), age_ms=120)

    await _beat(store, "runner-a", digest)

    (listed,) = await store.host_pool.list_runner_hosts()
    assert listed["capacity_snapshot"] is not None


async def test_re_enrollment_clears_the_observation_and_rotates_the_epoch(store):
    digest = await _enrol(store)
    await _beat(store, "runner-a", digest, block=capacity_block(), age_ms=120)
    (before,) = await store.host_pool.list_runner_hosts()

    await _enrol(store, "runner-a")

    (after,) = await store.host_pool.list_runner_hosts()
    assert after["capacity_snapshot"] is None
    assert after["capacity_sampled_at"] is None
    assert after["capacity_epoch"] != before["capacity_epoch"]


async def test_the_candidate_read_only_returns_fresh_observations(
    store, capacity: PostgresHostCapacityRepository
):
    digest = await _enrol(store)
    await _beat(store, "runner-a", digest, block=capacity_block(), age_ms=120_000)

    # Older than the ninety-second window: a host that stopped reporting must
    # stop adding points rather than have its last reading copied forward.
    assert await capacity.list_capacity_history_candidates() == []

    await _beat(
        store,
        "runner-a",
        digest,
        block=capacity_block("00000000-0000-4000-8000-000000000009"),
        age_ms=100,
    )
    (candidate,) = await capacity.list_capacity_history_candidates()
    assert candidate["host"] == "runner-a"
    assert candidate["cpu_percent"] == "37.2"
    assert candidate["memory_available_bytes"] == str(86 * 1024**3)


async def test_the_candidate_read_pages_by_host_name(
    store, capacity: PostgresHostCapacityRepository
):
    for name in ("runner-a", "runner-b", "runner-c"):
        digest = await _enrol(store, name)
        await _beat(store, name, digest, block=capacity_block(), age_ms=100)

    first = await capacity.list_capacity_history_candidates(limit=2)
    assert [row["host"] for row in first] == ["runner-a", "runner-b"]
    second = await capacity.list_capacity_history_candidates(after_host="runner-b", limit=2)
    assert [row["host"] for row in second] == ["runner-c"]


# ── The bounded history ───────────────────────────────────────────────


async def _record_point(
    store: Storage,
    capacity: PostgresHostCapacityRepository,
    host: str,
    *,
    minutes_ago: int = 0,
    sample_id: str = "00000000-0000-4000-8000-0000000000aa",
    cpu_percent: float | None = 37.2,
    cpu_missing_reason: str | None = None,
) -> bool:
    """Record one history point for ``host``, at an offset from now."""
    # The epoch is read the way the history task reads it: off the pool
    # projection, which is the only read that carries the capacity columns.
    host_row = next(row for row in await store.host_pool.list_runner_hosts() if row["host"] == host)
    sampled_at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return await capacity.record_capacity_history_point(
        host=host,
        capacity_epoch=host_row["capacity_epoch"],
        point=CapacityHistoryPointWrite(
            bucket_at=bucket_for(sampled_at),
            sampled_at=sampled_at,
            sample_id=sample_id,
            linux_boot_id="00000000-0000-4000-8000-000000000002",
            cpu_percent=cpu_percent,
            cpu_logical_count=32 if cpu_percent is not None else None,
            cpu_window_seconds=30.0 if cpu_percent is not None else None,
            cpu_missing_reason=cpu_missing_reason,
            memory_total_bytes=128 * 1024**3,
            memory_available_bytes=86 * 1024**3,
            memory_missing_reason=None,
        ),
    )


async def test_one_minute_keeps_the_first_observation_selected(
    store, capacity: PostgresHostCapacityRepository
):
    """Three replicas report the same minute; the chart must not wobble."""
    digest = await _enrol(store)
    await _beat(store, "runner-a", digest, block=capacity_block(), age_ms=100)
    assert await _record_point(store, capacity, "runner-a", cpu_percent=10.0) is True
    assert (
        await _record_point(
            store,
            capacity,
            "runner-a",
            sample_id="00000000-0000-4000-8000-0000000000bb",
            cpu_percent=90.0,
        )
        is False
    )

    history = await capacity.host_capacity_history(host="runner-a", range_seconds=3600)
    assert history is not None
    assert len(history["rows"]) == 1
    assert history["rows"][0]["cpu_percent"] == pytest.approx(10.0)


async def test_the_same_sample_cannot_produce_a_second_point(
    store, capacity: PostgresHostCapacityRepository
):
    digest = await _enrol(store)
    await _beat(store, "runner-a", digest, block=capacity_block(), age_ms=100)
    assert await _record_point(store, capacity, "runner-a", minutes_ago=0) is True
    # A retry a minute later is still the same observation.
    assert await _record_point(store, capacity, "runner-a", minutes_ago=1) is False


async def test_a_cpu_gap_still_stores_its_memory_half(
    store, capacity: PostgresHostCapacityRepository
):
    """A CPU that is warming up must not hide a perfectly good memory reading."""
    digest = await _enrol(store)
    await _beat(store, "runner-a", digest, block=capacity_block(), age_ms=100)

    assert (
        await _record_point(
            store,
            capacity,
            "runner-a",
            sample_id="00000000-0000-4000-8000-0000000000cc",
            cpu_percent=None,
            cpu_missing_reason="warming_up",
        )
        is True
    )

    history = await capacity.host_capacity_history(host="runner-a", range_seconds=3600)
    assert history is not None
    (point,) = history["rows"]
    assert point["cpu_percent"] is None
    assert point["cpu_missing_reason"] == "warming_up"
    assert point["memory_total_bytes"] == 128 * 1024**3


async def test_history_is_scoped_to_the_current_epoch(
    store, capacity: PostgresHostCapacityRepository
):
    """A rebuilt machine keeps its name; the old curve is not its curve."""
    digest = await _enrol(store)
    await _beat(store, "runner-a", digest, block=capacity_block(), age_ms=100)
    await _record_point(store, capacity, "runner-a", cpu_percent=11.0)

    await _enrol(store, "runner-a")
    digest = hash_secret("cred-runner-a")
    await _beat(store, "runner-a", digest, block=capacity_block(), age_ms=100)
    await _record_point(
        store,
        capacity,
        "runner-a",
        sample_id="00000000-0000-4000-8000-0000000000dd",
        cpu_percent=88.0,
    )

    history = await capacity.host_capacity_history(host="runner-a", range_seconds=3600)
    assert history is not None
    assert [row["cpu_percent"] for row in history["rows"]] == [pytest.approx(88.0)]


async def test_a_host_with_no_history_is_an_empty_window_not_a_missing_host(
    store, capacity: PostgresHostCapacityRepository
):
    await _enrol(store)
    history = await capacity.host_capacity_history(host="runner-a", range_seconds=3600)
    assert history is not None
    assert history["rows"] == []
    assert history["capacity_epoch"] is not None
    assert await capacity.host_capacity_history(host="nobody", range_seconds=3600) is None


async def test_pruning_removes_only_expired_rows_and_is_bounded(
    store, capacity: PostgresHostCapacityRepository
):
    digest = await _enrol(store)
    await _beat(store, "runner-a", digest, block=capacity_block(), age_ms=100)
    for index in range(4):
        await _record_point(
            store,
            capacity,
            "runner-a",
            minutes_ago=index,
            sample_id=f"00000000-0000-4000-8000-0000000000{index:02d}",
        )

    assert await capacity.prune_capacity_history(older_than_seconds=3600, limit=2000) == 0
    assert await capacity.prune_capacity_history(older_than_seconds=0, limit=2) == 2
    history = await capacity.host_capacity_history(host="runner-a", range_seconds=86400)
    assert history is not None
    assert len(history["rows"]) == 2


async def test_removing_a_host_keeps_its_history(store, capacity: PostgresHostCapacityRepository):
    """``Remove`` revokes a credential; it does not rewrite the record."""
    digest = await _enrol(store)
    await _beat(store, "runner-a", digest, block=capacity_block(), age_ms=100)
    await _record_point(store, capacity, "runner-a")

    assert await store.host_pool.remove_host(host="runner-a") == "ok"

    history = await capacity.host_capacity_history(host="runner-a", range_seconds=3600)
    assert history is not None
    assert len(history["rows"]) == 1


async def test_forgetting_a_host_takes_its_history_with_it(
    db, store, capacity: PostgresHostCapacityRepository
):
    """The one hard delete in the lifecycle, and the graph goes with it."""
    digest = await _enrol(store)
    await _beat(store, "runner-a", digest, block=capacity_block(), age_ms=100)
    await _record_point(store, capacity, "runner-a")
    (row,) = await store.host_pool.list_runner_hosts()
    epoch = row["capacity_epoch"]

    assert await store.host_pool.forget_runner_host(host="runner-a") == "deleted"

    assert await capacity.host_capacity_history(host="runner-a", range_seconds=3600) is None
    async with db.acquire() as conn:
        remaining = await conn.fetchval(
            "SELECT count(*) FROM agent_host_capacity_history WHERE capacity_epoch = $1", epoch
        )
    assert remaining == 0


async def test_a_block_the_database_refuses_cannot_roll_back_the_heartbeat(store):
    """The liveness half of a heartbeat is not hostage to the monitoring half.

    The schema already refuses the namespace this uses, so the store is handed
    the block directly: the guarantee has to hold for whatever a future
    constraint rejects, not only for the one input known today. Without the
    savepoint the two writes share a transaction, a raised statement finishes
    it, and the host that just reported in is recorded as never having done so.
    """
    digest = await _enrol(store)
    poisoned = {
        "sample_id": "00000000-0000-4000-8000-000000000001",
        "inventory": {"namespace": "staging-agent\u0000evil"},
    }

    await store.host_pool.record_host_heartbeat(
        host="runner-a",
        credential_hash=digest,
        capacity=poisoned,
        capacity_sample_age_ms=100,
        slots_total=9,
    )

    row = await store.host_pool.get_host(host="runner-a")
    assert row is not None
    assert row["slots_total"] == 9
    assert row["last_heartbeat_at"] is not None
    assert row["status"] == HOST_ONLINE
    (listed,) = await store.host_pool.list_runner_hosts()
    assert listed["capacity_snapshot"] is None


async def test_the_history_endpoint_maps_real_database_rows(
    store, capacity: PostgresHostCapacityRepository
):
    """The route's own mapping, over the types a real database hands back.

    asyncpg returns a UUID column as a ``UUID`` object, and the response model
    wants a string. Every unit fixture used strings, so nothing caught this
    until a window with rows in it was built end to end — which is what this
    does: real insert, real read, the endpoint function itself.
    """
    from cloud_agent.api import admin_hosts

    digest = await _enrol(store)
    await _beat(store, "runner-a", digest, block=capacity_block(), age_ms=100)
    assert await _record_point(store, capacity, "runner-a") is True

    response = await admin_hosts.get_agent_runner_host_capacity_history(
        host="runner-a", window="1h", _admin_id="admin", store=store
    )

    assert response.host == "runner-a"
    assert response.capacity_epoch is not None
    observed = [point for point in response.points if point.sampled_at is not None]
    assert len(observed) == 1
    assert observed[0].linux_boot_id == "00000000-0000-4000-8000-000000000002"
    assert observed[0].cpu_percent == pytest.approx(37.2)
    assert observed[0].memory_used_bytes == 128 * 1024**3 - 86 * 1024**3
    # Every bucket nobody observed is still a hole, not a zero.
    assert all(point.cpu_percent is None for point in response.points if point.sampled_at is None)


# ── The migration over rows that predate it ───────────────────────────


async def test_existing_hosts_each_get_their_own_epoch_and_no_invented_sample(db, store):
    """The upgrade runs against a live table, not an empty one.

    A single shared default would silently make two different machines look
    like one generation — which is the exact confusion the epoch exists to
    prevent — and a backfilled snapshot would draw a curve nobody measured.
    """
    await _enrol(store, "runner-a")
    await _enrol(store, "runner-b")

    config = _db_config()
    downgrade_to(config, _EPOCH_BEFORE_CAPACITY)
    apply_migrations(config)

    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT host, capacity_epoch, capacity_snapshot, capacity_sampled_at "
            "FROM agent_runner_hosts ORDER BY host"
        )
    assert [row["host"] for row in rows] == ["runner-a", "runner-b"]
    epochs = {row["capacity_epoch"] for row in rows}
    assert len(epochs) == 2
    assert None not in epochs
    assert all(row["capacity_snapshot"] is None for row in rows)
    assert all(row["capacity_sampled_at"] is None for row in rows)
