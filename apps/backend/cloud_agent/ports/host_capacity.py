"""Port: what each machine is actually doing -- CPU, memory, containers.

Separate from the pool above on purpose. A host can hold scheduling quota and
still be unusable, and the two have opposite remedies; the operator chart
reads this, the scheduler reads that.

Reading and retention only. Writing a history point is deliberately not here:
a point is kept by the heartbeat that accepted the observation, inside that
request's own transaction, because a sample that is only in the host row is
overwritten by the next heartbeat and anything reading on its own clock would
miss it. The statement lives beside this port's implementation as
``insert_capacity_history_point``.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import Any, Protocol

from cloud_agent.capacity_protocol import CAPACITY_HISTORY_RETENTION_S


class HostCapacityRepository(Protocol):
    """Port: what each machine is actually doing -- CPU, memory, containers."""

    async def prune_capacity_history(
        self, *, older_than_seconds: float = CAPACITY_HISTORY_RETENTION_S, limit: int = 2000
    ) -> int:
        """Delete one bounded batch of history older than the retention window.

        Bounded because this runs beside the reaper and the publisher on the
        same database: an unbounded ``DELETE`` after a control-plane outage
        would hold locks for as long as it took to remove the backlog, and the
        work it delayed is recovering jobs.

        Args:
            older_than_seconds: Retention window.
            limit: Rows this call may remove.

        Returns:
            How many rows were removed.
        """
        ...

    async def host_capacity_history(
        self, *, host: str, range_seconds: int
    ) -> dict[str, Any] | None:
        """Read one host's stored capacity history for the current epoch.

        The epoch and the points come from a single read-only repeatable-read
        view, which is what stops a re-enrollment landing between them from
        returning a window labelled with one generation and filled from
        another.

        Args:
            host: Enrollment name.
            range_seconds: Window length ending at the read's own minute.

        Returns:
            ``{"capacity_epoch", "since", "until", "observed_at", "rows"}``,
            or ``None`` when no such host exists. A host that has never
            reported capacity answers with an empty point list rather than
            ``None``: it exists, and "not measured" is a different answer from
            "not here".
        """
        ...
