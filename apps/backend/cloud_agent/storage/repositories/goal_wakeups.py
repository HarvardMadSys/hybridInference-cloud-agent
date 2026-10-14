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

import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from cloud_agent.goals.models import WAKEUP_BACKOFF_MAX_S
from cloud_agent.settings import number_setting

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


async def _lock_live_wakeup(conn: Any, wakeup_id: str, lease_owner: str) -> bool:
    """Lock first so expiry is evaluated after any row-lock wait."""
    locked = await conn.fetchval(
        "SELECT id FROM agent_goal_wakeups "
        "WHERE id = $1 AND state = 'pending' AND lease_owner = $2 FOR UPDATE",
        wakeup_id,
        lease_owner,
    )
    if locked is None:
        return False
    return bool(
        await conn.fetchval(
            "SELECT lease_expires_at > clock_timestamp() FROM agent_goal_wakeups WHERE id = $1",
            wakeup_id,
        )
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

    async def claim_wakeups(
        self, *, limit: int = 1, thread_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Lease up to ``limit`` due wakeups, oldest first, and return them.

        ``thread_id`` takes only that conversation's, which is what a finished
        turn drains; ``None`` takes the deployment's, which is the reaper's
        pass. The lease lasts two minutes. The decision under it commits in one
        transaction, but its consumer may first ask the Gateway and GitHub
        whether the owner and the repository still allow the work, so a lease
        can be held across network calls. Each claim spends one attempt, which
        stays spent only if the consumer backs off or its lease expires.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                UPDATE agent_goal_wakeups
                SET lease_owner = $1, lease_expires_at = clock_timestamp() + make_interval(secs => $2),
                    attempts = attempts + 1, updated_at = NOW()
                WHERE id IN (
                    SELECT id FROM agent_goal_wakeups
                    WHERE state = 'pending'
                      AND waiting_job_id IS NULL
                      AND not_before <= NOW()
                      AND (lease_expires_at IS NULL OR lease_expires_at < clock_timestamp())
                      AND ($4::text IS NULL OR thread_id = $4)
                    ORDER BY created_at
                    LIMIT $3
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING {_WAKEUP_COLUMNS}
                """,
                uuid.uuid4().hex,
                120.0,
                limit,
                thread_id,
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
        lease_owner: str | None = None,
        restore_attempt: bool = False,
    ) -> None:
        """Give a lease back, optionally parking it behind a turn.

        Parking is not a failure: "a person's turn is already waiting, judge
        this again when it ends" is the ordinary answer, and the turn's finish
        will clear ``waiting_job_id``. ``retry_in_seconds`` is for the other
        case — a temporary scheduling failure — and it defers the row's
        ``not_before`` rather than its lease, so a backoff and a dead worker's
        expiry never look alike. ``lease_owner`` fences a consumer's write.

        Only a backoff keeps the attempt its claim spent. A release without
        ``retry_in_seconds`` gives it back, so a goal that waited behind its
        owner's turns still has its retries for a real failure;
        ``restore_attempt`` gives it back for a backoff that is not the
        wakeup's failure either, such as an access-service outage.
        """
        if conn is not None:
            await _release_on(
                conn,
                wakeup_id=wakeup_id,
                waiting_job_id=waiting_job_id,
                retry_in_seconds=retry_in_seconds,
                detail=detail,
                lease_owner=lease_owner,
                restore_attempt=restore_attempt,
            )
            return
        async with self._pool.acquire() as acquired, acquired.transaction():
            await _release_on(
                acquired,
                wakeup_id=wakeup_id,
                waiting_job_id=waiting_job_id,
                retry_in_seconds=retry_in_seconds,
                detail=detail,
                lease_owner=lease_owner,
                restore_attempt=restore_attempt,
            )

    async def finish_wakeup(
        self,
        *,
        wakeup_id: str,
        processed: bool = True,
        detail: str | None = None,
        conn: Any | None = None,
        lease_owner: str | None = None,
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
              AND ($4::text IS NULL OR (
                  state = 'pending' AND lease_owner = $4 AND lease_expires_at > clock_timestamp()
              ))
            """
        if conn is not None:
            if lease_owner is not None and not await _lock_live_wakeup(
                conn, wakeup_id, lease_owner
            ):
                return
            await conn.execute(sql, wakeup_id, processed, detail, lease_owner)
            return
        async with self._pool.acquire() as acquired, acquired.transaction():
            if lease_owner is not None and not await _lock_live_wakeup(
                acquired, wakeup_id, lease_owner
            ):
                return
            await acquired.execute(sql, wakeup_id, processed, detail, lease_owner)

    async def lock_wakeup(self, *, wakeup_id: str, lease_owner: str, conn: Any) -> bool:
        """Fence a goal mutation on its live wakeup lease under the caller's transaction.

        Call after locking the conversation, matching the goal-write lock order.
        Holding this row lock makes the decision and wakeup completion atomic
        against another consumer reclaiming an expired lease.
        Check the wall clock: NOW() stays fixed while a transaction waits for
        the conversation lock and can predate the lease's expiry.
        """
        return await _lock_live_wakeup(conn, wakeup_id, lease_owner)

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
    lease_owner: str | None,
    restore_attempt: bool,
) -> None:
    """Release, back off and optionally restore the claim attempt in one fenced write."""
    delay = (
        min(
            retry_in_seconds,
            number_setting("goals.wakeup_backoff_max_seconds", WAKEUP_BACKOFF_MAX_S),
        )
        if retry_in_seconds is not None
        else None
    )
    # ``attempts`` is what ``is_exhausted`` reads, and it counts failures: a
    # claim spends one, and every release that is not a backoff — a park
    # behind a turn, a yield, or a hand-back to be judged again — gives it back.
    restore = restore_attempt or retry_in_seconds is None
    if lease_owner is not None and not await _lock_live_wakeup(conn, wakeup_id, lease_owner):
        return
    await conn.execute(
        """
        UPDATE agent_goal_wakeups
        SET lease_owner = NULL, lease_expires_at = NULL,
            waiting_job_id = COALESCE($2, waiting_job_id),
            detail = COALESCE($3, detail),
            not_before = CASE WHEN $4::double precision IS NULL THEN not_before
                              ELSE clock_timestamp() + make_interval(secs => $4) END,
            attempts = GREATEST(attempts - CASE WHEN $6 THEN 1 ELSE 0 END, 0),
            updated_at = NOW()
        WHERE id = $1 AND state = 'pending'
          AND ($5::text IS NULL OR (lease_owner = $5 AND lease_expires_at > clock_timestamp()))
        """,
        wakeup_id,
        waiting_job_id,
        detail,
        delay,
        lease_owner,
        restore,
    )


__all__ = [
    "PostgresGoalWakeupRepository",
]
