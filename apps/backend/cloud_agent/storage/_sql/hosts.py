"""SQL projections for the host pool.

Lifted verbatim from the single storage class this package replaced. The
text is what it was; only the module that owns it changed, so a statement
that reads these can import them without importing everything else.
"""

from __future__ import annotations

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


#: Whether stopped group ``g`` moves through its checkpoint: the restore point
#: of exactly this container, generation and start, taken from an image another
#: host can pull, recording every member's last settled turn, with no fork or
#: turn in flight. Every hand-over, and every count of what a hand-over would
#: leave behind, uses this one condition.
COVERED_GROUP_SQL = f"""(
    g.state = 'stopped'
    AND g.deletion_state = 'none'
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
