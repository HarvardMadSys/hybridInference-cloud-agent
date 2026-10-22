"""Real Postgres checks for one host heartbeat over its whole retained census.

A host lists every retained group it holds on every beat, stopped ones until
their chats are deleted. These pin what the beat may now leave unwritten -- an
observation that would change nothing, a checkpoint receipt already recorded, a
residency already released -- and what it must still do: reconcile a change
the database made on its own, refresh a running container's terminal census,
take a running container out of the archive, leave an unlisted group alone,
and take memberships before their group. They also pin the residency writes
the beat makes together: thread order, and a database failure raised rather
than answered as a lost residency.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from fastapi import BackgroundTasks
from tests.integration.storage.test_admin_sandbox_queries import seed
from tests.integration.storage.test_agent_job_store import _create_job, _waiting_on
from tests.integration.storage.test_attempt_ledger import db as db, store as store
from tests.integration.storage.test_sandbox_checkpoint_receipts import _checkpoint

from cloud_agent.api import hosts
from cloud_agent.schemas import HostHeartbeatRequest
from cloud_agent_shared.sandbox_archive_settings import SandboxArchivePolicyError

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

HOST = "runner-one"
CREDENTIAL = "runner-one-credential-hash"


async def _member(db, sandbox_id: str, job: dict, *, slot: int = 0, **columns: Any) -> None:
    """A member of ``sandbox_id``: settled, stopped, its residency released."""
    values: dict[str, Any] = {
        "resident_state": "stopped",
        "native_state": "ready",
        "context_status": "live",
        "activation_generation": 1,
        "process_generation": 1,
        "residency_generation": 1,
        "residency_status": "none",
        "residency_lease_expires_at": None,
        "residency_grant_id": None,
        **columns,
    }
    names = ", ".join(values)
    placeholders = ", ".join(f"${index}" for index in range(5, 5 + len(values)))
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_thread_sandbox_memberships "
            f"(thread_id, user_id, sandbox_id, resident_slot_id, {names}) "
            f"VALUES ($1, $2, $3, $4, {placeholders})",
            job["thread_id"],
            job["user_id"],
            sandbox_id,
            slot,
            *values.values(),
        )


def _hot(grant_id: str | None = None, *, lease_s: float = 60, **columns: Any) -> dict[str, Any]:
    """A live member whose residency is held, for ``lease_s`` more seconds."""
    return {
        "resident_state": "live",
        "residency_status": "hot",
        "residency_lease_expires_at": datetime.now(UTC) + timedelta(seconds=lease_s),
        "residency_grant_id": grant_id,
        **columns,
    }


async def _stopped_group(db, store, sandbox_id: str) -> dict:
    """A group already recorded as stopped, with its origin member released."""
    job = await seed(db, store, sandbox_id, state="stopped")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET idle_deadline = NULL WHERE id = $1", sandbox_id
        )
    await _member(db, sandbox_id, job)
    return job


def _reported(thread_id: str, **changes: Any) -> dict[str, Any]:
    """A member as the host lists it: stopped, at the generations ``_member`` writes."""
    return {
        "thread_id": thread_id,
        "native_state": "ready",
        "context_status": "live",
        "activation_generation": 1,
        "process_generation": 1,
        "residency_generation": 1,
        "resident_state": "stopped",
        **changes,
    }


def _observed(job: dict, sandbox_id: str, *, running: bool, **changes: Any) -> dict[str, Any]:
    """One listed group, as the heartbeat hands it to the store."""
    return {
        "thread_id": job["thread_id"],
        "sandbox_id": sandbox_id,
        "container_id": f"container-{sandbox_id}",
        "sandbox_generation": 1,
        "running": running,
        "activity_active": True if running else None,
        "idle_remaining_seconds": None,
        "members": [_reported(job["thread_id"], resident_state="live" if running else "stopped")],
        **changes,
    }


def _retained(job: dict, sandbox_id: str, *, running: bool, **changes: Any) -> dict[str, Any]:
    """One listed group, as the host sends it."""
    return {
        "thread_id": job["thread_id"],
        "sandbox_id": sandbox_id,
        "container_id": f"container-{sandbox_id}",
        "generation": 1,
        "activation_generation": 1,
        "running": running,
        "members": [_reported(job["thread_id"], resident_state="live" if running else "stopped")],
        **changes,
    }


async def _versions(db, sandbox_id: str) -> tuple[str, dict[str, str]]:
    """The current version of the group's row and of each member's.

    Any write makes a new one, even of the same values; a lock does not.
    """
    async with db.acquire() as conn:
        group = await conn.fetchval(
            "SELECT xmin::text FROM agent_sandboxes WHERE id = $1", sandbox_id
        )
        members = await conn.fetch(
            "SELECT thread_id, xmin::text AS version FROM agent_thread_sandbox_memberships "
            "WHERE sandbox_id = $1",
            sandbox_id,
        )
    return group, {row["thread_id"]: row["version"] for row in members}


async def _group(db, sandbox_id: str) -> dict[str, Any]:
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM agent_sandboxes WHERE id = $1", sandbox_id)
    assert row is not None
    return dict(row)


async def _membership(db, thread_id: str) -> dict[str, Any]:
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT *, xmin::text AS version FROM agent_thread_sandbox_memberships "
            "WHERE thread_id = $1",
            thread_id,
        )
    assert row is not None
    return dict(row)


def _no_fenced_path(monkeypatch, store) -> None:
    """Fail the test if any observation takes the fenced, locking path."""

    async def fenced(**_kwargs: Any) -> str | None:
        raise AssertionError("an observation that changes nothing took the fenced path")

    monkeypatch.setattr(store.sandbox, "reconcile_thread_sandbox_observation", fenced)


class RecordingGrants:
    """The grant client's shape, recording what a beat revokes."""

    def __init__(self) -> None:
        self.renewed: list[str] = []
        self.revoked: list[str] = []

    async def renew(self, grant_id: str) -> dict[str, Any]:
        self.renewed.append(grant_id)
        return {}

    async def revoke(self, grant_id: str) -> bool:
        self.revoked.append(grant_id)
        return True


