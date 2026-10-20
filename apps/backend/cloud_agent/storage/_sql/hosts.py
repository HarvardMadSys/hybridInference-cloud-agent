"""SQL projections for the host pool.

Lifted verbatim from the single storage class this package replaced. The
text is what it was; only the module that owns it changed, so a statement
that reads these can import them without importing everything else.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from cloud_agent_shared.sandbox_restore import PULLABLE_IMAGE_PATTERN

if TYPE_CHECKING:
    from typing import Any

# Everything a caller may see about a host. ``credential_hash`` is deliberately
# not in it — reading it back out would make the row as good as the credential
# — and is reported only as the boolean ``enrolled``.
HOST_COLUMN_NAMES = (
    "host",
    "compute_node_id",
    "last_worker_id",
    "first_seen_at",
    "last_seen_at",
    "status",
    "labels",
    "capabilities",
    "slots_total",
    "slots_used",
    "slots_desired",
    "launch_error",
    "runner_version",
    "sandbox_manager_url",
    "last_heartbeat_at",
    "drain_fence_token",
    "drain_fence_status_before",
    "drain_fence_active",
    "drain_fence_epoch",
    "removal_requested_at",
    "removal_moves_out",
    "removal_leaves_unmoved",
    "sandbox_runtime",
    "sandbox_image_digest",
    "runtime_versions",
    "runtime_versions_available",
    # A few numbers each, unlike the capacity snapshot below: small enough to
    # ride every projection, and the heartbeat answer needs the server's own
    # values from the row it just wrote.
    "settings_desired",
    "settings_applied",
    "settings_local",
    "sandbox_archive_settings_applied_revision",
)


# The database column is stable so old and new control planes can overlap.
HOST_COLUMNS = (
    ", ".join(
        "broker_url AS sandbox_manager_url" if column == "sandbox_manager_url" else column
        for column in HOST_COLUMN_NAMES
    )
    + ", (credential_hash IS NOT NULL) AS enrolled"
)


# The same list qualified with ``h.``. Required wherever the query joins
# ``agent_attempts``, which has its own ``status`` and ``host_id``: unqualified,
# Postgres rejects the whole statement as ambiguous.
QUALIFIED_HOST_COLUMNS = (
    ", ".join(
        "h.broker_url AS sandbox_manager_url" if column == "sandbox_manager_url" else f"h.{column}"
        for column in HOST_COLUMN_NAMES
    )
    + ", (h.credential_hash IS NOT NULL) AS enrolled"
)


#: The latest host-capacity observation, projected only where it is read.
#:
#: Deliberately *not* part of ``HOST_COLUMN_NAMES``. That list is also what
#: every sandbox manager request resolves a host through — a file read, a terminal, a
#: git operation — and an 8 KiB monitoring snapshot has no business riding
#: along on any of them. The admin pool read and the capacity history task ask
#: for these columns by name; nothing else does.
CAPACITY_COLUMN_NAMES = (
    "capacity_snapshot",
    "capacity_received_at",
    "capacity_sampled_at",
    "capacity_epoch",
)


HOST_CAPACITY_COLUMNS = ", ".join(CAPACITY_COLUMN_NAMES)


#: Whether the machine an attempt ran on may still speak for it. Binds the
#: attempt as ``a``; the caller supplies that alias.
#:
#: A worker token is a stateless signature over the attempt fence. It names no
#: host and carries no expiry, so revoking a host's credential does not, on its
#: own, stop anything that token opens. This predicate is what does: every
#: write that keeps an attempt going — the lease extension, the renewal of its
#: grant, a residency it establishes, a tool call it relays — asks it in the
#: same statement, so a removed or disabled machine stops being able to keep
#: its work alive the moment the revocation commits. ``EXISTS`` rather than a
#: join, so a caller's row count never depends on it.
LIVE_ATTEMPT_HOST = """EXISTS (
    SELECT 1 FROM agent_runner_hosts live_host
    WHERE live_host.host = a.host_id
      AND live_host.credential_hash IS NOT NULL
      AND live_host.status <> 'disabled'
)"""


#: Whether group ``g`` still needs the host it names, so that forgetting the
#: host must wait. Binds the group as ``g``; the caller supplies that alias.
#:
#: A deleted group is an audit row. A ``cold`` group lives in its checkpoint:
#: whichever restore-capable host claims it becomes its host. Until then it
#: keeps naming the host it last ran on (``host`` is never NULL), and nothing
#: reads that name for it: no foreign key ties it or its checkpoint's host to
#: the host row, and the cold claim, the restore it hands out and the
#: confirmation of the restored container do not look at either.
HOST_RETAINED_GROUP = "g.deletion_state <> 'deleted' AND g.state <> 'cold'"


#: Group ``g`` has no deletion in progress or done, as the claim reads it. A
#: deletion that failed before its archive (``failed``) kept the container,
#: and the group serves its chats as one never asked to be deleted: it is
#: counted, moved out, handed over and lost with its machine like any other.
UNDELETED_GROUP_SQL = "g.deletion_state NOT IN ('pending', 'archived', 'deleted')"


def host_claim_ceiling(slots_total: int | None, slots_desired: int | None) -> int | None:
    """The concurrency a host may be asked for: the lower of its two numbers.

    ``slots_total`` is the limit the host says it is serving, rewritten by
    every heartbeat, and ``slots_desired`` is what an operator asked for on the
    admin page. Either may be unset, and ``None`` here means unmetered.

    Taking the lower one is what makes a change safe in both directions. A
    number the host has not yet confirmed admits only what the host has said it
    will run, so a raise can never put work on a machine that has not agreed to
    it; and a number that was lowered stops admitting immediately, without
    waiting for the host to hear about it.
    """
    bounds = [bound for bound in (slots_total, slots_desired) if bound is not None]
    return min(bounds) if bounds else None


def host_slots_exhausted(row: Any) -> bool:
    """Whether a host row holds every slot its effective ceiling allows."""
    ceiling = host_claim_ceiling(row.get("slots_total"), row.get("slots_desired"))
    return ceiling is not None and row["slots_used"] >= ceiling


async def release_host_slot(conn: Any, host: str | None) -> None:
    """Give one slot back, and never take the count below zero.

    Called from inside the transaction that just moved an attempt out of
    ``running``, and only when that move actually changed a row — so it
    runs exactly once per attempt. The ``slots_used > 0`` predicate is the
    second line of defence and the schema CHECK is the third: a counter
    that drifts negative would make a host look permanently over-committed
    and quietly stop it claiming, with nothing in the logs.
    """
    if host is None:
        return
    await conn.execute(
        "UPDATE agent_runner_hosts SET slots_used = slots_used - 1 "
        "WHERE host = $1 AND slots_used > 0",
        host,
    )


async def revoke_host_credentials(conn: Any, hosts: list[str]) -> None:
    """Disable execution identities without deleting hosts or provider machines.

    Callers own admission locks and busy/retention policy; this is the one
    credential transition shared by Host Remove and committed resource release.
    Every caller takes the rows this writes below the host rows first: Host
    Remove itself, a resource release through :func:`lock_hosts_for_revocation`.

    **The work the machine was holding ends with its credential.** Clearing
    the hash stops the host's own requests, but its attempts run on worker
    tokens that name no host, and its resident processes on grants the host
    heartbeat renews. So the same transaction brings forward the expiry of
    every lease the machine holds: its running attempts' and its hot
    residencies'. Nothing is superseded or revoked here — the reaper's next
    pass does that exactly as it does for any lapsed lease, retained-context
    LOST policy and grant revocation included — but from this commit on no
    fenced write, heartbeat, renewal or tool call can succeed for them.
    ``LEAST`` so an already-lapsed lease is never pushed back out.
    """
    await conn.execute(
        "UPDATE agent_runner_hosts "
        "SET status='disabled',credential_hash=NULL,drain_fence_token=NULL,"
        "drain_fence_status_before=NULL,drain_fence_active=FALSE,removal_requested_at=NULL "
        "WHERE host=ANY($1::text[])",
        hosts,
    )
    await conn.execute(
        "UPDATE agent_attempts SET lease_expires_at = LEAST(lease_expires_at, NOW()) "
        "WHERE host_id = ANY($1::text[]) AND status = 'running'",
        hosts,
    )
    await conn.execute(
        """
        UPDATE agent_thread_sandbox_memberships m
        SET residency_lease_expires_at = LEAST(m.residency_lease_expires_at, NOW())
        FROM agent_sandboxes g
        WHERE g.id = m.sandbox_id AND g.host = ANY($1::text[])
          AND m.residency_status = 'hot'
        """,
        hosts,
    )
    await release_covered_sandboxes(conn, hosts)


async def lock_revocation_rows(conn: Any, hosts: list[str]) -> None:
    """Lock what revoking ``hosts`` reads and writes below their host rows.

    Taken before the host rows, in the documented order: the hot memberships
    whose residency leases the revocation ends, then every undeleted group
    the hosts hold. A removal's decision and its hand-over then see the same
    groups. A workspace request that fenced a covered group between the two
    would take it out of the hand-over without a word, and leave it on the
    revoked host with no checkpoint to move it. Locked, the fence waits, and
    then finds the group ``cold``; one that came first leaves the group
    uncovered, which the removal counts as a group that cannot move.
    """
    await conn.execute(
        """
        SELECT m.thread_id FROM agent_thread_sandbox_memberships m
        JOIN agent_sandboxes g ON g.id = m.sandbox_id
        WHERE g.host = ANY($1::text[]) AND m.residency_status = 'hot'
        ORDER BY m.thread_id
        FOR UPDATE OF m
        """,
        hosts,
    )
    await conn.execute(
        "SELECT g.id FROM agent_sandboxes g WHERE g.host = ANY($1::text[]) "
        f"AND {UNDELETED_GROUP_SQL} ORDER BY g.id FOR UPDATE",
        hosts,
    )


async def lock_hosts_for_revocation(conn: Any, hosts: list[str]) -> None:
    """Lock what revoking ``hosts`` writes, the host rows last.

    Their running attempts, then their memberships and groups
    (:func:`lock_revocation_rows`), then the host rows: the order of every
    writer that ends an attempt, which moves the attempt, writes its group and
    gives its slot back on the host row last. A release that locked the host
    rows first and then brought the attempts' leases forward deadlocked with an
    attempt the reaper was ending on that host. Host Remove takes the same rows
    in the same order itself.
    """
    await conn.execute(
        "SELECT id FROM agent_attempts WHERE host_id = ANY($1::text[]) AND status = 'running' "
        "ORDER BY id FOR UPDATE",
        hosts,
    )
    await lock_revocation_rows(conn, hosts)
    await conn.execute(
        "SELECT host FROM agent_runner_hosts WHERE host = ANY($1::text[]) ORDER BY host FOR UPDATE",
        hosts,
    )


#: Whether stopped group ``g`` moves through its checkpoint: the restore point
#: of exactly this container, generation and start, taken from an image another
#: host can pull, recording every member's last settled turn, with no fork or
#: turn in flight. Every hand-over, and every count of what a hand-over would
#: leave behind, uses this one condition.
COVERED_GROUP_SQL = f"""(
    g.state = 'stopped'
    AND {UNDELETED_GROUP_SQL}
    AND g.stop_requested_at IS NULL
    AND EXISTS (
        SELECT 1 FROM agent_sandbox_checkpoints c
        WHERE c.id = g.restore_checkpoint_id
          AND c.sandbox_id = g.id AND c.host = g.host
          AND c.generation = g.generation AND c.container_id = g.container_id
          AND c.start_seq = g.start_seq
          AND c.base_image = g.sandbox_image_digest
          AND c.base_image ~ '{PULLABLE_IMAGE_PATTERN}'
          AND NOT EXISTS (
              SELECT 1 FROM agent_thread_sandbox_memberships m
              WHERE m.sandbox_id = g.id
                AND (
                    m.fork_operation_id IS NOT NULL
                    OR m.active_attempt_id IS NOT NULL
                    OR (
                        m.native_session_id IS NOT NULL
                        AND m.resident_slot_id IS NOT NULL
                        AND NOT c.members @> jsonb_build_array(
                            jsonb_build_object(
                                'thread_id', m.thread_id,
                                'last_settled_job_id', m.last_settled_job_id
                            )
                        )
                    )
                )
          )
    )
)"""

#: A chat of group ``g`` has a turn running: a live Host may have claimed it
#: before its hold began, and that turn reserves the group only after the claim.
GROUP_TURN_CLAIMED_SQL = """EXISTS (
    SELECT 1 FROM agent_thread_sandbox_memberships claimed
    JOIN agent_jobs j ON j.thread_id = claimed.thread_id
    WHERE claimed.sandbox_id = g.id AND j.state = 'running'
)"""

#: How long a Host may stay silent before another Host may take its covered
#: stopped groups. Far longer than ``offline``: a network blip or a restart
#: must not move anything, but a machine that died should not keep its chats
#: waiting for a return that never comes.
SILENT_HOST_HANDOVER_SECONDS = 1800

#: A stopped group ``g`` of a Host silent that long, which another Host may take
#: at its claim: its checkpoint covers it, and no turn of its chats is running.
#: Nothing changes until such a claim, so a Host that returns first, or is the
#: only one, simply continues with its own container.
SILENT_HOST_GROUP_SQL = f"""(
    {COVERED_GROUP_SQL}
    AND NOT {GROUP_TURN_CLAIMED_SQL}
    AND EXISTS (
        SELECT 1 FROM agent_runner_hosts silent
        WHERE silent.host = g.host
          AND COALESCE(silent.last_heartbeat_at, silent.first_seen_at)
              < NOW() - make_interval(secs => {SILENT_HOST_HANDOVER_SECONDS})
    )
)"""


async def take_silent_host_group(conn: Any, *, sandbox_id: str, claimant: str) -> bool:
    """Hand one stopped group of a long-silent Host to the Host claiming it.

    The group becomes ``cold`` inside the claim that restores it, so it moves
    only when another Host actually takes it (``SILENT_HOST_GROUP_SQL``). The
    silent Host keeps its credential: it may only be partitioned, and an
    administrator decides whether it is gone. If it returns, the group is no
    longer its own. The caller holds the group row.
    """
    taken = await conn.fetchval(
        f"""
        UPDATE agent_sandboxes g
        SET state = 'cold', restore_attempts = 0, restore_attempt_id = NULL,
            idle_deadline = NULL, updated_at = NOW()
        WHERE g.id = $1 AND g.host IS DISTINCT FROM $2 AND {SILENT_HOST_GROUP_SQL}
        RETURNING TRUE
        """,
        sandbox_id,
        claimant,
    )
    return bool(taken)


async def release_covered_sandboxes(
    conn: Any,
    hosts: list[str],
    *,
    sandbox_ids: list[str] | None = None,
    live_host: bool = False,
) -> list[str]:
    """Hand every stopped group a checkpoint covers to whichever host claims it.

    The machine is leaving the pool, and with it the only Docker copy of its
    groups. A stopped group whose restore point still covers it
    (``COVERED_GROUP_SQL``) becomes ``cold``: the next turn of any of its chats
    is claimed by a restore-capable host and recreated there. Everything else
    stays bound to this host, as before, and continues only if the same host
    returns.

    ``sandbox_ids`` limits the hand-over to those groups, as a Host moving out
    does once each checkpoint arrives.

    ``live_host`` is for a Host that still runs, as one moving out does. It
    may have claimed a chat's next turn before its hold began, and that turn
    reserves the group only after the claim; such a group is not handed over.
    Callers lock the candidate groups first, so this statement's snapshot sees
    every claim that a group's row lock waited for. A revoked Host's claimed
    turns are requeued instead, and their groups move as before.
    """
    rows = await conn.fetch(
        f"""
        UPDATE agent_sandboxes g
        SET state = 'cold', restore_attempts = 0, restore_attempt_id = NULL,
            idle_deadline = NULL, updated_at = NOW()
        WHERE g.host = ANY($1::text[])
          AND ($2::text[] IS NULL OR g.id = ANY($2::text[]))
          AND {COVERED_GROUP_SQL}
          AND (NOT $3::boolean OR NOT {GROUP_TURN_CLAIMED_SQL})
        RETURNING g.id
        """,
        hosts,
        sandbox_ids,
        live_host,
    )
    return [str(row["id"]) for row in rows]


async def release_late_covered_sandboxes(conn: Any) -> list[str]:
    """Hand on the covered groups of hosts that were already revoked.

    A heartbeat authenticated just before its host was revoked can still be
    writing when the revocation commits. A checkpoint it carries then lands
    after :func:`release_covered_sandboxes` looked, and covers a stopped group
    bound to a host that will never beat again. The same rule moves that group
    on now. A revoked host has no other way to gain a covered group, so this
    moves only what the revocation would have moved had the beat been earlier.
    """
    hosts = [
        str(row["host"])
        for row in await conn.fetch(
            """
            SELECT h.host FROM agent_runner_hosts h
            WHERE h.credential_hash IS NULL AND h.status = 'disabled'
              AND EXISTS (
                  SELECT 1 FROM agent_sandboxes g
                  WHERE g.host = h.host AND g.state = 'stopped'
                    AND g.restore_checkpoint_id IS NOT NULL
              )
            ORDER BY h.host
            """
        )
    ]
    return await release_covered_sandboxes(conn, hosts) if hosts else []


#: Whether group ``g`` waits for a restore from a checkpoint still recorded:
#: its data lives in that checkpoint, not in any container. ``COVERED_GROUP_SQL``
#: checked the checkpoint against the group's exact container when it handed
#: the group over.
RESTORE_POINT_SQL = """EXISTS (
    SELECT 1 FROM agent_sandbox_checkpoints c
    WHERE c.id = g.restore_checkpoint_id AND c.sandbox_id = g.id
      AND c.state IN ('reported', 'verified')
)"""

#: Whether group ``g`` was handed over and lives in its checkpoint: ``cold``,
#: so nothing starts it on the host it still names, with a restore point.
#: Release counts on no container for it (``_retirement_impact``); the
#: container it left behind is removed before the inventory is taken
#: (``SUPERSEDED_CONTAINER_SQL``).
HANDED_OVER_GROUP_SQL = f"(g.state = 'cold' AND {RESTORE_POINT_SQL})"

#: Whether the container that host ``$1`` holds for group ``g`` under
#: generation ``x.generation`` and Docker id ``x.container_id`` may be removed
#: now: nothing will ever start it again, and the group's data no longer
#: depends on it.
#:
#: It never starts again once the group is past its generation (generations
#: only grow, and every start is fenced by one), or once the group is
#: ``cold`` here under that generation and names that container or none: a
#: ``cold`` group is refused every start, and the claim that ends it raises
#: the generation (and refuses this host while it still names the
#: container). A restore that failed here leaves the group ``cold`` with no
#: container. That condition cannot become false again.
#:
#: The data lives elsewhere once a newer generation has a container, or while
#: the group waits for a restore from a checkpoint that is still recorded.
#: That condition can: a group whose last restore fails becomes ``lost``, and
#: a ``lost`` group is never matched, since the container may be the only
#: good copy. So a removal acts only on what :func:`authorize_superseded_removals`
#: recorded, under the group's row lock, before the group became ``lost``.
#: A group being deleted belongs to its deletion. One whose deletion
#: completed (``lost`` and ``deleted``) keeps nothing.
SUPERSEDED_CONTAINER_SQL = f"""(
    g.deletion_state = 'deleted'
    OR (
        g.state <> 'lost'
        AND g.deletion_state NOT IN ('pending', 'archived')
        AND (
            g.generation > x.generation
            OR (
                g.state = 'cold' AND g.host = $1 AND g.generation = x.generation
                AND (g.container_id IS NULL OR g.container_id = x.container_id)
            )
        )
        AND (
            (g.state NOT IN ('cold', 'restoring') AND g.container_id IS NOT NULL)
            OR (g.state IN ('cold', 'restoring') AND {RESTORE_POINT_SQL})
        )
    )
)"""


def superseded_removal_request_id(host: str, container: dict[str, Any]) -> str:
    """The one request id a removal of ``container`` from ``host`` is ever sent under."""
    identity = [host, container["sandbox_id"], container["generation"], container["container_id"]]
    return "superseded-" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()


async def superseded_containers(
    conn: Any, host: str, containers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Of the chat containers ``host`` reports, those :data:`SUPERSEDED_CONTAINER_SQL` matches.

    ``containers`` carries each container's ``sandbox_id``, ``generation``,
    ``container_id`` and ``origin_thread_id`` as the host's Sandbox Manager
    reported them; the group's own origin thread must be the one reported.
    A read only: a removal acts on :func:`authorize_superseded_removals`.
    """
    if not containers:
        return []
    rows = await conn.fetch(
        f"""
        SELECT x.sandbox_id, x.generation, x.container_id, x.origin_thread_id
        FROM unnest($2::text[], $3::bigint[], $4::text[], $5::text[])
            AS x(sandbox_id, generation, container_id, origin_thread_id)
        JOIN agent_sandboxes g
            ON g.id = x.sandbox_id AND g.origin_thread_id = x.origin_thread_id
        WHERE {SUPERSEDED_CONTAINER_SQL}
        ORDER BY x.sandbox_id, x.container_id
        """,
        host,
        [str(item["sandbox_id"]) for item in containers],
        [int(item["generation"]) for item in containers],
        [str(item["container_id"]) for item in containers],
        [str(item["origin_thread_id"]) for item in containers],
    )
    return [
        {
            "sandbox_id": str(row["sandbox_id"]),
            "generation": int(row["generation"]),
            "container_id": str(row["container_id"]),
            "origin_thread_id": str(row["origin_thread_id"]),
        }
        for row in rows
    ]


