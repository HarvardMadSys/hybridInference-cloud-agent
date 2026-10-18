"""Real Postgres checks for the Usage page: an owner's containers and their use."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from tests.integration.storage.test_admin_sandbox_queries import observation, seed
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_attempt_ledger import db as db, store as store
from tests.unit.test_sandbox_resource_protocol import sample

from cloud_agent.admin_observability import decode_cursor
from cloud_agent.owner_usage import CONTAINER_CURSOR_SCOPE, OwnerContainerCursor

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

DAY = datetime(2026, 9, 20, tzinfo=UTC)
#: Far from either midnight: a sample taken then stands for time on DAY alone.
NOON = DAY + timedelta(hours=12)
GIB = 1024**3


async def container(
    db,
    job,
    sandbox_id,
    *,
    state="stopped",
    last_activity_at=None,
    created_at=DAY,
    memory_limit="4g",
    cpu_limit="2",
    disk_limit_mib=0,
):
    """A retained container whose origin chat is ``job``'s, with no member rows."""
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_sandboxes
               (id, user_id, origin_thread_id, host, container_id, container_name,
                runtime_id, execution_profile, memory_limit, cpu_limit, disk_limit_mib,
                state, created_at, last_activity_at, updated_at)
               VALUES ($1, $2, $3, 'runner-one', $4, $5, 'claude-code', 'untrusted-kata-v1',
                       $6, $7, $8, $9, $10, $11, $10)""",
            sandbox_id,
            job["user_id"],
            job["thread_id"],
            f"container-{sandbox_id}",
            f"agent-{sandbox_id}",
            memory_limit,
            cpu_limit,
            disk_limit_mib,
            state,
            created_at,
            last_activity_at or created_at,
        )


async def join(db, sandbox_id, job):
    """Make ``job``'s chat a member of the container, as a Fork is."""
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_thread_sandbox_memberships (thread_id, user_id, sandbox_id)
               VALUES ($1, $2, $3)""",
            job["thread_id"],
            job["user_id"],
            sandbox_id,
        )


async def execute(db, sql, *args):
    async with db.acquire() as conn:
        await conn.execute(sql, *args)


async def events(db, sandbox_id, *transitions):
    """Record state changes at chosen times, as the lifecycle trigger would."""
    async with db.acquire() as conn:
        for at, to_state in transitions:
            await conn.execute(
                """INSERT INTO agent_sandbox_events
                   (sandbox_id, generation, event_type, to_state, source, created_at)
                   VALUES ($1, 1, 'state_changed', $2, 'system', $3)""",
                sandbox_id,
                to_state,
                at,
            )


async def usage_days(db, sandbox_id):
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM agent_sandbox_usage_days WHERE sandbox_id = $1 ORDER BY day",
            sandbox_id,
        )
    return [dict(row) for row in rows]


def days(start, count):
    return [(start + timedelta(days=n), start + timedelta(days=n + 1)) for n in range(count)]


@contextlib.asynccontextmanager
async def sampled_at(db, sample_id, at):
    """Record the sample ``sample_id`` as taken ``at``.

    The database dates a sample by its own clock, which a test cannot move, so
    the days a sample counts on would depend on when the test runs: one taken
    just after midnight counts partly on the day before. A trigger dates this
    one instead.
    """
    await execute(
        db,
        f"""
        CREATE FUNCTION test_sampled_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.resource_sampled_at := '{at.isoformat()}'::timestamptz;
            RETURN NEW;
        END $$;
        CREATE TRIGGER test_sampled_at BEFORE UPDATE OF resource_snapshot ON agent_sandboxes
            FOR EACH ROW WHEN (NEW.resource_snapshot->>'sample_id' = '{sample_id}')
            EXECUTE FUNCTION test_sampled_at();
        """,
    )
    try:
        yield
    finally:
        await execute(
            db,
            "DROP TRIGGER test_sampled_at ON agent_sandboxes; DROP FUNCTION test_sampled_at();",
        )


async def test_an_owner_sees_only_containers_behind_chats_they_can_reach(db, store):
    origin = await _create_job(store, repo="example-org/app")
    fork = await _create_job(store, repo="example-org/app")
    await container(db, origin, "asbx_running", state="running", last_activity_at=DAY)
    await join(db, "asbx_running", origin)
    await join(db, "asbx_running", fork)

    shelved = await _create_job(store)
    await container(db, shelved, "asbx_shelved", last_activity_at=DAY - timedelta(hours=1))
    await execute(
        db, "UPDATE agent_threads SET archived_at = NOW() WHERE id = $1", shelved["thread_id"]
    )

    removed_chat = await _create_job(store)
    await container(db, removed_chat, "asbx_chat_deleted")
    await execute(
        db, "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", removed_chat["thread_id"]
    )

    gone = await _create_job(store)
    await container(db, gone, "asbx_gone")
    await execute(
        db,
        """UPDATE agent_sandboxes SET deletion_state = 'deleted', deletion_archive_key = 'k',
               deletion_sha256 = $2, deletion_size_bytes = 1 WHERE id = $1""",
        "asbx_gone",
        "a" * 64,
    )

    cold = await _create_job(store)
    await container(db, cold, "asbx_cold", disk_limit_mib=2048)
    await execute(
        db, "UPDATE agent_sandboxes SET deletion_state = 'pending' WHERE id = 'asbx_cold'"
    )

    stranger = await _create_job(store, user_id="user-2")
    await container(db, stranger, "asbx_stranger", state="running")

    page = await store.owner_usage.containers("user-1", cursor=None, limit=50)

    assert [item.id for item in page.containers] == ["asbx_running", "asbx_cold", "asbx_shelved"]
    assert page.total_count == 3
    assert page.counts["running"] == 1 and page.counts["stopped"] == 1
    assert page.counts["deleting"] == 1
    running = page.containers[0]
    assert running.state == "running"
    assert running.repo == "example-org/app"
    assert running.chat_count == 2
    assert {(chat.thread_id, chat.job_id) for chat in running.chats} == {
        (origin["thread_id"], origin["id"]),
        (fork["thread_id"], fork["id"]),
    }
    assert running.cpu_limit_cores == 2.0
    assert running.memory_limit_bytes == 4 * GIB
    assert running.disk_limit_bytes is None
    assert page.containers[1].state == "deleting"
    assert page.containers[1].disk_limit_bytes == 2048 * 1024**2
    shelved_view = page.containers[2]
    assert [(chat.thread_id, chat.archived) for chat in shelved_view.chats] == [
        (shelved["thread_id"], True)
    ]
    body = page.model_dump_json()
    assert "runner-one" not in body and "container-asbx" not in body
    assert (await store.owner_usage.containers("user-2", cursor=None, limit=50)).total_count == 1


async def test_current_use_sums_fresh_samples_of_running_containers_only(db, store):
    jobs = [await _create_job(store) for _ in range(4)]
    for job, (sandbox_id, state) in zip(
        jobs,
        [
            ("asbx_a", "running"),
            ("asbx_b", "running"),
            ("asbx_c", "running"),
            ("asbx_d", "stopping"),
        ],
        strict=True,
    ):
        await container(db, job, sandbox_id, state=state)
    reading = sample(cpu_cores=0.75, memory_bytes=GIB)
    for sandbox_id, age in [
        ("asbx_a", "5 seconds"),
        ("asbx_b", "10 minutes"),
        ("asbx_d", "5 seconds"),
    ]:
        await execute(
            db,
            f"""UPDATE agent_sandboxes SET resource_snapshot = $2::jsonb,
                   resource_sampled_at = NOW() - interval '{age}', resource_received_at = NOW()
               WHERE id = $1""",
            sandbox_id,
            json.dumps(reading),
        )

    page = await store.owner_usage.containers("user-1", cursor=None, limit=50)

    assert page.current.cpu_cores == pytest.approx(0.75)
    assert page.current.memory_bytes == GIB
    assert page.current.measured == 1 and page.current.unmeasured == 2
    by_id = {item.id: item for item in page.containers}
    assert by_id["asbx_a"].reading is not None and not by_id["asbx_a"].reading.stale
    assert by_id["asbx_b"].reading is not None and by_id["asbx_b"].reading.stale
    assert by_id["asbx_c"].reading is None
    # A stopping container's last sample does not describe it any more.
    assert by_id["asbx_d"].reading is None and by_id["asbx_d"].idle_deadline is None


async def test_pages_continue_without_gaps_or_repeats(db, store):
    states = ["stopped", "running", "stopped", "stopped_dirty", "running", "stopped", "lost"]
    for index, state in enumerate(states):
        job = await _create_job(store)
        await container(
            db,
            job,
            f"asbx_{index}",
            state=state,
            # Two share a timestamp, so the identity has to break the tie.
            last_activity_at=DAY + timedelta(minutes=min(index, 5)),
        )
    whole = await store.owner_usage.containers("user-1", cursor=None, limit=50)
    seen, cursor = [], None
    while True:
        page = await store.owner_usage.containers("user-1", cursor=cursor, limit=2)
        seen += [item.id for item in page.containers]
        assert page.total_count == len(states)
        if page.next_cursor is None:
            break
        cursor = decode_cursor(page.next_cursor, OwnerContainerCursor, CONTAINER_CURSOR_SCOPE)
    assert seen == [item.id for item in whole.containers]
    assert seen[:2] == ["asbx_4", "asbx_1"]
    assert len(set(seen)) == len(states)


async def test_running_time_per_utc_day_comes_from_recorded_transitions(db, store):
    job = await _create_job(store)
    # Created stopped before the window; running across the first midnight.
    await container(db, job, "asbx_history", created_at=DAY - timedelta(days=2))
    await events(
        db,
        "asbx_history",
        (DAY - timedelta(hours=2), "starting"),
        (DAY - timedelta(hours=1), "running"),
        (DAY + timedelta(hours=1), "stopping"),
        (DAY + timedelta(hours=1, minutes=1), "stopped"),
        (DAY + timedelta(days=1, hours=23), "running"),
        (DAY + timedelta(days=2, hours=1), "cold"),
    )
    other = await _create_job(store, user_id="user-2")
    await container(db, other, "asbx_other", state="running", created_at=DAY - timedelta(days=1))

    history = await store.owner_usage.resource_history("user-1", buckets=days(DAY, 3))

    assert [day.date for day in history.days] == ["2026-09-20", "2026-09-21", "2026-09-22"]
    assert [day.running_seconds for day in history.days] == pytest.approx([3660.0, 3600.0, 3600.0])
    assert history.totals.running_seconds == pytest.approx(10860.0)
    assert history.totals.measured_seconds == 0


async def test_a_container_still_running_counts_until_the_window_ends(db, store):
    job = await _create_job(store)
    await container(db, job, "asbx_open", created_at=DAY - timedelta(days=1))
    await events(db, "asbx_open", (DAY + timedelta(hours=12), "running"))
    until = DAY + timedelta(hours=18)
    history = await store.owner_usage.resource_history("user-1", buckets=[(DAY, until)])
    assert history.days[0].running_seconds == pytest.approx(6 * 3600)


async def test_a_stop_committed_by_an_earlier_transaction_ends_the_running_time(db, store):
    # Each transition is stamped with its transaction's start. A heartbeat that
    # began before an admin stop, and recorded the stopped container after the
    # stop committed, is stamped before the transition it follows.
    job = await _create_job(store)
    await container(
        db, job, "asbx_raced", state="running", created_at=datetime.now(UTC) - timedelta(hours=1)
    )
    async with db.acquire() as heartbeat, db.acquire() as admin:
        late = heartbeat.transaction()
        await late.start()
        began = await heartbeat.fetchval("SELECT NOW()")
        async with admin.transaction():
            await admin.execute(
                "UPDATE agent_sandboxes SET state = 'stopping' WHERE id = 'asbx_raced'"
            )
        await heartbeat.execute(
            "UPDATE agent_sandboxes SET state = 'stopped' WHERE id = 'asbx_raced'"
        )
        await late.commit()

    until = began + timedelta(hours=6)
    history = await store.owner_usage.resource_history(
        "user-1", buckets=[(until - timedelta(hours=8), until)]
    )

    # It ran for the hour before the stop, not until the window's end.
    assert history.days[0].running_seconds == pytest.approx(3600.0, abs=60.0)


async def test_measured_use_is_read_from_the_owners_daily_totals(db, store):
    mine = await _create_job(store)
    theirs = await _create_job(store, user_id="user-2")
    await container(db, mine, "asbx_mine")
    await container(db, theirs, "asbx_theirs")
    async with db.acquire() as conn:
        await conn.executemany(
            """INSERT INTO agent_sandbox_usage_days
               (sandbox_id, user_id, day, measured_seconds, cpu_core_seconds, memory_byte_seconds)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            [
                ("asbx_mine", "user-1", DAY.date(), 3600.0, 1800.0, 3600.0 * GIB),
                ("asbx_mine", "user-1", (DAY + timedelta(days=1)).date(), 60.0, 30.0, 60.0 * GIB),
                ("asbx_theirs", "user-2", DAY.date(), 999.0, 999.0, 999.0),
            ],
        )
    history = await store.owner_usage.resource_history("user-1", buckets=days(DAY, 2))
    assert [day.measured_seconds for day in history.days] == [3600.0, 60.0]
    assert [day.cpu_core_seconds for day in history.days] == [1800.0, 30.0]
    assert history.totals.memory_byte_seconds == pytest.approx(3660.0 * GIB)