async def _enrolled(store) -> None:
    await store.host_pool.register_host(
        host=HOST, credential_hash=CREDENTIAL, capabilities=[], slots_total=4
    )


async def _beat(store, retained: list[dict[str, Any]], *, grants: Any = None, **body: Any) -> Any:
    """One heartbeat through the real handler and the real store.

    Grant renewals and revocations run after the reply is sent, so the tasks
    the handler leaves for then run here once it returns, as a request's do.
    """
    background = BackgroundTasks()
    reply = await hosts.heartbeat_host(
        HostHeartbeatRequest.model_validate({"retained_sandboxes": retained, **body}),
        background_tasks=background,
        host={"host": HOST, "credential_hash": CREDENTIAL},
        store=store,
        grants=grants,
    )
    await background()
    return reply


# -- observations that change nothing ------------------------------------------


async def test_a_stopped_group_already_recorded_takes_no_lock_and_writes_nothing(
    db, store, monkeypatch
):
    job = await _stopped_group(db, store, "asbx_quiet")
    lost = await _create_job(store)
    await _member(db, "asbx_quiet", lost, slot=1, native_state="lost", resident_state="lost")
    before = await _versions(db, "asbx_quiet")
    _no_fenced_path(monkeypatch, store)
    observed = _observed(
        job,
        "asbx_quiet",
        running=False,
        # A lost member stays lost whatever the host says of it.
        members=[_reported(job["thread_id"]), _reported(lost["thread_id"], process_generation=7)],
    )

    assert await store.sandbox.reconcile_thread_sandbox_observations(
        host=HOST, observations=[observed, observed]
    ) == [None, None]
    assert await _versions(db, "asbx_quiet") == before


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "runner-two"},
        {"sandbox_generation": 2},
        {"container_id": "container-replaced"},
        {"thread_id": "athr_not-the-origin"},
        {"sandbox_id": "asbx_missing"},
    ],
)
async def test_an_observation_its_fence_refuses_takes_no_lock(db, store, monkeypatch, changes):
    job = await seed(db, store, "asbx_fenced")
    before = await _versions(db, "asbx_fenced")
    _no_fenced_path(monkeypatch, store)
    observed = {
        **_observed(job, "asbx_fenced", running=True),
        **{key: value for key, value in changes.items() if key != "host"},
    }

    assert await store.sandbox.reconcile_thread_sandbox_observations(
        host=changes.get("host", HOST), observations=[observed]
    ) == [None]
    assert await _versions(db, "asbx_fenced") == before


# -- what the database changed on its own --------------------------------------


