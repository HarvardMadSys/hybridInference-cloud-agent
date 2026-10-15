"""Postgres reads for the Usage settings page: one owner's containers and their use."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cloud_agent.admin_observability import encode_cursor
from cloud_agent.owner_usage import (
    ACTIVE_STATES,
    CONTAINER_CURSOR_SCOPE,
    CONTAINER_STATES,
    MAX_CONTAINER_CHATS,
    MAX_SUMMED_CONTAINERS,
    READING_STALE_SECONDS,
    RUNNING_STATES,
    OwnerContainer,
    OwnerContainerChat,
    OwnerContainerCursor,
    OwnerContainerList,
    OwnerContainerReading,
    OwnerCurrentUse,
    OwnerResourceDay,
    OwnerResourceHistory,
    OwnerResourceTotals,
    container_state,
    cpu_limit_cores,
    memory_limit_bytes,
)
from cloud_agent.storage._sql.rows import load_json
from cloud_agent_shared.sandbox_resources import parse_sandbox_resource_sample

if TYPE_CHECKING:
    import asyncpg

#: Containers the owner can still reach: not removed by a completed deletion,
#: and holding at least one chat they have not deleted. A chat belongs to the
#: container its membership names; an origin chat without a membership row
#: belongs to the container it started.
_VISIBLE = """
    g.user_id = $1
    AND g.deletion_state <> 'deleted'
    AND (
        EXISTS (
            SELECT 1
            FROM agent_thread_sandbox_memberships m
            JOIN agent_threads t ON t.id = m.thread_id AND t.user_id = m.user_id
            WHERE m.sandbox_id = g.id AND t.deleted_at IS NULL
        )
        OR EXISTS (
            SELECT 1 FROM agent_threads t
            WHERE t.id = g.origin_thread_id AND t.user_id = g.user_id
              AND t.deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM agent_thread_sandbox_memberships m WHERE m.thread_id = t.id
              )
        )
    )
"""

#: Active containers first, then the most recently used.
_RANK = "CASE WHEN g.state = ANY($2::text[]) THEN 0 ELSE 1 END"

_COUNTS_SQL = f"""
    SELECT g.state, g.deletion_state, COUNT(*) AS n
    FROM agent_sandboxes g
    WHERE {_VISIBLE}
    GROUP BY g.state, g.deletion_state
"""

_RUNNING_SQL = f"""
    SELECT g.resource_snapshot, g.resource_sampled_at
    FROM agent_sandboxes g
    WHERE {_VISIBLE} AND g.state = 'running' AND g.deletion_state NOT IN ('pending', 'archived')
    LIMIT {MAX_SUMMED_CONTAINERS}
"""

_PAGE_SQL = f"""
    WITH page AS (
        SELECT g.id, g.state, g.deletion_state, g.origin_thread_id,
               g.memory_limit, g.cpu_limit, g.disk_limit_mib,
               g.resource_snapshot, g.resource_sampled_at,
               g.created_at, g.last_activity_at, g.idle_deadline,
               {_RANK} AS rank
        FROM agent_sandboxes g
        WHERE {_VISIBLE}
          AND ($3::integer IS NULL
               OR {_RANK} > $3
               OR ({_RANK} = $3 AND (g.last_activity_at < $4
                                     OR (g.last_activity_at = $4 AND g.id < $5))))
        ORDER BY rank, g.last_activity_at DESC, g.id DESC
        LIMIT $6
    )
    SELECT p.*, origin.repo, chats.chat_count, chats.items
    FROM page p
    LEFT JOIN agent_threads origin ON origin.id = p.origin_thread_id
    CROSS JOIN LATERAL (
        SELECT COUNT(*) AS chat_count,
               COALESCE(
                   jsonb_agg(
                       jsonb_build_object(
                           'thread_id', c.id, 'job_id', c.job_id,
                           'title', c.title, 'archived', c.archived
                       )
                       ORDER BY c.n
                   ) FILTER (WHERE c.n <= {MAX_CONTAINER_CHATS}),
                   '[]'::jsonb
               ) AS items
        FROM (
            SELECT t.id, LEFT(t.title, 160) AS title, t.archived_at IS NOT NULL AS archived,
                   (
                       SELECT j.id FROM agent_jobs j
                       WHERE j.thread_id = t.id AND j.user_id = t.user_id
                       ORDER BY j.turn_no DESC, j.id DESC
                       LIMIT 1
                   ) AS job_id,
                   ROW_NUMBER() OVER (
                       ORDER BY t.archived_at IS NOT NULL, t.updated_at DESC, t.id
                   ) AS n
            FROM agent_threads t
            WHERE t.user_id = $1 AND t.deleted_at IS NULL
              AND (
                  t.id IN (
                      SELECT m.thread_id FROM agent_thread_sandbox_memberships m
                      WHERE m.sandbox_id = p.id
                  )
                  OR (
                      t.id = p.origin_thread_id
                      AND NOT EXISTS (
                          SELECT 1 FROM agent_thread_sandbox_memberships m
                          WHERE m.thread_id = t.id
                      )
                  )
              )
        ) c
    ) chats
    ORDER BY p.rank, p.last_activity_at DESC, p.id DESC
