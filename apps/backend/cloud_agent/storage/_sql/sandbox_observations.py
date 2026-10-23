"""What a host heartbeat's census of its retained groups writes, and what it skips.

A host lists every retained group it holds on every beat, stopped ones until
their chats are deleted. One unlocked read takes every listed group with its
members (:func:`observed_groups`), and an observation that would change
nothing there (:func:`observation_changes_nothing`) takes no lock and writes
nothing. That is safe because the read is a snapshot taken after the host
sampled Docker: an observation found to change nothing is one the fenced
reconcile, run at that moment, would have left alone, and a stop or idle
deadline set after the read is the next beat's to observe, as one set after a
fenced transaction is.

The fenced reconcile of the rest takes its steps from here too, each written
only where it changes the row this transaction has locked. A beat that repeats
what is recorded creates no row version and fires no trigger, so a group's or a
member's ``updated_at`` marks its last change, not its last observation; the
host row keeps when the host last reported.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent.storage._sql.hosts import DELETED_GROUP_STATES
from cloud_agent_shared.sandbox_resources import parse_sandbox_resource_sample

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

logger = get_logger(__name__)

#: Every listed group, one row per member (one with no member for a group
#: without any), with what deciding whether its observation changes anything
#: needs. ``$1`` is the listed sandbox ids.
_OBSERVED_GROUPS_SQL = """
SELECT g.id, g.origin_thread_id, g.host, g.generation, g.container_id, g.deletion_state,
       g.state, g.idle_deadline, g.stop_requested_at,
       (g.terminal_count IS NULL AND g.terminal_count_observed_at IS NULL
        AND g.resource_snapshot IS NULL AND g.resource_sampled_at IS NULL
        AND g.resource_received_at IS NULL) AS unmeasured,
       m.thread_id AS member_thread_id, m.native_state, m.context_status,
       m.activation_generation, m.process_generation, m.residency_generation,
       m.resident_state
FROM agent_sandboxes g
LEFT JOIN agent_thread_sandbox_memberships m ON m.sandbox_id = g.id
WHERE g.id = ANY($1::text[])
"""

#: The resident state a stopped group leaves its member in.
_STOPPED_RESIDENT_STATE = """CASE
    WHEN native_state = 'lost' OR context_status = 'lost' THEN 'lost'
    ELSE 'stopped'
END"""

#: Every member of stopped group ``$1`` to its stopped resident state, where
#: it is not already in it.
MEMBERS_STOPPED_SQL = f"""
UPDATE agent_thread_sandbox_memberships
SET resident_state = {_STOPPED_RESIDENT_STATE}, updated_at = NOW()
WHERE sandbox_id = $1 AND resident_state IS DISTINCT FROM {_STOPPED_RESIDENT_STATE}
"""

#: What a reported member makes of membership ``m``. A heartbeat may confirm an
#: equal or newer local process state, but cannot move a durable activation
#: fence backwards or overwrite a process belonging to a newer residency.
_REPORTED_PROCESS_GENERATION = """CASE
    WHEN m.native_state = 'lost' OR m.context_status = 'lost' THEN m.process_generation
    ELSE GREATEST(m.process_generation, reported.process_generation)
END"""
_REPORTED_RESIDENT_STATE = """CASE
    WHEN m.native_state = 'lost' OR m.context_status = 'lost' THEN 'lost'
    WHEN m.residency_generation <= reported.residency_generation
     AND m.process_generation <= reported.process_generation
     AND m.activation_generation <= reported.activation_generation
        THEN reported.resident_state
    ELSE m.resident_state
END"""

#: The members one observation of group ``$1`` reports, in one statement.
#: ``$2``..``$6`` are the thread ids, then their activation, process and
#: residency generations and resident states.
_REPORTED_MEMBERS_SQL = f"""
UPDATE agent_thread_sandbox_memberships m
SET process_generation = {_REPORTED_PROCESS_GENERATION},
    resident_state = {_REPORTED_RESIDENT_STATE},
    updated_at = NOW()
FROM unnest($2::text[], $3::bigint[], $4::bigint[], $5::bigint[], $6::text[])
    AS reported(thread_id, activation_generation, process_generation,
                residency_generation, resident_state)
WHERE m.thread_id = reported.thread_id AND m.sandbox_id = $1
  AND ({_REPORTED_PROCESS_GENERATION} IS DISTINCT FROM m.process_generation
       OR {_REPORTED_RESIDENT_STATE} IS DISTINCT FROM m.resident_state)
"""

_RESIDENT_STATES = frozenset({"stopped", "starting", "live", "restarting", "lost"})

_RESOURCE_COLUMNS = ("resource_snapshot", "resource_sampled_at", "resource_received_at")


async def observed_groups(
    conn: Any, sandbox_ids: Sequence[str]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Read every listed group, and each one's members by thread, in one statement."""
    groups: dict[str, Any] = {}
    members: dict[str, dict[str, Any]] = {}
    for row in await conn.fetch(_OBSERVED_GROUPS_SQL, sorted(set(sandbox_ids))):
        groups[row["id"]] = row
        if row["member_thread_id"] is not None:
            members.setdefault(row["id"], {})[row["member_thread_id"]] = row
    return groups, members