@pytest.mark.parametrize(
    ("arrange", "expected"),
    [
        # A stop an administrator asked for, which the container answered.
        (
            "UPDATE agent_sandboxes SET state = 'stopping', stop_requested_at = NOW() "
            "WHERE id = $1",
            {"state": "stopped", "stop_requested_at": None},
        ),
        ("UPDATE agent_sandboxes SET stop_requested_at = NOW() WHERE id = $1", {}),
        # An idle deadline that passed while the host still listed it running.
        (
            "UPDATE agent_sandboxes SET state = 'running', "
            "idle_deadline = NOW() - INTERVAL '1 minute' WHERE id = $1",
            {"state": "stopped", "idle_deadline": None},
        ),
        (
            "UPDATE agent_sandboxes SET terminal_count = 0, terminal_count_observed_at = NOW() "
            "WHERE id = $1",
            {"terminal_count": None, "terminal_count_observed_at": None},
        ),
        (
            "UPDATE agent_sandboxes SET resource_snapshot = '{}'::jsonb, "
            "resource_sampled_at = NOW(), resource_received_at = NOW() WHERE id = $1",
            {"resource_snapshot": None, "resource_sampled_at": None, "resource_received_at": None},
        ),
        (
            "UPDATE agent_sandboxes SET container_id = NULL WHERE id = $1",
            {"container_id": "container-asbx_drift"},
        ),
    ],
)
async def test_a_group_the_database_changed_is_reconciled(db, store, arrange, expected):
    job = await _stopped_group(db, store, "asbx_drift")
    async with db.acquire() as conn:
        await conn.execute(arrange, "asbx_drift")
    before = await _versions(db, "asbx_drift")

    await store.sandbox.reconcile_thread_sandbox_observations(
        host=HOST, observations=[_observed(job, "asbx_drift", running=False)]
    )

    group = await _group(db, "asbx_drift")
    assert {column: group[column] for column in expected} == expected
    assert group["state"] == "stopped"
    assert group["idle_deadline"] is None and group["stop_requested_at"] is None
    assert (await _versions(db, "asbx_drift"))[0] != before[0]


@pytest.mark.parametrize(
    ("stored", "reported", "expected"),
    [
        # A member a stop left marked live, or the host saw restart again.
        ({"resident_state": "live"}, {}, (1, "stopped")),
        ({}, {"process_generation": 2}, (2, "stopped")),
    ],
)
async def test_a_member_the_beat_changes_is_reconciled(db, store, stored, reported, expected):
    job = await seed(db, store, "asbx_member", state="stopped")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET idle_deadline = NULL WHERE id = 'asbx_member'"
        )
    await _member(db, "asbx_member", job, **stored)

    await store.sandbox.reconcile_thread_sandbox_observations(
        host=HOST,
        observations=[
            _observed(
                job, "asbx_member", running=False, members=[_reported(job["thread_id"], **reported)]
            )
        ],
    )

    member = await _membership(db, job["thread_id"])
    assert (member["process_generation"], member["resident_state"]) == expected


@pytest.mark.parametrize(
    ("method", "invalid_update", "kept"),
    [
        ("_record_terminal_observation", "terminal_count = -1", "terminal_count"),
        ("_record_resource_observation", "resource_snapshot = '[]'::jsonb", "resource_snapshot"),
    ],
)
async def test_a_measurement_the_database_refuses_to_clear_does_not_undo_the_stop(
    db, store, monkeypatch, method, invalid_update, kept
):
    """Clearing a stopped container's measurements still runs in its own savepoint.

    Clearing what is already clear takes none, so the measurements here are
    left over from when it ran, and each write is made to fail in turn.
    """
    job = await seed(db, store, "asbx_measured")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET terminal_count = 0, terminal_count_observed_at = NOW(), "
            "resource_snapshot = '{}'::jsonb, resource_sampled_at = NOW(), "
            "resource_received_at = NOW() WHERE id = 'asbx_measured'"
        )

    async def unavailable(conn: Any, *, sandbox_id: str, **_kwargs: Any) -> None:
        await conn.execute(f"UPDATE agent_sandboxes SET {invalid_update} WHERE id = $1", sandbox_id)

    monkeypatch.setattr(store.sandbox, method, unavailable)

    assert await store.sandbox.reconcile_thread_sandbox_observations(
        host=HOST, observations=[_observed(job, "asbx_measured", running=False)]
    ) == ["stopped"]
    group = await _group(db, "asbx_measured")
    assert group["state"] == "stopped" and group["idle_deadline"] is None
    assert group[kept] is not None
    cleared = {"terminal_count", "resource_snapshot"} - {kept}
    assert all(group[column] is None for column in cleared)