"""

#: Seconds each day bucket spent in a running state, summed over every
#: container the owner ever had, deleted ones included: the use happened.
#: Each container's state at the window start is its last recorded transition
#: before it; every transition inside the window then starts a new span, and
#: the last span runs to the window's end. The lifecycle trigger records these
#: transitions; ``current_state_imported`` is the state the history began with.
#: A container's transitions follow one another in the order of their ids, as
#: its row lock serializes them. Each is stamped with its transaction's start,
#: so one that began before the transition it follows is stamped earlier; its
#: span starts where that transition's did, and never runs backwards.
_RUNNING_TIME_SQL = """
    WITH owned AS (
        SELECT id FROM agent_sandboxes WHERE user_id = $1
    ),
    transitions AS (
        SELECT o.id AS sandbox_id, $2::timestamptz AS at, initial.id AS seq,
               initial.to_state AS state
        FROM owned o
        CROSS JOIN LATERAL (
            SELECT e.id, e.to_state FROM agent_sandbox_events e
            WHERE e.sandbox_id = o.id AND e.created_at < $2 AND e.to_state IS NOT NULL
              AND e.event_type IN ('created', 'state_changed', 'current_state_imported')
            ORDER BY e.id DESC
            LIMIT 1
        ) initial
        UNION ALL
        SELECT e.sandbox_id, e.created_at, e.id, e.to_state
        FROM agent_sandbox_events e
        JOIN owned o ON o.id = e.sandbox_id
        WHERE e.created_at >= $2 AND e.created_at < $3 AND e.to_state IS NOT NULL
          AND e.event_type IN ('created', 'state_changed', 'current_state_imported')
    ),
    ordered AS (
        SELECT sandbox_id, seq, state,
               MAX(at) OVER (PARTITION BY sandbox_id ORDER BY seq) AS at
        FROM transitions
    ),
    spans AS (
        SELECT state, at AS started,
               COALESCE(LEAD(at) OVER (PARTITION BY sandbox_id ORDER BY seq), $3) AS ended
        FROM ordered
    )
    SELECT b.n,
           COALESCE(SUM(EXTRACT(EPOCH FROM LEAST(s.ended, b.until) - GREATEST(s.started, b.since))),
                    0)::double precision AS running_seconds
    FROM unnest($4::timestamptz[], $5::timestamptz[]) WITH ORDINALITY AS b(since, until, n)
    LEFT JOIN spans s
      ON s.state = ANY($6::text[]) AND s.started < b.until AND s.ended > b.since
    GROUP BY b.n
    ORDER BY b.n
"""

_MEASURED_SQL = """
    SELECT b.n,
           COALESCE(SUM(d.measured_seconds), 0) AS measured_seconds,
           COALESCE(SUM(d.cpu_core_seconds), 0) AS cpu_core_seconds,
           COALESCE(SUM(d.memory_byte_seconds), 0) AS memory_byte_seconds
    FROM unnest($2::date[]) WITH ORDINALITY AS b(day, n)
    LEFT JOIN agent_sandbox_usage_days d ON d.user_id = $1 AND d.day = b.day
    GROUP BY b.n
    ORDER BY b.n
