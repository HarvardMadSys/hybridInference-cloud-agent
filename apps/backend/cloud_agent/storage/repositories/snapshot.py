"""Postgres implementation of the group-snapshot port.

Lifted verbatim from the single storage class this package replaced: the
statements, their bound parameters and their transaction ownership are what
they were, and only the class holding them changed.
"""

from __future__ import annotations

import hashlib
import math
import secrets
from typing import TYPE_CHECKING, Any

import asyncpg

from cloud_agent.logging_utils import get_logger
from cloud_agent.storage._sql.events import insert_event
from cloud_agent.storage._sql.hosts import REVOKED_HOST_SQL, refuse_start_on_revoked_host
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage._sql.sandbox import FENCE_START_SQL, fence_sandbox_start

if TYPE_CHECKING:
    from datetime import datetime

logger = get_logger(__name__)

#: How long a capture handed back by a busy or unreachable host waits before it
#: is claimed again. Until then the coordinator captures the workspaces behind
#: it, and sleeps when there are none.
CAPTURE_RETRY_S = 5.0
#: How long a published base whose import failed waits before it is retried.
BASE_IMPORT_RETRY_S = 15.0
#: The longest a publication waits on its group's base import while that import
#: keeps failing. Past it the publication fails visibly rather than holding the
#: group in ``importing`` for good: a commit GitHub no longer serves, or a
#: bundle the workspace cannot take, never imports however often it is retried.
BASE_IMPORT_WAIT_LIMIT_S = 3600.0
#: A publication whose entitlement preflight failed waits before retrying.
PUBLISH_RETRY_S = 30.0
#: How many transient failures may return a publication to the durable queue.
#: The next failure fails the snapshot, and the group's next capture supersedes it.
PUBLISH_MAX_ATTEMPTS = 5
#: The longest a retry waits for GitHub's rate limit to lift. The primary limit
#: resets within the hour; a longer wait it names is not believed.
PROVIDER_MAX_WAIT_S = 3600.0


def _retry_wait_s(delay_s: float | None, *, floor: float) -> float:
    """Wait at least ``floor`` before a retry, and as long as GitHub asked, up to the cap."""
    if delay_s is None or not math.isfinite(delay_s):
        return floor
    return min(max(delay_s, floor), PROVIDER_MAX_WAIT_S)


async def _fold_into_pending_snapshot(
    conn: Any, *, snapshot_id: int, sandbox_id: str
) -> int | None:
    """Give a capture's turns to its group's pending snapshot; ``None`` if there is none.

    A publication request is refused while a capture is on its way, so a
    pending snapshot beside a capture is one that turn settlement opened before
    publication became an owner's request. A group may hold one pending
    snapshot, so a capture handed back cannot become pending beside it. It is
    folded in instead: the pending snapshot takes the capture's turns and keeps
    its own boundary, anchor and base -- what settlement would have left had
    the capture never been claimed -- and the capture's row goes. A capture is
    the whole diff from the published base, so the pending snapshot, captured
    later, covers every turn of both.

    Called with the capture and its group locked; the pending snapshot is taken
    after the group, as settlement takes it. The turns' rows are moved, not
    copied, and the anchor is left alone: a new row or a new anchor would
    share-lock its turn through the foreign key after the group, the reverse of
    a follow-up, which holds the newest turn while it waits for the group.
    """
    pending = await conn.fetchval(
        """
        SELECT id FROM agent_sandbox_snapshots
        WHERE sandbox_id = $1 AND state = 'pending'
        FOR UPDATE
        """,
        sandbox_id,
    )
    if pending is None:
        return None
    await conn.execute(
        """
        UPDATE agent_snapshot_jobs moved
        SET snapshot_id = $2
        WHERE moved.snapshot_id = $1
          AND NOT EXISTS (
              SELECT 1 FROM agent_snapshot_jobs kept
              WHERE kept.snapshot_id = $2 AND kept.job_id = moved.job_id
          )
        """,
        snapshot_id,
        pending,
    )
    await conn.execute("DELETE FROM agent_sandbox_snapshots WHERE id = $1", snapshot_id)
    return int(pending)


