"""The bounded recovery page eventually reaches queues behind stuck threads."""

from __future__ import annotations

import asyncio

import pytest
from tests.integration.storage.test_agent_job_store import pool as pool, store as store

from cloud_agent.application.pending_messages import settle_waiting_queues
from cloud_agent.domain.pending_messages import SettleOutcome
from cloud_agent.source_control_runtime import SourceControlUnavailable
from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


class _UnavailableGitHubApp:
    def get(self) -> object:
        raise SourceControlUnavailable("github")


class _SweepStorage:
    def __init__(self, store: Storage, visited: list[str]) -> None:
        self.pending_messages = _SweepRepository(
            store.pending_messages, visited
        )
        self.conversation_queries = store.conversation_queries
        self.attachments = store.attachments
        self.skills = store.skills
        self.source_control = store.source_control


class _SweepRepository:
    def __init__(self, repository, visited: list[str]) -> None:
        self.repository = repository
        self.blocked_threads: set[str] = set()
        self.first_page: list[str] | None = None
        self.visited = visited

    async def threads_to_settle(
        self, *, limit: int = 50, scan_interval_seconds: float = 30.0
    ) -> list[str]:
        page = await self.repository.threads_to_settle(limit=limit)
        if self.first_page is None:
            self.first_page = page
            self.blocked_threads.update(page)
        return page

    async def settle_queue(self, *, thread_id: str, prepared=None):
        self.visited.append(thread_id)
        if thread_id in self.blocked_threads:
            return SettleOutcome()
        return await self.repository.settle_queue(thread_id=thread_id, prepared=prepared)


async def test_repeated_bounded_sweeps_rotate_past_unsettled_threads(store, pool):
    jobs = [
        await store.conversation_commands.create_job(
            user_id="st02-owner",
            repo="owner/name",
            task_prompt=f"pending queue {index}",
            runtime="claude-code",
            model="glm-5.1",
        )
        for index in range(51)
    ]
    thread_ids = {job["thread_id"] for job in jobs}
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', native_turn_no = 1 "
            "WHERE id = ANY($1::text[])",
            [job["id"] for job in jobs],
        )
        for index, job in enumerate(jobs):
            await conn.execute(
                "INSERT INTO agent_pending_messages (id, thread_id, user_id, state, prompt) "
                "VALUES ($1, $2, $3, 'queued', $4)",
                f"st02_message_{index:03d}",
                job["thread_id"],
                job["user_id"],
                "waiting",
            )

    repo = store.pending_messages
    assert set(await repo.threads_to_settle(limit=100)) == thread_ids

    visited: list[str] = []
    sweep_store = _SweepStorage(store, visited)

    # The first 50 remain eligible, as they would after a transient gateway
    # failure. The production sweep keeps reaching those threads, which return
    # retryable no-op outcomes; the healthy thread remains beyond the page.
    await settle_waiting_queues(
        sweep_store, gateway=None, github_app=_UnavailableGitHubApp(), limit=50
    )
    blocked_threads = sweep_store.pending_messages.blocked_threads
    assert len(blocked_threads) == 50
    healthy_later_thread = next(iter(thread_ids.difference(blocked_threads)))
    assert set(visited) == blocked_threads

    # A later bounded sweep must rotate its starting point and reach the
    # healthy thread through the same application code path.
    visited.clear()
    await asyncio.sleep(30.05)
    await settle_waiting_queues(
        sweep_store, gateway=None, github_app=_UnavailableGitHubApp(), limit=50
    )
    assert healthy_later_thread in visited

    # Selection is derived from shared database time, not process-local cursor
    # state, so another worker/restarted process sees the same current page.
    restarted_repo = Storage(pool).pending_messages
    concurrent_pages = await asyncio.gather(
        repo.threads_to_settle(limit=50),
        restarted_repo.threads_to_settle(limit=50),
    )
    assert healthy_later_thread in set(concurrent_pages[0]) | set(concurrent_pages[1])