"""


def _reading(
    snapshot: Any, sampled_at: datetime | None, now: datetime
) -> OwnerContainerReading | None:
    """The CPU and memory of a stored sample, revalidated, with its age."""
    sample = parse_sandbox_resource_sample(load_json(snapshot))
    if sample is None or sampled_at is None:
        return None
    age = (now - sampled_at).total_seconds()
    return OwnerContainerReading(
        cpu_cores=sample.cpu_cores,
        memory_bytes=sample.memory_bytes,
        sampled_at=sampled_at,
        stale=age > READING_STALE_SECONDS,
    )


class PostgresOwnerUsageRepository:
    """One owner's containers and their use; nothing is written."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Share the existing pool."""
        self._pool = pool

    async def containers(
        self, user_id: str, *, cursor: OwnerContainerCursor | None, limit: int
    ) -> OwnerContainerList:
        """A page of the owner's containers, with counts and sums over all of them.

        One repeatable-read snapshot, so the counts, the current sums and the
        page describe the same moment.
        """
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            observed_at = await conn.fetchval("SELECT NOW()")
            count_rows = await conn.fetch(_COUNTS_SQL, user_id)
            running_rows = await conn.fetch(_RUNNING_SQL, user_id)
            rows = await conn.fetch(
                _PAGE_SQL,
                user_id,
                list(ACTIVE_STATES),
                cursor.rank if cursor else None,
                cursor.last_activity_at if cursor else None,
                cursor.id if cursor else None,
                limit + 1,
            )

        counts = dict.fromkeys(CONTAINER_STATES, 0)
        for row in count_rows:
            counts[container_state(row["state"], row["deletion_state"])] += int(row["n"])

        cpu_cores, memory_bytes, measured = 0.0, 0, 0
        for row in running_rows:
            reading = _reading(row["resource_snapshot"], row["resource_sampled_at"], observed_at)
            if (
                reading is None
                or reading.stale
                or reading.cpu_cores is None
                or reading.memory_bytes is None
            ):
                continue
            cpu_cores += reading.cpu_cores
            memory_bytes += reading.memory_bytes
            measured += 1

        containers = []
        for row in rows[:limit]:
            state = container_state(row["state"], row["deletion_state"])
            disk_mib = int(row["disk_limit_mib"] or 0)
            containers.append(
                OwnerContainer(
                    id=row["id"],
                    state=state,
                    repo=row["repo"],
                    chats=[
                        OwnerContainerChat.model_validate(item)
                        for item in load_json(row["items"]) or []
                    ],
                    chat_count=int(row["chat_count"]),
                    cpu_limit_cores=cpu_limit_cores(row["cpu_limit"]),
                    memory_limit_bytes=memory_limit_bytes(row["memory_limit"]),
                    disk_limit_bytes=disk_mib * 1024**2 if disk_mib > 0 else None,
                    # Only a running container's sample describes it now; a
                    # stopping one's is its last moment before the stop.
                    reading=(
                        _reading(row["resource_snapshot"], row["resource_sampled_at"], observed_at)
                        if state == "running"
                        else None
                    ),
                    created_at=row["created_at"],
                    last_activity_at=row["last_activity_at"],
                    idle_deadline=row["idle_deadline"] if state == "running" else None,
                )
            )
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = encode_cursor(
                OwnerContainerCursor(
                    scope=CONTAINER_CURSOR_SCOPE,
                    rank=int(last["rank"]),
                    last_activity_at=last["last_activity_at"],
                    id=last["id"],
                )
            )
        return OwnerContainerList(
            observed_at=observed_at,
            counts=counts,
            total_count=sum(counts.values()),
            current=OwnerCurrentUse(
                cpu_cores=cpu_cores,
                memory_bytes=memory_bytes,
                measured=measured,
                unmeasured=counts["running"] - measured,
            ),
            containers=containers,
            next_cursor=next_cursor,
        )

    async def resource_history(
        self, user_id: str, *, buckets: list[tuple[datetime, datetime]]
    ) -> OwnerResourceHistory:
        """Running time from lifecycle events, and measured use from daily totals."""
        if not buckets:
            raise ValueError("A history needs at least one day.")
        since, until = buckets[0][0], buckets[-1][1]
        days = [start.astimezone(UTC).date() for start, _ in buckets]
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            generated_at = await conn.fetchval("SELECT NOW()")
            running = await conn.fetch(
                _RUNNING_TIME_SQL,
                user_id,
                since,
                until,
                [start for start, _ in buckets],
                [end for _, end in buckets],
                list(RUNNING_STATES),
            )
            measured = await conn.fetch(_MEASURED_SQL, user_id, days)

        result_days = []
        for (start, end), day, run_row, measured_row in zip(
            buckets, days, running, measured, strict=True
        ):
            result_days.append(
                OwnerResourceDay(
                    date=day.isoformat(),
                    since=start,
                    until=end,
                    running_seconds=max(0.0, float(run_row["running_seconds"])),
                    measured_seconds=float(measured_row["measured_seconds"]),
                    cpu_core_seconds=float(measured_row["cpu_core_seconds"]),
                    memory_byte_seconds=float(measured_row["memory_byte_seconds"]),
                )
            )
        return OwnerResourceHistory(
            since=since,
            until=until,
            generated_at=generated_at,
            totals=OwnerResourceTotals(
                running_seconds=sum(day.running_seconds for day in result_days),
                measured_seconds=sum(day.measured_seconds for day in result_days),
                cpu_core_seconds=sum(day.cpu_core_seconds for day in result_days),
                memory_byte_seconds=sum(day.memory_byte_seconds for day in result_days),
            ),
            days=result_days,
        )