async def test_a_group_listed_twice_is_read_again_after_its_first_write(db, store):
    """The second sighting of a group cannot be judged on the read the first one changed."""
    job = await _stopped_group(db, store, "asbx_twice")

    assert await store.sandbox.reconcile_thread_sandbox_observations(
        host=HOST,
        observations=[
            _observed(
                job,
                "asbx_twice",
                running=True,
                activity_active=False,
                idle_remaining_seconds=600,
                terminal_count=1,
            ),
            _observed(job, "asbx_twice", running=False),
        ],
    ) == ["running", None]
    group = await _group(db, "asbx_twice")
    # Running, its deadline not yet passed, so the stop is not recorded; but
    # the census of the container the second sighting says is stopped is gone.
    assert group["state"] == "running"
    assert group["terminal_count"] is None and group["terminal_count_observed_at"] is None


# -- running containers -----------------------------------------------------


async def test_every_running_observation_refreshes_the_terminal_census(db, store):
    """An administrator's stop needs a census newer than 90 seconds, on every beat.

    Nothing else about the group changes, so nothing else is written: not its
    ``updated_at``, not its member.
    """
    job = await seed(db, store, "asbx_busy")
    await _member(db, "asbx_busy", job, **_hot("agr-busy"))
    observed = _observed(job, "asbx_busy", running=True, terminal_count=0)
    await store.sandbox.reconcile_thread_sandbox_observations(host=HOST, observations=[observed])
    first = await _group(db, "asbx_busy")
    _, members = await _versions(db, "asbx_busy")

    assert await store.sandbox.reconcile_thread_sandbox_observations(
        host=HOST, observations=[observed]
    ) == ["running"]

    second = await _group(db, "asbx_busy")
    assert second["terminal_count"] == 0
    assert second["terminal_count_observed_at"] > first["terminal_count_observed_at"]
    assert second["updated_at"] == first["updated_at"]
    assert second["idle_deadline"] is None
    assert (await _versions(db, "asbx_busy"))[1] == members
    detail = await store.admin_sandboxes.detail("asbx_busy")
    assert detail is not None
    assert (
        await store.sandbox.request_admin_stop(
            sandbox_id="asbx_busy",
            generation=1,
            container_id="container-asbx_busy",
            last_activity_at=detail.sandbox.last_activity_at,
            actor_id="admin:operator",
            admin_ip="127.0.0.1",
        )
        == "accepted"
    )


async def test_an_idle_running_group_has_its_deadline_armed_on_every_beat(db, store):
    job = await seed(db, store, "asbx_idle")
    observed = _observed(job, "asbx_idle", running=True, activity_active=False)
    deadlines = []
    for remaining in (900, 600):
        await store.sandbox.reconcile_thread_sandbox_observations(
            host=HOST, observations=[{**observed, "idle_remaining_seconds": remaining}]
        )
        deadlines.append((await _group(db, "asbx_idle"))["idle_deadline"])
    assert deadlines[1] < deadlines[0]


async def test_a_container_observed_running_leaves_the_archive_and_a_stopped_one_does_not(
    db, store
):
    job = await _stopped_group(db, store, "asbx_archived")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET archived_at = NOW() WHERE id = 'asbx_archived'"
        )
    before = await _versions(db, "asbx_archived")

    await store.sandbox.reconcile_thread_sandbox_observations(
        host=HOST, observations=[_observed(job, "asbx_archived", running=False)]
    )
    assert await _versions(db, "asbx_archived") == before
    assert (await _group(db, "asbx_archived"))["archived_at"] is not None

    assert await store.sandbox.reconcile_thread_sandbox_observations(
        host=HOST, observations=[_observed(job, "asbx_archived", running=True)]
    ) == ["running"]
    group = await _group(db, "asbx_archived")
    assert group["archived_at"] is None
    assert group["state"] == "running"


async def test_a_group_the_host_did_not_list_is_left_alone(db, store):
    """Absence is not deletion, nor a stop: the omitted group is not even read."""
    listed = await _stopped_group(db, store, "asbx_listed")
    await seed(db, store, "asbx_omitted")
    omitted = await _versions(db, "asbx_omitted")

    await store.sandbox.reconcile_thread_sandbox_observations(
        host=HOST, observations=[_observed(listed, "asbx_listed", running=True)]
    )
    assert (
        await store.sandbox.reconcile_thread_sandbox_observations(host=HOST, observations=[]) == []
    )

    assert await _versions(db, "asbx_omitted") == omitted
    group = await _group(db, "asbx_omitted")
    assert group["state"] == "running" and group["deletion_state"] == "none"