async def test_each_new_sample_adds_the_time_since_the_previous_one(db, store):
    job = await seed(db, store, "asbx_resources")
    first = sample(sample_id="s1", sample_age_ms=0.0, cpu_cores=2.0, cpu_window_seconds=15.0)
    first["memory_bytes"] = GIB
    async with sampled_at(db, "s1", NOON):
        await store.sandbox.reconcile_thread_sandbox_observation(
            **observation(job), resource_usage=first
        )
    [day] = await usage_days(db, "asbx_resources")
    assert day["user_id"] == job["user_id"]
    assert day["day"] == DAY.date()
    # The first sample after a start stands for its own measurement window.
    assert day["measured_seconds"] == pytest.approx(15.0)
    assert day["cpu_core_seconds"] == pytest.approx(30.0)
    assert day["memory_byte_seconds"] == pytest.approx(15.0 * GIB)

    # The host reports again 30 seconds later.
    second = {**first, "sample_id": "s2", "cpu_cores": 1.0}
    async with sampled_at(db, "s2", NOON + timedelta(seconds=30)):
        await store.sandbox.reconcile_thread_sandbox_observation(
            **observation(job), resource_usage=second
        )
    [day] = await usage_days(db, "asbx_resources")
    assert day["measured_seconds"] == pytest.approx(45.0)
    assert day["cpu_core_seconds"] == pytest.approx(60.0)

    # The same sample again, or one without CPU, adds nothing.
    await store.sandbox.reconcile_thread_sandbox_observation(
        **observation(job), resource_usage=second
    )
    await store.sandbox.reconcile_thread_sandbox_observation(
        **observation(job), resource_usage={**second, "sample_id": "s3", "cpu_cores": None}
    )
    assert (await usage_days(db, "asbx_resources"))[0]["measured_seconds"] == day[
        "measured_seconds"
    ]


