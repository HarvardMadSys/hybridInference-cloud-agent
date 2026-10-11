"""Postgres implementation of the group-snapshot port.

Lifted verbatim from the single storage class this package replaced: the
statements, their bound parameters and their transaction ownership are what
they were, and only the class holding them changed.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING, Any

from cloud_agent.storage._sql.events import insert_event
from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    from datetime import datetime

    import asyncpg

#: How long a capture handed back by a busy or unreachable host waits before it
#: is claimed again. Until then the coordinator captures the workspaces behind
#: it, and sleeps when there are none.
CAPTURE_RETRY_S = 5.0
#: How long a published base whose import failed waits before it is retried.
BASE_IMPORT_RETRY_S = 15.0


class PostgresSnapshotRepository:
    """Capture, publish and base-import, each claimed with SKIP LOCKED."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def claim_due_sandbox_snapshot(self) -> dict[str, Any] | None:
        """Fence one quiescent shared workspace for bounded patch capture.

        This claim never waits for publication and never changes a job state.
        The host revalidates quiescence against its live operation ledger; a
        turn that entered between this transaction and the broker call simply
        makes the capture retry later.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
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
                  AND g.snapshot_state = 'pending'
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
                        AND prior.state <> 'published'
                  )
                ORDER BY s.created_at, s.id
                LIMIT 1
                FOR UPDATE OF s, g SKIP LOCKED
                """
            )
            if row is None:
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
            await conn.execute(
                """
                UPDATE agent_sandboxes
                SET snapshot_state = 'snapshotting', idle_deadline = NULL,
                    updated_at = NOW()
                WHERE id = $1 AND snapshot_state = 'pending'
                """,
                row["sandbox_id"],
            )
        result = dict(row)
        result["members"] = load_json(result.get("members")) or []
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
        """Store one immutable broker receipt and release the capture barrier.

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
                        THEN NOW() + make_interval(secs => g.idle_timeout_seconds)
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
        ``retry_at``, so a busy host is asked on that clock, not in a loop.
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
            await conn.execute(
                """
                UPDATE agent_sandbox_snapshots
                SET state = $2, last_error = $3, updated_at = NOW(),
                    retry_at = CASE WHEN $2 = 'pending'
                                    THEN NOW() + make_interval(secs => $4) END
                WHERE id = $1 AND state = 'capturing'
                """,
                snapshot_id,
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
                        THEN NOW() + make_interval(secs => idle_timeout_seconds)
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

    async def reap_stalled_sandbox_snapshot_captures(
        self, *, stall_seconds: float = 300.0
    ) -> list[int]:
        """Return abandoned CAPTURING claims to the retryable pending queue."""
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                """
                UPDATE agent_sandbox_snapshots
                SET state = 'pending',
                    last_error = 'snapshot coordinator stopped during capture',
                    updated_at = NOW()
                WHERE state = 'capturing'
                  AND updated_at < NOW() - make_interval(secs => $1)
                RETURNING id, sandbox_id
                """,
                stall_seconds,
            )
            if rows:
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
                            THEN NOW() + make_interval(secs => idle_timeout_seconds)
                            ELSE NULL END,
                        updated_at = NOW()
                    WHERE id = ANY($1::text[]) AND snapshot_state = 'snapshotting'
                    """,
                    [row["sandbox_id"] for row in rows],
                )
        return [int(row["id"]) for row in rows]

    async def claim_for_publish(self) -> dict[str, Any] | None:
        """Take one immutable shared-workspace snapshot.

        Marks it ``publishing`` in the same transaction, so a second publisher
        cannot pick it up. The thread lock is shared with retained-fork
        preparation: a group snapshot may capture a succeeded boundary or a
        publisher may start mutating its lineage, but those operations cannot
        overlap. Returns the snapshot and its publication metadata, or ``None``.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            snapshot = await conn.fetchrow(
                """
                SELECT s.id AS snapshot_id, s.sandbox_id, s.workspace_epoch,
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
                           JOIN agent_jobs history ON history.thread_id = boundary.thread_id
                               AND history.turn_no <= boundary.turn_no
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
                       prior.published_pr_url AS parent_pr_url
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
                  AND s.patch_content IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_sandbox_snapshots earlier
                      WHERE earlier.sandbox_id = s.sandbox_id
                        AND earlier.workspace_epoch < s.workspace_epoch
                        AND earlier.state <> 'published'
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
                    SET state = 'publishing', last_error = NULL, updated_at = NOW()
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
                }
            return None

    async def record_snapshot_publish(
        self,
        *,
        snapshot_id: int,
        pr_url: str,
        commit_sha: str,
    ) -> bool:
        """Advance one group lineage and wait for object-only base import.

        Included Agent turns stay succeeded throughout.  Publication is a
        group artifact and must never rewrite a successful model turn as a
        failure merely because GitHub or the broker was unavailable later.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE agent_sandbox_snapshots
                SET state = 'published', published_pr_url = $2,
                    published_commit_sha = $3, published_at = NOW(),
                    last_error = NULL, updated_at = NOW()
                WHERE id = $1 AND state = 'publishing'
                RETURNING sandbox_id, anchor_job_id
                """,
                snapshot_id,
                pr_url,
                commit_sha,
            )
            if row is None:
                return False
            await conn.execute(
                """
                UPDATE agent_sandboxes
                SET published_base_sha = $2, snapshot_state = 'importing',
                    idle_deadline = NULL, updated_at = NOW()
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

    async def fail_snapshot_publish(self, *, snapshot_id: int, detail: str) -> None:
        """Retain a rejected group artifact without failing included turns."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE agent_sandbox_snapshots
                SET state = 'failed', last_error = $2, updated_at = NOW()
                WHERE id = $1 AND state = 'publishing'
                RETURNING sandbox_id, anchor_job_id
                """,
                snapshot_id,
                detail[:2000],
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
                        THEN NOW() + make_interval(secs => idle_timeout_seconds)
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
        """Return one published group commit whose object import is pending."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT latest.id AS snapshot_id, latest.sandbox_id,
                       latest.anchor_job_id, latest.published_commit_sha,
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
                           s.published_commit_sha, s.retry_at
                    FROM agent_sandbox_snapshots s
                    WHERE s.sandbox_id = g.id AND s.state = 'published'
                      AND s.published_commit_sha = g.published_base_sha
                    ORDER BY s.workspace_epoch DESC
                    LIMIT 1
                ) latest ON TRUE
                JOIN agent_jobs j ON j.id = latest.anchor_job_id
                JOIN agent_threads t ON t.id = j.thread_id
                WHERE g.snapshot_state = 'importing'
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

    async def complete_snapshot_base_import(self, *, snapshot_id: int, commit_sha: str) -> bool:
        """Acknowledge that the exact published commit resolves in Docker."""
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
                        THEN NOW() + make_interval(secs => g.idle_timeout_seconds)
                        ELSE NULL END,
                    updated_at = NOW()
                WHERE g.id = $1 AND g.snapshot_state = 'importing'
                  AND g.published_base_sha = $2
                RETURNING g.id
                """,
                sandbox_id,
                commit_sha,
            )
            if updated is not None:
                await conn.execute(
                    "UPDATE agent_sandbox_snapshots SET retry_at = NULL "
                    "WHERE id = $1 AND retry_at IS NOT NULL",
                    snapshot_id,
                )
        return updated is not None

    async def record_snapshot_base_import_failure(self, *, snapshot_id: int, detail: str) -> None:
        """Keep an idempotent import retry visible without losing the commit.

        The import is not retried before the snapshot's ``retry_at``.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE agent_sandbox_snapshots
                SET last_error = $2, updated_at = NOW(),
                    retry_at = NOW() + make_interval(secs => $3)
                WHERE id = $1 AND state = 'published'
                """,
                snapshot_id,
                detail[:2000],
                BASE_IMPORT_RETRY_S,
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
        """Fail ambiguous shared publishes without changing Agent turn state."""
        detail = "the publisher stopped before finishing; manual review required"
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                """
                UPDATE agent_sandbox_snapshots
                SET state = 'failed', last_error = $2, updated_at = NOW()
                WHERE state = 'publishing'
                  AND published_pr_url IS NULL
                  AND updated_at < NOW() - make_interval(secs => $1)
                RETURNING id, sandbox_id
                """,
                stall_seconds,
                detail,
            )
            if rows:
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
                            THEN NOW() + make_interval(secs => idle_timeout_seconds)
                            ELSE NULL END,
                        updated_at = NOW()
                    WHERE id = ANY($1::text[])
                    """,
                    [row["sandbox_id"] for row in rows],
                    detail,
                )
        return [int(row["id"]) for row in rows]