async def test_reported_members_are_written_together_and_only_where_they_change(db, store):
    origin = await seed(db, store, "asbx_members")
    moved, steady, lost, stranger = [await _create_job(store) for _ in range(4)]
    await _member(db, "asbx_members", origin, slot=0, **_hot())
    await _member(db, "asbx_members", moved, slot=1, **_hot())
    await _member(db, "asbx_members", steady, slot=2, **_hot(process_generation=3))
    await _member(
        db,
        "asbx_members",
        lost,
        slot=3,
        **_hot(native_state="lost", resident_state="lost", process_generation=2),
    )
    _, before = await _versions(db, "asbx_members")

    await store.sandbox.reconcile_thread_sandbox_observations(
        host=HOST,
        observations=[
            _observed(
                origin,
                "asbx_members",
                running=True,
                members=[
                    _reported(origin["thread_id"], resident_state="live"),
                    # Reported twice: the last report is the one applied.
                    _reported(
                        moved["thread_id"], resident_state="restarting", process_generation=4
                    ),
                    _reported(moved["thread_id"], resident_state="live", process_generation=5),
                    # An older process cannot move a member back.
                    _reported(steady["thread_id"], resident_state="starting", process_generation=2),
                    _reported(lost["thread_id"], resident_state="live", process_generation=9),
                    # Not a member of this group: no row to write.
                    _reported(stranger["thread_id"], resident_state="live", process_generation=9),
                ],
            )
        ],
    )

    rows = {
        job["thread_id"]: await _membership(db, job["thread_id"])
        for job in (origin, moved, steady, lost)
    }
    assert (
        rows[moved["thread_id"]]["process_generation"],
        rows[moved["thread_id"]]["resident_state"],
    ) == (5, "live")
    assert rows[moved["thread_id"]]["version"] != before[moved["thread_id"]]
    for job, expected in ((origin, (1, "live")), (steady, (3, "live")), (lost, (2, "lost"))):
        row = rows[job["thread_id"]]
        assert (row["process_generation"], row["resident_state"]) == expected
        assert row["version"] == before[job["thread_id"]]
    async with db.acquire() as conn:
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM agent_thread_sandbox_memberships WHERE thread_id = $1)",
            stranger["thread_id"],
        )


async def test_a_changed_observation_still_takes_members_before_their_group(db, store):
    """Membership, then group: the order every writer of both keeps."""
    job = await _stopped_group(db, store, "asbx_locks")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET stop_requested_at = NOW() WHERE id = 'asbx_locks'"
        )
    async with db.acquire() as holder:
        transaction = holder.transaction()
        await transaction.start()
        reconciliation: asyncio.Task[Any] | None = None
        try:
            pid = await holder.fetchval("SELECT pg_backend_pid()")
            await holder.execute(
                "SELECT 1 FROM agent_thread_sandbox_memberships WHERE thread_id = $1 FOR UPDATE",
                job["thread_id"],
            )
            reconciliation = asyncio.create_task(
                store.sandbox.reconcile_thread_sandbox_observations(
                    host=HOST, observations=[_observed(job, "asbx_locks", running=False)]
                )
            )
            await _waiting_on(db, pid)
            # It waits on the membership without holding the group.
            await holder.execute(
                "SELECT 1 FROM agent_sandboxes WHERE id = 'asbx_locks' FOR UPDATE NOWAIT"
            )
        finally:
            await transaction.rollback()
    assert reconciliation is not None
    assert await asyncio.wait_for(reconciliation, timeout=5) == [None]
    assert (await _group(db, "asbx_locks"))["stop_requested_at"] is None


# -- residencies ------------------------------------------------------------


async def test_live_residencies_are_extended_together(db, store):
    origin = await seed(db, store, "asbx_live")
    grantless, expired, rotated = [await _create_job(store) for _ in range(3)]
    elsewhere = await seed(db, store, "asbx_elsewhere", host="runner-two")
    await _member(db, "asbx_live", origin, slot=0, **_hot("agr-origin"))
    await _member(db, "asbx_live", grantless, slot=1, **_hot())
    await _member(db, "asbx_live", expired, slot=2, **_hot("agr-expired", lease_s=-1))
    await _member(db, "asbx_live", rotated, slot=3, **_hot("agr-rotated", residency_generation=2))
    await _member(db, "asbx_elsewhere", elsewhere, **_hot("agr-elsewhere"))
    before = {
        job["thread_id"]: await _membership(db, job["thread_id"])
        for job in (expired, rotated, elsewhere)
    }

    extended = await store.residency.heartbeat_residencies(
        residencies=[
            (origin["thread_id"], 1),
            (grantless["thread_id"], 1),
            (expired["thread_id"], 1),
            (rotated["thread_id"], 1),
            (elsewhere["thread_id"], 1),
            (origin["thread_id"], 1),
        ],
        ttl_seconds=1800,
        host=HOST,
    )

    assert extended == {(origin["thread_id"], 1): "agr-origin", (grantless["thread_id"], 1): ""}
    soon = datetime.now(UTC) + timedelta(seconds=1700)
    for job in (origin, grantless):
        assert (await _membership(db, job["thread_id"]))["residency_lease_expires_at"] > soon
    for thread_id, row in before.items():
        assert (await _membership(db, thread_id))["version"] == row["version"]


