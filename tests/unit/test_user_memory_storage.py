"""Owner scoping, durable reset fences and serialized user-memory controls."""

from __future__ import annotations

import asyncio
import copy
import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from cloud_agent.storage.repositories import user_memory as memory_storage
from cloud_agent.storage.repositories.user_memory import PostgresUserMemoryRepository

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


def activity(message_id=1, content="I prefer small changes."):
    return {
        "id": message_id,
        "message_id": message_id,
        "job_id": f"job-{message_id}",
        "content": content[:8000],
        "content_digest": hashlib.md5(content.encode()).hexdigest(),
        "repo": "owner/project",
        "runtime": "codex",
        "model": "gpt-6",
        "title": "Review API changes",
        "created_at": NOW + timedelta(seconds=message_id),
    }


class MemoryPool:
    """A transactional fake with real per-owner asynchronous locks."""

    def __init__(self):
        self.profiles = {}
        self.activity = {}
        self.locks = {}
        self.queries = []
        self.pause_owner = None
        self.read_started = asyncio.Event()
        self.read_release = asyncio.Event()

    @asynccontextmanager
    async def acquire(self):
        yield MemoryConnection(self)


class MemoryConnection:
    def __init__(self, pool):
        self.pool = pool
        self.lock = None
        self.owner = None

    @asynccontextmanager
    async def transaction(self):
        try:
            yield self
        finally:
            if self.lock is not None:
                self.lock.release()

    async def execute(self, sql, user_id):
        assert "pg_advisory_xact_lock" in sql
        self.lock = self.pool.locks.setdefault(user_id, asyncio.Lock())
        await self.lock.acquire()
        self.owner = user_id
        self.pool.queries.append((sql, (user_id,)))

    async def fetchrow(self, sql, *params):
        self.pool.queries.append((sql, params))
        user_id = params[0]
        current = self.pool.profiles.get(user_id)
        if "SELECT *" in sql:
            return copy.deepcopy(current)
        assert self.owner == user_id, "all user-memory writes require the owner's lock"
        if "INSERT INTO" in sql:
            self.pool.profiles[user_id] = {
                "user_id": user_id,
                "enabled": True,
                "portrait": params[1],
                "memories": params[2],
                "suggestions": params[3],
                "revision": 0,
                "updated_at": NOW,
                "cleared_message_id": 0,
                "cleared_at": None,
                "activity_digest": "",
            }
            return copy.deepcopy(self.pool.profiles[user_id])
        assert current is not None
        if "SET enabled" in sql:
            current["enabled"] = params[1]
        else:
            current.update(
                portrait=params[1],
                memories=params[2],
                suggestions=params[3],
                activity_digest=params[4],
            )
            if "cleared_message_id =" in sql:
                current["cleared_message_id"] = max(
                    (row["id"] for row in self.pool.activity.get(user_id, [])), default=0
                )
                current["cleared_at"] = NOW + timedelta(hours=1)
        current["revision"] += 1
        current["updated_at"] += timedelta(seconds=1)
        return copy.deepcopy(current)

    async def fetch(self, sql, *params):
        self.pool.queries.append((sql, params))
        if "SELECT m.id" in sql:
            user_id, watermark, cleared_at = params
            assert self.owner == user_id, "source reads must share the privacy-control lock"
            if self.pool.pause_owner == user_id:
                self.pool.pause_owner = None
                self.pool.read_started.set()
                await self.pool.read_release.wait()
            return copy.deepcopy(
                [
                    row
                    for row in self.pool.activity.get(user_id, [])
                    if row["id"] > watermark
                    and (cleared_at is None or row["created_at"] > cleared_at)
                ][:50]
            )
        cursor, limit = params
        owners = sorted(set(self.pool.activity) | set(self.pool.profiles))
        owners = [
            owner for owner in owners if self.pool.profiles.get(owner, {}).get("enabled", True)
        ]
        if "candidates.user_id <= $1" in sql:
            owners = [owner for owner in owners if owner <= cursor]
        elif cursor is not None:
            owners = [owner for owner in owners if owner > cursor]
        return [{"user_id": owner} for owner in owners[:limit]]