async def authorize_superseded_removals(
    conn: Any, host: str, containers: list[dict[str, Any]], operation_id: str | None
) -> list[dict[str, Any]]:
    """Record which of the containers ``host`` reports may be removed; return what to remove.

    Each is decided under its group's row lock, which every transition to
    ``lost`` also takes: either the group became ``lost`` first and nothing is
    authorized, or the authorization is recorded first and stands. From then
    on the group counts on its checkpoint alone; a later ``lost`` does not
    take the authorization back, since the removal may already have run.
    Returned are the containers authorized now, those authorized before that
    the host still reports, and every removal on ``host`` not yet confirmed,
    reported or not: one the host carried out whose answer was lost no
    longer shows, and only its receipt completes it. Each comes with the
    ``request_id`` it is removed under, so a retry resends the same request
    whatever the group did since. The caller holds a transaction.
    """
    if not containers:
        return await _authorized_removals(conn, host, [])
    await conn.execute(
        "SELECT id FROM agent_sandboxes WHERE id = ANY($1::text[]) ORDER BY id FOR SHARE",
        sorted({str(item["sandbox_id"]) for item in containers}),
    )
    eligible = await superseded_containers(conn, host, containers)
    if eligible:
        await conn.execute(
            """
            INSERT INTO agent_superseded_container_removals
                (request_id, host, sandbox_id, origin_thread_id, generation, container_id,
                 operation_id)
            SELECT x.request_id, $1, x.sandbox_id, x.origin_thread_id, x.generation,
                   x.container_id, $7
            FROM unnest($2::text[], $3::text[], $4::text[], $5::bigint[], $6::text[])
                AS x(request_id, sandbox_id, origin_thread_id, generation, container_id)
            ON CONFLICT DO NOTHING
            """,
            host,
            [superseded_removal_request_id(host, item) for item in eligible],
            [item["sandbox_id"] for item in eligible],
            [item["origin_thread_id"] for item in eligible],
            [item["generation"] for item in eligible],
            [item["container_id"] for item in eligible],
            operation_id,
        )
    return await _authorized_removals(conn, host, containers)