async def test_residency_beats_lock_their_members_in_thread_order(db, store):
    origin = await seed(db, store, "asbx_order")
    other = await _create_job(store)
    first, second = sorted((origin, other), key=lambda job: job["thread_id"])
    # The later thread's row is written first, so a scan in table order meets it first.
    await _member(db, "asbx_order", second, slot=1, **_hot())
    await _member(db, "asbx_order", first, slot=0, **_hot())
    async with db.acquire() as holder:
        transaction = holder.transaction()
        await transaction.start()
        beat: asyncio.Task[Any] | None = None
        try:
            pid = await holder.fetchval("SELECT pg_backend_pid()")
            await holder.execute(
                "SELECT 1 FROM agent_thread_sandbox_memberships WHERE thread_id = $1 FOR UPDATE",
                first["thread_id"],
            )
            beat = asyncio.create_task(
                store.residency.heartbeat_residencies(
                    residencies=[(second["thread_id"], 1), (first["thread_id"], 1)],
                    ttl_seconds=1800,
                    host=HOST,
                )
            )
            await _waiting_on(db, pid)
            # It waits on the first thread without holding the second.
            await holder.execute(
                "SELECT 1 FROM agent_thread_sandbox_memberships WHERE thread_id = $1 "
                "FOR UPDATE NOWAIT",
                second["thread_id"],
            )
        finally:
            await transaction.rollback()
    assert beat is not None
    assert set(await asyncio.wait_for(beat, timeout=5)) == {
        (first["thread_id"], 1),
        (second["thread_id"], 1),
    }


async def test_only_a_residency_still_open_is_offered_for_release(db, store):
    origin = await _stopped_group(db, store, "asbx_release")
    held, rotated = [await _create_job(store) for _ in range(2)]
    await _member(db, "asbx_release", held, slot=1, **_hot(resident_state="stopped"))
    await _member(db, "asbx_release", rotated, slot=2, **_hot(residency_generation=2))

    assert await store.residency.open_shared_residencies(
        residencies=[
            ("asbx_release", origin["thread_id"], 1),  # released
            ("asbx_release", held["thread_id"], 1),
            ("asbx_release", rotated["thread_id"], 1),  # a later generation holds it
            ("asbx_elsewhere", held["thread_id"], 1),  # not its group
        ]
    ) == {("asbx_release", held["thread_id"], 1)}
    assert await store.residency.open_shared_residencies(residencies=[]) == set()


# -- the heartbeat ----------------------------------------------------------


async def test_a_repeated_beat_rewrites_only_the_running_group(db, store):
    await _enrolled(store)
    quiet = await _stopped_group(db, store, "asbx_quiet")
    busy = await seed(db, store, "asbx_busy")
    await _member(db, "asbx_busy", busy, **_hot("agr-busy"))
    retained = [
        _retained(quiet, "asbx_quiet", running=False, checkpoint=_checkpoint("asbx_quiet")),
        _retained(
            busy,
            "asbx_busy",
            running=True,
            activity_active=False,
            idle_remaining_seconds=600,
            terminal_count=0,
        ),
    ]
    grants = RecordingGrants()
    assert (await _beat(store, retained, grants=grants)).residencies_lost == []
    quiet_before = await _versions(db, "asbx_quiet")
    busy_before = await _versions(db, "asbx_busy")
    async with db.acquire() as conn:
        events = await conn.fetchval("SELECT COUNT(*) FROM agent_sandbox_events")

    reply = await _beat(store, retained, grants=grants)

    assert reply.residencies_lost == []
    assert await _versions(db, "asbx_quiet") == quiet_before
    busy_after = await _versions(db, "asbx_busy")
    # The census and the idle deadline, and the residency's lease.
    assert busy_after[0] != busy_before[0]
    assert busy_after[1][busy["thread_id"]] != busy_before[1][busy["thread_id"]]
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_sandbox_events") == events
    assert grants.revoked == []