@pytest.mark.asyncio
async def test_refresh_detects_edits_and_deletions_without_rebuilding_unchanged_activity(
    monkeypatch,
):
    pool = MemoryPool()
    pool.activity["alice"] = [activity()]
    repository = PostgresUserMemoryRepository(pool)
    builder = Mock(wraps=memory_storage.build_user_memory)
    monkeypatch.setattr(memory_storage, "build_user_memory", builder)
    first = await repository.refresh("alice")
    assert first["memories"]
    assert set(first) == {
        "portrait",
        "memories",
        "suggestions",
        "enabled",
        "revision",
        "updated_at",
    }
    assert await repository.get("bob") is None
    builder.reset_mock()
    assert await repository.refresh("alice") == first
    builder.assert_not_called()

    # The same message ID can change; edits past the input cap also invalidate
    # the fingerprint even when the builder's visible content stays identical.
    pool.activity["alice"] = [activity(content="x" * 8000 + "old")]
    second = await repository.refresh("alice")
    pool.activity["alice"] = [activity(content="x" * 8000 + "new")]
    third = await repository.refresh("alice")
    assert third["revision"] == second["revision"] + 1
    assert "content_digest" not in builder.call_args.args[0][0]
    pool.activity["alice"] = []
    cleared = await repository.refresh("alice")
    assert cleared["memories"] == []
    assert cleared["portrait"]["projects"] == []
    assert cleared["suggestions"] == []


@pytest.mark.asyncio
async def test_disabled_memory_skips_activity_and_clear_excludes_old_messages(monkeypatch):
    pool = MemoryPool()
    pool.activity["alice"] = [activity()]
    repository = PostgresUserMemoryRepository(pool)
    await repository.refresh("alice")
    disabled = await repository.set_enabled("alice", False)
    builder = Mock(wraps=memory_storage.build_user_memory)
    monkeypatch.setattr(memory_storage, "build_user_memory", builder)
    query_count = len(pool.queries)
    assert await repository.refresh("alice") == disabled
    assert not any("SELECT m.id" in sql for sql, _ in pool.queries[query_count:])
    builder.assert_not_called()
    assert await repository.set_enabled("alice", False) == disabled

    cleared = await repository.clear("alice")
    assert cleared["enabled"] is False
    assert cleared["memories"] == cleared["suggestions"] == []
    assert cleared["portrait"]["projects"] == []
    await repository.set_enabled("alice", True)
    forgotten = await repository.refresh("alice")
    assert forgotten["memories"] == []
    future = activity(2, "I prefer test-driven changes.")
    future["created_at"] = NOW + timedelta(hours=2)
    pool.activity["alice"].insert(0, future)
    refreshed = await repository.refresh("alice")
    assert {memory["source_job_id"] for memory in refreshed["memories"]} == {"job-2"}


@pytest.mark.asyncio
async def test_reenable_refreshes_removed_source_before_returning_knowledge():
    pool = MemoryPool()
    pool.activity["alice"] = [activity()]
    repository = PostgresUserMemoryRepository(pool)
    assert (await repository.refresh("alice"))["memories"]
    await repository.set_enabled("alice", False)
    pool.activity["alice"] = []
    reenabled = await repository.set_enabled("alice", True)
    assert reenabled["enabled"] is True
    assert reenabled["memories"] == reenabled["suggestions"] == []
    assert reenabled == await repository.set_enabled("alice", True)


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["disable", "clear"])
async def test_inflight_refresh_cannot_overwrite_disable_or_clear(control):
    pool = MemoryPool()
    pool.activity["alice"] = [activity()]
    repository = PostgresUserMemoryRepository(pool)
    pool.pause_owner = "alice"
    refreshing = asyncio.create_task(repository.refresh("alice"))
    await asyncio.wait_for(pool.read_started.wait(), timeout=1)
    mutation = asyncio.create_task(
        repository.set_enabled("alice", False)
        if control == "disable"
        else repository.clear("alice")
    )
    await asyncio.sleep(0)
    assert not mutation.done(), "privacy controls must wait for the same owner lock"
    pool.read_release.set()
    await asyncio.gather(refreshing, mutation)
    result = await repository.refresh("alice")
    if control == "disable":
        assert result["enabled"] is False
    else:
        assert result["memories"] == result["suggestions"] == []


