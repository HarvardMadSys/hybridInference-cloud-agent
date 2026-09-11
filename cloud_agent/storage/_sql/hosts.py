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
    "last_worker_id",
    "first_seen_at",
    "last_seen_at",
    "status",
    "labels",
    "capabilities",
    "slots_total",
    "slots_used",
    "runner_version",
    "broker_url",
    "last_heartbeat_at",
    "sandbox_runtime",
    "sandbox_image_digest",
    "runtime_versions",
    "runtime_versions_available",
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