async def _authorized_removals(
    conn: Any, host: str, containers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The recorded removals on ``host`` of ``containers``, and those not yet confirmed."""
    rows = await conn.fetch(
        """
        SELECT r.request_id, r.sandbox_id, r.generation, r.container_id, r.origin_thread_id
        FROM agent_superseded_container_removals r
        WHERE r.host = $1 AND (
            r.removed_at IS NULL
            OR EXISTS (
                SELECT 1
                FROM unnest($2::text[], $3::bigint[], $4::text[], $5::text[])
                    AS x(sandbox_id, generation, container_id, origin_thread_id)
                WHERE r.sandbox_id = x.sandbox_id AND r.generation = x.generation
                  AND r.container_id = x.container_id
                  AND r.origin_thread_id = x.origin_thread_id
            )
        )
        ORDER BY r.sandbox_id, r.container_id
        """,
        host,
        [str(item["sandbox_id"]) for item in containers],
        [int(item["generation"]) for item in containers],
        [str(item["container_id"]) for item in containers],
        [str(item["origin_thread_id"]) for item in containers],
    )
    return [
        {
            "request_id": str(row["request_id"]),
            "sandbox_id": str(row["sandbox_id"]),
            "generation": int(row["generation"]),
            "container_id": str(row["container_id"]),
            "origin_thread_id": str(row["origin_thread_id"]),
        }
        for row in rows
    ]


#: Whether host ``{host}`` has been revoked (:func:`revoke_host_credentials`)
#: and not enrolled again. ``{host}`` names the host column or parameter.
REVOKED_HOST_SQL = """EXISTS (
    SELECT 1 FROM agent_runner_hosts revoked
    WHERE revoked.host = {host}
      AND revoked.credential_hash IS NULL AND revoked.status = 'disabled'
)"""


#: Whether host ``{host}`` runs on a machine its provider confirmed
#: ``terminated`` and has been silent for ``{quiet}`` seconds: whatever Docker
#: held there went with the machine, and the host will never answer again. The
#: rule the compute reconciler marks such a host's remaining groups lost by.
RECLAIMED_HOST_SQL = """EXISTS (
    SELECT 1 FROM agent_runner_hosts reclaimed
    JOIN agent_compute_nodes reclaimed_node ON reclaimed_node.id = reclaimed.compute_node_id
    JOIN agent_compute_allocations reclaimed_machine
        ON reclaimed_machine.id = reclaimed_node.allocation_id
    WHERE reclaimed.host = {host}
      AND reclaimed_machine.observed_state = 'terminated'
      AND (reclaimed.last_heartbeat_at IS NULL
           OR reclaimed.last_heartbeat_at < NOW() - make_interval(secs => {quiet}))
)"""


async def refuse_start_on_revoked_host(conn: Any, group: Any) -> str | None:
    """Refuse to start ``group`` on a revoked host; ``None`` lets the start go ahead.

    For a caller about to start a stopped container, which holds the group's
    row lock and passes its ``id``, ``state`` and ``host``. Read after that
    lock, the host's revocation is the one any handover of the group saw.

    A revoked host never reports a stop again, so a start there would end the
    cover of the checkpoint that moves the group, for good. A checkpoint that
    landed after the revocation already covers it, and the group is handed on
    now, as the reaper's next pass would do (:func:`release_late_covered_sandboxes`):
    ``"cold"``. Any other group stays with that host, unstarted, so a checkpoint
    still in flight covers it when it lands: ``"host_revoked"``. A ``cold`` or
    ``restoring`` group lives in its checkpoint, whatever host it last named.
    """
    if group["state"] in ("cold", "restoring") or not await conn.fetchval(
        "SELECT " + REVOKED_HOST_SQL.format(host="$1"), group["host"]
    ):
        return None
    if await release_covered_sandboxes(conn, [str(group["host"])], sandbox_ids=[str(group["id"])]):
        return "cold"
    return "host_revoked"


# A Host moving out, in its lease window or because an administrator removes
# it: no claims, and each group stopped and handed to another Host through its
# checkpoint. ``{host}`` names the Host column.
MOVING_OUT_HOST_SQL = """(EXISTS (
    SELECT 1 FROM agent_host_admission_holds move_out
    WHERE move_out.host = {host} AND move_out.purpose = 'move_out'
) OR EXISTS (
    SELECT 1 FROM agent_runner_hosts removing
    WHERE removing.host = {host} AND removing.removal_requested_at IS NOT NULL
      AND removing.removal_moves_out
))"""


async def moving_out_hosts(conn: Any) -> list[str]:
    """Every enrolled Host now moving its groups out."""
    rows = await conn.fetch(
        "SELECT h.host FROM agent_runner_hosts h "
        "WHERE h.credential_hash IS NOT NULL AND "
        + MOVING_OUT_HOST_SQL.format(host="h.host")
        + " ORDER BY h.host"
    )
    return [str(row["host"]) for row in rows]
