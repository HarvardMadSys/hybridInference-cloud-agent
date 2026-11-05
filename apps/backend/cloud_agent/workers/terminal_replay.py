"""Fair, bounded replay of durable settled-terminal markers."""

from __future__ import annotations

import asyncio
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from datetime import datetime

    from cloud_agent.ports import SandboxRepository

logger = get_logger("cloud_agent.app")
REPLAY_PAGE_SIZE = 100
REPLAY_CONCURRENCY = 8
REPLAY_HOST_CONCURRENCY = 2
REPLAY_PASS_DEADLINE_S = 5.0


class TerminalReplayStore(Protocol):
    """The durable marker and routing projection replay consumes."""

    sandbox: SandboxRepository


@dataclass
class ReplayCursor:
    """Progress owned by the replay loop, not by any request or remote host."""

    after: tuple[datetime, str] | None = None


async def _resume(store: TerminalReplayStore, job: dict[str, Any]) -> None:
    from cloud_agent.sandbox_manager_routing import (
        hydrate_shared_sandbox_manager,
        sandbox_manager_for_host,
    )
    from cloud_agent.terminal_coordination import resume_settled_terminal

    job_id = job["id"]
    try:
        if job.get("thread_sandbox") is None:
            await store.sandbox.mark_terminal_resume_complete(job_id=job_id)
            return
        manager = await hydrate_shared_sandbox_manager(job, sandbox_manager_for_host(job))
        if await resume_settled_terminal(job["thread_id"], job_id=job_id, sandbox_manager=manager):
            await store.sandbox.mark_terminal_resume_complete(job_id=job_id)
    except Exception:
        # Only a confirmed resume clears a marker. Cancellation propagates;
        # another process or a later pass can safely replay the same job fence.
        logger.warning(
            "Settled terminal reconciliation failed",
            exc_info=True,
            extra={"event": "agent_terminal_reconcile_failed", "job_id": job_id},
        )


async def _run_page(
    store: TerminalReplayStore, rows: list[dict[str, Any]], cursor: ReplayCursor
) -> None:
    pending = deque(enumerate(rows))
    running: dict[asyncio.Task[None], str] = {}
    hosts: Counter[str] = Counter()
    started: set[int] = set()
    position = 0
    try:
        while pending or running:
            # Queue by host without occupying a global slot waiting for that
            # host's semaphore. Later healthy hosts can use the remaining slots.
            for _ in range(len(pending)):
                index, row = pending.popleft()
                host = row.get("host") or ""
                if len(running) >= REPLAY_CONCURRENCY or hosts[host] >= REPLAY_HOST_CONCURRENCY:
                    pending.append((index, row))
                    continue
                running[asyncio.create_task(_resume(store, row))] = host
                hosts[host] += 1
                started.add(index)
                # Advance only across a contiguous dispatched prefix. A
                # deadline must not forever skip rows waiting in this page.
                while position in started:
                    cursor.after = (rows[position]["updated_at"], rows[position]["id"])
                    position += 1
            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                hosts[running.pop(task)] -= 1
                task.result()
    finally:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


async def _reconcile_settled_terminals(
    store: TerminalReplayStore, *, cursor: ReplayCursor | None = None
) -> None:
    """Try one bounded page, retaining fair progress even across a timed-out pass."""
    cursor = cursor if cursor is not None else ReplayCursor()
    started = time.monotonic()
    try:
        async with asyncio.timeout(REPLAY_PASS_DEADLINE_S):
            rows = await store.sandbox.terminal_resume_page(
                after=cursor.after, limit=REPLAY_PAGE_SIZE
            )
            if rows:
                await _run_page(store, rows, cursor)
            if len(rows) < REPLAY_PAGE_SIZE:
                cursor.after = None
    except TimeoutError:
        logger.warning("Settled terminal reconciliation deadline reached")
    finally:
        logger.debug(
            "agent_terminal_reconcile_pass",
            extra={
                "duration_s": time.monotonic() - started,
                "continuing": cursor.after is not None,
            },
        )
