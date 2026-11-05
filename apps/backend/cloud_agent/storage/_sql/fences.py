"""The storage layer's fences: the attempt lease, and the relays' residency caller.

The attempt-lease fence
-----------------------

A worker write is refused unless its attempt is running under the lease
generation the worker was handed and its lease has not expired by the wall
clock (``clock_timestamp()``, never the transaction's ``NOW()``). Where the
clock is read relative to the row lock is part of the fence, so each shape is
named once here:

* :func:`lock_live_attempt` -- lock (``FOR UPDATE``) and judge the lease in one
  statement, the clock read after any wait for the lock.
* :func:`lock_running_attempt` then :func:`attempt_lease_is_live` -- the same
  fence in two statements, for a caller that takes more locks in between and
  must read the clock only after the last of them. The lock mode is the
  caller's (:class:`LockMode`). :func:`attempt_and_residency_are_live` is the
  second step for a caller that also holds a hot residency.
* :func:`leased_attempt_sql` -- the predicate a fenced statement repeats in its
  own snapshot. It takes no lock and reads the clock where the statement runs.

Statements whose fence has its own shape (more joins, the host, the job, a
different lock) spell it inline beside the lock they take.

The residency caller fence
--------------------------

The fence a relay reads before it speaks for a sandbox is one definition for
every relay.

A relay outside the sandbox holds a credential and acts for the turn a
residency placeholder names: the MCP relay
(:class:`~cloud_agent.storage.repositories.credential_queries.PostgresCredentialQueries`)
and the GitHub relay
(:class:`~cloud_agent.storage.repositories.github_relay.PostgresGitHubRelayRepository`).
Both ask the same question, so both build their statements from these
fragments: a change to the fence reaches every relay, or none.

The fence has two halves, and they compose:

* :func:`hot_residency_sql` -- the residency is the chat's current, hot,
  unexpired one, the chat is not deleted, and the attempt runs on the
  residency's own host. :func:`residency_caller_sql` adds which chat and
  ties the job to it.
* :data:`LIVE_ATTEMPT` -- the attempt is its job's current, running one with
  an unexpired lease, the job is running and not asked to stop, and its host
  is still enrolled. It extends :data:`CURRENT_RUNNING_ATTEMPT`.

The GitHub relay reads both halves in one statement
(:func:`live_residency_caller_sql`). The MCP relay reads them in two: the
residency names its attempt under :data:`CURRENT_RUNNING_ATTEMPT` only, and
the attempt is then checked under :data:`LIVE_ATTEMPT` with the residency
re-read beside it (:func:`~cloud_agent.storage._sql.mcp_residency.residency_fence_sql`,
which is built from :func:`hot_residency_sql` too). The split is deliberate:
"this chat has no running turn" and "this turn's credential no longer
works" are different refusals, and the second statement is the one the
relay re-reads after an asynchronous wait.

Every fragment binds the aliases its docstring names; the caller supplies
them. Placeholders are passed in as trusted SQL expressions, never values.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from cloud_agent.storage._sql.hosts import LIVE_ATTEMPT_HOST


class LockMode(StrEnum):
    """The row lock :func:`lock_running_attempt` takes on the attempt."""

    SHARE = "FOR SHARE"
    UPDATE = "FOR UPDATE"


def leased_attempt_sql(attempt_id: str, generation: str, *, alias: str = "a") -> str:
    """Attempt ``alias`` is ``attempt_id``, running under ``generation``, its lease unexpired.

    The in-statement half of the lease fence: no lock, and the clock is read
    where the statement runs. Says nothing of the job, the host or the
    residency; the caller adds what its write needs. An empty ``alias`` names
    the columns bare, for a statement over ``agent_attempts`` alone.
    """
    col = f"{alias}." if alias else ""
    return (
        f"{col}id = {attempt_id} AND {col}lease_generation = {generation}"
        f" AND {col}status = 'running' AND {col}lease_expires_at > clock_timestamp()"
    )


async def lock_live_attempt(conn: Any, attempt_id: int, lease_generation: int | None) -> str | None:
    """Lock a running attempt whose lease is live, and name its job.

    ``None`` when the attempt is not running under ``lease_generation`` (any,
    when that is ``None``) or its lease has expired. One statement: the lock is
    taken in a subquery, and the outer query compares the lease only with the
    rows the lock hands back, so the clock is read after any wait for the lock.
    In the locking query's own select list it would be read before the wait.
    """
    row = await conn.fetchrow(
        """
        WITH locked AS (
            SELECT job_id, lease_expires_at
            FROM agent_attempts
            WHERE id = $1 AND ($2::bigint IS NULL OR lease_generation = $2)
              AND status = 'running'
            FOR UPDATE
        )
        SELECT job_id, lease_expires_at > clock_timestamp() AS live FROM locked
        """,
        attempt_id,
        lease_generation,
    )
    if row is None or not row["live"]:
        return None
    return str(row["job_id"])


async def lock_running_attempt(
    conn: Any, attempt_id: int, lease_generation: int, mode: LockMode
) -> bool:
    """Lock the attempt if it is running under ``lease_generation``; read no clock.

    The first step of the two-statement fence. The caller takes whatever else
    it must lock, then reads the lease with :func:`attempt_lease_is_live` (or
    :func:`attempt_and_residency_are_live`) after the last wait.
    """
    return bool(
        await conn.fetchval(
            "SELECT id FROM agent_attempts "
            f"WHERE id = $1 AND lease_generation = $2 AND status = 'running' {mode}",
            attempt_id,
            lease_generation,
        )
    )


async def attempt_lease_is_live(conn: Any, attempt_id: int) -> bool:
    """Whether the attempt's lease is unexpired now; only after the caller has locked it."""
    return bool(
        await conn.fetchval(
            "SELECT lease_expires_at > clock_timestamp() FROM agent_attempts WHERE id = $1",
            attempt_id,
        )
    )