def reported_members(members: Sequence[Mapping[str, Any]]) -> dict[str, tuple[int, int, int, str]]:
    """The members an observation reports, by thread.

    Each is its activation, process and residency generations and its
    resident state. Only the first 128 entries count, and one with no thread
    or with a state no member can be in is ignored. A thread reported twice
    keeps its last report: one statement applies the members, and it would
    apply only one of two rows for the same membership, whichever it met
    first.
    """
    reported: dict[str, tuple[int, int, int, str]] = {}
    for member in members[:128]:
        thread_id = str(member.get("thread_id") or "")
        resident_state = str(member.get("resident_state") or "stopped")
        if not thread_id or resident_state not in _RESIDENT_STATES:
            continue
        reported[thread_id] = (
            int(member.get("activation_generation") or 0),
            int(member.get("process_generation") or 0),
            int(member.get("residency_generation") or 0),
            resident_state,
        )
    return reported


def stored_terminal_count(running: bool, terminal_count: int | None) -> int | None:
    """The terminal count an observation stores: a running container's, when valid."""
    count = terminal_count if running else None
    if count is not None and (
        isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 4096
    ):
        return None
    return count


def _fence_refuses(observation: Mapping[str, Any], host: str, group: Mapping[str, Any]) -> bool:
    """Whether the fenced reconcile turns ``observation`` away before writing anything."""
    return (
        group["origin_thread_id"] != observation["thread_id"]
        or group["host"] != host
        or group["generation"] != observation["sandbox_generation"]
        or group["container_id"] not in {None, observation["container_id"]}
        # Being deleted, or held only by its checkpoint until a restore confirms.
        or group["deletion_state"] in DELETED_GROUP_STATES
        or group["state"] in {"cold", "restoring"}
    )


def _member_moves(member: Mapping[str, Any], reported: tuple[int, int, int, str]) -> bool:
    """Whether a reported member would change membership ``member`` (:data:`_REPORTED_MEMBERS_SQL`)."""
    if member["native_state"] == "lost" or member["context_status"] == "lost":
        return False
    activation, process, residency, resident_state = reported
    if process > member["process_generation"]:
        return True
    return (
        member["residency_generation"] <= residency
        and member["process_generation"] <= process
        and member["activation_generation"] <= activation
        and member["resident_state"] != resident_state
    )


