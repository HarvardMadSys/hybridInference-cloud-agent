"""Postgres implementation of the durable goal-scheduling queue.

A wakeup says "this goal needs to be looked at", and nothing more. It is not an
authorization to run: it names the goal identity and revision it was created
for, and a consumer that finds either has moved discards it. That is what makes
the queue safe to replay after a crash, and what stops a wakeup that outlived
its goal from resurrecting it.

Immutability is the other half. A row is inserted once, by the same transaction
that created the obligation — an owner's write, or a turn's terminal
transition — and it is only ever updated to be *taken* or to be *finished*.
Consumption is idempotent not because the consumer remembers anything but
because each decision writes its own outcome in the same commit.
"""

from __future__ import annotations

import math
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from cloud_agent.goals.models import (
    MAX_WAKEUP_ATTEMPTS,
    WAKEUP_BACKOFF_MAX_S,
    WAKEUP_BACKOFF_S,
)

if TYPE_CHECKING:
    import asyncpg

    from cloud_agent.goals.models import GoalWakeupTrigger

#: The columns a consumer needs to decide, plus the lease it holds while it does.
_WAKEUP_COLUMNS = (
    "id, thread_id, goal_id, revision, trigger, trigger_ref, state, "
    "waiting_job_id, attempts, detail, lease_owner, lease_expires_at, "
    "not_before, created_at, updated_at"
)

#: One obligation, inserted once. The partial unique index is what makes a
#: replayed finish or a retried owner write find the row it already made; a
#: *processed* row does not block a genuinely new event with the same name,
#: because the index only covers ``pending``.
_ARM_SQL = """
INSERT INTO agent_goal_wakeups
    (id, thread_id, goal_id, revision, trigger, trigger_ref, waiting_job_id)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (thread_id, trigger, trigger_ref) WHERE state = 'pending'
DO NOTHING
RETURNING id
"""


async def _insert(
    conn: Any,
    thread_id: str,
    goal_id: str | None,
    revision: int,
    trigger: str,
    trigger_ref: str,
    waiting_job_id: str | None,
) -> str | None:
    """The one INSERT, so the helper and the repository cannot drift."""
    return await conn.fetchval(
        _ARM_SQL,
        uuid.uuid4().hex,
        thread_id,
        goal_id,
        revision,
        trigger,
        trigger_ref,
        waiting_job_id,
    )