async def attempt_and_residency_are_live(conn: Any, attempt_id: int, thread_id: str) -> bool:
    """Whether both the attempt's lease and chat ``thread_id``'s residency lease are unexpired.

    The second step of the two-statement fence for a caller holding the
    attempt and the membership locks; the clock is read after both waits.
    """
    return bool(
        await conn.fetchval(
            """
            SELECT a.lease_expires_at > clock_timestamp()
               AND m.residency_lease_expires_at > clock_timestamp()
            FROM agent_attempts a, agent_thread_sandbox_memberships m
            WHERE a.id = $1 AND m.thread_id = $2
            """,
            attempt_id,
            thread_id,
        )
    )


#: The residency, its chat, its group, the attempt it is serving and that
#: attempt's job, as ``m``, ``t``, ``g``, ``a`` and ``j``. The attempt is the one
#: admitted to the membership (``m.active_attempt_id``), never another turn.
RESIDENCY_CALLER_FROM = """agent_thread_sandbox_memberships m
                JOIN agent_threads t ON t.id = m.thread_id
                JOIN agent_sandboxes g ON g.id = m.sandbox_id
                JOIN agent_attempts a ON a.id = m.active_attempt_id
                JOIN agent_jobs j ON j.id = a.job_id"""

#: Attempt ``a`` is its job ``j``'s current one and is running.
CURRENT_RUNNING_ATTEMPT = """a.status = 'running'
                  AND j.current_attempt_id = a.id"""

#: Attempt ``a`` may still act for job ``j``: it is the job's current, running
#: attempt, its lease has not expired by the wall clock, the job is running and
#: has not been asked to stop, and the attempt's host is still enrolled. Stop
#: and host removal change neither the lease nor the job's state at once, which
#: is why both are read here rather than left to the lease.
LIVE_ATTEMPT = f"""{CURRENT_RUNNING_ATTEMPT}
                  AND a.lease_expires_at > clock_timestamp()
                  AND j.state = 'running'
                  AND NOT j.cancel_requested
                  AND {LIVE_ATTEMPT_HOST}"""


def hot_residency_sql(generation: str) -> str:
    """Membership ``m`` is the hot, unexpired residency ``generation`` names, on ``a``'s host.

    Binds ``m``, its chat ``t``, its group ``g`` and the attempt ``a``. The chat
    is not deleted, and the attempt runs on the group's own host, so a
    credential copied out of one sandbox cannot speak for a turn the chat runs
    anywhere else.
    """
    return f"""m.residency_generation = {generation}
                  AND m.residency_status = 'hot'
                  AND m.residency_lease_expires_at > clock_timestamp()
                  AND t.deleted_at IS NULL
                  AND a.host_id = g.host"""


def residency_caller_sql(thread_id: str, generation: str) -> str:
    """Chat ``thread_id``'s residency ``generation`` is hot, and job ``j`` is that chat's.

    Over :data:`RESIDENCY_CALLER_FROM`. Says nothing of the attempt's own
    liveness: add :data:`CURRENT_RUNNING_ATTEMPT` or :data:`LIVE_ATTEMPT`.
    """
    return f"""m.thread_id = {thread_id}
                  AND {hot_residency_sql(generation)}
                  AND j.thread_id = m.thread_id"""


def live_residency_caller_sql(thread_id: str, generation: str) -> str:
    """The whole relay fence in one predicate, over :data:`RESIDENCY_CALLER_FROM`.

    The residency is hot and serving its admitted attempt on its own host, and
    that attempt is :data:`LIVE_ATTEMPT`.
    """
    return f"""{residency_caller_sql(thread_id, generation)}
                  AND {LIVE_ATTEMPT}"""