def observation_changes_nothing(
    observation: Mapping[str, Any],
    *,
    host: str,
    group: Mapping[str, Any] | None,
    members: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Whether reconciling ``observation`` against this group would write nothing.

    ``group`` and ``members`` are the listed group and its memberships as
    :func:`observed_groups` found them. Two kinds of observation qualify: one
    the fence refuses, and a stopped container the group already records. That
    is a group stopped, with no idle deadline, stop request or container
    measurement left, every member in the resident state a stop leaves it in,
    and nothing the host reports of a member that would move its process or
    its state. Every running container takes the fenced path: each refreshes
    its terminal census, its idle deadline or both.
    """
    if group is None or _fence_refuses(observation, host, group):
        return True
    if (
        observation["running"]
        or group["container_id"] is None
        or not group["unmeasured"]
        or group["state"] != "stopped"
        or group["idle_deadline"] is not None
        or group["stop_requested_at"] is not None
    ):
        return False
    for member in members.values():
        lost = member["native_state"] == "lost" or member["context_status"] == "lost"
        if member["resident_state"] != ("lost" if lost else "stopped"):
            return False
    return not any(
        thread_id in members and _member_moves(members[thread_id], values)
        for thread_id, values in reported_members(observation.get("members") or []).items()
    )


async def write_observed_container(
    conn: Any, group: Mapping[str, Any], *, container_id: str, running: bool
) -> None:
    """Adopt the observed container where the group records none; unarchive a running one."""
    if group["container_id"] is not None and not (running and group["archived_at"] is not None):
        return
    await conn.execute(
        """
        UPDATE agent_sandboxes
        SET container_id = COALESCE(container_id, $2),
            archived_at = CASE WHEN $3 THEN NULL ELSE archived_at END,
            updated_at = NOW()
        WHERE id = $1
        """,
        group["id"],
        container_id,
        running,
    )


async def record_observed_measurements(
    conn: Any,
    group: Mapping[str, Any],
    *,
    running: bool,
    terminal_count: int | None,
    resource_usage: dict[str, Any] | None,
    terminals: Callable[..., Awaitable[None]],
    resources: Callable[..., Awaitable[None]],
) -> None:
    """Store a container's terminal census and resource sample, each in a savepoint.

    Terminal observations fence administrator stops independently of optional
    monitoring. Resource failures must not roll back a fresh terminal count,
    and neither savepoint can lose lifecycle progress. A count is stamped on
    every beat that carries one, since an administrator's stop needs it fresh;
    clearing what is already clear takes no savepoint. ``terminals`` and
    ``resources`` are the writes, given the connection and the observation.
    """
    if (
        stored_terminal_count(running, terminal_count) is not None
        or group["terminal_count"] is not None
        or group["terminal_count_observed_at"] is not None
    ):
        try:
            async with conn.transaction():
                await terminals(
                    conn, sandbox_id=group["id"], running=running, terminal_count=terminal_count
                )
        except Exception:
            logger.warning("sandbox_terminal_observation_unavailable", exc_info=True)
    # A running container's sample the host could not take keeps the last one.
    if (
        parse_sandbox_resource_sample(resource_usage) is not None
        if running
        else any(group[column] is not None for column in _RESOURCE_COLUMNS)
    ):
        try:
            async with conn.transaction():
                await resources(
                    conn, sandbox_id=group["id"], running=running, resource_usage=resource_usage
                )
        except Exception:
            logger.warning("sandbox_resource_snapshot_unavailable", exc_info=True)


async def write_reported_members(
    conn: Any, sandbox_id: str, members: Sequence[Mapping[str, Any]]
) -> None:
    """Apply what the host reports of each member, in one statement, where it changes it."""
    reported = reported_members(members)
    if not reported:
        return
    activations, processes, residencies, states = zip(*reported.values(), strict=True)
    await conn.execute(
        _REPORTED_MEMBERS_SQL,
        sandbox_id,
        list(reported),
        list(activations),
        list(processes),
        list(residencies),
        list(states),
    )


async def record_running_container(
    conn: Any,
    group: Mapping[str, Any],
    *,
    activity_active: bool | None,
    idle_remaining_seconds: float | None,
    durable_activity: bool,
) -> str:
    """Record that the group's container runs, idling out as its activity says.

    ``durable_activity`` is a member turn, an operation or a capture the
    database holds open, which no stale host sample may re-arm a deadline
    under. The answer is the group's state.
    """
    if group["stop_requested_at"] is not None:
        # A heartbeat is an observation of a still-running container, not
        # cancellation of an administrator's request. Keep admission closed and
        # retry the safe host-side idle check on every subsequent heartbeat.
        return "stopping"
    deadline_sql = "idle_deadline"
    deadline_arg: float | None = None
    if activity_active is True or durable_activity:
        deadline_sql = "NULL"
    elif activity_active is False and idle_remaining_seconds is not None:
        deadline_sql = "clock_timestamp() + make_interval(secs => $2)"
        deadline_arg = idle_remaining_seconds
    elif activity_active is None:
        # A sandbox manager reconstructed this running container without its old
        # monotonic clock. Give a group whose durable deadline was cleared one
        # bounded recovery window, but do not refresh that window on every host
        # heartbeat. Once a real terminal/operation closes, the sandbox manager
        # reports its concrete decreasing remainder through the branch above.
        deadline_sql = (
            "COALESCE(idle_deadline, "
            "clock_timestamp() + make_interval(secs => idle_timeout_seconds))"
        )
    # A running container has moved past any checkpoint of its last stop. Paths
    # that start it fence first; this catches one that did not.
    row = await conn.fetchrow(
        f"""
        UPDATE agent_sandboxes
        SET state = 'running', idle_deadline = {deadline_sql},
            restore_checkpoint_id = NULL, updated_at = NOW()
        WHERE id = $1
          AND (state <> 'running'
               OR idle_deadline IS DISTINCT FROM {deadline_sql}
               OR restore_checkpoint_id IS NOT NULL)
        RETURNING state
        """,
        *((group["id"], deadline_arg) if deadline_arg is not None else (group["id"],)),
    )
    # Unwritten, the group is already running as reported.
    return "running" if row is None else str(row["state"])


async def record_stopped_container(conn: Any, group: Mapping[str, Any]) -> str | None:
    """Record that the group's container stopped, and every member with it.

    A repeated stopped observation may still repair stale member state, but it
    does not ask the host to perform another stop: the answer is the state a
    stop that this observation completed reached, else ``None``.
    """
    if (
        group["state"] == "stopped"
        and group["idle_deadline"] is None
        and group["stop_requested_at"] is None
    ):
        await conn.execute(MEMBERS_STOPPED_SQL, group["id"])
        return None
    row = await conn.fetchrow(
        """
        UPDATE agent_sandboxes
        SET state = 'stopped', idle_deadline = NULL,
            stop_requested_at = NULL, updated_at = NOW()
        WHERE id = $1 AND (
            state IN ('stopped', 'stopping', 'stopped_dirty', 'lost')
            OR (state = 'running' AND idle_deadline IS NOT NULL
                AND idle_deadline <= clock_timestamp())
        )
        RETURNING state
        """,
        group["id"],
    )
    if row is None:
        return None
    await conn.execute(MEMBERS_STOPPED_SQL, group["id"])
    return None if group["state"] == "stopped" else str(row["state"])