@pytest.mark.asyncio
async def test_background_batches_visit_every_owner_and_do_not_increment_idle_profiles():
    pool = MemoryPool()
    pool.activity = {owner: [activity()] for owner in ["alice", "bob", "carol", "dana", "erin"]}
    repository = PostgresUserMemoryRepository(pool)
    await repository.set_enabled("bob", False)
    assert await repository.refresh_active_users(limit=0) == 0
    for _ in range(2):
        assert await repository.refresh_active_users(limit=2) == 2
    assert set(pool.profiles) == set(pool.activity)
    before = copy.deepcopy(pool.profiles)
    for _ in range(2):
        await repository.refresh_active_users(limit=2)
    assert pool.profiles == before
    # Existing profiles remain eligible when their final visible source goes
    # away, so a deleted conversation cannot leave retained derived knowledge.
    del pool.activity["alice"]
    await repository.refresh_active_users(limit=100)
    assert (await repository.get("alice"))["memories"] == []
    assert pool.profiles["bob"]["activity_digest"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_owner", ["alice", "bob"])
async def test_permanent_first_or_middle_owner_failure_does_not_starve_later_owners(
    monkeypatch, caplog, failing_owner
):
    pool = MemoryPool()
    pool.activity = {owner: [activity()] for owner in ["alice", "bob", "carol", "dana"]}
    repository = PostgresUserMemoryRepository(pool)
    refresh = repository.refresh
    visited = []

    async def fail_one_owner(user_id):
        visited.append(user_id)
        if user_id == failing_owner:
            raise ValueError("sensitive user content must not enter logs")
        return await refresh(user_id)

    monkeypatch.setattr(repository, "refresh", fail_one_owner)
    for _ in range(3):
        assert await repository.refresh_active_users(limit=2) == 2
    assert set(pool.profiles) == set(pool.activity) - {failing_owner}
    assert visited == ["alice", "bob", "carol", "dana", "alice", "bob"]
    assert len(caplog.records) == 2
    assert all(
        record.message == "agent_user_memory_owner_refresh_failed" for record in caplog.records
    )
    assert all(record.exc_info is None for record in caplog.records)
    assert "sensitive" not in caplog.text


@pytest.mark.asyncio
async def test_batch_cancellation_propagates_and_releases_locks_for_the_next_owner(caplog):
    pool = MemoryPool()
    pool.activity = {owner: [activity()] for owner in ["alice", "bob"]}
    pool.pause_owner = "alice"
    repository = PostgresUserMemoryRepository(pool)
    refreshing = asyncio.create_task(repository.refresh_active_users(limit=2))
    await asyncio.wait_for(pool.read_started.wait(), timeout=1)
    refreshing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await refreshing
    assert not caplog.records
    assert await asyncio.wait_for(repository.refresh_active_users(limit=1), timeout=1) == 1
    assert (await repository.get("bob"))["memories"]


@pytest.mark.asyncio
async def test_source_query_scopes_both_owners_and_bounds_only_authored_live_messages():
    pool = MemoryPool()
    pool.activity["alice"] = [activity()]
    repository = PostgresUserMemoryRepository(pool)
    await repository.refresh("alice")
    sql, params = next((sql, params) for sql, params in pool.queries if "SELECT m.id" in sql)
    assert "j.user_id = $1" in sql and "t.user_id = $1" in sql
    assert "j.thread_id = m.thread_id" in sql and "t.user_id = j.user_id" in sql
    assert "t.deleted_at IS NULL" in sql and "m.role = 'user'" in sql
    assert "j.fork_source_job_id IS NULL" in sql
    assert "(j.turn_source <> 'goal' OR m.mid_turn)" in sql
    assert "(j.turn_source <> 'goal' OR m.mid_turn)" in memory_storage._ACTIVE_MEMORY_USERS
    assert "left(m.content, 8000)" in sql and "LIMIT 50" in sql
    assert "m.id > $2" in sql and "m.created_at > $3" in sql
    assert "ORDER BY m.created_at DESC, m.id DESC" in sql
    assert params == ("alice", 0, None)
    await repository.clear("alice")
    clear_sql = next(sql for sql, _ in pool.queries if "cleared_message_id =" in sql)
    assert "j.user_id = $1" in clear_sql and "t.user_id = $1" in clear_sql
    assert "t.deleted_at IS NULL" not in clear_sql, "reset must fence hidden histories too"