@pytest.mark.parametrize(
    ("column", "stored", "error"),
    [
        ("sandbox_archive_settings", '{"enabled": true}', SandboxArchivePolicyError),
        # Nothing keeps the version selection an object, as the schema keeps
        # the global settings and the agent updates.
        ("runtime_versions", "[1]", TypeError),
    ],
)
async def test_a_policy_that_does_not_decode_fails_only_the_answer(
    db, store, column: str, stored: str, error: type[Exception]
):
    """The beat reads the policy in its own write, and that read must not fail it.

    A stored policy that does not decode fails the answer, as it always has,
    once the beat is recorded and its observations are reconciled.
    """
    await _enrolled(store)
    job = await _stopped_group(db, store, "asbx_policy")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET state = 'running', "
            "idle_deadline = NOW() - INTERVAL '1 minute' WHERE id = 'asbx_policy'"
        )
        await conn.execute(f"UPDATE agent_runner_policy SET {column} = $1::jsonb", stored)

    with pytest.raises(error):
        await _beat(store, [_retained(job, "asbx_policy", running=False)])

    group = await _group(db, "asbx_policy")
    assert group["state"] == "stopped" and group["idle_deadline"] is None


async def test_an_archive_policy_that_does_not_decode_still_lets_the_beat_switch_an_update(
    db, store
):
    """The agent update a beat completes switches before the archive policy fails the answer.

    Until the beat read the policy itself, the switch came before the answer
    read the archive policy, and one that did not decode failed only the
    answer. Reading it again first kept every update waiting for as long as
    the archive policy stayed broken.
    """
    await _enrolled(store)
    update = {
        "version": "2.1.300",
        "before": "2026-10-15T00:00:00Z",
        "nonce": "4f1c0d2e9b8a4c6d8e0f1a2b3c4d5e6f",
        "previous_version": None,
        "activated_at": None,
    }
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_runner_policy SET sandbox_archive_settings = $1::jsonb, "
            "runtime_upgrades = $2::jsonb",
            json.dumps({"enabled": True}),
            json.dumps({"claude-code": update}),
        )

    with pytest.raises(SandboxArchivePolicyError):
        await _beat(store, [], runtime_versions_available={"claude-code": ["2.1.300"]})

    async with db.acquire() as conn:
        policy = await conn.fetchrow(
            "SELECT runtime_versions, runtime_upgrades FROM agent_runner_policy"
        )
    assert json.loads(policy["runtime_versions"]) == {"claude-code": "2.1.300"}
    assert json.loads(policy["runtime_upgrades"])["claude-code"]["activated_at"]


async def test_a_repeated_checkpoint_receipt_is_not_recorded_again(db, store, monkeypatch):
    await _enrolled(store)
    job = await _stopped_group(db, store, "asbx_receipt")
    recorded: list[str] = []
    record = store.sandbox.record_sandbox_checkpoint

    async def counted(**kwargs: Any) -> bool:
        recorded.append(kwargs["checkpoint"]["checkpoint_id"])
        return await record(**kwargs)

    monkeypatch.setattr(store.sandbox, "record_sandbox_checkpoint", counted)
    first = _checkpoint("asbx_receipt", checkpoint_id="1" * 32)
    for _ in range(2):
        await _beat(store, [_retained(job, "asbx_receipt", running=False, checkpoint=first)])
    assert recorded == ["1" * 32]

    later = _checkpoint("asbx_receipt", checkpoint_id="2" * 32)
    await _beat(store, [_retained(job, "asbx_receipt", running=False, checkpoint=later)])

    assert recorded == ["1" * 32, "2" * 32]
    assert await store.sandbox.recorded_sandbox_checkpoints(
        checkpoint_ids=["1" * 32, "2" * 32, "3" * 32]
    ) == {"1" * 32, "2" * 32}


async def test_a_beat_releases_only_the_residencies_still_open(db, store, monkeypatch):
    await _enrolled(store)
    origin = await _stopped_group(db, store, "asbx_release")
    held = await _create_job(store)
    await _member(db, "asbx_release", held, slot=1, **_hot("agr-held", resident_state="stopped"))
    released: list[str] = []
    release = store.residency.release_shared_residency_if_quiescent

    async def counted(**kwargs: Any) -> str | None:
        released.append(kwargs["thread_id"])
        return await release(**kwargs)

    monkeypatch.setattr(store.residency, "release_shared_residency_if_quiescent", counted)
    retained = [
        _retained(
            origin,
            "asbx_release",
            running=False,
            members=[_reported(origin["thread_id"]), _reported(held["thread_id"])],
        )
    ]
    grants = RecordingGrants()

    for _ in range(2):
        assert (await _beat(store, retained, grants=grants)).residencies_lost == []

    assert released == [held["thread_id"]]
    assert grants.revoked == ["agr-held"]
    assert (await _membership(db, held["thread_id"]))["residency_status"] == "none"