async def test_a_sample_after_a_stop_does_not_count_the_time_stopped(db, store):
    job = await seed(db, store, "asbx_resources")
    reading = sample(sample_id="before", sample_age_ms=0.0, cpu_window_seconds=10.0)
    async with sampled_at(db, "before", NOON):
        await store.sandbox.reconcile_thread_sandbox_observation(
            **observation(job), resource_usage=reading
        )
    await store.sandbox.reconcile_thread_sandbox_observation(**observation(job, running=False))
    # Had the stop kept the earlier sample, this one would count the 30 seconds since it.
    async with sampled_at(db, "after", NOON + timedelta(seconds=30)):
        await store.sandbox.reconcile_thread_sandbox_observation(
            **observation(job), resource_usage={**reading, "sample_id": "after"}
        )
    [day] = await usage_days(db, "asbx_resources")
    assert day["day"] == DAY.date()
    assert day["measured_seconds"] == pytest.approx(20.0)


async def test_a_sample_across_utc_midnight_counts_on_both_days(db, store):
    """The time a sample stands for is split at UTC midnight.

    It all went to the day the sample was taken on, so up to 120 seconds a
    container used before midnight counted on the next day.
    """
    job = await seed(db, store, "asbx_resources")
    # Two samples thirty seconds apart, twenty of them before midnight.
    before = {**sample(sample_id="before", cpu_cores=2.0), "memory_bytes": GIB}
    await execute(
        db,
        "UPDATE agent_sandboxes SET resource_snapshot = $2::jsonb, resource_sampled_at = $3,"
        " resource_received_at = $3 WHERE id = $1",
        "asbx_resources",
        json.dumps(before),
        DAY - timedelta(seconds=20),
    )
    async with sampled_at(db, "after", DAY + timedelta(seconds=10)):
        await store.sandbox.reconcile_thread_sandbox_observation(
            **observation(job), resource_usage={**before, "sample_id": "after"}
        )

    rows = await usage_days(db, "asbx_resources")
    assert [row["day"] for row in rows] == [(DAY - timedelta(days=1)).date(), DAY.date()]
    assert [row["measured_seconds"] for row in rows] == pytest.approx([20.0, 10.0])
    assert [row["cpu_core_seconds"] for row in rows] == pytest.approx([40.0, 20.0])
    assert [row["memory_byte_seconds"] for row in rows] == pytest.approx([20.0 * GIB, 10.0 * GIB])
    history = await store.owner_usage.resource_history(
        job["user_id"], buckets=days(DAY - timedelta(days=1), 2)
    )
    assert [day.measured_seconds for day in history.days] == pytest.approx([20.0, 10.0])
