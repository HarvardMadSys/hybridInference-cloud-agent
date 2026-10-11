"""SQL projections for the host pool.

Lifted verbatim from the single storage class this package replaced. The
text is what it was; only the module that owns it changed, so a statement
that reads these can import them without importing everything else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
    "broker_url",
    "last_heartbeat_at",
    "drain_fence_token",
    "drain_fence_status_before",
    "drain_fence_active",
    "drain_fence_epoch",
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


HOST_COLUMNS = ", ".join(HOST_COLUMN_NAMES) + ", (credential_hash IS NOT NULL) AS enrolled"


# The same list qualified with ``h.``. Required wherever the query joins
# ``agent_attempts``, which has its own ``status`` and ``host_id``: unqualified,
# Postgres rejects the whole statement as ambiguous.
QUALIFIED_HOST_COLUMNS = (
    ", ".join(f"h.{column}" for column in HOST_COLUMN_NAMES)
    + ", (h.credential_hash IS NOT NULL) AS enrolled"
)


#: The latest host-capacity observation, projected only where it is read.
#:
#: Deliberately *not* part of ``HOST_COLUMN_NAMES``. That list is also what
#: every broker request resolves a host through — a file read, a terminal, a
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
    """
    await conn.execute(
        "UPDATE agent_runner_hosts "
        "SET status='disabled',credential_hash=NULL,drain_fence_token=NULL,"
        "drain_fence_status_before=NULL,drain_fence_active=FALSE "
        "WHERE host=ANY($1::text[])",
        hosts,
    )