@pytest_asyncio.fixture
async def refused_lease_extension(db):
    """Make the database refuse every residency lease extension, then stop."""
    async with db.acquire() as conn:
        await conn.execute(
            """
            CREATE FUNCTION test_refuse_lease_extension() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'lease extension refused';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        await conn.execute(
            """
            CREATE TRIGGER test_refuse_lease_extension
            BEFORE UPDATE OF residency_lease_expires_at ON agent_thread_sandbox_memberships
            FOR EACH ROW EXECUTE FUNCTION test_refuse_lease_extension()
            """
        )
    try:
        yield
    finally:
        async with db.acquire() as conn:
            await conn.execute(
                "DROP TRIGGER IF EXISTS test_refuse_lease_extension "
                "ON agent_thread_sandbox_memberships"
            )
            await conn.execute("DROP FUNCTION IF EXISTS test_refuse_lease_extension()")


async def test_a_database_failure_is_raised_and_never_answered_as_a_lost_residency(
    db, store, refused_lease_extension
):
    """The host tears a sandbox down when told its residency is lost."""
    await _enrolled(store)
    job = await seed(db, store, "asbx_live")
    await _member(db, "asbx_live", job, **_hot("agr-live"))
    before = await _membership(db, job["thread_id"])

    with pytest.raises(asyncpg.exceptions.RaiseError, match="lease extension refused"):
        await _beat(store, [_retained(job, "asbx_live", running=True, terminal_count=0)])

    after = await _membership(db, job["thread_id"])
    assert after["residency_lease_expires_at"] == before["residency_lease_expires_at"]
    assert after["residency_status"] == "hot"


async def test_a_residency_beat_picked_as_a_deadlock_victim_is_tried_again(db, store):
    await _enrolled(store)
    job = await seed(db, store, "asbx_live")
    await _member(db, "asbx_live", job, **_hot("agr-live"))
    before = await _membership(db, job["thread_id"])
    async with db.acquire() as conn:
        # A sequence, unlike a row, keeps its count when the victim rolls back.
        await conn.execute("CREATE SEQUENCE test_beat_deadlocks")
        await conn.execute(
            """
            CREATE FUNCTION test_first_beat_deadlocks() RETURNS trigger AS $$
            BEGIN
                IF nextval('test_beat_deadlocks') = 1 THEN
                    RAISE EXCEPTION 'deadlock detected' USING ERRCODE = 'deadlock_detected';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        await conn.execute(
            """
            CREATE TRIGGER test_first_beat_deadlocks
            BEFORE UPDATE OF residency_lease_expires_at ON agent_thread_sandbox_memberships
            FOR EACH ROW EXECUTE FUNCTION test_first_beat_deadlocks()
            """
        )
    try:
        reply = await _beat(store, [_retained(job, "asbx_live", running=True, terminal_count=0)])
    finally:
        async with db.acquire() as conn:
            await conn.execute(
                "DROP TRIGGER IF EXISTS test_first_beat_deadlocks "
                "ON agent_thread_sandbox_memberships"
            )
            await conn.execute("DROP FUNCTION IF EXISTS test_first_beat_deadlocks()")
            await conn.execute("DROP SEQUENCE IF EXISTS test_beat_deadlocks")

    assert reply.residencies_lost == []
    after = await _membership(db, job["thread_id"])
    assert after["residency_lease_expires_at"] > before["residency_lease_expires_at"]


async def test_a_beat_with_no_census_touches_no_group(db, store):
    """A beat whose sandbox manager could not be read lists nothing; that deletes nothing."""
    await _enrolled(store)
    await _stopped_group(db, store, "asbx_quiet")
    await seed(db, store, "asbx_busy")
    before = {
        sandbox_id: await _versions(db, sandbox_id) for sandbox_id in ("asbx_quiet", "asbx_busy")
    }

    for body in ({}, {"retained_sandboxes": []}):
        await hosts.heartbeat_host(
            HostHeartbeatRequest.model_validate(body),
            background_tasks=BackgroundTasks(),
            host={"host": HOST, "credential_hash": CREDENTIAL},
            store=store,
            grants=None,
        )

    assert {sandbox_id: await _versions(db, sandbox_id) for sandbox_id in before} == before