class PostgresGoalWakeupRepository:
    """The pending-advance queue behind one conversation's goal."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    @asynccontextmanager
    async def transaction(self) -> Any:
        """One transaction, over one connection, for a whole decision.

        The controller holds this across "read the goal under its lock → create
        the round → mark the wakeup processed". Anything less and a crash
        between the last two leaves either a round nobody accounted for or a
        pending wakeup that would create a second one.

        It is on this port because every method here takes the connection it
        produces: the queue is the only subject whose work spans a transaction
        the caller owns end to end.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            yield conn

    async def arm_wakeup(
        self,
        *,
        thread_id: str,
        goal_id: str | None,
        revision: int,
        trigger: GoalWakeupTrigger,
        trigger_ref: str,
        waiting_job_id: str | None = None,
        conn: Any | None = None,
    ) -> str | None:
        """Record the obligation, or return ``None`` if it already exists.

        Idempotent on ``(thread, goal_id, revision, trigger, trigger_ref)``: a
        replayed finish or a retried owner write finds the wakeup it already
        made instead of making a second one. ``conn`` lets a caller that is
        already inside the transaction that moved the goal arm the wakeup in
        that same commit.
        """
        if conn is not None:
            # The caller is already inside the transaction that moved the goal;
            # the obligation has to commit with it or not at all.
            return await _insert(
                conn, thread_id, goal_id, revision, trigger, trigger_ref, waiting_job_id
            )
        async with self._pool.acquire() as acquired:
            return await _insert(
                acquired, thread_id, goal_id, revision, trigger, trigger_ref, waiting_job_id
            )

    async def claim_wakeups(self, *, limit: int = 1) -> list[dict[str, Any]]:
        """Lease up to ``limit`` due wakeups and return them.

        A lease is short and the work under it is one decision against the
        database, never a network call.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                UPDATE agent_goal_wakeups
                SET lease_owner = $1, lease_expires_at = NOW() + make_interval(secs => $2),
                    attempts = attempts + 1, updated_at = NOW()
                WHERE id IN (
                    SELECT id FROM agent_goal_wakeups
                    WHERE state = 'pending'
                      AND waiting_job_id IS NULL
                      AND not_before <= NOW()
                      AND (lease_expires_at IS NULL OR lease_expires_at < NOW())
                    ORDER BY created_at
                    LIMIT $3
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING {_WAKEUP_COLUMNS}
                """,
                uuid.uuid4().hex,
                120.0,
                limit,
            )
        return [dict(row) for row in rows]

    async def release_wakeup(
        self,
        *,
        wakeup_id: str,
        waiting_job_id: str | None = None,
        retry_in_seconds: float | None = None,
        detail: str | None = None,
        conn: Any | None = None,
    ) -> None:
        """Give a lease back, optionally parking it behind a turn.

        Parking is not a failure: "a person's turn is already waiting, judge
        this again when it ends" is the ordinary answer, and the turn's finish
        will clear ``waiting_job_id``. ``retry_in_seconds`` is for the other
        case — a temporary scheduling failure — and it defers the row's
        ``not_before`` rather than its lease, so a backoff and a dead worker's
        expiry never look alike.
        """
        if conn is not None:
            await _release_on(
                conn,
                wakeup_id=wakeup_id,
                waiting_job_id=waiting_job_id,
                retry_in_seconds=retry_in_seconds,
                detail=detail,
            )
            return
        async with self._pool.acquire() as acquired:
            await _release_on(
                acquired,
                wakeup_id=wakeup_id,
                waiting_job_id=waiting_job_id,
                retry_in_seconds=retry_in_seconds,
                detail=detail,
            )

    async def finish_wakeup(
        self,
        *,
        wakeup_id: str,
        processed: bool = True,
        detail: str | None = None,
        conn: Any | None = None,
    ) -> None:
        """Mark a wakeup processed, or record a permanent refusal.

        A refusal is still terminal: a goal that cannot be scheduled must be
        paused with a reason, not retried behind an "active" badge. The row
        stays as the audit record of what was decided.
        """
        sql = """
            UPDATE agent_goal_wakeups
            SET state = CASE WHEN $2 THEN 'processed' ELSE 'pending' END,
                lease_owner = NULL, lease_expires_at = NULL,
                detail = COALESCE($3, detail),
                updated_at = NOW()
            WHERE id = $1
            """
        if conn is not None:
            await conn.execute(sql, wakeup_id, processed, detail)
            return
        async with self._pool.acquire() as acquired:
            await acquired.execute(sql, wakeup_id, processed, detail)

    async def pending_wakeup(self, *, thread_id: str) -> dict[str, Any] | None:
        """The newest unprocessed wakeup of a conversation, if any."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {_WAKEUP_COLUMNS} FROM agent_goal_wakeups
                WHERE thread_id = $1 AND state = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                thread_id,
            )
        return None if row is None else dict(row)


async def _release_on(
    conn: Any,
    *,
    wakeup_id: str,
    waiting_job_id: str | None,
    retry_in_seconds: float | None,
    detail: str | None,
) -> None:
    """The two statements a release is, on whichever connection owns them."""
    await conn.execute(
        """
        UPDATE agent_goal_wakeups
        SET lease_owner = NULL, lease_expires_at = NULL,
            waiting_job_id = COALESCE($2, waiting_job_id),
            detail = COALESCE($3, detail),
            updated_at = NOW()
        WHERE id = $1 AND state = 'pending'
        """,
        wakeup_id,
        waiting_job_id,
        detail,
    )
    if retry_in_seconds is not None:
        await conn.execute(
            """
            UPDATE agent_goal_wakeups
            SET not_before = NOW() + make_interval(secs => $2)
            WHERE id = $1 AND state = 'pending'
            """,
            wakeup_id,
            min(retry_in_seconds, WAKEUP_BACKOFF_MAX_S),
        )


def wakeup_backoff(attempts: int) -> float:
    """How long to wait before trying a failed wakeup again.

    Doubling from a few seconds to the cap. The point is to stop a hot loop over
    a database that is refusing writes, not to schedule anything precisely:
    every wakeup that matters is also re-armed by the finish that unblocks it.
    """
    if attempts <= 1:
        return WAKEUP_BACKOFF_S
    return min(WAKEUP_BACKOFF_S * math.pow(2, attempts - 1), WAKEUP_BACKOFF_MAX_S)


def is_exhausted(attempts: int) -> bool:
    """Whether a wakeup has failed often enough to stop retrying it."""
    return attempts >= MAX_WAKEUP_ATTEMPTS


__all__ = [
    "PostgresGoalWakeupRepository",
    "is_exhausted",
    "wakeup_backoff",
]