class PostgresSnapshotRepository:
    """Capture, publish and base-import, each claimed with SKIP LOCKED."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def snapshot_database_time(self) -> datetime:
        """Use the database clock for deadlines written by database transactions."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT clock_timestamp()")

    async def request_snapshot_publication(self, *, thread_id: str, user_id: str) -> dict[str, Any]:
        """Open the next snapshot of a conversation's workspace for its owner.

        The platform never publishes on its own: a turn boundary opens no
        snapshot, and this is the one way a workspace reaches its branch. The
        group's epoch advances and a ``pending`` snapshot opens at it, holding
        every turn settled in the group since the last snapshot -- or, when
        there is none, the group's newest settled turn, so that a retry after a
        failure, or a file changed from the terminal, can still be published.
        Capture, publication and base import then run as they always have; the
        insert wakes the capture coordinator.

        A snapshot already on its way is returned rather than doubled, so a
        repeated request is harmless. Every request opens a fresh epoch rather
        than reopening a failed snapshot: the failed one keeps the commit it may
        have pushed, which the new one's publication builds on (``own_pushes``).

        Locks follow :mod:`cloud_agent.storage`: the conversation, then the
        group, then the snapshot it opens. The included turns are share-locked
        with ``NOWAIT`` after the group, out of order, so a follow-up holding
        its newest turn while it waits for the group fails this request
        (``workspace_busy``) instead of deadlocking with it.

        Returns:
            ``{"snapshot_id", "state", "requested"}``, where ``requested`` says
            whether this call opened the snapshot, or ``{"error": code}`` with
            ``not_found``, ``temporary_workspace``, ``nothing_to_publish``,
            ``workspace_unavailable``, ``turn_running`` or ``workspace_busy``.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            thread = await conn.fetchrow(
                """
                SELECT id, repo FROM agent_threads
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                FOR UPDATE
                """,
                thread_id,
                user_id,
            )
            if thread is None:
                return {"error": "not_found"}
            if thread["repo"] is None:
                return {"error": "temporary_workspace"}
            sandbox_id = await conn.fetchval(
                "SELECT sandbox_id FROM agent_thread_sandbox_memberships WHERE thread_id = $1",
                thread_id,
            )
            if sandbox_id is None:
                return {"error": "nothing_to_publish"}
            group = await conn.fetchrow(
                """
                SELECT id, state, deletion_state, published_base_sha
                FROM agent_sandboxes WHERE id = $1
                FOR UPDATE
                """,
                sandbox_id,
            )
            if group is None:
                return {"error": "nothing_to_publish"}
            in_flight = await conn.fetchrow(
                """
                SELECT id, state FROM agent_sandbox_snapshots
                WHERE sandbox_id = $1
                  AND state IN ('pending', 'capturing', 'captured', 'publishing')
                ORDER BY workspace_epoch DESC, id DESC
                LIMIT 1
                """,
                sandbox_id,
            )
            if in_flight is not None:
                return {
                    "snapshot_id": int(in_flight["id"]),
                    "state": in_flight["state"],
                    "requested": False,
                }
            if await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM agent_thread_sandbox_memberships m
                    WHERE m.sandbox_id = $1
                      AND (
                          m.active_attempt_id IS NOT NULL
                          OR m.fork_operation_id IS NOT NULL
                          OR EXISTS (
                              SELECT 1 FROM agent_jobs j
                              WHERE j.thread_id = m.thread_id AND j.state = 'running'
                          )
                      )
                )
                """,
                sandbox_id,
            ):
                return {"error": "turn_running"}
            if (
                group["deletion_state"] in ("pending", "archived", "deleted")
                or group["state"] not in ("running", "stopped")
                or not group["published_base_sha"]
            ):
                return {"error": "workspace_unavailable"}
            # Settled turns no snapshot holds yet. A conversation's turns
            # settle in order and each request takes all of them, so only the
            # ones after its newest snapshotted turn can be missing; the bound
            # keeps turns from before publication was snapshotted out of it. A
            # Fork's copies of its source's turns ran in no workspace and are
            # never included.
            included = [
                row["id"]
                for row in await conn.fetch(
                    """
                    SELECT j.id
                    FROM agent_thread_sandbox_memberships m
                    JOIN agent_jobs j ON j.thread_id = m.thread_id
                    WHERE m.sandbox_id = $1
                      AND j.native_turn_no IS NOT NULL
                      AND j.fork_source_job_id IS NULL
                      AND j.state IN ('succeeded', 'failed', 'cancelled')
                      AND NOT EXISTS (
                          SELECT 1 FROM agent_snapshot_jobs held WHERE held.job_id = j.id
                      )
                      AND j.turn_no > COALESCE((
                          SELECT MAX(snapshotted.turn_no)
                          FROM agent_jobs snapshotted
                          JOIN agent_snapshot_jobs held ON held.job_id = snapshotted.id
                          WHERE snapshotted.thread_id = j.thread_id
                      ), 0)
                    ORDER BY j.created_at, j.id
                    """,
                    sandbox_id,
                )
            ]
            if not included:
                newest = await conn.fetchval(
                    """
                    SELECT j.id
                    FROM agent_thread_sandbox_memberships m
                    JOIN agent_jobs j ON j.id = m.last_settled_job_id
                    WHERE m.sandbox_id = $1
                      AND j.fork_source_job_id IS NULL
                      AND j.state IN ('succeeded', 'failed', 'cancelled')
                    ORDER BY j.created_at DESC, j.id DESC
                    LIMIT 1
                    """,
                    sandbox_id,
                )
                if newest is None:
                    return {"error": "nothing_to_publish"}
                included = [newest]
            try:
                await conn.execute(
                    """
                    SELECT id FROM agent_jobs
                    WHERE id = ANY($1::text[])
                    ORDER BY id
                    FOR KEY SHARE NOWAIT
                    """,
                    included,
                )
            except asyncpg.exceptions.LockNotAvailableError:
                return {"error": "workspace_busy"}
            epoch = await conn.fetchval(
                """
                UPDATE agent_sandboxes
                SET workspace_epoch = workspace_epoch + 1,
                    snapshot_state = CASE
                        WHEN snapshot_state IN ('snapshotting', 'importing') THEN snapshot_state
                        ELSE 'pending'
                    END,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING workspace_epoch
                """,
                sandbox_id,
            )
            # The anchor must be one of the included turns; a deferred foreign
            # key checks that at commit.
            anchor = included[-1]
            snapshot_id = await conn.fetchval(
                """
                INSERT INTO agent_sandbox_snapshots (
                    sandbox_id, workspace_epoch, anchor_job_id, base_sha
                )
                VALUES ($1, $2, $3, $4)
                RETURNING id
                """,
                sandbox_id,
                epoch,
                anchor,
                group["published_base_sha"],
            )
            # In settlement order: the publication reads its turns, their
            # summaries and their messages by this timestamp.
            await conn.execute(
                """
                INSERT INTO agent_snapshot_jobs (snapshot_id, job_id, created_at)
                SELECT $1, included.job_id,
                       NOW() + included.position * INTERVAL '1 microsecond'
                FROM unnest($2::text[]) WITH ORDINALITY AS included(job_id, position)
                """,
                snapshot_id,
                included,
            )
        logger.info(
            "agent_snapshot_publication_requested",
            extra={
                "event": "agent_snapshot_publication_requested",
                "thread_id": thread_id,
                "snapshot_id": snapshot_id,
                "workspace_epoch": epoch,
                "turns": len(included),
            },
        )
        return {"snapshot_id": int(snapshot_id), "state": "pending", "requested": True}

    async def claim_due_sandbox_snapshot(self) -> dict[str, Any] | None:
        """Fence one quiescent shared workspace for bounded patch capture.

        This claim never waits for publication and never changes a job state.
        The host revalidates quiescence against its live operation ledger; a
        turn that entered between this transaction and the sandbox manager call simply
        makes the capture retry later.

        An earlier snapshot still on its way to publication holds this one
        back. One that failed does not, and neither does the ``dirty`` state
        it left on the group: a capture is the whole diff from the group's
        published base, which a failure never advances, so this later one
        carries the failed one's changes, or their reversal, and supersedes it.

        A group still on a host that has left the pool waits: capturing starts
        its container, which nothing may do there any more
        (:func:`refuse_start_on_revoked_host`). Once the group moves, its new
        host captures it.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"""
                SELECT s.id AS snapshot_id, s.sandbox_id, s.workspace_epoch,
                       s.anchor_job_id, s.base_sha, g.generation,
                       g.origin_thread_id, g.container_id, g.container_name,
                       g.runtime_id, g.runtime_version, g.sandbox_image_digest,
                       g.memory_limit, g.cpu_limit, g.disk_limit_mib, g.stop_timeout_seconds,
                       g.resident_slot_capacity,
                       COALESCE((
                           SELECT jsonb_agg(
                               jsonb_build_object(
                                   'thread_id', m.thread_id,
                                   'native_session_id', m.native_session_id,
                                   'context_status', m.context_status,
                                   'activation_generation', m.activation_generation,
                                   'process_generation', m.process_generation,
                                   'residency_generation', m.residency_generation,
                                   'resident_state', m.resident_state,
                                   'resident_slot_id', m.resident_slot_id,
                                   'last_settled_job_id', m.last_settled_job_id
                               ) ORDER BY m.created_at, m.thread_id
                           )
                           FROM agent_thread_sandbox_memberships m
                           WHERE m.sandbox_id = g.id
                             AND m.resident_slot_id IS NOT NULL
                       ), '[]'::jsonb) AS members
                FROM agent_sandbox_snapshots s
                JOIN agent_sandboxes g ON g.id = s.sandbox_id
                WHERE s.state = 'pending'
                  AND (s.retry_at IS NULL OR s.retry_at <= NOW())
                  AND g.state IN ('running', 'stopped')
                  AND g.stop_requested_at IS NULL
                  AND g.deletion_state NOT IN ('pending', 'archived', 'deleted')
                  AND NOT {REVOKED_HOST_SQL.format(host="g.host")}
                  -- A group's one pending snapshot is its newest epoch, so in a
                  -- dirty group it is the later capture that supersedes the
                  -- failure that dirtied it.
                  AND g.snapshot_state IN ('pending', 'dirty')
                  AND g.active_operation_count = 0
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_thread_sandbox_memberships active
                      WHERE active.sandbox_id = g.id
                        AND active.active_attempt_id IS NOT NULL
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM agent_snapshot_jobs included
                      JOIN agent_jobs included_job ON included_job.id = included.job_id
                      WHERE included.snapshot_id = s.id
                        AND included_job.state NOT IN ('succeeded', 'failed', 'cancelled')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_sandbox_snapshots prior
                      WHERE prior.sandbox_id = s.sandbox_id
                        AND prior.workspace_epoch < s.workspace_epoch
                        AND prior.state NOT IN ('published', 'failed')
                  )
                ORDER BY s.created_at, s.id
                LIMIT 1
                FOR UPDATE OF s, g SKIP LOCKED
                """
            )
            if row is None:
                return None
            # The host was read before the group's lock was this claim's; a
            # revocation that committed meanwhile shows only from here.
            group = await conn.fetchrow(
                "SELECT id, state, host FROM agent_sandboxes WHERE id = $1", row["sandbox_id"]
            )
            if await refuse_start_on_revoked_host(conn, group) is not None:
                return None
            captured = await conn.fetchval(
                """
                UPDATE agent_sandbox_snapshots
                SET state = 'capturing', last_error = NULL, retry_at = NULL, updated_at = NOW()
                WHERE id = $1 AND state = 'pending'
                RETURNING id
                """,
                row["snapshot_id"],
            )
            if captured is None:
                return None
            # Capturing starts the container if it is stopped.
            await conn.execute(
                f"""
                UPDATE agent_sandboxes
                SET snapshot_state = 'snapshotting', idle_deadline = NULL,
                    {FENCE_START_SQL}, updated_at = NOW()
                WHERE id = $1 AND snapshot_state IN ('pending', 'dirty')
                """,
                row["sandbox_id"],
            )
            # Read after the fence: the capture must name the start it fenced.
            start_seq = await conn.fetchval(
                "SELECT start_seq FROM agent_sandboxes WHERE id = $1", row["sandbox_id"]
            )
        result = dict(row)
        result["members"] = load_json(result.get("members")) or []
        result["start_seq"] = start_seq
        return result

    async def record_sandbox_snapshot_capture(
        self,
        *,
        snapshot_id: int,
        sandbox_id: str,
        sandbox_generation: int,
        container_id: str,
        workspace_epoch: int,
        base_sha: str,
        patch: str,
        content_sha256: str,
    ) -> str | None:
        """Store one immutable sandbox manager receipt and release the capture barrier.

        Returns ``captured`` or ``published`` (for an empty no-op snapshot),
        and ``None`` for a stale/mismatched receipt.
        """
        digest = hashlib.sha256(patch.encode("utf-8")).hexdigest()
        if not secrets.compare_digest(digest, content_sha256):
            raise ValueError("sandbox snapshot content hash does not match its patch")
        async with self._pool.acquire() as conn, conn.transaction():
            capture = await conn.fetchval(
                """
                SELECT sandbox_id FROM agent_sandbox_snapshots
                WHERE id = $1 AND sandbox_id = $2 AND workspace_epoch = $3
                  AND base_sha = $4 AND state = 'capturing'
                FOR UPDATE
                """,
                snapshot_id,
                sandbox_id,
                workspace_epoch,
                base_sha,
            )
            if capture is None:
                return None
            # Lock and revalidate the exact Docker lifecycle identity before
            # accepting an asynchronous host receipt. A stale capture from a
            # superseded generation must not mark its replacement RUNNING or
            # become that replacement's publication base.
            group = await conn.fetchval(
                """
                SELECT id FROM agent_sandboxes
                WHERE id = $1 AND generation = $2 AND container_id = $3
                  AND snapshot_state = 'snapshotting'
                FOR UPDATE
                """,
                sandbox_id,
                sandbox_generation,
                container_id,
            )
            if group is None:
                return None
            state = "captured" if patch.strip() else "published"
            row = await conn.fetchrow(
                """
                UPDATE agent_sandbox_snapshots
                SET patch_content = $5, content_sha256 = $6, state = $7,
                    published_commit_sha = CASE WHEN $7 = 'published' THEN base_sha END,
                    captured_at = NOW(),
                    published_at = CASE WHEN $7 = 'published' THEN NOW() END,
                    last_error = NULL, updated_at = NOW()
                WHERE id = $1 AND sandbox_id = $2 AND workspace_epoch = $3
                  AND base_sha = $4 AND state = 'capturing'
                RETURNING sandbox_id
                """,
                snapshot_id,
                sandbox_id,
                workspace_epoch,
                base_sha,
                patch,
                content_sha256,
                state,
            )
            if row is None:
                return None
            await conn.execute(
                """
                UPDATE agent_sandboxes g
                SET state = 'running',
                    snapshot_state = CASE
                        WHEN EXISTS (
                            SELECT 1 FROM agent_sandbox_snapshots pending
                            WHERE pending.sandbox_id = g.id AND pending.state = 'pending'
                        ) THEN 'pending' ELSE 'quiescent' END,
                    idle_deadline = CASE
                        WHEN g.active_operation_count = 0
                         AND NOT EXISTS (
                            SELECT 1 FROM agent_thread_sandbox_memberships active
                            WHERE active.sandbox_id = g.id
                              AND active.active_attempt_id IS NOT NULL
                         )
                        THEN clock_timestamp() + make_interval(secs => g.idle_timeout_seconds)
                        ELSE NULL END,
                    updated_at = NOW()
                WHERE g.id = $1 AND g.generation = $2 AND g.container_id = $3
                  AND g.snapshot_state = 'snapshotting'
                """,
                sandbox_id,
                sandbox_generation,
                container_id,
            )
        return state

    async def defer_sandbox_snapshot_capture(
        self,
        *,
        snapshot_id: int,
        sandbox_id: str,
        sandbox_generation: int,
        container_id: str,
        detail: str,
        dirty: bool = False,
    ) -> bool:
        """Release a failed host capture; busy retries, unsafe census dirties.

        A capture handed back for a retry is not claimed again before its
        ``retry_at``, so a busy host is asked on that clock, not in a loop. A
        turn that settled meanwhile left a pending snapshot, and the capture is
        folded into it, which then waits for that retry instead. A dirtied
        capture fails for good; the group's next capture supersedes it.
        """
        next_state = "failed" if dirty else "pending"
        group_state = "dirty" if dirty else "pending"
        async with self._pool.acquire() as conn, conn.transaction():
            claimed_sandbox_id = await conn.fetchval(
                """
                SELECT sandbox_id FROM agent_sandbox_snapshots
                WHERE id = $1 AND state = 'capturing'
                FOR UPDATE
                """,
                snapshot_id,
            )
            if claimed_sandbox_id is None:
                return False
            if str(claimed_sandbox_id) != sandbox_id:
                raise ValueError("snapshot claim belongs to a different sandbox")
            group = await conn.fetchval(
                """
                SELECT id FROM agent_sandboxes
                WHERE id = $1 AND generation = $2 AND container_id = $3
                  AND snapshot_state = 'snapshotting'
                FOR UPDATE
                """,
                sandbox_id,
                sandbox_generation,
                container_id,
            )
            if group is None:
                # The async result belongs to an obsolete Docker identity.
                # Make that immutable snapshot visibly failed, but leave the
                # successor group's lifecycle fields untouched.
                await conn.execute(
                    """
                    UPDATE agent_sandbox_snapshots
                    SET state = 'failed',
                        last_error = 'snapshot capture lost its sandbox lifecycle fence',
                        updated_at = NOW()
                    WHERE id = $1 AND state = 'capturing'
                    """,
                    snapshot_id,
                )
                return False
            retried = snapshot_id
            if not dirty:
                retried = (
                    await _fold_into_pending_snapshot(
                        conn, snapshot_id=snapshot_id, sandbox_id=sandbox_id
                    )
                    or snapshot_id
                )
            await conn.execute(
                """
                UPDATE agent_sandbox_snapshots
                SET state = $2, last_error = $3, updated_at = NOW(),
                    retry_at = CASE WHEN $2 = 'pending'
                                    THEN clock_timestamp() + make_interval(secs => $4) END
                WHERE id = $1 AND state IN ('capturing', 'pending')
                """,
                retried,
                next_state,
                detail[:2000],
                CAPTURE_RETRY_S,
            )
            await conn.execute(
                """
                UPDATE agent_sandboxes
                SET snapshot_state = $2,
                    idle_deadline = CASE WHEN active_operation_count = 0
                         AND NOT EXISTS (
                            SELECT 1 FROM agent_thread_sandbox_memberships active
                            WHERE active.sandbox_id = agent_sandboxes.id
                              AND active.active_attempt_id IS NOT NULL
                         )
                        THEN clock_timestamp() + make_interval(secs => idle_timeout_seconds)
                        ELSE NULL END,
                    last_error = CASE WHEN $2 = 'dirty' THEN $3 ELSE last_error END,
                    updated_at = NOW()
                WHERE id = $1 AND generation = $4 AND container_id = $5
                  AND snapshot_state = 'snapshotting'
                """,
                sandbox_id,
                group_state,
                detail[:2000],
                sandbox_generation,
                container_id,
            )
        return True

    async def reimport_snapshot_base(
        self,
        *,
        snapshot_id: int,
        sandbox_id: str,
        sandbox_generation: int,
        container_id: str,
        base_sha: str,
    ) -> bool:
        """Hand back a capture whose workspace lost its published base, and import it again.

        The host imports a published commit as objects no ref names, so Git's
        own housekeeping prunes them once their pack is old enough, and the
        next capture of that base finds it missing. Every later capture is cut
        from the same base, so failing the snapshot left the workspace
        unpublishable for good. Instead the group goes back to ``importing``
        for the same ``published_base_sha``, as publication leaves it: the
        start is fenced, the idle deadline cleared, and the base-import worker
        woken by the trigger on that state. The capture goes back to
        ``pending`` with no retry delay; a group that is importing is not
        claimed, so it waits, and the import's completion makes it due again.
        A pending snapshot beside it takes its turns, as a deferred capture's.

        Returns ``False``, changing nothing, unless this exact capture is still
        claimed, its group still has the lifecycle identity it was claimed
        under and ``base_sha`` as its published base, and one of the group's
        snapshots published that commit, which is what the import fetches
        again. A base no snapshot published, such as the one a workspace
        usually started from, has nothing for the import to fetch; the caller
        then fails the capture as before.

        Locks follow :mod:`cloud_agent.storage`: the claimed capture, its
        group, then the group's pending snapshot. The published snapshot is
        only read: it is terminal, and the group's lock keeps the base still.
        """
        detail = (
            f"the workspace no longer holds published base {base_sha[:12]}; "
            "it is being imported again before this snapshot is captured"
        )
        async with self._pool.acquire() as conn, conn.transaction():
            claimed_sandbox_id = await conn.fetchval(
                """
                SELECT sandbox_id FROM agent_sandbox_snapshots
                WHERE id = $1 AND state = 'capturing' AND base_sha = $2
                FOR UPDATE
                """,
                snapshot_id,
                base_sha,
            )
            if claimed_sandbox_id is None:
                return False
            if str(claimed_sandbox_id) != sandbox_id:
                raise ValueError("snapshot claim belongs to a different sandbox")
            group = await conn.fetchval(
                """
                SELECT id FROM agent_sandboxes
                WHERE id = $1 AND generation = $2 AND container_id = $3
                  AND snapshot_state = 'snapshotting' AND published_base_sha = $4
                FOR UPDATE
                """,
                sandbox_id,
                sandbox_generation,
                container_id,
                base_sha,
            )
            if group is None:
                return False
            if not await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM agent_sandbox_snapshots
                    WHERE sandbox_id = $1 AND state = 'published'
                      AND published_commit_sha = $2
                )
                """,
                sandbox_id,
                base_sha,
            ):
                return False
            retried = (
                await _fold_into_pending_snapshot(
                    conn, snapshot_id=snapshot_id, sandbox_id=sandbox_id
                )
                or snapshot_id
            )
            await conn.execute(
                """
                UPDATE agent_sandbox_snapshots
                SET state = 'pending', last_error = $2, retry_at = NULL, updated_at = NOW()
                WHERE id = $1 AND state IN ('capturing', 'pending')
                """,
                retried,
                detail,
            )
            # Importing the published base starts the container if stopped.
            await conn.execute(
                f"""
                UPDATE agent_sandboxes
                SET snapshot_state = 'importing', idle_deadline = NULL,
                    {FENCE_START_SQL}, updated_at = NOW()
                WHERE id = $1 AND snapshot_state = 'snapshotting'
                """,
                sandbox_id,
            )
        logger.warning(
            "agent_snapshot_base_reimport",
            extra={
                "event": "agent_snapshot_base_reimport",
                "snapshot_id": retried,
                "sandbox_id": sandbox_id,
                "base_sha": base_sha,
            },
        )
        return True

    async def reap_stalled_sandbox_snapshot_captures(
        self, *, stall_seconds: float = 300.0
    ) -> list[int]:
        """Return abandoned CAPTURING claims to the retryable pending queue.

        A claim whose workspace settled another turn while it was out is
        folded into that turn's pending snapshot, as a deferred capture is.
        Each claim is reaped in a transaction of its own, so one the database
        refuses stays capturing for the next pass instead of holding back the
        claims of every other workspace. Returns the claims reaped.
        """
        reaped: list[int] = []
        async with self._pool.acquire() as conn:
            stalled = await conn.fetch(
                """
                SELECT id FROM agent_sandbox_snapshots
                WHERE state = 'capturing'
                  AND updated_at < NOW() - make_interval(secs => $1)
                ORDER BY id
                """,
                stall_seconds,
            )
            for candidate in stalled:
                snapshot_id = int(candidate["id"])
                try:
                    async with conn.transaction():
                        handed_back = await self._reap_stalled_capture(
                            conn, snapshot_id=snapshot_id, stall_seconds=stall_seconds
                        )
                except asyncpg.PostgresError:
                    logger.warning(
                        "agent_snapshot_capture_reap_failed",
                        exc_info=True,
                        extra={
                            "event": "agent_snapshot_capture_reap_failed",
                            "snapshot_id": snapshot_id,
                        },
                    )
                    continue
                if handed_back:
                    reaped.append(snapshot_id)
        return reaped

    @staticmethod
    async def _reap_stalled_capture(conn: Any, *, snapshot_id: int, stall_seconds: float) -> bool:
        """Hand one stalled claim back inside the caller's transaction.

        ``False`` when it is no longer a stalled claim, or a writer holds it:
        that writer is settling it now. The claim is locked before its group
        and the group before its pending snapshot, as a deferred capture does.
        """
        claim = await conn.fetchrow(
            """
            SELECT sandbox_id FROM agent_sandbox_snapshots
            WHERE id = $1 AND state = 'capturing'
              AND updated_at < NOW() - make_interval(secs => $2)
            FOR UPDATE SKIP LOCKED
            """,
            snapshot_id,
            stall_seconds,
        )
        if claim is None:
            return False
        sandbox_id = str(claim["sandbox_id"])
        await conn.execute("SELECT id FROM agent_sandboxes WHERE id = $1 FOR UPDATE", sandbox_id)
        retried = (
            await _fold_into_pending_snapshot(conn, snapshot_id=snapshot_id, sandbox_id=sandbox_id)
            or snapshot_id
        )
        await conn.execute(
            """
            UPDATE agent_sandbox_snapshots
            SET state = 'pending',
                last_error = 'snapshot coordinator stopped during capture',
                updated_at = NOW()
            WHERE id = $1 AND state IN ('capturing', 'pending')
            """,
            retried,
        )
        await conn.execute(
            """
            UPDATE agent_sandboxes
            SET snapshot_state = 'pending',
                idle_deadline = CASE
                    WHEN active_operation_count = 0
                     AND NOT EXISTS (
                        SELECT 1 FROM agent_thread_sandbox_memberships active
                        WHERE active.sandbox_id = agent_sandboxes.id
                          AND active.active_attempt_id IS NOT NULL
                     )
                    THEN clock_timestamp() + make_interval(secs => idle_timeout_seconds)
                    ELSE NULL END,
                updated_at = NOW()
            WHERE id = $1 AND snapshot_state = 'snapshotting'
            """,
            sandbox_id,
        )
        return True

    async def claim_for_publish(self) -> dict[str, Any] | None:
        """Take one immutable shared-workspace snapshot.

        Marks it ``publishing`` in the same transaction, so a second publisher
        cannot pick it up. The thread lock is shared with retained-fork
        preparation: a group snapshot may capture a succeeded boundary or a
        publisher may start mutating its lineage, but those operations cannot
        overlap. Returns the snapshot and its publication metadata, or ``None``.
        Deferred publications are eligible only after their ``retry_at``.

        Epochs publish in order: an earlier snapshot not yet published holds
        this one back. A failed one does not. It is never claimed again, and
        this later one was cut from the same published base, so it carries
        the failed one's changes and supersedes it.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            snapshot = await conn.fetchrow(
                """
                SELECT s.id AS snapshot_id, s.sandbox_id, s.workspace_epoch,
                       s.publish_attempt_count,
                       s.anchor_job_id AS id, s.base_sha, s.patch_content AS patch,
                       (
                           SELECT COALESCE(
                               jsonb_agg(included.job_id ORDER BY included.created_at, included.job_id),
                               '[]'::jsonb
                           )
                           FROM agent_snapshot_jobs included
                           WHERE included.snapshot_id = s.id
                       ) AS included_job_ids,
                       COALESCE((
                           SELECT jsonb_agg(summary.content ORDER BY included.created_at, included.job_id)
                           FROM agent_snapshot_jobs included
                           JOIN agent_jobs included_job ON included_job.id = included.job_id
                           JOIN LATERAL (
                               SELECT LEFT(message.content, 4000) AS content
                               FROM agent_thread_messages message
                               JOIN agent_job_events event ON event.id = message.source_event_id
                               WHERE message.job_id = included.job_id
                                 AND message.thread_id = included_job.thread_id
                                 AND message.role = 'assistant'
                                 AND event.attempt_id = included_job.current_attempt_id
                               ORDER BY message.id DESC
                               LIMIT 1
                           ) summary ON TRUE
                           WHERE included.snapshot_id = s.id
                       ), '[]'::jsonb) AS session_summaries,
                       COALESCE((
                           SELECT jsonb_agg(
                               jsonb_build_object(
                                   'thread_id', message.thread_id,
                                   'job_id', message.job_id,
                                   'role', message.role,
                                   'content', message.content
                               ) ORDER BY history.thread_id, history.turn_no,
                                   CASE WHEN message.role = 'user' AND NOT message.mid_turn
                                        THEN 0 ELSE 1 END,
                                   message.id
                           )
                           FROM (
                               SELECT included_job.thread_id, MAX(included_job.turn_no) AS turn_no
                               FROM agent_sandbox_snapshots captured
                               JOIN agent_snapshot_jobs included ON included.snapshot_id = captured.id
                               JOIN agent_jobs included_job ON included_job.id = included.job_id
                               WHERE captured.sandbox_id = s.sandbox_id
                                 AND captured.workspace_epoch <= s.workspace_epoch
                               GROUP BY included_job.thread_id
                           ) boundary
                           -- A Fork's copies of its source's turns are read
                           -- once, in the chat they were copied from.
                           JOIN agent_jobs history ON history.thread_id = boundary.thread_id
                               AND history.turn_no <= boundary.turn_no
                               AND history.fork_source_job_id IS NULL
                           JOIN agent_thread_messages message ON message.job_id = history.id
                               AND message.thread_id = history.thread_id
                           LEFT JOIN agent_job_events event ON event.id = message.source_event_id
                           WHERE message.role = 'user'
                              OR (message.role = 'assistant' AND (
                                  message.source_event_id IS NULL
                                  OR event.attempt_id = history.current_attempt_id
                              ))
                       ), '[]'::jsonb) AS session_messages,
                       g.origin_thread_id,
                       g.generation, g.container_id, g.runtime_id,
                       g.runtime_version, g.sandbox_image_digest,
                       g.memory_limit, g.cpu_limit, g.disk_limit_mib, g.stop_timeout_seconds,
                       g.resident_slot_capacity,
                       COALESCE((
                           SELECT jsonb_agg(
                               jsonb_build_object(
                                   'thread_id', member.thread_id,
                                   'native_session_id', member.native_session_id,
                                   'context_status', member.context_status,
                                   'activation_generation', member.activation_generation,
                                   'process_generation', member.process_generation,
                                   'residency_generation', member.residency_generation,
                                   'resident_state', member.resident_state,
                                   'resident_slot_id', member.resident_slot_id,
                                   'last_settled_job_id', member.last_settled_job_id
                               ) ORDER BY member.created_at, member.thread_id
                           )
                           FROM agent_thread_sandbox_memberships member
                           WHERE member.sandbox_id = g.id
                             AND member.resident_slot_id IS NOT NULL
                       ), '[]'::jsonb) AS members,
                       j.thread_id, j.parent_job_id, j.user_id, t.repo,
                       j.task_prompt, j.metadata, j.model,
                       prior.published_pr_url AS parent_pr_url,
                       -- The release before 0081 recorded its pushes in
                       -- published_commit_sha, and keeps doing so until it is
                       -- replaced, after the migration ran: read both.
                       COALESCE((
                           SELECT jsonb_agg(DISTINCT own.sha)
                           FROM agent_sandbox_snapshots pushed
                           CROSS JOIN LATERAL (
                               VALUES (pushed.pushed_commit_sha), (pushed.published_commit_sha)
                           ) AS own (sha)
                           WHERE pushed.sandbox_id = s.sandbox_id
                             AND pushed.base_sha = s.base_sha
                             AND pushed.state <> 'published'
                             AND own.sha IS NOT NULL
                       ), '[]'::jsonb) AS own_pushes
                FROM agent_sandbox_snapshots s
                JOIN agent_sandboxes g ON g.id = s.sandbox_id
                JOIN agent_jobs j ON j.id = s.anchor_job_id
                JOIN agent_threads t ON t.id = j.thread_id
                LEFT JOIN LATERAL (
                    SELECT previous.published_pr_url
                    FROM agent_sandbox_snapshots previous
                    WHERE previous.sandbox_id = s.sandbox_id
                      AND previous.published_pr_url IS NOT NULL
                    ORDER BY previous.workspace_epoch DESC
                    LIMIT 1
                ) prior ON TRUE
                WHERE s.state = 'captured'
                  AND (s.retry_at IS NULL OR s.retry_at <= NOW())
                  AND s.patch_content IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_sandbox_snapshots earlier
                      WHERE earlier.sandbox_id = s.sandbox_id
                        AND earlier.workspace_epoch < s.workspace_epoch
                        AND earlier.state NOT IN ('published', 'failed')
                  )
                ORDER BY s.created_at, s.id
                LIMIT 1
                FOR UPDATE OF s SKIP LOCKED
                """
            )
            if snapshot is not None:
                claimed = await conn.fetchval(
                    """
                    UPDATE agent_sandbox_snapshots
                    SET state = 'publishing', last_error = NULL, retry_at = NULL,
                        updated_at = NOW()
                    WHERE id = $1 AND state = 'captured'
                    RETURNING id
                    """,
                    snapshot["snapshot_id"],
                )
                if claimed is None:
                    return None
                attempt_id = await conn.fetchval(
                    "SELECT current_attempt_id FROM agent_jobs WHERE id = $1",
                    snapshot["id"],
                )
                if attempt_id is not None:
                    await insert_event(
                        conn,
                        job_id=snapshot["id"],
                        attempt_id=attempt_id,
                        event_type="lifecycle",
                        payload={
                            "phase": "shared_snapshot_publishing",
                            "snapshot_id": snapshot["snapshot_id"],
                            "workspace_epoch": snapshot["workspace_epoch"],
                        },
                    )
                return {
                    "publication_kind": "shared_snapshot",
                    "snapshot_id": int(snapshot["snapshot_id"]),
                    "publish_attempt_count": int(snapshot["publish_attempt_count"]),
                    "sandbox_id": snapshot["sandbox_id"],
                    "workspace_epoch": int(snapshot["workspace_epoch"]),
                    "included_job_ids": load_json(snapshot["included_job_ids"]) or [],
                    "session_summaries": load_json(snapshot["session_summaries"]) or [],
                    "session_messages": load_json(snapshot["session_messages"]) or [],
                    "job_id": snapshot["id"],
                    "anchor_job_id": snapshot["id"],
                    "thread_id": snapshot["thread_id"],
                    "branch_id": snapshot["origin_thread_id"],
                    "origin_thread_id": snapshot["origin_thread_id"],
                    "generation": int(snapshot["generation"]),
                    "container_id": snapshot["container_id"],
                    "runtime_id": snapshot["runtime_id"],
                    "runtime_version": snapshot["runtime_version"],
                    "sandbox_image_digest": snapshot["sandbox_image_digest"],
                    "memory_limit": snapshot["memory_limit"],
                    "cpu_limit": snapshot["cpu_limit"],
                    "disk_limit_mib": snapshot["disk_limit_mib"],
                    "stop_timeout_seconds": int(snapshot["stop_timeout_seconds"]),
                    "resident_slot_capacity": int(snapshot["resident_slot_capacity"]),
                    "members": load_json(snapshot["members"]) or [],
                    "parent_job_id": snapshot["parent_job_id"],
                    "parent_pr_url": snapshot["parent_pr_url"],
                    "user_id": snapshot["user_id"],
                    "repo": snapshot["repo"],
                    "base_sha": snapshot["base_sha"],
                    "task_prompt": snapshot["task_prompt"],
                    "model": snapshot["model"],
                    "metadata": load_json(snapshot["metadata"]),
                    "patch": snapshot["patch"],
                    "own_pushes": load_json(snapshot["own_pushes"]) or [],
                }
            return None

    async def renew_snapshot_publish_claim(
        self, *, snapshot_id: int, expected_publish_attempt_count: int
    ) -> bool:
        """Heartbeat this exact publishing claim using database wall time."""
        async with self._pool.acquire() as conn:
            renewed = await conn.fetchval(
                """
                UPDATE agent_sandbox_snapshots
                SET updated_at = clock_timestamp()
                WHERE id = $1 AND state = 'publishing'
                  AND publish_attempt_count = $2
                RETURNING id
                """,
                snapshot_id,
                expected_publish_attempt_count,
            )
        return renewed is not None

    async def defer_snapshot_publish(
        self,
        *,
        snapshot_id: int,
        expected_publish_attempt_count: int,
        detail: str,
        delay_s: float | None = None,
    ) -> bool:
        """Retry a transient publication under this exact fenced claim.

        ``delay_s`` is how long GitHub asked to wait, for a rate limit.
        """
        return await self._defer_snapshot_publish(
            snapshot_id=snapshot_id,
            expected_publish_attempt_count=expected_publish_attempt_count,
            detail=detail,
            stall_seconds=None,
            delay_s=delay_s,
        )

    async def _defer_snapshot_publish(
        self,
        *,
        snapshot_id: int,
        expected_publish_attempt_count: int,
        detail: str,
        stall_seconds: float | None,
        delay_s: float | None = None,
    ) -> bool:
        """Retry a transient publication, or stop after the durable budget.

        Deterministic branch and PR reconciliation makes replay safe even when
        the process died after a remote effect. The counter survives restarts;
        exhaustion fails the artifact, keeping why, and marks its group dirty.
        A later epoch never publishes while this one may still be retried;
        once it has failed, the group's next capture supersedes it.
        The expected counter rejects a publisher whose stalled claim was
        reclaimed. Returns ``False`` unless this exact claim is still
        ``publishing``.
        """
        retry_detail = detail[:2000]
        exhausted_detail = (
            f"{detail[:1800]} Publication retry limit ({PUBLISH_MAX_ATTEMPTS}) exhausted; "
            "the workspace's next snapshot supersedes this one."
        )[:2000]
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE agent_sandbox_snapshots
                SET state = CASE
                        WHEN publish_attempt_count >= $5 THEN 'failed'
                        ELSE 'captured'
                    END,
                    publish_attempt_count = CASE
                        WHEN publish_attempt_count >= $5 THEN publish_attempt_count
                        ELSE publish_attempt_count + 1
                    END,
                    last_error = CASE
                        WHEN publish_attempt_count >= $5 THEN $6
                        ELSE $3
                    END,
                    retry_at = CASE
                        WHEN publish_attempt_count >= $5 THEN NULL
                        ELSE clock_timestamp() + make_interval(secs => $4)
                    END,
                    updated_at = NOW()
                WHERE id = $1 AND state = 'publishing'
                  AND publish_attempt_count = $2
                  AND (
                      $7::double precision IS NULL
                      OR updated_at < clock_timestamp() - make_interval(secs => $7)
                  )
                RETURNING id, sandbox_id, anchor_job_id, state, last_error
                """,
                snapshot_id,
                expected_publish_attempt_count,
                retry_detail,
                _retry_wait_s(delay_s, floor=PUBLISH_RETRY_S),
                PUBLISH_MAX_ATTEMPTS,
                exhausted_detail,
                stall_seconds,
            )
            if row is None:
                return False
            if row["state"] != "failed":
                return True
            await conn.execute(
                """
                UPDATE agent_sandboxes
                SET snapshot_state = 'dirty', last_error = $2,
                    idle_deadline = CASE
                        WHEN active_operation_count = 0
                         AND NOT EXISTS (
                            SELECT 1 FROM agent_thread_sandbox_memberships active
                            WHERE active.sandbox_id = agent_sandboxes.id
                              AND active.active_attempt_id IS NOT NULL
                         )
                        THEN clock_timestamp() + make_interval(secs => idle_timeout_seconds)
                        ELSE NULL END,
                    updated_at = NOW()
                WHERE id = $1
                """,
                row["sandbox_id"],
                row["last_error"],
            )
            attempt_id = await conn.fetchval(
                "SELECT current_attempt_id FROM agent_jobs WHERE id = $1",
                row["anchor_job_id"],
            )
            if attempt_id is not None:
                await insert_event(
                    conn,
                    job_id=row["anchor_job_id"],
                    attempt_id=attempt_id,
                    event_type="error",
                    payload={
                        "phase": "shared_snapshot_publish_retry_exhausted",
                        "snapshot_id": snapshot_id,
                        "detail": row["last_error"],
                    },
                )
        return True

    async def record_snapshot_push(
        self, *, snapshot_id: int, expected_publish_attempt_count: int, commit_sha: str
    ) -> bool:
        """Remember the commit this claim is about to push, before the push.

        The push is a remote effect no later step can undo, and it can land
        unseen: a timeout after the remote accepted it, a crash. If the
        publication then fails, the snapshot fails or waits with its commit on
        the branch while the group's published base stays where it was, and
        the next capture is cut from that base again. Recorded here, the commit
        is handed to that next publication as the platform's own (``own_pushes``
        on its claim), which may build on it instead of reading it as somebody
        else's push. A commit recorded and never pushed is harmless: nobody
        else holds its object. ``pushed_commit_sha`` holds it, apart from
        ``published_commit_sha``, which only publication sets.
        """
        async with self._pool.acquire() as conn:
            recorded = await conn.fetchval(
                """
                UPDATE agent_sandbox_snapshots
                SET pushed_commit_sha = $3, updated_at = NOW()
                WHERE id = $1 AND state = 'publishing'
                  AND publish_attempt_count = $2
                RETURNING id
                """,
                snapshot_id,
                expected_publish_attempt_count,
                commit_sha,
            )
        return recorded is not None

    async def record_snapshot_publish(
        self,
        *,
        snapshot_id: int,
        expected_publish_attempt_count: int,
        pr_url: str,
        commit_sha: str,
    ) -> bool:
        """Advance one group lineage and wait for object-only base import.

        Included Agent turns stay succeeded throughout.  Publication is a
        group artifact and must never rewrite a successful model turn as a
        failure merely because GitHub or the sandbox manager was unavailable later.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE agent_sandbox_snapshots
                SET state = 'published', published_pr_url = $2,
                    published_commit_sha = $3, published_at = NOW(),
                    last_error = NULL, retry_at = NULL, updated_at = NOW()
                WHERE id = $1 AND state = 'publishing'
                  AND publish_attempt_count = $4
                RETURNING sandbox_id, anchor_job_id
                """,
                snapshot_id,
                pr_url,
                commit_sha,
                expected_publish_attempt_count,
            )
            if row is None:
                return False
            # The included turns before their group, in id order: a follow-up,
            # a settlement and a Fork hold a turn while they wait for the group.
            await conn.fetch(
                """
                SELECT id FROM agent_jobs
                WHERE id IN (
                    SELECT job_id FROM agent_snapshot_jobs WHERE snapshot_id = $1
                ) AND state = 'succeeded'
                ORDER BY id
                FOR NO KEY UPDATE
                """,
                snapshot_id,
            )
            # Importing the published base starts the container if stopped, but
            # not on a revoked host: there the group waits to move, and its
            # next host imports the base.
            group = await conn.fetchrow(
                "SELECT id, state, host FROM agent_sandboxes WHERE id = $1 FOR UPDATE",
                row["sandbox_id"],
            )
            fence = (
                "" if await refuse_start_on_revoked_host(conn, group) else f"{FENCE_START_SQL}, "
            )
            await conn.execute(
                f"""
                UPDATE agent_sandboxes
                SET published_base_sha = $2, snapshot_state = 'importing',
                    idle_deadline = NULL, {fence}updated_at = NOW()
                WHERE id = $1
                """,
                row["sandbox_id"],
                commit_sha,
            )
            await conn.execute(
                """
                UPDATE agent_sandbox_snapshots
                SET base_sha = $2, updated_at = NOW()
                WHERE sandbox_id = $1 AND state = 'pending'
                """,
                row["sandbox_id"],
                commit_sha,
            )
            await conn.execute(
                """
                UPDATE agent_jobs
                SET published_pr_url = $2,
                    published_commit_sha = $3,
                    updated_at = NOW()
                WHERE id IN (
                    SELECT job_id FROM agent_snapshot_jobs WHERE snapshot_id = $1
                ) AND state = 'succeeded'
                """,
                snapshot_id,
                pr_url,
                commit_sha,
            )
            attempt_id = await conn.fetchval(
                "SELECT current_attempt_id FROM agent_jobs WHERE id = $1",
                row["anchor_job_id"],
            )
            if attempt_id is not None:
                await insert_event(
                    conn,
                    job_id=row["anchor_job_id"],
                    attempt_id=attempt_id,
                    event_type="lifecycle",
                    payload={
                        "phase": "shared_snapshot_published",
                        "snapshot_id": snapshot_id,
                        "pr_url": pr_url,
                    },
                )
        return True

    async def fail_snapshot_publish(
        self, *, snapshot_id: int, expected_publish_attempt_count: int, detail: str
    ) -> None:
        """Retain a rejected group artifact without failing included turns.

        The snapshot stays ``failed`` with the reason, and its group ``dirty``
        until its owner publishes again and a later snapshot is captured. That
        capture, cut from the same published base, supersedes this one and is
        refused in turn if it still carries what was rejected.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE agent_sandbox_snapshots
                SET state = 'failed', last_error = $2, retry_at = NULL,
                    updated_at = NOW()
                WHERE id = $1 AND state = 'publishing'
                  AND publish_attempt_count = $3
                RETURNING sandbox_id, anchor_job_id
                """,
                snapshot_id,
                detail[:2000],
                expected_publish_attempt_count,
            )
            if row is None:
                return
            await conn.execute(
                """
                UPDATE agent_sandboxes
                SET snapshot_state = 'dirty', last_error = $2,
                    idle_deadline = CASE
                        WHEN active_operation_count = 0
                         AND NOT EXISTS (
                            SELECT 1 FROM agent_thread_sandbox_memberships active
                            WHERE active.sandbox_id = agent_sandboxes.id
                              AND active.active_attempt_id IS NOT NULL
                         )
                        THEN clock_timestamp() + make_interval(secs => idle_timeout_seconds)
                        ELSE NULL END,
                    updated_at = NOW()
                WHERE id = $1
                """,
                row["sandbox_id"],
                detail[:2000],
            )
            attempt_id = await conn.fetchval(
                "SELECT current_attempt_id FROM agent_jobs WHERE id = $1",
                row["anchor_job_id"],
            )
            if attempt_id is not None:
                await insert_event(
                    conn,
                    job_id=row["anchor_job_id"],
                    attempt_id=attempt_id,
                    event_type="error",
                    payload={
                        "phase": "shared_snapshot_publish_rejected",
                        "snapshot_id": snapshot_id,
                        "detail": detail[:2000],
                    },
                )

    async def pending_snapshot_base_import(self) -> dict[str, Any] | None:
        """Return one published group commit whose object import is pending.

        ``origin_base_sha`` is the base the group's first snapshot was cut
        from: the commit its workspace was checked out at, which the
        workspace's own history keeps. Every published commit descends from
        it. The published ones are kept by nothing, so Git prunes them
        together, and a bundle that needs only the previous one in the
        workspace cannot import their successor again.

        A group still on a revoked host waits, as a ``cold`` one does: an
        import starts the container, which nothing may do there any more.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT latest.id AS snapshot_id, latest.sandbox_id,
                       latest.anchor_job_id, latest.published_commit_sha, latest.base_sha,
                       COALESCE((
                           SELECT origin.base_sha FROM agent_sandbox_snapshots origin
                           WHERE origin.sandbox_id = g.id
                           ORDER BY origin.workspace_epoch, origin.id
                           LIMIT 1
                       ), latest.base_sha) AS origin_base_sha,
                       g.origin_thread_id, g.generation, g.container_id,
                       g.runtime_id, g.runtime_version, g.sandbox_image_digest,
                       g.memory_limit, g.cpu_limit, g.disk_limit_mib, g.stop_timeout_seconds,
                       g.resident_slot_capacity, t.repo, j.user_id,
                       COALESCE((
                           SELECT jsonb_agg(
                               jsonb_build_object(
                                   'thread_id', member.thread_id,
                                   'native_session_id', member.native_session_id,
                                   'context_status', member.context_status,
                                   'activation_generation', member.activation_generation,
                                   'process_generation', member.process_generation,
                                   'residency_generation', member.residency_generation,
                                   'resident_state', member.resident_state,
                                   'resident_slot_id', member.resident_slot_id,
                                   'last_settled_job_id', member.last_settled_job_id
                               ) ORDER BY member.created_at, member.thread_id
                           )
                           FROM agent_thread_sandbox_memberships member
                           WHERE member.sandbox_id = g.id
                             AND member.resident_slot_id IS NOT NULL
                       ), '[]'::jsonb) AS members
                FROM agent_sandboxes g
                JOIN LATERAL (
                    SELECT s.id, s.sandbox_id, s.anchor_job_id,
                           s.published_commit_sha, s.base_sha, s.retry_at
                    FROM agent_sandbox_snapshots s
                    WHERE s.sandbox_id = g.id AND s.state = 'published'
                      AND s.published_commit_sha = g.published_base_sha
                    ORDER BY s.workspace_epoch DESC
                    LIMIT 1
                ) latest ON TRUE
                JOIN agent_jobs j ON j.id = latest.anchor_job_id
                JOIN agent_threads t ON t.id = j.thread_id
                WHERE g.snapshot_state = 'importing'
                  -- A group only its checkpoint holds has no container to
                  -- import into; it waits until a restore recreates one.
                  AND g.state NOT IN ('cold', 'restoring')
                  AND NOT {REVOKED_HOST_SQL.format(host="g.host")}
                  AND g.stop_requested_at IS NULL
                  AND g.deletion_state NOT IN ('pending', 'archived', 'deleted')
                  AND (latest.retry_at IS NULL OR latest.retry_at <= NOW())
                ORDER BY g.updated_at
                LIMIT 1
                """
            )
        if row is None:
            return None
        result = dict(row)
        result["members"] = load_json(result.get("members")) or []
        return result

    async def fence_base_import_start(self, *, sandbox_id: str) -> tuple[str, int | None] | None:
        """Fence a base import about to be sent; return state and the new fence.

        Recording the commit fenced the group already, but that fence was
        read by no request, and a retry may leave much later. The request
        names a fence of its own: a later one would label the import's run
        with a start still in flight, so a checkpoint could cover the group
        before that start changes it.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            return await fence_sandbox_start(conn, sandbox_id)

    async def complete_snapshot_base_import(
        self,
        *,
        snapshot_id: int,
        commit_sha: str,
        generation: int,
        container_id: str | None,
    ) -> bool:
        """Acknowledge that the exact published commit resolves in Docker.

        Fenced to the generation and container the import was sent to, which
        the import request named and its host checked. A restore moves a group
        to a new generation and container while an import sent to the old one
        may still be on its way back. Without the fence that late answer
        marked the restored container imported, though its base never was.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            sandbox_id = await conn.fetchval(
                """
                SELECT sandbox_id FROM agent_sandbox_snapshots
                WHERE id = $1 AND state = 'published'
                  AND published_commit_sha = $2
                FOR UPDATE
                """,
                snapshot_id,
                commit_sha,
            )
            if sandbox_id is None:
                return False
            updated = await conn.fetchval(
                """
                UPDATE agent_sandboxes g
                SET state = 'running',
                    snapshot_state = CASE
                        WHEN EXISTS (
                            SELECT 1 FROM agent_sandbox_snapshots pending
                            WHERE pending.sandbox_id = g.id AND pending.state = 'pending'
                        ) THEN 'pending' ELSE 'quiescent' END,
                    last_error = NULL,
                    idle_deadline = CASE
                        WHEN g.active_operation_count = 0
                         AND NOT EXISTS (
                            SELECT 1 FROM agent_thread_sandbox_memberships active
                            WHERE active.sandbox_id = g.id
                              AND active.active_attempt_id IS NOT NULL
                         )
                        THEN clock_timestamp() + make_interval(secs => g.idle_timeout_seconds)
                        ELSE NULL END,
                    updated_at = NOW()
                WHERE g.id = $1 AND g.snapshot_state = 'importing'
                  AND g.published_base_sha = $2
                  -- An import answered by a host that no longer holds the
                  -- group did not land in it; the restored container imports
                  -- again. Marking the group running here would strand it,
                  -- and once the restore is confirmed, mark the new container
                  -- as holding a base it never received.
                  AND g.state NOT IN ('cold', 'restoring')
                  AND g.generation = $3
                  AND g.container_id IS NOT DISTINCT FROM $4
                RETURNING g.id
                """,
                sandbox_id,
                commit_sha,
                generation,
                container_id,
            )
            if updated is not None:
                await conn.execute(
                    "UPDATE agent_sandbox_snapshots SET retry_at = NULL "
                    "WHERE id = $1 AND retry_at IS NOT NULL",
                    snapshot_id,
                )
        return updated is not None

    async def record_snapshot_base_import_failure(
        self, *, snapshot_id: int, detail: str, delay_s: float | None = None
    ) -> None:
        """Keep an idempotent import retry visible without losing the commit.

        The import is not retried before the snapshot's ``retry_at``: the usual
        interval, or longer when GitHub asked to wait out a rate limit
        (``delay_s``).

        A publication waiting on the import does not wait for good. Once the
        import has kept failing for ``BASE_IMPORT_WAIT_LIMIT_S`` since the
        group's pending snapshot began waiting, that snapshot fails with the
        import's reason and the group goes ``dirty``, which also ends the
        retries. The owner's next request captures again, and a capture that
        still finds the base missing imports it again.

        Locks follow :mod:`cloud_agent.storage`: the published snapshot, then
        its group, then the group's pending snapshot.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            published = await conn.fetchrow(
                """
                UPDATE agent_sandbox_snapshots
                SET last_error = $2, updated_at = NOW(),
                    retry_at = clock_timestamp() + make_interval(secs => $3)
                WHERE id = $1 AND state = 'published'
                RETURNING sandbox_id, published_commit_sha
                """,
                snapshot_id,
                detail[:2000],
                _retry_wait_s(delay_s, floor=BASE_IMPORT_RETRY_S),
            )
            if published is None:
                return
            group = await conn.fetchval(
                """
                SELECT id FROM agent_sandboxes
                WHERE id = $1 AND snapshot_state = 'importing'
                  AND published_base_sha = $2
                FOR UPDATE
                """,
                published["sandbox_id"],
                published["published_commit_sha"],
            )
            if group is None:
                return
            waiting = await conn.fetchval(
                """
                SELECT id FROM agent_sandbox_snapshots
                WHERE sandbox_id = $1 AND state = 'pending'
                  AND updated_at < clock_timestamp() - make_interval(secs => $2)
                FOR UPDATE
                """,
                group,
                BASE_IMPORT_WAIT_LIMIT_S,
            )
            if waiting is None:
                return
            reason = (
                f"the workspace's published base {str(published['published_commit_sha'])[:12]} "
                f"could not be imported: {detail}"
            )[:2000]
            await conn.execute(
                """
                UPDATE agent_sandbox_snapshots
                SET state = 'failed', last_error = $2, retry_at = NULL, updated_at = NOW()
                WHERE id = $1 AND state = 'pending'
                """,
                waiting,
                reason,
            )
            await conn.execute(
                """
                UPDATE agent_sandboxes
                SET snapshot_state = 'dirty', last_error = $2,
                    idle_deadline = CASE
                        WHEN active_operation_count = 0
                         AND NOT EXISTS (
                            SELECT 1 FROM agent_thread_sandbox_memberships active
                            WHERE active.sandbox_id = agent_sandboxes.id
                              AND active.active_attempt_id IS NOT NULL
                         )
                        THEN clock_timestamp() + make_interval(secs => idle_timeout_seconds)
                        ELSE NULL END,
                    updated_at = NOW()
                WHERE id = $1
                """,
                group,
                reason,
            )
        logger.warning(
            "agent_snapshot_base_import_abandoned",
            extra={
                "event": "agent_snapshot_base_import_abandoned",
                "snapshot_id": waiting,
                "sandbox_id": group,
            },
        )

    async def next_snapshot_retry_due(self) -> datetime | None:
        """When the earliest capture handed back for a retry may be claimed again.

        ``None`` when none waits. Everything else that makes a capture due is
        announced by a notification.
        """
        async with self._pool.acquire() as conn:
            due: datetime | None = await conn.fetchval(
                "SELECT MIN(retry_at) FROM agent_sandbox_snapshots "
                "WHERE state = 'pending' AND retry_at > NOW()"
            )
        return due

    async def next_snapshot_publish_retry_due(self) -> datetime | None:
        """When the earliest deferred publication may be claimed again.

        ``None`` when no captured snapshot waits for a future retry. Capture
        completion announces other publication work through a notification.
        """
        async with self._pool.acquire() as conn:
            due: datetime | None = await conn.fetchval(
                "SELECT MIN(retry_at) FROM agent_sandbox_snapshots "
                "WHERE state = 'captured' AND retry_at > NOW()"
            )
        return due

    async def next_base_import_retry_due(self) -> datetime | None:
        """When the earliest failed base import may be retried.

        ``None`` when none waits; a workspace that starts importing is announced
        by a notification.
        """
        async with self._pool.acquire() as conn:
            due: datetime | None = await conn.fetchval(
                """
                SELECT MIN(s.retry_at)
                FROM agent_sandbox_snapshots s
                JOIN agent_sandboxes g ON g.id = s.sandbox_id
                WHERE s.state = 'published' AND s.retry_at > NOW()
                  AND g.snapshot_state = 'importing'
                  AND s.published_commit_sha = g.published_base_sha
                """
            )
        return due

    async def reap_stalled_snapshot_publishes(self, *, stall_seconds: float = 900.0) -> list[int]:
        """Reclaim a crashed publisher through deterministic remote reconciliation."""
        detail = "the publisher stopped before finishing; deterministic publication will retry"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, publish_attempt_count FROM agent_sandbox_snapshots
                WHERE state = 'publishing'
                  AND published_pr_url IS NULL
                  AND updated_at < clock_timestamp() - make_interval(secs => $1)
                ORDER BY id
                """,
                stall_seconds,
            )
        reclaimed: list[int] = []
        for row in rows:
            snapshot_id = int(row["id"])
            if await self._defer_snapshot_publish(
                snapshot_id=snapshot_id,
                expected_publish_attempt_count=int(row["publish_attempt_count"]),
                detail=detail,
                stall_seconds=stall_seconds,
            ):
                reclaimed.append(snapshot_id)
        return reclaimed
