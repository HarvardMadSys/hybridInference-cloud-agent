"""The periodic sweep reaches every waiting queue, however many cannot move."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from tests.integration.storage.test_agent_job_store import pool as pool, store as store

from cloud_agent.application.pending_messages import QueueSweep, settle_waiting_queues
from cloud_agent.domain.pending_messages import SettleOutcome

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


class _SweepStorage:
    """The real storage, with some threads whose queue never moves."""

    def __init__(self, store: Storage, blocked: set[str]) -> None:
        self.pending_messages = _SweepRepository(store.pending_messages, blocked)
        self.conversation_queries = store.conversation_queries
        self.attachments = store.attachments
        self.skills = store.skills
        self.source_control = store.source_control


class _SweepRepository:
    def __init__(self, repository, blocked: set[str]) -> None:
        self.repository = repository
        self.blocked = blocked
        self.visited: list[str] = []

    async def threads_to_settle(self, *, limit: int = 50, after: str | None = None) -> list[str]:
        return await self.repository.threads_to_settle(limit=limit, after=after)

    async def settle_queue(self, *, thread_id: str, prepared=None):
        self.visited.append(thread_id)
        if thread_id in self.blocked:
            # As after a gateway failure: nothing moved, and it stays eligible.
            return SettleOutcome()
        return await self.repository.settle_queue(thread_id=thread_id, prepared=prepared)


async def _waiting_threads(store, pool, count: int) -> list[str]:
    jobs = [
        await store.conversation_commands.create_job(
            user_id="sweep-owner",
            repo="owner/name",
            task_prompt=f"pending queue {index}",
            runtime="claude-code",
            model="glm-5.1",
        )
        for index in range(count)
    ]
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', native_turn_no = 1 "
            "WHERE id = ANY($1::text[])",
            [job["id"] for job in jobs],
        )
        for job in jobs:
            await conn.execute(
                "INSERT INTO agent_pending_messages (id, thread_id, user_id, state, prompt) "
                "VALUES ($1, $2, $3, 'queued', 'waiting')",
                f"sweep_message_{uuid.uuid4().hex}",
                job["thread_id"],
                job["user_id"],
            )
    return sorted(job["thread_id"] for job in jobs)


async def test_threads_to_settle_pages_in_thread_order(store, pool):
    threads = await _waiting_threads(store, pool, 5)
    repository = store.pending_messages

    assert await repository.threads_to_settle(limit=2) == threads[:2]
    assert await repository.threads_to_settle(limit=2, after=threads[1]) == threads[2:4]
    assert await repository.threads_to_settle(limit=2, after=threads[3]) == threads[4:]
    assert await repository.threads_to_settle(limit=2, after=threads[4]) == []


async def test_a_page_of_stuck_queues_does_not_hide_the_ones_after_it(store, pool):
    threads = await _waiting_threads(store, pool, 5)
    stuck, healthy = set(threads[:4]), threads[4]
    storage = _SweepStorage(store, stuck)
    sweep = QueueSweep()

    async def run() -> list[str]:
        storage.pending_messages.visited.clear()
        await settle_waiting_queues(storage, gateway=None, github_app=None, limit=2, sweep=sweep)
        return list(dict.fromkeys(storage.pending_messages.visited))

    # Without the sweep, every pass would stop at the same two stuck threads.
    assert await run() == threads[:2]
    assert await run() == threads[2:4]
    # The third pass reaches the thread after them. Whether its message then
    # goes out is the settlement's business, covered with the queue itself.
    assert await run() == [healthy]
    # A short page reached the end: the next pass starts over.
    assert sweep.after is None
    assert await run() == threads[:2]


async def test_a_sweep_rejects_a_zero_page_limit(store):
    with pytest.raises(ValueError, match="limit must be at least 1"):
        await settle_waiting_queues(
            store, gateway=None, github_app=None, limit=0, sweep=QueueSweep()
        )
